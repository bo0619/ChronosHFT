"""Rank Binance USDT perpetuals by how well a back-of-queue maker can quote them.

``rank_tradfi_symbols.py`` favours coarse ticks: a wider spread in bps pays
more per fill. For a maker without queue priority that is a trap. On a
one-tick book everyone queues at one price, time priority decides, and a
quote at the back fills mostly when the price trades through it. KORUUSDT
(~7 bps tick, ~17 average trades queued at the touch, price set on other
markets) showed exactly that.

A back-of-queue maker wants:

* a spread of several ticks, so a quote one tick better than the touch is
  first in its queue (price priority instead of time priority);
* little queued at the touch relative to the average trade;
* price moves of one tick, not sweeps through several levels;
* a half-spread that is large against short-horizon volatility.

All inputs are public (no key): ``exchangeInfo``, ``ticker/24hr``, sampled
``depth`` (limit 5) and the last ``aggTrades``. The score is a screen only:
``scripts/replay_queue_quotes.py`` on recorded data is the real test.

Example::

    python scripts/rank_queue_friendly.py --top 120 --samples 5 --csv queue_rank.csv
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
import time
from collections.abc import Mapping
from typing import Any

import requests

BASE_URL = "https://fapi.binance.com"
BPS = 1e4
# Stay well under Binance's 2,400 request weight per minute.
WEIGHT_PER_MINUTE = 1_200
DEPTH_WEIGHT = 2
AGG_TRADES_WEIGHT = 20

OUTPUT_FIELDS = (
    "rank",
    "symbol",
    "score",
    "contract_type",
    "rpi",
    "quote_volume_usd",
    "trades_24h",
    "last_price",
    "tick_bps",
    "spread_ticks_median",
    "spread_bps_median",
    "share_spread_one_tick",
    "touch_notional_usd",
    "avg_trade_usd",
    "trades_to_clear_touch",
    "multi_tick_move_share",
    "sigma_1s_bps",
    "half_spread_over_sigma",
)


class WeightPacer:
    """Sleep so the requests sent stay under a weight budget per minute."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self.window_start = time.monotonic()
        self.used = 0

    def spend(self, weight: int) -> None:
        now = time.monotonic()
        if now - self.window_start >= 60.0:
            self.window_start, self.used = now, 0
        if self.used + weight > self.per_minute:
            time.sleep(max(0.0, 60.0 - (now - self.window_start)))
            self.window_start, self.used = time.monotonic(), 0
        self.used += weight


def get(session: requests.Session, path: str, **params) -> Any:
    response = session.get(f"{BASE_URL}{path}", params=params, timeout=10)
    response.raise_for_status()
    return response.json()


def contracts(session: requests.Session) -> dict[str, dict]:
    info = get(session, "/fapi/v1/exchangeInfo")
    out = {}
    for item in info["symbols"]:
        if (
            item.get("status") != "TRADING"
            or item.get("quoteAsset") != "USDT"
            or item.get("contractType") not in {"PERPETUAL", "TRADIFI_PERPETUAL"}
        ):
            continue
        tick = next(
            float(f["tickSize"])
            for f in item["filters"]
            if f["filterType"] == "PRICE_FILTER"
        )
        out[item["symbol"]] = {
            "tick": tick,
            "contract_type": item["contractType"],
            "rpi": "RPI" in (item.get("permissionSets") or []),
        }
    return out


def book_metrics(depth_samples: list[Mapping], tick: float) -> dict[str, float]:
    spreads, touch = [], []
    for depth in depth_samples:
        bids, asks = depth.get("bids") or [], depth.get("asks") or []
        if not bids or not asks:
            continue
        bid_p, bid_q = float(bids[0][0]), float(bids[0][1])
        ask_p, ask_q = float(asks[0][0]), float(asks[0][1])
        if ask_p <= bid_p:
            continue
        spreads.append(round((ask_p - bid_p) / tick))
        touch.append((bid_p * bid_q + ask_p * ask_q) / 2.0)
    if not spreads:
        return {}
    return {
        "spread_ticks_median": statistics.median(spreads),
        "share_spread_one_tick": sum(1 for s in spreads if s <= 1) / len(spreads),
        "touch_notional_usd": statistics.median(touch),
    }


