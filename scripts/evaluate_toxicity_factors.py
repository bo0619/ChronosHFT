"""Test whether single-venue high-frequency factors flag toxic maker fills.

A maker at the back of the queue loses on fills that are followed by an
adverse move (KORUUSDT: ~63% of fills at about -1.7 bps against ~+1.1 bps
on the rest). If a factor flags a toxic fill early enough to cancel first,
skipping those fills turns the per-fill edge positive; roughly, it must
catch ~70% of toxic fills while blocking no more than ~10% of good ones.

This replays the recording with ``replay_queue_quotes.py`` (exchange
clock, measured or fixed latency) and, for every fill, computes factors
from what we had *received* by the last moment a cancel could still have
landed first: the fill's exchange time minus the one-way order latency.
Factors are signed so that positive means adverse for the filled side:

* ``imb_l1``, ``imb_l5``: best-level and five-level size imbalance;
* ``micro``: microprice offset from the mid, bps;
* ``ofi_100ms``, ``ofi_1s``: best-level order-flow imbalance (Cont et al.)
  over the window, in touch sizes;
* ``flow_1s``, ``flow_5s``: signed taker notional received in the window,
  in touch notionals (aggTrade is often published ~150 ms late, so this
  sees only what had arrived);
* ``ret_1s``, ``ret_5s``: mid return, bps;
* unsigned state: ``vol_5s`` (sum of absolute mid changes, bps),
  ``own_size`` and ``opp_size`` (log touch notional on our and the other
  side), ``own_depletion_1s`` (log change of our side's touch size at an
  unchanged price), ``since_move`` (log seconds since the mid last moved),
  ``quote_age`` (log seconds our order had rested).

The label is toxic when the 5 s markout is negative. A logistic regression
is fitted on the first half of the window and scored on the second half;
the report gives each factor's out-of-sample AUC, the model's, and the
trade-off of skipping the riskiest fills: toxic fills avoided, good fills
blocked, and the mean 5 s markout of the fills kept. Skipping a fill
ignores the fills a cancelled quote might have earned later, so the kept
markout is an upper bound.

Example::

    python scripts/evaluate_toxicity_factors.py --dir storage/research/binance --symbols KORUUSDT \\
        --start 2026-10-14T16:00 --end 2026-10-15T10:00 --slice-hours 3 \\
        --latency-samples storage/research/latency/latency_20261014_155847.csv.gz --extra-latency-ms 1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import replay_queue_quotes as rq  # noqa: E402

FACTORS = ("imb_l1", "imb_l5", "micro", "ofi_100ms", "ofi_1s", "flow_1s", "flow_5s", "ret_1s", "ret_5s",
           "vol_5s", "own_size", "opp_size", "own_depletion_1s", "since_move", "quote_age")
SKIP_SHARES = (0.05, 0.10, 0.20, 0.30, 0.40, 0.50)


def auc(score: np.ndarray, label: np.ndarray) -> float:
    """Probability that a toxic fill scores above a good one (ties count half)."""
    order = np.argsort(score, kind="mergesort")
    ranks = np.empty(len(score))
    ranks[order] = np.arange(1, len(score) + 1)
    # average ranks over ties
    sorted_scores = score[order]
    start = 0
    for end in range(1, len(score) + 1):
        if end == len(score) or sorted_scores[end] != sorted_scores[start]:
            ranks[order[start:end]] = (start + 1 + end) / 2.0
            start = end
    positives = label.astype(bool)
    n_pos, n_neg = positives.sum(), (~positives).sum()
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    return float((ranks[positives].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def fit_logistic(x: np.ndarray, y: np.ndarray, l2: float = 1.0, iterations: int = 50) -> np.ndarray:
    """Newton (IRLS) logistic regression with an L2 penalty; intercept first."""
    design = np.c_[np.ones(len(x)), x]
    beta = np.zeros(design.shape[1])
    penalty = np.eye(design.shape[1]) * l2
    penalty[0, 0] = 0.0
    for _ in range(iterations):
        p = 1.0 / (1.0 + np.exp(-np.clip(design @ beta, -30, 30)))
        gradient = design.T @ (y - p) - penalty @ beta
        hessian = (design * (p * (1 - p))[:, None]).T @ design + penalty
        step = np.linalg.solve(hessian, gradient)
        beta += step
        if np.max(np.abs(step)) < 1e-8:
            break
    return beta


def window_sum(cumulative: np.ndarray, times: np.ndarray, end: np.ndarray, width: float) -> np.ndarray:
    hi = np.searchsorted(times, end, side="right") - 1
    lo = np.searchsorted(times, end - width, side="right") - 1
    upper = np.where(hi >= 0, cumulative[np.maximum(hi, 0)], 0.0)
    lower = np.where(lo >= 0, cumulative[np.maximum(lo, 0)], 0.0)
    return upper - lower


def factors(fills: pd.DataFrame, depth: pd.DataFrame, trades: pd.DataFrame, ticker: pd.DataFrame,
            cutoff: np.ndarray) -> pd.DataFrame:
    """Factors from data received by ``cutoff`` (one per fill)."""
    side = np.where(fills.side.to_numpy() == "BUY", 1.0, -1.0)
    tk = ticker.sort_values("received_ts")
    rx = tk.received_ts.to_numpy(float)
    bp, bv, ap, av = (tk[c].to_numpy(float) for c in ("bid_p", "bid_v", "ask_p", "ask_v"))
    mid = (bp + ap) / 2
    i = np.clip(np.searchsorted(rx, cutoff, side="right") - 1, 0, len(rx) - 1)
    out = pd.DataFrame(index=fills.index)
    out["imb_l1"] = -side * (bv[i] - av[i]) / (bv[i] + av[i])
    micro = (bp[i] * av[i] + ap[i] * bv[i]) / (bv[i] + av[i])
    out["micro"] = -side * (micro / mid[i] - 1) * 1e4

    e = np.zeros(len(rx))
    e[1:] = (np.where(bp[1:] >= bp[:-1], bv[1:], 0) - np.where(bp[1:] <= bp[:-1], bv[:-1], 0)
             - np.where(ap[1:] <= ap[:-1], av[1:], 0) + np.where(ap[1:] >= ap[:-1], av[:-1], 0))
    ofi = np.cumsum(e)
    touch_size = np.maximum((bv[i] + av[i]) / 2, 1e-12)
    for name, width in (("ofi_100ms", 0.1), ("ofi_1s", 1.0)):
        out[name] = -side * window_sum(ofi, rx, cutoff, width) / touch_size

    tr = trades.sort_values("received_ts")
    trx = tr.received_ts.to_numpy(float)
    signed = np.where(tr.maker_is_buyer.to_numpy(bool), -1.0, 1.0) * (tr.price * tr.qty).to_numpy(float)
    flow = np.cumsum(signed)
    touch_notional = np.maximum(touch_size * mid[i], 1e-9)
    for name, width in (("flow_1s", 1.0), ("flow_5s", 5.0)):
        out[name] = -side * window_sum(flow, trx, cutoff, width) / touch_notional

    log_mid = np.log(mid)
    for name, width in (("ret_1s", 1.0), ("ret_5s", 5.0)):
        j = np.clip(np.searchsorted(rx, cutoff - width, side="right") - 1, 0, len(rx) - 1)
        out[name] = -side * (log_mid[i] - log_mid[j]) * 1e4
    moves = np.r_[0.0, np.abs(np.diff(log_mid))] * 1e4
    out["vol_5s"] = window_sum(np.cumsum(moves), rx, cutoff, 5.0)
    own = np.where(side > 0, bv[i] * bp[i], av[i] * ap[i])
    opp = np.where(side > 0, av[i] * ap[i], bv[i] * bp[i])
    out["own_size"] = np.log1p(own)
    out["opp_size"] = np.log1p(opp)
    j = np.clip(np.searchsorted(rx, cutoff - 1.0, side="right") - 1, 0, len(rx) - 1)
    same_price = np.where(side > 0, bp[j] == bp[i], ap[j] == ap[i])
    own_before = np.where(side > 0, bv[j] * bp[j], av[j] * ap[j])
    out["own_depletion_1s"] = np.where(same_price, np.log1p(own) - np.log1p(own_before), 0.0)
    changed = np.r_[True, mid[1:] != mid[:-1]]
    last_change = np.maximum.accumulate(np.where(changed, np.arange(len(rx)), 0))
    out["since_move"] = np.log1p(np.maximum(cutoff - rx[last_change[i]], 0.0))
    out["quote_age"] = np.log1p(np.maximum(cutoff - fills.placed_t.to_numpy(float), 0.0))

    dp = depth.sort_values("received_ts")
    drx = dp.received_ts.to_numpy(float)
    k = np.clip(np.searchsorted(drx, cutoff, side="right") - 1, 0, len(drx) - 1)
    bid5 = sum(dp[f"bid{level}_v"].to_numpy(float) for level in range(1, 6))
    ask5 = sum(dp[f"ask{level}_v"].to_numpy(float) for level in range(1, 6))
    out["imb_l5"] = -side * (bid5[k] - ask5[k]) / np.maximum(bid5[k] + ask5[k], 1e-12)
    return out[list(FACTORS)]


def evaluate(symbol: str, frame: pd.DataFrame) -> None:
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna()
    label = (frame.m5 < 0).to_numpy(float)
    split = frame.t.quantile(0.5)
    train, test = frame.t <= split, frame.t > split
    x = frame[list(FACTORS)].to_numpy(float)
    lo, hi = np.nanpercentile(x[train], [1, 99], axis=0)
    x = np.clip(x, lo, hi)
    mean, std = x[train].mean(axis=0), x[train].std(axis=0) + 1e-12
    z = (x - mean) / std
    beta = fit_logistic(z[train], label[train])
    score = np.c_[np.ones(len(z)), z] @ beta
    y_test, s_test, m5_test = label[test], score[test], frame.m5.to_numpy(float)[test]
    print(f"\n== {symbol}: {len(frame):,} fills, {label.mean():.0%} toxic (5 s markout < 0); "
          f"train {int(train.sum()):,} / test {int(test.sum()):,}")
    print("   out-of-sample AUC per factor (0.5 = no information; signed so higher = more adverse):")
    cells = [f"{name} {auc(z[test][:, j], y_test):.3f}" for j, name in enumerate(FACTORS)]
    for row in range(0, len(cells), 5):
        print("     " + "   ".join(cells[row:row + 5]))
    print(f"   model AUC: train {auc(score[train], label[train]):.3f}, test {auc(s_test, y_test):.3f}")
    print(f"   all test fills: mean 5 s markout {m5_test.mean():+.2f} bps")
    print(f"   {'skip riskiest':>14} {'toxic avoided':>14} {'good blocked':>13} {'kept mean m5':>13}")
    order = np.argsort(-s_test)
    for share in SKIP_SHARES:
        skipped = np.zeros(len(s_test), bool)
        skipped[order[: int(round(share * len(s_test)))]] = True
        toxic = y_test.astype(bool)
        print(f"   {share:>14.0%} {np.mean(skipped[toxic]):>14.0%} {np.mean(skipped[~toxic]):>13.0%} "
              f"{m5_test[~skipped].mean():>+13.2f}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dir", required=True)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--notional", type=float, default=100.0)
    parser.add_argument("--latency-ms", type=float, default=5.0)
    parser.add_argument("--latency-samples", default="")
    parser.add_argument("--latency-probe", default="binance_ws_api_rtt")
    parser.add_argument("--extra-latency-ms", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--slice-hours", type=float, default=0.0,
                        help="Replay and score the window in slices of this many hours to bound memory")
    args = parser.parse_args(argv)
    start = pd.Timestamp(args.start, tz="UTC").timestamp() if args.start else None
    end = pd.Timestamp(args.end, tz="UTC").timestamp() if args.end else None
    one_way = None
    if args.latency_samples:
        samples = pd.read_csv(args.latency_samples)
        one_way = samples.loc[samples.probe == args.latency_probe, "ms"].to_numpy(float) / 2 + args.extra_latency_ms
    cutoff_latency = (np.median(one_way) if one_way is not None else args.latency_ms) / 1000.0
    for symbol in args.symbols.split(","):
        frames, tick = [], None
        for window_start, window_end in rq.windows(start, end, args.slice_hours):
            depth, trades, ticker = rq.load(args.dir, symbol, window_start, window_end)
            if depth.empty or trades.empty or ticker.empty:
                continue
            if tick is None:
                prices = np.unique(np.r_[depth.bid1_p.to_numpy(), depth.ask1_p.to_numpy()])
                diffs = np.diff(prices[prices > 0])
                tick = float(np.round(diffs[diffs > 0].min(), 10))
            replay_args = argparse.Namespace(
                notional=args.notional, inside_ticks=0, keep_ticks=2.0, min_rest_sec=5.0, react="depth",
                latency_ms=args.latency_ms, thin_notional=0.0, cancel_ahead_model="proportional",
                tick=tick, event_clock="exchange", seed=args.seed, one_way_samples=one_way,
            )
            result = rq.replay(symbol, depth, trades, ticker, replay_args)
            fills = pd.DataFrame([f for f in result["fills"] if f["trigger"]])
            if fills.empty:
                continue
            fills = fills.sort_values("t").reset_index(drop=True)
            mid_times, mids = result["mid_times"], result["mids"]
            sign = np.where(fills.side == "BUY", 1.0, -1.0)
            j = np.clip(np.searchsorted(mid_times, fills.t + 5.0), 0, len(mids) - 1)
            fills["m5"] = sign * np.log(mids[j] / fills.price.to_numpy(float)) * 1e4
            features = factors(fills, depth, trades, ticker, fills.t.to_numpy(float) - cutoff_latency)
            frames.append(pd.concat([fills, features], axis=1))
            del depth, trades, ticker, result
        if not frames:
            print(f"{symbol}: needs depth, trade and ticker recordings in {args.dir}")
            continue
        evaluate(symbol, pd.concat(frames, ignore_index=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
