import hashlib
import math
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from risk.sidecar_settings import SidecarSupervisorConfiguration


def _finite_float(value, label):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _build(config, risk_manager=None):
    return SidecarSupervisorConfiguration.from_root(
        config,
        risk_manager,
        _finite_float,
        "independent_supervisor",
    )


def test_configuration_assembles_parent_and_child_settings():
    risk_manager = SimpleNamespace(
        risk_day="2026-08-02",
        initial_equity=10_000.0,
        initial_external_cash_flow_total=20.0,
        peak_equity=10_100.0,
        last_equity=10_050.0,
        deployment_start_equity=9_900.0,
        deployment_start_external_cash_flow_total=5.0,
        deployment_adjusted_equity=10_045.0,
        deployment_loss=15.0,
    )
    config = {
        "testnet": True,
        "symbols": ["SNDKUSDT", "SOXLUSDT"],
        "system": {
            "binance_rest_rate_limit": {
                "full_open_orders_audit_interval_sec": 12.0,
            }
        },
        "oms": {"max_total_active_orders": 77},
        "live_launch": {
            "deployment_id": "paper-jp",
            "declared_account_equity_usdt": 10_000.0,
            "max_deployed_capital_usdt": 2_000.0,
            "max_deployment_loss_usdt": 100.0,
            "deployment_loss_reduce_only_fraction": 0.7,
        },
        "risk": {
            "risk_control_heartbeat": {
                "enabled": True,
                "required_source": "independent_supervisor",
            },
            "limits": {
                "max_account_gross_notional": 500.0,
                "max_daily_loss": 100.0,
                "max_drawdown_pct": 0.1,
            },
            "margin_health": {
                "reduce_only_ratio": 0.6,
                "kill_ratio": 0.8,
            },
            "cash_flow_truth": {
                "external_income_types": ["TRANSFER", "WELCOME_BONUS"],
                "poll_interval_sec": 15.0,
            },
            "funding_guard": {"enabled": True},
            "independent_supervisor": {
                "enabled": True,
                "api_key": "risk-key",
                "api_secret": "risk-secret",
                "heartbeat_interval_sec": 0.2,
                "status_max_age_sec": 1.5,
                "max_daily_loss": 80.0,
                "custom_child_setting": "preserved",
            },
        },
    }

    configuration = _build(config, risk_manager)
    settings = configuration.child_settings

    assert configuration.enabled is True
    assert configuration.heartbeat_interval_sec == 0.2
    assert configuration.status_max_age_sec == 1.5
    assert configuration.supervisor_config["custom_child_setting"] == (
        "preserved"
    )
    assert settings["custom_child_setting"] == "preserved"
    assert settings["testnet"] is True
    assert settings["symbols"] == ["SNDKUSDT", "SOXLUSDT"]
    assert settings["full_open_orders_audit_interval_sec"] == 12.0
    assert settings["funding_guard"] == {"enabled": True}
    assert settings["max_account_gross_notional"] == 500.0
    assert settings["max_daily_loss"] == 80.0
    assert settings["max_drawdown_pct"] == 0.1
    assert settings["margin_reduce_only_ratio"] == 0.6
    assert settings["margin_kill_ratio"] == 0.8
    assert settings["max_open_orders"] == 77
    assert settings["deployment_id"] == "paper-jp"
    assert settings["cash_flow_income_types"] == [
        "TRANSFER",
        "WELCOME_BONUS",
    ]
    assert settings["cash_flow_poll_interval_sec"] == 15.0
    assert settings["seed_risk_day"] == "2026-08-02"
    assert settings["seed_day_start_equity"] == 10_000.0
    assert settings["seed_external_cash_flow_total"] == 20.0
    assert settings["seed_peak_adjusted_equity"] == 10_100.0
    assert settings["seed_last_equity"] == 10_050.0
    assert settings["seed_deployment_start_equity"] == 9_900.0
    assert settings["seed_deployment_external_cash_flow_total"] == 5.0
    assert settings["seed_deployment_adjusted_equity"] == 10_045.0
    assert settings["seed_deployment_loss"] == 15.0
    assert settings["account_key_fingerprint"] == hashlib.sha256(
        b"risk-key"
    ).hexdigest()


