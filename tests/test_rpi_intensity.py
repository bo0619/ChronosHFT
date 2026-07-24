import math

import pytest

from alpha.rpi_intensity import (
    RPIExposureBin,
    RPIIntensityAccumulator,
    RPIIntensityRequirements,
    RPIOrderExposure,
    estimate_rpi_intensity,
)


def _requirements(**overrides):
    values = {
        "min_sample_count": 3,
        "min_depth_level_count": 3,
        "min_total_exposure_seconds": 1.0,
        "min_fill_count": 1,
        "min_depth_span_bps": 0.5,
        "min_k_per_bps": 1e-8,
        "max_k_per_bps": 100.0,
    }
    values.update(overrides)
    return RPIIntensityRequirements(**values)


def test_profile_poisson_fit_recovers_exponential_intensity():
    estimate = estimate_rpi_intensity(
        (
            RPIExposureBin(0.0, 100.0, 200, 10),
            RPIExposureBin(1.0, 100.0, 121, 10),
            RPIExposureBin(2.0, 100.0, 74, 10),
            RPIExposureBin(3.0, 100.0, 45, 10),
        ),
        requirements=_requirements(),
    )

    assert estimate.ready
    assert estimate.state == "READY"
    assert estimate.A_per_s == pytest.approx(2.0, rel=0.01)
    assert estimate.k_per_bps == pytest.approx(0.5, rel=0.02)
    assert estimate.sample_count == 40
    assert estimate.total_exposure_seconds == 400.0
    assert estimate.fill_count == 440
    assert estimate.log_likelihood is not None
    assert math.isfinite(estimate.log_likelihood)


def test_zero_fill_exposure_is_kept_in_poisson_likelihood():
    common_bins = (
        RPIExposureBin(0.0, 100.0, 100),
        RPIExposureBin(1.0, 100.0, 50),
        RPIExposureBin(2.0, 100.0, 25),
    )
    without_zero = estimate_rpi_intensity(
        common_bins,
        requirements=_requirements(),
    )
    with_zero = estimate_rpi_intensity(
        (*common_bins, RPIExposureBin(3.0, 100.0, 0)),
        requirements=_requirements(),
    )

    assert without_zero.ready
    assert with_zero.ready
    assert with_zero.zero_fill_depth_level_count == 1
    assert with_zero.zero_fill_exposure_seconds == 100.0
    assert with_zero.total_exposure_seconds == 400.0
    assert with_zero.k_per_bps > without_zero.k_per_bps


def test_accumulator_measures_only_positive_ack_to_terminal_intervals():
    accumulator = RPIIntensityAccumulator()

    assert accumulator.add_acked_interval(
        depth_bps=1.0,
        acknowledged_at_seconds=10.0,
        ended_at_seconds=12.5,
        fill_count=0,
    )
    assert not accumulator.add_acked_interval(
        depth_bps=1.0,
        acknowledged_at_seconds=12.5,
        ended_at_seconds=12.5,
        fill_count=0,
    )

    assert accumulator.snapshot_bins() == (
        RPIExposureBin(
            depth_bps=1.0,
            exposure_seconds=2.5,
            fill_count=0,
            sample_count=1,
        ),
    )
    estimate = accumulator.estimate(_requirements())
    assert not estimate.ready
    assert estimate.state == "INVALID_DATA"
    assert estimate.invalid_sample_count == 1


def test_order_exposure_integrates_contemporaneous_depth_and_fill_bin():
    exposure = RPIOrderExposure(
        acknowledged_at_monotonic=10.0,
        initial_depth_bps=0.11,
        depth_bin_width_bps=0.25,
    )

    assert exposure.observe_depth(
        observed_at_monotonic=11.0,
        depth_bps=0.39,
    )
    assert exposure.record_fill("trade-1")
    assert not exposure.record_fill("trade-1")
    assert exposure.observe_depth(
        observed_at_monotonic=13.0,
        depth_bps=0.64,
    )
    assert exposure.record_fill("trade-2")
    result = exposure.finish(terminal_at_monotonic=14.0)

    assert not result.censored
    assert result.fill_count == 2
    assert result.exposure_bins == (
        RPIExposureBin(0.0, 1.0, 0, 1),
        RPIExposureBin(0.5, 2.0, 1, 1),
        RPIExposureBin(0.75, 1.0, 1, 1),
    )


