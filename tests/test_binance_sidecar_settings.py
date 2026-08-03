import math
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from risk.binance_sidecar_settings import (
    BinanceSidecarExchangeConfiguration,
)
from risk.independent_supervisor import BinanceRiskSidecarExchange


def _finite_float(value, label):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _configuration(settings=None):
    return BinanceSidecarExchangeConfiguration.from_settings(
        settings or {},
        _finite_float,
    )


def test_exchange_configuration_preserves_defaults_and_initial_state():
    configuration = _configuration()
    owner = SimpleNamespace()

    configuration.initialize_owner(owner)

    assert configuration.rate_limit_settings == {}
    assert owner.symbols == ()
    assert owner.funding_guard_enabled is False
    assert owner.funding_max_source_age_ms == 3_000.0
    assert owner.daily_loss_enabled is False
    assert owner.cash_flow_income_types == {"TRANSFER"}
    assert owner.cash_flow_assets == {"USDT", "USDC", "BUSD", "FDUSD"}
    assert owner.cash_flow_max_pages == 5
    assert owner.cash_flow_poll_interval_sec == 30.0
    assert owner.full_open_orders_audit_interval_sec == 60.0
    assert owner.clock_sync_enabled is True
    assert owner.clock_sync_interval_sec == 30.0
    assert owner.clock_sample_count == 5
    assert owner.clock_min_successful_samples == 3
    assert owner.clock_low_rtt_sample_count == 3
    assert owner.clock_sample_spacing_ms == 10.0
    assert owner.clock_max_rtt_ms == 200.0
    assert owner.clock_max_uncertainty_ms == 50.0
    assert owner.clock_max_offset_dispersion_ms == 10.0
    assert owner.clock_max_wall_step_ms == 20.0
    assert owner.clock_max_initial_offset_ms == 5_000.0
    assert owner.clock_reduce_only_phase_error_ms == 25.0
    assert owner.clock_kill_phase_error_ms == 100.0
    assert owner._last_cash_flow_poll_monotonic == 0.0
    assert owner._cached_external_cash_flow_total == 0.0
    assert owner._cash_flow_cache_initialized is False
    assert owner._last_full_open_orders_audit_monotonic == 0.0
    assert owner._known_open_order_symbols == set()
    assert owner.last_clock_sync_monotonic == 0.0
    assert owner.clock_reason == "clock_sync_missing"


def test_exchange_configuration_normalizes_symbols_sets_and_thresholds():
    configuration = _configuration(
        {
            "rest_rate_limit": {"enabled": True, "capacity": 100},
            "symbols": [" ethusdt ", "BTCUSDT", "ethusdt", ""],
            "funding_guard": {
                "enabled": True,
                "max_snapshot_age_ms": 1_500.0,
            },
            "daily_loss_enabled": True,
            "cash_flow_income_types": ["transfer", "WELCOME_BONUS"],
            "cash_flow_assets": ["usdt", "USDC"],
            "cash_flow_max_pages": -2,
            "cash_flow_poll_interval_sec": 0.2,
            "full_open_orders_audit_interval_sec": 1.0,
            "clock_sync_enabled": False,
            "clock_sync_interval_sec": 0.2,
            "clock_sample_count": 2,
            "clock_min_successful_samples": 5,
            "clock_low_rtt_sample_count": -3,
            "clock_sample_spacing_ms": -1.0,
            "clock_max_rtt_ms": -1.0,
            "clock_max_uncertainty_ms": -1.0,
            "clock_max_offset_dispersion_ms": -1.0,
            "clock_max_wall_step_ms": -1.0,
            "clock_max_initial_offset_ms": -1.0,
            "clock_reduce_only_offset_ms": 40.0,
            "clock_kill_offset_ms": 20.0,
        }
    )
    owner = SimpleNamespace()
    configuration.initialize_owner(owner)

    assert configuration.rate_limit_settings == {
        "enabled": True,
        "capacity": 100,
    }
    assert owner.symbols == ("BTCUSDT", "ETHUSDT")
    assert owner.funding_guard_enabled is True
    assert owner.funding_max_source_age_ms == 1_500.0
    assert owner.daily_loss_enabled is True
    assert owner.cash_flow_income_types == {"TRANSFER", "WELCOME_BONUS"}
    assert owner.cash_flow_assets == {"USDT", "USDC"}
    assert owner.cash_flow_max_pages == 1
    assert owner.cash_flow_poll_interval_sec == 1.0
    assert owner.full_open_orders_audit_interval_sec == 5.0
    assert owner.clock_sync_enabled is False
    assert owner.clock_sync_interval_sec == 1.0
    assert owner.clock_sample_count == 2
    assert owner.clock_min_successful_samples == 2
    assert owner.clock_low_rtt_sample_count == 1
    assert owner.clock_sample_spacing_ms == 0.0
    assert owner.clock_max_rtt_ms == 0.0
    assert owner.clock_max_uncertainty_ms == 0.0
    assert owner.clock_max_offset_dispersion_ms == 0.0
    assert owner.clock_max_wall_step_ms == 0.0
    assert owner.clock_max_initial_offset_ms == 0.0
    assert owner.clock_reduce_only_phase_error_ms == 40.0
    assert owner.clock_kill_phase_error_ms == 40.0


