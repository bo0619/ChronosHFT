import math

import pytest

from scripts.benchmark_runtime_hot_paths import run_benchmarks


def test_runtime_hot_path_benchmark_is_bounded_offline_and_machine_readable():
    report = run_benchmarks(
        iterations=2,
        repeats=1,
        book_levels=20,
        book_event_rate_hz=10.0,
        quote_cycle_sec=0.5,
        rust_threshold_core_percent=100.0,
    )

    assert report["schema"] == "chronoshft.runtime_hot_paths.v1"
    assert report["decision"]["rust_recommended"] is False
    assert set(report["results"]) == {
        "orderbook_delta",
        "robust_as_cycle",
        "robust_glft_cycle",
    }
    for result in report["results"].values():
        assert result["iterations_per_repeat"] == 2
        assert result["repeats"] == 1
        assert math.isfinite(result["median_ns_per_call"])
        assert result["median_ns_per_call"] > 0.0
        assert result["max_to_min_ratio"] >= 1.0
        assert result["stable"] is True
        assert math.isfinite(result["estimated_one_core_percent"])
        assert result["estimated_one_core_percent"] >= 0.0


@pytest.mark.parametrize(
    "overrides",
    (
        {"iterations": 0},
        {"repeats": 0},
        {"book_levels": 0},
        {"book_levels": 50_001},
        {"book_event_rate_hz": float("nan")},
        {"quote_cycle_sec": 0.0},
        {"rust_threshold_core_percent": -1.0},
    ),
)
def test_runtime_hot_path_benchmark_rejects_invalid_settings(overrides):
    settings = {
        "iterations": 1,
        "repeats": 1,
        "book_levels": 20,
        "book_event_rate_hz": 10.0,
        "quote_cycle_sec": 0.5,
        "rust_threshold_core_percent": 10.0,
    }
    settings.update(overrides)

    with pytest.raises(ValueError):
        run_benchmarks(**settings)
