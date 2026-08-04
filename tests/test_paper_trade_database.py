import json
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from event.type import (
    ExchangeOrderUpdate,
    LifecycleState,
    OrderIntent,
    Side,
    TIF_RPI,
)
from oms.engine import OMS
from oms.initializer import OMSInitializer
from oms.journal import OMSJournal
from oms.order import Order
from oms.paper_trade_database import (
    LEGACY_RUN_ID,
    PaperTradeDatabase,
    PaperTradeDatabaseError,
)
from scripts.query_paper_trades import main as query_main
from scripts.query_paper_trades import query_observations
from scripts.query_paper_trades import query_rows


def make_config(tmp_path: Path, *, paper=True):
    return {
        "execution": {"mode": "paper" if paper else "live"},
        "paper_trade": {
            "enabled": paper,
            "initial_balance_usdt": 500_000.0,
        },
        "paper_trade_database": {
            "enabled": True,
            "path": str(tmp_path / "trades.sqlite3"),
            "sqlite_timeout_sec": 1.0,
            "queue_capacity": 100,
            "close_timeout_sec": 2.0,
        },
        "symbols": ["SNDKUSDT", "SOXLUSDT"],
        "account": {"initial_balance_usdt": 500_000.0, "leverage": 10},
        "backtest": {"maker_fee": 0.0002, "taker_fee": 0.0005},
        "risk": {"limits": {"max_pos_notional": 90_000.0}},
        "oms": {
            "journal_enabled": True,
            "journal_fsync": True,
            "journal_integrity_check": True,
            "replay_journal_on_startup": False,
            "journal_path": str(tmp_path / "oms.jsonl"),
        },
    }


class DummyEngine:
    def __init__(self):
        self.events = []

    def put(self, event):
        self.events.append(event)


class DummyGateway:
    gateway_name = "BINANCE"


def execution_payload(*, run_id=""):
    payload = {
        "execution_id": "BINANCE:SNDKUSDT:7",
        "venue": "BINANCE",
        "client_oid": "paper-order-1",
        "exchange_oid": "paper-exchange-1",
        "strategy_id": "GLFT_MultiScale",
        "symbol": "SNDKUSDT",
        "side": "BUY",
        "fill_qty": 2.0,
        "fill_price": 1250.0,
        "cum_filled_qty": 2.0,
        "exchange_status": "FILLED",
        "exchange_time": 1234.5,
        "trade_id": 7,
        "commission": 0.5,
        "commission_asset": "USDT",
        "booked_fee": 0.5,
        "realized_pnl": 0.0,
        "is_maker": True,
        "order_type": "LIMIT",
        "time_in_force": "RPI",
        "is_rpi": True,
        "fill_model": "rpi_public_trade_proxy",
        "fill_trigger": "through",
        "market_trade_id": 700,
        "market_trade_price": 1249.5,
        "market_trade_qty": 3.0,
        "market_trade_exchange_time": 1234.48,
        "market_trade_received_time": 1234.49,
        "market_trade_clock_offset_ms": -1.0,
        "market_trade_transport_latency_ms": 9.0,
        "market_trade_local_age_ms": 4.0,
        "queue_ahead_before": 1.5,
        "best_bid_at_fill": 1249.9,
        "best_ask_at_fill": 1250.1,
        "mid_at_fill": 1250.0,
        "quote_age_ms": 275.0,
        "reduce_only": False,
        "pre_status": "NEW",
    }
    if run_id:
        payload["paper_run_id"] = run_id
    return payload


def fetch_one(path: Path, sql: str, parameters=()):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(sql, parameters).fetchone()
        return dict(row) if row is not None else None


