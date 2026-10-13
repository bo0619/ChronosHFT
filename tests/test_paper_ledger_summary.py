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
            run_id TEXT, sample_time REAL, equity REAL, unrealized_pnl REAL
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
            ('clean-run-0001', 100.0, 10000.0, 0.0),
            ('clean-run-0001', 200.0, 9999.475, 0.0),
            ('crashed-run-02', 300.0, 10009.0, 9.0),
            ('crashed-run-02', 400.0, 9999.80, -0.40),
            ('current-run-03', 500.0, 10005.0, 5.0);
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

    # Each run's equity P&L is stacked on the net of the runs before it.
    assert [point["t"] for point in summary["curve"][:5]] == [
        100.0, 200.0, 300.0, 400.0, 500.0,
    ]
    assert [point["net"] for point in summary["curve"][:5]] == pytest.approx(
        [0.0, -0.525, 8.475, -0.725, 4.275]
    )
    assert summary["run_starts"] == [100.0, 300.0, 500.0]
    # The live point closes the curve at the cumulative net.
    assert summary["curve"][-1]["net"] == pytest.approx(-0.572)

    totals = summary["totals"]
    assert totals["runs"] == 3
    assert totals["fills"] == 4
    assert totals["maker_fills"] == 3
    assert totals["notional"] == pytest.approx(280.0)
    assert totals["commission"] == pytest.approx(0.022)
    assert totals["net_pnl"] == pytest.approx(-0.572)
    assert totals["net_bps"] == pytest.approx(-0.572 / 280.0 * 10_000.0)
    assert totals["net_per_fill"] == pytest.approx(-0.143)


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


def test_markout_symbol_and_quote_position_sections(tmp_path):
    path = tmp_path / "trades.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE paper_runs (
            run_id TEXT PRIMARY KEY, started_at_utc TEXT, stopped_at_utc TEXT,
            status TEXT, clean_shutdown INTEGER, symbols_json TEXT,
            initial_balance_usdt REAL
        );
        CREATE TABLE paper_fills (
            execution_id TEXT, run_id TEXT, symbol TEXT, is_maker INTEGER,
            fill_notional REAL, realized_pnl REAL, commission REAL
        );
        CREATE TABLE paper_account_samples (
            run_id TEXT, sample_time REAL, equity REAL, unrealized_pnl REAL
        );
        CREATE TABLE paper_fill_markouts (
            run_id TEXT, side TEXT, horizon_ms INTEGER, signed_markout_bps REAL
        );
        CREATE TABLE paper_strategy_samples (
            run_id TEXT, symbol TEXT, state TEXT, mid_price REAL,
            best_bid REAL, best_ask REAL, target_bid REAL, target_ask REAL
        );
        INSERT INTO paper_runs VALUES
            ('old-run-000001', '2026-10-13T01:00:00Z', '2026-10-13T02:00:00Z',
             'stopped', 1, '["KORUUSDT"]', 10000),
            ('current-run-03', '2026-10-13T04:00:00Z', NULL,
             'running', NULL, '["KORUUSDT"]', 10000);
        INSERT INTO paper_fills VALUES
            ('a', 'old-run-000001', 'KORUUSDT', 1, 1000.0, 0.30, 0.0),
            ('b', 'current-run-03', 'KORUUSDT', 0, 1000.0, -0.10, 0.25);
        INSERT INTO paper_fill_markouts VALUES
            ('old-run-000001', 'BUY', 1000, 2.0),
            ('old-run-000001', 'BUY', 1000, 4.0),
            ('current-run-03', 'SELL', 1000, -1.0),
            ('current-run-03', 'SELL', 5000, -3.0);
        INSERT INTO paper_strategy_samples VALUES
            ('old-run-000001', 'KORUUSDT', 'QUOTING', 10.0, 9.99, 10.01, 9.95, 10.05),
            ('current-run-03', 'KORUUSDT', 'QUOTING', 10.0, 9.99, 10.01, 9.99, 10.03),
            ('current-run-03', 'KORUUSDT', 'QUOTING', 10.0, 9.99, 10.01, 9.98, 10.01),
            ('current-run-03', 'KORUUSDT', 'WARMING_UP', 10.0, 9.99, 10.01, 0, 0);
        """
    )
    connection.commit()
    connection.close()

    summary = PaperLedgerSummary(path).snapshot(current_run_id="current-run-03")

    one_second = summary["markouts"][0]
    assert one_second["horizon_ms"] == 1000
    assert one_second["samples"] == 3
    assert one_second["mean_bps"] == pytest.approx(5.0 / 3.0)
    # [2, 4, -1]: squared deviations sum to 114/9, sample variance (114/9)/2.
    assert one_second["standard_error_bps"] == pytest.approx(
        ((114.0 / 9.0) / 2.0 / 3.0) ** 0.5
    )
    assert one_second["t_stat"] == pytest.approx(
        (5.0 / 3.0) / ((114.0 / 9.0) / 2.0 / 3.0) ** 0.5
    )
    assert one_second["favourable_share"] == pytest.approx(2.0 / 3.0)
    assert one_second["buy"]["mean_bps"] == pytest.approx(3.0)
    assert one_second["sell"]["samples"] == 1
    assert summary["markouts"][1]["horizon_ms"] == 5000

    (symbol,) = summary["symbols"]
    assert symbol["fills"] == 2
    assert symbol["net_pnl"] == pytest.approx(-0.05)
    assert symbol["net_bps"] == pytest.approx(-0.25)

    # Quote position covers the current run only.
    (position,) = summary["quote_position"]
    assert position["samples"] == 3
    assert position["quoting_share"] == pytest.approx(2.0 / 3.0)
    assert position["bid_at_touch_share"] == pytest.approx(0.5)
    assert position["ask_at_touch_share"] == pytest.approx(0.5)
    assert position["bid_behind_bps"] == pytest.approx(5.0)
    assert position["ask_behind_bps"] == pytest.approx(10.0)
