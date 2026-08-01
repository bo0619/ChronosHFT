import math

import pytest

from risk.sidecar_protocol import SidecarProtocol


def _finite_float(value, label):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _healthy_status():
    return {
        "sequence": 1,
        "healthy": True,
        "reason": "",
        "risk_action": "NONE",
        "stage": "ARMED",
        "exchange_healthy": True,
        "kill_latched": False,
        "quiesced": False,
        "risk_snapshot_sequence": 1,
        "risk_snapshot_captured_monotonic": 10.0,
    }


def test_healthy_status_requires_complete_safe_state():
    status = _healthy_status()
    assert SidecarProtocol.validate_status(status, _finite_float) == status

    status["kill_latched"] = True
    with pytest.raises(ValueError, match="status_healthy_state_inconsistent"):
        SidecarProtocol.validate_status(status, _finite_float)


def test_boolean_cannot_be_used_as_status_sequence():
    status = _healthy_status()
    status["sequence"] = True

    with pytest.raises(ValueError, match="status_sequence_invalid"):
        SidecarProtocol.validate_status(status, _finite_float)


def test_control_ack_ignores_a_different_request_id():
    status = {
        "last_quiesce_request_id": "old-request",
        "last_quiesce_accepted": True,
    }

    assert SidecarProtocol.read_control_ack(
        "QUIESCE",
        "new-request",
        status,
    ) is None


def test_stop_failure_result_preserves_cancel_intent():
    result = SidecarProtocol.control_failure(
        "STOP",
        {"quiesced": True},
        "supervisor_process_down",
        "request-1",
        cancel_orders=False,
    )

    assert result == {
        "accepted": False,
        "reason": "supervisor_process_down",
        "request_id": "request-1",
        "quiesced": True,
        "cancel_requested": False,
        "cancel_attempted": False,
        "cancel_ok": None,
    }
