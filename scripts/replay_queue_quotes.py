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

Two clocks (``--event-clock exchange``, the default). The venue's state
moves on the exchange's timestamps: trades at their matching time, books at
their event time. We only learn of an update when it reaches us
(``received_ts``), and a cancel or new order decided then lands at the
venue after the order latency. Ordering everything by receipt instead
(``--event-clock received``, the earlier behaviour) is wrong where feeds
are delayed unevenly: Binance often publishes aggTrade ~150 ms after the
trade, after the book change the trade caused, so a fill by that trade was
seen as the touch moving through the quote (``cross``).

Decisions are made on depth snapshots (``--react depth``, pushed every
100 ms, as the live gateway) or also on every bookTicker (``--react
ticker``, pushed on each change of the best bid or ask). The order latency
is ``--latency-ms``, or drawn per action from measured round trips
(``--latency-samples``, a ``measure_latency.py`` CSV; half the
``--latency-probe`` round trip plus ``--extra-latency-ms`` for the venue's
own handling). Until a cancel lands the old quote can still fill. What
happened in the market is read from the finest data recorded in both
modes, so the two are compared on the same fills: when bookTicker is
recorded, crosses and the touch size a new order queues behind come from
it, and the replay window starts where it starts.

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
# Same-time order: a trade before the book change it caused, venue state
# before what we observe of it.
TRADE, DEPTH, TICKER, SEE_DEPTH, SEE_TICKER = 0, 1, 2, 3, 4
EPS = 1e-12


