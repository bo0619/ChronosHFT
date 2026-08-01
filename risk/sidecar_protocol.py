"""Pure message validation and ACK decoding for sidecar IPC."""

from __future__ import annotations

from typing import Callable


class SidecarProtocol:
    @staticmethod
    def validate_status(
        status: dict,
        finite_float: Callable[[object, str], float],
    ) -> dict:
        if not isinstance(status, dict):
            raise ValueError("status_not_object")
        sequence = status.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise ValueError("status_sequence_invalid")
        healthy = status.get("healthy")
        if not isinstance(healthy, bool):
            raise ValueError("status_healthy_invalid")
        if not isinstance(status.get("reason", ""), str):
            raise ValueError("status_reason_invalid")
        for field in ("exchange_healthy", "kill_latched", "quiesced", "state_recovered"):
            if field in status and not isinstance(status[field], bool):
                raise ValueError(f"status_{field}_invalid")
        for field in (
            "risk_snapshot_sequence",
            "state_generation",
            "parent_sequence",
            "flat_verification_count",
        ):
            value = status.get(field)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"status_{field}_invalid")
        for field in (
            "reported_at",
            "risk_snapshot_captured_at",
            "risk_snapshot_captured_monotonic",
            "quiesced_at",
        ):
            if field not in status:
                continue
            if finite_float(status[field], f"status.{field}") < 0.0:
                raise ValueError(f"status_{field}_invalid")
        risk_action = str(status.get("risk_action", "NONE") or "NONE").upper()
        if risk_action not in {"NONE", "REDUCE_ONLY", "KILL"}:
            raise ValueError("status_risk_action_invalid")
        stage = str(status.get("stage", "") or "").upper()
        if stage and stage not in {
            "ARMED", "CANCEL_PENDING", "CANCEL_VERIFIED", "FLATTENING",
            "FLAT_VERIFIED", "FAILED", "QUIESCED",
        }:
            raise ValueError("status_stage_invalid")
        if "risk_metrics" in status and not isinstance(status["risk_metrics"], dict):
            raise ValueError("status_risk_metrics_invalid")
        if healthy and (
            risk_action != "NONE"
            or stage != "ARMED"
            or status.get("exchange_healthy") is not True
            or status.get("kill_latched", False) is not False
            or status.get("quiesced", False) is not False
            or int(status.get("risk_snapshot_sequence", 0) or 0) <= 0
            or float(status.get("risk_snapshot_captured_monotonic", 0.0) or 0.0) <= 0.0
        ):
            raise ValueError("status_healthy_state_inconsistent")
        return dict(status)

    @staticmethod
    def control_failure(command_type, last_status, failure_reason, request_id="", **payload):
        command_type = str(command_type or "").upper()
        result = {
            "accepted": False,
            "reason": str(failure_reason or "supervisor_control_failed"),
            "request_id": str(request_id or ""),
        }
        if command_type == "QUIESCE":
            result.update(quiesced=bool(last_status.get("quiesced", False)), persisted=False)
        elif command_type == "RESUME_SHUTDOWN":
            result.update(
                quiesced=bool(last_status.get("quiesced", False)),
                kill_latched=bool(last_status.get("kill_latched", False)),
                persisted=False,
            )
        elif command_type == "STOP":
            result.update(
                quiesced=bool(last_status.get("quiesced", False)),
                cancel_requested=bool(payload.get("cancel_orders", True)),
                cancel_attempted=False,
                cancel_ok=None,
            )
        else:
            result["token"] = ""
        return result

    @staticmethod
    def read_control_ack(command_type, request_id, status):
        command_type = str(command_type or "").upper()
        if command_type == "QUIESCE":
            if str(status.get("last_quiesce_request_id", "") or "") != request_id:
                return None
            return {
                "accepted": bool(status.get("last_quiesce_accepted", False)),
                "reason": str(status.get("last_quiesce_reason", "") or ""),
                "request_id": request_id,
                "quiesced": bool(status.get("quiesced", False)),
                "persisted": bool(status.get("last_quiesce_persisted", False)),
            }
        if command_type == "RESUME_SHUTDOWN":
            if str(status.get("last_shutdown_resume_request_id", "") or "") != request_id:
                return None
            return {
                "accepted": bool(status.get("last_shutdown_resume_accepted", False)),
                "reason": str(status.get("last_shutdown_resume_reason", "") or ""),
                "request_id": request_id,
                "quiesced": bool(status.get("quiesced", False)),
                "kill_latched": bool(status.get("kill_latched", False)),
                "persisted": bool(status.get("last_shutdown_resume_persisted", False)),
            }
        if command_type == "STOP":
            if str(status.get("last_stop_request_id", "") or "") != request_id:
                return None
            return {
                "accepted": bool(status.get("last_stop_accepted", False)),
                "reason": str(status.get("last_stop_reason", "") or ""),
                "request_id": request_id,
                "quiesced": bool(status.get("last_stop_quiesced", False)),
                "cancel_requested": bool(status.get("last_stop_cancel_requested", False)),
                "cancel_attempted": bool(status.get("last_stop_cancel_attempted", False)),
                "cancel_ok": status.get("last_stop_cancel_ok"),
            }
        if str(status.get("last_rearm_request_id", "") or "") != request_id:
            return None
        return {
            "accepted": bool(status.get("last_rearm_accepted", False)),
            "reason": str(status.get("last_rearm_reason", "") or ""),
            "request_id": request_id,
            "token": str(status.get("last_rearm_token", "") or ""),
        }
