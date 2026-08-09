"""Read-only status projection for the child risk-sidecar core."""

from dataclasses import asdict

from risk.exchange_port import StateVersion


class RiskSidecarStatusProjection:
    @staticmethod
    def build(
        owner,
        healthy: bool,
        reason: str,
        action: str,
        now: float,
    ) -> dict:
        control = owner.control
        state = control.state
        truth = owner.observation
        funding = truth.funding_risk
        state_version = getattr(
            owner,
            "state_version",
            StateVersion(0, 0, 0, int(owner.state_generation), ""),
        )
        flat_proof = getattr(owner, "last_flat_proof", None)
        return {
            "healthy": bool(healthy),
            "reason": str(reason or ""),
            "risk_action": str(action or "NONE"),
            "risk_reason": truth.risk_reason,
            "funding_action": funding.action,
            "funding_reason": funding.reason,
            "kill_latched": state.kill_latched,
            "kill_reason": state.kill_reason,
            "quiesced": state.quiesced,
            "quiesce_reason": state.quiesce_reason,
            "quiesced_at": state.quiesced_at,
            "stage": state.stage,
            "state_path": owner.state_path,
            "state_generation": owner.state_generation,
            "writer_epoch": state_version.writer_epoch,
            "owner_epoch": state_version.owner_epoch,
            "safety_epoch": state_version.safety_epoch,
            "state_sha256": state_version.state_sha256,
            "state_store_v2": getattr(owner, "state_store", None) is not None,
            "state_recovered": owner.state_recovered,
            "state_load_error": owner.state_load_error,
            "state_persist_error": owner.state_persist_error,
            "risk_metrics": dict(truth.risk_metrics),
            "parent_sequence": owner.last_parent_sequence,
            "parent_age_sec": max(
                0.0,
                now - owner.last_parent_heartbeat_at,
            ),
            "parent_heartbeat_error": owner.parent_heartbeat_error,
            "parent_stale_since": owner.parent_stale_since,
            "parent_stale_snapshot_sequence": (
                owner.parent_stale_snapshot_sequence
            ),
            "parent_heartbeat_sent_monotonic": (
                owner.last_parent_heartbeat_sent_monotonic
            ),
            "exchange_healthy": bool(truth.exchange_healthy),
            "exchange_reason": truth.exchange_reason,
            "exchange_age_sec": (
                max(0.0, now - truth.last_exchange_success_at)
                if truth.last_exchange_success_at > 0.0
                else None
            ),
            "last_cancel_ok": owner.last_cancel_ok,
            "last_cancel_reason": owner.last_cancel_reason,
            "last_flatten_ok": owner.last_flatten_ok,
            "last_flatten_count": owner.last_flatten_count,
            "last_flatten_reason": owner.last_flatten_reason,
            "flat_verification_count": state.flat_verification_count,
            "flat_verification_checks": control.flat_verification_checks,
            "last_verified_snapshot_sequence": (
                state.last_verified_snapshot_sequence
            ),
            "last_flat_proof": (
                asdict(flat_proof)
                if flat_proof is not None
                else None
            ),
            "last_flat_proof_error": str(
                getattr(owner, "last_flat_proof_error", "") or ""
            ),
            "risk_snapshot_sequence": truth.risk_snapshot_sequence,
            "quiesce_snapshot_sequence": state.quiesce_snapshot_sequence,
            "risk_snapshot_captured_at": truth.risk_snapshot_captured_at,
            "risk_snapshot_captured_monotonic": (
                truth.risk_snapshot_captured_monotonic
            ),
            "risk_snapshot_age_sec": (
                max(
                    0.0,
                    now - truth.risk_snapshot_captured_monotonic,
                )
                if truth.risk_snapshot_captured_monotonic > 0.0
                else None
            ),
            "risk_snapshot_worker_inflight": bool(
                truth.snapshot_request_inflight_sequence > 0
            ),
            "last_rearm_request_id": state.last_rearm_request_id,
            "last_rearm_phase": state.last_rearm_phase,
            "last_rearm_accepted": state.last_rearm_accepted,
            "last_rearm_reason": state.last_rearm_reason,
            "last_rearm_token": state.last_rearm_token,
            "last_quiesce_request_id": state.last_quiesce_request_id,
            "last_quiesce_accepted": state.last_quiesce_accepted,
            "last_quiesce_reason": state.last_quiesce_reason,
            "last_quiesce_persisted": state.last_quiesce_persisted,
            "last_shutdown_resume_request_id": (
                state.last_shutdown_resume_request_id
            ),
            "last_shutdown_resume_accepted": (
                state.last_shutdown_resume_accepted
            ),
            "last_shutdown_resume_reason": (
                state.last_shutdown_resume_reason
            ),
            "last_shutdown_resume_persisted": (
                state.last_shutdown_resume_persisted
            ),
            "last_stop_request_id": state.last_stop_request_id,
            "last_stop_accepted": state.last_stop_accepted,
            "last_stop_reason": state.last_stop_reason,
            "last_stop_quiesced": state.last_stop_quiesced,
            "last_stop_cancel_requested": (
                state.last_stop_cancel_requested
            ),
            "last_stop_cancel_attempted": (
                state.last_stop_cancel_attempted
            ),
            "last_stop_cancel_ok": state.last_stop_cancel_ok,
        }
