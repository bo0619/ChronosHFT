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


class AdminControlServer:
    def __init__(self, oms, config=None, admin_dir: str = ""):
        self.oms = oms
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
            accepted = bool(self.oms.rearm_system(reason))
            status = "ok" if accepted else "rejected"
            message = "OMS rearm completed." if accepted else "OMS refused the rearm request."
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
        state = getattr(getattr(self.oms, "state", None), "value", str(getattr(self.oms, "state", "")))
        capability_mode = getattr(getattr(self.oms, "capability_mode", None), "value", str(getattr(self.oms, "capability_mode", "")))
        return {
            "state": state,
            "capability_mode": capability_mode,
            "capability_reason": str(getattr(self.oms, "capability_reason", "") or ""),
            "manual_rearm_required": bool(getattr(self.oms, "manual_rearm_required", False)),
            "last_halt_reason": str(getattr(self.oms, "last_halt_reason", "") or ""),
            "last_freeze_reason": str(getattr(self.oms, "last_freeze_reason", "") or ""),
        }

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
