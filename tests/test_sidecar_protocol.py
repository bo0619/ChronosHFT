import math

import pytest

from risk.sidecar_protocol import SidecarProtocol


def _finite_float(value, label):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _healthy_status():
    return SidecarProtocol.child_status(
        {
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
        },
        handshake_complete=True,
    )


def _parent_message(message_type, **payload):
    return SidecarProtocol.parent_message(
        message_type,
        "session-1",
        **payload,
    )


def test_launch_contract_round_trip_declares_both_sides_capabilities():
    settings = SidecarProtocol.with_launch_contract({"session_id": "s1"})

    assert SidecarProtocol.validate_launch_contract(settings) == settings
    assert settings["protocol_version"] == SidecarProtocol.VERSION
    assert set(settings["parent_capabilities"]) == (
        SidecarProtocol.PARENT_CAPABILITIES
    )
    assert set(settings["required_child_capabilities"]) == (
        SidecarProtocol.REQUIRED_CHILD_CAPABILITIES
    )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("protocol_version", None, "launch_protocol_version_invalid"),
        (
            "protocol_version",
            SidecarProtocol.VERSION + 1,
            "launch_protocol_version_incompatible",
        ),
        (
            "parent_capabilities",
            [],
            "launch_parent_capabilities_missing",
        ),
        (
            "required_child_capabilities",
            ["future.child.capability"],
            "launch_required_child_capabilities_missing",
        ),
        (
            "required_child_capabilities",
            sorted(SidecarProtocol.REQUIRED_CHILD_CAPABILITIES)
            + ["future.child.capability"],
            "launch_child_capabilities_unsupported",
        ),
    ],
)
def test_launch_contract_rejects_incompatible_peers(field, value, error):
    settings = SidecarProtocol.with_launch_contract({})
    settings[field] = value

    with pytest.raises(ValueError, match=error):
        SidecarProtocol.validate_launch_contract(settings)


def test_status_requires_current_version_and_required_capabilities():
    status = _healthy_status()
    status.pop("protocol_version")

    with pytest.raises(ValueError, match="status_protocol_version_invalid"):
        SidecarProtocol.validate_status(status, _finite_float)

    status = _healthy_status()
    status["protocol_version"] = SidecarProtocol.VERSION + 1
    with pytest.raises(
        ValueError,
        match="status_protocol_version_incompatible",
    ):
        SidecarProtocol.validate_status(status, _finite_float)

    status = _healthy_status()
    status["capabilities"] = []
    with pytest.raises(ValueError, match="status_capabilities_missing"):
        SidecarProtocol.validate_status(status, _finite_float)


def test_status_handshake_flag_is_explicit_and_strictly_boolean():
    status = _healthy_status()
    status.pop("protocol_handshake_complete")
    with pytest.raises(
        ValueError,
        match="status_protocol_handshake_complete_invalid",
    ):
        SidecarProtocol.validate_status(status, _finite_float)

    status = _healthy_status()
    status["protocol_handshake_complete"] = "false"
    with pytest.raises(
        ValueError,
        match="status_protocol_handshake_complete_invalid",
    ):
        SidecarProtocol.validate_status(status, _finite_float)

    with pytest.raises(
        ValueError,
        match="status_protocol_handshake_complete_invalid",
    ):
        SidecarProtocol.child_status({}, handshake_complete="false")


def test_healthy_status_requires_completed_handshake():
    status = _healthy_status()
    status["protocol_handshake_complete"] = False

    with pytest.raises(ValueError, match="status_healthy_state_inconsistent"):
        SidecarProtocol.validate_status(status, _finite_float)

    status["healthy"] = False
    assert SidecarProtocol.validate_status(status, _finite_float) == status
    assert SidecarProtocol.has_compatible_child_contract(status) is False


def test_status_allows_additional_optional_child_capability():
    status = _healthy_status()
    status["capabilities"].append("status.diagnostics.future")

    assert SidecarProtocol.validate_status(status, _finite_float) == status


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


def test_pending_control_ack_fields_may_be_null_in_status_projection():
    status = _healthy_status()
    status.update(
        last_quiesce_accepted=None,
        last_shutdown_resume_accepted=None,
        last_stop_accepted=None,
        last_rearm_accepted=None,
        last_stop_cancel_ok=None,
    )

    assert SidecarProtocol.validate_status(status, _finite_float) == status


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


@pytest.mark.parametrize(
    ("command_type", "status", "field"),
    [
        (
            "QUIESCE",
            {
                "last_quiesce_request_id": "request-1",
                "last_quiesce_accepted": "false",
            },
            "last_quiesce_accepted",
        ),
        (
            "STOP",
            {
                "last_stop_request_id": "request-1",
                "last_stop_accepted": True,
                "last_stop_cancel_ok": "false",
            },
            "last_stop_cancel_ok",
        ),
    ],
)
def test_control_ack_rejects_non_boolean_safety_fields(
    command_type,
    status,
    field,
):
    with pytest.raises(ValueError, match=f"status_{field}_invalid"):
        SidecarProtocol.read_control_ack(command_type, "request-1", status)


def test_unknown_control_command_is_rejected():
    with pytest.raises(ValueError, match="control_command_type_invalid"):
        SidecarProtocol.read_control_ack("UNKNOWN", "request-1", {})


@pytest.mark.parametrize(
    "command",
    [
        None,
        SidecarProtocol.parent_message(
            "STOP",
            "other",
            cancel_orders=True,
        ),
        _parent_message("UNKNOWN"),
        _parent_message("STOP", cancel_orders="false"),
        _parent_message("QUIESCE", reason=7),
        _parent_message(
            "HEARTBEAT",
            sequence=True,
            sent_monotonic=10.0,
        ),
        {
            "type": "STOP",
            "session_id": "session-1",
            "cancel_orders": True,
        },
        {
            "protocol_version": SidecarProtocol.VERSION + 1,
            "type": "STOP",
            "session_id": "session-1",
            "cancel_orders": True,
        },
    ],
)
def test_control_command_validation_rejects_malformed_fields(command):
    assert (
        SidecarProtocol.validate_control_command(command, "session-1")
        is None
    )


def test_control_command_validation_preserves_explicit_false_cancel_intent():
    command = _parent_message(
        "stop",
        request_id="request-1",
        cancel_orders=False,
    )

    assert SidecarProtocol.validate_control_command(command, "session-1") == {
        **command,
        "type": "STOP",
    }
