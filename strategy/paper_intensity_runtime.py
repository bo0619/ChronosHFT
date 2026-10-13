"""Choose GLFT's A and k: the frozen Live artifact, or the Paper estimate.

The per-symbol Paper estimator (``alpha.paper_intensity``) lives on that
symbol's ``GLFTCalibrator`` so the strategy owns no extra state. It is
created on the first quote cycle, sees every book through the calibrator,
and every own order through :func:`record_paper_intensity_order`.
"""

from __future__ import annotations

import math

from alpha.paper_intensity import PaperIntensityConfig, PaperIntensityTracker
from event.type import Side

APPROVED_SOURCE = "APPROVED_RPI_ARTIFACT_FROZEN"
CONFIGURED_SOURCE = "CONFIGURED_PAPER_PROXY"


def paper_intensity_tracker(strategy, symbol: str, calibrator):
    """Return the symbol's Paper tracker, creating it once; None if disabled."""
    tracker = getattr(calibrator, "paper_intensity", None)
    if tracker is None:
        section = strategy.strat_conf.get(
            "paper_intensity",
            strategy.glft_conf.get("paper_intensity"),
        )
        config = PaperIntensityConfig.for_symbol(section, symbol)
        tracker = PaperIntensityTracker(config) if config.enabled else False
        calibrator.paper_intensity = tracker
    return tracker or None


def resolve_glft_intensity(
    strategy,
    symbol: str,
    calibrator,
    approved_intensity,
    *,
    bid_1: float,
    ask_1: float,
    bid_1_volume: float,
    ask_1_volume: float,
    now: float,
) -> tuple[float, float, str]:
    if strategy.live_mode:
        return (
            approved_intensity.A_per_s,
            approved_intensity.k_per_bps,
            APPROVED_SOURCE,
        )
    tracker = paper_intensity_tracker(strategy, symbol, calibrator)
    if tracker is None:
        return float(calibrator.A), float(calibrator.k), CONFIGURED_SOURCE

    queue = getattr(strategy, "adaptive_queue", None)

    def service_rate(side: Side) -> float | None:
        if queue is None:
            return None
        return queue.observed_service_rate(
            symbol=symbol,
            quote_side=side,
            now_monotonic=now,
        )

    mid = (bid_1 + ask_1) / 2.0
    prior_A, prior_source = tracker.prior(
        bid_queue_qty=max(0.0, float(bid_1_volume or 0.0)),
        ask_queue_qty=max(0.0, float(ask_1_volume or 0.0)),
        bid_service_rate=service_rate(Side.BUY),
        ask_service_rate=service_rate(Side.SELL),
        bid_touch_depth_bps=math.log(mid / bid_1) * 10_000.0,
        ask_touch_depth_bps=math.log(ask_1 / mid) * 10_000.0,
    )
    estimate = tracker.resolve(
        prior_A_per_s=prior_A,
        prior_source=prior_source,
        now=now,
    )
    return estimate.A_per_s, estimate.k_per_bps, estimate.source


def record_paper_intensity_order(strategy, snapshot) -> None:
    calibrator = strategy.calibrators.get(snapshot.symbol)
    tracker = getattr(calibrator, "paper_intensity", None)
    if not tracker:
        return
    intent = strategy.active_orders.get(snapshot.client_oid)
    tracker.on_order(snapshot, strategy._snapshot_side(snapshot, intent))


__all__ = [
    "paper_intensity_tracker",
    "record_paper_intensity_order",
    "resolve_glft_intensity",
]
