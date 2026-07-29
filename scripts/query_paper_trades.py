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
                journal_ts_utc
            FROM paper_fills
            {where_sql}
            ORDER BY journal_seq DESC
            LIMIT ?
        """
    parameters.append(max(1, min(10_000, int(limit))))
    return [dict(row) for row in connection.execute(sql, parameters).fetchall()]


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        with _connect_read_only(Path(args.database)) as connection:
            rows = query_rows(
                connection,
                symbol=str(args.symbol or "").strip(),
                run_id=str(args.run_id or "").strip(),
                limit=args.limit,
                summary=bool(args.summary),
            )
    except (FileNotFoundError, sqlite3.Error, ValueError) as exc:
        print(f"Paper trade query failed: {exc}")
        return 2

    print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
