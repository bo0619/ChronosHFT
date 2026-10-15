"""Replay a GRVT maker that prices off Binance, on recorded data.

GRVT follows Binance: on the exchange clock, ~10 ms after a Binance move
GRVT has made only 12-17% of its response, and 10 ms is about how long it
takes us to see Binance and land a GRVT order. This tests whether quoting
on GRVT around a Binance-based fair value earns a positive edge.

Fair value is the observed Binance mid times the GRVT/Binance basis, an
exponential average (``--basis-half-life``, seconds) of
``GRVT mid / Binance mid`` updated on each GRVT book we receive, so a
standing price difference between the venues (funding, index) does not
read as a signal. Each side holds one post-only order of ``--notional``:

* ``--anchor grvt``: join the GRVT touch and ignore Binance (baseline);
* ``--anchor binance``: the bid target is ``fair * (1 - half_spread)``
  rounded down to the tick, capped at the GRVT best bid, or, with
  ``--improve``, allowed inside the spread up to one tick below the best
  ask (and the mirror for the ask). With a cap at the touch this is a
  guard: a quote steps back when fair comes within ``--half-spread-bps``.

A quote more aggressive than its target is replaced at once; one behind it
by more than ``--keep-ticks`` is replaced after ``--min-rest-sec``. A
filled side quotes again on the next decision.

Clocks as in ``replay_queue_quotes.py``: the GRVT venue moves on GRVT's
exchange time (trades and books from the recording), decisions are taken
when a GRVT book or a Binance bookTicker is received, and an order or
cancel lands on GRVT after the order latency (``--latency-ms``, or half a
round trip drawn from ``--latency-samples``, probe ``grvt_rest_rtt`` by
default, plus ``--extra-latency-ms``). Fills use the Paper matching engine:
queue position at the touch, trades at (``at_price``) or through our price,
and an opposite touch that reaches our price (``cross``; pessimistic when
those joiners are post-only: with ``--cross skip`` the quote is pulled
without a fill instead, the optimistic bound).

Reported per symbol: fills per hour, fill mix, mean markout to the GRVT mid
1/5/30 s later in bps (positive in our favour), the 5 s edge net of
``--maker-fee-bps``, and the 5 s P&L in USDT per hour. Inventory is not
modelled or hedged.

Reads ``<grvt-dir>/<SYMBOL>_{depth,trade}_*_v2.h5`` and
``<binance-dir>/<SYMBOL>_ticker_*_v2.h5``.

Example::

    python scripts/replay_cross_venue.py --grvt-dir storage/research/grvt \\
        --binance-dir storage/research/binance --symbols SOXLUSDT \\
        --start 2026-10-14T16:00 --end 2026-10-15T10:00 --slice-hours 3 \\
        --anchor binance --half-spread-bps 2 --improve
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import replay_queue_quotes as rq  # noqa: E402
from event.type import TIF_GTX, AggTradeData, OrderRequest  # noqa: E402
from gateway.binance.paper_matching import (  # noqa: E402
    PaperMatchingEngine,
    PaperMatchingPolicy,
    PaperMatchingPort,
    PaperVenueState,
)
from gateway.binance.paper_state import PaperOrder  # noqa: E402

HORIZONS = (1.0, 5.0, 30.0)
EPS = 1e-12
# Same-time order: venue state first, then what we observe.
TRADE, DEPTH, SEE_GRVT, SEE_BINANCE = 0, 1, 2, 3


def load_columns(directory: str, symbol: str, kind: str, columns: list[str],
                 start: float | None, end: float | None) -> pd.DataFrame:
    """Chosen columns of a recording within a receipt-time window, in chunks."""
    parts = []
    for path in sorted(glob.glob(f"{directory}/{symbol}_{kind}_*_v2.h5")):
        for chunk in pd.read_hdf(path, kind, chunksize=1_000_000, columns=columns):
            keep = chunk.received_ts > 0
            if start is not None:
                keep &= chunk.received_ts >= start
            if end is not None:
                keep &= chunk.received_ts < end
            if keep.any():
                parts.append(chunk[keep])
    if not parts:
        return pd.DataFrame(columns=columns)
    return pd.concat(parts).sort_values("received_ts").reset_index(drop=True)


def round_down(price: float, tick: float) -> float:
    return round(math.floor(price / tick + 1e-9) * tick, 10)


def round_up(price: float, tick: float) -> float:
    return round(math.ceil(price / tick - 1e-9) * tick, 10)


def replay(symbol: str, grvt_depth: pd.DataFrame, grvt_trades: pd.DataFrame,
           binance: pd.DataFrame, args) -> dict:
    fills: list[dict] = []
    state = PaperVenueState()
    clock = [0.0]

    def apply_fill(order, quantity, price, *, is_maker, fill_context=None):
        order.cum_filled_qty += quantity
        order.status = "FILLED" if order.remaining <= EPS else "PARTIALLY_FILLED"
        fills.append({"t": clock[0], "side": order.request.side, "price": float(price),
                      "qty": float(quantity), "trigger": (fill_context or {}).get("fill_trigger") or ""})

    engine = PaperMatchingEngine(
        state,
        PaperMatchingPort(apply_fill=apply_fill, expire_order=lambda *_: None),
        PaperMatchingPolicy(
            rpi_fill_model="public_trade_proxy", cancel_ahead_fraction=0.0,
            market_order_max_slippage_bps=100.0, maker_fee=0.0, taker_fee=0.0,
            rpi_commission_rate=0.0, rpi_commission_rates={}, cancel_ahead_model="proportional",
        ),
    )
    tick = args.tick
    rng = np.random.default_rng(args.seed)

    def latency() -> float:
        if args.one_way_samples is not None:
            return float(rng.choice(args.one_way_samples)) / 1000.0
        return args.latency_ms / 1000.0

    seq = itertools.count(1)
    quotes: dict[str, PaperOrder | None] = {"BUY": None, "SELL": None}
    placed_at = {"BUY": 0.0, "SELL": 0.0}
    pending = {"BUY": False, "SELL": False}
    actions: list = []
    touch = [0.0, 0.0, 0.0, 0.0]  # venue truth: bid, bid size, ask, ask size
    seen = {"bid": 0.0, "ask": 0.0, "binance_mid": 0.0, "log_basis": None, "basis_t": 0.0}

    def place(side: str, price: float) -> None:
        bid, bid_size, ask, ask_size = touch
        if (price >= ask - EPS) if side == "BUY" else (price <= bid + EPS):
            return  # post-only reject
        n = next(seq)
        order = PaperOrder(
            client_oid=f"{side}-{n}", exchange_oid=f"x{n}",
            request=OrderRequest(symbol=symbol, price=price, volume=args.notional / price, side=side,
                                 time_in_force=TIF_GTX, post_only=True),
            accept_seq=n, created_ms=int(clock[0] * 1000), created_monotonic=clock[0],
            update_ms=int(clock[0] * 1000), status="NEW", committed=True,
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
            if price is not None:
                place(side, price)
            pending[side] = False

    def cross_fills() -> None:
        bid, _, ask, _ = touch
        for side, order in quotes.items():
            if order is not None and order.active:
                price = order.request.price
                if (price >= ask - EPS) if side == "BUY" else (price <= bid + EPS):
                    if args.cross == "fill":
                        apply_fill(order, order.remaining, price, is_maker=True, fill_context={"fill_trigger": "cross"})
                    else:  # post-only joiners were rejected; we see it and pull the quote
                        order.status = "CANCELED"
                        engine.remove_from_later_local_queue(order, order.remaining)

    def targets() -> dict[str, float | None]:
        bid, ask = seen["bid"], seen["ask"]
        if bid <= 0 or ask <= bid:
            return {"BUY": None, "SELL": None}
        if args.anchor == "grvt":
            return {"BUY": bid, "SELL": ask}
        if seen["binance_mid"] <= 0 or seen["log_basis"] is None:
            return {"BUY": None, "SELL": None}  # no fair value yet: stay out
        fair = seen["binance_mid"] * math.exp(seen["log_basis"])
        half = args.half_spread_bps / 1e4
        want_bid, want_ask = round_down(fair * (1 - half), tick), round_up(fair * (1 + half), tick)
        if args.improve:
            want_bid, want_ask = min(want_bid, round(ask - tick, 10)), max(want_ask, round(bid + tick, 10))
        else:
            want_bid, want_ask = min(want_bid, bid), max(want_ask, ask)
        return {"BUY": want_bid, "SELL": want_ask}

    def decide(t: float) -> None:
        for side, target in targets().items():
            if pending[side]:
                continue
            order = quotes[side]
            if order is not None and not order.active:
                state.orders.pop(order.client_oid, None)
                quotes[side] = order = None
            if target is None:
                if order is not None:
                    pending[side] = True
                    heapq.heappush(actions, (t + latency(), next(seq), side, None))
                continue
            if order is not None:
                behind = (target - order.request.price) if side == "BUY" else (order.request.price - target)
                if not (behind < -EPS or (behind > args.keep_ticks * tick + EPS
                                          and t - placed_at[side] >= args.min_rest_sec)):
                    continue
            pending[side] = True
            heapq.heappush(actions, (t + latency(), next(seq), side, target))

    def see_grvt(t: float, bid: float, ask: float) -> None:
        seen["bid"], seen["ask"] = bid, ask
        if seen["binance_mid"] > 0:
            observed = math.log((bid + ask) / 2 / seen["binance_mid"])
            if seen["log_basis"] is None:
                seen["log_basis"] = observed
            else:
                weight = 1 - 0.5 ** (max(t - seen["basis_t"], 0.0) / args.basis_half_life)
                seen["log_basis"] += weight * (observed - seen["log_basis"])
            seen["basis_t"] = t
        decide(t)

    cols = [c for i in range(1, rq.LEVELS + 1) for c in (f"bid{i}_p", f"bid{i}_v", f"ask{i}_p", f"ask{i}_v")]
    depth_rows = grvt_depth[cols].to_numpy(float)
    g_venue = np.where(grvt_depth.exchange_ts.to_numpy(float) > 0, grvt_depth.exchange_ts.to_numpy(float),
                       grvt_depth.received_ts.to_numpy(float))
    g_seen = grvt_depth.received_ts.to_numpy(float)
    t_venue = np.where(grvt_trades.exchange_ts.to_numpy(float) > 0, grvt_trades.exchange_ts.to_numpy(float),
                       grvt_trades.received_ts.to_numpy(float)) if len(grvt_trades) else np.empty(0)
    b_seen = binance.received_ts.to_numpy(float)
    b_mid = ((binance.bid_p + binance.ask_p) / 2).to_numpy(float)
    parts = [(t_venue, TRADE), (g_venue, DEPTH), (g_seen, SEE_GRVT)]
    if args.anchor == "binance":
        parts.append((b_seen, SEE_BINANCE))
    times = np.concatenate([p for p, _ in parts])
    kinds = np.concatenate([np.full(len(p), k) for p, k in parts])
    index = np.concatenate([np.arange(len(p)) for p, _ in parts])
    order_by = np.lexsort((kinds, times))
    trade_px = grvt_trades.price.to_numpy(float) if len(grvt_trades) else np.empty(0)
    trade_qty = grvt_trades.qty.to_numpy(float) if len(grvt_trades) else np.empty(0)
    trade_mib = grvt_trades.maker_is_buyer.to_numpy(bool) if len(grvt_trades) else np.empty(0, bool)
    trade_id = grvt_trades.trade_id.to_numpy(np.int64) if len(grvt_trades) else np.empty(0, np.int64)

    for t, kind, i in zip(times[order_by], kinds[order_by], index[order_by]):
        run_actions(t)
        clock[0] = t
        if kind == TRADE:
            engine.on_market_trade(AggTradeData(
                symbol=symbol, trade_id=int(trade_id[i]), price=float(trade_px[i]), quantity=float(trade_qty[i]),
                maker_is_buyer=bool(trade_mib[i]), datetime=datetime.fromtimestamp(t, tz=timezone.utc)))
        elif kind == DEPTH:
            book = rq.make_book(symbol, t, depth_rows[i])
            if book is not None:
                engine.on_book(book)
                bid, ask = max(book.bids), min(book.asks)
                touch[:] = [bid, book.bids[bid], ask, book.asks[ask]]
                cross_fills()
        elif kind == SEE_GRVT:
            bid, ask = depth_rows[i][0], depth_rows[i][2]
            if 0 < bid < ask:
                see_grvt(t, bid, ask)
        else:
            seen["binance_mid"] = b_mid[i]
            decide(t)

    order_mid = np.argsort(g_venue, kind="stable")
    mid_times = g_venue[order_mid]
    mids = ((depth_rows[:, 0] + depth_rows[:, 2]) / 2)[order_mid]
    valid = (depth_rows[order_mid, 0] > 0) & (depth_rows[order_mid, 2] > depth_rows[order_mid, 0])
    mid_times, mids = mid_times[valid], mids[valid]
    for fill in fills:
        sign = 1.0 if fill["side"] == "BUY" else -1.0
        for horizon in HORIZONS:
            j = min(len(mids) - 1, int(np.searchsorted(mid_times, fill["t"] + horizon)))
            fill[f"m{int(horizon)}"] = sign * math.log(mids[j] / fill["price"]) * 1e4
    hours = (times.max() - times.min()) / 3600 if len(times) > 1 else 0.0
    return {"hours": hours, "fills": fills}


def summarize(symbol: str, result: dict, label: str, maker_fee_bps: float) -> None:
    fills = [f for f in result["fills"] if f["trigger"]]
    hours = result["hours"]
    if not fills or hours <= 0:
        print(f"\n== {symbol} {label}: no fills over {hours:.1f} h")
        return
    by: dict[str, list] = {}
    for f in fills:
        by.setdefault(f["trigger"], []).append(f)
    notional = sum(f["qty"] * f["price"] for f in fills)
    pnl = sum(f["qty"] * f["price"] * (f["m5"] - maker_fee_bps) / 1e4 for f in fills)
    edge = statistics.mean(f["m5"] for f in fills) - maker_fee_bps
    print(f"\n== {symbol} {label} {hours:.1f} h  fills {len(fills)} ({len(fills) / hours:.1f}/h)  "
          f"notional {notional / hours:,.0f} USDT/h  mix "
          + "  ".join(f"{k} {len(v) / len(fills):.0%}" for k, v in sorted(by.items()))
          + f"\n   net of {maker_fee_bps:g} bps fee: 5 s edge {edge:+.2f} bps/fill, P&L {pnl / hours:+.2f} USDT/h")
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
    parser.add_argument("--grvt-dir", required=True)
    parser.add_argument("--binance-dir", required=True)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--slice-hours", type=float, default=0.0)
    parser.add_argument("--notional", type=float, default=100.0)
    parser.add_argument("--anchor", choices=("grvt", "binance"), default="binance")
    parser.add_argument("--half-spread-bps", type=float, default=2.0,
                        help="Distance of our quotes from the Binance-based fair value")
    parser.add_argument("--improve", action="store_true",
                        help="Allow quotes inside the GRVT spread (otherwise capped at the GRVT touch)")
    parser.add_argument("--basis-half-life", type=float, default=60.0,
                        help="Seconds; half-life of the GRVT/Binance basis average")
    parser.add_argument("--cross", choices=("fill", "skip"), default="fill",
                        help="An opposite touch reaching our price fills us (pessimistic) or not (optimistic)")
    parser.add_argument("--keep-ticks", type=float, default=1.0)
    parser.add_argument("--min-rest-sec", type=float, default=1.0)
    parser.add_argument("--latency-ms", type=float, default=8.0)
    parser.add_argument("--latency-samples", default="")
    parser.add_argument("--latency-probe", default="grvt_rest_rtt")
    parser.add_argument("--extra-latency-ms", type=float, default=0.0)
    parser.add_argument("--maker-fee-bps", type=float, default=0.0,
                        help="GRVT maker fee in bps (negative for a rebate)")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    start = pd.Timestamp(args.start, tz="UTC").timestamp() if args.start else None
    end = pd.Timestamp(args.end, tz="UTC").timestamp() if args.end else None
    args.one_way_samples = None
    if args.latency_samples:
        samples = pd.read_csv(args.latency_samples)
        rtt = samples.loc[samples.probe == args.latency_probe, "ms"].to_numpy(float)
        if not len(rtt):
            raise SystemExit(f"no {args.latency_probe} samples in {args.latency_samples}")
        args.one_way_samples = rtt / 2.0 + args.extra_latency_ms
    depth_cols = ["received_ts", "exchange_ts"] + [
        c for i in range(1, rq.LEVELS + 1) for c in (f"bid{i}_p", f"bid{i}_v", f"ask{i}_p", f"ask{i}_v")]
    trade_cols = ["received_ts", "exchange_ts", "trade_id", "price", "qty", "maker_is_buyer"]
    for symbol in args.symbols.split(","):
        merged = {"hours": 0.0, "fills": []}
        args.tick = None
        for window_start, window_end in rq.windows(start, end, args.slice_hours):
            depth = load_columns(args.grvt_dir, symbol, "depth", depth_cols, window_start, window_end)
            trades = load_columns(args.grvt_dir, symbol, "trade", trade_cols, window_start, window_end)
            binance = load_columns(args.binance_dir, symbol, "ticker", ["received_ts", "bid_p", "ask_p"],
                                   window_start, window_end)
            if depth.empty or (args.anchor == "binance" and binance.empty):
                continue
            if args.tick is None:
                prices = np.unique(np.r_[depth.bid1_p.to_numpy(), depth.ask1_p.to_numpy()])
                diffs = np.diff(prices[prices > 0])
                args.tick = float(np.round(diffs[diffs > 0].min(), 10))
            result = replay(symbol, depth, trades, binance, args)
            merged["hours"] += result["hours"]
            merged["fills"] += result["fills"]
            del depth, trades, binance, result
        latency = (f"sampled {args.latency_probe}/2+{args.extra_latency_ms:g} ms" if args.one_way_samples is not None
                   else f"{args.latency_ms:g} ms")
        label = (f"[anchor {args.anchor}" + (f", +/-{args.half_spread_bps:g} bps" if args.anchor == "binance" else "")
                 + (", improve" if args.improve else "") + f", cross {args.cross}, {latency}]")
        summarize(symbol, merged, label, args.maker_fee_bps)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
