"""Which signals warn that a fill of the GRVT maker will lose?

Runs the cross-venue replay (``scripts/replay_cross_venue.py``) to get the
strategy's own fills, then computes for each fill a set of factors from data
received ``--react-ms`` before it (time enough to pull the quote), each
oriented so that larger means more toxic:

* ``quote_vs_fair``: how far our price is on the wrong side of the latest
  fair value (Binance mid times the GRVT/Binance basis), in bps;
* ``grvt_vs_fair``: GRVT mid against fair, in the direction GRVT is about
  to move against the fill;
* ``binance_ret_50ms`` / ``_200ms`` / ``_1s``: the Binance move over the
  window, against the fill;
* ``binance_updates_500ms``: Binance ticker updates in the last 0.5 s;
* ``since_binance_move``: seconds since the Binance mid last changed,
  negated (a fresh move is riskier);
* ``market_ret_500ms``: the mean Binance move of the other symbols in
  ``--binance-dir`` over 0.5 s, against the fill (cross-asset lead);
* ``binance_vol_10s``: realized Binance volatility over 10 s, bps;
* ``grvt_imbalance``: touch size on our side against the far side, thin
  side positive;
* ``grvt_flow_5s``: GRVT taker notional over 5 s hitting our side, as a
  share of all taker notional then;
* ``grvt_spread_ticks``: the GRVT spread.

The target is the fill's 5 s markout to the GRVT mid (bps, positive in our
favour). Fills before ``--split`` train, later ones test. Per factor:
Spearman correlation with the markout in each half and the test markout by
train-quintile of the factor (bucket edges from the train half, per
symbol). Then a linear combination fit on train (factors standardized per
symbol on train) is scored on test: correlation, markout by quintile, and
what dropping the predicted-worst fills would have changed, an upper bound
on what quote widening could save.

Example::

    python scripts/evaluate_maker_toxicity.py --grvt-dir storage/research2/grvt \\
        --binance-dir storage/research2/binance --symbols XRPUSDT,PUMPUSDT \\
        --start 2026-10-17T03:46 --end 2026-10-18T23:10 --split 2026-10-18T09:10 \\
        --latency-samples storage/research2/latency/latency_20261018_220001.csv.gz
"""

from __future__ import annotations

import argparse
import glob
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import replay_cross_venue as cv  # noqa: E402
import replay_queue_quotes as rq  # noqa: E402
from strategy.grvt_cross_maker import BasisTracker  # noqa: E402

FACTORS = ("quote_vs_fair", "grvt_vs_fair", "binance_ret_50ms", "binance_ret_200ms", "binance_ret_1s",
           "binance_updates_500ms", "since_binance_move", "market_ret_500ms", "binance_vol_10s",
           "grvt_imbalance", "grvt_flow_5s", "grvt_spread_ticks")


def last_index(times: np.ndarray, at: np.ndarray) -> np.ndarray:
    return np.searchsorted(times, at, side="right") - 1


def binance_series(directory: str, symbol: str, start: float, end: float) -> tuple[np.ndarray, np.ndarray]:
    frame = cv.load_columns(directory, symbol, "ticker", ["received_ts", "bid_p", "ask_p"], start, end)
    frame = frame[(frame.bid_p > 0) & (frame.ask_p >= frame.bid_p)]
    return frame.received_ts.to_numpy(float), np.log(((frame.bid_p + frame.ask_p) / 2).to_numpy(float))


def move_against(log_mid: np.ndarray, times: np.ndarray, at: np.ndarray, window: float,
                 maker_buy: np.ndarray) -> np.ndarray:
    """Move over ``window`` before ``at``, in bps, positive when it goes against the maker."""
    now = last_index(times, at)
    then = last_index(times, at - window)
    move = np.where((now >= 0) & (then >= 0), log_mid[np.maximum(now, 0)] - log_mid[np.maximum(then, 0)], 0.0) * 1e4
    return np.where(maker_buy, -move, move)


