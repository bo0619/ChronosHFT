from risk.independent_supervisor import RiskSidecarCore
from risk.sidecar_control_state import SidecarControlController


def _controller(*, flat_verification_checks=1):
    return SidecarControlController(
        cancel_retry_sec=1.0,
        flatten_enabled=True,
        flatten_retry_sec=0.5,
        flat_verification_checks=flat_verification_checks,
        rearm_prepare_ttl_sec=10.0,
        token_factory=lambda size: f"token-{size}",
        wall_time=lambda: 1_700_000_000.0,
    )


def test_quiesce_transition_is_owned_and_requires_durable_commit():
    controller = _controller()
    persisted = []

    accepted, reason, effects = controller.request_quiesce(
        "quiesce-1",
        "shutdown_complete",
        7,
        lambda event, require_path: (
            persisted.append((event, require_path)) is None,
            "",
        ),
    )

    assert accepted is True
    assert reason == "supervisor_quiesced"
    assert effects.reset_cancel_retry is False
    assert persisted == [("supervisor_quiesced", True)]
    assert controller.state.quiesced is True
    assert controller.state.quiesce_reason == "shutdown_complete"
    assert controller.state.quiesced_at == 1_700_000_000.0
    assert controller.state.quiesce_snapshot_sequence == 7
    assert controller.state.stage == "QUIESCED"
    assert controller.state.last_quiesce_accepted is True
    assert controller.state.last_quiesce_persisted is True
    assert not hasattr(controller, "owner")
    assert not hasattr(controller, "core")


def test_quiesce_persist_failure_restores_active_fail_closed_supervision():
    controller = _controller()

    accepted, reason, effects = controller.request_quiesce(
        "quiesce-2",
        "shutdown_complete",
        8,
        lambda event, require_path: (False, "disk_unavailable"),
    )

    assert accepted is False
    assert reason == "disk_unavailable"
    assert effects.reset_cancel_retry is True
    assert effects.reset_flatten_retry is True
    assert controller.state.quiesced is False
    assert controller.state.kill_latched is True
    assert controller.state.kill_reason == "disk_unavailable"
    assert controller.state.stage == "FAILED"
    assert controller.state.last_quiesce_accepted is False
    assert controller.state.last_quiesce_persisted is False


def test_stop_plan_waits_for_fresh_flat_truth_and_takes_over_on_drift():
    controller = _controller()
    controller.request_quiesce(
        "quiesce-3",
        "shutdown_complete",
        10,
        lambda event, require_path: (True, ""),
    )
    controller.request_stop("stop-1", cancel_orders=False)

    waiting = controller.stop_plan(
        exchange_valid=True,
        exchange_reason="",
        open_order_count=0,
        nonzero_position_count=0,
        risk_snapshot_sequence=10,
    )
    drifted = controller.stop_plan(
        exchange_valid=True,
        exchange_reason="",
        open_order_count=1,
        nonzero_position_count=0,
        risk_snapshot_sequence=11,
    )

    assert waiting.action == "WAIT"
    assert drifted.action == "TAKEOVER"
    assert drifted.reason == (
        "stop_guard_account_not_flat:open_orders=1:positions=0"
    )
    assert drifted.transition_reason == drifted.reason

    controller.finish_stop(
        accepted=False,
        reason=drifted.reason,
        cancel_requested=False,
        cancel_attempted=False,
        cancel_ok=None,
    )
    assert controller.state.stop_requested is False
    assert controller.state.last_stop_request_id == "stop-1"
    assert controller.state.last_stop_accepted is False


def test_rearm_generation_fence_rejects_lost_flat_verification():
    controller = _controller()
    controller.state.kill_latched = True
    controller.state.kill_reason = "risk_breach"
    controller.state.stage = "FLAT_VERIFIED"
    controller.state.flat_verification_count = 1

    accepted, token, reason = controller.prepare_rearm(
        "prepare-1",
        "operator_ack",
        100.0,
        (True, ""),
    )
    controller.reset_flat_verification()
    valid, refusal = controller.validate_rearm_commit(
        "commit-1",
        token,
        101.0,
    )

    assert accepted is True
    assert reason == "rearm_prepared"
    assert valid is False
    assert refusal == "rearm_generation_changed"
    assert controller.state.prepared_rearm is None


