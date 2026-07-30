import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from event.type import ExchangeOrderUpdate, OrderIntent, Side, TIF_RPI
from oms.engine import OMS
from oms.initializer import OMSInitializer
from oms.journal import OMSJournal
from oms.order import Order
from oms.paper_trade_database import LEGACY_RUN_ID, PaperTradeDatabase
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


def test_journal_backfill_is_idempotent_when_oms_replay_is_disabled(tmp_path):
    config = make_config(tmp_path)
    journal = OMSJournal(config)
    sequence = journal.append("execution_record", execution_payload())
    assert journal.load() == []
    assert [record["seq"] for record in journal.read_all()] == [sequence]

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


def test_schema_v1_is_migrated_in_place_without_losing_runs(tmp_path):
    config = make_config(tmp_path)
    journal = OMSJournal(config)
    first = PaperTradeDatabase(config, journal)
    first_run_id = first.run_id
    assert first.close(clean_shutdown=True, reason="before_migration")

    path = Path(config["paper_trade_database"]["path"])
    upgraded_fill_columns = (
        "fill_trigger",
        "market_trade_id",
        "market_trade_price",
        "market_trade_qty",
        "market_trade_exchange_time",
        "market_trade_received_time",
        "market_trade_clock_offset_ms",
        "market_trade_transport_latency_ms",
        "market_trade_local_age_ms",
        "queue_ahead_before",
        "best_bid_at_fill",
        "best_ask_at_fill",
        "mid_at_fill",
        "quote_age_ms",
    )
    with sqlite3.connect(path) as connection:
        for column in upgraded_fill_columns:
            connection.execute(f"ALTER TABLE paper_fills DROP COLUMN {column}")
        for column in ("software_version", "code_revision"):
            connection.execute(f"ALTER TABLE paper_runs DROP COLUMN {column}")
        for table in (
            "paper_order_events",
            "paper_strategy_samples",
            "paper_fill_markouts",
            "paper_account_samples",
            "paper_system_events",
            "paper_market_samples",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=1")
        connection.execute(
            "UPDATE projection_metadata SET value='1' WHERE key='schema_version'"
        )

    second = PaperTradeDatabase(config, journal)
    assert second.close(clean_shutdown=True, reason="after_migration")
    with sqlite3.connect(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(paper_fills)")
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        run_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(paper_runs)")
        }
        recovered_run = connection.execute(
            "SELECT run_id FROM paper_runs WHERE run_id = ?",
            (first_run_id,),
        ).fetchone()
    assert version == 4
    assert set(upgraded_fill_columns) <= columns
    assert {"software_version", "code_revision"} <= run_columns
    assert {
        "paper_order_events",
        "paper_strategy_samples",
        "paper_fill_markouts",
        "paper_account_samples",
        "paper_system_events",
        "paper_market_samples",
    } <= tables
    assert recovered_run == (first_run_id,)


def test_schema_v2_is_migrated_to_v4_with_all_telemetry(tmp_path):
    config = make_config(tmp_path)
    journal = OMSJournal(config)
    first = PaperTradeDatabase(config, journal)
    assert first.close(clean_shutdown=True, reason="before_v4_migration")

    path = Path(config["paper_trade_database"]["path"])
    fill_columns = (
        "market_trade_exchange_time",
        "market_trade_received_time",
        "market_trade_clock_offset_ms",
        "market_trade_transport_latency_ms",
        "market_trade_local_age_ms",
    )
    order_columns = ("order_type", "reduce_only", "tag")
    strategy_columns = (
        "best_bid_qty",
        "best_ask_qty",
        "orderbook_exchange_time",
        "orderbook_received_time",
        "orderbook_corrected_received_time",
        "orderbook_dispatch_time",
        "orderbook_received_monotonic",
        "orderbook_dispatch_monotonic",
        "strategy_callback_monotonic",
        "clock_offset_ms",
        "transport_latency_ms",
        "gateway_processing_latency_ms",
        "strategy_queue_latency_ms",
        "callback_age_ms",
        "strategy_compute_latency_ms",
        "formula_version",
        "units_version",
        "intensity_source",
    )
    run_columns = ("software_version", "code_revision")
    with sqlite3.connect(path) as connection:
        for table, columns in (
            ("paper_fills", fill_columns),
            ("paper_order_events", order_columns),
            ("paper_strategy_samples", strategy_columns),
            ("paper_runs", run_columns),
        ):
            for column in columns:
                connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        connection.execute("DROP TABLE paper_account_samples")
        connection.execute("DROP TABLE paper_system_events")
        connection.execute("DROP TABLE paper_market_samples")
        connection.execute("PRAGMA user_version=2")
        connection.execute(
            "UPDATE projection_metadata SET value='2' WHERE key='schema_version'"
        )

    second = PaperTradeDatabase(config, journal)
    assert second.close(clean_shutdown=True, reason="after_v4_migration")
    with sqlite3.connect(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        restored = {}
        for table in (
            "paper_fills",
            "paper_order_events",
            "paper_strategy_samples",
            "paper_runs",
        ):
            restored[table] = {
                row[1]
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
    assert version == 4
    assert set(fill_columns) <= restored["paper_fills"]
    assert set(order_columns) <= restored["paper_order_events"]
    assert set(strategy_columns) <= restored["paper_strategy_samples"]
    assert set(run_columns) <= restored["paper_runs"]
    assert {
        "paper_account_samples",
        "paper_system_events",
        "paper_market_samples",
    } <= tables


def test_schema_v3_is_migrated_to_v4_without_losing_runs(tmp_path):
    config = make_config(tmp_path)
    journal = OMSJournal(config)
    first = PaperTradeDatabase(config, journal)
    first_run_id = first.run_id
    assert first.close(clean_shutdown=True, reason="before_v3_to_v4_migration")

    path = Path(config["paper_trade_database"]["path"])
    strategy_columns = ("formula_version", "units_version", "intensity_source")
    run_columns = ("software_version", "code_revision")
    with sqlite3.connect(path) as connection:
        for column in strategy_columns:
            connection.execute(
                f"ALTER TABLE paper_strategy_samples DROP COLUMN {column}"
            )
        for column in run_columns:
            connection.execute(f"ALTER TABLE paper_runs DROP COLUMN {column}")
        connection.execute("DROP TABLE paper_market_samples")
        connection.execute("PRAGMA user_version=3")
        connection.execute(
            "UPDATE projection_metadata SET value='3' WHERE key='schema_version'"
        )

    second = PaperTradeDatabase(config, journal)
    assert second.close(clean_shutdown=True, reason="after_v3_to_v4_migration")
    with sqlite3.connect(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        restored_strategy = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(paper_strategy_samples)"
            )
        }
        restored_runs = {
            row[1] for row in connection.execute("PRAGMA table_info(paper_runs)")
        }
        recovered_run = connection.execute(
            "SELECT run_id FROM paper_runs WHERE run_id = ?",
            (first_run_id,),
        ).fetchone()

    assert version == 4
    assert "paper_market_samples" in tables
    assert set(strategy_columns) <= restored_strategy
    assert set(run_columns) <= restored_runs
    assert recovered_run == (first_run_id,)


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
