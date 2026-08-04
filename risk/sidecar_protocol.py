"""Pure message validation and ACK decoding for sidecar IPC."""

from __future__ import annotations

import math
from typing import Callable


class SidecarProtocol:
    VERSION = 2
    PARENT_CAPABILITIES = frozenset(
        {
            "control.request.v1",
            "heartbeat.latest.v1",
            "state.version.v2",
        }
    )
    CHILD_CAPABILITIES = frozenset(
        {
            "control.ack.v1",
            "status.safety.v1",
            "account.flat-proof.v1",
            "cash-flow.split.v1",
            "state.cas.v2",
        }
    )
    REQUIRED_PARENT_CAPABILITIES = PARENT_CAPABILITIES
    REQUIRED_CHILD_CAPABILITIES = CHILD_CAPABILITIES

    _CONTROL_STRING_FIELDS = {
        "QUIESCE": ("request_id", "reason"),
        "RESUME_SHUTDOWN": ("request_id", "reason"),
        "STOP": ("request_id",),
        "PREPARE_REARM": ("request_id", "reason"),
        "COMMIT_REARM": ("request_id", "token"),
        "ABORT_REARM": ("token",),
    }
    _BOOLEAN_STATUS_FIELDS = (
        "exchange_healthy",
        "kill_latched",
        "quiesced",
        "state_recovered",
        "state_store_v2",
        "last_quiesce_persisted",
        "last_shutdown_resume_persisted",
        "last_stop_quiesced",
        "last_stop_cancel_requested",
        "last_stop_cancel_attempted",
    )
    _OPTIONAL_BOOLEAN_STATUS_FIELDS = (
        "last_quiesce_accepted",
        "last_shutdown_resume_accepted",
        "last_stop_accepted",
        "last_rearm_accepted",
    )

    @classmethod
    def _validate_version(cls, value, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{label}_protocol_version_invalid")
        if value != cls.VERSION:
            raise ValueError(
                f"{label}_protocol_version_incompatible:"
                f"expected={cls.VERSION}:received={value}"
            )
        return value

    @staticmethod
    def _validate_capabilities(value, label: str) -> frozenset[str]:
        if not isinstance(value, list):
            raise ValueError(f"{label}_capabilities_invalid")
        capabilities = []
        for capability in value:
            if not isinstance(capability, str) or not capability:
                raise ValueError(f"{label}_capabilities_invalid")
            capabilities.append(capability)
        if len(capabilities) != len(set(capabilities)):
            raise ValueError(f"{label}_capabilities_invalid")
        return frozenset(capabilities)

    @staticmethod
    def _missing_capabilities(
        required: frozenset[str],
        offered: frozenset[str],
    ) -> str:
        return ",".join(sorted(required - offered))

    @classmethod
    def with_launch_contract(cls, settings: dict) -> dict:
        """Attach the parent side of the same-release spawn handshake."""
        return {
            **settings,
            "protocol_version": cls.VERSION,
            "parent_capabilities": sorted(cls.PARENT_CAPABILITIES),
            "required_child_capabilities": sorted(
                cls.REQUIRED_CHILD_CAPABILITIES
            ),
        }

    @classmethod
    def validate_launch_contract(cls, settings: dict) -> dict:
        if not isinstance(settings, dict):
            raise ValueError("launch_contract_not_object")
        cls._validate_version(
            settings.get("protocol_version"),
            "launch",
        )
        parent_capabilities = cls._validate_capabilities(
            settings.get("parent_capabilities"),
            "launch_parent",
        )
        missing_parent = cls._missing_capabilities(
            cls.REQUIRED_PARENT_CAPABILITIES,
            parent_capabilities,
        )
        if missing_parent:
            raise ValueError(
                "launch_parent_capabilities_missing:"
                f"{missing_parent}"
            )
        required_child = cls._validate_capabilities(
            settings.get("required_child_capabilities"),
            "launch_required_child",
        )
        missing_requirement = cls._missing_capabilities(
            cls.REQUIRED_CHILD_CAPABILITIES,
            required_child,
        )
        if missing_requirement:
            raise ValueError(
                "launch_required_child_capabilities_missing:"
                f"{missing_requirement}"
            )
        unsupported = required_child - cls.CHILD_CAPABILITIES
        if unsupported:
            raise ValueError(
                "launch_child_capabilities_unsupported:"
                f"{','.join(sorted(unsupported))}"
            )
        return dict(settings)

    @classmethod
    def parent_message(
        cls,
        message_type: str,
        session_id: str,
        **payload,
    ) -> dict:
        return {
            **payload,
            "protocol_version": cls.VERSION,
            "type": str(message_type or "").upper(),
            "session_id": str(session_id or ""),
        }

    @classmethod
    def child_status(
        cls,
        status: dict,
        *,
        handshake_complete: bool,
    ) -> dict:
        if not isinstance(handshake_complete, bool):
            raise ValueError("status_protocol_handshake_complete_invalid")
        return {
            **status,
            "protocol_version": cls.VERSION,
            "capabilities": sorted(cls.CHILD_CAPABILITIES),
            "protocol_handshake_complete": handshake_complete,
        }

    @classmethod
    def has_compatible_child_contract(cls, status: dict) -> bool:
        if not isinstance(status, dict):
            return False
        try:
            cls._validate_version(
                status.get("protocol_version"),
                "status",
            )
            capabilities = cls._validate_capabilities(
                status.get("capabilities"),
                "status",
            )
        except ValueError:
            return False
        return (
            status.get("protocol_handshake_complete") is True
            and cls.REQUIRED_CHILD_CAPABILITIES <= capabilities
        )

    @staticmethod
    def _read_bool(status: dict, field: str, default: bool = False) -> bool:
        value = status.get(field, default)
        if not isinstance(value, bool):
            raise ValueError(f"status_{field}_invalid")
        return value

    @staticmethod
    def _read_optional_bool(status: dict, field: str):
        value = status.get(field)
        if value is not None and not isinstance(value, bool):
            raise ValueError(f"status_{field}_invalid")
        return value

    @staticmethod
    def validate_heartbeat(message, session_id: str) -> dict | None:
        if not isinstance(message, dict):
            return None
        if message.get("session_id") != session_id:
            return None
        try:
            SidecarProtocol._validate_version(
                message.get("protocol_version"),
                "message",
            )
        except ValueError:
            return None
        message_type = message.get("type", "HEARTBEAT")
        if (
            not isinstance(message_type, str)
            or message_type.upper() != "HEARTBEAT"
        ):
            return None
        sequence = message.get("sequence")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence <= 0
        ):
            return None
        sent_monotonic = message.get("sent_monotonic")
        if isinstance(sent_monotonic, bool) or not isinstance(
            sent_monotonic,
            (int, float),
        ):
            return None
        if not math.isfinite(float(sent_monotonic)) or sent_monotonic <= 0.0:
            return None
        return dict(message)

    @classmethod
    def validate_control_command(cls, command, session_id: str) -> dict | None:
        if not isinstance(command, dict):
            return None
        if command.get("session_id") != session_id:
            return None
        try:
            cls._validate_version(
                command.get("protocol_version"),
                "message",
            )
        except ValueError:
            return None
        command_type = command.get("type")
        if not isinstance(command_type, str):
            return None
        command_type = command_type.upper()
        if command_type == "HEARTBEAT":
            if cls.validate_heartbeat(command, session_id) is None:
                return None
        elif command_type not in cls._CONTROL_STRING_FIELDS:
            return None
        for field in cls._CONTROL_STRING_FIELDS.get(command_type, ()):
            if field in command and not isinstance(command[field], str):
                return None
        if command_type == "STOP" and "cancel_orders" in command:
            if not isinstance(command["cancel_orders"], bool):
                return None
        return {**command, "type": command_type}

    @staticmethod
    def validate_status(
        status: dict,
        finite_float: Callable[[object, str], float],
    ) -> dict:
        if not isinstance(status, dict):
            raise ValueError("status_not_object")
        SidecarProtocol._validate_version(
            status.get("protocol_version"),
            "status",
        )
        capabilities = SidecarProtocol._validate_capabilities(
            status.get("capabilities"),
            "status",
        )
        if not isinstance(status.get("protocol_handshake_complete"), bool):
            raise ValueError("status_protocol_handshake_complete_invalid")
        missing_capabilities = SidecarProtocol._missing_capabilities(
            SidecarProtocol.REQUIRED_CHILD_CAPABILITIES,
            capabilities,
        )
        if missing_capabilities:
            raise ValueError(
                "status_capabilities_missing:"
                f"{missing_capabilities}"
            )
        sequence = status.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise ValueError("status_sequence_invalid")
        healthy = status.get("healthy")
        if not isinstance(healthy, bool):
            raise ValueError("status_healthy_invalid")
        if not isinstance(status.get("reason", ""), str):
            raise ValueError("status_reason_invalid")
        for field in SidecarProtocol._BOOLEAN_STATUS_FIELDS:
            if field in status and not isinstance(status[field], bool):
                raise ValueError(f"status_{field}_invalid")
        for field in SidecarProtocol._OPTIONAL_BOOLEAN_STATUS_FIELDS:
            if field in status:
                SidecarProtocol._read_optional_bool(status, field)
        if "last_stop_cancel_ok" in status:
            SidecarProtocol._read_optional_bool(status, "last_stop_cancel_ok")
        for field in (
            "risk_snapshot_sequence",
            "state_generation",
            "writer_epoch",
            "owner_epoch",
            "safety_epoch",
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
        if "last_flat_proof" in status and status["last_flat_proof"] is not None:
            if not isinstance(status["last_flat_proof"], dict):
                raise ValueError("status_last_flat_proof_invalid")
        if healthy and (
            status["protocol_handshake_complete"] is not True
            or risk_action != "NONE"
            or stage != "ARMED"
            or status.get("exchange_healthy") is not True
            or status.get("kill_latched", False) is not False
            or status.get("quiesced", False) is not False
            or int(status.get("risk_snapshot_sequence", 0) or 0) <= 0
            or float(status.get("risk_snapshot_captured_monotonic", 0.0) or 0.0) <= 0.0
        ):
            raise ValueError("status_healthy_state_inconsistent")
        if status.get("state_store_v2") is True and (
            status.get("state_recovered") is not True
            or int(status.get("writer_epoch", 0) or 0) <= 0
            or int(status.get("state_generation", 0) or 0) <= 0
            or len(str(status.get("state_sha256", "") or "")) != 64
        ):
            raise ValueError("status_state_store_v2_inconsistent")
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
        if command_type not in {
            "QUIESCE",
            "RESUME_SHUTDOWN",
            "STOP",
            "PREPARE_REARM",
            "COMMIT_REARM",
        }:
            raise ValueError("control_command_type_invalid")
        if command_type == "QUIESCE":
            if str(status.get("last_quiesce_request_id", "") or "") != request_id:
                return None
            return {
                "accepted": SidecarProtocol._read_bool(
                    status,
                    "last_quiesce_accepted",
                ),
                "reason": str(status.get("last_quiesce_reason", "") or ""),
                "request_id": request_id,
                "quiesced": SidecarProtocol._read_bool(status, "quiesced"),
                "persisted": SidecarProtocol._read_bool(
                    status,
                    "last_quiesce_persisted",
                ),
            }
        if command_type == "RESUME_SHUTDOWN":
            if str(status.get("last_shutdown_resume_request_id", "") or "") != request_id:
                return None
            return {
                "accepted": SidecarProtocol._read_bool(
                    status,
                    "last_shutdown_resume_accepted",
                ),
                "reason": str(status.get("last_shutdown_resume_reason", "") or ""),
                "request_id": request_id,
                "quiesced": SidecarProtocol._read_bool(status, "quiesced"),
                "kill_latched": SidecarProtocol._read_bool(
                    status,
                    "kill_latched",
                ),
                "persisted": SidecarProtocol._read_bool(
                    status,
                    "last_shutdown_resume_persisted",
                ),
            }
        if command_type == "STOP":
            if str(status.get("last_stop_request_id", "") or "") != request_id:
                return None
            return {
                "accepted": SidecarProtocol._read_bool(
                    status,
                    "last_stop_accepted",
                ),
                "reason": str(status.get("last_stop_reason", "") or ""),
                "request_id": request_id,
                "quiesced": SidecarProtocol._read_bool(
                    status,
                    "last_stop_quiesced",
                ),
                "cancel_requested": SidecarProtocol._read_bool(
                    status,
                    "last_stop_cancel_requested",
                ),
                "cancel_attempted": SidecarProtocol._read_bool(
                    status,
                    "last_stop_cancel_attempted",
                ),
                "cancel_ok": SidecarProtocol._read_optional_bool(
                    status,
                    "last_stop_cancel_ok",
                ),
            }
        if str(status.get("last_rearm_request_id", "") or "") != request_id:
            return None
        return {
            "accepted": SidecarProtocol._read_bool(
                status,
                "last_rearm_accepted",
            ),
            "reason": str(status.get("last_rearm_reason", "") or ""),
            "request_id": request_id,
            "token": str(status.get("last_rearm_token", "") or ""),
        }
