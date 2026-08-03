"""Read-only parent-side status projection for the risk sidecar."""


class SidecarStatusProjection:
    @staticmethod
    def build(
        *,
        enabled: bool,
        process,
        parent_heartbeat_suspended_reason: str,
        last_status: dict,
        last_status_received_at: float,
        last_status_protocol_error: str,
        status_max_age_sec: float,
        settings: dict,
        now: float,
    ) -> dict:
        process_alive = bool(process is not None and process.is_alive())
        status_age = (
            max(0.0, now - last_status_received_at)
            if last_status_received_at > 0.0
            else None
        )
        return {
            "enabled": enabled,
            "process_alive": process_alive,
            "parent_heartbeat_suspended_reason": (
                parent_heartbeat_suspended_reason
            ),
            "pid": getattr(process, "pid", None),
            "healthy": bool(
                not enabled
                or (
                    process_alive
                    and last_status.get("healthy", False)
                    and status_age is not None
                    and status_age <= status_max_age_sec
                )
            ),
            "reason": str(last_status.get("reason", "") or ""),
            "status_protocol_error": last_status_protocol_error,
            "status_age_sec": status_age,
            "parent_sequence": int(
                last_status.get("parent_sequence", 0) or 0
            ),
            "parent_heartbeat_error": str(
                last_status.get("parent_heartbeat_error", "") or ""
            ),
            "exchange_healthy": bool(
                last_status.get("exchange_healthy", False)
            ),
            "last_cancel_ok": last_status.get("last_cancel_ok"),
            "last_cancel_reason": str(
                last_status.get("last_cancel_reason", "") or ""
            ),
            "risk_action": str(
                last_status.get("risk_action", "NONE") or "NONE"
            ),
            "risk_reason": str(
                last_status.get("risk_reason", "") or ""
            ),
            "funding_action": str(
                last_status.get("funding_action", "NONE") or "NONE"
            ),
            "funding_reason": str(
                last_status.get("funding_reason", "") or ""
            ),
            "stage": str(last_status.get("stage", "") or ""),
            "kill_latched": bool(
                last_status.get("kill_latched", False)
            ),
            "kill_reason": str(
                last_status.get("kill_reason", "") or ""
            ),
            "quiesced": bool(last_status.get("quiesced", False)),
            "quiesce_reason": str(
                last_status.get("quiesce_reason", "") or ""
            ),
            "quiesced_at": float(
                last_status.get("quiesced_at", 0.0) or 0.0
            ),
            "state_path": str(
                last_status.get(
                    "state_path",
                    settings.get("state_path", ""),
                )
                or ""
            ),
            "state_generation": int(
                last_status.get("state_generation", 0) or 0
            ),
            "state_recovered": bool(
                last_status.get("state_recovered", False)
            ),
            "state_load_error": str(
                last_status.get("state_load_error", "") or ""
            ),
            "state_persist_error": str(
                last_status.get("state_persist_error", "") or ""
            ),
            "risk_metrics": dict(
                last_status.get("risk_metrics", {}) or {}
            ),
            "risk_snapshot_sequence": int(
                last_status.get("risk_snapshot_sequence", 0) or 0
            ),
            "risk_snapshot_captured_at": float(
                last_status.get("risk_snapshot_captured_at", 0.0) or 0.0
            ),
            "risk_snapshot_captured_monotonic": float(
                last_status.get(
                    "risk_snapshot_captured_monotonic",
                    0.0,
                )
                or 0.0
            ),
            "risk_snapshot_age_sec": last_status.get(
                "risk_snapshot_age_sec"
            ),
            "risk_snapshot_worker_inflight": bool(
                last_status.get(
                    "risk_snapshot_worker_inflight",
                    False,
                )
            ),
            "last_flatten_ok": last_status.get("last_flatten_ok"),
            "last_flatten_count": int(
                last_status.get("last_flatten_count", 0) or 0
            ),
            "last_flatten_reason": str(
                last_status.get("last_flatten_reason", "") or ""
            ),
            "last_quiesce_request_id": str(
                last_status.get("last_quiesce_request_id", "") or ""
            ),
            "last_quiesce_accepted": last_status.get(
                "last_quiesce_accepted"
            ),
            "last_quiesce_reason": str(
                last_status.get("last_quiesce_reason", "") or ""
            ),
            "last_quiesce_persisted": bool(
                last_status.get("last_quiesce_persisted", False)
            ),
            "last_shutdown_resume_request_id": str(
                last_status.get(
                    "last_shutdown_resume_request_id",
                    "",
                )
                or ""
            ),
            "last_shutdown_resume_accepted": last_status.get(
                "last_shutdown_resume_accepted"
            ),
            "last_shutdown_resume_reason": str(
                last_status.get("last_shutdown_resume_reason", "") or ""
            ),
            "last_shutdown_resume_persisted": bool(
                last_status.get("last_shutdown_resume_persisted", False)
            ),
            "last_stop_request_id": str(
                last_status.get("last_stop_request_id", "") or ""
            ),
            "last_stop_accepted": last_status.get("last_stop_accepted"),
            "last_stop_reason": str(
                last_status.get("last_stop_reason", "") or ""
            ),
            "last_stop_quiesced": bool(
                last_status.get("last_stop_quiesced", False)
            ),
            "last_stop_cancel_requested": bool(
                last_status.get("last_stop_cancel_requested", False)
            ),
            "last_stop_cancel_attempted": bool(
                last_status.get("last_stop_cancel_attempted", False)
            ),
            "last_stop_cancel_ok": last_status.get(
                "last_stop_cancel_ok"
            ),
        }
