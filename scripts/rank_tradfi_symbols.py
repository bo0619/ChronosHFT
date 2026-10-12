"""Rank Binance TradFi perpetuals by activity and tick coarseness.

Market making only pays when half the quoted spread covers the adverse move
after a passive fill. A coarse tick (in bps of price) forces a wider spread, so
the useful candidates are liquid contracts whose tick is not negligible.

Sources (all public, no key): ``/fapi/v1/exchangeInfo`` for contract type and
tick size, ``/fapi/v1/ticker/24hr`` for volume, and ``/fapi/v1/ticker/bookTicker``
sampled ``--samples`` times for the quoted spread.

Example::

    python scripts/rank_tradfi_symbols.py --top 30 --samples 10 --csv tradfi_rank.csv
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
import time
from collections.abc import Iterable, Mapping
from typing import Any

import requests

BASE_URL = "https://fapi.binance.com"
BPS = 1e4

OUTPUT_FIELDS = (
    "symbol",
    "quote_volume_usd",
    "trades_24h",
    "last_price",
    "tick_size",
    "tick_bps",
    "spread_bps_median",
    "spread_ticks_median",
    "rpi",
)


def _get(path: str, *, timeout: float = 15.0) -> Any:
    response = requests.get(BASE_URL + path, timeout=timeout)
    response.raise_for_status()
    return response.json()


def tradfi_contracts(exchange_info: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Map trading TradFi perpetual symbols to tick size and RPI support."""
    contracts: dict[str, dict[str, Any]] = {}
    for raw in exchange_info.get("symbols", []):
        if raw.get("contractType") != "TRADIFI_PERPETUAL":
            continue
        if raw.get("status") != "TRADING":
            continue
        tick = 0.0
        for item in raw.get("filters", []):
            if item.get("filterType") == "PRICE_FILTER":
                tick = float(item.get("tickSize") or 0.0)
        permissions = str(raw.get("permissionSets", ""))
        contracts[raw["symbol"]] = {"tick_size": tick, "rpi": "RPI" in permissions}
    return contracts


def rank_rows(
    contracts: Mapping[str, Mapping[str, Any]],
    tickers: Iterable[Mapping[str, Any]],
    spreads_bps: Mapping[str, list[float]],
) -> list[dict[str, Any]]:
    """Join contract, 24h ticker and spread samples; sort by quote volume."""
    rows = []
    for ticker in tickers:
        symbol = ticker.get("symbol")
        if symbol not in contracts:
            continue
        price = float(ticker.get("lastPrice") or 0.0)
        tick = float(contracts[symbol]["tick_size"])
        tick_bps = tick / price * BPS if price > 0 else float("nan")
        samples = spreads_bps.get(symbol) or []
        spread = statistics.median(samples) if samples else float("nan")
        rows.append(
            {
                "symbol": symbol,
                "quote_volume_usd": float(ticker.get("quoteVolume") or 0.0),
                "trades_24h": int(ticker.get("count") or 0),
                "last_price": price,
                "tick_size": tick,
                "tick_bps": tick_bps,
                "spread_bps_median": spread,
                "spread_ticks_median": spread / tick_bps if tick_bps > 0 else float("nan"),
                "rpi": bool(contracts[symbol]["rpi"]),
            }
        )
    rows.sort(key=lambda row: row["quote_volume_usd"], reverse=True)
    return rows


def sample_spreads(symbols: set[str], samples: int, interval: float) -> dict[str, list[float]]:
    spreads: dict[str, list[float]] = {symbol: [] for symbol in symbols}
    for index in range(samples):
        for book in _get("/fapi/v1/ticker/bookTicker"):
            symbol = book.get("symbol")
            if symbol not in spreads:
                continue
            bid = float(book.get("bidPrice") or 0.0)
            ask = float(book.get("askPrice") or 0.0)
            if bid > 0 and ask > bid:
                spreads[symbol].append((ask - bid) / ((ask + bid) / 2) * BPS)
        if index + 1 < samples:
            time.sleep(interval)
    return spreads


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--samples", type=int, default=5, help="bookTicker snapshots")
    parser.add_argument("--interval", type=float, default=2.0, help="Seconds between snapshots")
    parser.add_argument("--csv", help="Also write all rows to this CSV file")
    args = parser.parse_args(argv)

    contracts = tradfi_contracts(_get("/fapi/v1/exchangeInfo"))
    tickers = _get("/fapi/v1/ticker/24hr")
    spreads = sample_spreads(set(contracts), max(args.samples, 1), args.interval)
    rows = rank_rows(contracts, tickers, spreads)

    header = f"{'symbol':<16}{'vol24h(M$)':>12}{'trades':>10}{'tick_bps':>10}{'spread_bps':>12}{'spread_ticks':>14}{'rpi':>5}"
    print(header)
    for row in rows[: args.top]:
        print(
            f"{row['symbol']:<16}{row['quote_volume_usd'] / 1e6:>12.1f}{row['trades_24h']:>10}"
            f"{row['tick_bps']:>10.3f}{row['spread_bps_median']:>12.3f}"
            f"{row['spread_ticks_median']:>14.1f}{'Y' if row['rpi'] else 'N':>5}"
        )
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