def test_explicit_phase_keys_override_legacy_offset_aliases():
    configuration = _configuration(
        {
            "clock_reduce_only_phase_error_ms": 30.0,
            "clock_reduce_only_offset_ms": 99.0,
            "clock_kill_phase_error_ms": 80.0,
            "clock_kill_offset_ms": 100.0,
        }
    )

    assert configuration.clock_reduce_only_phase_error_ms == 30.0
    assert configuration.clock_kill_phase_error_ms == 80.0


def test_rate_limit_coordination_and_finite_values_fail_closed():
    with pytest.raises(ValueError, match="rate-limit coordination"):
        BinanceSidecarExchangeConfiguration.validated_rate_limit_settings(
            {"rest_rate_limit": {"enabled": False}}
        )

    with pytest.raises(
        ValueError,
        match="clock_max_rtt_ms must be a finite number",
    ):
        _configuration({"clock_max_rtt_ms": float("nan")})

    with pytest.raises(ValueError, match="funding_guard settings"):
        _configuration({"funding_guard": []})


def test_configuration_is_frozen_and_owner_sets_are_independent_copies():
    configuration = _configuration()
    first = SimpleNamespace()
    second = SimpleNamespace()
    configuration.initialize_owner(first)
    configuration.initialize_owner(second)

    first.cash_flow_assets.add("TUSD")

    assert "TUSD" not in second.cash_flow_assets
    with pytest.raises(FrozenInstanceError):
        configuration.clock_sync_enabled = False


def test_exchange_facade_builds_rest_client_and_applies_configuration():
    session = SimpleNamespace(headers={}, close=lambda: None)
    rest = SimpleNamespace(clock_resync_callback=None)
    budget = object()
    settings = {
        "rest_rate_limit": {"enabled": True},
        "symbols": ["ethusdt"],
        "clock_sample_count": 2,
    }

    with (
        patch("requests.Session", return_value=session) as session_type,
        patch(
            "gateway.binance.rate_limit_budget."
            "BinanceRateLimitBudget.from_config",
            return_value=budget,
        ) as budget_factory,
        patch(
            "gateway.binance.rest_api.BinanceRestApi",
            return_value=rest,
        ) as rest_type,
    ):
        exchange = BinanceRiskSidecarExchange(
            "risk-key",
            "risk-secret",
            True,
            settings=settings,
        )

    session_type.assert_called_once_with()
    budget_factory.assert_called_once_with({"enabled": True})
    rest_type.assert_called_once_with(
        "risk-key",
        "risk-secret",
        session,
        testnet=True,
        rate_limit_budget=budget,
    )
    assert session.headers == {"Content-Type": "application/json"}
    assert rest.clock_resync_callback == exchange.sync_exchange_clock
    assert exchange.symbols == ("ETHUSDT",)
    assert exchange.clock_sample_count == 2
