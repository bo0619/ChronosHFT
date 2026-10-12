"""Offline comparison of fair value estimators on recorded depth/trade data.

Reads the HDF5 files written by ``data/recorder.py``
(``{symbol}_depth_{YYYYMMDD}.h5`` / ``{symbol}_trade_{YYYYMMDD}.h5``, and the
``*_v2.h5`` files that also carry exchange/arrival timestamps) and compares
candidate fair values on three axes:

* prediction error of the future mid at each clock horizon (RMSE, skill vs.
  mid, correlation and direction hit rate of the predicted move),
* event-time direction: the sign of the next mid change and the mid after N
  L1 book changes, which is what a tick-level quoter actually has to call, and
* markout of a naive symmetric quoter centred on each fair value, filled
  against the recorded aggressive trades.

Clocks: legacy files stamp depth with local receive time and trades with
exchange time, so ``--depth-lag-ms auto`` shifts the book onto the trade
clock. v2 files carry ``corrected_received_ts`` (arrival on the exchange
clock) and ``exchange_ts``; with ``--clock auto`` and complete v2 data the
book is placed at its arrival time, i.e. when the strategy could first have
seen it, and trades at exchange time, i.e. when they hit a resting quote. No
lag estimate is needed then and the measured feed latency is reported.

Fitted estimators are trained on the first ``--train-fraction`` of every
symbol's grid and every metric is reported on the remaining test period only.
Nothing here touches the live strategy.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_ROOT / "storage"
LEVELS = 5
BPS = 1e4
FILE_PATTERN = re.compile(
    r"^(?P<symbol>[A-Z0-9]+)_(?P<kind>depth|trade)_(?P<day>\d{8})(?:_v2)?\.h5$"
)

RAW_ESTIMATORS = ("mid", "microprice_l1", "microprice_multi")
FITTED_ESTIMATORS = (
    "microprice_l1_fitted",
    "imbalance_table",
    "ofi_adjusted",
    "linear_combo",
)
ESTIMATORS = RAW_ESTIMATORS + FITTED_ESTIMATORS


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Compare fair value estimators on recorded order book data"
    )
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument(
        "--symbols", default="", help="Comma separated symbols (default: all found)"
    )
    parser.add_argument(
        "--days", default="", help="Comma separated YYYYMMDD days (default: all)"
    )
    parser.add_argument("--horizons", default="1,5,30", help="Seconds")
    parser.add_argument("--grid-ms", type=int, default=100)
    parser.add_argument("--train-fraction", type=float, default=0.6)
    parser.add_argument(
        "--gap-sec",
        type=float,
        default=30.0,
        help="A silence longer than this splits the recording into segments",
    )
    parser.add_argument("--ofi-window-sec", type=float, default=1.0)
    parser.add_argument(
        "--level-decay",
        type=float,
        default=0.5,
        help="Weight exp(-decay*(level-1)) for the multi-level microprice",
    )
    parser.add_argument("--ridge", type=float, default=1e-3)
    parser.add_argument(
        "--quote-half-spread-bps",
        default="0,2.5",
        help=(
            "Comma separated half spreads of the simulated quoter around each "
            "fair value; 0 quotes the ticks just around it"
        ),
    )
    parser.add_argument(
        "--quote-horizon",
        type=float,
        default=5.0,
        help="Horizon whose fitted model centres the simulated quotes",
    )
    parser.add_argument("--latency-ms", type=float, default=50.0)
    parser.add_argument(
        "--depth-lag-ms",
        default="auto",
        help=(
            "How far depth timestamps (local receive time) trail trade timestamps "
            "(exchange time); 'auto' estimates it per symbol from trade flow"
        ),
    )
    parser.add_argument(
        "--clock",
        choices=("auto", "arrival", "recorded"),
        default="auto",
        help=(
            "arrival: book at corrected_received_ts, trades at exchange_ts "
            "(v2 files only); recorded: the datetime column plus --depth-lag-ms; "
            "auto: arrival when every row carries v2 timestamps"
        ),
    )
    parser.add_argument(
        "--event-horizons",
        default="1,5,20",
        help="L1 book changes ahead for the event-time labels",
    )
    parser.add_argument(
        "--event-ofi-updates",
        type=int,
        default=10,
        help="OFI window of the event-time models, in L1 book changes",
    )
    parser.add_argument(
        "--event-deadband-ticks",
        type=float,
        default=0.25,
        help="Leans smaller than this call 'flat' in the three-way accuracy",
    )
    parser.add_argument(
        "--fill-rule",
        choices=("touch", "through"),
        default="touch",
        help="touch: trade at our price fills us; through: needs a better price",
    )
    parser.add_argument("--min-test-points", type=int, default=2000)
    parser.add_argument("--output", default="", help="Markdown report path")
    parser.add_argument("--csv", default="", help="Optional metrics CSV path")
    return parser.parse_args(argv)


# --------------------------------------------------------------------------
# Loading


def discover_files(data_dir: Path, symbols, days):
    found: dict[str, dict[str, list[Path]]] = {}
    for path in sorted(data_dir.glob("*.h5")):
        match = FILE_PATTERN.match(path.name)
        if not match:
            continue
        symbol = match["symbol"]
        if symbols and symbol not in symbols:
            continue
        if days and match["day"] not in days:
            continue
        found.setdefault(symbol, {"depth": [], "trade": []})[match["kind"]].append(path)
    return found


def _read_frames(paths, key):
    frames = [pd.read_hdf(path, key=key) for path in paths]
    frames = [frame for frame in frames if len(frame)]
    if not frames:
        return pd.DataFrame()
    frame = pd.concat(frames, ignore_index=True)
    frame["ts"] = pd.to_datetime(frame["datetime"], utc=True)
    return frame.sort_values("ts", kind="stable").reset_index(drop=True)


def load_depth(paths) -> pd.DataFrame:
    return clean_depth(_read_frames(paths, "depth"))


def clean_depth(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    valid = (
        (frame["bid1_p"] > 0)
        & (frame["ask1_p"] > frame["bid1_p"])
        & (frame["bid1_v"] > 0)
        & (frame["ask1_v"] > 0)
    )
    frame = frame.loc[valid].drop_duplicates("ts", keep="last")
    return frame.reset_index(drop=True)


def load_trades(paths) -> pd.DataFrame:
    frame = _read_frames(paths, "trade")
    if frame.empty:
        return frame
    frame = frame.loc[(frame["price"] > 0) & (frame["qty"] > 0)].copy()
    # maker_is_buyer means the aggressor sold into the bid.
    frame["aggressor"] = np.where(frame["maker_is_buyer"].astype(bool), -1, 1)
    return frame.reset_index(drop=True)


def _has_clock(frame: pd.DataFrame, column: str) -> bool:
    return column in frame and bool((frame[column].fillna(0.0) > 0).all())


def choose_clock(depth: pd.DataFrame, trades: pd.DataFrame, mode: str) -> str:
    """'arrival' only when every depth and trade row carries v2 timestamps."""
    if mode == "recorded":
        return "recorded"
    available = _has_clock(depth, "corrected_received_ts") and (
        trades.empty or _has_clock(trades, "exchange_ts")
    )
    if mode == "arrival" and not available:
        raise ValueError("--clock arrival needs v2 timestamps on every row")
    return "arrival" if available else "recorded"


def retime(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    """Re-key rows on an epoch-seconds column and restore time order."""
    if frame.empty:
        return frame
    frame = frame.copy()
    frame["ts"] = pd.to_datetime(frame[column].to_numpy(float), unit="s", utc=True)
    return frame.sort_values("ts", kind="stable").reset_index(drop=True)


def feed_latency_ms(frame: pd.DataFrame) -> dict:
    """Arrival minus venue time, from the v2 columns where both are set."""
    if not {"exchange_ts", "corrected_received_ts"} <= set(frame.columns):
        return {}
    ok = (frame["exchange_ts"] > 0) & (frame["corrected_received_ts"] > 0)
    if not ok.any():
        return {}
    latency = (
        frame.loc[ok, "corrected_received_ts"] - frame.loc[ok, "exchange_ts"]
    ).to_numpy(float) * 1e3
    return {"p50": float(np.median(latency)), "p90": float(np.quantile(latency, 0.9))}


# --------------------------------------------------------------------------
# Book features


def infer_tick(depth: pd.DataFrame) -> float:
    prices = np.unique(
        np.concatenate([depth["bid1_p"].to_numpy(), depth["ask1_p"].to_numpy()])
    )
    steps = np.diff(prices)
    steps = steps[steps > 0]
    if steps.size == 0:
        return float("nan")
    tick = float(np.min(steps))
    digits = max(0, 10 - math.floor(math.log10(tick)))
    return round(tick, digits)


def book_features(depth: pd.DataFrame, level_decay: float) -> pd.DataFrame:
    bid = depth["bid1_p"].to_numpy(float)
    ask = depth["ask1_p"].to_numpy(float)
    bid_v = depth["bid1_v"].to_numpy(float)
    ask_v = depth["ask1_v"].to_numpy(float)
    mid = 0.5 * (bid + ask)
    spread = ask - bid

    imbalance_l1 = bid_v / (bid_v + ask_v)
    weighted_bid = np.zeros_like(mid)
    weighted_total = np.zeros_like(mid)
    for level in range(1, LEVELS + 1):
        weight = math.exp(-level_decay * (level - 1))
        level_bid = depth[f"bid{level}_v"].to_numpy(float)
        level_ask = depth[f"ask{level}_v"].to_numpy(float)
        weighted_bid += weight * level_bid
        weighted_total += weight * (level_bid + level_ask)
    imbalance_multi = weighted_bid / weighted_total

    # Cont-Kukanov-Stoikov L1 order flow imbalance per book update.
    prev_bid = np.r_[bid[0], bid[:-1]]
    prev_ask = np.r_[ask[0], ask[:-1]]
    prev_bid_v = np.r_[bid_v[0], bid_v[:-1]]
    prev_ask_v = np.r_[ask_v[0], ask_v[:-1]]
    ofi = (
        np.where(bid >= prev_bid, bid_v, 0.0)
        - np.where(bid <= prev_bid, prev_bid_v, 0.0)
        - np.where(ask <= prev_ask, ask_v, 0.0)
        + np.where(ask >= prev_ask, prev_ask_v, 0.0)
    )
    ofi[0] = 0.0

    return pd.DataFrame(
        {
            "ts_ns": depth["ts"].to_numpy("datetime64[ns]").astype(np.int64),
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread": spread,
            "imbalance_l1": imbalance_l1,
            "imbalance_multi": imbalance_multi,
            "l1_depth": 0.5 * (bid_v + ask_v),
            "cum_ofi": np.cumsum(ofi),
        }
    )


def segment_ids(ts_ns: np.ndarray, gap_sec: float) -> np.ndarray:
    gaps = np.diff(ts_ns) > gap_sec * 1e9
    return np.r_[0, np.cumsum(gaps)]


@dataclass
class Grid:
    ts_ns: np.ndarray
    book_index: np.ndarray
    valid: np.ndarray
    frame: pd.DataFrame


def build_grid(book: pd.DataFrame, grid_ms: int, gap_sec: float) -> Grid:
    ts = book["ts_ns"].to_numpy()
    step = int(grid_ms * 1e6)
    start = (ts[0] // step + 1) * step
    grid_ts = np.arange(start, ts[-1] + 1, step, dtype=np.int64)
    index = np.searchsorted(ts, grid_ts, side="right") - 1
    staleness = grid_ts - ts[index]
    segments = segment_ids(ts, gap_sec)
    # A grid point after a long silence belongs to no segment.
    valid = staleness <= gap_sec * 1e9
    frame = book.iloc[index].reset_index(drop=True)
    frame["segment"] = np.where(valid, segments[index], -1)
    return Grid(grid_ts, index, valid, frame)


# --------------------------------------------------------------------------
# Estimators


def _ridge(features: np.ndarray, target: np.ndarray, ridge: float) -> np.ndarray:
    scale = np.sqrt(np.mean(features * features, axis=0)) + 1e-12
    scaled = features / scale
    gram = scaled.T @ scaled + ridge * len(target) * np.eye(features.shape[1])
    return np.linalg.solve(gram, scaled.T @ target) / scale


def imbalance_buckets(frame: pd.DataFrame, tick: float):
    imbalance_bin = np.clip((frame["imbalance_l1"].to_numpy() * 10).astype(int), 0, 9)
    spread_ticks = np.rint(frame["spread"].to_numpy() / tick).astype(int)
    spread_bin = np.clip(spread_ticks, 1, 3) - 1
    return imbalance_bin * 3 + spread_bin


class FairValueModels:
    """Raw estimators plus models fitted per horizon on the train split."""

    def __init__(self, frame: pd.DataFrame, tick: float, ofi_window_steps: int):
        self.frame = frame
        self.tick = tick
        mid = frame["mid"].to_numpy()
        spread = frame["spread"].to_numpy()
        self.mid = mid
        self.x_mp1 = (frame["imbalance_l1"].to_numpy() - 0.5) * spread / mid * BPS
        self.x_mpn = (frame["imbalance_multi"].to_numpy() - 0.5) * spread / mid * BPS
        cum_ofi = frame["cum_ofi"].to_numpy()
        lagged = np.r_[np.full(ofi_window_steps, np.nan), cum_ofi[:-ofi_window_steps]]
        segment = frame["segment"].to_numpy()
        lagged_segment = np.r_[
            np.full(ofi_window_steps, -2), segment[:-ofi_window_steps]
        ]
        ofi = np.where(lagged_segment == segment, cum_ofi - lagged, 0.0)
        self.ofi_raw = np.nan_to_num(ofi)
        self.buckets = imbalance_buckets(frame, tick)
        self.params: dict[float, dict] = {}

    def fit(
        self, horizon: float, target_bps: np.ndarray, train_mask: np.ndarray, ridge
    ):
        depth_scale = float(np.median(self.frame["l1_depth"].to_numpy()[train_mask]))
        ofi = self.ofi_raw / max(depth_scale, 1e-12)
        y = target_bps[train_mask]
        params = {"ofi_scale": depth_scale}
        x_mp1 = self.x_mp1[train_mask]
        params["mp1_beta"] = float(_ridge(x_mp1[:, None], y, ridge)[0])
        params["ofi_beta"] = float(
            _ridge(ofi[train_mask][:, None], y - x_mp1, ridge)[0]
        )
        combo = np.column_stack([x_mp1, self.x_mpn[train_mask], ofi[train_mask]])
        params["combo_beta"] = _ridge(combo, y, ridge)
        table = np.zeros(30)
        buckets = self.buckets[train_mask]
        for bucket in range(30):
            selected = buckets == bucket
            if selected.sum() >= 50:
                table[bucket] = float(np.mean(y[selected]))
        params["table"] = table
        self.params[horizon] = params

    def predict_bps(self, name: str, horizon: float) -> np.ndarray:
        """Predicted fair value as an offset from mid in bps."""
        if name == "mid":
            return np.zeros_like(self.mid)
        if name == "microprice_l1":
            return self.x_mp1
        if name == "microprice_multi":
            return self.x_mpn
        params = self.params[horizon]
        ofi = self.ofi_raw / max(params["ofi_scale"], 1e-12)
        if name == "microprice_l1_fitted":
            return params["mp1_beta"] * self.x_mp1
        if name == "imbalance_table":
            return params["table"][self.buckets]
        if name == "ofi_adjusted":
            return self.x_mp1 + params["ofi_beta"] * ofi
        if name == "linear_combo":
            combo = np.column_stack([self.x_mp1, self.x_mpn, ofi])
            return combo @ params["combo_beta"]
        raise KeyError(name)


# --------------------------------------------------------------------------
# Evaluation


def future_mid_bps(grid: Grid, steps: int):
    mid = grid.frame["mid"].to_numpy()
    segment = grid.frame["segment"].to_numpy()
    future_mid = np.r_[mid[steps:], np.full(steps, np.nan)]
    future_segment = np.r_[segment[steps:], np.full(steps, -2)]
    valid = (segment >= 0) & (future_segment == segment)
    return (future_mid - mid) / mid * BPS, valid


def prediction_metrics(pred, target):
    error = target - pred
    mse = float(np.mean(error * error))
    moved = target != 0
    leaning = pred != 0
    both = moved & leaning
    hit = (
        float(np.mean(np.sign(pred[both]) == np.sign(target[both])))
        if both.any()
        else float("nan")
    )
    corr = (
        float(np.corrcoef(pred, target)[0, 1])
        if np.std(pred) > 0 and np.std(target) > 0
        else float("nan")
    )
    return {
        "rmse_bps": math.sqrt(mse),
        "mae_bps": float(np.mean(np.abs(error))),
        "mse": mse,
        "corr": corr,
        "hit_rate": hit,
        "mean_abs_lean_bps": float(np.mean(np.abs(pred))),
    }


def diebold_mariano_t(pred, target, steps):
    """t-stat of the squared-error gain over mid on non-overlapping points."""
    gain = target[::steps] ** 2 - (target[::steps] - pred[::steps]) ** 2
    if gain.size < 3 or np.std(gain) == 0:
        return float("nan")
    return float(np.mean(gain) / (np.std(gain, ddof=1) / math.sqrt(gain.size)))


def simulate_markouts(
    grid: Grid,
    book: pd.DataFrame,
    trades: pd.DataFrame,
    fair_bps: np.ndarray,
    test_start_ns: int,
    tick: float,
    args,
    horizons,
    half_spread_bps: float,
):
    """Fill a symmetric quote around the fair value against recorded trades."""
    if trades.empty:
        return None
    trade_ts = trades["ts"].to_numpy("datetime64[ns]").astype(np.int64)
    step = int(args.grid_ms * 1e6)
    quote_ts = trade_ts - int(args.latency_ms * 1e6)
    grid_index = (quote_ts - grid.ts_ns[0]) // step
    usable = (
        (trade_ts >= test_start_ns) & (grid_index >= 0) & (grid_index < len(grid.ts_ns))
    )
    grid_index = np.clip(grid_index, 0, len(grid.ts_ns) - 1)
    usable &= grid.frame["segment"].to_numpy()[grid_index] >= 0

    mid = grid.frame["mid"].to_numpy()[grid_index]
    best_bid = grid.frame["bid"].to_numpy()[grid_index]
    best_ask = grid.frame["ask"].to_numpy()[grid_index]
    fair = mid * (1 + fair_bps[grid_index] / BPS)
    half = fair * half_spread_bps / BPS
    our_bid = np.minimum(np.floor((fair - half) / tick + 1e-9) * tick, best_ask - tick)
    our_ask = np.maximum(np.ceil((fair + half) / tick - 1e-9) * tick, best_bid + tick)

    price = trades["price"].to_numpy()
    aggressor = trades["aggressor"].to_numpy()
    if args.fill_rule == "touch":
        buy_fill = (aggressor < 0) & (price <= our_bid + 1e-12)
        sell_fill = (aggressor > 0) & (price >= our_ask - 1e-12)
    else:
        buy_fill = (aggressor < 0) & (price < our_bid - 1e-12)
        sell_fill = (aggressor > 0) & (price > our_ask + 1e-12)
    side = np.where(buy_fill, 1, np.where(sell_fill, -1, 0))
    fill_price = np.where(side > 0, our_bid, our_ask)
    filled = usable & (side != 0)
    # One fill per quote refresh and side: a burst of prints is one fill.
    key = pd.Series(grid_index * 2 + (side > 0))
    filled &= ~key.duplicated().to_numpy() | ~filled
    first = pd.Series(np.where(filled, key, -1))
    filled &= ~first.duplicated().to_numpy() | (first.to_numpy() < 0)

    book_ts = book["ts_ns"].to_numpy()
    book_mid = book["mid"].to_numpy()
    book_segment = segment_ids(book_ts, args.gap_sec)
    now_index = np.searchsorted(book_ts, trade_ts, side="right") - 1
    result = {"half_spread_bps": half_spread_bps, "fills": int(filled.sum())}
    test_points = (grid.frame["segment"].to_numpy() >= 0) & (
        grid.ts_ns >= test_start_ns
    )
    hours = max(test_points.sum() * args.grid_ms / 3.6e6, 1e-9)
    result["fills_per_hour"] = result["fills"] / hours
    for horizon in horizons:
        later = (
            np.searchsorted(book_ts, trade_ts + int(horizon * 1e9), side="right") - 1
        )
        ok = (
            filled
            & (now_index >= 0)
            & (book_segment[later] == book_segment[np.maximum(now_index, 0)])
        )
        ok &= book_ts[-1] >= trade_ts + int(horizon * 1e9)
        markout = side * (book_mid[later] - fill_price) / fill_price * BPS
        values = markout[ok]
        result[f"markout_{horizon:g}s_bps"] = (
            float(np.mean(values)) if values.size else float("nan")
        )
        result[f"markout_{horizon:g}s_se"] = (
            float(np.std(values, ddof=1) / math.sqrt(values.size))
            if values.size > 1
            else float("nan")
        )
    edge = side * (mid - fill_price) / fill_price * BPS
    result["edge_vs_mid_bps"] = (
        float(np.mean(edge[filled])) if filled.any() else float("nan")
    )
    return result


def half_spreads(args):
    return [float(v) for v in str(args.quote_half_spread_bps).split(",") if v.strip()]


def estimate_depth_lag_ms(depth, trades, step_ms=50, max_lag_ms=3000):
    """Lag at which signed trade flow best lines up with later mid changes."""
    depth_ts = depth["ts"].to_numpy("datetime64[ns]").astype(np.int64)
    trade_ts = trades["ts"].to_numpy("datetime64[ns]").astype(np.int64)
    step = int(step_ms * 1e6)
    start = max(depth_ts[0], trade_ts[0])
    end = min(depth_ts[-1], trade_ts[-1])
    if end - start < 1000 * step:
        return 0.0
    bins = np.arange(start, end, step)
    index = np.searchsorted(depth_ts, bins, side="right") - 1
    mid = 0.5 * (depth["bid1_p"].to_numpy() + depth["ask1_p"].to_numpy())
    move = np.sign(np.diff(mid[index]))
    fresh = (bins - depth_ts[index]) <= 5e9
    fresh = fresh[1:] & fresh[:-1]
    flow = np.zeros(len(bins) - 1)
    trade_bin = (trade_ts - start) // step
    inside = (trade_bin >= 0) & (trade_bin < len(flow))
    signed = trades["aggressor"].to_numpy() * trades["qty"].to_numpy()
    np.add.at(flow, trade_bin[inside], signed[inside])
    best_lag, best_corr = 0, -np.inf
    for lag in range(max_lag_ms // step_ms + 1):
        size = len(flow) - lag
        ok = fresh[:size] & fresh[lag:]
        a, b = flow[:size][ok], move[lag:][ok]
        if a.std() == 0 or b.std() == 0:
            continue
        corr = float(np.corrcoef(a, b)[0, 1])
        if corr > best_corr:
            best_lag, best_corr = lag, corr
    return float(best_lag * step_ms)


def event_book(book: pd.DataFrame, gap_sec: float) -> pd.DataFrame:
    """Book rows where the L1 state changed: the event clock."""
    l1 = book[["bid", "ask", "imbalance_l1", "l1_depth"]].to_numpy()
    changed = np.r_[True, np.any(l1[1:] != l1[:-1], axis=1)]
    # cum_ofi stays valid: an unchanged L1 contributes zero OFI.
    frame = book.loc[changed].reset_index(drop=True)
    frame["segment"] = segment_ids(frame["ts_ns"].to_numpy(), gap_sec)
    return frame


def next_mid_change(frame: pd.DataFrame):
    """Move (bps) and wait (ms) to the next mid change within the segment."""
    mid = frame["mid"].to_numpy()
    ts = frame["ts_ns"].to_numpy()
    segment = frame["segment"].to_numpy()
    n = len(mid)
    changes = np.flatnonzero(np.r_[False, mid[1:] != mid[:-1]])
    if changes.size == 0:
        return np.zeros(n), np.zeros(n), np.zeros(n, dtype=bool)
    position = np.searchsorted(changes, np.arange(n), side="right")
    later = changes[np.minimum(position, changes.size - 1)]
    valid = (position < changes.size) & (segment[later] == segment)
    move = np.where(valid, (mid[later] - mid) / mid * BPS, 0.0)
    wait_ms = np.where(valid, (ts[later] - ts) / 1e6, np.nan)
    return move, wait_ms, valid


def mid_after_updates(frame: pd.DataFrame, updates: int):
    mid = frame["mid"].to_numpy()
    segment = frame["segment"].to_numpy()
    future = np.r_[mid[updates:], np.full(updates, np.nan)]
    future_segment = np.r_[segment[updates:], np.full(updates, -2)]
    valid = future_segment == segment
    return np.where(valid, (future - mid) / mid * BPS, 0.0), valid


def direction_metrics(pred, move_bps):
    """Next-change call: how often a lean is right and what it earns."""
    lean = np.sign(pred)
    label = np.sign(move_bps)
    active = lean != 0
    return {
        "coverage": float(np.mean(active)),
        "hit_rate": (
            float(np.mean(lean[active] == label[active]))
            if active.any()
            else float("nan")
        ),
        # Mean signed move per book state, zero where the estimator abstains.
        "edge_bps": float(np.mean(lean * move_bps)),
        "up_share": float(np.mean(label > 0)),
    }


def three_class_accuracy(pred, target, deadband_bps):
    called = np.where(np.abs(pred) >= deadband_bps, np.sign(pred), 0.0)
    return float(np.mean(called == np.sign(target)))


def evaluate_events(book, test_start_ns, tick, args):
    """Event-time labels on L1 book changes; fitted models use the same split."""
    frame = event_book(book, args.gap_sec)
    if len(frame) < 2 * args.min_test_points:
        return [], {}
    models = FairValueModels(frame, tick, max(1, args.event_ofi_updates))
    is_train = frame["ts_ns"].to_numpy() < test_start_ns
    mid = frame["mid"].to_numpy()
    deadband_bps = args.event_deadband_ticks * tick / mid * BPS
    rows = []

    move, wait_ms, valid = next_mid_change(frame)
    train, test = valid & is_train, valid & ~is_train
    info = {
        "event_updates": len(frame),
        "next_change_wait_ms_p50": (
            float(np.nanmedian(wait_ms[test])) if test.any() else float("nan")
        ),
    }
    if train.sum() >= args.min_test_points and test.sum() >= args.min_test_points:
        models.fit("next", move, train, args.ridge)
        for name in ESTIMATORS:
            pred = models.predict_bps(name, "next")
            rows.append(
                {
                    "estimator": name,
                    "label": "next_change",
                    **direction_metrics(pred[test], move[test]),
                    "test_points": int(test.sum()),
                }
            )

    for updates in event_horizons(args):
        target, valid = mid_after_updates(frame, updates)
        train, test = valid & is_train, valid & ~is_train
        if train.sum() < args.min_test_points or test.sum() < args.min_test_points:
            continue
        key = f"ev{updates}"
        models.fit(key, target, train, args.ridge)
        mid_mse = None
        for name in ESTIMATORS:
            pred = models.predict_bps(name, key)
            metrics = prediction_metrics(pred[test], target[test])
            if name == "mid":
                mid_mse = metrics["mse"]
            metrics["skill"] = (
                1.0 - metrics["mse"] / mid_mse if mid_mse > 0 else float("nan")
            )
            rows.append(
                {
                    "estimator": name,
                    "label": f"after_{updates}_updates",
                    **metrics,
                    "three_class_acc": three_class_accuracy(
                        pred[test], target[test], deadband_bps[test]
                    ),
                    "flat_share": float(np.mean(target[test] == 0)),
                    "test_points": int(test.sum()),
                }
            )
    return rows, info


def event_horizons(args):
    return [int(v) for v in str(args.event_horizons).split(",") if v.strip()]


def evaluate_symbol(symbol, files, args, horizons):
    depth = _read_frames(files["depth"], "depth")
    trades = load_trades(files["trade"]) if files["trade"] else pd.DataFrame()
    if len(depth) < 100:
        return None, f"{symbol}: too few depth rows ({len(depth)})"
    try:
        clock = choose_clock(depth, trades, args.clock)
    except ValueError as exc:
        return None, f"{symbol}: {exc}"
    latency = {"depth": feed_latency_ms(depth), "trade": feed_latency_ms(trades)}
    if clock == "arrival":
        # Book when we could first see it, trades when they hit the venue.
        depth = clean_depth(retime(depth, "corrected_received_ts"))
        trades = retime(trades, "exchange_ts")
        depth_lag_ms = 0.0
    else:
        depth = clean_depth(depth)
        if args.depth_lag_ms == "auto":
            depth_lag_ms = (
                estimate_depth_lag_ms(depth, trades) if not trades.empty else 0.0
            )
        else:
            depth_lag_ms = float(args.depth_lag_ms)
        # Put the book on the trades' exchange clock.
        depth["ts"] = depth["ts"] - pd.Timedelta(milliseconds=depth_lag_ms)
    if len(depth) < 100:
        return None, f"{symbol}: too few depth rows ({len(depth)})"
    tick = infer_tick(depth)
    book = book_features(depth, args.level_decay)
    grid = build_grid(book, args.grid_ms, args.gap_sec)
    step_sec = args.grid_ms / 1000.0
    ofi_steps = max(1, round(args.ofi_window_sec / step_sec))
    models = FairValueModels(grid.frame, tick, ofi_steps)

    n = len(grid.ts_ns)
    live = grid.frame["segment"].to_numpy() >= 0
    cumulative = np.cumsum(live)
    split = min(
        int(np.searchsorted(cumulative, cumulative[-1] * args.train_fraction)), n - 1
    )
    is_train = np.arange(n) < split
    test_start_ns = int(grid.ts_ns[split])

    rows = []
    info = {
        "symbol": symbol,
        "depth_rows": len(depth),
        "trades": len(trades),
        "tick": tick,
        "clock": clock,
        "depth_lag_ms": depth_lag_ms,
        "depth_latency_ms_p50": latency["depth"].get("p50", float("nan")),
        "trade_latency_ms_p50": latency["trade"].get("p50", float("nan")),
        "median_spread_bps": float(np.median(book["spread"] / book["mid"]) * BPS),
        "active_hours": float(live.sum() * args.grid_ms / 3.6e6),
        "updates_per_sec": len(depth) / max(live.sum() * args.grid_ms / 1e3, 1.0),
        "start": pd.Timestamp(grid.ts_ns[0], tz="UTC"),
        "end": pd.Timestamp(grid.ts_ns[-1], tz="UTC"),
    }
    for horizon in horizons:
        steps = max(1, round(horizon / step_sec))
        target, valid = future_mid_bps(grid, steps)
        train = valid & is_train
        test = valid & ~is_train
        if train.sum() < args.min_test_points or test.sum() < args.min_test_points:
            return None, f"{symbol}: not enough points for {horizon:g}s"
        models.fit(horizon, target, train, args.ridge)
        mid_mse = None
        for name in ESTIMATORS:
            pred = models.predict_bps(name, horizon)
            metrics = prediction_metrics(pred[test], target[test])
            if name == "mid":
                mid_mse = metrics["mse"]
                info[f"moved_share_{horizon:g}s"] = float(np.mean(target[test] != 0))
            metrics["skill"] = (
                1.0 - metrics["mse"] / mid_mse if mid_mse > 0 else float("nan")
            )
            metrics["dm_t"] = diebold_mariano_t(pred[test], target[test], steps)
            rows.append(
                {
                    "symbol": symbol,
                    "estimator": name,
                    "horizon_s": horizon,
                    **metrics,
                    "test_points": int(test.sum()),
                }
            )
    markouts = []
    quote_horizon = min(horizons, key=lambda h: abs(h - args.quote_horizon))
    if not trades.empty:
        for half_spread in half_spreads(args):
            for name in ESTIMATORS:
                result = simulate_markouts(
                    grid,
                    book,
                    trades,
                    models.predict_bps(name, quote_horizon),
                    test_start_ns,
                    tick,
                    args,
                    horizons,
                    half_spread,
                )
                if result is not None:
                    markouts.append({"symbol": symbol, "estimator": name, **result})
    events, event_info = evaluate_events(book, test_start_ns, tick, args)
    info.update(event_info)
    events = [{"symbol": symbol, **row} for row in events]
    info["fitted"] = {
        h: {
            "mp1_beta": p["mp1_beta"],
            "ofi_beta": p["ofi_beta"],
            "combo_beta": [float(v) for v in p["combo_beta"]],
        }
        for h, p in models.params.items()
    }
    return {
        "info": info,
        "prediction": rows,
        "events": events,
        "markout": markouts,
    }, None


# --------------------------------------------------------------------------
# Report


def _fmt(value, digits=3):
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "–"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _table(frame: pd.DataFrame, columns, digits=3):
    header = "| " + " | ".join(columns) + " |"
    sep = "|" + "|".join("---" for _ in columns) + "|"
    lines = [header, sep]
    for _, row in frame.iterrows():
        lines.append("| " + " | ".join(_fmt(row[c], digits) for c in columns) + " |")
    return "\n".join(lines)


EVENT_COLUMNS = [
    "coverage",
    "hit_rate",
    "edge_bps",
    "up_share",
    "skill",
    "three_class_acc",
    "flat_share",
    "test_points",
]


def render_event_summary(events: pd.DataFrame):
    lines = ["", "## 汇总：事件时间方向（各品种中位数）", ""]
    lines.append(
        "next_change：预测下一次 mid 变动的方向。coverage 是给出方向的比例，"
        "hit_rate 是给出方向时猜对的比例（随机为 0.5），edge_bps 是按预测方向"
        "持有到下一次变动的平均收益（不给方向记 0）。after_N_updates：N 次 L1 "
        "变化后的 mid；three_class_acc 把小于死区的偏移算作“不变”，按"
        "涨/平/跌三类计准确率。要在实盘用上，延迟必须明显短于"
        "上表的 next_change_wait_ms_p50。"
    )
    for label, sub in events.groupby("label", sort=False):
        cols = [
            c
            for c in EVENT_COLUMNS
            if c != "test_points" and c in sub and sub[c].notna().any()
        ]
        agg = sub.groupby("estimator")[cols].median().reindex(ESTIMATORS).reset_index()
        lines.append("")
        lines.append(f"### {label}")
        lines.append("")
        lines.append(_table(agg, ["estimator"] + cols, 3))
    return lines


def render_report(results, horizons, args, skipped):
    lines = ["# Fair value 估计方法离线对比", ""]
    lines.append(
        f"参数：grid {args.grid_ms}ms，train/test {args.train_fraction:.0%}/{1 - args.train_fraction:.0%}（按时间切分），"
        f"OFI 窗口 {args.ofi_window_sec:g}s，多档衰减 {args.level_decay:g}，"
        f"模拟报价半价差 {args.quote_half_spread_bps}bps，延迟 {args.latency_ms:g}ms，"
        f"成交规则 {args.fill_rule}，报价用 {args.quote_horizon:g}s 模型，"
        f"时钟 {args.clock}，事件 horizon {args.event_horizons} 次 L1 变化。"
    )
    lines.append("")
    info = pd.DataFrame([r["info"] for r in results])
    lines.append("## 数据")
    lines.append(
        _table(
            info,
            [
                "symbol",
                "clock",
                "depth_lag_ms",
                "depth_latency_ms_p50",
                "trade_latency_ms_p50",
                "next_change_wait_ms_p50",
                "active_hours",
                "depth_rows",
                "trades",
                "updates_per_sec",
                "median_spread_bps",
            ]
            + [f"moved_share_{h:g}s" for h in horizons],
            2,
        )
    )
    if skipped:
        lines.append("")
        lines.append("跳过：" + "；".join(skipped))
    prediction = pd.DataFrame([row for r in results for row in r["prediction"]])
    lines.append("")
    lines.append("## 汇总：各品种测试集 skill（1 − MSE/MSE_mid）中位数")
    summary = prediction.pivot_table(
        index="estimator", columns="horizon_s", values="skill", aggfunc="median"
    ).reindex(ESTIMATORS)
    summary.columns = [f"skill_{c:g}s" for c in summary.columns]
    wins = (
        prediction.loc[prediction["estimator"] != "mid"]
        .assign(win=lambda f: f["skill"] > 0)
        .pivot_table(
            index="estimator", columns="horizon_s", values="win", aggfunc="mean"
        )
    )
    for h in horizons:
        summary[f"beats_mid_{h:g}s"] = wins.get(h)
    summary = summary.reset_index()
    lines.append(_table(summary, list(summary.columns), 3))

    events = pd.DataFrame([row for r in results for row in r.get("events", [])])
    if not events.empty:
        lines.extend(render_event_summary(events))

    markout = pd.DataFrame([row for r in results for row in r["markout"]])
    if not markout.empty:
        cols = ["fills_per_hour", "edge_vs_mid_bps"] + [
            f"markout_{h:g}s_bps" for h in horizons
        ]
        for half_spread, sub in markout.groupby("half_spread_bps"):
            lines.append("")
            lines.append(
                f"## 汇总：半价差 {half_spread:g}bps 模拟报价的 markout（bps，各品种中位数）"
            )
            agg = (
                sub.groupby("estimator")[cols]
                .median()
                .reindex(ESTIMATORS)
                .reset_index()
            )
            lines.append(_table(agg, ["estimator"] + cols, 3))

    lines.append("")
    lines.append(f"## 拟合系数（{args.quote_horizon:g}s 附近的模型）")
    lines.append("")
    lines.append(
        "mp1_beta：microprice_l1_fitted 对 L1 microprice 偏移的缩放；"
        "ofi_beta：ofi_adjusted 在 microprice 之上的 OFI 系数（bps / 一档深度）。"
    )
    lines.append("")
    beta_rows = []
    for result in results:
        fitted = result["info"]["fitted"]
        horizon = min(fitted, key=lambda h: abs(h - args.quote_horizon))
        beta_rows.append(
            {
                "symbol": result["info"]["symbol"],
                "mp1_beta": fitted[horizon]["mp1_beta"],
                "ofi_beta": fitted[horizon]["ofi_beta"],
            }
        )
    lines.append(_table(pd.DataFrame(beta_rows), ["symbol", "mp1_beta", "ofi_beta"], 3))

    for result in results:
        symbol = result["info"]["symbol"]
        lines.append("")
        lines.append(f"## {symbol}")
        frame = pd.DataFrame(result["prediction"])
        for h in horizons:
            sub = frame.loc[frame["horizon_s"] == h]
            lines.append("")
            lines.append(
                f"预测 {h:g}s 后 mid（测试集 {int(sub['test_points'].iloc[0])} 点）"
            )
            lines.append("")
            lines.append(
                _table(
                    sub,
                    [
                        "estimator",
                        "rmse_bps",
                        "mae_bps",
                        "skill",
                        "dm_t",
                        "corr",
                        "hit_rate",
                        "mean_abs_lean_bps",
                    ],
                    4,
                )
            )
        if result.get("events"):
            lines.append("")
            lines.append("事件时间（测试集，按 L1 变化计数）")
            lines.append("")
            ev = pd.DataFrame(result["events"])
            lines.append(_table(ev, ["label", "estimator"] + EVENT_COLUMNS, 3))
        if result["markout"]:
            lines.append("")
            lines.append("模拟报价 markout（bps，均值；se 为标准误）")
            lines.append("")
            mk = pd.DataFrame(result["markout"])
            cols = [
                "half_spread_bps",
                "estimator",
                "fills",
                "fills_per_hour",
                "edge_vs_mid_bps",
            ]
            for h in horizons:
                cols += [f"markout_{h:g}s_bps", f"markout_{h:g}s_se"]
            lines.append(_table(mk, cols, 3))
    lines.append("")
    return "\n".join(lines)


def main(argv=None):
    args = parse_args(argv)
    horizons = [float(h) for h in args.horizons.split(",") if h.strip()]
    symbols = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
    days = {d.strip() for d in args.days.split(",") if d.strip()}
    files = discover_files(Path(args.data_dir), symbols, days)
    if not files:
        print(f"No recorder files found in {args.data_dir}", file=sys.stderr)
        return 1
    results, skipped = [], []
    for symbol in sorted(files):
        if not files[symbol]["depth"]:
            continue
        result, reason = evaluate_symbol(symbol, files[symbol], args, horizons)
        if result is None:
            skipped.append(reason)
            print(f"skip {reason}", file=sys.stderr)
            continue
        print(f"done {symbol}", file=sys.stderr)
        results.append(result)
    if not results:
        print("No symbol had enough data", file=sys.stderr)
        return 1
    report = render_report(results, horizons, args, skipped)
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
    else:
        print(report)
    if args.csv:
        rows = [row for r in results for row in r["prediction"]]
        pd.DataFrame(rows).to_csv(args.csv, index=False)
        event_rows = [row for r in results for row in r.get("events", [])]
        if event_rows:
            pd.DataFrame(event_rows).to_csv(
                Path(args.csv).with_name(Path(args.csv).stem + "_events.csv"),
                index=False,
            )
        markout_rows = [row for r in results for row in r["markout"]]
        if markout_rows:
            pd.DataFrame(markout_rows).to_csv(
                Path(args.csv).with_name(Path(args.csv).stem + "_markout.csv"),
                index=False,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
