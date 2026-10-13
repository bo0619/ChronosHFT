"""Per-symbol Paper fill intensity: queue-adjusted prior, own-fill posterior.

GLFT prices quotes from ``lambda(delta) = A * exp(-k * delta)``: the rate at
which *our* quote fills at depth ``delta`` bps from mid. Live takes A and k
from a signed, frozen RPI calibration artifact. Paper previously used one
configured A and k for every symbol, which can be wrong by orders of
magnitude (KORUUSDT's own-fill A was ~25x below the configured 10/s).

Paper now estimates A and k per symbol:

* Prior. A quote at the back of an L1 queue of ``Q`` contracts fills when
  ``Q`` has traded, so its intensity at the touch is about ``mu / Q`` for a
  traded volume rate ``mu`` of the aggressors that consume that side. With
  the prior ``k`` this gives ``A = (mu / Q) * exp(k * touch_depth)``. The
  Paper venue's own matching works the same way (queue ahead drains only by
  trades), so the prior is consistent with how Paper fills are produced.
  Until ``mu`` has been observed the configured ``prior_A_per_s`` is used.
* Posterior. Every own quote is tracked from acknowledgement to terminal as
  piecewise-constant depth exposure with its fills
  (``alpha.rpi_intensity.RPIOrderExposure``). Given ``k``, A has a
  Gamma-Poisson posterior: a Gamma prior worth ``prior_strength_fills``
  fills, updated by ``n`` fills over depth-weighted exposure ``E``, has mean
  ``(alpha0 + n) / (alpha0 / A0 + E)``. ``k`` moves from the prior to the
  own-fill maximum-likelihood ``k`` (``estimate_rpi_intensity``) with weight
  ``n / (n + prior_strength_fills)`` once the exposure spans enough depths.

Own fills are the estimand GLFT needs; the prior only carries the estimate
until they accumulate. Neither is Live RPI evidence: Paper fills come from
the public-trade proxy.

Every setting can be overridden per symbol under ``symbols.<SYMBOL>``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from typing import Any

from alpha.rpi_intensity import (
    RPIIntensityAccumulator,
    RPIIntensityRequirements,
    RPIOrderExposure,
)
from event.type import OrderStatus, Side

TERMINAL_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    }
)
LIVE_STATUSES = frozenset({OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED})


@dataclass(frozen=True, slots=True)
class PaperIntensityConfig:
    enabled: bool = False
    prior_A_per_s: float = 10.0
    prior_k_per_bps: float = 0.8
    prior_strength_fills: float = 10.0
    min_A_per_s: float = 1e-4
    max_A_per_s: float = 1_000.0
    min_k_per_bps: float = 0.01
    max_k_per_bps: float = 10.0
    min_fills_for_k: int = 10
    min_depth_levels_for_k: int = 2
    min_depth_span_bps_for_k: float = 1.0
    depth_bin_width_bps: float = 0.5
    estimate_interval_s: float = 1.0

    @classmethod
    def for_symbol(cls, section: object, symbol: str) -> "PaperIntensityConfig":
        """Global settings, then ``symbols.<SYMBOL>`` overrides."""
        section = section if isinstance(section, Mapping) else {}
        merged = {key: value for key, value in section.items() if key != "symbols"}
        overrides = section.get("symbols")
        if isinstance(overrides, Mapping):
            symbol_section = overrides.get(str(symbol or "").upper())
            if isinstance(symbol_section, Mapping):
                merged.update(symbol_section)
        known = {item.name for item in fields(cls)}
        config = replace(
            cls(),
            **{key: value for key, value in merged.items() if key in known},
        )
        config._validate()
        return config

    def _validate(self) -> None:
        for name in (
            "prior_A_per_s",
            "prior_k_per_bps",
            "prior_strength_fills",
            "min_A_per_s",
            "max_A_per_s",
            "min_k_per_bps",
            "max_k_per_bps",
            "min_depth_span_bps_for_k",
            "depth_bin_width_bps",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"paper_intensity.{name} must be positive")
        if self.max_A_per_s <= self.min_A_per_s:
            raise ValueError("paper_intensity.max_A_per_s must exceed min_A_per_s")
        if self.max_k_per_bps <= self.min_k_per_bps:
            raise ValueError(
                "paper_intensity.max_k_per_bps must exceed min_k_per_bps"
            )
        if int(self.min_fills_for_k) < 1 or int(self.min_depth_levels_for_k) < 2:
            raise ValueError(
                "paper_intensity needs min_fills_for_k >= 1 and "
                "min_depth_levels_for_k >= 2"
            )
        if not math.isfinite(float(self.estimate_interval_s)) or (
            self.estimate_interval_s < 0.0
        ):
            raise ValueError("paper_intensity.estimate_interval_s must be >= 0")


@dataclass(frozen=True, slots=True)
class PaperIntensity:
    A_per_s: float
    k_per_bps: float
    source: str
    prior_A_per_s: float
    prior_k_per_bps: float
    prior_source: str
    fill_count: int
    exposure_seconds: float
    k_weight: float

    def as_dict(self) -> dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in fields(self)}


@dataclass(slots=True)
class _OpenQuote:
    side: Side
    price: float
    exposure: RPIOrderExposure
    filled_volume: float = 0.0


def _depth_bps(side: Side, price: float, mid: float) -> float:
    if not (mid > 0.0 and price > 0.0):
        return math.nan
    ratio = mid / price if side == Side.BUY else price / mid
    return math.log(ratio) * 10_000.0


def _positive_time(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0.0 else None


class PaperIntensityTracker:
    """Own-quote exposure and the per-symbol A/k estimate for one symbol."""

    def __init__(self, config: PaperIntensityConfig) -> None:
        self.config = config
        self._open: dict[str, _OpenQuote] = {}
        self._accumulator = RPIIntensityAccumulator()
        self._last_mid = 0.0
        self._last_book_at = 0.0
        self._k_requirements = RPIIntensityRequirements(
            min_sample_count=1,
            min_depth_level_count=int(config.min_depth_levels_for_k),
            min_total_exposure_seconds=1e-9,
            min_fill_count=int(config.min_fills_for_k),
            min_depth_span_bps=float(config.min_depth_span_bps_for_k),
            min_k_per_bps=float(config.min_k_per_bps),
            max_k_per_bps=float(config.max_k_per_bps),
        )
        self._own_k: tuple[float | None, int] = (None, 0)
        self._own_k_at = -math.inf
        self.last: PaperIntensity | None = None

    # Observation -----------------------------------------------------------

    def on_book(self, mid: float, at_monotonic: float) -> None:
        at = _positive_time(at_monotonic)
        if at is None or not mid > 0.0 or at < self._last_book_at:
            return
        self._last_mid = float(mid)
        self._last_book_at = at
        for client_oid, quote in list(self._open.items()):
            quote.exposure.observe_depth(
                observed_at_monotonic=at,
                depth_bps=_depth_bps(quote.side, quote.price, mid),
            )
            if quote.exposure.censored:
                self._open.pop(client_oid, None)

    def on_order(self, snapshot, side: Side | None) -> None:
        client_oid = str(getattr(snapshot, "client_oid", "") or "")
        status = getattr(snapshot, "status", None)
        if not client_oid:
            return
        at = _positive_time(getattr(snapshot, "updated_monotonic", 0.0))
        quote = self._open.get(client_oid)
        if quote is None and status in LIVE_STATUSES:
            quote = self._start(client_oid, snapshot, side, at)
        if quote is None:
            return
        filled = float(getattr(snapshot, "filled_volume", 0.0) or 0.0)
        if filled > quote.filled_volume + 1e-12 and at is not None:
            if at > quote.exposure.last_observation_monotonic:
                quote.exposure.observe_depth(
                    observed_at_monotonic=at,
                    depth_bps=_depth_bps(quote.side, quote.price, self._last_mid),
                )
            quote.exposure.record_fill(f"{client_oid}:{filled:.12g}")
            quote.filled_volume = filled
        if status in TERMINAL_STATUSES or status == OrderStatus.REJECTED_LOCALLY:
            self._open.pop(client_oid, None)
            result = quote.exposure.finish(
                terminal_at_monotonic=at or quote.exposure.last_observation_monotonic
            )
            for exposure_bin in result.exposure_bins:
                self._accumulator.add_acked_bin(exposure_bin)

    def _start(self, client_oid, snapshot, side, at) -> _OpenQuote | None:
        price = float(getattr(snapshot, "price", 0.0) or 0.0)
        if side not in (Side.BUY, Side.SELL) or not price > 0.0:
            return None
        if not self._last_mid > 0.0 or at is None:
            return None
        start = max(at, self._last_book_at)
        try:
            exposure = RPIOrderExposure(
                acknowledged_at_monotonic=start,
                initial_depth_bps=_depth_bps(side, price, self._last_mid),
                depth_bin_width_bps=self.config.depth_bin_width_bps,
            )
        except ValueError:
            return None
        if exposure.censored:
            return None
        quote = _OpenQuote(side=side, price=price, exposure=exposure)
        self._open[client_oid] = quote
        return quote

    # Estimation ------------------------------------------------------------

    def prior(
        self,
        *,
        bid_queue_qty: float,
        ask_queue_qty: float,
        bid_service_rate: float | None,
        ask_service_rate: float | None,
        bid_touch_depth_bps: float,
        ask_touch_depth_bps: float,
    ) -> tuple[float, str]:
        """Prior A from queue-adjusted public flow, else the configured A."""
        k = self.config.prior_k_per_bps
        sides = []
        for queue, rate, depth in (
            (bid_queue_qty, bid_service_rate, bid_touch_depth_bps),
            (ask_queue_qty, ask_service_rate, ask_touch_depth_bps),
        ):
            if rate and rate > 0.0 and queue > 0.0 and math.isfinite(depth):
                sides.append(math.log(rate / queue) + k * max(0.0, depth))
        if not sides:
            return self.config.prior_A_per_s, "PAPER_PRIOR_CONFIG"
        # Geometric mean of the two sides' touch-implied A.
        return math.exp(sum(sides) / len(sides)), "PAPER_PRIOR_QUEUE"

    def resolve(self, *, prior_A_per_s: float, prior_source: str, now: float) -> PaperIntensity:
        config = self.config
        prior_A = min(max(prior_A_per_s, config.min_A_per_s), config.max_A_per_s)
        prior_k = config.prior_k_per_bps
        bins = self._accumulator.snapshot_bins()
        fill_count = sum(item.fill_count for item in bins)
        exposure_seconds = math.fsum(item.exposure_seconds for item in bins)

        own_k, own_k_fills = self._own_k_estimate(now)
        weight = 0.0
        k = prior_k
        if own_k is not None:
            weight = own_k_fills / (own_k_fills + config.prior_strength_fills)
            k = math.exp(weight * math.log(own_k) + (1.0 - weight) * math.log(prior_k))
        k = min(max(k, config.min_k_per_bps), config.max_k_per_bps)

        # Gamma(alpha0, alpha0 / A0) prior on A, Poisson fills given k.
        alpha0 = config.prior_strength_fills
        weighted_exposure = math.fsum(
            item.exposure_seconds * math.exp(-k * item.depth_bps) for item in bins
        )
        A = (alpha0 + fill_count) / (alpha0 / prior_A + weighted_exposure)
        A = min(max(A, config.min_A_per_s), config.max_A_per_s)

        self.last = PaperIntensity(
            A_per_s=A,
            k_per_bps=k,
            source="PAPER_POSTERIOR" if bins else prior_source,
            prior_A_per_s=prior_A,
            prior_k_per_bps=prior_k,
            prior_source=prior_source,
            fill_count=fill_count,
            exposure_seconds=exposure_seconds,
            k_weight=weight,
        )
        return self.last

    def _own_k_estimate(self, now: float) -> tuple[float | None, int]:
        if now - self._own_k_at >= self.config.estimate_interval_s:
            estimate = self._accumulator.estimate(self._k_requirements)
            k = estimate.k_per_bps if estimate.ready else None
            self._own_k = (
                float(k) if k is not None and k > 0.0 else None,
                int(estimate.fill_count or 0),
            )
            self._own_k_at = now
        return self._own_k


__all__ = [
    "PaperIntensity",
    "PaperIntensityConfig",
    "PaperIntensityTracker",
]
