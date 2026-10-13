import sqlite3

import pytest

from ui.paper_ledger import PaperLedgerSummary, paper_ledger_from_config


def _database(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE paper_runs (
            run_id TEXT PRIMARY KEY, started_at_utc TEXT, stopped_at_utc TEXT,
            status TEXT, clean_shutdown INTEGER, symbols_json TEXT,
            initial_balance_usdt REAL
        );
        CREATE TABLE paper_fills (
            execution_id TEXT, run_id TEXT, is_maker INTEGER,
            fill_notional REAL, realized_pnl REAL, commission REAL
        );
        CREATE TABLE paper_account_samples (
            run_id TEXT, sample_time REAL, unrealized_pnl REAL
        );
        INSERT INTO paper_runs VALUES
            ('clean-run-0001', '2026-10-13T01:00:00Z', '2026-10-13T02:00:00Z',
             'stopped', 1, '["KORUUSDT"]', 10000),
            ('crashed-run-02', '2026-10-13T03:00:00Z', NULL,
             'running', NULL, '["KORUUSDT"]', 10000),
            ('current-run-03', '2026-10-13T04:00:00Z', NULL,
             'running', NULL, '["KORUUSDT"]', 10000);
        INSERT INTO paper_fills VALUES
            ('a', 'clean-run-0001', 1, 100.0, 0.0, 0.0),
            ('b', 'clean-run-0001', 0, 100.0, -0.50, 0.025),
            ('c', 'crashed-run-02', 1, 50.0, 0.20, 0.0),
            ('d', 'current-run-03', 1, 30.0, 0.10, -0.003);
        INSERT INTO paper_account_samples VALUES
            ('crashed-run-02', 1.0, 9.0),
            ('crashed-run-02', 2.0, -0.40),
            ('current-run-03', 1.0, 5.0);
        """
    )
    connection.commit()
    connection.close()


def test_cumulative_net_includes_crash_leftover_and_live_unrealized(tmp_path):
    path = tmp_path / "trades.sqlite3"
    _database(path)

    summary = PaperLedgerSummary(path).snapshot(
        current_run_id="current-run-03",
        live_unrealized_pnl=0.05,
    )

    runs = {run["run_id"]: run for run in summary["runs"]}
    assert runs["clean-ru"]["net_pnl"] == pytest.approx(-0.525)
    # A crashed run keeps its last sampled open position.
    assert runs["crashed-"]["open_unrealized"] == pytest.approx(-0.40)
    assert runs["crashed-"]["net_pnl"] == pytest.approx(-0.20)
    # The current run is priced from the live account, not its samples.
    assert runs["current-"]["current"] is True
    assert runs["current-"]["open_unrealized"] == pytest.approx(0.05)
    assert runs["current-"]["net_pnl"] == pytest.approx(0.153)
    assert runs["current-"]["cumulative_net_pnl"] == pytest.approx(-0.572)

    totals = summary["totals"]
    assert totals["runs"] == 3
    assert totals["fills"] == 4
    assert totals["maker_fills"] == 3
    assert totals["notional"] == pytest.approx(280.0)
    assert totals["commission"] == pytest.approx(0.022)
    assert totals["net_pnl"] == pytest.approx(-0.572)


def test_missing_database_is_unavailable(tmp_path):
    summary = PaperLedgerSummary(tmp_path / "missing.sqlite3").snapshot()

    assert summary == {"available": False, "reason": "database_missing"}


def test_ledger_only_for_enabled_paper_database():
    config = {"paper_trade_database": {"enabled": True, "path": "x.sqlite3"}}

    assert paper_ledger_from_config(config, is_paper=True) is not None
    assert paper_ledger_from_config(config, is_paper=False) is None
    assert (
        paper_ledger_from_config(
            {"paper_trade_database": {"enabled": False, "path": "x.sqlite3"}},
            is_paper=True,
        )
        is None
    )
