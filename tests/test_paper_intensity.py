import math
from types import SimpleNamespace

import pytest

from alpha.glft_adaptive import (
    PUBLIC_RATE_MIN_OBSERVED_S,
    QueueLatencyEstimator,
)
from alpha.paper_intensity import PaperIntensityConfig, PaperIntensityTracker
from event.type import OrderStatus, Side
from strategy.paper_intensity_runtime import resolve_glft_intensity


def config(**overrides):
    return PaperIntensityConfig.for_symbol({"enabled": True, **overrides}, "KORUUSDT")


def snapshot(client_oid, status, *, at, price=14.37, filled=0.0, side=Side.BUY):
    return SimpleNamespace(
        client_oid=client_oid,
        status=status,
        price=price,
        filled_volume=filled,
        updated_monotonic=at,
        side=side,
    )


def test_symbol_overrides_replace_global_values_only_for_that_symbol():
    section = {
        "enabled": True,
        "prior_k_per_bps": 1.0,
        "symbols": {"KORUUSDT": {"prior_k_per_bps": 1.2, "prior_A_per_s": 0.4}},
    }
    koru = PaperIntensityConfig.for_symbol(section, "KORUUSDT")
    other = PaperIntensityConfig.for_symbol(section, "SOXLUSDT")

    assert (koru.prior_k_per_bps, koru.prior_A_per_s) == (1.2, 0.4)
    assert other.prior_k_per_bps == 1.0
    assert other.prior_A_per_s == PaperIntensityConfig().prior_A_per_s


def test_invalid_bounds_are_rejected():
    with pytest.raises(ValueError):
        config(min_A_per_s=5.0, max_A_per_s=1.0)


def test_prior_is_queue_adjusted_public_rate_at_the_touch():
    tracker = PaperIntensityTracker(config(prior_k_per_bps=1.0))
    # Both sides: 8,000 contracts ahead, 4 contracts/s traded into them,
    # touch 3.48 bps from mid.
    prior_A, source = tracker.prior(
        bid_queue_qty=8000.0,
        ask_queue_qty=8000.0,
        bid_service_rate=4.0,
        ask_service_rate=4.0,
        bid_touch_depth_bps=3.48,
        ask_touch_depth_bps=3.48,
    )
    assert source == "PAPER_PRIOR_QUEUE"
    # lambda(touch) = 4 / 8000 per second, so A = lambda * exp(k * depth).
    assert prior_A == pytest.approx(4.0 / 8000.0 * math.exp(3.48))


def test_prior_falls_back_to_config_until_the_rate_is_observed():
    tracker = PaperIntensityTracker(config(prior_A_per_s=0.7))
    assert tracker.prior(
        bid_queue_qty=8000.0,
        ask_queue_qty=8000.0,
        bid_service_rate=None,
        ask_service_rate=None,
        bid_touch_depth_bps=3.48,
        ask_touch_depth_bps=3.48,
    ) == (0.7, "PAPER_PRIOR_CONFIG")


def test_without_own_exposure_the_prior_is_used():
    tracker = PaperIntensityTracker(config(prior_k_per_bps=1.0))
    estimate = tracker.resolve(prior_A_per_s=0.5, prior_source="PAPER_PRIOR_QUEUE", now=1.0)
    assert (estimate.A_per_s, estimate.k_per_bps) == pytest.approx((0.5, 1.0))
    assert estimate.source == "PAPER_PRIOR_QUEUE"


def test_own_fills_update_A_with_a_gamma_poisson_posterior():
    tracker = PaperIntensityTracker(
        config(prior_k_per_bps=1.0, prior_strength_fills=10.0, depth_bin_width_bps=0.5)
    )
    tracker.on_book(14.375, 100.0)
    # A bid at 14.37 is ~3.48 bps deep (bin 3.5). It rests 100 s and fills.
    tracker.on_order(snapshot("q1", OrderStatus.NEW, at=100.0), Side.BUY)
    tracker.on_book(14.375, 200.0)
    tracker.on_order(
        snapshot("q1", OrderStatus.FILLED, at=200.0, filled=34.8), Side.BUY
    )

    estimate = tracker.resolve(prior_A_per_s=0.5, prior_source="PAPER_PRIOR_QUEUE", now=200.0)

    weighted_exposure = 100.0 * math.exp(-1.0 * 3.5)
    expected_A = (10.0 + 1) / (10.0 / 0.5 + weighted_exposure)
    assert estimate.source == "PAPER_POSTERIOR"
    assert estimate.fill_count == 1
    assert estimate.exposure_seconds == pytest.approx(100.0)
    assert estimate.A_per_s == pytest.approx(expected_A)
    # One fill at one depth cannot identify k: it stays at the prior.
    assert estimate.k_per_bps == pytest.approx(1.0)
    assert estimate.k_weight == 0.0


