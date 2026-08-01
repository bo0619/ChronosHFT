"""Offline microbenchmarks for the bounded ChronosHFT runtime hot paths.

This script performs no network, OMS, order, database, or credential access.
Run it on the deployment host before moving a component across a Rust FFI
boundary; desktop timings are not evidence for the AWS instance.
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.orderbook import LocalOrderBook  # noqa: E402
from strategy.quote_math import (  # noqa: E402
    ASQuoteScenario,
    GLFTQuoteScenario,
    robust_adaptive_portfolio_as_quote_offsets,
    robust_adaptive_portfolio_glft_quote_offsets,
)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise argparse.ArgumentTypeError("value must be positive and finite")
    return parsed


def _measure_ns(
    operation: Callable[[int], Any],
    *,
    iterations: int,
    repeats: int,
) -> dict[str, float | int]:
    warmup = min(100, iterations)
    for index in range(warmup):
        operation(index)

    samples = []
    gc.collect()
    gc_was_enabled = gc.isenabled()
    if gc_was_enabled:
        gc.disable()
    try:
        for repeat in range(repeats):
            started = time.perf_counter_ns()
            for index in range(iterations):
                operation(index + repeat * iterations)
            elapsed = time.perf_counter_ns() - started
            samples.append(elapsed / iterations)
    finally:
        if gc_was_enabled:
            gc.enable()

    minimum = min(samples)
    maximum = max(samples)
    stability_ratio = maximum / minimum if minimum > 0.0 else math.inf
    return {
        "median_ns_per_call": statistics.median(samples),
        "min_ns_per_call": minimum,
        "max_ns_per_call": maximum,
        "max_to_min_ratio": stability_ratio,
        "stable": stability_ratio <= 1.5,
        "iterations_per_repeat": iterations,
        "repeats": repeats,
    }


def _one_core_percent(ns_per_call: float, calls_per_sec: float) -> float:
    return ns_per_call * calls_per_sec / 1_000_000_000.0 * 100.0


def _build_orderbook_operation(levels: int) -> Callable[[int], None]:
    tick = 0.001
    book = LocalOrderBook(
        "BENCHUSDT",
        publish_depth_levels=20,
        max_levels_per_side=max(levels, 20),
        max_delta_levels_per_side=8,
    )
    snapshot = {
        "lastUpdateId": 1,
        "bids": [
            [f"{100.0 - (index + 1) * tick:.6f}", "1.0"]
            for index in range(levels)
        ],
        "asks": [
            [f"{100.0 + (index + 1) * tick:.6f}", "1.0"]
            for index in range(levels)
        ],
    }
    book.init_snapshot(snapshot)
    best_bid = snapshot["bids"][0][0]
    delta = {
        "U": 2,
        "u": 2,
        "pu": 1,
        "b": [[best_bid, "1.1"]],
        "a": [],
        "E": 1_700_000_000_000,
    }

    def apply_delta(index: int) -> None:
        next_update_id = book.last_update_id + 1
        delta["U"] = next_update_id
        delta["u"] = next_update_id
        delta["pu"] = book.last_update_id
        delta["b"][0][1] = "1.1" if index % 2 else "1.2"
        book.process_delta(
            delta,
            received_timestamp=1_700_000_000.0,
            received_monotonic=1.0,
            clock_offset_ms=0.0,
            corrected_received_timestamp=1_700_000_000.0,
        )

    return apply_delta


def _as_scenarios() -> tuple[ASQuoteScenario, ...]:
    scenarios = []
    for k_name, k_multiplier in (("LOW_K", 0.8), ("BASE_K", 1.0), ("HIGH_K", 1.25)):
        for vol_name, vol_multiplier in (("BASE_VOL", 1.0), ("HIGH_VOL", 1.2)):
            scenarios.append(
                ASQuoteScenario(
                    name=f"{k_name}_{vol_name}",
                    bid_k_per_bps=[0.5 * k_multiplier],
                    ask_k_per_bps=[0.55 * k_multiplier],
                    covariance_bps2_per_s=[[vol_multiplier**2]],
                    bid_adverse_cost_bps=[0.4],
                    ask_adverse_cost_bps=[0.5],
                )
            )
    return tuple(scenarios)


def _glft_scenarios() -> tuple[GLFTQuoteScenario, ...]:
    scenarios = []
    for intensity_name, intensity_multiplier in (("LOW_A", 0.8), ("HIGH_A", 1.2)):
        for k_name, k_multiplier in (("LOW_K", 0.8), ("BASE_K", 1.0), ("HIGH_K", 1.25)):
            for vol_name, vol_multiplier in (("BASE_VOL", 1.0), ("HIGH_VOL", 1.2)):
                scenarios.append(
                    GLFTQuoteScenario(
                        name=f"{intensity_name}_{k_name}_{vol_name}",
                        bid_A_per_s=[2.0 * intensity_multiplier],
                        ask_A_per_s=[1.8 * intensity_multiplier],
                        bid_k_per_bps=[0.5 * k_multiplier],
                        ask_k_per_bps=[0.55 * k_multiplier],
                        covariance_bps2_per_s=[[vol_multiplier**2]],
                        bid_adverse_cost_bps=[0.4],
                        ask_adverse_cost_bps=[0.5],
                    )
                )
    return tuple(scenarios)


def run_benchmarks(
    *,
    iterations: int,
    repeats: int,
    book_levels: int,
    book_event_rate_hz: float,
    quote_cycle_sec: float,
    rust_threshold_core_percent: float,
) -> dict[str, Any]:
    if iterations <= 0 or repeats <= 0:
        raise ValueError("iterations and repeats must be positive")
    if not 1 <= book_levels <= 50_000:
        raise ValueError("book_levels must be from 1 to 50000")
    if (
        not math.isfinite(book_event_rate_hz)
        or book_event_rate_hz <= 0.0
        or not math.isfinite(quote_cycle_sec)
        or quote_cycle_sec <= 0.0
        or not math.isfinite(rust_threshold_core_percent)
        or rust_threshold_core_percent <= 0.0
    ):
        raise ValueError("benchmark rates and threshold must be positive and finite")

    quote_rate_hz = 1.0 / quote_cycle_sec
    operations: Sequence[tuple[str, Callable[[int], Any], float]] = (
        (
            "orderbook_delta",
            _build_orderbook_operation(book_levels),
            book_event_rate_hz,
        ),
        (
            "robust_as_cycle",
            lambda _index: robust_adaptive_portfolio_as_quote_offsets(
                mid_prices=[100.0],
                inventory_lots=[1.25],
                gamma_per_bps=0.05,
                order_size_lots=[1.0],
                horizon_s=1.0,
                scenarios=_as_scenarios_cached,
            ),
            quote_rate_hz,
        ),
        (
            "robust_glft_cycle",
            lambda _index: robust_adaptive_portfolio_glft_quote_offsets(
                mid_prices=[100.0],
                inventory_lots=[1.25],
                gamma_per_bps=0.05,
                order_size_lots=[1.0],
                horizon_s=1.0,
                scenarios=_glft_scenarios_cached,
            ),
            quote_rate_hz,
        ),
    )
    results = {}
    rust_candidates = []
    unstable_measurements = []
    for name, operation, call_rate_hz in operations:
        measurement = _measure_ns(
            operation,
            iterations=iterations,
            repeats=repeats,
        )
        core_percent = _one_core_percent(
            float(measurement["median_ns_per_call"]),
            call_rate_hz,
        )
        measurement["configured_calls_per_sec"] = call_rate_hz
        measurement["estimated_one_core_percent"] = core_percent
        measurement["rust_candidate"] = bool(
            measurement["stable"]
            and core_percent >= rust_threshold_core_percent
        )
        results[name] = measurement
        if measurement["rust_candidate"]:
            rust_candidates.append(name)
        if not measurement["stable"]:
            unstable_measurements.append(name)

    return {
        "schema": "chronoshft.runtime_hot_paths.v1",
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "logical_cpu_count": os.cpu_count(),
        },
        "settings": {
            "iterations": iterations,
            "repeats": repeats,
            "book_levels": book_levels,
            "book_event_rate_hz": book_event_rate_hz,
            "quote_cycle_sec": quote_cycle_sec,
            "rust_threshold_one_core_percent": rust_threshold_core_percent,
            "garbage_collection_during_measurement": False,
        },
        "results": results,
        "decision": {
            "rust_candidates": rust_candidates,
            "rust_recommended": bool(rust_candidates),
            "unstable_measurements": unstable_measurements,
            "rule": (
                "Profile production first; consider Rust only when max/min "
                "repeat timing is <=1.5 and the pure-compute path is at or "
                "above the configured one-core threshold."
            ),
        },
    }


_as_scenarios_cached = _as_scenarios()
_glft_scenarios_cached = _glft_scenarios()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark ChronosHFT pure-compute hot paths offline.",
    )
    parser.add_argument("--iterations", type=_positive_int, default=1000)
    parser.add_argument("--repeats", type=_positive_int, default=5)
    parser.add_argument("--book-levels", type=_positive_int, default=2000)
    parser.add_argument(
        "--book-event-rate-hz",
        type=_positive_float,
        default=10.0,
    )
    parser.add_argument("--quote-cycle-sec", type=_positive_float, default=0.5)
    parser.add_argument(
        "--rust-threshold-core-percent",
        type=_positive_float,
        default=10.0,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_benchmarks(
        iterations=args.iterations,
        repeats=args.repeats,
        book_levels=args.book_levels,
        book_event_rate_hz=args.book_event_rate_hz,
        quote_cycle_sec=args.quote_cycle_sec,
        rust_threshold_core_percent=args.rust_threshold_core_percent,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
