"""Replay recorded books and trades through the Paper matching engine.

Answers, per symbol: how does a maker without queue priority do if it keeps
one quote on each side at the touch? It uses the live Paper matching engine
(``gateway.binance.paper_matching``), so queue placement, proportional
cancel-ahead credit, and through/at-price fills match a Paper run.

Quote policy, mirroring GLFT with touch snapping and queue retention:

* each side holds one order of ``--notional`` USDT at the touch;
* a quote the touch moves away from is kept (it may be reached later from
  the front of its new level) until it is more than ``--keep-ticks`` behind
  and has rested ``--min-rest-sec``; then it rejoins the touch;
* a filled quote rejoins the touch on the next book.

Inventory is not modelled: this measures the per-fill edge of resting at
the touch. Markouts are signed from the fill price to the mid
``1/5/30 s`` later, positive in our favour (as ``paper_fill_markouts``).

Reads ``<dir>/<SYMBOL>_depth_*_v2.h5`` (key ``depth``) and
``<dir>/<SYMBOL>_trade_*_v2.h5`` (key ``trade``), from both the engine
recorder and ``scripts/record_public_book.py``.

Example::

    python scripts/replay_queue_quotes.py --dir storage/research --symbols ARCUSDT,KORUUSDT
"""

from __future__ import annotations

import argparse
import glob
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from event.type import TIF_GTX, AggTradeData, OrderBook, OrderRequest  # noqa: E402
from gateway.binance.paper_matching import (  # noqa: E402
    PaperMatchingEngine,
    PaperMatchingPolicy,
    PaperMatchingPort,
    PaperVenueState,
)
from gateway.binance.paper_state import PaperOrder  # noqa: E402

HORIZONS = (1.0, 5.0, 30.0)
LEVELS = 5


def load(directory: str, symbol: str, start: float | None, end: float | None):
    def read(kind: str, key: str) -> pd.DataFrame:
        files = sorted(glob.glob(f"{directory}/{symbol}_{kind}_*_v2.h5"))
        if not files:
            return pd.DataFrame()
        frame = pd.concat(pd.read_hdf(f, key) for f in files)
        frame = frame[frame.received_ts > 0]
        if start is not None:
            frame = frame[frame.received_ts >= start]
        if end is not None:
            frame = frame[frame.received_ts < end]
        return frame.sort_values("received_ts").reset_index(drop=True)

    return read("depth", "depth"), read("trade", "trade")


def make_book(symbol: str, row) -> OrderBook | None:
    bids, asks = {}, {}
    for level in range(1, LEVELS + 1):
        bp, bv = getattr(row, f"bid{level}_p", 0.0), getattr(row, f"bid{level}_v", 0.0)
        ap, av = getattr(row, f"ask{level}_p", 0.0), getattr(row, f"ask{level}_v", 0.0)
        if bp > 0 and bv > 0:
            bids[float(bp)] = float(bv)
        if ap > 0 and av > 0:
            asks[float(ap)] = float(av)
    if not bids or not asks or max(bids) >= min(asks):
        return None
    return OrderBook(
        symbol=symbol, exchange="BINANCE",
        datetime=datetime.fromtimestamp(row.received_ts, tz=timezone.utc),
        bids=bids, asks=asks,
    )


