"""Read Paper run and fill history from the local SQLite projection."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = PROJECT_ROOT / "storage" / "paper" / "trades.sqlite3"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Query ChronosHFT Paper fills")
    parser.add_argument(
        "--database",
        default=str(DEFAULT_DATABASE),
        help="Paper SQLite path",
    )
    parser.add_argument("--symbol", default="", help="Optional symbol filter")
    parser.add_argument("--run-id", default="", help="Optional Paper run filter")
    parser.add_argument(
        "--dataset",
        choices=(
            "fills",
            "orders",
            "strategy",
            "markouts",
            "accounts",
            "system",
            "runtime",
            "markets",
        ),
        default="fills",
        help="Dataset to query (default: fills)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum rows (default: 100, maximum: 10000)",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Group fills by run and symbol",
    )
    return parser.parse_args(argv)


def _connect_read_only(path: Path):
    if not path.is_file():
        raise FileNotFoundError(f"Paper trade database not found: {path}")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def query_rows(connection, *, symbol: str, run_id: str, limit: int, summary: bool):
    where = []
    parameters = []
    if symbol:
        where.append("symbol = ?")
        parameters.append(symbol.upper())
    if run_id:
        where.append("run_id = ?")
        parameters.append(run_id)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    if summary:
        sql = f"""
            SELECT
                run_id,
                symbol,
                COUNT(*) AS fill_count,
                SUM(fill_qty) AS filled_quantity,
                SUM(fill_notional) AS filled_notional,
                SUM(COALESCE(commission, booked_fee, 0.0)) AS commission,
                SUM(COALESCE(realized_pnl, 0.0)) AS realized_pnl,
                SUM(CASE WHEN is_maker = 1 THEN 1 ELSE 0 END) AS maker_fills,
                MIN(exchange_time) AS first_exchange_time,
                MAX(exchange_time) AS last_exchange_time
            FROM paper_fills
            {where_sql}
            GROUP BY run_id, symbol
            ORDER BY MAX(journal_seq) DESC
            LIMIT ?
        """
    else:
        sql = f"""
            SELECT
                journal_seq,
                run_id,
                execution_id,
                symbol,
                side,
                fill_qty,
                fill_price,
                fill_notional,
                commission,
                booked_fee,
                realized_pnl,
                is_maker,
                is_rpi,
                fill_model,
                client_oid,
                exchange_oid,
                trade_id,
                exchange_time,
                fill_trigger,
                market_trade_id,
                market_trade_price,
                market_trade_qty,
                market_trade_exchange_time,
                market_trade_received_time,
                market_trade_clock_offset_ms,
                market_trade_transport_latency_ms,
                market_trade_local_age_ms,
                queue_ahead_before,
                best_bid_at_fill,
                best_ask_at_fill,
                mid_at_fill,
                quote_age_ms,
                journal_ts_utc
            FROM paper_fills
            {where_sql}
            ORDER BY journal_seq DESC
            LIMIT ?
        """
    parameters.append(max(1, min(10_000, int(limit))))
    return [dict(row) for row in connection.execute(sql, parameters).fetchall()]


def query_observations(
    connection,
    *,
    dataset: str,
    symbol: str,
    run_id: str,
    limit: int,
):
    specs = {
        "orders": (
            "paper_order_events",
            """
                event_id, run_id, client_oid, exchange_oid, symbol,
                strategy_id, side, status, price, quantity, filled_quantity,
                average_price, time_in_force, is_post_only, is_rpi,
                order_type, reduce_only, tag,
                created_monotonic, updated_monotonic, event_time, error_message,
                recorded_at_utc
            """,
            "event_id",
            True,
        ),
        "strategy": (
            "paper_strategy_samples",
            """
                sample_id, run_id, sample_time, symbol, strategy_id, state,
                mode, mid_price, best_bid, best_ask, best_bid_qty,
                best_ask_qty, fair_value, target_bid, target_ask,
                market_spread_bps, quote_spread_bps, bid_quote_qty,
                ask_quote_qty, position_qty, position_notional, sigma_bps,
                A_per_s, k_per_bps, bid_markout_cost_bps,
                ask_markout_cost_bps, bid_flow_cost_bps, ask_flow_cost_bps,
                signed_trade_imbalance, microprice_offset_bps,
                bid_queue_latency_cost_bps, ask_queue_latency_cost_bps,
                bid_stale_depth_bps, ask_stale_depth_bps,
                bid_stale_at_risk, ask_stale_at_risk,
                bid_size_multiplier, ask_size_multiplier,
                orderbook_exchange_time, orderbook_received_time,
                orderbook_corrected_received_time,
                orderbook_dispatch_time, clock_offset_ms,
                transport_latency_ms, gateway_processing_latency_ms,
                strategy_queue_latency_ms, callback_age_ms,
                strategy_compute_latency_ms, formula_version, units_version,
                intensity_source, recorded_at_utc
            """,
            "sample_id",
            True,
        ),
        "markouts": (
            "paper_fill_markouts",
            """
                markout_id, run_id, client_oid, trade_id, symbol, side,
                fill_price, horizon_ms, mid_price, signed_markout_bps,
                observation_lag_ms, fill_observed_monotonic,
                mid_observed_monotonic, recorded_at_utc
            """,
            "markout_id",
            True,
        ),
        "accounts": (
            "paper_account_samples",
            """
                sample_id, run_id, sample_time, balance, equity,
                unrealized_pnl, available, used_margin, budget_balance,
                budget_available, maintenance_margin, margin_balance,
                maintenance_margin_ratio, margin_snapshot_time,
                margin_snapshot_synced, external_cash_flow_total,
                cash_flow_snapshot_time, cash_flow_snapshot_synced,
                recorded_at_utc
            """,
            "sample_id",
            False,
        ),
        "system": (
            "paper_system_events",
            """
                event_id, run_id, event_time, event_kind, severity, message,
                state, total_exposure, margin_ratio, order_count_local,
                order_count_remote, cancelling_count, fill_ratio, api_weight,
                is_sync_error, recorded_at_utc
            """,
            "event_id",
            False,
        ),
        "runtime": (
            "paper_system_events",
            """
                event_id, run_id, event_time, event_kind, severity, message,
                state, recorded_at_utc
            """,
            "event_id",
            False,
        ),
        "markets": (
            "paper_market_samples",
            """
                sample_id, run_id, sample_time, symbol, mark_price,
                index_price, basis_bps, funding_rate, next_funding_time,
                exchange_time, received_time, corrected_received_time,
                dispatch_time, clock_offset_ms, transport_latency_ms,
                gateway_processing_latency_ms, recorded_at_utc
            """,
            "sample_id",
            True,
        ),
    }
    if dataset not in specs:
        raise ValueError(f"Unsupported Paper dataset: {dataset}")
    table, columns, order_column, supports_symbol = specs[dataset]
    where = []
    parameters = []
    if symbol:
        if not supports_symbol:
            raise ValueError(f"--symbol is not supported for {dataset}")
        where.append("symbol = ?")
        parameters.append(symbol.upper())
    if run_id:
        where.append("run_id = ?")
        parameters.append(run_id)
    if dataset == "runtime":
        where.append("event_kind = ?")
        parameters.append("runtime_resources")
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    parameters.append(max(1, min(10_000, int(limit))))
    sql = f"""
        SELECT {columns}
        FROM {table}
        {where_sql}
        ORDER BY {order_column} DESC
        LIMIT ?
    """
    rows = [
        dict(row) for row in connection.execute(sql, parameters).fetchall()
    ]
    if dataset == "runtime":
        for row in rows:
            try:
                row["runtime"] = json.loads(row.get("message", ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                row["runtime"] = None
    return rows


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        with _connect_read_only(Path(args.database)) as connection:
            filters = {
                "symbol": str(args.symbol or "").strip(),
                "run_id": str(args.run_id or "").strip(),
                "limit": args.limit,
            }
            if args.dataset == "fills":
                rows = query_rows(
                    connection,
                    summary=bool(args.summary),
                    **filters,
                )
            else:
                if args.summary:
                    raise ValueError("--summary currently applies only to fills")
                rows = query_observations(
                    connection,
                    dataset=args.dataset,
                    **filters,
                )
    except (FileNotFoundError, sqlite3.Error, ValueError) as exc:
        print(f"Paper trade query failed: {exc}")
        return 2

    print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