def test_censored_order_is_dropped_without_tainting_intensity_accumulator():
    exposure = RPIOrderExposure(
        acknowledged_at_monotonic=10.0,
        initial_depth_bps=None,
        depth_bin_width_bps=0.25,
        censor_reason="missing_pre_ack_book",
    )
    result = exposure.finish(terminal_at_monotonic=12.0)
    accumulator = RPIIntensityAccumulator()
    for exposure_bin in result.exposure_bins:
        accumulator.add_acked_bin(exposure_bin)

    assert result.censored
    assert result.censor_reason == "missing_pre_ack_book"
    assert result.exposure_bins == ()
    assert accumulator.invalid_sample_count == 0
    estimate = accumulator.estimate(_requirements())
    assert estimate.state == "WARMING_UP"


def test_order_exposure_censors_non_monotonic_book_without_invalid_bin():
    exposure = RPIOrderExposure(
        acknowledged_at_monotonic=10.0,
        initial_depth_bps=1.0,
        depth_bin_width_bps=0.5,
    )

    assert not exposure.observe_depth(
        observed_at_monotonic=9.0,
        depth_bps=1.0,
    )
    result = exposure.finish(terminal_at_monotonic=12.0)

    assert result.censored
    assert result.censor_reason == "non_monotonic_book_time"
    assert result.exposure_bins == ()


def test_accumulator_preserves_preaggregated_sample_count():
    accumulator = RPIIntensityAccumulator()

    assert accumulator.add_acked_bin(RPIExposureBin(1.0, 2.0, 1, 4))
    assert accumulator.add_acked_bin(RPIExposureBin(1.0, 3.0, 2, 5))

    assert accumulator.snapshot_bins() == (
        RPIExposureBin(1.0, 5.0, 3, 9),
    )


def test_invalid_values_taint_result_instead_of_being_silently_dropped():
    accumulator = RPIIntensityAccumulator()
    for depth, fills in ((0.0, 10), (1.0, 5), (2.0, 2)):
        assert accumulator.add_acked_exposure(
            depth_bps=depth,
            exposure_seconds=10.0,
            fill_count=fills,
        )

    assert not accumulator.add_acked_exposure(
        depth_bps=float("nan"),
        exposure_seconds=10.0,
        fill_count=0,
    )
    assert not accumulator.add_acked_exposure(
        depth_bps=3.0,
        exposure_seconds=-1.0,
        fill_count=0,
    )
    assert not accumulator.add_acked_exposure(
        depth_bps=3.0,
        exposure_seconds=10.0,
        fill_count=-1,
    )

    estimate = accumulator.estimate(_requirements())
    assert not estimate.ready
    assert estimate.state == "INVALID_DATA"
    assert estimate.A_per_s is None
    assert estimate.k_per_bps is None
    assert estimate.invalid_sample_count == 3
    assert estimate.sample_count == 3
    assert estimate.fill_count == 17
    assert estimate.reasons[0].startswith("invalid_samples:3")


@pytest.mark.parametrize(
    "invalid_bin",
    [
        RPIExposureBin(float("inf"), 10.0, 0),
        RPIExposureBin(-1.0, 10.0, 0),
        RPIExposureBin(1.0, float("nan"), 0),
        RPIExposureBin(1.0, -10.0, 0),
        RPIExposureBin(1.0, 10.0, -1),
        RPIExposureBin(1.0, 10.0, 0, 0),
    ],
)
def test_aggregate_input_rejects_non_finite_and_negative_values(invalid_bin):
    estimate = estimate_rpi_intensity(
        (
            RPIExposureBin(0.0, 10.0, 10),
            RPIExposureBin(1.0, 10.0, 5),
            RPIExposureBin(2.0, 10.0, 2),
            invalid_bin,
        ),
        requirements=_requirements(),
    )

    assert not estimate.ready
    assert estimate.state == "INVALID_DATA"
    assert estimate.A_per_s is None
    assert estimate.k_per_bps is None
    assert estimate.invalid_sample_count >= 1


