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


SCHEMA_VERSION = 3
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


def _nullable_integer(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
        self.strategy_sample_interval_sec = max(
            0.1,
            float(
                database_config.get("strategy_sample_interval_sec", 1.0)
                or 1.0
            ),
        )
        self.account_sample_interval_sec = max(
            0.1,
            float(
                database_config.get("account_sample_interval_sec", 1.0)
                or 1.0
            ),
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
        self._sample_lock = threading.Lock()
        self._last_strategy_sample_time: dict[str, float] = {}
        self._last_account_sample_time: float | None = None
        self._accepting = True
        self._closed = False
        self._close_result = False
        self._healthy = True
        self._last_error = ""
        self._committed_fill_count = 0
        self._committed_order_event_count = 0
        self._committed_strategy_sample_count = 0
        self._committed_markout_count = 0
        self._committed_account_sample_count = 0
        self._committed_system_event_count = 0
        self._throttled_strategy_sample_count = 0
        self._throttled_account_sample_count = 0
        self._backfilled_fill_count = 0
        self._failed_fill_count = 0
        self._failed_observation_count = 0
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
        if version not in {0, 1, 2, SCHEMA_VERSION}:
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
                fill_trigger TEXT NOT NULL DEFAULT '',
                market_trade_id INTEGER,
                market_trade_price REAL,
                market_trade_qty REAL,
                market_trade_exchange_time REAL,
                market_trade_received_time REAL,
                market_trade_clock_offset_ms REAL,
                market_trade_transport_latency_ms REAL,
                market_trade_local_age_ms REAL,
                queue_ahead_before REAL,
                best_bid_at_fill REAL,
                best_ask_at_fill REAL,
                mid_at_fill REAL,
                quote_age_ms REAL,
                raw_payload_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_order_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at_utc TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES paper_runs(run_id),
                client_oid TEXT NOT NULL,
                exchange_oid TEXT NOT NULL,
                symbol TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                side TEXT NOT NULL,
                status TEXT NOT NULL,
                price REAL NOT NULL,
                quantity REAL NOT NULL,
                filled_quantity REAL NOT NULL,
                average_price REAL NOT NULL,
                time_in_force TEXT NOT NULL,
                is_post_only INTEGER NOT NULL,
                is_rpi INTEGER NOT NULL,
                order_type TEXT NOT NULL DEFAULT '',
                reduce_only INTEGER NOT NULL DEFAULT 0,
                tag TEXT NOT NULL DEFAULT '',
                created_monotonic REAL,
                updated_monotonic REAL,
                event_time REAL,
                error_message TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_strategy_samples (
                sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at_utc TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES paper_runs(run_id),
                sample_time REAL,
                symbol TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                state TEXT NOT NULL,
                mode TEXT NOT NULL,
                mid_price REAL,
                best_bid REAL,
                best_ask REAL,
                best_bid_qty REAL,
                best_ask_qty REAL,
                fair_value REAL,
                alpha_bps REAL,
                target_bid REAL,
                target_ask REAL,
                market_spread_bps REAL,
                quote_spread_bps REAL,
                bid_quote_qty REAL,
                ask_quote_qty REAL,
                position_qty REAL,
                position_notional REAL,
                sigma_bps REAL,
                A_per_s REAL,
                k_per_bps REAL,
                bid_markout_cost_bps REAL,
                ask_markout_cost_bps REAL,
                bid_flow_cost_bps REAL,
                ask_flow_cost_bps REAL,
                signed_trade_imbalance REAL,
                microprice_offset_bps REAL,
                bid_queue_latency_cost_bps REAL,
                ask_queue_latency_cost_bps REAL,
                bid_stale_depth_bps REAL,
                ask_stale_depth_bps REAL,
                bid_stale_at_risk INTEGER,
                ask_stale_at_risk INTEGER,
                bid_size_multiplier REAL,
                ask_size_multiplier REAL,
                orderbook_exchange_time REAL,
                orderbook_received_time REAL,
                orderbook_corrected_received_time REAL,
                orderbook_dispatch_time REAL,
                orderbook_received_monotonic REAL,
                orderbook_dispatch_monotonic REAL,
                strategy_callback_monotonic REAL,
                clock_offset_ms REAL,
                transport_latency_ms REAL,
                gateway_processing_latency_ms REAL,
                strategy_queue_latency_ms REAL,
                callback_age_ms REAL,
                strategy_compute_latency_ms REAL
            );

            CREATE TABLE IF NOT EXISTS paper_fill_markouts (
                markout_id INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at_utc TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES paper_runs(run_id),
                client_oid TEXT NOT NULL,
                trade_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                fill_price REAL NOT NULL,
                horizon_ms INTEGER NOT NULL,
                mid_price REAL NOT NULL,
                signed_markout_bps REAL NOT NULL,
                fill_observed_monotonic REAL NOT NULL,
                mid_observed_monotonic REAL NOT NULL,
                observation_lag_ms REAL NOT NULL,
                UNIQUE(run_id, client_oid, trade_id, horizon_ms)
            );

            CREATE TABLE IF NOT EXISTS paper_account_samples (
                sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at_utc TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES paper_runs(run_id),
                sample_time REAL,
                balance REAL NOT NULL,
                equity REAL NOT NULL,
                unrealized_pnl REAL NOT NULL,
                available REAL NOT NULL,
                used_margin REAL NOT NULL,
                budget_balance REAL NOT NULL,
                budget_available REAL NOT NULL,
                maintenance_margin REAL NOT NULL,
                margin_balance REAL NOT NULL,
                maintenance_margin_ratio REAL NOT NULL,
                margin_snapshot_time REAL,
                margin_snapshot_synced INTEGER NOT NULL,
                external_cash_flow_total REAL NOT NULL,
                cash_flow_snapshot_time REAL,
                cash_flow_snapshot_synced INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_system_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at_utc TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES paper_runs(run_id),
                event_time REAL,
                event_kind TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                state TEXT NOT NULL,
                total_exposure REAL,
                margin_ratio REAL,
                order_count_local INTEGER,
                order_count_remote INTEGER,
                cancelling_count INTEGER,
                fill_ratio REAL,
                api_weight INTEGER,
                is_sync_error INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_paper_fills_run_seq
                ON paper_fills(run_id, journal_seq);
            CREATE INDEX IF NOT EXISTS idx_paper_fills_symbol_time
                ON paper_fills(symbol, exchange_time);
            CREATE INDEX IF NOT EXISTS idx_paper_fills_execution_id
                ON paper_fills(execution_id);
            CREATE INDEX IF NOT EXISTS idx_paper_fills_client_oid
                ON paper_fills(client_oid);
            CREATE INDEX IF NOT EXISTS idx_paper_order_events_run_symbol
                ON paper_order_events(run_id, symbol, event_id);
            CREATE INDEX IF NOT EXISTS idx_paper_order_events_client_oid
                ON paper_order_events(run_id, client_oid, event_id);
            CREATE INDEX IF NOT EXISTS idx_paper_strategy_samples_run_symbol_time
                ON paper_strategy_samples(run_id, symbol, sample_time);
            CREATE INDEX IF NOT EXISTS idx_paper_markouts_run_symbol_horizon
                ON paper_fill_markouts(run_id, symbol, horizon_ms);
            CREATE INDEX IF NOT EXISTS idx_paper_account_samples_run_time
                ON paper_account_samples(run_id, sample_time);
            CREATE INDEX IF NOT EXISTS idx_paper_system_events_run_time
                ON paper_system_events(run_id, event_time);
            """
        )
        self._migrate_to_v3(connection)
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

    @staticmethod
    def _migrate_to_v3(connection) -> None:
        fill_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(paper_fills)")
        }
        fill_additions = {
            "fill_trigger": "TEXT NOT NULL DEFAULT ''",
            "market_trade_id": "INTEGER",
            "market_trade_price": "REAL",
            "market_trade_qty": "REAL",
            "market_trade_exchange_time": "REAL",
            "market_trade_received_time": "REAL",
            "market_trade_clock_offset_ms": "REAL",
            "market_trade_transport_latency_ms": "REAL",
            "market_trade_local_age_ms": "REAL",
            "queue_ahead_before": "REAL",
            "best_bid_at_fill": "REAL",
            "best_ask_at_fill": "REAL",
            "mid_at_fill": "REAL",
            "quote_age_ms": "REAL",
        }
        for column, declaration in fill_additions.items():
            if column in fill_columns:
                continue
            connection.execute(
                f"ALTER TABLE paper_fills ADD COLUMN {column} {declaration}"
            )

        additions_by_table = {
            "paper_order_events": {
                "order_type": "TEXT NOT NULL DEFAULT ''",
                "reduce_only": "INTEGER NOT NULL DEFAULT 0",
                "tag": "TEXT NOT NULL DEFAULT ''",
            },
            "paper_strategy_samples": {
                "best_bid_qty": "REAL",
                "best_ask_qty": "REAL",
                "orderbook_exchange_time": "REAL",
                "orderbook_received_time": "REAL",
                "orderbook_corrected_received_time": "REAL",
                "orderbook_dispatch_time": "REAL",
                "orderbook_received_monotonic": "REAL",
                "orderbook_dispatch_monotonic": "REAL",
                "strategy_callback_monotonic": "REAL",
                "clock_offset_ms": "REAL",
                "transport_latency_ms": "REAL",
                "gateway_processing_latency_ms": "REAL",
                "strategy_queue_latency_ms": "REAL",
                "callback_age_ms": "REAL",
                "strategy_compute_latency_ms": "REAL",
            },
        }
        for table, additions in additions_by_table.items():
            existing = {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for column, declaration in additions.items():
                if column in existing:
                    continue
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
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
                reduce_only, pre_status, fill_trigger, market_trade_id,
                market_trade_price, market_trade_qty,
                market_trade_exchange_time, market_trade_received_time,
                market_trade_clock_offset_ms,
                market_trade_transport_latency_ms,
                market_trade_local_age_ms, queue_ahead_before,
                best_bid_at_fill, best_ask_at_fill, mid_at_fill, quote_age_ms,
                raw_payload_json
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?
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
                str(payload.get("fill_trigger", "") or ""),
                _integer(payload.get("market_trade_id"), -1)
                if payload.get("market_trade_id") is not None
                else None,
                _finite_float(payload.get("market_trade_price")),
                _finite_float(payload.get("market_trade_qty")),
                _finite_float(payload.get("market_trade_exchange_time")),
                _finite_float(payload.get("market_trade_received_time")),
                _finite_float(payload.get("market_trade_clock_offset_ms")),
                _finite_float(
                    payload.get("market_trade_transport_latency_ms")
                ),
                _finite_float(payload.get("market_trade_local_age_ms")),
                _finite_float(payload.get("queue_ahead_before")),
                _finite_float(payload.get("best_bid_at_fill")),
                _finite_float(payload.get("best_ask_at_fill")),
                _finite_float(payload.get("mid_at_fill")),
                _finite_float(payload.get("quote_age_ms")),
                _canonical_json(payload),
            ),
        )
        return cursor.rowcount == 1

    def _insert_order_event(self, connection, payload: dict) -> bool:
        cursor = connection.execute(
            """
            INSERT INTO paper_order_events(
                recorded_at_utc, run_id, client_oid, exchange_oid, symbol,
                strategy_id, side, status, price, quantity, filled_quantity,
                average_price, time_in_force, is_post_only, is_rpi,
                order_type, reduce_only, tag, created_monotonic,
                updated_monotonic, event_time,
                error_message
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?
            )
            """,
            (
                _utc_now(),
                self.run_id,
                str(payload.get("client_oid", "") or ""),
                str(payload.get("exchange_oid", "") or ""),
                str(payload.get("symbol", "") or "").upper(),
                str(payload.get("strategy_id", "") or ""),
                str(payload.get("side", "") or "").upper(),
                str(payload.get("status", "") or "").upper(),
                _finite_float(payload.get("price"), 0.0),
                _finite_float(payload.get("quantity"), 0.0),
                _finite_float(payload.get("filled_quantity"), 0.0),
                _finite_float(payload.get("average_price"), 0.0),
                str(payload.get("time_in_force", "") or ""),
                int(bool(payload.get("is_post_only", False))),
                int(bool(payload.get("is_rpi", False))),
                str(payload.get("order_type", "") or ""),
                int(bool(payload.get("reduce_only", False))),
                str(payload.get("tag", "") or ""),
                _finite_float(payload.get("created_monotonic")),
                _finite_float(payload.get("updated_monotonic")),
                _finite_float(payload.get("event_time")),
                str(payload.get("error_message", "") or ""),
            ),
        )
        return cursor.rowcount == 1

    def _insert_strategy_sample(self, connection, payload: dict) -> bool:
        params = payload.get("params", {})
        params = params if isinstance(params, dict) else {}
        adaptive = params.get("adaptive", {})
        adaptive = adaptive if isinstance(adaptive, dict) else {}
        markout = adaptive.get("markout", {})
        markout = markout if isinstance(markout, dict) else {}
        markout_sides = markout.get("sides", {})
        markout_sides = markout_sides if isinstance(markout_sides, dict) else {}
        bid_markout = markout_sides.get("BUY", {})
        bid_markout = bid_markout if isinstance(bid_markout, dict) else {}
        ask_markout = markout_sides.get("SELL", {})
        ask_markout = ask_markout if isinstance(ask_markout, dict) else {}
        flow = adaptive.get("flow_toxicity", {})
        flow = flow if isinstance(flow, dict) else {}
        bid_queue = adaptive.get("bid_queue", {})
        bid_queue = bid_queue if isinstance(bid_queue, dict) else {}
        ask_queue = adaptive.get("ask_queue", {})
        ask_queue = ask_queue if isinstance(ask_queue, dict) else {}
        stale = params.get("stale_quote_guard", {})
        stale = stale if isinstance(stale, dict) else {}
        sizing = params.get("size_optimization", {})
        sizing = sizing if isinstance(sizing, dict) else {}
        timing = params.get("market_data_timing", {})
        timing = timing if isinstance(timing, dict) else {}

        cursor = connection.execute(
            """
            INSERT INTO paper_strategy_samples(
                recorded_at_utc, run_id, sample_time, symbol, strategy_id,
                state, mode, mid_price, best_bid, best_ask,
                best_bid_qty, best_ask_qty, fair_value,
                alpha_bps, target_bid, target_ask, market_spread_bps,
                quote_spread_bps, bid_quote_qty, ask_quote_qty, position_qty,
                position_notional, sigma_bps, A_per_s, k_per_bps,
                bid_markout_cost_bps, ask_markout_cost_bps,
                bid_flow_cost_bps, ask_flow_cost_bps,
                signed_trade_imbalance, microprice_offset_bps,
                bid_queue_latency_cost_bps, ask_queue_latency_cost_bps,
                bid_stale_depth_bps, ask_stale_depth_bps,
                bid_stale_at_risk, ask_stale_at_risk,
                bid_size_multiplier, ask_size_multiplier,
                orderbook_exchange_time, orderbook_received_time,
                orderbook_corrected_received_time,
                orderbook_dispatch_time, orderbook_received_monotonic,
                orderbook_dispatch_monotonic, strategy_callback_monotonic,
                clock_offset_ms, transport_latency_ms,
                gateway_processing_latency_ms, strategy_queue_latency_ms,
                callback_age_ms, strategy_compute_latency_ms
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?
            )
            """,
            (
                _utc_now(),
                self.run_id,
                _finite_float(payload.get("sample_time")),
                str(payload.get("symbol", "") or "").upper(),
                str(params.get("strategy", "") or ""),
                str(params.get("state", params.get("State", "")) or ""),
                str(params.get("mode", params.get("Mode", "")) or ""),
                _finite_float(params.get("mid_price")),
                _finite_float(params.get("best_bid")),
                _finite_float(params.get("best_ask")),
                _finite_float(timing.get("best_bid_qty")),
                _finite_float(timing.get("best_ask_qty")),
                _finite_float(payload.get("fair_value", params.get("fair_value"))),
                _finite_float(payload.get("alpha_bps", params.get("alpha_bps"))),
                _finite_float(params.get("target_bid")),
                _finite_float(params.get("target_ask")),
                _finite_float(params.get("market_spread_bps")),
                _finite_float(params.get("quote_spread_bps")),
                _finite_float(params.get("bid_quote_qty")),
                _finite_float(params.get("ask_quote_qty")),
                _finite_float(params.get("position_qty")),
                _finite_float(params.get("position_notional")),
                _finite_float(params.get("sigma_bps")),
                _finite_float(params.get("A_per_s")),
                _finite_float(params.get("k_per_bps")),
                _finite_float(bid_markout.get("adverse_cost_bps")),
                _finite_float(ask_markout.get("adverse_cost_bps")),
                _finite_float(flow.get("bid_adverse_cost_bps")),
                _finite_float(flow.get("ask_adverse_cost_bps")),
                _finite_float(flow.get("signed_trade_imbalance")),
                _finite_float(flow.get("microprice_offset_bps")),
                _finite_float(bid_queue.get("latency_cost_bps")),
                _finite_float(ask_queue.get("latency_cost_bps")),
                _finite_float(stale.get("bid_depth_bps")),
                _finite_float(stale.get("ask_depth_bps")),
                _nullable_bool(stale.get("bid_at_risk")),
                _nullable_bool(stale.get("ask_at_risk")),
                _finite_float(sizing.get("bid_multiplier")),
                _finite_float(sizing.get("ask_multiplier")),
                _finite_float(timing.get("exchange_timestamp")),
                _finite_float(timing.get("received_timestamp")),
                _finite_float(timing.get("corrected_received_timestamp")),
                _finite_float(timing.get("dispatch_timestamp")),
                _finite_float(timing.get("received_monotonic")),
                _finite_float(timing.get("dispatch_monotonic")),
                _finite_float(timing.get("callback_monotonic")),
                _finite_float(timing.get("clock_offset_ms")),
                _finite_float(timing.get("transport_latency_ms")),
                _finite_float(
                    timing.get("gateway_processing_latency_ms")
                ),
                _finite_float(timing.get("strategy_queue_latency_ms")),
                _finite_float(timing.get("callback_age_ms")),
                _finite_float(timing.get("strategy_compute_latency_ms")),
            ),
        )
        return cursor.rowcount == 1

    def _insert_markout(self, connection, payload: dict) -> bool:
        cursor = connection.execute(
            """
            INSERT INTO paper_fill_markouts(
                recorded_at_utc, run_id, client_oid, trade_id, symbol, side,
                fill_price, horizon_ms, mid_price, signed_markout_bps,
                fill_observed_monotonic, mid_observed_monotonic,
                observation_lag_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, client_oid, trade_id, horizon_ms) DO NOTHING
            """,
            (
                _utc_now(),
                self.run_id,
                str(payload.get("client_oid", "") or ""),
                str(payload.get("trade_id", "") or ""),
                str(payload.get("symbol", "") or "").upper(),
                str(payload.get("side", "") or "").upper(),
                _finite_float(payload.get("fill_price"), 0.0),
                _integer(payload.get("horizon_ms"), 0),
                _finite_float(payload.get("mid_price"), 0.0),
                _finite_float(payload.get("signed_markout_bps"), 0.0),
                _finite_float(payload.get("fill_observed_monotonic"), 0.0),
                _finite_float(payload.get("mid_observed_monotonic"), 0.0),
                _finite_float(payload.get("observation_lag_ms"), 0.0),
            ),
        )
        return cursor.rowcount == 1

    def _insert_account_sample(self, connection, payload: dict) -> bool:
        balance = _finite_float(payload.get("balance"), 0.0)
        equity = _finite_float(payload.get("equity"), 0.0)
        cursor = connection.execute(
            """
            INSERT INTO paper_account_samples(
                recorded_at_utc, run_id, sample_time, balance, equity,
                unrealized_pnl, available, used_margin, budget_balance,
                budget_available, maintenance_margin, margin_balance,
                maintenance_margin_ratio, margin_snapshot_time,
                margin_snapshot_synced, external_cash_flow_total,
                cash_flow_snapshot_time, cash_flow_snapshot_synced
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                _utc_now(),
                self.run_id,
                _finite_float(payload.get("sample_time")),
                balance,
                equity,
                _finite_float(
                    payload.get("unrealized_pnl"),
                    equity - balance,
                ),
                _finite_float(payload.get("available"), 0.0),
                _finite_float(payload.get("used_margin"), 0.0),
                _finite_float(payload.get("budget_balance"), 0.0),
                _finite_float(payload.get("budget_available"), 0.0),
                _finite_float(payload.get("maintenance_margin"), 0.0),
                _finite_float(payload.get("margin_balance"), 0.0),
                _finite_float(
                    payload.get("maintenance_margin_ratio"),
                    0.0,
                ),
                _finite_float(payload.get("margin_snapshot_time")),
                int(bool(payload.get("margin_snapshot_synced", False))),
                _finite_float(
                    payload.get("external_cash_flow_total"),
                    0.0,
                ),
                _finite_float(payload.get("cash_flow_snapshot_time")),
                int(bool(payload.get("cash_flow_snapshot_synced", False))),
            ),
        )
        return cursor.rowcount == 1

    def _insert_system_event(self, connection, payload: dict) -> bool:
        cursor = connection.execute(
            """
            INSERT INTO paper_system_events(
                recorded_at_utc, run_id, event_time, event_kind, severity,
                message, state, total_exposure, margin_ratio,
                order_count_local, order_count_remote, cancelling_count,
                fill_ratio, api_weight, is_sync_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _utc_now(),
                self.run_id,
                _finite_float(payload.get("event_time")),
                str(payload.get("event_kind", "") or ""),
                str(payload.get("severity", "") or ""),
                str(payload.get("message", "") or ""),
                str(payload.get("state", "") or ""),
                _finite_float(payload.get("total_exposure")),
                _finite_float(payload.get("margin_ratio")),
                _nullable_integer(payload.get("order_count_local")),
                _nullable_integer(payload.get("order_count_remote")),
                _nullable_integer(payload.get("cancelling_count")),
                _finite_float(payload.get("fill_ratio")),
                _nullable_integer(payload.get("api_weight")),
                _nullable_bool(payload.get("is_sync_error")),
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
                "execution",
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

    def _record_observation(self, kind: str, payload: dict) -> bool:
        with self._close_lock:
            if not self._accepting or self._closed:
                return False
            try:
                self._queue.put_nowait((str(kind), dict(payload)))
                return True
            except queue.Full:
                self._set_failure(f"{kind}_projection_queue_full", observation=True)
                logger.error(
                    "[PaperTradeDatabase] Observation projection queue is full: "
                    f"kind={kind}"
                )
                return False

    def record_order_event(self, payload: dict) -> bool:
        return self._record_observation("order_event", payload)

    def record_strategy_sample(self, payload: dict) -> bool:
        normalized = dict(payload)
        symbol = str(normalized.get("symbol", "") or "").upper()
        sample_time = _finite_float(normalized.get("sample_time"))
        with self._sample_lock:
            previous = self._last_strategy_sample_time.get(symbol)
            if (
                sample_time is not None
                and previous is not None
                and sample_time >= previous
                and sample_time - previous < self.strategy_sample_interval_sec
            ):
                with self._health_lock:
                    self._throttled_strategy_sample_count += 1
                return True
            accepted = self._record_observation("strategy_sample", normalized)
            if accepted and sample_time is not None:
                self._last_strategy_sample_time[symbol] = sample_time
            return accepted

    def record_markout(self, payload: dict) -> bool:
        return self._record_observation("markout", payload)

    def record_account_sample(self, payload: dict) -> bool:
        normalized = dict(payload)
        sample_time = _finite_float(normalized.get("sample_time"))
        with self._sample_lock:
            previous = self._last_account_sample_time
            if (
                sample_time is not None
                and previous is not None
                and sample_time >= previous
                and sample_time - previous < self.account_sample_interval_sec
            ):
                with self._health_lock:
                    self._throttled_account_sample_count += 1
                return True
            accepted = self._record_observation("account_sample", normalized)
            if accepted and sample_time is not None:
                self._last_account_sample_time = sample_time
            return accepted

    def record_system_event(self, payload: dict) -> bool:
        return self._record_observation("system_event", payload)

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
                    kind = item[0]
                    inserted = False
                    with connection:
                        if kind == "execution":
                            _, journal_seq, journal_ts, journal_hash, payload = item
                            inserted = self._insert_fill(
                                connection,
                                journal_seq=journal_seq,
                                journal_hash=journal_hash,
                                journal_ts=journal_ts,
                                payload=payload,
                            )
                        elif kind == "order_event":
                            inserted = self._insert_order_event(connection, item[1])
                        elif kind == "strategy_sample":
                            inserted = self._insert_strategy_sample(connection, item[1])
                        elif kind == "markout":
                            inserted = self._insert_markout(connection, item[1])
                        elif kind == "account_sample":
                            inserted = self._insert_account_sample(
                                connection,
                                item[1],
                            )
                        elif kind == "system_event":
                            inserted = self._insert_system_event(
                                connection,
                                item[1],
                            )
                        else:
                            raise ValueError(f"unsupported projection kind: {kind}")
                    if not inserted:
                        continue
                    with self._health_lock:
                        if kind == "execution":
                            self._committed_fill_count += 1
                        elif kind == "order_event":
                            self._committed_order_event_count += 1
                        elif kind == "strategy_sample":
                            self._committed_strategy_sample_count += 1
                        elif kind == "markout":
                            self._committed_markout_count += 1
                        elif kind == "account_sample":
                            self._committed_account_sample_count += 1
                        elif kind == "system_event":
                            self._committed_system_event_count += 1
                except Exception as exc:
                    kind = item[0] if isinstance(item, tuple) and item else "unknown"
                    self._set_failure(
                        f"{kind}_projection_failed:{type(exc).__name__}:{exc}",
                        observation=kind != "execution",
                    )
                    logger.critical(
                        "[PaperTradeDatabase] Projection failed: "
                        f"kind={kind} "
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
                    if isinstance(item, tuple) and item and item[0] == "execution":
                        self._failed_fill_count += 1
                    else:
                        self._failed_observation_count += 1
            finally:
                self._queue.task_done()

    def _set_failure(self, reason: str, *, observation: bool = False) -> None:
        with self._health_lock:
            self._healthy = False
            self._last_error = str(reason)
            if observation:
                self._failed_observation_count += 1
            else:
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
                "committed_order_event_count": self._committed_order_event_count,
                "committed_strategy_sample_count": (
                    self._committed_strategy_sample_count
                ),
                "throttled_strategy_sample_count": (
                    self._throttled_strategy_sample_count
                ),
                "committed_markout_count": self._committed_markout_count,
                "committed_account_sample_count": (
                    self._committed_account_sample_count
                ),
                "throttled_account_sample_count": (
                    self._throttled_account_sample_count
                ),
                "committed_system_event_count": (
                    self._committed_system_event_count
                ),
                "backfilled_fill_count": self._backfilled_fill_count,
                "failed_fill_count": self._failed_fill_count,
                "failed_observation_count": self._failed_observation_count,
                "last_error": self._last_error,
                "closed": self._closed,
            }