def replay(symbol: str, depth: pd.DataFrame, trades: pd.DataFrame, args) -> dict:
    fills: list[dict] = []
    state = PaperVenueState()

    def apply_fill(order, quantity, price, *, is_maker, fill_context=None):
        order.cum_filled_qty += quantity
        if order.remaining <= 1e-12:
            order.status = "FILLED"
        else:
            order.status = "PARTIALLY_FILLED"
        fills.append({
            "t": clock[0], "side": order.request.side, "price": float(price),
            "qty": float(quantity), "trigger": (fill_context or {}).get("fill_trigger") or "",
        })

    engine = PaperMatchingEngine(
        state,
        PaperMatchingPort(apply_fill=apply_fill, expire_order=lambda *_: None),
        PaperMatchingPolicy(
            rpi_fill_model="public_trade_proxy", cancel_ahead_fraction=0.0,
            market_order_max_slippage_bps=100.0, maker_fee=0.0, taker_fee=0.0,
            rpi_commission_rate=0.0, rpi_commission_rates={},
            cancel_ahead_model=args.cancel_ahead_model,
        ),
    )
    clock = [0.0]
    seq = [0]
    quotes: dict[str, PaperOrder | None] = {"BUY": None, "SELL": None}
    placed_at = {"BUY": 0.0, "SELL": 0.0}

    def place(side: str, price: float) -> None:
        seq[0] += 1
        order = PaperOrder(
            client_oid=f"{side}-{seq[0]}", exchange_oid=f"x{seq[0]}",
            request=OrderRequest(symbol=symbol, price=price,
                                 volume=args.notional / price, side=side,
                                 time_in_force=TIF_GTX, post_only=True),
            accept_seq=seq[0], created_ms=int(clock[0] * 1000),
            created_monotonic=clock[0], update_ms=int(clock[0] * 1000),
            status="NEW", committed=True,
        )
        state.orders[order.client_oid] = order
        engine.insert_into_local_queue(order)
        quotes[side], placed_at[side] = order, clock[0]

    def cancel(side: str) -> None:
        order = quotes[side]
        if order is not None and order.active:
            order.status = "CANCELED"
            engine.remove_from_later_local_queue(order, order.remaining)
        if order is not None:
            state.orders.pop(order.client_oid, None)
        quotes[side] = None

    tick = args.tick
    book_times = depth.received_ts.to_numpy()
    mids = ((depth.bid1_p + depth.ask1_p) / 2).to_numpy()
    events = [(t, 0, i) for i, t in enumerate(book_times)] + [
        (t, 1, i) for i, t in enumerate(trades.received_ts.to_numpy())
    ]
    events.sort()
    for t, kind, index in events:
        clock[0] = t
        if kind == 1:
            row = trades.iloc[index]
            engine.on_market_trade(AggTradeData(
                symbol=symbol, trade_id=int(row.get("trade_id", index) if hasattr(row, "get") else index),
                price=float(row.price), quantity=float(row.qty),
                maker_is_buyer=bool(row.maker_is_buyer),
                datetime=datetime.fromtimestamp(t, tz=timezone.utc),
            ))
            continue
        book = make_book(symbol, depth.iloc[index])
        if book is None:
            continue
        engine.on_book(book)
        best_bid, best_ask = max(book.bids), min(book.asks)
        for side, touch in (("BUY", best_bid), ("SELL", best_ask)):
            order = quotes[side]
            if order is not None and not order.active:
                state.orders.pop(order.client_oid, None)
                quotes[side] = order = None
            if order is None:
                place(side, touch)
                continue
            behind = (touch - order.request.price) if side == "BUY" else (order.request.price - touch)
            if behind > args.keep_ticks * tick + 1e-12 and t - placed_at[side] >= args.min_rest_sec:
                cancel(side)
                place(side, touch)

    hours = (book_times[-1] - book_times[0]) / 3600 if len(book_times) > 1 else 0.0
    for fill in fills:
        for horizon in HORIZONS:
            j = min(len(book_times) - 1, int(np.searchsorted(book_times, fill["t"] + horizon)))
            mid = mids[j]
            sign = 1.0 if fill["side"] == "BUY" else -1.0
            fill[f"m{int(horizon)}"] = sign * math.log(mid / fill["price"]) * 1e4
    return {"hours": hours, "fills": fills}


def summarize(symbol: str, result: dict, notional: float) -> None:
    fills = [f for f in result["fills"] if f["trigger"]]
    hours = result["hours"]
    if not fills or hours <= 0:
        print(f"{symbol}: no fills over {hours:.1f} h")
        return
    by = {}
    for f in fills:
        by.setdefault(f["trigger"], []).append(f)
    traded = sum(f["qty"] * f["price"] for f in fills)
    print(f"\n== {symbol}  {hours:.1f} h  fills {len(fills)} ({len(fills) / hours:.0f}/h)  "
          f"notional {traded / hours:,.0f} USDT/h  mix " +
          "  ".join(f"{k} {len(v) / len(fills):.0%}" for k, v in sorted(by.items())))
    for name, group in [("ALL", fills)] + sorted(by.items()):
        cells = []
        for horizon in HORIZONS:
            values = [f[f"m{int(horizon)}"] for f in group]
            mean = statistics.mean(values)
            se = statistics.stdev(values) / math.sqrt(len(values)) if len(values) > 1 else float("nan")
            cells.append(f"{int(horizon):>2}s {mean:+6.2f} (t {mean / se if se else float('nan'):+5.1f})")
        print(f"   {name:<9} n={len(group):5d}  " + "  ".join(cells))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dir", default="storage/research")
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--ticks", default="", help="SYMBOL=tick,... (default: from the book)")
    parser.add_argument("--notional", type=float, default=500.0)
    parser.add_argument("--keep-ticks", type=float, default=2.0)
    parser.add_argument("--min-rest-sec", type=float, default=5.0)
    parser.add_argument("--cancel-ahead-model", default="proportional", choices=("fraction", "proportional"))
    parser.add_argument("--start", default="", help="UTC, e.g. 2026-10-14T06:02")
    parser.add_argument("--end", default="")
    args = parser.parse_args(argv)
    start = pd.Timestamp(args.start, tz="UTC").timestamp() if args.start else None
    end = pd.Timestamp(args.end, tz="UTC").timestamp() if args.end else None
    ticks = dict(item.split("=") for item in args.ticks.split(",") if item)
    for symbol in args.symbols.split(","):
        depth, trades = load(args.dir, symbol, start, end)
        if depth.empty or trades.empty:
            print(f"{symbol}: no recording in {args.dir}")
            continue
        prices = np.unique(np.concatenate([depth.bid1_p.to_numpy(), depth.ask1_p.to_numpy()]))
        diffs = np.diff(prices[prices > 0])
        args.tick = float(ticks.get(symbol) or np.round(diffs[diffs > 0].min(), 10))
        summarize(symbol, replay(symbol, depth, trades, args), args.notional)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