def test_oms_market_sample_computes_basis_and_latency_components():
    database = SimpleNamespace(record_market_sample=MagicMock(return_value=True))
    oms = object.__new__(OMS)
    oms.paper_trade_database = database
    market_data = SimpleNamespace(
        symbol="SNDKUSDT",
        mark_price=100.2,
        index_price=100.0,
        funding_rate=0.0001,
        next_funding_timestamp=2000.0,
        exchange_timestamp=1000.0,
        received_timestamp=1000.012,
        corrected_received_timestamp=1000.010,
        dispatch_timestamp=1000.013,
        received_monotonic=20.0,
        dispatch_monotonic=20.003,
        clock_offset_ms=-2.0,
    )

    assert oms.record_paper_market_sample(market_data)

    payload = database.record_market_sample.call_args.args[0]
    assert payload["sample_time"] == pytest.approx(1000.010)
    assert payload["basis_bps"] == pytest.approx(19.9800266)
    assert payload["transport_latency_ms"] == pytest.approx(10.0)
    assert payload["gateway_processing_latency_ms"] == pytest.approx(3.0)
    assert payload["next_funding_time"] == 2000.0


def test_oms_market_sample_rejects_invalid_prices_and_missing_database():
    oms = object.__new__(OMS)
    assert not oms.record_paper_market_sample(SimpleNamespace())

    database = SimpleNamespace(record_market_sample=MagicMock(return_value=True))
    oms.paper_trade_database = database
    assert not oms.record_paper_market_sample(
        SimpleNamespace(mark_price=float("nan"), index_price=100.0)
    )
    database.record_market_sample.assert_not_called()


def test_oms_runtime_system_event_serializes_mapping_details():
    database = SimpleNamespace(record_system_event=MagicMock(return_value=True))
    oms = object.__new__(OMS)
    oms.paper_trade_database = database

    assert oms.record_paper_system_event(
        "runtime_resources",
        {
            "event_time": 1234.5,
            "level": "WARNING",
            "state": "warning",
            "details": {
                "schema": "chronoshft.paper_runtime_resources.v1",
                "resource": {"rss_bytes": 100},
            },
        },
    )

    payload = database.record_system_event.call_args.args[0]
    assert payload["event_time"] == 1234.5
    assert payload["event_kind"] == "runtime_resources"
    assert payload["severity"] == "WARNING"
    assert payload["state"] == "warning"
    assert json.loads(payload["message"])["resource"]["rss_bytes"] == 100


def test_runtime_dataset_filters_and_decodes_resource_events():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE paper_system_events (
            event_id INTEGER PRIMARY KEY,
            run_id TEXT,
            event_time REAL,
            event_kind TEXT,
            severity TEXT,
            message TEXT,
            state TEXT,
            recorded_at_utc TEXT
        )
        """
    )
    connection.executemany(
        """
        INSERT INTO paper_system_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (
                1,
                "run-1",
                1.0,
                "runtime_resources",
                "INFO",
                '{"resource":{"rss_bytes":100}}',
                "healthy",
                "now",
            ),
            (2, "run-1", 2.0, "alert", "WARNING", "alert", "", "now"),
        ),
    )

    rows = query_observations(
        connection,
        dataset="runtime",
        symbol="",
        run_id="run-1",
        limit=10,
    )

    assert len(rows) == 1
    assert rows[0]["runtime"]["resource"]["rss_bytes"] == 100
    connection.close()


def test_async_projection_records_run_and_fill(tmp_path):
    config = make_config(tmp_path)
    journal = OMSJournal(config)
    database = PaperTradeDatabase(config, journal)
    payload = execution_payload(run_id=database.run_id)
    sequence = journal.append("execution_record", payload)

    metadata = journal.commit_metadata(sequence)
    assert database.record_execution(
        sequence,
        payload,
        journal_ts=metadata["ts"],
        journal_hash=metadata["hash"],
    )
    assert database.close(clean_shutdown=True, reason="test_complete")

    fill = fetch_one(
        Path(config["paper_trade_database"]["path"]),
        "SELECT * FROM paper_fills WHERE journal_seq = ?",
        (sequence,),
    )
    assert fill["run_id"] == database.run_id
    assert fill["fill_notional"] == 2500.0
    assert fill["fill_model"] == "rpi_public_trade_proxy"
    assert fill["journal_hash"] == metadata["hash"]
    assert fill["journal_ts_utc"] == metadata["ts"]
    assert fill["is_maker"] == 1
    assert fill["fill_trigger"] == "through"
    assert fill["market_trade_id"] == 700
    assert fill["market_trade_transport_latency_ms"] == 9.0
    assert fill["market_trade_local_age_ms"] == 4.0
    assert fill["queue_ahead_before"] == 1.5
    assert fill["mid_at_fill"] == 1250.0
    assert fill["quote_age_ms"] == 275.0
    assert fill["raw_payload_json"]

    run = fetch_one(
        Path(config["paper_trade_database"]["path"]),
        "SELECT * FROM paper_runs WHERE run_id = ?",
        (database.run_id,),
    )
    assert run["status"] == "stopped"
    assert run["clean_shutdown"] == 1
    assert run["shutdown_reason"] == "test_complete"


