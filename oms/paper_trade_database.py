"""Queryable SQLite projection of durable Paper execution records."""

from __future__ import annotations

import hashlib
import json
import math
import queue
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from infrastructure.logger import logger
from infrastructure.paper_trade import validate_paper_trade_database_config


SCHEMA_VERSION = 1
LEGACY_RUN_ID = "legacy-journal-import"


class PaperTradeDatabaseError(RuntimeError):
    """Raised when the configured Paper trade projection cannot start."""


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        default=str,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _finite_float(value, default=None):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _nullable_bool(value):
    if value is None:
        return None
    return int(bool(value))


def _integer(value, default=-1):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


class PaperTradeDatabase:
    """Maintain an idempotent, query-oriented projection of Paper fills."""

    _STOP = object()

    def __init__(self, config: dict, journal) -> None:
        database_config = validate_paper_trade_database_config(config)
        if not database_config.get("enabled", False):
            raise ValueError("paper_trade_database is not enabled")
        if not (
            bool(getattr(journal, "enabled", False))
            and bool(getattr(journal, "fsync_enabled", False))
            and bool(getattr(journal, "integrity_check_enabled", False))
        ):
            raise ValueError(
                "paper_trade_database requires an enabled, fsync-backed, "
                "integrity-checked OMS journal"
            )

        configured_path = str(
            database_config.get("path", "storage/paper/trades.sqlite3") or ""
        ).strip()
        if not configured_path:
            raise ValueError("paper_trade_database.path is required")
        self.path = Path(configured_path).resolve()
        self.journal_path = Path(str(journal.path)).resolve()
        if self.path == self.journal_path:
            raise ValueError(
                "paper_trade_database.path must differ from oms.journal_path"
            )

        self.sqlite_timeout_sec = max(
            0.1,
            float(database_config.get("sqlite_timeout_sec", 5.0) or 5.0),
        )
        self.close_timeout_sec = max(
            1.0,
            float(database_config.get("close_timeout_sec", 10.0) or 10.0),
        )
        queue_capacity = max(
            100,
            int(database_config.get("queue_capacity", 10_000) or 10_000),
        )
        self.run_id = uuid.uuid4().hex
        self.started_at_utc = _utc_now()
        self.config_sha256 = hashlib.sha256(
            _canonical_json(config).encode("utf-8")
        ).hexdigest()
        self.symbols = tuple(
            dict.fromkeys(
                str(symbol or "").strip().upper()
                for symbol in config.get("symbols", [])
                if str(symbol or "").strip()
            )
        )
        paper_config = config.get("paper_trade", {}) or {}
        account_config = config.get("account", {}) or {}
        self.initial_balance_usdt = _finite_float(
            paper_config.get(
                "initial_balance_usdt",
                account_config.get("initial_balance_usdt", 0.0),
            ),
            0.0,
        )

        self._journal = journal
        self._queue = queue.Queue(maxsize=queue_capacity)
        self._health_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._accepting = True
        self._closed = False
        self._close_result = False
        self._healthy = True
        self._last_error = ""
        self._committed_fill_count = 0
        self._backfilled_fill_count = 0
        self._failed_fill_count = 0
        self._thread = None

        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            historical_records = journal.read_all()
            self._bootstrap(historical_records)
            start_seq = journal.append("paper_run_started", self.run_payload())
            if not start_seq:
                raise PaperTradeDatabaseError(
                    "paper_run_started was not committed to the OMS journal"
                )
            self._set_start_journal_seq(start_seq)
        except Exception as exc:
            self._mark_bootstrap_failure(exc)
            if isinstance(exc, (PaperTradeDatabaseError, ValueError)):
                raise
            raise PaperTradeDatabaseError(
                f"Paper trade database startup failed: {type(exc).__name__}:{exc}"
            ) from exc

        self._thread = threading.Thread(
            target=self._writer_main,
            daemon=True,
            name="PaperTradeDatabaseWriter",
        )
        self._thread.start()

    def _connect(self):
        connection = sqlite3.connect(
            str(self.path),
            timeout=self.sqlite_timeout_sec,
        )
        connection.execute(
            f"PRAGMA busy_timeout={int(self.sqlite_timeout_sec * 1000)}"
        )
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _create_schema(self, connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, SCHEMA_VERSION}:
            raise PaperTradeDatabaseError(
                f"Unsupported Paper trade database schema version: {version}"
            )
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS projection_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_runs (
                run_id TEXT PRIMARY KEY,
                started_at_utc TEXT NOT NULL,
                stopped_at_utc TEXT,
                status TEXT NOT NULL,
                clean_shutdown INTEGER,
                shutdown_reason TEXT NOT NULL DEFAULT '',
                config_sha256 TEXT NOT NULL DEFAULT '',
                symbols_json TEXT NOT NULL DEFAULT '[]',
                initial_balance_usdt REAL NOT NULL DEFAULT 0.0,
                journal_path TEXT NOT NULL,
                start_journal_seq INTEGER
            );

            CREATE TABLE IF NOT EXISTS paper_fills (
                journal_seq INTEGER PRIMARY KEY,
                journal_hash TEXT,
                journal_ts_utc TEXT NOT NULL,
                recorded_at_utc TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES paper_runs(run_id),
                execution_id TEXT NOT NULL,
                venue TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                client_oid TEXT NOT NULL,
                exchange_oid TEXT NOT NULL,
                trade_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                fill_qty REAL NOT NULL,
                fill_price REAL NOT NULL,
                fill_notional REAL NOT NULL,
                cum_filled_qty REAL NOT NULL,
                exchange_status TEXT NOT NULL,
                exchange_time REAL,
                commission REAL,
                commission_asset TEXT NOT NULL,
                booked_fee REAL NOT NULL,
                realized_pnl REAL,
                is_maker INTEGER,
                order_type TEXT NOT NULL,
                time_in_force TEXT NOT NULL,
                is_rpi INTEGER NOT NULL,
                fill_model TEXT NOT NULL,
                reduce_only INTEGER NOT NULL,
                pre_status TEXT NOT NULL,
                raw_payload_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_paper_fills_run_seq
                ON paper_fills(run_id, journal_seq);
            CREATE INDEX IF NOT EXISTS idx_paper_fills_symbol_time
                ON paper_fills(symbol, exchange_time);
            CREATE INDEX IF NOT EXISTS idx_paper_fills_execution_id
                ON paper_fills(execution_id);
            CREATE INDEX IF NOT EXISTS idx_paper_fills_client_oid
                ON paper_fills(client_oid);
            """
        )
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

        metadata = dict(
            connection.execute(
                "SELECT key, value FROM projection_metadata"
            ).fetchall()
        )
        product = metadata.get("product")
        if product not in {None, "ChronosHFT.paper_trades"}:
            raise PaperTradeDatabaseError(
                "Configured database belongs to a different application"
            )
        configured_journal = metadata.get("journal_path")
        if configured_journal not in {None, str(self.journal_path)}:
            raise PaperTradeDatabaseError(
                "Configured database is already bound to a different OMS journal"
            )
        connection.executemany(
            "INSERT OR REPLACE INTO projection_metadata(key, value) VALUES (?, ?)",
            (
                ("product", "ChronosHFT.paper_trades"),
                ("schema_version", str(SCHEMA_VERSION)),
                ("journal_path", str(self.journal_path)),
            ),
        )

    def _bootstrap(self, records) -> None:
        detected_at = self.started_at_utc
        with self._connect() as connection:
            self._create_schema(connection)
            check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            if check.lower() != "ok":
                raise PaperTradeDatabaseError(
                    f"Paper trade database integrity check failed: {check}"
                )
            connection.execute(
                """
                UPDATE paper_runs
                SET stopped_at_utc = COALESCE(stopped_at_utc, ?),
                    status = 'interrupted',
                    clean_shutdown = 0,
                    shutdown_reason = CASE
                        WHEN shutdown_reason = ''
                        THEN 'process_ended_without_database_close'
                        ELSE shutdown_reason
                    END
                WHERE status = 'running'
                """,
                (detected_at,),
            )
            for record in records:
                self._project_historical_record(connection, record)
            connection.execute(
                """
                INSERT INTO paper_runs(
                    run_id, started_at_utc, status, clean_shutdown,
                    config_sha256, symbols_json, initial_balance_usdt,
                    journal_path
                ) VALUES (?, ?, 'running', NULL, ?, ?, ?, ?)
                """,
                (
                    self.run_id,
                    self.started_at_utc,
                    self.config_sha256,
                    _canonical_json(self.symbols),
                    self.initial_balance_usdt,
                    str(self.journal_path),
                ),
            )

    def _project_historical_record(self, connection, record: dict) -> None:
        if not isinstance(record, dict):
            return
        kind = str(record.get("kind", "") or "")
        payload = record.get("payload", {})
        if not isinstance(payload, dict):
            return
        try:
            journal_seq = int(record.get("seq", 0) or 0)
        except (TypeError, ValueError):
            return
        if journal_seq <= 0:
            return

        if kind == "paper_run_started":
            self._ensure_recovered_run(
                connection,
                str(payload.get("paper_run_id", "") or ""),
                str(payload.get("started_at_utc", record.get("ts", "")) or ""),
                payload=payload,
                start_journal_seq=journal_seq,
            )
            return
        if kind == "execution_record":
            inserted = self._insert_fill(
                connection,
                journal_seq=journal_seq,
                journal_hash=str(record.get("hash", "") or "") or None,
                journal_ts=str(record.get("ts", "") or "") or _utc_now(),
                payload=payload,
            )
            if inserted:
                self._backfilled_fill_count += 1
            return
        if kind not in {"oms_stopped", "shutdown_cancel_unverified"}:
            return
        run_id = str(payload.get("paper_run_id", "") or "")
        if not run_id:
            return
        clean = kind == "oms_stopped" and bool(payload.get("cancel_verified", False))
        self._ensure_recovered_run(
            connection,
            run_id,
            str(record.get("ts", "") or "") or _utc_now(),
        )
        connection.execute(
            """
            UPDATE paper_runs
            SET stopped_at_utc = ?, status = ?, clean_shutdown = ?,
                shutdown_reason = ?
            WHERE run_id = ?
            """,
            (
                str(record.get("ts", "") or "") or _utc_now(),
                "stopped" if clean else "incomplete",
                int(clean),
                str(payload.get("reason", "") or kind),
                run_id,
            ),
        )

    def _ensure_recovered_run(
        self,
        connection,
        run_id: str,
        started_at_utc: str,
        *,
        payload: dict | None = None,
        start_journal_seq: int | None = None,
    ) -> str:
        run_id = str(run_id or "").strip() or LEGACY_RUN_ID
        payload = payload if isinstance(payload, dict) else {}
        connection.execute(
            """
            INSERT INTO paper_runs(
                run_id, started_at_utc, status, clean_shutdown,
                config_sha256, symbols_json, initial_balance_usdt,
                journal_path, start_journal_seq
            ) VALUES (?, ?, 'interrupted', 0, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                start_journal_seq = COALESCE(
                    paper_runs.start_journal_seq,
                    excluded.start_journal_seq
                )
            """,
            (
                run_id,
                started_at_utc or _utc_now(),
                str(payload.get("config_sha256", "") or ""),
                _canonical_json(payload.get("symbols", [])),
                _finite_float(payload.get("initial_balance_usdt"), 0.0),
                str(self.journal_path),
                start_journal_seq,
            ),
        )
        return run_id

    def _insert_fill(
        self,
        connection,
        *,
        journal_seq: int,
        journal_hash: str | None,
        journal_ts: str,
        payload: dict,
    ) -> bool:
        run_id = self._ensure_recovered_run(
            connection,
            str(payload.get("paper_run_id", "") or ""),
            journal_ts,
        )
        fill_qty = _finite_float(payload.get("fill_qty"), 0.0)
        fill_price = _finite_float(payload.get("fill_price"), 0.0)
        cursor = connection.execute(
            """
            INSERT INTO paper_fills(
                journal_seq, journal_hash, journal_ts_utc, recorded_at_utc,
                run_id, execution_id, venue, strategy_id, client_oid,
                exchange_oid, trade_id, symbol, side, fill_qty, fill_price,
                fill_notional, cum_filled_qty, exchange_status, exchange_time,
                commission, commission_asset, booked_fee, realized_pnl,
                is_maker, order_type, time_in_force, is_rpi, fill_model,
                reduce_only, pre_status, raw_payload_json
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(journal_seq) DO NOTHING
            """,
            (
                int(journal_seq),
                journal_hash,
                journal_ts,
                _utc_now(),
                run_id,
                str(payload.get("execution_id", "") or ""),
                str(payload.get("venue", "") or ""),
                str(payload.get("strategy_id", "") or ""),
                str(payload.get("client_oid", "") or ""),
                str(payload.get("exchange_oid", "") or ""),
                _integer(payload.get("trade_id", -1), -1),
                str(payload.get("symbol", "") or "").upper(),
                str(payload.get("side", "") or "").upper(),
                fill_qty,
                fill_price,
                fill_qty * fill_price,
                _finite_float(payload.get("cum_filled_qty"), 0.0),
                str(payload.get("exchange_status", "") or ""),
                _finite_float(payload.get("exchange_time")),
                _finite_float(payload.get("commission")),
                str(payload.get("commission_asset", "") or ""),
                _finite_float(payload.get("booked_fee"), 0.0),
                _finite_float(payload.get("realized_pnl")),
                _nullable_bool(payload.get("is_maker")),
                str(payload.get("order_type", "") or ""),
                str(payload.get("time_in_force", "") or ""),
                int(bool(payload.get("is_rpi", False))),
                str(payload.get("fill_model", "") or "unknown"),
                int(bool(payload.get("reduce_only", False))),
                str(payload.get("pre_status", "") or ""),
                _canonical_json(payload),
            ),
        )
        return cursor.rowcount == 1

    def _set_start_journal_seq(self, sequence: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE paper_runs SET start_journal_seq = ? WHERE run_id = ?",
                (int(sequence), self.run_id),
            )

    def _mark_bootstrap_failure(self, exc: Exception) -> None:
        try:
            if not self.path.exists():
                return
            with self._connect() as connection:
                self._create_schema(connection)
                connection.execute(
                    """
                    UPDATE paper_runs
                    SET stopped_at_utc = ?, status = 'incomplete',
                        clean_shutdown = 0, shutdown_reason = ?
                    WHERE run_id = ?
                    """,
                    (
                        _utc_now(),
                        f"database_bootstrap_failed:{type(exc).__name__}:{exc}",
                        self.run_id,
                    ),
                )
        except Exception:
            return

    def run_payload(self) -> dict:
        return {
            "paper_run_id": self.run_id,
            "started_at_utc": self.started_at_utc,
            "config_sha256": self.config_sha256,
            "symbols": list(self.symbols),
            "initial_balance_usdt": self.initial_balance_usdt,
            "database_path": str(self.path),
        }

    def record_execution(
        self,
        journal_seq: int,
        payload: dict,
        *,
        journal_ts: str = "",
        journal_hash: str = "",
    ) -> bool:
        with self._close_lock:
            if not self._accepting or self._closed:
                return False
            item = (
                int(journal_seq),
                str(journal_ts or "") or _utc_now(),
                str(journal_hash or "") or None,
                dict(payload),
            )
            try:
                self._queue.put_nowait(item)
                return True
            except queue.Full:
                self._set_failure("projection_queue_full")
                logger.critical(
                    "[PaperTradeDatabase] Projection queue is full; the durable "
                    "OMS journal will be backfilled on the next start"
                )
                return False

    def _writer_main(self) -> None:
        try:
            connection = self._connect()
        except Exception as exc:
            self._set_failure(f"writer_connect_failed:{type(exc).__name__}:{exc}")
            logger.critical(
                "[PaperTradeDatabase] Writer connection failed: "
                f"{type(exc).__name__}:{exc}"
            )
            self._drain_without_writing()
            return

        try:
            while True:
                item = self._queue.get()
                try:
                    if item is self._STOP:
                        return
                    journal_seq, journal_ts, journal_hash, payload = item
                    with connection:
                        inserted = self._insert_fill(
                            connection,
                            journal_seq=journal_seq,
                            journal_hash=journal_hash,
                            journal_ts=journal_ts,
                            payload=payload,
                        )
                    if inserted:
                        with self._health_lock:
                            self._committed_fill_count += 1
                except Exception as exc:
                    self._set_failure(
                        f"fill_projection_failed:{type(exc).__name__}:{exc}"
                    )
                    logger.critical(
                        "[PaperTradeDatabase] Fill projection failed; the OMS "
                        "journal remains authoritative and will be backfilled: "
                        f"{type(exc).__name__}:{exc}"
                    )
                finally:
                    self._queue.task_done()
        finally:
            connection.close()

    def _drain_without_writing(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._STOP:
                    return
                with self._health_lock:
                    self._failed_fill_count += 1
            finally:
                self._queue.task_done()

    def _set_failure(self, reason: str) -> None:
        with self._health_lock:
            self._healthy = False
            self._last_error = str(reason)
            self._failed_fill_count += 1

    def close(self, *, clean_shutdown: bool, reason: str = "") -> bool:
        with self._close_lock:
            if self._closed:
                return self._close_result
            self._accepting = False
            try:
                self._queue.put(self._STOP, timeout=self.close_timeout_sec)
            except queue.Full:
                self._set_failure("projection_queue_did_not_drain")

            thread = self._thread
            if thread is not None:
                thread.join(self.close_timeout_sec)
            drained = bool(thread is None or not thread.is_alive())
            if not drained:
                self._set_failure("projection_writer_stop_timeout")

            with self._health_lock:
                healthy = self._healthy
            database_clean = bool(clean_shutdown and drained and healthy)
            status = "stopped" if database_clean else "incomplete"
            try:
                with self._connect() as connection:
                    connection.execute(
                        """
                        UPDATE paper_runs
                        SET stopped_at_utc = ?, status = ?, clean_shutdown = ?,
                            shutdown_reason = ?
                        WHERE run_id = ?
                        """,
                        (
                            _utc_now(),
                            status,
                            int(database_clean),
                            str(reason or "oms_stop"),
                            self.run_id,
                        ),
                    )
            except Exception as exc:
                self._set_failure(
                    f"run_close_failed:{type(exc).__name__}:{exc}"
                )
                database_clean = False

            with self._health_lock:
                close_healthy = self._healthy
            self._closed = True
            self._close_result = bool(drained and close_healthy)
            return self._close_result

    def health_snapshot(self) -> dict:
        with self._health_lock:
            return {
                "enabled": True,
                "healthy": self._healthy,
                "path": str(self.path),
                "run_id": self.run_id,
                "queue_depth": self._queue.qsize(),
                "committed_fill_count": self._committed_fill_count,
                "backfilled_fill_count": self._backfilled_fill_count,
                "failed_fill_count": self._failed_fill_count,
                "last_error": self._last_error,
                "closed": self._closed,
            }