def fill_factors(fills: pd.DataFrame, depth: pd.DataFrame, trades: pd.DataFrame, b_times: np.ndarray,
                 b_logmid: np.ndarray, others: list[tuple[np.ndarray, np.ndarray]], *, react_sec: float,
                 basis_half_life: float, tick: float) -> pd.DataFrame:
    """Factors for each fill from what was received ``react_sec`` before it."""
    at = fills.t.to_numpy(float) - react_sec
    buy = (fills.side == "BUY").to_numpy()
    price = fills.price.to_numpy(float)
    out = pd.DataFrame(index=fills.index)

    # GRVT book as received, with the replay's basis average on each book.
    d_times = depth.received_ts.to_numpy(float)
    bid, ask = depth.bid1_p.to_numpy(float), depth.ask1_p.to_numpy(float)
    bid_v, ask_v = depth.bid1_v.to_numpy(float), depth.ask1_v.to_numpy(float)
    b_mid = np.exp(b_logmid)
    seen = last_index(b_times, d_times)
    basis = BasisTracker(basis_half_life)
    log_basis = np.full(len(d_times), np.nan)
    for i, (t, bb, aa, j) in enumerate(zip(d_times, bid, ask, seen)):
        if bb > 0 and aa > bb and j >= 0:
            value = basis.update(t, bb, aa, b_mid[j])
            log_basis[i] = np.nan if value is None else value
    k = last_index(d_times, at)
    ok = k >= 0
    k = np.maximum(k, 0)
    j = np.maximum(last_index(b_times, at), 0)
    fair = b_mid[j] * np.exp(log_basis[k])
    grvt_mid = (bid[k] + ask[k]) / 2
    sign = np.where(buy, 1.0, -1.0)
    out["quote_vs_fair"] = sign * (price - fair) / fair * 1e4
    out["grvt_vs_fair"] = sign * (grvt_mid - fair) / fair * 1e4
    for name, window in (("binance_ret_50ms", 0.05), ("binance_ret_200ms", 0.2), ("binance_ret_1s", 1.0)):
        out[name] = move_against(b_logmid, b_times, at, window, buy)
    out["binance_updates_500ms"] = (np.searchsorted(b_times, at, side="right")
                                    - np.searchsorted(b_times, at - 0.5, side="right")).astype(float)
    changed = np.r_[True, np.diff(b_logmid) != 0]
    c_times = b_times[changed]
    last_change = last_index(c_times, at)
    out["since_binance_move"] = -(at - c_times[np.maximum(last_change, 0)])
    if others:
        moves = np.vstack([move_against(lm, tm, at, 0.5, buy) for tm, lm in others])
        out["market_ret_500ms"] = moves.mean(axis=0)
    else:
        out["market_ret_500ms"] = 0.0
    r2 = np.r_[0.0, np.diff(b_logmid) ** 2] * 1e8
    cum = np.cumsum(r2)
    hi, lo = last_index(b_times, at), last_index(b_times, at - 10.0)
    out["binance_vol_10s"] = np.sqrt(np.where(hi >= 0, cum[np.maximum(hi, 0)], 0) - np.where(lo >= 0, cum[np.maximum(lo, 0)], 0))
    ours, far = np.where(buy, bid_v[k], ask_v[k]), np.where(buy, ask_v[k], bid_v[k])
    out["grvt_imbalance"] = (far - ours) / np.maximum(far + ours, 1e-12)
    t_times = trades.received_ts.to_numpy(float)
    usd = (trades.price * trades.qty).to_numpy(float)
    hits_bid = trades.maker_is_buyer.to_numpy(bool)  # taker sold into bids
    cum_bid, cum_all = np.cumsum(np.where(hits_bid, usd, 0.0)), np.cumsum(usd)
    hi, lo = last_index(t_times, at), last_index(t_times, at - 5.0)

    def window(cum):
        return np.where(hi >= 0, cum[np.maximum(hi, 0)], 0) - np.where(lo >= 0, cum[np.maximum(lo, 0)], 0)
    on_bids, total = window(cum_bid), window(cum_all)
    out["grvt_flow_5s"] = np.where(total > 0, np.where(buy, on_bids, total - on_bids) / np.maximum(total, 1e-12), 0.5)
    out["grvt_spread_ticks"] = np.round((ask[k] - bid[k]) / tick)
    out.loc[~ok, :] = np.nan
    return out


