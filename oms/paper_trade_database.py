"""Queryable SQLite projection of durable Paper execution records."""

from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from infrastructure.logger import logger
from infrastructure.paper_trade import validate_paper_trade_database_config


SCHEMA_VERSION = 5
LEGACY_RUN_ID = "legacy-journal-import"
SOFTWARE_VERSION = "0.1.0"


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


def _code_revision(project_root: Path) -> str:
    configured = str(os.environ.get("CHRONOSHFT_CODE_REVISION", "") or "").strip()
    if configured:
        return configured
    git_path = project_root / ".git"
    try:
        if git_path.is_file():
            marker = git_path.read_text(encoding="utf-8").strip()
            if not marker.lower().startswith("gitdir:"):
                return ""
            git_path = (git_path.parent / marker.split(":", 1)[1].strip()).resolve()
        head = (git_path / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head
        reference = head.split(":", 1)[1].strip()
        loose_ref = git_path / reference
        if loose_ref.is_file():
            return loose_ref.read_text(encoding="utf-8").strip()
        packed_refs = git_path / "packed-refs"
        if packed_refs.is_file():
            for line in packed_refs.read_text(encoding="utf-8").splitlines():
                if not line or line.startswith(("#", "^")):
                    continue
                revision, name = line.split(" ", 1)
                if name.strip() == reference:
                    return revision.strip()
    except (OSError, ValueError):
        return ""
    return ""


class PaperTradeDatabase:
    """Maintain an idempotent, query-oriented projection of Paper fills."""

    _STOP = object()

    @classmethod
    def rebuild_offline(
        cls,
        config: dict,
        journal,
        *,
        destination_path: str | os.PathLike | None = None,
    ) -> dict:
        """Build a new v5 projection without starting runtime services."""
        database_config = validate_paper_trade_database_config(config)
        configured_path = destination_path or database_config.get("path")
        if not str(configured_path or "").strip():
            raise ValueError("Paper projection destination path is required")
        path = Path(str(configured_path)).resolve()
        if path.exists():
            raise PaperTradeDatabaseError(
                "Offline Paper projection destination already exists"
            )
        journal_id = str(getattr(journal, "journal_id", "") or "")
        try:
            journal_id = str(uuid.UUID(journal_id))
        except ValueError as exc:
            raise PaperTradeDatabaseError(
                "Offline Paper rebuild requires a v3 OMS journal"
            ) from exc

        instance = cls.__new__(cls)
        instance.path = path
        instance.journal_path = Path(str(journal.path)).resolve()
        instance.journal_id = journal_id
        instance.sqlite_timeout_sec = max(
            0.1,
            float(database_config.get("sqlite_timeout_sec", 5.0) or 5.0),
        )
        instance._journal = journal
        instance._projection_high_water_seq = 0
        instance._projection_high_water_hash = ""
        instance._backfilled_fill_count = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(instance._connect()) as connection, connection:
                instance._create_schema(connection)
                for record in journal.iter_records():
                    instance._project_historical_record(connection, record)
                check = str(
                    connection.execute("PRAGMA quick_check").fetchone()[0]
                )
                if check.lower() != "ok":
                    raise PaperTradeDatabaseError(
                        f"Offline Paper projection integrity failure: {check}"
                    )
                run_count = int(
                    connection.execute("SELECT COUNT(*) FROM paper_runs").fetchone()[0]
                )
                fill_count = int(
                    connection.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]
                )
                connection.commit()
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("PRAGMA journal_mode=DELETE")
        except Exception:
            for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass
            raise
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {
            "schema_version": SCHEMA_VERSION,
            "journal_id": journal_id,
            "projection_high_water_seq": instance._projection_high_water_seq,
            "projection_high_water_hash": instance._projection_high_water_hash,
            "run_count": run_count,
            "fill_count": fill_count,
            "sha256": digest,
            "path": str(path),
        }

    def __init__(self, config: dict, journal, failure_callback=None) -> None:
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
        self.journal_id = str(getattr(journal, "journal_id", "") or "").strip()
        try:
            self.journal_id = str(uuid.UUID(self.journal_id))
        except ValueError as exc:
            raise ValueError(
                "paper_trade_database requires a v3 OMS journal identity"
            ) from exc
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
            int(database_config.get("queue_capacity", 4096) or 4096),
        )
        self.write_batch_size = int(
            database_config.get("write_batch_size", 64) or 64
        )
        self.min_free_bytes = int(
            database_config.get("min_free_bytes", 0) or 0
        )
        self.space_check_interval_sec = float(
            database_config.get("space_check_interval_sec", 1.0) or 1.0
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
        self.market_sample_interval_sec = max(
            0.1,
            float(
                database_config.get("market_sample_interval_sec", 1.0)
                or 1.0
            ),
        )
        self.run_id = uuid.uuid4().hex
        self.started_at_utc = _utc_now()
        self.software_version = SOFTWARE_VERSION
        self.code_revision = _code_revision(Path(__file__).resolve().parents[1])
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
        self._failure_callback = failure_callback
        self._queue = queue.Queue(maxsize=queue_capacity)
        self._health_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._sample_lock = threading.Lock()
        self._last_strategy_sample_time: dict[str, float] = {}
        self._last_account_sample_time: float | None = None
        self._last_market_sample_time: dict[str, float] = {}
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
        self._committed_market_sample_count = 0
        self._committed_batch_count = 0
        self._max_committed_batch_size = 0
        self._throttled_strategy_sample_count = 0
        self._throttled_account_sample_count = 0
        self._throttled_market_sample_count = 0
        self._backfilled_fill_count = 0
        self._failed_fill_count = 0
        self._failed_observation_count = 0
        self._disk_free_bytes = None
        self._last_space_check_monotonic = 0.0
        self._last_space_check_at = 0.0
        self._space_check_failure_count = 0
        self._space_rejection_count = 0
        self._last_space_error = ""
        self._failure_notified = False
        self._thread = None
        self._projection_high_water_seq = 0
        self._projection_high_water_hash = ""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._ensure_disk_space(1024 * 1024)
            self._bootstrap()
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
        existing_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if version == 0 and existing_tables.intersection(
            {"paper_fills", "paper_runs", "projection_metadata"}
        ):
            raise PaperTradeDatabaseError(
                "Unversioned Paper projection requires an offline rebuild"
            )
        if version not in {0, SCHEMA_VERSION}:
            raise PaperTradeDatabaseError(
                "Paper projection schema requires an offline rebuild: "
                f"found v{version}, expected v{SCHEMA_VERSION}"
            )
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS projection_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_runs (
                run_id TEXT PRIMARY KEY,
                journal_id TEXT NOT NULL,
                started_at_utc TEXT NOT NULL,
                stopped_at_utc TEXT,
                status TEXT NOT NULL,
                clean_shutdown INTEGER,
                shutdown_reason TEXT NOT NULL DEFAULT '',
                config_sha256 TEXT NOT NULL DEFAULT '',
                symbols_json TEXT NOT NULL DEFAULT '[]',
                initial_balance_usdt REAL NOT NULL DEFAULT 0.0,
                software_version TEXT NOT NULL DEFAULT '',
                code_revision TEXT NOT NULL DEFAULT '',
                journal_path TEXT NOT NULL,
                start_journal_seq INTEGER
            );

            CREATE TABLE IF NOT EXISTS paper_fills (
                journal_id TEXT NOT NULL,
                journal_seq INTEGER NOT NULL,
                journal_hash TEXT NOT NULL,
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
                raw_payload_json TEXT NOT NULL,
                PRIMARY KEY(journal_id, journal_seq)
            );

            CREATE TABLE IF NOT EXISTS projection_journal_records (
                journal_id TEXT NOT NULL,
                journal_seq INTEGER NOT NULL,
                journal_hash TEXT NOT NULL,
                record_kind TEXT NOT NULL,
                projected_at_utc TEXT NOT NULL,
                PRIMARY KEY(journal_id, journal_seq)
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
                strategy_compute_latency_ms REAL,
                formula_version TEXT NOT NULL DEFAULT '',
                units_version TEXT NOT NULL DEFAULT '',
                intensity_source TEXT NOT NULL DEFAULT ''
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

            CREATE TABLE IF NOT EXISTS paper_market_samples (
                sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at_utc TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES paper_runs(run_id),
                sample_time REAL,
                symbol TEXT NOT NULL,
                mark_price REAL NOT NULL,
                index_price REAL NOT NULL,
                basis_bps REAL NOT NULL,
                funding_rate REAL NOT NULL,
                next_funding_time REAL,
                exchange_time REAL,
                received_time REAL,
                corrected_received_time REAL,
                dispatch_time REAL,
                received_monotonic REAL,
                dispatch_monotonic REAL,
                clock_offset_ms REAL,
                transport_latency_ms REAL,
                gateway_processing_latency_ms REAL
            );

            CREATE INDEX IF NOT EXISTS idx_paper_fills_run_seq
                ON paper_fills(run_id, journal_id, journal_seq);
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
            CREATE INDEX IF NOT EXISTS idx_paper_market_samples_run_symbol_time
                ON paper_market_samples(run_id, symbol, sample_time);
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
        configured_journal_id = metadata.get("journal_id")
        if configured_journal_id not in {None, self.journal_id}:
            raise PaperTradeDatabaseError(
                "Paper projection is bound to a different journal_id; "
                "rebuild it offline before replacing the journal"
            )
        if configured_journal_id is None:
            row_count = sum(
                int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ("paper_runs", "paper_fills")
            )
            if row_count:
                raise PaperTradeDatabaseError(
                    "Paper projection has data but no journal identity; "
                    "an offline rebuild is required"
                )
        connection.executemany(
            "INSERT OR REPLACE INTO projection_metadata(key, value) VALUES (?, ?)",
            (
                ("product", "ChronosHFT.paper_trades"),
                ("schema_version", str(SCHEMA_VERSION)),
                ("journal_id", self.journal_id),
                ("journal_path", str(self.journal_path)),
                (
                    "projection_high_water_seq",
                    metadata.get("projection_high_water_seq", "0"),
                ),
                (
                    "projection_high_water_hash",
                    metadata.get("projection_high_water_hash", ""),
                ),
            ),
        )

    @staticmethod
    def _migrate_to_v4(connection) -> None:
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
            "paper_runs": {
                "software_version": "TEXT NOT NULL DEFAULT ''",
                "code_revision": "TEXT NOT NULL DEFAULT ''",
            },
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
                "formula_version": "TEXT NOT NULL DEFAULT ''",
                "units_version": "TEXT NOT NULL DEFAULT ''",
                "intensity_source": "TEXT NOT NULL DEFAULT ''",
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

    def _bootstrap(self) -> None:
        detected_at = self.started_at_utc
        with closing(self._connect()) as connection, connection:
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
            metadata = dict(
                connection.execute(
                    "SELECT key, value FROM projection_metadata"
                ).fetchall()
            )
            try:
                high_water_seq = int(
                    metadata.get("projection_high_water_seq", "0") or 0
                )
            except (TypeError, ValueError) as exc:
                raise PaperTradeDatabaseError(
                    "Invalid Paper projection high-water sequence"
                ) from exc
            high_water_hash = str(
                metadata.get("projection_high_water_hash", "") or ""
            )
            if high_water_seq < 0 or (
                high_water_seq > 0 and len(high_water_hash) != 64
            ):
                raise PaperTradeDatabaseError(
                    "Invalid Paper projection high-water identity"
                )
            journal_health = self._journal.health_snapshot()
            journal_head_seq = int(journal_health.get("next_seq", 1) or 1) - 1
            journal_head_hash = str(journal_health.get("last_hash", "") or "")
            if high_water_seq > journal_head_seq:
                raise PaperTradeDatabaseError(
                    "Paper projection high-water is ahead of the OMS journal"
                )
            if (
                high_water_seq == journal_head_seq
                and high_water_seq > 0
                and high_water_hash != journal_head_hash
            ):
                raise PaperTradeDatabaseError(
                    "Paper projection high-water hash conflicts with the OMS journal"
                )
            self._projection_high_water_seq = high_water_seq
            self._projection_high_water_hash = high_water_hash
            stream_records = getattr(self._journal, "iter_records", None)
            if callable(stream_records):
                records = stream_records(
                    start_seq=high_water_seq + 1,
                    expected_prev_hash=(
                        high_water_hash if high_water_seq else None
                    ),
                )
            else:
                records = iter(self._journal.read_all())
            for record_index, record in enumerate(records):
                if record_index % 256 == 0:
                    bootstrap_reserve = 1024 * 1024
                    self._ensure_disk_space(bootstrap_reserve)
                    self._consume_reserved_space(bootstrap_reserve)
                self._project_historical_record(connection, record)
            connection.execute(
                """
                INSERT INTO paper_runs(
                    run_id, journal_id, started_at_utc, status, clean_shutdown,
                    config_sha256, symbols_json, initial_balance_usdt,
                    software_version, code_revision, journal_path
                ) VALUES (?, ?, ?, 'running', NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.run_id,
                    self.journal_id,
                    self.started_at_utc,
                    self.config_sha256,
                    _canonical_json(self.symbols),
                    self.initial_balance_usdt,
                    self.software_version,
                    self.code_revision,
                    str(self.journal_path),
                ),
            )
        with self._health_lock:
            self._disk_free_bytes = None
        self._ensure_disk_space(0)

    def _record_projection_identity(
        self,
        connection,
        *,
        journal_seq: int,
        journal_hash: str,
        record_kind: str,
    ) -> bool:
        journal_seq = int(journal_seq)
        journal_hash = str(journal_hash or "")
        if journal_seq <= 0 or len(journal_hash) != 64:
            raise PaperTradeDatabaseError(
                "Paper projection received an invalid journal identity"
            )
        existing = connection.execute(
            """
            SELECT journal_hash FROM projection_journal_records
            WHERE journal_id = ? AND journal_seq = ?
            """,
            (self.journal_id, journal_seq),
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != journal_hash:
                raise PaperTradeDatabaseError(
                    "Paper projection collision: identical journal sequence "
                    "has a different hash"
                )
            return False
        connection.execute(
            """
            INSERT INTO projection_journal_records(
                journal_id, journal_seq, journal_hash, record_kind,
                projected_at_utc
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                self.journal_id,
                journal_seq,
                journal_hash,
                str(record_kind or ""),
                _utc_now(),
            ),
        )
        if journal_seq == self._projection_high_water_seq + 1:
            next_seq = journal_seq
            next_hash = journal_hash
            while True:
                following = connection.execute(
                    """
                    SELECT journal_hash FROM projection_journal_records
                    WHERE journal_id = ? AND journal_seq = ?
                    """,
                    (self.journal_id, next_seq + 1),
                ).fetchone()
                if following is None:
                    break
                next_seq += 1
                next_hash = str(following[0])
            self._projection_high_water_seq = next_seq
            self._projection_high_water_hash = next_hash
            connection.executemany(
                """
                INSERT OR REPLACE INTO projection_metadata(key, value)
                VALUES (?, ?)
                """,
                (
                    ("projection_high_water_seq", str(next_seq)),
                    ("projection_high_water_hash", next_hash),
                ),
            )
        return True

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
        record_journal_id = str(record.get("journal_id", "") or "")
        if record_journal_id != self.journal_id:
            raise PaperTradeDatabaseError(
                "Paper projection record journal_id mismatch"
            )
        journal_hash = str(record.get("hash", "") or "")
        inserted_identity = self._record_projection_identity(
            connection,
            journal_seq=journal_seq,
            journal_hash=journal_hash,
            record_kind=kind,
        )
        if not inserted_identity:
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
                journal_hash=journal_hash,
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
                run_id, journal_id, started_at_utc, status, clean_shutdown,
                config_sha256, symbols_json, initial_balance_usdt,
                software_version, code_revision, journal_path,
                start_journal_seq
            ) VALUES (?, ?, ?, 'interrupted', 0, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                start_journal_seq = COALESCE(
                    paper_runs.start_journal_seq,
                    excluded.start_journal_seq
                )
            """,
            (
                run_id,
                self.journal_id,
                started_at_utc or _utc_now(),
                str(payload.get("config_sha256", "") or ""),
                _canonical_json(payload.get("symbols", [])),
                _finite_float(payload.get("initial_balance_usdt"), 0.0),
                str(payload.get("software_version", "") or ""),
                str(payload.get("code_revision", "") or ""),
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
                journal_id, journal_seq, journal_hash, journal_ts_utc,
                recorded_at_utc,
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
                ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(journal_id, journal_seq) DO NOTHING
            """,
            (
                self.journal_id,
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
        if cursor.rowcount == 0:
            existing = connection.execute(
                """
                SELECT journal_hash FROM paper_fills
                WHERE journal_id = ? AND journal_seq = ?
                """,
                (self.journal_id, int(journal_seq)),
            ).fetchone()
            if existing is None or str(existing[0]) != str(journal_hash or ""):
                raise PaperTradeDatabaseError(
                    "Paper fill projection hash collision"
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
                callback_age_ms, strategy_compute_latency_ms,
                formula_version, units_version, intensity_source
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?
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
                str(params.get("formula_version", "") or ""),
                str(params.get("units_version", "") or ""),
                str(params.get("intensity_source", "") or ""),
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

    def _insert_market_sample(self, connection, payload: dict) -> bool:
        cursor = connection.execute(
            """
            INSERT INTO paper_market_samples(
                recorded_at_utc, run_id, sample_time, symbol, mark_price,
                index_price, basis_bps, funding_rate, next_funding_time,
                exchange_time, received_time, corrected_received_time,
                dispatch_time, received_monotonic, dispatch_monotonic,
                clock_offset_ms, transport_latency_ms,
                gateway_processing_latency_ms
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                _utc_now(),
                self.run_id,
                _finite_float(payload.get("sample_time")),
                str(payload.get("symbol", "") or "").upper(),
                _finite_float(payload.get("mark_price"), 0.0),
                _finite_float(payload.get("index_price"), 0.0),
                _finite_float(payload.get("basis_bps"), 0.0),
                _finite_float(payload.get("funding_rate"), 0.0),
                _finite_float(payload.get("next_funding_time")),
                _finite_float(payload.get("exchange_time")),
                _finite_float(payload.get("received_time")),
                _finite_float(payload.get("corrected_received_time")),
                _finite_float(payload.get("dispatch_time")),
                _finite_float(payload.get("received_monotonic")),
                _finite_float(payload.get("dispatch_monotonic")),
                _finite_float(payload.get("clock_offset_ms")),
                _finite_float(payload.get("transport_latency_ms")),
                _finite_float(
                    payload.get("gateway_processing_latency_ms")
                ),
            ),
        )
        return cursor.rowcount == 1

    def _set_start_journal_seq(self, sequence: int) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE paper_runs SET start_journal_seq = ? WHERE run_id = ?",
                (int(sequence), self.run_id),
            )

    def _mark_bootstrap_failure(self, exc: Exception) -> None:
        try:
            if not self.path.exists():
                return
            with closing(self._connect()) as connection, connection:
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
            "journal_id": self.journal_id,
            "started_at_utc": self.started_at_utc,
            "config_sha256": self.config_sha256,
            "symbols": list(self.symbols),
            "initial_balance_usdt": self.initial_balance_usdt,
            "software_version": self.software_version,
            "code_revision": self.code_revision,
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
        if len(str(journal_hash or "")) != 64:
            self._set_failure("execution_projection_missing_journal_hash")
            return False
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

    def record_market_sample(self, payload: dict) -> bool:
        normalized = dict(payload)
        symbol = str(normalized.get("symbol", "") or "").upper()
        sample_time = _finite_float(normalized.get("sample_time"))
        with self._sample_lock:
            previous = self._last_market_sample_time.get(symbol)
            if (
                sample_time is not None
                and previous is not None
                and sample_time >= previous
                and sample_time - previous < self.market_sample_interval_sec
            ):
                with self._health_lock:
                    self._throttled_market_sample_count += 1
                return True
            accepted = self._record_observation("market_sample", normalized)
            if accepted and sample_time is not None:
                self._last_market_sample_time[symbol] = sample_time
            return accepted

    @staticmethod
    def _estimate_batch_bytes(batch) -> int:
        payload_bytes = sum(
            len(_canonical_json(item).encode("utf-8")) for item in batch
        )
        return max(64 * 1024, payload_bytes * 2)

    def _ensure_disk_space(self, required_bytes: int) -> None:
        if self.min_free_bytes <= 0:
            return

        required_bytes = max(0, int(required_bytes))
        now_monotonic = time.monotonic()
        with self._health_lock:
            refresh_required = (
                self._disk_free_bytes is None
                or now_monotonic - self._last_space_check_monotonic
                >= self.space_check_interval_sec
                or self._disk_free_bytes < self.min_free_bytes + required_bytes
            )
        if refresh_required:
            try:
                free_bytes = int(shutil.disk_usage(self.path.parent).free)
            except Exception as exc:
                with self._health_lock:
                    self._space_check_failure_count += 1
                    self._last_space_error = (
                        f"space_check_failed:{type(exc).__name__}:{exc}"
                    )
                raise PaperTradeDatabaseError(
                    "Failed to verify free space for the Paper database: "
                    f"{type(exc).__name__}:{exc}"
                ) from exc
            with self._health_lock:
                self._disk_free_bytes = free_bytes
                self._last_space_check_monotonic = now_monotonic
                self._last_space_check_at = time.time()
                self._last_space_error = ""

        with self._health_lock:
            free_bytes = int(self._disk_free_bytes or 0)
            free_after_write = free_bytes - required_bytes
            if free_after_write >= self.min_free_bytes:
                return
            self._space_rejection_count += 1
            self._last_space_error = (
                "insufficient_space:"
                f"free={free_bytes}:"
                f"required={required_bytes}:"
                f"reserve={self.min_free_bytes}"
            )
        raise PaperTradeDatabaseError(
            "Paper database write rejected because disk free space would fall "
            f"below the reserve: free={free_bytes} batch={required_bytes} "
            f"reserve={self.min_free_bytes}"
        )

    def _consume_reserved_space(self, reserved_bytes: int) -> None:
        with self._health_lock:
            if self._disk_free_bytes is not None:
                self._disk_free_bytes = max(
                    0,
                    self._disk_free_bytes - max(0, int(reserved_bytes)),
                )

    def _set_batch_failure(self, batch, reason: str) -> None:
        execution_count = sum(
            1
            for item in batch
            if isinstance(item, tuple) and item and item[0] == "execution"
        )
        observation_count = len(batch) - execution_count
        self._set_failure_counts(
            reason,
            execution_count=execution_count,
            observation_count=observation_count,
        )

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
                batch = [self._queue.get()]
                stop_requested = batch[0] is self._STOP
                while not stop_requested and len(batch) < self.write_batch_size:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    batch.append(item)
                    stop_requested = item is self._STOP

                projections = batch[:-1] if stop_requested else batch
                try:
                    if projections:
                        write_ok = self._write_projection_batch(
                            connection,
                            projections,
                        )
                    else:
                        write_ok = True
                finally:
                    for _item in batch:
                        self._queue.task_done()
                if not write_ok and not stop_requested:
                    self._drain_without_writing()
                    return
                if stop_requested:
                    return
        finally:
            connection.close()

    def _write_projection_batch(self, connection, batch) -> bool:
        outcomes = []
        reserved_bytes = self._estimate_batch_bytes(batch)
        try:
            self._ensure_disk_space(reserved_bytes)
            with connection:
                outcomes = [
                    self._insert_projection_item(connection, item)
                    for item in batch
                ]
        except PaperTradeDatabaseError as exc:
            self._set_batch_failure(
                batch,
                f"disk_guard_failed:{type(exc).__name__}:{exc}",
            )
            logger.critical(f"[PaperTradeDatabase] {exc}")
            return False
        except Exception as batch_error:
            logger.warning(
                "[PaperTradeDatabase] Batch projection failed; retrying "
                "records individually: "
                f"size={len(batch)} "
                f"{type(batch_error).__name__}:{batch_error}"
            )
            all_written = True
            for item in batch:
                all_written = (
                    self._write_projection_item_individually(connection, item)
                    and all_written
                )
            return all_written
        self._record_committed_outcomes(outcomes, len(batch))
        self._consume_reserved_space(reserved_bytes)
        return True

    def _write_projection_item_individually(self, connection, item) -> bool:
        try:
            reserved_bytes = self._estimate_batch_bytes((item,))
            self._ensure_disk_space(reserved_bytes)
            with connection:
                outcome = self._insert_projection_item(connection, item)
        except Exception as exc:
            kind = item[0] if isinstance(item, tuple) and item else "unknown"
            self._set_failure(
                f"{kind}_projection_failed:{type(exc).__name__}:{exc}",
                observation=kind != "execution",
            )
            logger.critical(
                "[PaperTradeDatabase] Projection failed: "
                f"kind={kind} {type(exc).__name__}:{exc}"
            )
            return False
        self._record_committed_outcomes([outcome], 1)
        self._consume_reserved_space(reserved_bytes)
        return True

    def _insert_projection_item(self, connection, item) -> tuple[str, bool]:
        kind = item[0]
        if kind == "execution":
            _, journal_seq, journal_ts, journal_hash, payload = item
            self._record_projection_identity(
                connection,
                journal_seq=journal_seq,
                journal_hash=str(journal_hash or ""),
                record_kind="execution_record",
            )
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
            inserted = self._insert_account_sample(connection, item[1])
        elif kind == "system_event":
            inserted = self._insert_system_event(connection, item[1])
        elif kind == "market_sample":
            inserted = self._insert_market_sample(connection, item[1])
        else:
            raise ValueError(f"unsupported projection kind: {kind}")
        return kind, bool(inserted)

    def _record_committed_outcomes(self, outcomes, batch_size: int) -> None:
        with self._health_lock:
            self._committed_batch_count += 1
            self._max_committed_batch_size = max(
                self._max_committed_batch_size,
                int(batch_size),
            )
            for kind, inserted in outcomes:
                if not inserted:
                    continue
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
                elif kind == "market_sample":
                    self._committed_market_sample_count += 1

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
        self._set_failure_counts(
            reason,
            execution_count=0 if observation else 1,
            observation_count=1 if observation else 0,
        )

    def _set_failure_counts(
        self,
        reason: str,
        *,
        execution_count: int,
        observation_count: int,
    ) -> None:
        should_notify = False
        with self._health_lock:
            self._healthy = False
            self._last_error = str(reason)
            self._failed_fill_count += max(0, int(execution_count))
            self._failed_observation_count += max(
                0,
                int(observation_count),
            )
            if (
                self._accepting
                and not self._closed
                and not self._failure_notified
                and callable(self._failure_callback)
            ):
                self._failure_notified = True
                should_notify = True
            self._accepting = False
        if should_notify:
            try:
                self._failure_callback(str(reason))
            except Exception as exc:
                logger.critical(
                    "[PaperTradeDatabase] Fail-closed callback failed: "
                    f"{type(exc).__name__}:{exc}"
                )

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
                with closing(self._connect()) as connection, connection:
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
                "journal_id": self.journal_id,
                "projection_high_water_seq": self._projection_high_water_seq,
                "projection_high_water_hash": self._projection_high_water_hash,
                "run_id": self.run_id,
                "queue_depth": self._queue.qsize(),
                "write_batch_size": self.write_batch_size,
                "min_free_bytes": self.min_free_bytes,
                "disk_free_bytes": self._disk_free_bytes,
                "last_space_check_at": self._last_space_check_at,
                "space_check_failure_count": (
                    self._space_check_failure_count
                ),
                "space_rejection_count": self._space_rejection_count,
                "last_space_error": self._last_space_error,
                "committed_batch_count": self._committed_batch_count,
                "max_committed_batch_size": self._max_committed_batch_size,
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
                "committed_market_sample_count": (
                    self._committed_market_sample_count
                ),
                "throttled_market_sample_count": (
                    self._throttled_market_sample_count
                ),
                "backfilled_fill_count": self._backfilled_fill_count,
                "failed_fill_count": self._failed_fill_count,
                "failed_observation_count": self._failed_observation_count,
                "last_error": self._last_error,
                "closed": self._closed,
            }