def test_missing_sidecar_caps_inherit_canonical_risk_defaults():
    configuration = _build(
        {
            "risk": {
                "risk_control_heartbeat": {
                    "enabled": True,
                    "required_source": "independent_supervisor",
                },
                "independent_supervisor": {
                    "enabled": True,
                    "api_key": "risk-key",
                    "api_secret": "risk-secret",
                },
            }
        }
    )

    assert configuration.child_settings["max_daily_loss"] == 500.0
    assert configuration.child_settings["max_account_gross_notional"] == 0.0
    assert configuration.child_settings["max_drawdown_pct"] == 0.0


def test_configuration_preserves_parent_timing_floors():
    configuration = _build(
        {
            "risk": {
                "independent_supervisor": {
                    "heartbeat_interval_sec": 0.01,
                    "status_max_age_sec": 0.01,
                    "stop_timeout_sec": 0.1,
                    "control_enqueue_timeout_sec": 0.001,
                    "recovery_checks": -1,
                    "exchange_max_age_sec": 0.01,
                    "rearm_command_timeout_sec": 0.1,
                }
            }
        }
    )

    assert configuration.heartbeat_interval_sec == 0.05
    assert configuration.status_max_age_sec == 0.05
    assert configuration.stop_timeout_sec == 0.5
    assert configuration.control_enqueue_timeout_sec == 0.01
    assert configuration.recovery_checks == 1
    assert configuration.recovery_snapshot_max_age_sec == 0.05
    assert configuration.rearm_command_timeout_sec == 0.5
    assert configuration.child_settings["account_key_fingerprint"] == ""


def test_enabled_configuration_requires_heartbeat_identity_and_credentials():
    with pytest.raises(
        ValueError,
        match="risk_control_heartbeat.enabled",
    ):
        _build({"risk": {"independent_supervisor": {"enabled": True}}})

    with pytest.raises(ValueError, match="required_source"):
        _build(
            {
                "risk": {
                    "risk_control_heartbeat": {
                        "enabled": True,
                        "required_source": "wrong-source",
                    },
                    "independent_supervisor": {"enabled": True},
                }
            }
        )

    with pytest.raises(ValueError, match="dedicated API credentials"):
        _build(
            {
                "risk": {
                    "risk_control_heartbeat": {
                        "enabled": True,
                        "required_source": "independent_supervisor",
                    },
                    "independent_supervisor": {"enabled": True},
                }
            }
        )


def test_non_finite_parent_timing_fails_with_original_field_label():
    with pytest.raises(
        ValueError,
        match="status_max_age_sec must be a finite number",
    ):
        _build(
            {
                "risk": {
                    "independent_supervisor": {
                        "status_max_age_sec": float("inf"),
                    }
                }
            }
        )


@pytest.mark.parametrize(
    ("field", "root_value", "sidecar_value", "message"),
    [
        ("max_account_gross_notional", 500.0, 0.0, "must be positive"),
        ("max_daily_loss", 100.0, 101.0, "must not exceed"),
        ("max_drawdown_pct", 0.1, float("inf"), "finite number"),
    ],
)
def test_enabled_sidecar_caps_must_be_positive_finite_and_no_wider_than_root(
    field,
    root_value,
    sidecar_value,
    message,
):
    config = {
        "risk": {
            "limits": {
                "max_account_gross_notional": 500.0,
                "max_daily_loss": 100.0,
                "max_drawdown_pct": 0.1,
                field: root_value,
            },
            "risk_control_heartbeat": {
                "enabled": True,
                "required_source": "independent_supervisor",
            },
            "independent_supervisor": {
                "enabled": True,
                "api_key": "risk-key",
                "api_secret": "risk-secret",
                field: sidecar_value,
            },
        }
    }

    with pytest.raises(ValueError, match=message):
        _build(config)


def test_top_level_configuration_is_immutable():
    configuration = _build({})

    with pytest.raises(FrozenInstanceError):
        configuration.enabled = True