def load(directory: str, symbol: str, start: float | None, end: float | None):
    def read(kind: str) -> pd.DataFrame:
        files = sorted(glob.glob(f"{directory}/{symbol}_{kind}_*_v2.h5"))
        if not files:
            return pd.DataFrame()
        # Read in chunks and keep only the window: a day of bookTicker is
        # millions of rows, too many to load whole on a small host.
        parts = []
        for path in files:
            for chunk in pd.read_hdf(path, kind, chunksize=1_000_000):
                keep = chunk.received_ts > 0
                if start is not None:
                    keep &= chunk.received_ts >= start
                if end is not None:
                    keep &= chunk.received_ts < end
                if keep.any():
                    parts.append(chunk[keep])
        if not parts:
            return pd.DataFrame()
        return pd.concat(parts).sort_values("received_ts").reset_index(drop=True)

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
            "placed_t": float(order.created_monotonic),
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
    rng = np.random.default_rng(args.seed)
    one_way = getattr(args, "one_way_samples", None)

    def latency() -> float:
        if one_way is not None and len(one_way):
            return float(rng.choice(one_way)) / 1000.0
        return args.latency_ms / 1000.0

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
            heapq.heappush(actions, (t + latency(), next(seq), side, target))

    depth_cols = [c for i in range(1, LEVELS + 1) for c in (f"bid{i}_p", f"bid{i}_v", f"ask{i}_p", f"ask{i}_v")]
    depth_rows = depth[depth_cols].to_numpy(dtype=float)
    trade_px, trade_qty = trades.price.to_numpy(float), trades.qty.to_numpy(float)
    trade_mib = trades.maker_is_buyer.to_numpy(bool)
    trade_ids = trades.trade_id.to_numpy(np.int64) if "trade_id" in trades else np.arange(len(trades))
    tick_rows = ticker[["bid_p", "bid_v", "ask_p", "ask_v"]].to_numpy(float) if not ticker.empty else np.empty((0, 4))
    use_ticker = len(tick_rows) > 0

    def venue_time(frame: pd.DataFrame) -> np.ndarray:
        """When the venue state changed: exchange time, else receipt."""
        if not len(frame):
            return np.empty(0)
        received = frame.received_ts.to_numpy(float)
        if args.event_clock == "received" or "exchange_ts" not in frame:
            return received
        exchange = frame.exchange_ts.to_numpy(float)
        return np.where(exchange > 0, exchange, received)

    depth_venue, ticker_venue, trade_venue = venue_time(depth), venue_time(ticker), venue_time(trades)
    depth_seen = depth.received_ts.to_numpy(float)
    ticker_seen = ticker.received_ts.to_numpy(float) if use_ticker else np.empty(0)
    parts = [(trade_venue, TRADE), (depth_venue, DEPTH), (ticker_venue, TICKER), (depth_seen, SEE_DEPTH)]
    if args.react == "ticker":
        parts.append((ticker_seen, SEE_TICKER))
    times = np.concatenate([t for t, _ in parts])
    kinds = np.concatenate([np.full(len(t), k) for t, k in parts])
    index = np.concatenate([np.arange(len(t)) for t, _ in parts])
    order_by = np.lexsort((kinds, times))
    books: dict[int, OrderBook | None] = {}

    def depth_book(i: int, t: float) -> OrderBook | None:
        # Each depth row is used twice (venue state, then what we see); the
        # two uses are close together, so a small cache avoids rebuilding.
        if i not in books:
            if len(books) > 64:
                books.clear()
            books[i] = make_book(symbol, t, depth_rows[i])
        return books[i]

    for t, kind, i in zip(times[order_by], kinds[order_by], index[order_by]):
        run_actions(t)
        clock[0] = t
        if kind == TRADE:
            engine.on_market_trade(AggTradeData(
                symbol=symbol, trade_id=int(trade_ids[i]), price=float(trade_px[i]),
                quantity=float(trade_qty[i]), maker_is_buyer=bool(trade_mib[i]),
                datetime=datetime.fromtimestamp(t, tz=timezone.utc),
            ))
        elif kind == TICKER:
            bid, bid_size, ask, ask_size = tick_rows[i]
            if 0 < bid < ask:
                touch[:] = [bid, bid_size, ask, ask_size]
                cross_fills()
        elif kind == DEPTH:
            book = depth_book(i, t)
            if book is not None:
                engine.on_book(book)
                if not use_ticker:
                    bid, ask = max(book.bids), min(book.asks)
                    touch[:] = [bid, book.bids[bid], ask, book.asks[ask]]
                    cross_fills()
        elif kind == SEE_DEPTH:
            book = depth_book(i, t)
            if book is not None:
                bid, ask = max(book.bids), min(book.asks)
                decide(t, bid, book.bids[bid], ask, book.asks[ask])
        else:  # SEE_TICKER
            bid, bid_size, ask, ask_size = tick_rows[i]
            if 0 < bid < ask:
                decide(t, bid, bid_size, ask, ask_size)

    if use_ticker:
        mid_order = np.argsort(ticker_venue, kind="stable")
        mid_times = ticker_venue[mid_order]
        mids = ((tick_rows[:, 0] + tick_rows[:, 2]) / 2)[mid_order]
    else:
        mid_order = np.argsort(depth_venue, kind="stable")
        mid_times = depth_venue[mid_order]
        mids = ((depth_rows[:, 0] + depth_rows[:, 2]) / 2)[mid_order]
    hours = (times.max() - times.min()) / 3600 if len(times) > 1 else 0.0
    for fill in fills:
        for horizon in HORIZONS:
            j = min(len(mid_times) - 1, int(np.searchsorted(mid_times, fill["t"] + horizon)))
            sign = 1.0 if fill["side"] == "BUY" else -1.0
            fill[f"m{int(horizon)}"] = sign * math.log(mids[j] / fill["price"]) * 1e4
    return {"hours": hours, "fills": fills, "mid_times": mid_times, "mids": mids}


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
    parser.add_argument("--latency-samples", default="",
                        help="measure_latency.py CSV; draw the order latency from it instead")
    parser.add_argument("--latency-probe", default="binance_ws_api_rtt",
                        help="Round-trip probe in --latency-samples; half of it is one way")
    parser.add_argument("--extra-latency-ms", type=float, default=0.0,
                        help="Added to sampled latency: the venue's own order handling")
    parser.add_argument("--event-clock", default="exchange", choices=("exchange", "received"),
                        help="Order venue events by exchange time (default) or by receipt")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--slice-hours", type=float, default=0.0,
                        help="Replay the window in slices of this many hours to bound memory")
    parser.add_argument("--thin-notional", type=float, default=0.0,
                        help="Step back one tick when less than this USDT of others is left at our touch")
    parser.add_argument("--cancel-ahead-model", default="proportional", choices=("fraction", "proportional"))
    parser.add_argument("--start", default="", help="UTC, e.g. 2026-10-14T06:02")
    parser.add_argument("--end", default="")
    args = parser.parse_args(argv)
    start = pd.Timestamp(args.start, tz="UTC").timestamp() if args.start else None
    end = pd.Timestamp(args.end, tz="UTC").timestamp() if args.end else None
    ticks = {symbol: float(tick) for symbol, tick in (item.split("=") for item in args.ticks.split(",") if item)}
    args.one_way_samples = None
    if args.latency_samples:
        samples = pd.read_csv(args.latency_samples)
        rtt = samples.loc[samples.probe == args.latency_probe, "ms"].to_numpy(float)
        if not len(rtt):
            raise SystemExit(f"no {args.latency_probe} samples in {args.latency_samples}")
        args.one_way_samples = rtt / 2.0 + args.extra_latency_ms
    for symbol in args.symbols.split(","):
        merged = {"hours": 0.0, "fills": []}
        for window_start, window_end in windows(start, end, args.slice_hours):
            depth, trades, ticker = load(args.dir, symbol, window_start, window_end)
            if depth.empty or trades.empty:
                continue
            if symbol not in ticks:
                prices = np.unique(np.concatenate([depth.bid1_p.to_numpy(), depth.ask1_p.to_numpy()]))
                diffs = np.diff(prices[prices > 0])
                ticks[symbol] = np.round(diffs[diffs > 0].min(), 10)
            args.tick = float(ticks[symbol])
            result = replay(symbol, depth, trades, ticker, args)
            merged["hours"] += result["hours"]
            merged["fills"] += result["fills"]
            del depth, trades, ticker, result
        if not merged["fills"] and merged["hours"] == 0.0:
            print(f"{symbol}: no recording in {args.dir}")
            continue
        latency_label = (f"sampled {args.latency_probe}/2+{args.extra_latency_ms:g} ms"
                         if args.one_way_samples is not None else f"{args.latency_ms:g} ms")
        label = f"[{args.event_clock} clock, react {args.react}, {latency_label}" + (
            f", thin {args.thin_notional:g}" if args.thin_notional else "") + "]"
        summarize(symbol, merged, label)
    return 0


def windows(start: float | None, end: float | None, slice_hours: float) -> list:
    """``[start, end)`` in pieces of ``slice_hours`` (one piece if 0).

    A day of bookTicker on a busy symbol does not fit in memory on a small
    host; replaying in slices bounds it. Quotes restart at each boundary,
    which costs a few seconds of queue position per slice.
    """
    if not slice_hours:
        return [(start, end)]
    if start is None or end is None:
        raise SystemExit("--slice-hours needs --start and --end")
    edges = list(np.arange(start, end, slice_hours * 3600.0)) + [end]
    return list(zip(edges[:-1], edges[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
