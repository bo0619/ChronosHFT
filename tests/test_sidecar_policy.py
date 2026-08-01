from dataclasses import FrozenInstanceError
import math

import pytest

from risk.sidecar_policy import RiskSidecarPolicy


def _finite_float(value, label):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def test_policy_is_immutable_and_normalizes_symbols():
    policy = RiskSidecarPolicy.from_settings(
        {"symbols": ["ethusdt", "BTCUSDT", "ethusdt"]},
        _finite_float,
    )

    assert policy.symbols == ("BTCUSDT", "ETHUSDT")
    with pytest.raises(FrozenInstanceError):
        policy.max_open_orders = 99


def test_funding_guard_caps_exchange_poll_to_half_snapshot_age():
    policy = RiskSidecarPolicy.from_settings(
        {
            "exchange_poll_interval_sec": 5.0,
            "funding_guard": {
                "enabled": True,
                "max_snapshot_age_ms": 3_000.0,
            },
        },
        _finite_float,
    )

    assert policy.exchange_poll_interval_sec == 1.5
    assert policy.snapshot_worker_timeout_sec <= policy.exchange_max_age_sec
    assert policy.rearm_snapshot_max_age_sec <= policy.exchange_max_age_sec


def test_related_kill_thresholds_cannot_be_weaker_than_reduce_only():
    policy = RiskSidecarPolicy.from_settings(
        {
            "clock_reduce_only_offset_ms": 40.0,
            "clock_kill_offset_ms": 20.0,
            "margin_reduce_only_ratio": 0.8,
            "margin_kill_ratio": 0.7,
            "liquidation_reduce_only_distance_pct": 0.04,
            "liquidation_kill_distance_pct": 0.08,
        },
        _finite_float,
    )

    assert policy.clock_reduce_only_phase_error_ms == 40.0
    assert policy.clock_kill_phase_error_ms == 40.0
    assert policy.clock_reduce_only_offset_ms == 40.0
    assert policy.clock_kill_offset_ms == 40.0
    assert policy.margin_kill_ratio == 0.8
    assert policy.liquidation_kill_distance_pct == 0.04


def test_non_finite_policy_input_fails_closed_with_original_label():
    with pytest.raises(
        ValueError,
        match="parent_heartbeat_timeout_sec must be a finite number",
    ):
        RiskSidecarPolicy.from_settings(
            {"parent_heartbeat_timeout_sec": float("nan")},
            _finite_float,
        )
