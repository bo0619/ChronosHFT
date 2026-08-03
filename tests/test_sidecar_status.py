from unittest.mock import patch

from risk.independent_supervisor import IndependentRiskSupervisor
from risk.sidecar_status import SidecarStatusProjection


class _Process:
    pid = 4321

    def __init__(self, alive=True):
        self.alive = alive

    def is_alive(self):
        return self.alive


def _build(**overrides):
    arguments = {
        "enabled": True,
        "process": _Process(),
        "parent_heartbeat_suspended_reason": "",
        "last_status": {},
        "last_status_received_at": 0.0,
        "last_status_protocol_error": "",
        "status_max_age_sec": 2.0,
        "settings": {"state_path": "fallback-state.json"},
        "now": 100.0,
    }
    arguments.update(overrides)
    return SidecarStatusProjection.build(**arguments)


def test_disabled_projection_is_healthy_and_preserves_safe_defaults():
    snapshot = _build(enabled=False, process=None)

    assert snapshot["enabled"] is False
    assert snapshot["process_alive"] is False
    assert snapshot["pid"] is None
    assert snapshot["healthy"] is True
    assert snapshot["status_age_sec"] is None
    assert snapshot["risk_action"] == "NONE"
    assert snapshot["funding_action"] == "NONE"
    assert snapshot["state_path"] == "fallback-state.json"
    assert snapshot["risk_metrics"] == {}
    assert snapshot["last_cancel_ok"] is None
    assert snapshot["last_stop_cancel_ok"] is None


def test_enabled_projection_requires_a_live_process_and_fresh_status():
    last_status = {
        "healthy": True,
        "reason": "",
        "exchange_healthy": True,
        "risk_action": "NONE",
        "risk_metrics": {"open_order_count": 0},
        "state_path": "reported-state.json",
        "last_stop_cancel_ok": True,
    }

    snapshot = _build(
        last_status=last_status,
        last_status_received_at=98.0,
    )

    assert snapshot["process_alive"] is True
    assert snapshot["pid"] == 4321
    assert snapshot["healthy"] is True
    assert snapshot["status_age_sec"] == 2.0
    assert snapshot["state_path"] == "reported-state.json"
    assert snapshot["last_stop_cancel_ok"] is True

    stale = _build(
        last_status=last_status,
        last_status_received_at=98.0,
        now=100.001,
    )
    assert stale["healthy"] is False

    process_down = _build(
        process=_Process(alive=False),
        last_status=last_status,
        last_status_received_at=100.0,
    )
    assert process_down["healthy"] is False


def test_projection_copies_nested_risk_metrics_for_callers():
    last_status = {"risk_metrics": {"gross_notional": 10.0}}

    snapshot = _build(last_status=last_status)
    snapshot["risk_metrics"]["gross_notional"] = 99.0

    assert last_status["risk_metrics"] == {"gross_notional": 10.0}


def test_supervisor_facade_uses_its_patchable_monotonic_clock():
    supervisor = IndependentRiskSupervisor(
        object(),
        {
            "risk": {
                "independent_supervisor": {
                    "enabled": False,
                }
            }
        },
    )
    supervisor.last_status_received_at = 10.0

    with patch(
        "risk.independent_supervisor.time.perf_counter",
        return_value=11.25,
    ):
        snapshot = supervisor.get_status_snapshot()

    assert snapshot["status_age_sec"] == 1.25