def collect(args) -> pd.DataFrame:
    start, end = (pd.Timestamp(x, tz="UTC").timestamp() for x in (args.start, args.end))
    samples = pd.read_csv(args.latency_samples)
    one_way = samples.loc[samples.probe == "grvt_rest_rtt", "ms"].to_numpy(float) / 2
    depth_cols = ["received_ts", "exchange_ts"] + [
        c for i in range(1, rq.LEVELS + 1) for c in (f"bid{i}_p", f"bid{i}_v", f"ask{i}_p", f"ask{i}_v")]
    trade_cols = ["received_ts", "exchange_ts", "trade_id", "price", "qty", "maker_is_buyer"]
    every = sorted({Path(p).name.split("_")[0] for p in glob.glob(f"{args.binance_dir}/*_ticker_*_v2.h5")})
    frames = []
    for symbol in args.symbols.split(","):
        replay_args = argparse.Namespace(
            notional=args.notional, anchor="binance", half_spread_bps=args.half_spread_bps, improve=True,
            basis_half_life=args.basis_half_life, keep_ticks=1.0, min_rest_sec=1.0, latency_ms=8.0,
            one_way_samples=one_way, seed=7, cross="skip", skew_bps_per_lot=1.0, max_inventory_lots=5.0,
            fair_shrink=1.0, initial_position=0.0, tick=None)
        others = [binance_series(args.binance_dir, s, start - 60, end) for s in every if s != symbol]
        for window_start, window_end in rq.windows(start, end, args.slice_hours):
            depth = cv.load_columns(args.grvt_dir, symbol, "depth", depth_cols, window_start - 120, window_end)
            trades = cv.load_columns(args.grvt_dir, symbol, "trade", trade_cols, window_start - 60, window_end)
            b_times, b_logmid = binance_series(args.binance_dir, symbol, window_start - 120, window_end)
            in_window = lambda f: f[f.received_ts >= window_start].reset_index(drop=True)  # noqa: E731
            binance = cv.load_columns(args.binance_dir, symbol, "ticker", ["received_ts", "bid_p", "ask_p"],
                                      window_start, window_end)
            if replay_args.tick is None:
                prices = np.unique(np.r_[depth.bid1_p.to_numpy(), depth.ask1_p.to_numpy()])
                diffs = np.diff(prices[prices > 0])
                replay_args.tick = float(np.round(diffs[diffs > 0].min(), 10))
            result = cv.replay(symbol, in_window(depth), in_window(trades), binance, replay_args)
            replay_args.initial_position = result["final_position"]
            fills = pd.DataFrame([f for f in result["fills"] if "m5" in f])
            if fills.empty:
                continue
            factors = fill_factors(fills, depth, trades, b_times, b_logmid, others, react_sec=args.react_ms / 1000,
                                   basis_half_life=args.basis_half_life, tick=replay_args.tick)
            frames.append(pd.concat([fills.assign(symbol=symbol), factors], axis=1))
        print(f"{symbol}: {sum(len(f) for f in frames if f.symbol.iloc[0] == symbol)} fills", flush=True)
    return pd.concat(frames, ignore_index=True)


def spearman(x: pd.Series, y: pd.Series) -> float:
    keep = x.notna() & y.notna()
    if keep.sum() < 10 or x[keep].nunique() < 2:
        return float("nan")
    return float(x[keep].rank().corr(y[keep].rank()))