def test_writer_batches_queued_projections_and_drains_before_stop(tmp_path):
    config = make_config(tmp_path)
    config["paper_trade_database"].update(
        {"queue_capacity": 200, "write_batch_size": 32}
    )
    journal = OMSJournal(config)
    with patch.object(PaperTradeDatabase, "_writer_main", autospec=True):
        database = PaperTradeDatabase(config, journal)
    database._thread.join(timeout=1.0)

    for index in range(90):
        assert database.record_system_event(
            {
                "event_time": float(index),
                "event_kind": "batch_test",
                "severity": "INFO",
                "message": f"event-{index}",
            }
        )

    database._thread = threading.Thread(
        target=database._writer_main,
        daemon=True,
        name="PaperTradeDatabaseWriterTest",
    )
    database._thread.start()
    assert database.close(clean_shutdown=True, reason="batch_test")

    health = database.health_snapshot()
    assert health["queue_depth"] == 0
    assert health["write_batch_size"] == 32
    assert health["committed_system_event_count"] == 90
    assert health["committed_batch_count"] == 3
    assert health["max_committed_batch_size"] == 32
    count = fetch_one(
        Path(config["paper_trade_database"]["path"]),
        "SELECT COUNT(*) AS count FROM paper_system_events",
    )
    assert count["count"] == 90


def test_bad_projection_does_not_discard_valid_records_from_batch(tmp_path):
    config = make_config(tmp_path)
    config["paper_trade_database"]["write_batch_size"] = 16
    journal = OMSJournal(config)
    with patch.object(PaperTradeDatabase, "_writer_main", autospec=True):
        database = PaperTradeDatabase(config, journal)
    database._thread.join(timeout=1.0)

    database._queue.put_nowait(("unsupported", {}))
    assert database.record_system_event(
        {
            "event_time": 1.0,
            "event_kind": "valid_after_bad",
            "severity": "INFO",
            "message": "must survive batch rollback",
        }
    )
    database._thread = threading.Thread(
        target=database._writer_main,
        daemon=True,
        name="PaperTradeDatabaseWriterFallbackTest",
    )
    database._thread.start()

    assert not database.close(clean_shutdown=True, reason="fallback_test")
    health = database.health_snapshot()
    assert not health["healthy"]
    assert health["failed_observation_count"] == 1
    assert health["committed_system_event_count"] == 1
    row = fetch_one(
        Path(config["paper_trade_database"]["path"]),
        "SELECT event_kind FROM paper_system_events",
    )
    assert row["event_kind"] == "valid_after_bad"


def test_journal_backfill_is_idempotent_when_oms_replay_is_disabled(tmp_path):
    config = make_config(tmp_path)
    journal = OMSJournal(config)
    sequence = journal.append("execution_record", execution_payload())
    assert journal.load() == []
    assert [record["seq"] for record in journal.read_all()] == [sequence]

    with patch.object(
        journal,
        "read_all",
        side_effect=AssertionError("database bootstrap must stream"),
    ):
        first = PaperTradeDatabase(config, journal)
    assert first.health_snapshot()["backfilled_fill_count"] == 1
    assert first.close(clean_shutdown=False, reason="first_projection")

    second = PaperTradeDatabase(config, journal)
    assert second.health_snapshot()["backfilled_fill_count"] == 0
    assert second.close(clean_shutdown=False, reason="second_projection")

    database_path = Path(config["paper_trade_database"]["path"])
    fill = fetch_one(database_path, "SELECT * FROM paper_fills")
    assert fill["journal_seq"] == sequence
    assert fill["run_id"] == LEGACY_RUN_ID
    count = fetch_one(database_path, "SELECT COUNT(*) AS count FROM paper_fills")
    assert count["count"] == 1


