"""STOP and rearm command orchestration for the sidecar control machine."""

from __future__ import annotations


def complete_stop_request(owner, now: float) -> bool | None:
    control = owner.control.state
    truth = owner.observation
    cancel_requested = bool(control.cancel_on_stop)
    cancel_attempted = False
    cancel_ok = None
    accepted = False

    if control.quiesced:
        owner._service_exchange_risk(
            now,
            force=(
                truth.risk_snapshot_sequence
                <= control.quiesce_snapshot_sequence
            ),
        )
        exchange_valid = owner._exchange_snapshot_valid(now)
        proof_sequence = truth.risk_snapshot_sequence
        if owner.flat_proof_engine is not None:
            exchange_valid = owner._capture_account_flat_proof("STOP", now)
            open_order_count, nonzero_position_count = 0, 0
            if exchange_valid:
                proof_sequence = max(
                    truth.risk_snapshot_sequence,
                    control.quiesce_snapshot_sequence + 1,
                )
            else:
                truth.exchange_reason = (
                    owner.last_flat_proof_error
                    or "account_wide_flat_proof_failed"
                )
        else:
            open_order_count, nonzero_position_count = (
                owner._account_truth_counts()
            )
    else:
        exchange_valid = False
        open_order_count = 0
        nonzero_position_count = 0
        proof_sequence = truth.risk_snapshot_sequence
    plan = owner.control.stop_plan(
        exchange_valid=exchange_valid,
        exchange_reason=truth.exchange_reason,
        open_order_count=open_order_count,
        nonzero_position_count=nonzero_position_count,
        risk_snapshot_sequence=proof_sequence,
    )

    if plan.action == "WAIT":
        return None
    if plan.action == "TAKEOVER":
        owner._takeover_from_quiesce(
            plan.transition_reason or plan.reason,
            "supervisor_stop_guard_takeover",
        )
        reason = plan.reason
    elif plan.action == "QUIESCE":
        persisted = owner._enter_quiesced(
            plan.reason,
            "supervisor_stop_quiesced",
        )
        accepted = bool(persisted)
        cancel_ok = True if cancel_requested else None
        reason = (
            "supervisor_stop_ack"
            if accepted
            else owner.state_persist_error
            or "stop_quiesce_state_persist_failed"
        )
    elif plan.action == "CANCEL":
        cancel_attempted = True
        cancel_ok = owner._emergency_cancel(now)
        reason = (
            "stop_after_cancel_requires_fresh_quiesce"
            if cancel_ok
            else owner.last_cancel_reason or "stop_cancel_failed"
        )
    else:
        reason = plan.reason

    owner.control.finish_stop(
        accepted=accepted,
        reason=reason,
        cancel_requested=cancel_requested,
        cancel_attempted=cancel_attempted,
        cancel_ok=cancel_ok,
    )
    return accepted


def check_rearm_safety(owner, now: float):
    truth = owner.observation
    gate = owner.control.rearm_control_gate(
        parent_heartbeat_error=owner.parent_heartbeat_error,
        parent_age_sec=max(0.0, now - owner.last_parent_heartbeat_at),
        parent_timeout_sec=owner.parent_heartbeat_timeout_sec,
    )
    if not gate[0]:
        return gate
    owner._service_exchange_risk(now, force=True)
    if truth.snapshot_worker is None:
        snapshot_fresh = truth.last_exchange_success_at == now
        snapshot_pending_reason = "exchange_snapshot_failed"
    else:
        snapshot_fresh = bool(
            truth.last_exchange_success_at > 0.0
            and now - truth.last_exchange_success_at
            <= owner.rearm_snapshot_max_age_sec
        )
        snapshot_pending_reason = "exchange_snapshot_refresh_pending"
    funding_action, funding_reason = owner._evaluate_funding_guard(now)
    if not owner._capture_account_flat_proof("REARM", now):
        return (
            False,
            owner.last_flat_proof_error
            or "account_wide_flat_proof_failed",
        )
    if owner.flat_proof_engine is not None:
        open_order_count, nonzero_position_count = 0, 0
    else:
        open_order_count, nonzero_position_count = (
            owner._account_truth_counts()
        )
    return owner.control.rearm_truth_gate(
        exchange_healthy=truth.exchange_healthy,
        exchange_reason=truth.exchange_reason,
        snapshot_fresh=snapshot_fresh,
        snapshot_pending_reason=snapshot_pending_reason,
        risk_action=truth.risk_action,
        risk_reason=truth.risk_reason,
        funding_action=funding_action,
        funding_reason=funding_reason,
        open_order_count=open_order_count,
        nonzero_position_count=nonzero_position_count,
    )


def rearm_proof_binding(owner, now: float | None = None) -> tuple | None:
    proof = owner.last_flat_proof
    if proof is None:
        return None
    version = owner._effective_state_version()
    effective_now = owner.clock.monotonic() if now is None else float(now)
    store = owner.state_store
    expected_scope = store.account_scope_id if store is not None else ""
    if (
        proof.purpose != "REARM"
        or proof.deployment_id
        != owner.observation.account_risk.state.deployment_id
        or (expected_scope and proof.account_scope_id != expected_scope)
        or not proof.is_valid(effective_now, version)
    ):
        return None
    return (
        version.writer_epoch,
        version.owner_epoch,
        version.safety_epoch,
        version.generation,
        version.state_sha256,
        proof.proof_id,
        proof.last_truth_sequence,
        proof.snapshot_digest,
    )


def prepare_rearm(owner, request_id: str, reason: str, now: float | None):
    now = owner.clock.monotonic() if now is None else float(now)
    request_id = str(request_id or "")
    safety = (
        check_rearm_safety(owner, now)
        if request_id
        else (False, "request_id_missing")
    )
    return owner.control.prepare_rearm(
        request_id,
        reason,
        now,
        safety,
        rearm_proof_binding(owner, now),
    )


def commit_rearm(owner, request_id: str, token: str, now: float | None):
    now = owner.clock.monotonic() if now is None else float(now)
    request_id = str(request_id or "")
    valid, refusal_reason = owner.control.validate_rearm_commit(
        request_id,
        token,
        now,
        rearm_proof_binding(owner, now),
    )
    if not valid:
        return False, refusal_reason
    safe, refusal_reason = check_rearm_safety(owner, now)
    if not safe:
        return owner.control.reject_rearm_commit(
            request_id,
            refusal_reason,
        )

    checkpoint = owner.control.begin_rearm_commit()
    truth = owner.observation
    previous_risk_action = truth.risk_action
    previous_risk_reason = truth.risk_reason
    truth.risk_action = "NONE"
    truth.risk_reason = ""
    persisted = owner._persist_durable_state(
        "operator_rearm_committed",
        force=True,
    )
    if not persisted:
        truth.risk_action = previous_risk_action
        truth.risk_reason = previous_risk_reason
        return owner.control.finish_rearm_commit(
            request_id,
            False,
            owner.state_persist_error or "state_persist_failed",
            checkpoint,
        )
    owner.state_load_error = ""
    return owner.control.finish_rearm_commit(
        request_id,
        True,
        "rearm_committed",
        checkpoint,
    )
