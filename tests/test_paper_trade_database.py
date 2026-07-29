import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from event.type import ExchangeOrderUpdate, OrderIntent, Side, TIF_RPI
from oms.engine import OMS
from oms.initializer import OMSInitializer
from oms.journal import OMSJournal
from oms.order import Order
from oms.paper_trade_database import LEGACY_RUN_ID, PaperTradeDatabase
from scripts.query_paper_trades import main as query_main
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