def quintile_table(frame: pd.DataFrame, column: str, train: pd.Series, target: str) -> list[float]:
    """Mean test target by quintile, edges from each symbol's train half."""
    means = []
    labels = pd.Series(np.nan, index=frame.index)
    for _, group in frame.groupby("symbol"):
        tr = group.loc[train[group.index], column].dropna()
        if len(tr) < 20 or tr.nunique() < 2:
            continue
        edges = np.unique(np.nanquantile(tr, [0.2, 0.4, 0.6, 0.8]))
        labels[group.index] = np.searchsorted(edges, group[column].to_numpy(), side="right")
    test = frame[~train & labels.notna()]
    for q in range(5):
        values = test.loc[labels[test.index] == q, target]
        means.append(float(values.mean()) if len(values) else float("nan"))
    return means


def evaluate(frame: pd.DataFrame, split: float, target: str = "m5") -> None:
    train = frame.t < split
    print(f"\nfills: train {int(train.sum())}, test {int((~train).sum())}; mean {target} train "
          f"{frame.loc[train, target].mean():+.3f} test {frame.loc[~train, target].mean():+.3f} bps")
    print(f"{'factor':<24}{'rho train':>10}{'rho test':>10}   test {target} by factor quintile (Q1 safe .. Q5 toxic)")
    for column in FACTORS:
        q = quintile_table(frame, column, train, target)
        print(f"{column:<24}{spearman(frame.loc[train, column], frame.loc[train, target]):>+10.3f}"
              f"{spearman(frame.loc[~train, column], frame.loc[~train, target]):>+10.3f}   "
              + "  ".join(f"{v:+6.2f}" for v in q))
    # Linear combination, standardized per symbol on train.
    z = pd.DataFrame(index=frame.index)
    for column in FACTORS:
        z[column] = 0.0
        for _, group in frame.groupby("symbol"):
            tr = group.loc[train[group.index], column]
            mean, std = tr.mean(), tr.std()
            z.loc[group.index, column] = ((group[column] - mean) / std).clip(-4, 4) if std and std > 0 else 0.0
    z = z.fillna(0.0)
    x = np.c_[np.ones(len(z)), z.to_numpy()]
    y = frame[target].to_numpy(float)
    beta = np.linalg.lstsq(x[train.to_numpy()], y[train.to_numpy()], rcond=None)[0]
    frame["predicted"] = x @ beta
    print("\nlinear model weights (bps per std):", ", ".join(f"{c} {b:+.2f}" for c, b in zip(FACTORS, beta[1:])))
    test = frame[~train]
    print(f"predicted vs actual on test: rho {spearman(test.predicted, test[target]):+.3f}")
    q = quintile_table(frame.assign(neg=-frame.predicted), "neg", train, target)
    print("test markout by predicted quintile (Q1 best .. Q5 worst): " + "  ".join(f"{v:+6.2f}" for v in q))
    for share in (0.1, 0.2, 0.3):
        cut = np.nanquantile(frame.loc[train, "predicted"], share)
        drop = test.predicted < cut
        pnl = (test[target] * test.qty * test.price / 1e4)
        print(f"drop predicted-worst {share:.0%} (train cut {cut:+.2f}): test fills {int((~drop).sum())}/{len(test)}, "
              f"mean {test.loc[~drop, target].mean():+.3f} (dropped {test.loc[drop, target].mean():+.3f}), "
              f"P&L {pnl[~drop].sum():+.2f} vs {pnl.sum():+.2f} USDT")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--grvt-dir", required=True)
    parser.add_argument("--binance-dir", required=True)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--split", required=True, help="Fills before this train, later ones test (UTC)")
    parser.add_argument("--latency-samples", required=True)
    parser.add_argument("--react-ms", type=float, default=10.0)
    parser.add_argument("--notional", type=float, default=100.0)
    parser.add_argument("--half-spread-bps", type=float, default=0.25)
    parser.add_argument("--basis-half-life", type=float, default=60.0)
    parser.add_argument("--slice-hours", type=float, default=6.0)
    parser.add_argument("--save", default="", help="Write fills with factors to this CSV")
    args = parser.parse_args(argv)
    frame = collect(args)
    if args.save:
        frame.to_csv(args.save, index=False)
    evaluate(frame, pd.Timestamp(args.split, tz="UTC").timestamp())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
