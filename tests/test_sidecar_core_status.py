from types import SimpleNamespace
from unittest.mock import patch

from risk.independent_supervisor import RiskSidecarCore
from risk.sidecar_core_status import RiskSidecarStatusProjection


def _owner():
    return SimpleNamespace(
        risk_reason="risk-reason",
        funding_action="REDUCE_ONLY",
        funding_reason="funding-reason",
        kill_latched=True,
        kill_reason="kill-reason",
        quiesced=True,
        quiesce_reason="quiesce-reason",
        quiesced_at=90.0,
        stage="FLAT_VERIFIED",
        state_path="state.json",
        state_generation=4,
        state_recovered=True,
        state_load_error="",
        state_persist_error="persist-error",
        risk_metrics={"gross_notional": 12.0},
        last_parent_sequence=8,
        last_parent_heartbeat_at=99.0,
        parent_heartbeat_error="parent-error",
        parent_stale_since=95.0,
        parent_stale_snapshot_sequence=6,
        last_parent_heartbeat_sent_monotonic=98.5,
        exchange_healthy=1,
        exchange_reason="exchange-reason",
        last_exchange_success_at=98.0,
        last_cancel_ok=False,
        last_cancel_reason="cancel-reason",
        last_flatten_ok=True,
        last_flatten_count=2,
        last_flatten_reason="flatten-reason",
        flat_verification_count=3,
        flat_verification_checks=3,
        last_verified_snapshot_sequence=10,
        risk_snapshot_sequence=10,
        quiesce_snapshot_sequence=9,
        risk_snapshot_captured_at=1_000.0,
        risk_snapshot_captured_monotonic=97.0,
        snapshot_request_inflight_sequence=11,
        last_rearm_request_id="rearm-request",
        last_rearm_phase="COMMIT",
        last_rearm_accepted=False,
        last_rearm_reason="rearm-reason",
        last_rearm_token="rearm-token",
        last_quiesce_request_id="quiesce-request",
        last_quiesce_accepted=True,
        last_quiesce_reason="quiesce-ack-reason",
        last_quiesce_persisted=True,
        last_shutdown_resume_request_id="resume-request",
        last_shutdown_resume_accepted=False,
        last_shutdown_resume_reason="resume-reason",
        last_shutdown_resume_persisted=False,
        last_stop_request_id="stop-request",
        last_stop_accepted=True,
        last_stop_reason="stop-reason",
        last_stop_quiesced=True,
        last_stop_cancel_requested=True,
        last_stop_cancel_attempted=True,
        last_stop_cancel_ok=False,
    )


def test_core_status_projection_preserves_the_complete_protocol_contract():
    owner = _owner()

    status = RiskSidecarStatusProjection.build(
        owner,
        1,
        None,
        "",
        100.0,
    )

    assert status == {
        "healthy": True,
        "reason": "",
        "risk_action": "NONE",
        "risk_reason": "risk-reason",
        "funding_action": "REDUCE_ONLY",
        "funding_reason": "funding-reason",
        "kill_latched": True,
        "kill_reason": "kill-reason",
        "quiesced": True,
        "quiesce_reason": "quiesce-reason",
        "quiesced_at": 90.0,
        "stage": "FLAT_VERIFIED",
        "state_path": "state.json",
        "state_generation": 4,
        "state_recovered": True,
        "state_load_error": "",
        "state_persist_error": "persist-error",
        "risk_metrics": {"gross_notional": 12.0},
        "parent_sequence": 8,
        "parent_age_sec": 1.0,
        "parent_heartbeat_error": "parent-error",
        "parent_stale_since": 95.0,
        "parent_stale_snapshot_sequence": 6,
        "parent_heartbeat_sent_monotonic": 98.5,
        "exchange_healthy": True,
        "exchange_reason": "exchange-reason",
        "exchange_age_sec": 2.0,
        "last_cancel_ok": False,
        "last_cancel_reason": "cancel-reason",
        "last_flatten_ok": True,
        "last_flatten_count": 2,
        "last_flatten_reason": "flatten-reason",
        "flat_verification_count": 3,
        "flat_verification_checks": 3,
        "last_verified_snapshot_sequence": 10,
        "risk_snapshot_sequence": 10,
        "quiesce_snapshot_sequence": 9,
        "risk_snapshot_captured_at": 1_000.0,
        "risk_snapshot_captured_monotonic": 97.0,
        "risk_snapshot_age_sec": 3.0,
        "risk_snapshot_worker_inflight": True,
        "last_rearm_request_id": "rearm-request",
        "last_rearm_phase": "COMMIT",
        "last_rearm_accepted": False,
        "last_rearm_reason": "rearm-reason",
        "last_rearm_token": "rearm-token",
        "last_quiesce_request_id": "quiesce-request",
        "last_quiesce_accepted": True,
        "last_quiesce_reason": "quiesce-ack-reason",
        "last_quiesce_persisted": True,
        "last_shutdown_resume_request_id": "resume-request",
        "last_shutdown_resume_accepted": False,
        "last_shutdown_resume_reason": "resume-reason",
        "last_shutdown_resume_persisted": False,
        "last_stop_request_id": "stop-request",
        "last_stop_accepted": True,
        "last_stop_reason": "stop-reason",
        "last_stop_quiesced": True,
        "last_stop_cancel_requested": True,
        "last_stop_cancel_attempted": True,
        "last_stop_cancel_ok": False,
    }

    status["risk_metrics"]["gross_notional"] = 99.0
    assert owner.risk_metrics == {"gross_notional": 12.0}


def test_core_status_age_fields_handle_missing_and_future_timestamps():
    owner = _owner()
    owner.last_parent_heartbeat_at = 101.0
    owner.last_exchange_success_at = 0.0
    owner.risk_snapshot_captured_monotonic = 0.0
    owner.snapshot_request_inflight_sequence = 0

    status = RiskSidecarStatusProjection.build(
        owner,
        False,
        "waiting",
        "REDUCE_ONLY",
        100.0,
    )

    assert status["parent_age_sec"] == 0.0
    assert status["exchange_age_sec"] is None
    assert status["risk_snapshot_age_sec"] is None
    assert status["risk_snapshot_worker_inflight"] is False


def test_core_private_status_method_remains_a_compatible_facade():
    core = RiskSidecarCore.__new__(RiskSidecarCore)
    expected = {"healthy": True}

    with patch(
        "risk.independent_supervisor.RiskSidecarStatusProjection.build",
        return_value=expected,
    ) as build:
        result = core._status(True, "ok", "NONE", 12.0)

    assert result is expected
    build.assert_called_once_with(core, True, "ok", "NONE", 12.0)
