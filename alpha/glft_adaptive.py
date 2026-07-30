"""Paper-only adaptive state used by the extended GLFT quoting model.

The estimators in this module never submit or cancel orders.  They expose
bounded, unit-labelled statistics for the strategy and leave all final volume
and inventory limits to the OMS-facing strategy layer.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass
from numbers import Real
from typing import Iterable, Sequence

import numpy as np

from event.type import Side


class _RollingMoments:
    def __init__(self, max_samples: int) -> None:
        self.max_samples = _positive_int(max_samples)
        self.values: deque[float] = deque()
        self.total = 0.0
        self.total_squared = 0.0

    def update(self, value: float) -> None:
        parsed = _finite(value, "observation")
        if len(self.values) >= self.max_samples:
            removed = self.values.popleft()
            self.total -= removed
            self.total_squared -= removed * removed
        self.values.append(parsed)
        self.total += parsed
        self.total_squared += parsed * parsed

    @property
    def count(self) -> int:
        return len(self.values)

    @property
    def mean(self) -> float:
        return self.total / self.count if self.count else 0.0

    @property
    def variance(self) -> float:
        if self.count < 2:
            return 0.0
        centered_sum = self.total_squared - self.total * self.total / self.count
        return max(0.0, centered_sum / (self.count - 1))

    @property
    def standard_error(self) -> float:
        if self.count < 2:
            return math.inf
        return math.sqrt(self.variance / self.count)


@dataclass(frozen=True, slots=True)
class MarkoutEstimate:
    adverse_cost_bps: float
    sample_count: int
    horizon_ms: int | None
    mean_signed_markout_bps: float | None
    standard_error_bps: float | None


@dataclass(frozen=True, slots=True)
class ResolvedFillMarkout:
    client_oid: str
    trade_id: str
    symbol: str
    side: Side
    fill_price: float
    horizon_ms: int
    mid_price: float
    signed_markout_bps: float
    fill_observed_monotonic: float
    mid_observed_monotonic: float
    observation_lag_ms: float


@dataclass(frozen=True, slots=True)
class FlowAdverseCostEstimate:
    microprice_offset_bps: float
    signed_trade_imbalance: float
    bid_adverse_cost_bps: float
    ask_adverse_cost_bps: float


def estimate_flow_adverse_costs(
    *,
    mid_price: float,
    best_bid: float,
    best_ask: float,
    bid_quantity: float,
    ask_quantity: float,
    signed_trade_imbalance: float,
    trade_imbalance_cost_bps: float,
    microprice_weight: float,
    max_adverse_cost_bps: float,
) -> FlowAdverseCostEstimate:
    """Convert directional book and trade pressure into side-specific cost."""

    mid = _positive(mid_price, "mid_price")
    bid = _positive(best_bid, "best_bid")
    ask = _positive(best_ask, "best_ask")
    if not bid < mid < ask:
        raise ValueError("best_bid < mid_price < best_ask is required")
    bid_qty = _nonnegative(bid_quantity, "bid_quantity")
    ask_qty = _nonnegative(ask_quantity, "ask_quantity")
    imbalance = max(
        -1.0,
        min(1.0, _finite(signed_trade_imbalance, "signed_trade_imbalance")),
    )
    trade_cost = _nonnegative(
        trade_imbalance_cost_bps,
        "trade_imbalance_cost_bps",
    )
    book_weight = _nonnegative(microprice_weight, "microprice_weight")
    cost_cap = _nonnegative(max_adverse_cost_bps, "max_adverse_cost_bps")

    total_quantity = bid_qty + ask_qty
    microprice = (
        (ask * bid_qty + bid * ask_qty) / total_quantity
        if total_quantity > 0.0
        else mid
    )
    microprice_offset = math.log(microprice / mid) * 10_000.0
    bid_cost = (
        trade_cost * max(0.0, -imbalance)
        + book_weight * max(0.0, -microprice_offset)
    )
    ask_cost = (
        trade_cost * max(0.0, imbalance)
        + book_weight * max(0.0, microprice_offset)
    )
    return FlowAdverseCostEstimate(
        microprice_offset_bps=microprice_offset,
        signed_trade_imbalance=imbalance,
        bid_adverse_cost_bps=min(cost_cap, bid_cost),
        ask_adverse_cost_bps=min(cost_cap, ask_cost),
    )


@dataclass(slots=True)
class _PendingFill:
    client_oid: str
    trade_id: str
    symbol: str
    side: Side
    price: float
    observed_at_monotonic: float
    unresolved_horizons_ms: set[int]


class FillMarkoutEstimator:
    """Estimate conservative post-fill adverse selection at fixed horizons."""

    def __init__(
        self,
        *,
        horizons_ms: Sequence[int] = (100, 500, 1000),
        min_samples: int = 20,
        confidence_z: float = 1.645,
        max_pending: int = 5000,
        window_size: int = 500,
    ) -> None:
        parsed_horizons = tuple(sorted({_positive_int(value) for value in horizons_ms}))
        if not parsed_horizons:
            raise ValueError("markout horizons_ms must not be empty")
        self.horizons_ms = parsed_horizons
        self.min_samples = _positive_int(min_samples)
        self.confidence_z = _nonnegative(confidence_z, "confidence_z")
        self.max_pending = _positive_int(max_pending)
        self.window_size = _positive_int(window_size)
        if self.window_size < self.min_samples:
            raise ValueError("window_size must be at least min_samples")
        self._pending: deque[_PendingFill] = deque()
        self._resolved: deque[ResolvedFillMarkout] = deque()
        self._moments: dict[tuple[str, Side, int], _RollingMoments] = defaultdict(
            lambda: _RollingMoments(self.window_size)
        )

    def record_fill(
        self,
        *,
        symbol: str,
        side: Side | str,
        fill_price: float,
        observed_at_monotonic: float,
        client_oid: str = "",
        trade_id: str = "",
    ) -> None:
        parsed_side = _side(side)
        price = _positive(fill_price, "fill_price")
        timestamp = _nonnegative(observed_at_monotonic, "observed_at_monotonic")
        self._pending.append(
            _PendingFill(
                client_oid=str(client_oid or ""),
                trade_id=str(trade_id or ""),
                symbol=_symbol(symbol),
                side=parsed_side,
                price=price,
                observed_at_monotonic=timestamp,
                unresolved_horizons_ms=set(self.horizons_ms),
            )
        )
        while len(self._pending) > self.max_pending:
            self._pending.popleft()

    def observe_mid(
        self,
        *,
        symbol: str,
        mid_price: float,
        observed_at_monotonic: float,
    ) -> int:
        normalized_symbol = _symbol(symbol)
        mid = _positive(mid_price, "mid_price")
        timestamp = _nonnegative(observed_at_monotonic, "observed_at_monotonic")
        resolved = 0
        retained: deque[_PendingFill] = deque()
        for pending in self._pending:
            if pending.symbol != normalized_symbol:
                retained.append(pending)
                continue
            age_ms = (timestamp - pending.observed_at_monotonic) * 1000.0
            if age_ms < 0.0:
                retained.append(pending)
                continue
            side_sign = 1.0 if pending.side == Side.BUY else -1.0
            signed_markout = side_sign * math.log(mid / pending.price) * 10_000.0
            for horizon_ms in tuple(pending.unresolved_horizons_ms):
                if age_ms < horizon_ms:
                    continue
                self._moments[
                    (pending.symbol, pending.side, horizon_ms)
                ].update(signed_markout)
                self._resolved.append(
                    ResolvedFillMarkout(
                        client_oid=pending.client_oid,
                        trade_id=pending.trade_id,
                        symbol=pending.symbol,
                        side=pending.side,
                        fill_price=pending.price,
                        horizon_ms=horizon_ms,
                        mid_price=mid,
                        signed_markout_bps=signed_markout,
                        fill_observed_monotonic=pending.observed_at_monotonic,
                        mid_observed_monotonic=timestamp,
                        observation_lag_ms=max(0.0, age_ms - horizon_ms),
                    )
                )
                pending.unresolved_horizons_ms.remove(horizon_ms)
                resolved += 1
            if pending.unresolved_horizons_ms:
                retained.append(pending)
        self._pending = retained
        return resolved

    def drain_resolved(self) -> tuple[ResolvedFillMarkout, ...]:
        resolved = tuple(self._resolved)
        self._resolved.clear()
        return resolved

    def estimate(self, symbol: str, side: Side | str) -> MarkoutEstimate:
        normalized_symbol = _symbol(symbol)
        parsed_side = _side(side)
        candidates = []
        total_samples = 0
        for horizon_ms in self.horizons_ms:
            moments = self._moments[(normalized_symbol, parsed_side, horizon_ms)]
            total_samples = max(total_samples, moments.count)
            if moments.count < self.min_samples:
                continue
            standard_error = moments.standard_error
            uncertainty = (
                self.confidence_z * standard_error
                if math.isfinite(standard_error)
                else 0.0
            )
            adverse_cost = max(0.0, -moments.mean + uncertainty)
            candidates.append((adverse_cost, horizon_ms, moments))
        if not candidates:
            return MarkoutEstimate(0.0, total_samples, None, None, None)
        adverse_cost, horizon_ms, moments = max(candidates, key=lambda item: item[0])
        standard_error = moments.standard_error
        return MarkoutEstimate(
            adverse_cost_bps=adverse_cost,
            sample_count=moments.count,
            horizon_ms=horizon_ms,
            mean_signed_markout_bps=moments.mean,
            standard_error_bps=(
                standard_error if math.isfinite(standard_error) else None
            ),
        )

    def summary(self, symbol: str) -> dict[str, object]:
        normalized_symbol = _symbol(symbol)
        result: dict[str, object] = {
            "pending_fill_count": 0,
            "window_size": self.window_size,
            "sides": {},
        }
        result["pending_fill_count"] = sum(
            pending.symbol == normalized_symbol for pending in self._pending
        )
        sides = {}
        for side in (Side.BUY, Side.SELL):
            estimate = self.estimate(normalized_symbol, side)
            sides[side.value] = {
                "adverse_cost_bps": estimate.adverse_cost_bps,
                "selected_horizon_ms": estimate.horizon_ms,
                "sample_count": estimate.sample_count,
                "mean_signed_markout_bps": estimate.mean_signed_markout_bps,
                "standard_error_bps": estimate.standard_error_bps,
                "horizons": {
                    str(horizon_ms): {
                        "sample_count": self._moments[
                            (normalized_symbol, side, horizon_ms)
                        ].count,
                        "mean_signed_markout_bps": self._moments[
                            (normalized_symbol, side, horizon_ms)
                        ].mean,
                    }
                    for horizon_ms in self.horizons_ms
                },
            }
        result["sides"] = sides
        return result


@dataclass(slots=True)
class _HawkesState:
    buy_trace: float = 0.0
    sell_trace: float = 0.0
    updated_at_monotonic: float | None = None
    event_count: int = 0


class HawkesFlowIntensity:
    """Bounded exponential-kernel Hawkes proxy for aggressive public flow."""

    def __init__(
        self,
        *,
        decay_rate_per_s: float = 2.0,
        self_excitation: float = 0.12,
        cross_excitation: float = 0.03,
        max_multiplier: float = 3.0,
    ) -> None:
        self.decay_rate_per_s = _positive(decay_rate_per_s, "decay_rate_per_s")
        self.self_excitation = _nonnegative(self_excitation, "self_excitation")
        self.cross_excitation = _nonnegative(cross_excitation, "cross_excitation")
        self.max_multiplier = _positive(max_multiplier, "max_multiplier")
        if self.max_multiplier < 1.0:
            raise ValueError("max_multiplier must be at least one")
        self._states: dict[str, _HawkesState] = defaultdict(_HawkesState)

    def _decay(self, state: _HawkesState, now: float) -> None:
        if state.updated_at_monotonic is None:
            state.updated_at_monotonic = now
            return
        elapsed = max(0.0, now - state.updated_at_monotonic)
        decay = math.exp(-self.decay_rate_per_s * elapsed)
        state.buy_trace *= decay
        state.sell_trace *= decay
        state.updated_at_monotonic = max(now, state.updated_at_monotonic)

    def record_trade(
        self,
        *,
        symbol: str,
        aggressor_side: Side | str,
        observed_at_monotonic: float,
        event_weight: float = 1.0,
    ) -> None:
        normalized_symbol = _symbol(symbol)
        side = _side(aggressor_side)
        timestamp = _nonnegative(observed_at_monotonic, "observed_at_monotonic")
        weight = _positive(event_weight, "event_weight")
        state = self._states[normalized_symbol]
        self._decay(state, timestamp)
        if side == Side.BUY:
            state.buy_trace += weight
        else:
            state.sell_trace += weight
        state.event_count += 1

    def multipliers(
        self,
        symbol: str,
        observed_at_monotonic: float,
    ) -> tuple[float, float]:
        state = self._states[_symbol(symbol)]
        timestamp = _nonnegative(observed_at_monotonic, "observed_at_monotonic")
        self._decay(state, timestamp)
        # Bid fills are caused by aggressive sells; ask fills by aggressive buys.
        bid = 1.0 + self.self_excitation * state.sell_trace
        bid += self.cross_excitation * state.buy_trace
        ask = 1.0 + self.self_excitation * state.buy_trace
        ask += self.cross_excitation * state.sell_trace
        return min(self.max_multiplier, bid), min(self.max_multiplier, ask)

    def summary(self, symbol: str, observed_at_monotonic: float) -> dict[str, float | int]:
        normalized_symbol = _symbol(symbol)
        bid, ask = self.multipliers(normalized_symbol, observed_at_monotonic)
        state = self._states[normalized_symbol]
        return {
            "bid_intensity_multiplier": bid,
            "ask_intensity_multiplier": ask,
            "buy_trace": state.buy_trace,
            "sell_trace": state.sell_trace,
            "event_count": state.event_count,
        }


@dataclass(slots=True)
class _MidObservation:
    price: float
    observed_at_monotonic: float


class DynamicCovarianceEstimator:
    """Synchronized EWMA covariance with diagonal shrinkage and PSD projection."""

    def __init__(
        self,
        symbols: Sequence[str],
        *,
        sample_interval_s: float = 1.0,
        max_state_age_s: float = 3.0,
        max_sync_skew_s: float = 0.25,
        ewma_alpha: float = 0.05,
        diagonal_shrinkage: float = 0.15,
        min_samples: int = 30,
    ) -> None:
        self.symbols = tuple(_symbol(symbol) for symbol in symbols)
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("dynamic covariance symbols must be unique")
        self.sample_interval_s = _positive(sample_interval_s, "sample_interval_s")
        self.max_state_age_s = _positive(max_state_age_s, "max_state_age_s")
        self.max_sync_skew_s = _nonnegative(max_sync_skew_s, "max_sync_skew_s")
        self.ewma_alpha = _unit_interval(ewma_alpha, "ewma_alpha", open_zero=True)
        self.diagonal_shrinkage = _unit_interval(
            diagonal_shrinkage,
            "diagonal_shrinkage",
        )
        self.min_samples = _positive_int(min_samples)
        self._latest: dict[str, _MidObservation] = {}
        self._last_sample_prices: np.ndarray | None = None
        self._last_sample_at: float | None = None
        self._covariance: np.ndarray | None = None
        self.sample_count = 0

    def observe_mid(
        self,
        *,
        symbol: str,
        mid_price: float,
        observed_at_monotonic: float,
    ) -> bool:
        normalized_symbol = _symbol(symbol)
        if self.symbols and normalized_symbol not in self.symbols:
            return False
        timestamp = _nonnegative(observed_at_monotonic, "observed_at_monotonic")
        self._latest[normalized_symbol] = _MidObservation(
            _positive(mid_price, "mid_price"),
            timestamp,
        )
        universe = self.symbols or tuple(sorted(self._latest))
        if not universe or any(item not in self._latest for item in universe):
            return False
        if any(
            timestamp - self._latest[item].observed_at_monotonic
            > self.max_state_age_s
            for item in universe
        ):
            return False
        observation_times = [
            self._latest[item].observed_at_monotonic for item in universe
        ]
        if max(observation_times) - min(observation_times) > self.max_sync_skew_s:
            return False
        if self._last_sample_at is not None and any(
            self._latest[item].observed_at_monotonic <= self._last_sample_at
            for item in universe
        ):
            return False
        if (
            self._last_sample_at is not None
            and timestamp - self._last_sample_at < self.sample_interval_s
        ):
            return False
        prices = np.asarray([self._latest[item].price for item in universe], dtype=float)
        if self._last_sample_prices is None or self._last_sample_at is None:
            self._last_sample_prices = prices
            self._last_sample_at = timestamp
            return False
        elapsed = timestamp - self._last_sample_at
        if elapsed <= 0.0:
            return False
        returns = np.log(prices / self._last_sample_prices) * 10_000.0 / math.sqrt(elapsed)
        outer = np.outer(returns, returns)
        if self._covariance is None:
            self._covariance = outer
        else:
            alpha = self.ewma_alpha
            self._covariance = (1.0 - alpha) * self._covariance + alpha * outer
        self._last_sample_prices = prices
        self._last_sample_at = timestamp
        self.sample_count += 1
        return True

    def covariance(
        self,
        symbols: Sequence[str],
        fallback_covariance: Sequence[Sequence[float]],
    ) -> tuple[tuple[tuple[float, ...], ...], str]:
        requested = tuple(_symbol(symbol) for symbol in symbols)
        fallback = _covariance_array(fallback_covariance, len(requested))
        if (
            self._covariance is None
            or self.sample_count < self.min_samples
            or any(symbol not in self.symbols for symbol in requested)
        ):
            return _matrix_tuple(fallback), "STATIC_BOOTSTRAP"
        indices = [self.symbols.index(symbol) for symbol in requested]
        covariance = self._covariance[np.ix_(indices, indices)]
        diagonal = np.diag(np.diag(covariance))
        shrunk = (
            (1.0 - self.diagonal_shrinkage) * covariance
            + self.diagonal_shrinkage * diagonal
        )
        shrunk = _nearest_positive_semidefinite(shrunk, fallback)
        return _matrix_tuple(shrunk), "DYNAMIC_EWMA_SHRUNK"

    def summary(self) -> dict[str, object]:
        return {
            "sample_count": self.sample_count,
            "min_samples": self.min_samples,
            "ready": self.sample_count >= self.min_samples,
            "symbols": list(self.symbols),
        }


@dataclass(slots=True)
class _TradeRateState:
    buy_qty_per_s: float = 0.0
    sell_qty_per_s: float = 0.0
    updated_at_monotonic: float | None = None


@dataclass(frozen=True, slots=True)
class QueueEstimate:
    queue_ahead_qty: float
    service_rate_qty_per_s: float
    expected_delay_s: float
    latency_cost_bps: float


class QueueLatencyEstimator:
    """Estimate L1 queue delay and convert uncertainty into a bps quote cost."""

    def __init__(
        self,
        *,
        rate_ewma_alpha: float = 0.15,
        default_service_rate_qty_per_s: float = 1.0,
        max_queue_delay_s: float = 5.0,
        network_latency_ms: float = 30.0,
        queue_risk_time_weight: float = 0.02,
        confidence_z: float = 1.0,
    ) -> None:
        self.rate_ewma_alpha = _unit_interval(
            rate_ewma_alpha,
            "rate_ewma_alpha",
            open_zero=True,
        )
        self.default_service_rate_qty_per_s = _positive(
            default_service_rate_qty_per_s,
            "default_service_rate_qty_per_s",
        )
        self.max_queue_delay_s = _positive(max_queue_delay_s, "max_queue_delay_s")
        self.network_latency_s = _nonnegative(network_latency_ms, "network_latency_ms") / 1000.0
        self.queue_risk_time_weight = _nonnegative(
            queue_risk_time_weight,
            "queue_risk_time_weight",
        )
        self.confidence_z = _nonnegative(confidence_z, "confidence_z")
        self._states: dict[str, _TradeRateState] = defaultdict(_TradeRateState)

    def record_trade(
        self,
        *,
        symbol: str,
        aggressor_side: Side | str,
        quantity: float,
        observed_at_monotonic: float,
    ) -> None:
        normalized_symbol = _symbol(symbol)
        side = _side(aggressor_side)
        qty = _positive(quantity, "quantity")
        timestamp = _nonnegative(observed_at_monotonic, "observed_at_monotonic")
        state = self._states[normalized_symbol]
        if state.updated_at_monotonic is None:
            instantaneous_rate = self.default_service_rate_qty_per_s
        else:
            elapsed = max(1e-3, timestamp - state.updated_at_monotonic)
            instantaneous_rate = qty / elapsed
        alpha = self.rate_ewma_alpha
        if side == Side.BUY:
            previous = state.buy_qty_per_s or self.default_service_rate_qty_per_s
            state.buy_qty_per_s = (1.0 - alpha) * previous + alpha * instantaneous_rate
        else:
            previous = state.sell_qty_per_s or self.default_service_rate_qty_per_s
            state.sell_qty_per_s = (1.0 - alpha) * previous + alpha * instantaneous_rate
        state.updated_at_monotonic = timestamp

    def estimate(
        self,
        *,
        symbol: str,
        quote_side: Side | str,
        queue_ahead_qty: float,
        sigma_bps_sqrt_s: float,
    ) -> QueueEstimate:
        normalized_symbol = _symbol(symbol)
        side = _side(quote_side)
        queue = _nonnegative(queue_ahead_qty, "queue_ahead_qty")
        sigma = _positive(sigma_bps_sqrt_s, "sigma_bps_sqrt_s")
        state = self._states[normalized_symbol]
        if side == Side.BUY:
            service_rate = state.sell_qty_per_s
        else:
            service_rate = state.buy_qty_per_s
        service_rate = service_rate or self.default_service_rate_qty_per_s
        delay = min(self.max_queue_delay_s, queue / service_rate)
        risk_time = self.network_latency_s + self.queue_risk_time_weight * delay
        latency_cost = self.confidence_z * sigma * math.sqrt(max(0.0, risk_time))
        return QueueEstimate(queue, service_rate, delay, latency_cost)


@dataclass(frozen=True, slots=True)
class SizeOptimizationResult:
    multiplier: float
    expected_utility_bps_lots_per_s: float
    effective_fill_intensity_per_s: float
    evaluated_multipliers: tuple[float, ...]


def optimize_quote_size(
    *,
    side: Side | str,
    candidate_multipliers: Iterable[float],
    base_order_size_lots: float,
    inventory_lots: float,
    depth_bps: float,
    A_per_s: float,
    k_per_bps: float,
    sigma_bps_sqrt_s: float,
    gamma_per_bps: float,
    fee_bps: float,
    adverse_cost_bps: float,
    expected_queue_delay_s: float,
    utility_horizon_s: float = 1.0,
    size_penalty_bps: float = 0.05,
) -> SizeOptimizationResult:
    """Select a bounded size by expected edge minus inventory and size risk."""

    parsed_side = _side(side)
    candidates = tuple(
        sorted(
            {
                _nonnegative(value, "candidate multiplier")
                for value in candidate_multipliers
            }
        )
    )
    if not candidates or candidates[-1] > 1.0 + 1e-12:
        raise ValueError("candidate multipliers must be non-empty and at most one")
    base_size = _positive(base_order_size_lots, "base_order_size_lots")
    inventory = _finite(inventory_lots, "inventory_lots")
    depth = _finite(depth_bps, "depth_bps")
    intensity = _positive(A_per_s, "A_per_s")
    k = _positive(k_per_bps, "k_per_bps")
    sigma = _positive(sigma_bps_sqrt_s, "sigma_bps_sqrt_s")
    gamma = _positive(gamma_per_bps, "gamma_per_bps")
    fee = _nonnegative(fee_bps, "fee_bps")
    adverse = _nonnegative(adverse_cost_bps, "adverse_cost_bps")
    queue_delay = _nonnegative(expected_queue_delay_s, "expected_queue_delay_s")
    horizon = _positive(utility_horizon_s, "utility_horizon_s")
    size_penalty = _nonnegative(size_penalty_bps, "size_penalty_bps")

    raw_fill_intensity = intensity * math.exp(-k * max(0.0, depth))
    effective_fill_intensity = raw_fill_intensity / (
        1.0 + raw_fill_intensity * queue_delay
    )
    signed_inventory_change = 1.0 if parsed_side == Side.BUY else -1.0
    net_edge = depth - fee - adverse
    best_multiplier = candidates[0]
    best_utility = -math.inf
    for multiplier in candidates:
        size_lots = base_size * multiplier
        post_inventory = inventory + signed_inventory_change * size_lots
        inventory_risk = (
            0.5
            * gamma
            * sigma
            * sigma
            * horizon
            * (post_inventory * post_inventory - inventory * inventory)
        )
        per_fill_value = size_lots * net_edge - inventory_risk
        utility = effective_fill_intensity * per_fill_value
        utility -= size_penalty * size_lots * size_lots
        if utility > best_utility:
            best_multiplier = multiplier
            best_utility = utility
    return SizeOptimizationResult(
        multiplier=best_multiplier,
        expected_utility_bps_lots_per_s=best_utility,
        effective_fill_intensity_per_s=effective_fill_intensity,
        evaluated_multipliers=candidates,
    )


def _nearest_positive_semidefinite(
    covariance: np.ndarray,
    fallback: np.ndarray,
) -> np.ndarray:
    symmetric = 0.5 * (covariance + covariance.T)
    eigenvalues, eigenvectors = np.linalg.eigh(symmetric)
    fallback_floor = max(1e-12, float(np.min(np.diag(fallback))) * 1e-6)
    projected = eigenvectors @ np.diag(np.maximum(eigenvalues, fallback_floor)) @ eigenvectors.T
    return 0.5 * (projected + projected.T)


def _covariance_array(values: Sequence[Sequence[float]], size: int) -> np.ndarray:
    try:
        result = np.asarray(values, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError("fallback_covariance must be a finite square matrix") from exc
    if result.shape != (size, size) or not np.isfinite(result).all():
        raise ValueError("fallback_covariance must be a finite square matrix")
    if not np.allclose(result, result.T, rtol=1e-10, atol=1e-10):
        raise ValueError("fallback_covariance must be symmetric")
    if np.any(np.diag(result) <= 0.0):
        raise ValueError("fallback_covariance must have a positive diagonal")
    return 0.5 * (result + result.T)


def _matrix_tuple(values: np.ndarray) -> tuple[tuple[float, ...], ...]:
    return tuple(tuple(float(value) for value in row) for row in values)


def _symbol(value: object) -> str:
    result = str(value or "").strip().upper()
    if not result:
        raise ValueError("symbol must not be empty")
    return result


def _side(value: Side | str) -> Side:
    if isinstance(value, Side):
        return value
    try:
        return Side(str(value or "").strip().upper())
    except ValueError as exc:
        raise ValueError("side must be BUY or SELL") from exc


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field} must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def _positive(value: object, field: str) -> float:
    result = _finite(value, field)
    if result <= 0.0:
        raise ValueError(f"{field} must be positive")
    return result


def _nonnegative(value: object, field: str) -> float:
    result = _finite(value, field)
    if result < 0.0:
        raise ValueError(f"{field} must be nonnegative")
    return result


def _positive_int(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("value must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be a positive integer") from exc
    if result <= 0 or result != value:
        raise ValueError("value must be a positive integer")
    return result


def _unit_interval(value: object, field: str, *, open_zero: bool = False) -> float:
    result = _finite(value, field)
    if result > 1.0 or result < 0.0 or (open_zero and result == 0.0):
        boundary = "(0, 1]" if open_zero else "[0, 1]"
        raise ValueError(f"{field} must be in {boundary}")
    return result