def trade_metrics(trades: list[Mapping], tick: float, price: float) -> dict[str, float]:
    """Multi-tick move share and 1 s volatility from the last aggTrades.

    Each aggressor side is read on its own: consecutive buys trace the ask
    and consecutive sells the bid. Mixing them would count every bid/ask
    alternation on a multi-tick spread as a multi-tick move and as
    volatility (bid-ask bounce).
    """
    if len(trades) < 20:
        return {}
    moves, returns = [], []
    for maker_is_buyer in (True, False):
        side = [t for t in trades if bool(t["m"]) == maker_is_buyer]
        prices = [float(t["p"]) for t in side]
        moves += [abs(b - a) / tick for a, b in zip(prices, prices[1:]) if b != a]
        by_second: dict[int, float] = {}
        for t in side:
            by_second[int(t["T"]) // 1000] = float(t["p"])
        seconds = sorted(by_second)
        returns += [
            math.log(by_second[b] / by_second[a]) * BPS / math.sqrt(b - a)
            for a, b in zip(seconds, seconds[1:])
            if b > a
        ]
    multi = sum(1 for m in moves if m >= 1.5) / len(moves) if moves else 0.0
    sigma = statistics.pstdev(returns) if len(returns) > 2 else float("nan")
    return {"multi_tick_move_share": multi, "sigma_1s_bps": sigma}


def score(rows: list[dict]) -> None:
    """Average percentile across the four criteria (higher is friendlier)."""
    criteria = (
        ("spread_ticks_median", True),
        ("trades_to_clear_touch", False),
        ("multi_tick_move_share", False),
        ("half_spread_over_sigma", True),
    )
    for name, higher_is_better in criteria:
        values = sorted(r[name] for r in rows)
        for row in rows:
            pct = values.index(row[name]) / max(1, len(values) - 1)
            row.setdefault("_pct", []).append(pct if higher_is_better else 1.0 - pct)
    for row in rows:
        row["score"] = round(sum(row.pop("_pct")) / len(criteria), 3)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--top", type=int, default=120, help="Candidates by 24h quote volume")
    parser.add_argument("--min-volume", type=float, default=20e6, help="Minimum 24h quote volume (USD)")
    parser.add_argument("--samples", type=int, default=5, help="Depth snapshots per symbol")
    parser.add_argument("--show", type=int, default=30, help="Rows to print")
    parser.add_argument("--csv", default="", help="Write all rows to this CSV")
    args = parser.parse_args(argv)

    session = requests.Session()
    pacer = WeightPacer(WEIGHT_PER_MINUTE)
    pacer.spend(1 + 40)
    meta = contracts(session)
    tickers = {
        t["symbol"]: t
        for t in get(session, "/fapi/v1/ticker/24hr")
        if t["symbol"] in meta and float(t["quoteVolume"]) >= args.min_volume
    }
    candidates = sorted(tickers, key=lambda s: -float(tickers[s]["quoteVolume"]))[: args.top]
    print(f"{len(candidates)} candidates", file=sys.stderr)

    rows = []
    for index, symbol in enumerate(candidates, 1):
        ticker, info = tickers[symbol], meta[symbol]
        price, tick = float(ticker["lastPrice"]), info["tick"]
        if price <= 0.0:
            continue
        depths = []
        for _ in range(max(1, args.samples)):
            pacer.spend(DEPTH_WEIGHT)
            depths.append(get(session, "/fapi/v1/depth", symbol=symbol, limit=5))
            time.sleep(0.2)
        pacer.spend(AGG_TRADES_WEIGHT)
        trades = get(session, "/fapi/v1/aggTrades", symbol=symbol, limit=1000)
        book, flow = book_metrics(depths, tick), trade_metrics(trades, tick, price)
        count = float(ticker["count"] or 0)
        if not book or not flow or count <= 0 or not math.isfinite(flow["sigma_1s_bps"]):
            continue
        tick_bps = tick / price * BPS
        avg_trade = float(ticker["quoteVolume"]) / count
        half_spread = book["spread_ticks_median"] * tick_bps / 2.0
        rows.append(
            {
                "symbol": symbol,
                "contract_type": info["contract_type"],
                "rpi": info["rpi"],
                "quote_volume_usd": round(float(ticker["quoteVolume"])),
                "trades_24h": int(count),
                "last_price": price,
                "tick_bps": round(tick_bps, 3),
                "spread_ticks_median": book["spread_ticks_median"],
                "spread_bps_median": round(book["spread_ticks_median"] * tick_bps, 3),
                "share_spread_one_tick": round(book["share_spread_one_tick"], 3),
                "touch_notional_usd": round(book["touch_notional_usd"]),
                "avg_trade_usd": round(avg_trade),
                "trades_to_clear_touch": round(book["touch_notional_usd"] / avg_trade, 2),
                "multi_tick_move_share": round(flow["multi_tick_move_share"], 3),
                "sigma_1s_bps": round(flow["sigma_1s_bps"], 3),
                "half_spread_over_sigma": round(half_spread / flow["sigma_1s_bps"], 3)
                if flow["sigma_1s_bps"] > 0
                else 0.0,
            }
        )
        print(f"  {index}/{len(candidates)} {symbol}", file=sys.stderr)

    score(rows)
    rows.sort(key=lambda r: -r["score"])
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    if args.csv:
        with open(args.csv, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    header = (
        f"{'#':>3} {'symbol':<14} {'score':>5} {'type':<6} {'rpi':<3} {'vol$M':>7} "
        f"{'tick':>6} {'sprd_t':>6} {'1tick%':>6} {'q/trd':>7} {'jump%':>6} {'h/sig':>6}"
    )
    print(header)
    for row in rows[: args.show]:
        print(
            f"{row['rank']:>3} {row['symbol']:<14} {row['score']:>5.2f} "
            f"{'tradfi' if row['contract_type'] == 'TRADIFI_PERPETUAL' else 'crypto':<6} "
            f"{'Y' if row['rpi'] else '-':<3} {row['quote_volume_usd'] / 1e6:>7.1f} "
            f"{row['tick_bps']:>6.2f} {row['spread_ticks_median']:>6.1f} "
            f"{row['share_spread_one_tick'] * 100:>5.0f}% {row['trades_to_clear_touch']:>7.1f} "
            f"{row['multi_tick_move_share'] * 100:>5.0f}% {row['half_spread_over_sigma']:>6.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