def test_unfilled_exposure_lowers_A_below_the_prior():
    tracker = PaperIntensityTracker(config(prior_k_per_bps=1.0))
    tracker.on_book(14.375, 100.0)
    tracker.on_order(snapshot("q1", OrderStatus.NEW, at=100.0), Side.BUY)
    tracker.on_book(14.375, 1100.0)
    tracker.on_order(snapshot("q1", OrderStatus.CANCELLED, at=1100.0), Side.BUY)

    estimate = tracker.resolve(prior_A_per_s=10.0, prior_source="PAPER_PRIOR_CONFIG", now=1100.0)

    assert estimate.fill_count == 0
    assert estimate.A_per_s < 1.0


def test_k_moves_to_the_own_fill_estimate_when_fills_span_depths():
    tracker = PaperIntensityTracker(
        config(prior_k_per_bps=1.0, min_fills_for_k=10, prior_strength_fills=10.0)
    )
    tracker.on_book(14.375, 1.0)
    at = 1.0
    # True k = 0.5: quotes 1 tick (3.5 bps) and 2 ticks (10.4 bps) deep fill
    # at exp(-0.5 * depth) relative rates.
    for index in range(400):
        price, depth = (14.37, 3.48) if index % 2 else (14.36, 10.43)
        oid = f"q{index}"
        tracker.on_order(snapshot(oid, OrderStatus.NEW, at=at, price=price), Side.BUY)
        at += 10.0
        tracker.on_book(14.375, at)
        filled = (index // 2) % round(math.exp(0.5 * depth) / math.exp(0.5 * 3.48)) == 0
        tracker.on_order(
            snapshot(
                oid,
                OrderStatus.FILLED if filled else OrderStatus.CANCELLED,
                at=at,
                price=price,
                filled=1.0 if filled else 0.0,
            ),
            Side.BUY,
        )
    estimate = tracker.resolve(prior_A_per_s=1.0, prior_source="PAPER_PRIOR_CONFIG", now=at)

    assert estimate.fill_count > 100
    assert estimate.k_weight > 0.9
    assert estimate.k_per_bps == pytest.approx(0.5, rel=0.1)


def test_runtime_uses_frozen_artifact_live_and_config_when_disabled():
    approved = SimpleNamespace(A_per_s=2.0, k_per_bps=0.3)
    calibrator = SimpleNamespace(A=10.0, k=0.8)
    live = SimpleNamespace(live_mode=True)
    assert resolve_glft_intensity(
        live, "KORUUSDT", calibrator, approved,
        bid_1=14.37, ask_1=14.38, bid_1_volume=1.0, ask_1_volume=1.0, now=1.0,
    ) == (2.0, 0.3, "APPROVED_RPI_ARTIFACT_FROZEN")

    disabled = SimpleNamespace(
        live_mode=False, strat_conf={}, glft_conf={"paper_intensity": {"enabled": False}}
    )
    calibrator = SimpleNamespace(A=10.0, k=0.8, paper_intensity=None)
    assert resolve_glft_intensity(
        disabled, "KORUUSDT", calibrator, approved,
        bid_1=14.37, ask_1=14.38, bid_1_volume=1.0, ask_1_volume=1.0, now=1.0,
    ) == (10.0, 0.8, "CONFIGURED_PAPER_PROXY")


def test_runtime_builds_a_queue_prior_from_observed_public_trades():
    queue = QueueLatencyEstimator()
    for index in range(40):
        for aggressor in (Side.BUY, Side.SELL):
            queue.record_trade(
                symbol="KORUUSDT",
                aggressor_side=aggressor,
                quantity=2.0,
                observed_at_monotonic=100.0 + index * 0.5,
            )
    strategy = SimpleNamespace(
        live_mode=False,
        strat_conf={},
        glft_conf={"paper_intensity": {"enabled": True, "prior_k_per_bps": 1.0}},
        adaptive_queue=queue,
    )
    calibrator = SimpleNamespace(A=10.0, k=0.8, paper_intensity=None)
    A, k, source = resolve_glft_intensity(
        strategy, "KORUUSDT", calibrator, None,
        bid_1=14.37, ask_1=14.38, bid_1_volume=8000.0, ask_1_volume=8000.0,
        now=100.0 + PUBLIC_RATE_MIN_OBSERVED_S + 10.0,
    )
    assert source == "PAPER_PRIOR_QUEUE"
    assert k == 1.0
    # About 4 contracts/s against 8,000 ahead, ~3.48 bps from mid.
    assert A == pytest.approx(4.0 / 8000.0 * math.exp(3.48), rel=0.25)
    assert isinstance(calibrator.paper_intensity, PaperIntensityTracker)