def test_database_records_calibration_observations_as_structured_rows(tmp_path):
    config = make_config(tmp_path)
    journal = OMSJournal(config)
    database = PaperTradeDatabase(config, journal)

    assert database.record_order_event(
        {
            "client_oid": "order-1",
            "exchange_oid": "exchange-1",
            "symbol": "SNDKUSDT",
            "strategy_id": "GLFT_MultiScale",
            "side": "BUY",
            "status": "NEW",
            "price": 100.0,
            "quantity": 0.1,
            "filled_quantity": 0.0,
            "average_price": 0.0,
            "time_in_force": "RPI",
            "is_post_only": True,
            "is_rpi": True,
            "order_type": "LIMIT",
            "reduce_only": False,
            "tag": "glft_quote",
            "created_monotonic": 10.0,
            "updated_monotonic": 10.1,
            "event_time": 1000.0,
            "error_message": "",
        }
    )
    assert database.record_strategy_sample(
        {
            "sample_time": 1000.2,
            "symbol": "SNDKUSDT",
            "fair_value": 100.05,
            "alpha_bps": 0.0,
            "params": {
                "strategy": "GLFT_MultiScale",
                "state": "QUOTING",
                "mode": "RPI",
                "mid_price": 100.0,
                "best_bid": 99.9,
                "best_ask": 100.1,
                "target_bid": 99.8,
                "target_ask": 100.2,
                "market_spread_bps": 20.0,
                "quote_spread_bps": 40.0,
                "bid_quote_qty": 0.1,
                "ask_quote_qty": 0.1,
                "position_qty": 0.0,
                "position_notional": 0.0,
                "sigma_bps": 2.5,
                "A_per_s": 1.2,
                "k_per_bps": 0.8,
                "adaptive": {
                    "markout": {
                        "sides": {
                            "BUY": {"adverse_cost_bps": 1.1},
                            "SELL": {"adverse_cost_bps": 1.3},
                        }
                    },
                    "flow_toxicity": {
                        "signed_trade_imbalance": -0.4,
                        "microprice_offset_bps": -0.7,
                        "bid_adverse_cost_bps": 1.5,
                        "ask_adverse_cost_bps": 0.0,
                    },
                    "bid_queue": {"latency_cost_bps": 0.2},
                    "ask_queue": {"latency_cost_bps": 0.3},
                },
                "stale_quote_guard": {
                    "bid_depth_bps": 2.0,
                    "ask_depth_bps": 2.5,
                    "bid_at_risk": False,
                    "ask_at_risk": True,
                },
                "size_optimization": {
                    "bid_multiplier": 0.5,
                    "ask_multiplier": 0.25,
                },
                "market_data_timing": {
                    "exchange_timestamp": 1000.0,
                    "received_timestamp": 1000.01,
                    "corrected_received_timestamp": 1000.009,
                    "dispatch_timestamp": 1000.011,
                    "received_monotonic": 10.0,
                    "dispatch_monotonic": 10.002,
                    "callback_monotonic": 10.006,
                    "clock_offset_ms": -1.0,
                    "transport_latency_ms": 9.0,
                    "gateway_processing_latency_ms": 2.0,
                    "strategy_queue_latency_ms": 4.0,
                    "callback_age_ms": 6.0,
                    "strategy_compute_latency_ms": 0.5,
                    "best_bid_qty": 12.0,
                    "best_ask_qty": 13.0,
                },
            },
        }
    )
    assert database.record_markout(
        {
            "client_oid": "order-1",
            "trade_id": "7",
            "symbol": "SNDKUSDT",
            "side": "BUY",
            "fill_price": 100.0,
            "horizon_ms": 500,
            "mid_price": 99.98,
            "signed_markout_bps": -2.0002,
            "fill_observed_monotonic": 10.2,
            "mid_observed_monotonic": 10.71,
            "observation_lag_ms": 10.0,
        }
    )
    assert database.record_account_sample(
        {
            "sample_time": 1000.2,
            "balance": 10_000.0,
            "equity": 10_005.0,
            "available": 9_500.0,
            "used_margin": 505.0,
            "budget_balance": 10_000.0,
            "budget_available": 9_495.0,
            "maintenance_margin": 50.0,
            "margin_balance": 10_005.0,
            "maintenance_margin_ratio": 0.0049975,
            "margin_snapshot_time": 1000.1,
            "margin_snapshot_synced": True,
            "external_cash_flow_total": 0.0,
            "cash_flow_snapshot_time": 999.0,
            "cash_flow_snapshot_synced": True,
        }
    )
    assert database.record_account_sample(
        {
            "sample_time": 1000.5,
            "balance": 10_000.0,
            "equity": 10_006.0,
        }
    )
    assert database.record_system_event(
        {
            "event_time": 1000.3,
            "event_kind": "system_health",
            "severity": "ERROR",
            "message": "FREEZE_VENUE:BINANCE:WS_TRANSPORT_DROP",
            "state": "FROZEN",
            "api_weight": 12,
        }
    )
    assert database.record_market_sample(
        {
            "sample_time": 1000.4,
            "symbol": "SNDKUSDT",
            "mark_price": 100.2,
            "index_price": 100.0,
            "basis_bps": 19.9800266,
            "funding_rate": 0.0001,
            "next_funding_time": 2000.0,
            "exchange_time": 1000.38,
            "received_time": 1000.4,
            "transport_latency_ms": 20.0,
        }
    )
    assert database.record_market_sample(
        {
            "sample_time": 1000.8,
            "symbol": "SNDKUSDT",
            "mark_price": 100.3,
            "index_price": 100.0,
        }
    )
    assert database.record_strategy_sample(
        {
            "sample_time": 1000.5,
            "symbol": "SNDKUSDT",
            "fair_value": 100.0,
            "alpha_bps": 0.0,
            "params": {},
        }
    )
    assert database.health_snapshot()["throttled_strategy_sample_count"] == 1
    assert database.health_snapshot()["throttled_account_sample_count"] == 1
    assert database.health_snapshot()["throttled_market_sample_count"] == 1
    assert database.close(clean_shutdown=True, reason="observation_test")

    path = Path(config["paper_trade_database"]["path"])
    order = fetch_one(path, "SELECT * FROM paper_order_events")
    strategy = fetch_one(path, "SELECT * FROM paper_strategy_samples")
    markout = fetch_one(path, "SELECT * FROM paper_fill_markouts")
    account = fetch_one(path, "SELECT * FROM paper_account_samples")
    system_event = fetch_one(path, "SELECT * FROM paper_system_events")
    market = fetch_one(path, "SELECT * FROM paper_market_samples")
    run = fetch_one(
        path,
        "SELECT * FROM paper_runs WHERE run_id = ?",
        (database.run_id,),
    )
    assert order["client_oid"] == "order-1"
    assert order["is_rpi"] == 1
    assert order["order_type"] == "LIMIT"
    assert order["tag"] == "glft_quote"
    assert strategy["bid_markout_cost_bps"] == 1.1
    assert strategy["bid_flow_cost_bps"] == 1.5
    assert strategy["ask_stale_at_risk"] == 1
    assert strategy["bid_size_multiplier"] == 0.5
    assert strategy["best_bid_qty"] == 12.0
    assert strategy["transport_latency_ms"] == 9.0
    assert strategy["strategy_queue_latency_ms"] == 4.0
    assert strategy["formula_version"] == ""
    strategy_count = fetch_one(
        path,
        "SELECT COUNT(*) AS count FROM paper_strategy_samples",
    )
    assert strategy_count["count"] == 1
    assert markout["horizon_ms"] == 500
    assert markout["observation_lag_ms"] == 10.0
    assert account["unrealized_pnl"] == 5.0
    assert account["margin_snapshot_synced"] == 1
    assert system_event["event_kind"] == "system_health"
    assert system_event["api_weight"] == 12
    assert market["funding_rate"] == 0.0001
    assert market["transport_latency_ms"] == 20.0
    assert run["software_version"] == "0.1.0"
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        queried = query_observations(
            connection,
            dataset="markouts",
            symbol="SNDKUSDT",
            run_id=database.run_id,
            limit=10,
        )
        queried_accounts = query_observations(
            connection,
            dataset="accounts",
            symbol="",
            run_id=database.run_id,
            limit=10,
        )
        queried_system = query_observations(
            connection,
            dataset="system",
            symbol="",
            run_id=database.run_id,
            limit=10,
        )
        queried_markets = query_observations(
            connection,
            dataset="markets",
            symbol="SNDKUSDT",
            run_id=database.run_id,
            limit=10,
        )
    assert queried[0]["client_oid"] == "order-1"
    assert queried[0]["signed_markout_bps"] == -2.0002
    assert queried_accounts[0]["equity"] == 10_005.0
    assert queried_system[0]["severity"] == "ERROR"
    assert queried_markets[0]["basis_bps"] == 19.9800266


