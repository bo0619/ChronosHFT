"""Replay recorded books and trades through the Paper matching engine.

Answers, per symbol: how does a maker without queue priority do if it keeps
one quote on each side at the touch? It uses the live Paper matching engine
(``gateway.binance.paper_matching``), so queue placement, proportional
cancel-ahead credit, and through/at-price fills match a Paper run.

Quote policy, mirroring GLFT with touch snapping and queue retention:

* each side holds one order of ``--notional`` USDT at the touch, or
  ``--inside-ticks`` better when the spread leaves room (our bid stays at
  least one tick under our ask), which buys price priority: an order inside
  the spread has no queue ahead;
* a quote the touch moves away from is kept (it may be reached later from
  the front of its new level) until it is more than ``--keep-ticks`` behind
  and has rested ``--min-rest-sec``; then it rejoins the touch;
* a quote more aggressive than its target (the touch moved through it
  without trading, or the spread closed) is replaced at once, as GLFT's
  risk requote does;
* with ``--thin-notional``, a quote resting at the touch steps back one
  tick once less than that much USDT of other orders is left at its price,
  instead of being the last order standing there;
* an opposite touch that reaches our price would have traded with us: the
  quote fills (trigger ``cross``);
* a filled quote rejoins the touch.

Decisions are made on depth snapshots (``--react depth``, pushed every
100 ms, as the live gateway) or also on every bookTicker (``--react
ticker``, pushed on each change of the best bid or ask). A cancel or a new
order takes effect ``--latency-ms`` after the decision; until then the old
quote can still fill. What happened in the market is read from the finest
data recorded in both modes, so the two are compared on the same fills:
when bookTicker is recorded, crosses and the touch size a new order queues
behind come from it, and the replay window starts where it starts.

Inventory is not modelled: this measures the per-fill edge of resting at
the touch. Markouts are signed from the fill price to the mid
``1/5/30 s`` later, positive in our favour (as ``paper_fill_markouts``).

Reads ``<dir>/<SYMBOL>_depth_*_v2.h5`` (key ``depth``),
``<dir>/<SYMBOL>_trade_*_v2.h5`` (key ``trade``) and, when present,
``<dir>/<SYMBOL>_ticker_*_v2.h5`` (key ``ticker``), from both the engine
recorder and ``scripts/record_public_book.py``.

Example::

    python scripts/replay_queue_quotes.py --dir storage/research --symbols ARCUSDT,KORUUSDT
    python scripts/replay_queue_quotes.py --symbols KORUUSDT --react ticker --thin-notional 2000
"""

from __future__ import annotations

import argparse
import glob
import heapq
import itertools
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
DEPTH, TICKER, TRADE = 0, 1, 2
EPS = 1e-12


def load(directory: str, symbol: str, start: float | None, end: float | None):
    def read(kind: str) -> pd.DataFrame:
        files = sorted(glob.glob(f"{directory}/{symbol}_{kind}_*_v2.h5"))
        if not files:
            return pd.DataFrame()
        frame = pd.concat(pd.read_hdf(f, kind) for f in files)
        frame = frame[frame.received_ts > 0]
        if start is not None:
            frame = frame[frame.received_ts >= start]
        if end is not None:
            frame = frame[frame.received_ts < end]
        return frame.sort_values("received_ts").reset_index(drop=True)

    depth, trades, ticker = read("depth"), read("trade"), read("ticker")
    if not ticker.empty:
        first = float(ticker.received_ts.iloc[0])
        depth = depth[depth.received_ts >= first].reset_index(drop=True)
        trades = trades[trades.received_ts >= first].reset_index(drop=True)
    return depth, trades, ticker


def make_book(symbol: str, t: float, levels: np.ndarray) -> OrderBook | None:
    """``levels`` is one depth row: bid1_p, bid1_v, ... ask5_p, ask5_v."""
    bids, asks = {}, {}
    for level in range(LEVELS):
        bp, bv, ap, av = levels[4 * level: 4 * level + 4]
        if bp > 0 and bv > 0:
            bids[float(bp)] = float(bv)
        if ap > 0 and av > 0:
            asks[float(ap)] = float(av)
    if not bids or not asks or max(bids) >= min(asks):
        return None
    return OrderBook(
        symbol=symbol, exchange="BINANCE",
        datetime=datetime.fromtimestamp(t, tz=timezone.utc),
        bids=bids, asks=asks,
    )


