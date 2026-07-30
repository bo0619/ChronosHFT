"""Offline calibration primitives for Paper market-making observations."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import asdict, dataclass
import math
import sqlite3
from typing import Iterable, Sequence

import numpy as np


TERMINAL_ORDER_STATUSES = frozenset(
    {
        "FILLED",
        "CANCELLED",
        "CANCELED",
        "EXPIRED",
        "REJECTED",
        "REJECTED_LOCALLY",
    }
)

MARKOUT_FEATURE_NAMES = (
    "fill_depth_bps",
    "quote_age_s",
    "queue_ahead_qty",
    "sigma_bps",
    "signed_trade_imbalance",
    "microprice_offset_bps",
    "market_spread_bps",
    "flow_cost_bps",
    "market_trade_transport_latency_ms",
    "market_trade_local_age_ms",
    "strategy_callback_age_ms",
    "basis_bps",
    "funding_rate_bps",
    "fill_trigger_through",
)


@dataclass(frozen=True, slots=True)
class QuoteExposure:
    run_id: str
    client_oid: str
    symbol: str
    side: str
    start_time: float
    exposure_seconds: float
    depth_bps: float
    filled: bool


@dataclass(frozen=True, slots=True)
class IntensityFit:
    ready: bool
    reason: str
    exposure_count: int
    fill_count: int
    total_exposure_seconds: float
    depth_span_bps: float
    A_per_s: float | None
    k_per_bps: float | None
    log_likelihood: float | None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MarkoutObservation:
    run_id: str
    symbol: str
    side: str
    horizon_ms: int
    observed_time: float
    signed_markout_bps: float
    features: tuple[float | None, ...]


def _finite(value, default=None):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _latest_row_at_or_before(
    indexed_rows: dict[tuple[str, str], tuple[list[float], list[sqlite3.Row]]],
    key: tuple[str, str],
    timestamp: float,
    max_gap_sec: float,
) -> sqlite3.Row | None:
    times_and_rows = indexed_rows.get(key)
    if times_and_rows is None:
        return None
    times, rows = times_and_rows
    index = bisect_right(times, timestamp) - 1
    if index < 0:
        return None
    if timestamp - times[index] > max_gap_sec:
        return None
    return rows[index]


def _index_rows(
    rows: Iterable[sqlite3.Row],
    *,
    time_field: str,
) -> dict[tuple[str, str], tuple[list[float], list[sqlite3.Row]]]:
    grouped: dict[tuple[str, str], list[tuple[float, sqlite3.Row]]] = {}
    for row in rows:
        timestamp = _finite(row[time_field])
        if timestamp is None:
            continue
        key = (str(row["run_id"]), str(row["symbol"]).upper())
        grouped.setdefault(key, []).append((timestamp, row))
    result = {}
    for key, values in grouped.items():
        values.sort(key=lambda item: item[0])
        result[key] = (
            [item[0] for item in values],
            [item[1] for item in values],
        )
    return result


def load_quote_exposures(
    connection: sqlite3.Connection,
    *,
    symbol: str = "",
    run_id: str = "",
    rpi_only: bool = True,
    max_strategy_join_sec: float = 2.0,
) -> list[QuoteExposure]:
    """Reconstruct first-fill exposure episodes from OMS lifecycle rows."""
    connection.row_factory = sqlite3.Row
    where = []
    parameters: list[object] = []
    if symbol:
        where.append("symbol = ?")
        parameters.append(symbol.upper())
    if run_id:
        where.append("run_id = ?")
        parameters.append(run_id)
    if rpi_only:
        where.append("is_rpi = 1")
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    order_rows = connection.execute(
        f"""
        SELECT event_id, run_id, client_oid, symbol, side, status, price,
               filled_quantity, is_rpi, created_monotonic,
               updated_monotonic, event_time
        FROM paper_order_events
        {where_sql}
        ORDER BY run_id, client_oid, event_id
        """,
        parameters,
    ).fetchall()
    strategy_rows = connection.execute(
        """
        SELECT run_id, symbol, sample_time, mid_price
        FROM paper_strategy_samples
        WHERE mid_price IS NOT NULL AND mid_price > 0.0
        ORDER BY run_id, symbol, sample_time
        """
    ).fetchall()
    strategy_index = _index_rows(strategy_rows, time_field="sample_time")

    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in order_rows:
        grouped.setdefault(
            (str(row["run_id"]), str(row["client_oid"])),
            [],
        ).append(row)

    result = []
    for rows in grouped.values():
        first = rows[0]
        side = str(first["side"] or "").upper()
        price = _finite(first["price"])
        if side not in {"BUY", "SELL"} or price is None or price <= 0.0:
            continue
        active_rows = [
            row
            for row in rows
            if str(row["status"] or "").upper()
            in {"NEW", "PARTIALLY_FILLED", "FILLED"}
        ]
        if not active_rows:
            continue
        start = active_rows[0]
        fill = next(
            (
                row
                for row in rows
                if (_finite(row["filled_quantity"], 0.0) or 0.0) > 0.0
            ),
            None,
        )
        terminal = next(
            (
                row
                for row in rows
                if str(row["status"] or "").upper()
                in TERMINAL_ORDER_STATUSES
            ),
            None,
        )
        stop = fill or terminal
        if stop is None:
            continue
        start_monotonic = _finite(start["updated_monotonic"])
        stop_monotonic = _finite(stop["updated_monotonic"])
        if fill is not None and (
            start_monotonic is None
            or stop_monotonic is None
            or stop_monotonic <= start_monotonic
        ):
            start_monotonic = _finite(start["created_monotonic"])
        if (
            start_monotonic is None
            or stop_monotonic is None
            or stop_monotonic <= start_monotonic
        ):
            continue
        event_time = _finite(start["event_time"])
        if event_time is None:
            continue
        key = (str(start["run_id"]), str(start["symbol"]).upper())
        strategy = _latest_row_at_or_before(
            strategy_index,
            key,
            event_time,
            max_strategy_join_sec,
        )
        if strategy is None:
            continue
        mid = _finite(strategy["mid_price"])
        if mid is None or mid <= 0.0:
            continue
        depth = (
            math.log(mid / price) * 10_000.0
            if side == "BUY"
            else math.log(price / mid) * 10_000.0
        )
        if not math.isfinite(depth) or depth < -1e-6:
            continue
        result.append(
            QuoteExposure(
                run_id=key[0],
                client_oid=str(start["client_oid"]),
                symbol=key[1],
                side=side,
                start_time=event_time,
                exposure_seconds=stop_monotonic - start_monotonic,
                depth_bps=max(0.0, depth),
                filled=fill is not None,
            )
        )
    return result


def _intensity_log_likelihood(
    exposures: Sequence[QuoteExposure],
    A_per_s: float,
    k_per_bps: float,
) -> float:
    result = 0.0
    for exposure in exposures:
        intensity = A_per_s * math.exp(-k_per_bps * exposure.depth_bps)
        result -= exposure.exposure_seconds * intensity
        if exposure.filled:
            result += math.log(max(intensity, 1e-300))
    return result


def fit_exponential_intensity(
    exposures: Sequence[QuoteExposure],
    *,
    min_exposures: int = 30,
    min_fills: int = 5,
    min_depth_span_bps: float = 0.25,
    min_k_per_bps: float = 1e-6,
    max_k_per_bps: float = 100.0,
) -> IntensityFit:
    """Fit lambda(delta)=A*exp(-k*delta) with censored exposure likelihood."""
    parsed = [
        exposure
        for exposure in exposures
        if exposure.exposure_seconds > 0.0
        and exposure.depth_bps >= 0.0
        and math.isfinite(exposure.exposure_seconds)
        and math.isfinite(exposure.depth_bps)
    ]
    exposure_count = len(parsed)
    fill_count = sum(exposure.filled for exposure in parsed)
    total_exposure = sum(exposure.exposure_seconds for exposure in parsed)
    depths = [exposure.depth_bps for exposure in parsed]
    depth_span = max(depths) - min(depths) if depths else 0.0
    reason = ""
    if exposure_count < min_exposures:
        reason = f"insufficient_exposures:{exposure_count}<{min_exposures}"
    elif fill_count < min_fills:
        reason = f"insufficient_fills:{fill_count}<{min_fills}"
    elif total_exposure <= 0.0:
        reason = "non_positive_exposure"
    elif depth_span < min_depth_span_bps:
        reason = f"insufficient_depth_span:{depth_span:g}<{min_depth_span_bps:g}"
    if reason:
        return IntensityFit(
            False,
            reason,
            exposure_count,
            fill_count,
            total_exposure,
            depth_span,
            None,
            None,
            None,
        )

    filled_depth_sum = sum(
        exposure.depth_bps for exposure in parsed if exposure.filled
    )

    def profile(k_value: float) -> tuple[float, float]:
        weighted_exposure = sum(
            exposure.exposure_seconds
            * math.exp(-k_value * exposure.depth_bps)
            for exposure in parsed
        )
        if weighted_exposure <= 0.0 or not math.isfinite(weighted_exposure):
            return -math.inf, math.nan
        A_value = fill_count / weighted_exposure
        log_likelihood = (
            fill_count * math.log(A_value)
            - k_value * filled_depth_sum
            - fill_count
        )
        return log_likelihood, A_value

    lower = max(0.0, float(min_k_per_bps))
    upper = max(lower + 1e-9, float(max_k_per_bps))
    ratio = (math.sqrt(5.0) - 1.0) / 2.0
    left = upper - ratio * (upper - lower)
    right = lower + ratio * (upper - lower)
    left_value = profile(left)[0]
    right_value = profile(right)[0]
    for _ in range(160):
        if upper - lower <= 1e-10 * max(1.0, upper):
            break
        if left_value < right_value:
            lower = left
            left = right
            left_value = right_value
            right = lower + ratio * (upper - lower)
            right_value = profile(right)[0]
        else:
            upper = right
            right = left
            right_value = left_value
            left = upper - ratio * (upper - lower)
            left_value = profile(left)[0]
    candidates = (float(min_k_per_bps), (lower + upper) / 2.0, max_k_per_bps)
    k_value = max(candidates, key=lambda value: profile(value)[0])
    log_likelihood, A_value = profile(k_value)
    if not all(math.isfinite(value) and value > 0.0 for value in (A_value, k_value)):
        return IntensityFit(
            False,
            "non_finite_fit",
            exposure_count,
            fill_count,
            total_exposure,
            depth_span,
            None,
            None,
            None,
        )
    return IntensityFit(
        True,
        "",
        exposure_count,
        fill_count,
        total_exposure,
        depth_span,
        A_value,
        k_value,
        log_likelihood,
    )


def bootstrap_intensity_intervals(
    exposures: Sequence[QuoteExposure],
    *,
    block_seconds: float = 300.0,
    replicates: int = 200,
    seed: int = 7,
) -> dict:
    if replicates <= 0 or block_seconds <= 0.0:
        return {"replicates": 0, "A_per_s_95pct": None, "k_per_bps_95pct": None}
    blocks: dict[int, list[QuoteExposure]] = {}
    for exposure in exposures:
        block = math.floor(exposure.start_time / block_seconds)
        blocks.setdefault(block, []).append(exposure)
    values = list(blocks.values())
    if len(values) < 2:
        return {"replicates": 0, "A_per_s_95pct": None, "k_per_bps_95pct": None}
    generator = np.random.default_rng(seed)
    fitted = []
    for _ in range(int(replicates)):
        selected = generator.integers(0, len(values), size=len(values))
        sample = [item for index in selected for item in values[int(index)]]
        fit = fit_exponential_intensity(
            sample,
            min_exposures=1,
            min_fills=1,
            min_depth_span_bps=0.0,
        )
        if fit.ready:
            fitted.append((fit.A_per_s, fit.k_per_bps))
    if not fitted:
        return {"replicates": 0, "A_per_s_95pct": None, "k_per_bps_95pct": None}
    array = np.asarray(fitted, dtype=float)
    return {
        "replicates": len(fitted),
        "block_seconds": block_seconds,
        "A_per_s_95pct": np.quantile(array[:, 0], [0.025, 0.975]).tolist(),
        "k_per_bps_95pct": np.quantile(array[:, 1], [0.025, 0.975]).tolist(),
    }


def walk_forward_intensity(
    exposures: Sequence[QuoteExposure],
    *,
    train_fraction: float = 0.7,
) -> dict:
    ordered = sorted(exposures, key=lambda item: item.start_time)
    split = min(len(ordered) - 1, max(1, int(len(ordered) * train_fraction)))
    if len(ordered) < 4 or split <= 0 or split >= len(ordered):
        return {"ready": False, "reason": "insufficient_walk_forward_rows"}
    train = ordered[:split]
    test = ordered[split:]
    fit = fit_exponential_intensity(
        train,
        min_exposures=2,
        min_fills=1,
        min_depth_span_bps=0.0,
    )
    if not fit.ready:
        return {"ready": False, "reason": fit.reason}
    baseline_A = sum(item.filled for item in train) / sum(
        item.exposure_seconds for item in train
    )
    model_ll = _intensity_log_likelihood(test, fit.A_per_s, fit.k_per_bps)
    baseline_ll = _intensity_log_likelihood(test, baseline_A, 0.0)
    return {
        "ready": True,
        "train_count": len(train),
        "test_count": len(test),
        "train_ended_at": train[-1].start_time,
        "A_per_s": fit.A_per_s,
        "k_per_bps": fit.k_per_bps,
        "test_log_likelihood": model_ll,
        "constant_intensity_test_log_likelihood": baseline_ll,
        "log_likelihood_improvement": model_ll - baseline_ll,
        "beats_constant_intensity_baseline": model_ll > baseline_ll,
    }


def _market_index(connection: sqlite3.Connection):
    if not _table_exists(connection, "paper_market_samples"):
        return {}
    rows = connection.execute(
        """
        SELECT run_id, symbol, sample_time, basis_bps, funding_rate
        FROM paper_market_samples
        ORDER BY run_id, symbol, sample_time
        """
    ).fetchall()
    return _index_rows(rows, time_field="sample_time")


def load_markout_observations(
    connection: sqlite3.Connection,
    *,
    symbol: str = "",
    run_id: str = "",
    max_strategy_join_sec: float = 2.0,
    max_market_join_sec: float = 3.0,
) -> list[MarkoutObservation]:
    connection.row_factory = sqlite3.Row
    where = []
    parameters: list[object] = []
    if symbol:
        where.append("m.symbol = ?")
        parameters.append(symbol.upper())
    if run_id:
        where.append("m.run_id = ?")
        parameters.append(run_id)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    rows = connection.execute(
        f"""
        SELECT m.run_id, m.symbol, m.side, m.horizon_ms,
               m.signed_markout_bps, f.exchange_time, f.fill_price,
               f.mid_at_fill, f.quote_age_ms, f.queue_ahead_before,
               f.market_trade_transport_latency_ms,
               f.market_trade_local_age_ms, f.fill_trigger
        FROM paper_fill_markouts AS m
        JOIN paper_fills AS f
          ON f.run_id = m.run_id
         AND f.client_oid = m.client_oid
         AND CAST(f.trade_id AS TEXT) = m.trade_id
        {where_sql}
        ORDER BY f.exchange_time, m.markout_id
        """,
        parameters,
    ).fetchall()
    strategy_rows = connection.execute(
        """
        SELECT run_id, symbol, sample_time, sigma_bps,
               signed_trade_imbalance, microprice_offset_bps,
               market_spread_bps, bid_flow_cost_bps, ask_flow_cost_bps,
               callback_age_ms
        FROM paper_strategy_samples
        ORDER BY run_id, symbol, sample_time
        """
    ).fetchall()
    strategy_index = _index_rows(strategy_rows, time_field="sample_time")
    market_index = _market_index(connection)

    result = []
    for row in rows:
        observed_time = _finite(row["exchange_time"])
        fill_price = _finite(row["fill_price"])
        mid = _finite(row["mid_at_fill"])
        markout = _finite(row["signed_markout_bps"])
        if (
            observed_time is None
            or fill_price is None
            or fill_price <= 0.0
            or markout is None
        ):
            continue
        key = (str(row["run_id"]), str(row["symbol"]).upper())
        strategy = _latest_row_at_or_before(
            strategy_index,
            key,
            observed_time,
            max_strategy_join_sec,
        )
        if strategy is None:
            continue
        market = _latest_row_at_or_before(
            market_index,
            key,
            observed_time,
            max_market_join_sec,
        )
        side = str(row["side"] or "").upper()
        if side not in {"BUY", "SELL"}:
            continue
        fill_depth = None
        if mid is not None and mid > 0.0:
            fill_depth = (
                math.log(mid / fill_price) * 10_000.0
                if side == "BUY"
                else math.log(fill_price / mid) * 10_000.0
            )
        flow_field = (
            "bid_flow_cost_bps" if side == "BUY" else "ask_flow_cost_bps"
        )
        quote_age_ms = _finite(row["quote_age_ms"])
        features = (
            fill_depth,
            quote_age_ms / 1000.0 if quote_age_ms is not None else None,
            _finite(row["queue_ahead_before"]),
            _finite(strategy["sigma_bps"]),
            _finite(strategy["signed_trade_imbalance"]),
            _finite(strategy["microprice_offset_bps"]),
            _finite(strategy["market_spread_bps"]),
            _finite(strategy[flow_field]),
            _finite(row["market_trade_transport_latency_ms"]),
            _finite(row["market_trade_local_age_ms"]),
            _finite(strategy["callback_age_ms"]),
            _finite(market["basis_bps"]) if market is not None else None,
            (
                (_finite(market["funding_rate"]) or 0.0) * 10_000.0
                if market is not None
                else None
            ),
            1.0 if str(row["fill_trigger"] or "") == "through" else 0.0,
        )
        result.append(
            MarkoutObservation(
                run_id=key[0],
                symbol=key[1],
                side=side,
                horizon_ms=int(row["horizon_ms"]),
                observed_time=observed_time,
                signed_markout_bps=markout,
                features=features,
            )
        )
    return result


def fit_conditional_markout(
    observations: Sequence[MarkoutObservation],
    *,
    min_samples: int = 30,
    train_fraction: float = 0.7,
    ridge_penalty: float = 1.0,
) -> dict:
    """Fit an interpretable candidate markout model with chronological OOS."""
    if not 0.0 < train_fraction < 1.0:
        return {"ready": False, "reason": "invalid_train_fraction"}
    ordered = sorted(
        (
            item
            for item in observations
            if math.isfinite(item.observed_time)
            and math.isfinite(item.signed_markout_bps)
            and len(item.features) == len(MARKOUT_FEATURE_NAMES)
        ),
        key=lambda item: item.observed_time,
    )
    if len(ordered) < min_samples:
        return {
            "ready": False,
            "reason": f"insufficient_samples:{len(ordered)}<{min_samples}",
            "sample_count": len(ordered),
        }
    split = min(len(ordered) - 1, max(1, int(len(ordered) * train_fraction)))
    train = ordered[:split]
    test = ordered[split:]
    train_X = np.asarray(
        [
            [np.nan if value is None else value for value in item.features]
            for item in train
        ],
        dtype=float,
    )
    test_X = np.asarray(
        [
            [np.nan if value is None else value for value in item.features]
            for item in test
        ],
        dtype=float,
    )
    train_y = np.asarray([item.signed_markout_bps for item in train], dtype=float)
    test_y = np.asarray([item.signed_markout_bps for item in test], dtype=float)
    medians = np.asarray(
        [
            float(np.median(column[np.isfinite(column)]))
            if np.any(np.isfinite(column))
            else 0.0
            for column in train_X.T
        ],
        dtype=float,
    )
    train_X = np.where(np.isfinite(train_X), train_X, medians)
    test_X = np.where(np.isfinite(test_X), test_X, medians)
    scales = np.std(train_X, axis=0)
    scales = np.where(scales > 1e-12, scales, 1.0)
    means = np.mean(train_X, axis=0)
    train_Z = (train_X - means) / scales
    test_Z = (test_X - means) / scales
    train_design = np.column_stack((np.ones(len(train_Z)), train_Z))
    test_design = np.column_stack((np.ones(len(test_Z)), test_Z))
    penalty = np.eye(train_design.shape[1]) * max(0.0, ridge_penalty)
    penalty[0, 0] = 0.0
    coefficients = np.linalg.solve(
        train_design.T @ train_design + penalty,
        train_design.T @ train_y,
    )
    train_prediction = train_design @ coefficients
    test_prediction = test_design @ coefficients
    baseline_prediction = np.full_like(test_y, np.mean(train_y))

    def metrics(actual: np.ndarray, predicted: np.ndarray) -> dict:
        residual = actual - predicted
        return {
            "mae_bps": float(np.mean(np.abs(residual))),
            "rmse_bps": float(np.sqrt(np.mean(residual * residual))),
            "mean_error_bps": float(np.mean(residual)),
        }

    train_residual = train_y - train_prediction
    oos = metrics(test_y, test_prediction)
    baseline_oos = metrics(test_y, baseline_prediction)
    beats_baseline = (
        oos["mae_bps"] < baseline_oos["mae_bps"]
        and oos["rmse_bps"] < baseline_oos["rmse_bps"]
    )
    return {
        "ready": True,
        "candidate_only": True,
        "sample_count": len(ordered),
        "train_count": len(train),
        "test_count": len(test),
        "train_ended_at": train[-1].observed_time,
        "ridge_penalty": ridge_penalty,
        "feature_names": list(MARKOUT_FEATURE_NAMES),
        "feature_medians": medians.tolist(),
        "feature_means": means.tolist(),
        "feature_scales": scales.tolist(),
        "intercept_bps": float(coefficients[0]),
        "standardized_coefficients_bps": dict(
            zip(MARKOUT_FEATURE_NAMES, coefficients[1:].tolist(), strict=True)
        ),
        "train_residual_lower_05_bps": float(
            np.quantile(train_residual, 0.05)
        ),
        "oos": oos,
        "constant_mean_baseline_oos": baseline_oos,
        "beats_constant_mean_baseline": beats_baseline,
        "promotion_eligible": False,
    }