@pytest.mark.parametrize("legacy_version", [1, 2, 3, 4])
def test_legacy_schema_requires_offline_projection_rebuild(
    tmp_path,
    legacy_version,
):
    config = make_config(tmp_path)
    journal = OMSJournal(config)
    first = PaperTradeDatabase(config, journal)
    assert first.close(clean_shutdown=True, reason="before_schema_downgrade")

    path = Path(config["paper_trade_database"]["path"])
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version={legacy_version}")
        connection.execute(
            "UPDATE projection_metadata SET value=? WHERE key='schema_version'",
            (str(legacy_version),),
        )

    with pytest.raises(PaperTradeDatabaseError, match="offline rebuild"):
        PaperTradeDatabase(config, journal)


def test_projection_rejects_replacement_journal_identity(tmp_path):
    config = make_config(tmp_path)
    original = OMSJournal(config)
    database = PaperTradeDatabase(config, original)
    assert database.close(clean_shutdown=True, reason="identity_test")

    replacement_config = make_config(tmp_path)
    replacement_config["oms"]["journal_path"] = str(
        tmp_path / "replacement-journal"
    )
    replacement = OMSJournal(replacement_config)

    with pytest.raises(PaperTradeDatabaseError, match="different journal_id"):
        PaperTradeDatabase(replacement_config, replacement)