def replay(symbol: str, depth: pd.DataFrame, trades: pd.DataFrame,
           ticker: pd.DataFrame, args) -> dict:
    if args.react == "ticker" and ticker.empty:
        raise SystemExit(f"{symbol}: --react ticker needs a bookTicker recording")
    fills: list[dict] = []
    state = PaperVenueState()
    clock = [0.0]

    def apply_fill(order, quantity, price, *, is_maker, fill_context=None):
        order.cum_filled_qty += quantity
        order.status = "FILLED" if order.remaining <= EPS else "PARTIALLY_FILLED"
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
    tick = args.tick
    latency = args.latency_ms / 1000.0
    seq = itertools.count(1)
    quotes: dict[str, PaperOrder | None] = {"BUY": None, "SELL": None}
    placed_at = {"BUY": 0.0, "SELL": 0.0}
    pending = {"BUY": False, "SELL": False}
    actions: list[tuple[float, int, str, float]] = []
    # Freshest touch: (bid, bid size, ask, ask size).
    touch = [0.0, 0.0, 0.0, 0.0]

    def place(side: str, price: float) -> None:
        bid, bid_size, ask, ask_size = touch
        if (price >= ask - EPS) if side == "BUY" else (price <= bid + EPS):
            return  # post-only reject; the next decision quotes again
        n = next(seq)
        order = PaperOrder(
            client_oid=f"{side}-{n}", exchange_oid=f"x{n}",
            request=OrderRequest(symbol=symbol, price=price,
                                 volume=args.notional / price, side=side,
                                 time_in_force=TIF_GTX, post_only=True),
            accept_seq=n, created_ms=int(clock[0] * 1000),
            created_monotonic=clock[0], update_ms=int(clock[0] * 1000),
            status="NEW", committed=True,
        )
        state.orders[order.client_oid] = order
        engine.insert_into_local_queue(order)
        if abs(price - (bid if side == "BUY" else ask)) <= EPS:
            order.queue_ahead = bid_size if side == "BUY" else ask_size
        quotes[side], placed_at[side] = order, clock[0]

    def cancel(side: str) -> None:
        order = quotes[side]
        if order is not None and order.active:
            order.status = "CANCELED"
            engine.remove_from_later_local_queue(order, order.remaining)
        if order is not None:
            state.orders.pop(order.client_oid, None)
        quotes[side] = None

    def run_actions(until: float) -> None:
        while actions and actions[0][0] <= until:
            t, _, side, price = heapq.heappop(actions)
            clock[0] = t
            cancel(side)
            place(side, price)
            pending[side] = False

    def cross_fills() -> None:
        bid, _, ask, _ = touch
        for side, order in quotes.items():
            if order is None or not order.active:
                continue
            price = order.request.price
            if (price >= ask - EPS) if side == "BUY" else (price <= bid + EPS):
                apply_fill(order, order.remaining, price, is_maker=True,
                           fill_context={"fill_trigger": "cross"})

    def decide(t: float, bid: float, bid_size: float, ask: float, ask_size: float) -> None:
        spread_ticks = round((ask - bid) / tick)
        inside = min(args.inside_ticks, max(0, (spread_ticks - 1) // 2))
        targets = {"BUY": bid + inside * tick, "SELL": ask - inside * tick}
        for side, size, best in (("BUY", bid_size, bid), ("SELL", ask_size, ask)):
            order = quotes[side]
            if (args.thin_notional > 0 and inside == 0 and order is not None and order.active
                    and abs(order.request.price - best) <= EPS
                    and size * best < args.thin_notional):
                targets[side] += -tick if side == "BUY" else tick
        for side, target in targets.items():
            if pending[side]:
                continue
            target = round(round(target / tick) * tick, 10)
            order = quotes[side]
            if order is not None and not order.active:
                state.orders.pop(order.client_oid, None)
                quotes[side] = order = None
            if order is not None:
                behind = (target - order.request.price) if side == "BUY" else (order.request.price - target)
                if not (behind < -EPS or (
                    behind > args.keep_ticks * tick + EPS and t - placed_at[side] >= args.min_rest_sec
                )):
                    continue
            pending[side] = True
            heapq.heappush(actions, (t + latency, next(seq), side, target))

    depth_cols = [c for i in range(1, LEVELS + 1) for c in (f"bid{i}_p", f"bid{i}_v", f"ask{i}_p", f"ask{i}_v")]
    depth_rows = depth[depth_cols].to_numpy(dtype=float)
    trade_px, trade_qty = trades.price.to_numpy(float), trades.qty.to_numpy(float)
    trade_mib = trades.maker_is_buyer.to_numpy(bool)
    trade_ids = trades.trade_id.to_numpy(np.int64) if "trade_id" in trades else np.arange(len(trades))
    tick_rows = ticker[["bid_p", "bid_v", "ask_p", "ask_v"]].to_numpy(float) if not ticker.empty else np.empty((0, 4))
    times = np.concatenate([depth.received_ts.to_numpy(float), ticker.received_ts.to_numpy(float) if not ticker.empty else [],
                            trades.received_ts.to_numpy(float)])
    kinds = np.concatenate([np.full(len(depth), DEPTH), np.full(len(tick_rows), TICKER), np.full(len(trades), TRADE)])
    index = np.concatenate([np.arange(len(depth)), np.arange(len(tick_rows)), np.arange(len(trades))])
    order_by = np.lexsort((kinds, times))
    use_ticker = len(tick_rows) > 0

    for t, kind, i in zip(times[order_by], kinds[order_by], index[order_by]):
        run_actions(t)
        clock[0] = t
        if kind == TRADE:
            engine.on_market_trade(AggTradeData(
                symbol=symbol, trade_id=int(trade_ids[i]), price=float(trade_px[i]),
                quantity=float(trade_qty[i]), maker_is_buyer=bool(trade_mib[i]),
                datetime=datetime.fromtimestamp(t, tz=timezone.utc),
            ))
            continue
        if kind == TICKER:
            bid, bid_size, ask, ask_size = tick_rows[i]
            if not (0 < bid < ask):
                continue
            touch[:] = [bid, bid_size, ask, ask_size]
            cross_fills()
            if args.react == "ticker":
                decide(t, bid, bid_size, ask, ask_size)
            continue
        book = make_book(symbol, t, depth_rows[i])
        if book is None:
            continue
        engine.on_book(book)
        bid, ask = max(book.bids), min(book.asks)
        if not use_ticker:
            touch[:] = [bid, book.bids[bid], ask, book.asks[ask]]
            cross_fills()
        decide(t, bid, book.bids[bid], ask, book.asks[ask])

    if use_ticker:
        mid_times = ticker.received_ts.to_numpy(float)
        mids = (tick_rows[:, 0] + tick_rows[:, 2]) / 2
    else:
        mid_times = depth.received_ts.to_numpy(float)
        mids = (depth_rows[:, 0] + depth_rows[:, 2]) / 2
    hours = (times.max() - times.min()) / 3600 if len(times) > 1 else 0.0
    for fill in fills:
        for horizon in HORIZONS:
            j = min(len(mid_times) - 1, int(np.searchsorted(mid_times, fill["t"] + horizon)))
            sign = 1.0 if fill["side"] == "BUY" else -1.0
            fill[f"m{int(horizon)}"] = sign * math.log(mids[j] / fill["price"]) * 1e4
    return {"hours": hours, "fills": fills}


def summarize(symbol: str, result: dict, label: str = "") -> None:
    fills = [f for f in result["fills"] if f["trigger"]]
    hours = result["hours"]
    if not fills or hours <= 0:
        print(f"{symbol}: no fills over {hours:.1f} h")
        return
    by = {}
    for f in fills:
        by.setdefault(f["trigger"], []).append(f)
    traded = sum(f["qty"] * f["price"] for f in fills)
    print(f"\n== {symbol} {label} {hours:.1f} h  fills {len(fills)} ({len(fills) / hours:.0f}/h)  "
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
    parser.add_argument("--inside-ticks", type=int, default=0,
                        help="Quote this many ticks inside the touch when the spread allows")
    parser.add_argument("--keep-ticks", type=float, default=2.0)
    parser.add_argument("--min-rest-sec", type=float, default=5.0)
    parser.add_argument("--react", default="depth", choices=("depth", "ticker"),
                        help="Decide on depth snapshots only, or also on every bookTicker")
    parser.add_argument("--latency-ms", type=float, default=5.0,
                        help="Delay from a decision to the cancel or new order taking effect")
    parser.add_argument("--thin-notional", type=float, default=0.0,
                        help="Step back one tick when less than this USDT of others is left at our touch")
    parser.add_argument("--cancel-ahead-model", default="proportional", choices=("fraction", "proportional"))
    parser.add_argument("--start", default="", help="UTC, e.g. 2026-10-14T06:02")
    parser.add_argument("--end", default="")
    args = parser.parse_args(argv)
    start = pd.Timestamp(args.start, tz="UTC").timestamp() if args.start else None
    end = pd.Timestamp(args.end, tz="UTC").timestamp() if args.end else None
    ticks = dict(item.split("=") for item in args.ticks.split(",") if item)
    for symbol in args.symbols.split(","):
        depth, trades, ticker = load(args.dir, symbol, start, end)
        if depth.empty or trades.empty:
            print(f"{symbol}: no recording in {args.dir}")
            continue
        prices = np.unique(np.concatenate([depth.bid1_p.to_numpy(), depth.ask1_p.to_numpy()]))
        diffs = np.diff(prices[prices > 0])
        args.tick = float(ticks.get(symbol) or np.round(diffs[diffs > 0].min(), 10))
        label = f"[react {args.react}, {args.latency_ms:g} ms" + (
            f", thin {args.thin_notional:g}" if args.thin_notional else "") + "]"
        summarize(symbol, replay(symbol, depth, trades, ticker, args), label)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
