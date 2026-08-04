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
            "risk_reason": owner.risk_reason,
            "funding_action": owner.funding_action,
            "funding_reason": owner.funding_reason,
            "kill_latched": owner.kill_latched,
            "kill_reason": owner.kill_reason,
            "quiesced": owner.quiesced,
            "quiesce_reason": owner.quiesce_reason,
            "quiesced_at": owner.quiesced_at,
            "stage": owner.stage,
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
            "risk_metrics": dict(owner.risk_metrics),
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
            "exchange_healthy": bool(owner.exchange_healthy),
            "exchange_reason": owner.exchange_reason,
            "exchange_age_sec": (
                max(0.0, now - owner.last_exchange_success_at)
                if owner.last_exchange_success_at > 0.0
                else None
            ),
            "last_cancel_ok": owner.last_cancel_ok,
            "last_cancel_reason": owner.last_cancel_reason,
            "last_flatten_ok": owner.last_flatten_ok,
            "last_flatten_count": owner.last_flatten_count,
            "last_flatten_reason": owner.last_flatten_reason,
            "flat_verification_count": owner.flat_verification_count,
            "flat_verification_checks": owner.flat_verification_checks,
            "last_verified_snapshot_sequence": (
                owner.last_verified_snapshot_sequence
            ),
            "last_flat_proof": (
                asdict(flat_proof)
                if flat_proof is not None
                else None
            ),
            "last_flat_proof_error": str(
                getattr(owner, "last_flat_proof_error", "") or ""
            ),
            "risk_snapshot_sequence": owner.risk_snapshot_sequence,
            "quiesce_snapshot_sequence": owner.quiesce_snapshot_sequence,
            "risk_snapshot_captured_at": owner.risk_snapshot_captured_at,
            "risk_snapshot_captured_monotonic": (
                owner.risk_snapshot_captured_monotonic
            ),
            "risk_snapshot_age_sec": (
                max(
                    0.0,
                    now - owner.risk_snapshot_captured_monotonic,
                )
                if owner.risk_snapshot_captured_monotonic > 0.0
                else None
            ),
            "risk_snapshot_worker_inflight": bool(
                owner.snapshot_request_inflight_sequence > 0
            ),
            "last_rearm_request_id": owner.last_rearm_request_id,
            "last_rearm_phase": owner.last_rearm_phase,
            "last_rearm_accepted": owner.last_rearm_accepted,
            "last_rearm_reason": owner.last_rearm_reason,
            "last_rearm_token": owner.last_rearm_token,
            "last_quiesce_request_id": owner.last_quiesce_request_id,
            "last_quiesce_accepted": owner.last_quiesce_accepted,
            "last_quiesce_reason": owner.last_quiesce_reason,
            "last_quiesce_persisted": owner.last_quiesce_persisted,
            "last_shutdown_resume_request_id": (
                owner.last_shutdown_resume_request_id
            ),
            "last_shutdown_resume_accepted": (
                owner.last_shutdown_resume_accepted
            ),
            "last_shutdown_resume_reason": (
                owner.last_shutdown_resume_reason
            ),
            "last_shutdown_resume_persisted": (
                owner.last_shutdown_resume_persisted
            ),
            "last_stop_request_id": owner.last_stop_request_id,
            "last_stop_accepted": owner.last_stop_accepted,
            "last_stop_reason": owner.last_stop_reason,
            "last_stop_quiesced": owner.last_stop_quiesced,
            "last_stop_cancel_requested": (
                owner.last_stop_cancel_requested
            ),
            "last_stop_cancel_attempted": (
                owner.last_stop_cancel_attempted
            ),
            "last_stop_cancel_ok": owner.last_stop_cancel_ok,
        }