def test_readiness_thresholds_fail_closed_before_fit():
    estimate = estimate_rpi_intensity(
        (
            RPIExposureBin(0.0, 1.0, 2),
            RPIExposureBin(1.0, 1.0, 1),
            RPIExposureBin(2.0, 1.0, 0),
        ),
        requirements=_requirements(
            min_sample_count=10,
            min_total_exposure_seconds=20.0,
            min_fill_count=10,
        ),
    )

    assert not estimate.ready
    assert estimate.state == "WARMING_UP"
    assert estimate.A_per_s is None
    assert estimate.k_per_bps is None
    assert "sample_count:3<10" in estimate.reasons
    assert "total_exposure_seconds:3<20" in estimate.reasons
    assert "fill_count:3<10" in estimate.reasons


def test_non_decaying_counts_fail_closed():
    estimate = estimate_rpi_intensity(
        (
            RPIExposureBin(0.0, 100.0, 10),
            RPIExposureBin(1.0, 100.0, 20),
            RPIExposureBin(2.0, 100.0, 30),
        ),
        requirements=_requirements(),
    )

    assert not estimate.ready
    assert estimate.state == "FIT_FAILED"
    assert estimate.A_per_s is None
    assert estimate.k_per_bps is None
    assert estimate.reasons == ("intensity_slope:not_strictly_decaying",)


def test_complete_separation_has_no_finite_usable_slope():
    estimate = estimate_rpi_intensity(
        (
            RPIExposureBin(0.0, 100.0, 50),
            RPIExposureBin(1.0, 100.0, 0),
            RPIExposureBin(2.0, 100.0, 0),
        ),
        requirements=_requirements(),
    )

    assert not estimate.ready
    assert estimate.state == "FIT_FAILED"
    assert estimate.A_per_s is None
    assert estimate.k_per_bps is None
    assert estimate.reasons == ("k_per_bps:unbounded_or_above_100",)


def test_log_sum_exp_fit_remains_stable_when_naive_products_underflow():
    estimate = estimate_rpi_intensity(
        (
            RPIExposureBin(1000.0, 1e200, 100),
            RPIExposureBin(1001.0, 1e200, 37),
            RPIExposureBin(1002.0, 1e200, 14),
        ),
        requirements=_requirements(),
    )

    assert estimate.ready
    assert estimate.A_per_s is not None
    assert math.isfinite(estimate.A_per_s)
    assert estimate.A_per_s > 1e200
    assert estimate.k_per_bps == pytest.approx(1.0, rel=0.08)
    assert estimate.log_likelihood is not None
    assert math.isfinite(estimate.log_likelihood)


def test_non_finite_total_exposure_is_invalid_data():
    estimate = estimate_rpi_intensity(
        (
            RPIExposureBin(0.0, 1e308, 100),
            RPIExposureBin(1.0, 1e308, 50),
            RPIExposureBin(2.0, 1e308, 25),
        ),
        requirements=_requirements(),
    )

    assert not estimate.ready
    assert estimate.state == "INVALID_DATA"
    assert estimate.A_per_s is None
    assert estimate.k_per_bps is None
    assert estimate.total_exposure_seconds == math.inf
    assert "total_exposure_seconds:not_finite" in estimate.reasons


def test_result_exports_named_units_and_aggregated_counts():
    estimate = estimate_rpi_intensity(
        (
            RPIExposureBin(0.0, 10.0, 10, 2),
            RPIExposureBin(1.0, 10.0, 5, 3),
            RPIExposureBin(2.0, 10.0, 2, 4),
        ),
        requirements=_requirements(),
    )

    payload = estimate.as_dict()
    assert payload["A_per_s"] == estimate.A_per_s
    assert payload["k_per_bps"] == estimate.k_per_bps
    assert payload["sample_count"] == 9
    assert payload["total_exposure_seconds"] == 30.0
    assert payload["fill_count"] == 17
    assert payload["depth_level_count"] == 3


def test_invalid_readiness_requirements_are_rejected():
    with pytest.raises(ValueError, match="min_total_exposure_seconds"):
        RPIIntensityRequirements(min_total_exposure_seconds=float("nan"))
    with pytest.raises(ValueError, match="max_k_per_bps"):
        RPIIntensityRequirements(
            min_k_per_bps=1.0,
            max_k_per_bps=1.0,
        )