def test_projection_rejects_same_sequence_with_different_hash(tmp_path):
    config = make_config(tmp_path)
    journal = OMSJournal(config)
    database = PaperTradeDatabase(config, journal)
    payload = execution_payload(run_id=database.run_id)
    sequence = journal.append("execution_record", payload)
    metadata = journal.commit_metadata(sequence)
    assert database.record_execution(
        sequence,
        payload,
        journal_ts=metadata["ts"],
        journal_hash=metadata["hash"],
    )
    assert database.close(clean_shutdown=True, reason="collision_test")

    with sqlite3.connect(config["paper_trade_database"]["path"]) as connection:
        connection.execute(
            """
            UPDATE projection_journal_records SET journal_hash = ?
            WHERE journal_id = ? AND journal_seq = ?
            """,
            ("0" * 64, journal.journal_id, sequence),
        )

    with pytest.raises(PaperTradeDatabaseError, match="collision"):
        PaperTradeDatabase(config, journal)


def test_offline_rebuild_creates_new_v5_projection_without_journal_writes(
    tmp_path,
):
    config = make_config(tmp_path)
    journal = OMSJournal(config)
    database = PaperTradeDatabase(config, journal)
    payload = execution_payload(run_id=database.run_id)
    journal.append("execution_record", payload)
    assert database.close(clean_shutdown=True, reason="offline_rebuild_source")
    head_before = journal.health_snapshot()["next_seq"]
    destination = tmp_path / "rebuilt.sqlite3"

    receipt = PaperTradeDatabase.rebuild_offline(
        config,
        journal,
        destination_path=destination,
    )

    assert journal.health_snapshot()["next_seq"] == head_before
    assert receipt["schema_version"] == 5
    assert receipt["journal_id"] == journal.journal_id
    assert receipt["fill_count"] == 1
    with sqlite3.connect(destination) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert connection.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0] == 1


