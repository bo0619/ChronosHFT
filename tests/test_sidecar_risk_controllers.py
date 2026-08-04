from datetime import datetime, timezone
from pathlib import Path
import tempfile

from risk.funding_guard import FundingGuardPolicy
from risk.independent_supervisor import RiskSidecarCore
from risk.sidecar_account_risk import SidecarAccountRiskController
from risk.sidecar_funding_risk import SidecarFundingRiskController
from risk.sidecar_policy import RiskSidecarPolicy


def _finite_float(value, label):
    result = float(value)
    if result != result or result in {float("inf"), float("-inf")}:
        raise ValueError(f"{label} must be finite")
    return result


def _account_controller(**settings):
    configured = {
        "symbols": ["BTCUSDT"],
        "daily_loss_enabled": True,
        "max_daily_loss": 100.0,
        "max_drawdown_pct": 0.0,
        "daily_loss_reduce_only_fraction": 0.5,
        **settings,
    }
    policy = RiskSidecarPolicy.from_settings(configured, _finite_float)
    return SidecarAccountRiskController.from_settings(
        policy,
        configured,
        _finite_float,
        wall_time=lambda: 1_700_000_000.0,
    )


def _snapshot(equity, external_cash_flow_total, captured_at):
    return {
        "account": {
            "totalMaintMargin": "0",
            "totalMarginBalance": str(equity),
        },
        "positions": [],
        "open_orders": [],
        "external_cash_flow_total": external_cash_flow_total,
        "daily_external_cash_flow_total": external_cash_flow_total,
        "deployment_external_cash_flow_total": external_cash_flow_total,
        "captured_at": captured_at,
    }


def test_account_controller_owns_cash_flow_adjusted_equity_state():
    controller = _account_controller()
    captured_at = datetime(
        2026,
        7,
        20,
        1,
        tzinfo=timezone.utc,
    ).timestamp()

    first_action, first_reason, _ = controller.evaluate(
        _snapshot(1_000.0, 0.0, captured_at)
    )
    action, reason, metrics = controller.evaluate(
        _snapshot(1_040.0, 100.0, captured_at + 60.0)
    )

    assert (first_action, first_reason) == ("NONE", "")
    assert action == "REDUCE_ONLY"
    assert reason == "daily_loss_reduce_only:60.000000"
    assert metrics["cash_flow_adjusted_equity"] == 940.0
    assert metrics["cash_flow_adjusted_daily_loss"] == 60.0
    assert controller.state.day_start_equity == 1_000.0
    assert controller.state.last_equity == 1_040.0
    assert not hasattr(controller, "owner")


def test_deployment_loss_does_not_reset_at_the_utc_day_boundary():
    controller = _account_controller(
        max_daily_loss=0.0,
        max_deployment_loss_usdt=200.0,
        deployment_loss_reduce_only_fraction=0.5,
    )
    first_day = datetime(
        2026,
        7,
        20,
        23,
        59,
        tzinfo=timezone.utc,
    ).timestamp()

    controller.evaluate(_snapshot(1_000.0, 0.0, first_day))
    action, reason, metrics = controller.evaluate(
        _snapshot(900.0, 0.0, first_day + 120.0)
    )

    assert action == "REDUCE_ONLY"
    assert reason == "deployment_loss_reduce_only:100.000000"
    assert metrics["risk_day"] == "2026-07-21"
    assert controller.state.day_start_equity == 900.0
    assert controller.state.deployment_start_equity == 1_000.0
    assert controller.state.deployment_loss == 100.0


def test_account_controller_fails_closed_on_missing_equity_truth():
    controller = _account_controller()
    snapshot = _snapshot(1_000.0, 0.0, 1_700_000_000.0)
    del snapshot["daily_external_cash_flow_total"]

    action, reason, metrics = controller.evaluate(snapshot)

    assert action == "REDUCE_ONLY"
    assert reason == "daily_equity_snapshot_invalid"
    assert metrics["projected_gross_notional"] == 0.0
    assert controller.state.day_start_equity == 0.0


def test_funding_controller_recovers_only_from_owned_observation_state():
    policy = FundingGuardPolicy(
        enabled=True,
        require_snapshot=True,
        max_snapshot_age_ms=3_000.0,
        pre_funding_reduce_only_sec=60.0,
        post_funding_hold_sec=10.0,
        max_abs_funding_rate=0.001,
        max_next_funding_horizon_sec=10_000.0,
        recovery_updates=1,
    )
    controller = SidecarFundingRiskController(
        policy,
        ("BTCUSDT",),
        1.0,
        now=100.0,
    )
    controller.ingest(
        {
            "funding_observations": {
                "BTCUSDT": {
                    "observation_id": "btc-1",
                    "funding_rate": 0.0001,
                    "next_funding_epoch": 2_000.0,
                    "corrected_received_epoch": 1_000.0,
                    "received_monotonic": 111.0,
                    "clock_healthy": True,
                }
            }
        }
    )

    action, reason, metrics = controller.evaluate(111.0)

    assert (action, reason) == ("NONE", "")
    assert metrics["healthy"] is True
    assert metrics["symbols"]["BTCUSDT"]["observation_valid"] is True
    assert controller.observations["BTCUSDT"].observation_id == "btc-1"
    assert not hasattr(controller, "owner")

    controller.ingest({"funding_observations": None})
    action, reason, metrics = controller.evaluate(112.0)

    assert action == "REDUCE_ONLY"
    assert reason == "funding_guard:snapshot_unavailable:BTCUSDT"
    assert metrics["healthy"] is False
    assert controller.observations == {}


def test_core_compatibility_fields_target_controller_owned_state():
    core = RiskSidecarCore(
        object(),
        {
            "symbols": ["BTCUSDT"],
            "funding_guard": {"enabled": False},
        },
        now=10.0,
    )

    core.day_start_equity = 975.0
    core.deployment_loss = 12.5
    core.deployment_id = "restored-deployment"
    core.funding_action = "REDUCE_ONLY"

    assert core.account_risk.state.day_start_equity == 975.0
    assert core.account_risk.state.deployment_loss == 12.5
    assert core.account_risk.state.deployment_id == "restored-deployment"
    assert core.funding_risk.action == "REDUCE_ONLY"
    assert "day_start_equity" not in core.__dict__
    assert "funding_action" not in core.__dict__


def test_durable_deployment_identity_restores_into_controller_state():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = str(Path(tmpdir) / "sidecar-state.json")
        RiskSidecarCore(
            object(),
            {
                "symbols": ["BTCUSDT"],
                "deployment_id": "deployment-a",
                "state_path": state_path,
                "state_required": True,
                "state_fsync": False,
            },
            now=10.0,
        )

        recovered = RiskSidecarCore(
            object(),
            {
                "symbols": ["BTCUSDT"],
                "state_path": state_path,
                "state_required": True,
                "state_fsync": False,
            },
            now=11.0,
        )

    assert recovered.state_recovered is True
    assert recovered.deployment_id == "deployment-a"
    assert recovered.account_risk.state.deployment_id == "deployment-a"
    metrics = recovered.account_risk.fallback_metrics(
        {"positions": [], "open_orders": []}
    )
    assert metrics["deployment_id"] == "deployment-a"
