import json
import os
import time
import uuid
from datetime import datetime


DEFAULT_ADMIN_DIR = os.path.join("storage", "admin")


def _ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)
    return path


def _utc_now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def resolve_admin_paths(config: dict = None, override_dir: str = "") -> dict:
    base_dir = str(override_dir or "").strip()
    if not base_dir:
        base_dir = (
            ((config or {}).get("system", {}) or {})
            .get("admin_control", {})
            .get("path", DEFAULT_ADMIN_DIR)
        )
    base_dir = os.path.abspath(base_dir)
    inbox_dir = _ensure_dir(os.path.join(base_dir, "inbox"))
    results_dir = _ensure_dir(os.path.join(base_dir, "results"))
    archive_dir = _ensure_dir(os.path.join(base_dir, "archive"))
    return {
        "base_dir": base_dir,
        "inbox_dir": inbox_dir,
        "results_dir": results_dir,
        "archive_dir": archive_dir,
    }


def submit_admin_command(
    action: str,
    reason: str = "",
    config: dict = None,
    admin_dir: str = "",
    wait_timeout_sec: float = 5.0,
):
    paths = resolve_admin_paths(config, admin_dir)
    command_id = uuid.uuid4().hex
    payload = {
        "id": command_id,
        "action": str(action or "").strip().lower(),
        "reason": str(reason or "").strip(),
        "created_at": _utc_now_iso(),
    }
    command_path = os.path.join(paths["inbox_dir"], f"{command_id}.json")
    result_path = os.path.join(paths["results_dir"], f"{command_id}.json")

    with open(command_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True)

    deadline = time.time() + max(0.0, float(wait_timeout_sec or 0.0))
    while time.time() <= deadline:
        if os.path.exists(result_path):
            with open(result_path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        time.sleep(0.1)

    return {
        "id": command_id,
        "accepted": False,
        "status": "timeout",
        "message": "No running process acknowledged the admin command before timeout.",
        "result_path": result_path,
    }


def coordinated_rearm(
    oms,
    reason: str,
    risk_manager=None,
    risk_supervisor=None,
) -> dict:
    reason = str(reason or "operator_rearm")
    can_rearm = getattr(risk_manager, "can_operator_rearm", None)
    if callable(can_rearm) and not bool(can_rearm()):
        return {
            "accepted": False,
            "reason": "risk_manager_flat_state_not_verified",
        }

    token = ""
    prepare = getattr(risk_supervisor, "prepare_rearm", None)
    if callable(prepare):
        prepared = prepare(reason)
        if not bool(prepared.get("accepted", False)):
            return {
                "accepted": False,
                "reason": str(
                    prepared.get("reason", "sidecar_rearm_prepare_failed")
                    or "sidecar_rearm_prepare_failed"
                ),
            }
        token = str(prepared.get("token", "") or "")

    if not bool(oms.rearm_system(reason)):
        abort = getattr(risk_supervisor, "abort_rearm", None)
        if callable(abort) and token:
            abort(token)
        return {"accepted": False, "reason": "oms_rearm_refused"}

    commit = getattr(risk_supervisor, "commit_rearm", None)
    if callable(commit):
        committed = commit(token)
        if not bool(committed.get("accepted", False)):
            failure_reason = str(
                committed.get("reason", "sidecar_rearm_commit_failed")
                or "sidecar_rearm_commit_failed"
            )
            trigger_kill = getattr(risk_manager, "trigger_kill_switch", None)
            if callable(trigger_kill):
                trigger_kill(f"IndependentSupervisor rearm failed: {failure_reason}")
            halt = getattr(oms, "halt_system", None)
            if callable(halt):
                halt(f"IndependentSupervisor rearm failed: {failure_reason}")
            return {"accepted": False, "reason": failure_reason}

    acknowledge = getattr(
        risk_manager,
        "acknowledge_operator_rearm",
        None,
    )
    if callable(acknowledge) and not bool(acknowledge()):
        halt = getattr(oms, "halt_system", None)
        if callable(halt):
            halt("RiskManager refused operator rearm acknowledgement")
        return {
            "accepted": False,
            "reason": "risk_manager_rearm_acknowledgement_failed",
        }
    return {"accepted": True, "reason": "coordinated_rearm_completed"}


class AdminControlServer:
    def __init__(
        self,
        oms,
        config=None,
        admin_dir: str = "",
        risk_manager=None,
        risk_supervisor=None,
    ):
        self.oms = oms
        self.risk_manager = risk_manager
        self.risk_supervisor = risk_supervisor
        self.paths = resolve_admin_paths(config or {}, admin_dir)

    def poll_once(self):
        inbox_dir = self.paths["inbox_dir"]
        for name in sorted(os.listdir(inbox_dir)):
            if not name.endswith(".json"):
                continue
            command_path = os.path.join(inbox_dir, name)
            self._process_command_file(command_path)

    def _process_command_file(self, command_path: str):
        try:
            with open(command_path, "r", encoding="utf-8") as handle:
                command = json.load(handle)
        except Exception as exc:
            self._write_result(
                command_id=os.path.splitext(os.path.basename(command_path))[0],
                accepted=False,
                status="invalid",
                message=f"Failed to load admin command: {exc}",
            )
            self._archive_command(command_path)
            return

        command_id = str(command.get("id", "") or os.path.splitext(os.path.basename(command_path))[0])
        action = str(command.get("action", "") or "").strip().lower()
        reason = str(command.get("reason", "") or "").strip() or "admin"

        if action == "rearm":
            rearm_result = coordinated_rearm(
                self.oms,
                reason,
                risk_manager=self.risk_manager,
                risk_supervisor=self.risk_supervisor,
            )
            accepted = bool(rearm_result.get("accepted", False))
            status = "ok" if accepted else "rejected"
            message = (
                "Coordinated OMS and risk rearm completed."
                if accepted
                else "Rearm refused: "
                + str(rearm_result.get("reason", "unknown"))
            )
            snapshot = self._status_snapshot()
            self._write_result(
                command_id=command_id,
                accepted=accepted,
                status=status,
                message=message,
                snapshot=snapshot,
            )
            self._archive_command(command_path)
            return

        if action == "status":
            self._write_result(
                command_id=command_id,
                accepted=True,
                status="ok",
                message="OMS status snapshot.",
                snapshot=self._status_snapshot(),
            )
            self._archive_command(command_path)
            return

        self._write_result(
            command_id=command_id,
            accepted=False,
            status="unsupported",
            message=f"Unsupported admin action: {action or 'empty'}",
            snapshot=self._status_snapshot(),
        )
        self._archive_command(command_path)

    def _status_snapshot(self):
        state = getattr(
            getattr(self.oms, "state", None),
            "value",
            str(getattr(self.oms, "state", "")),
        )
        capability_mode = getattr(
            getattr(self.oms, "capability_mode", None),
            "value",
            str(getattr(self.oms, "capability_mode", "")),
        )
        snapshot = {
            "state": state,
            "capability_mode": capability_mode,
            "capability_reason": str(
                getattr(self.oms, "capability_reason", "") or ""
            ),
            "manual_rearm_required": bool(
                getattr(self.oms, "manual_rearm_required", False)
            ),
            "last_halt_reason": str(
                getattr(self.oms, "last_halt_reason", "") or ""
            ),
            "last_freeze_reason": str(
                getattr(self.oms, "last_freeze_reason", "") or ""
            ),
        }
        capability_snapshot = getattr(self.oms, "get_capability_snapshot", None)
        if callable(capability_snapshot):
            try:
                snapshot["oms"] = capability_snapshot()
            except Exception as exc:
                snapshot["oms"] = {
                    "snapshot_error": f"{type(exc).__name__}:{exc}"
                }

        risk_snapshot = getattr(self.risk_manager, "get_status_snapshot", None)
        if callable(risk_snapshot):
            try:
                snapshot["risk"] = risk_snapshot()
            except Exception as exc:
                snapshot["risk"] = {
                    "snapshot_error": f"{type(exc).__name__}:{exc}"
                }

        supervisor_snapshot = getattr(
            self.risk_supervisor,
            "get_status_snapshot",
            None,
        )
        if callable(supervisor_snapshot):
            try:
                snapshot["risk_supervisor"] = supervisor_snapshot()
            except Exception as exc:
                snapshot["risk_supervisor"] = {
                    "snapshot_error": f"{type(exc).__name__}:{exc}"
                }
        return snapshot

    def _write_result(self, command_id: str, accepted: bool, status: str, message: str, snapshot=None):
        result_path = os.path.join(self.paths["results_dir"], f"{command_id}.json")
        payload = {
            "id": command_id,
            "accepted": bool(accepted),
            "status": status,
            "message": message,
            "handled_at": _utc_now_iso(),
            "snapshot": snapshot or {},
        }
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True)

    def _archive_command(self, command_path: str):
        archive_name = os.path.basename(command_path)
        archive_path = os.path.join(self.paths["archive_dir"], archive_name)
        try:
            if os.path.exists(archive_path):
                os.remove(archive_path)
            os.replace(command_path, archive_path)
        except OSError:
            try:
                os.remove(command_path)
            except OSError:
                pass