def test_backfill_repairs_missing_run_start_sequence(tmp_path):
    config = make_config(tmp_path)
    journal = OMSJournal(config)
    first = PaperTradeDatabase(config, journal)
    run_id = first.run_id
    start_record = next(
        record
        for record in journal.read_all()
        if record.get("kind") == "paper_run_started"
        and record.get("payload", {}).get("paper_run_id") == run_id
    )
    assert first.close(clean_shutdown=False, reason="simulated_crash_window")

    with sqlite3.connect(config["paper_trade_database"]["path"]) as connection:
        connection.execute(
            "UPDATE paper_runs SET start_journal_seq = NULL WHERE run_id = ?",
            (run_id,),
        )

    second = PaperTradeDatabase(config, journal)
    try:
        recovered = fetch_one(
            Path(config["paper_trade_database"]["path"]),
            "SELECT start_journal_seq FROM paper_runs WHERE run_id = ?",
            (run_id,),
        )
        assert recovered["start_journal_seq"] == start_record["seq"]
    finally:
        assert second.close(clean_shutdown=False, reason="recovery_test")


def test_query_helper_filters_and_summarizes_fills(tmp_path, capsys):
    config = make_config(tmp_path)
    journal = OMSJournal(config)
    database = PaperTradeDatabase(config, journal)
    payload = execution_payload(run_id=database.run_id)
    sequence = journal.append("execution_record", payload)
    metadata = journal.commit_metadata(sequence)
    assert database.record_execution(
        sequence,
        payload,
        journal_ts=metadata["ts"],
        journal_hash=metadata["hash"],
    )
    assert database.close(clean_shutdown=True, reason="query_test")

    with sqlite3.connect(config["paper_trade_database"]["path"]) as connection:
        connection.row_factory = sqlite3.Row
        rows = query_rows(
            connection,
            symbol="SNDKUSDT",
            run_id=database.run_id,
            limit=10,
            summary=False,
        )
        summary = query_rows(
            connection,
            symbol="SNDKUSDT",
            run_id=database.run_id,
            limit=10,
            summary=True,
        )

    assert len(rows) == 1
    assert rows[0]["journal_seq"] == sequence
    assert summary == [
        {
            "run_id": database.run_id,
            "symbol": "SNDKUSDT",
            "fill_count": 1,
            "filled_quantity": 2.0,
            "filled_notional": 2500.0,
            "commission": 0.5,
            "realized_pnl": 0.0,
            "maker_fills": 1,
            "first_exchange_time": 1234.5,
            "last_exchange_time": 1234.5,
        }
    ]
    assert (
        query_main(
            [
                "--database",
                config["paper_trade_database"]["path"],
                "--symbol",
                "SNDKUSDT",
                "--summary",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert '"filled_notional": 2500.0' in output


def test_database_rejects_live_configuration(tmp_path):
    config = make_config(tmp_path, paper=False)
    journal = OMSJournal(config)

    with pytest.raises(ValueError, match="Paper-only"):
        PaperTradeDatabase(config, journal)


def test_database_config_rejects_non_durable_journal(tmp_path):
    config = make_config(tmp_path)
    config["oms"]["journal_fsync"] = False

    with pytest.raises(ValueError, match="journal_fsync"):
        PaperTradeDatabase(config, OMSJournal(config))


def test_database_failure_precedes_background_thread_startup(tmp_path):
    config = make_config(tmp_path)

    with (
        patch(
            "oms.initializer.PaperTradeDatabase",
            side_effect=RuntimeError("database unavailable"),
        ),
        patch.object(OMSInitializer, "_initialize_background_tasks") as start_tasks,
        pytest.raises(RuntimeError, match="database unavailable"),
    ):
        OMS(DummyEngine(), DummyGateway(), config)

    start_tasks.assert_not_called()


def test_database_disk_guard_marks_unhealthy_and_notifies_once(tmp_path):
    config = make_config(tmp_path)
    config["paper_trade_database"].update(
        {
            "min_free_bytes": 1024,
            "space_check_interval_sec": 0.1,
        }
    )
    usage = SimpleNamespace(free=10 * 1024 * 1024)
    failures = []

    with patch(
        "oms.paper_trade_database.shutil.disk_usage",
        return_value=usage,
    ):
        database = PaperTradeDatabase(
            config,
            OMSJournal(config),
            failure_callback=failures.append,
        )
        database._disk_free_bytes = None
        usage.free = 1024

        assert database.record_system_event({"event_kind": "health"})
        deadline = time.monotonic() + 1.0
        while database.health_snapshot()["healthy"]:
            assert time.monotonic() < deadline
            time.sleep(0.01)

        health = database.health_snapshot()
        assert health["space_rejection_count"] == 1
        assert health["failed_observation_count"] == 1
        assert len(failures) == 1
        assert failures[0].startswith("disk_guard_failed:")
        assert not database.record_system_event({"event_kind": "late"})
        assert database.close(clean_shutdown=False, reason="disk_guard_test") is False


def test_database_failure_freezes_oms_and_blocks_new_risk(tmp_path):
    config = make_config(tmp_path)
    oms = OMS(DummyEngine(), DummyGateway(), config)
    oms.state = LifecycleState.LIVE
    oms._sync_capability_mode("database_failure_test")
    database = oms.paper_trade_database

    try:
        database._set_failure("forced_projection_failure")

        assert oms.state == LifecycleState.FROZEN
        assert oms.last_freeze_reason.startswith(
            "paper_trade_database_unhealthy:"
        )
        opening = OrderIntent(
            "GLFT_MultiScale",
            "SNDKUSDT",
            Side.BUY,
            1250.0,
            1.0,
        )
        reduction = OrderIntent(
            "GLFT_MultiScale",
            "SNDKUSDT",
            Side.SELL,
            1250.0,
            1.0,
            reduce_only=True,
        )
        assert oms._get_paper_database_rejection_locked(opening).startswith(
            "paper_trade_database_unhealthy:"
        )
        assert oms._get_paper_database_rejection_locked(reduction) == ""
    finally:
        oms.stop(reason="database_failure_test")


@pytest.mark.parametrize("value", [-1, 1.5, True])
def test_database_config_rejects_invalid_min_free_bytes(tmp_path, value):
    config = make_config(tmp_path)
    config["paper_trade_database"]["min_free_bytes"] = value

    with pytest.raises(ValueError, match="min_free_bytes"):
        PaperTradeDatabase(config, OMSJournal(config))


def test_oms_execution_commit_projects_paper_fill_model(tmp_path):
    config = make_config(tmp_path)
    oms = OMS(DummyEngine(), DummyGateway(), config)
    order = Order(
        "paper-order-oms",
        OrderIntent(
            "GLFT_MultiScale",
            "SNDKUSDT",
            Side.BUY,
            1250.0,
            2.0,
            time_in_force=TIF_RPI,
        ),
    )
    order.mark_submitting()
    order.mark_pending_ack("paper-exchange-oms")
    order.mark_new("paper-exchange-oms", update_time=100.0)
    update = ExchangeOrderUpdate(
        client_oid=order.client_oid,
        exchange_oid=order.exchange_oid,
        symbol="SNDKUSDT",
        status="FILLED",
        filled_qty=2.0,
        filled_price=1250.0,
        cum_filled_qty=2.0,
        update_time=101.0,
        trade_id=8,
        commission=0.5,
        commission_asset="USDT",
        is_maker=True,
        fill_model="rpi_public_trade_proxy",
    )
    run_id = oms.paper_trade_database.run_id

    try:
        assert oms._record_execution(order, update, fill_qty=2.0, fee=0.5)
    finally:
        stop_result = oms.stop(reason="integration_test")

    assert stop_result["paper_trade_database_stopped"] is True
    fill = fetch_one(
        Path(config["paper_trade_database"]["path"]),
        "SELECT * FROM paper_fills WHERE run_id = ?",
        (run_id,),
    )
    assert fill["fill_model"] == "rpi_public_trade_proxy"
    assert fill["execution_id"] == "BINANCE:SNDKUSDT:8"