def test_rearm_commit_and_rollback_are_explicit_state_transitions():
    controller = _controller()
    controller.state.kill_latched = True
    controller.state.kill_reason = "risk_breach"
    controller.state.stage = "FLAT_VERIFIED"
    controller.state.flat_verification_count = 1
    accepted, token, _ = controller.prepare_rearm(
        "prepare-2",
        "operator_ack",
        200.0,
        (True, ""),
    )
    valid, _ = controller.validate_rearm_commit(
        "commit-2",
        token,
        201.0,
    )

    checkpoint = controller.begin_rearm_commit()
    committed, reason = controller.finish_rearm_commit(
        "commit-2",
        False,
        "state_write_failed",
        checkpoint,
    )

    assert accepted is True
    assert valid is True
    assert committed is False
    assert reason == "state_write_failed"
    assert controller.state.kill_latched is True
    assert controller.state.kill_reason == "risk_breach"
    assert controller.state.stage == "FLAT_VERIFIED"
    assert controller.state.last_rearm_accepted is False
    assert controller.state.prepared_rearm is None


def test_safe_flat_verification_progress_does_not_invalidate_rearm_token():
    controller = _controller()
    controller.state.kill_latched = True
    controller.state.kill_reason = "risk_breach"
    controller.state.stage = "FLAT_VERIFIED"
    controller.state.flat_verification_count = 1
    controller.state.last_verified_snapshot_sequence = 4
    accepted, token, _ = controller.prepare_rearm(
        "prepare-3",
        "operator_ack",
        300.0,
        (True, ""),
    )
    prepared_generation = controller.generation

    actions = controller.advance_risk_stage(
        healthy=False,
        action="KILL",
        now=300.5,
        parent_healthy=True,
        exchange_valid=True,
        open_order_count=0,
        nonzero_position_count=0,
        risk_snapshot_sequence=5,
        parent_stale_snapshot_sequence=0,
        last_cancel_attempt_at=300.0,
        last_flatten_attempt_at=300.0,
    )
    valid, reason = controller.validate_rearm_commit(
        "commit-3",
        token,
        301.0,
    )

    assert accepted is True
    assert actions.cancel is False
    assert actions.flatten is False
    assert controller.state.stage == "FLAT_VERIFIED"
    assert controller.generation == prepared_generation
    assert valid is True
    assert reason == ""


def test_risk_stage_returns_emergency_actions_and_flat_verification():
    controller = _controller()
    controller.latch_kill("maintenance_margin_kill")

    actions = controller.advance_risk_stage(
        healthy=False,
        action="KILL",
        now=400.0,
        parent_healthy=True,
        exchange_valid=True,
        open_order_count=1,
        nonzero_position_count=1,
        risk_snapshot_sequence=1,
        parent_stale_snapshot_sequence=0,
        last_cancel_attempt_at=0.0,
        last_flatten_attempt_at=0.0,
    )
    verified = controller.advance_risk_stage(
        healthy=False,
        action="KILL",
        now=401.0,
        parent_healthy=True,
        exchange_valid=True,
        open_order_count=0,
        nonzero_position_count=0,
        risk_snapshot_sequence=2,
        parent_stale_snapshot_sequence=0,
        last_cancel_attempt_at=400.0,
        last_flatten_attempt_at=400.0,
    )

    assert actions.cancel is True
    assert actions.flatten is True
    assert verified.flatten is False
    assert controller.state.stage == "FLAT_VERIFIED"
    assert controller.state.flat_verification_count == 1


def test_core_control_fields_are_not_duplicated_on_the_runtime_owner():
    core = RiskSidecarCore(
        object(),
        {"symbols": ["BTCUSDT"]},
        now=10.0,
    )

    core.kill_latched = True
    core.kill_reason = "manual_test"
    core.stage = "FLATTENING"

    assert core.control.state.kill_latched is True
    assert core.control.state.kill_reason == "manual_test"
    assert core.control.state.stage == "FLATTENING"
    assert "kill_latched" not in core.__dict__
    assert "kill_reason" not in core.__dict__
    assert "stage" not in core.__dict__


def test_core_quiesce_without_durable_path_fails_closed():
    core = RiskSidecarCore(
        object(),
        {"symbols": ["BTCUSDT"]},
        now=20.0,
    )

    accepted, reason = core.request_quiesce(
        "quiesce-without-path",
        "shutdown_complete",
    )

    assert accepted is False
    assert reason == "quiesce_state_path_missing"
    assert core.quiesced is False
    assert core.kill_latched is True
    assert core.kill_reason == "quiesce_state_path_missing"
    assert core.stage == "FAILED"
    assert core.last_quiesce_accepted is False
    assert core.last_quiesce_persisted is False
