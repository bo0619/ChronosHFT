import json
import math
import os
import re
import time
import uuid
from datetime import datetime, timezone


DEFAULT_ADMIN_DIR = os.path.join("storage", "admin")
DEFAULT_COMMAND_TTL_SEC = 10.0
DEFAULT_SESSION_MAX_AGE_SEC = 2.0
DEFAULT_MAX_RETAINED_RESULTS = 256
DEFAULT_MAX_RETAINED_ARCHIVES = 256
DEFAULT_RETENTION_MAX_AGE_SEC = 7 * 24 * 60 * 60.0
ATOMIC_REPLACE_MAX_ATTEMPTS = 5
ATOMIC_REPLACE_RETRY_BASE_SEC = 0.01
TRANSIENT_WINDOWS_REPLACE_ERRORS = frozenset({5, 32, 33})
ADMIN_SESSION_SCHEMA = "chronoshft.admin_session.v1"
ADMIN_COMMAND_SCHEMA = "chronoshft.admin_command.v1"
_WINDOWS_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")
_COMMAND_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON number {value!r} is not allowed")


def _reject_duplicate_json_keys(pairs):
    payload = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON object key {key!r} is not allowed")
        payload[key] = value
    return payload


def _load_strict_json_object(path: str, label: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(
                handle,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_json_keys,
            )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label} at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return payload


def _validate_admin_control_path(value, *, base_dir: str = "") -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(
            "system.admin_control.path must be a non-empty text path"
        )
    if "\x00" in value:
        raise ValueError("system.admin_control.path must not contain NUL")
    if os.path.expanduser(value) != value or os.path.expandvars(value) != value:
        raise ValueError(
            "system.admin_control.path must not use user or environment expansion"
        )

    windows_value = value.replace("/", "\\")
    if windows_value.startswith("\\\\"):
        raise ValueError(
            "system.admin_control.path must not use UNC or device storage"
        )
    drive_match = _WINDOWS_DRIVE_PREFIX_RE.match(windows_value)
    if drive_match is not None and (
        len(windows_value) == 2 or windows_value[2] != "\\"
    ):
        raise ValueError(
            "system.admin_control.path must not use a drive-relative path"
        )
    if any(part == ".." for part in windows_value.split("\\")):
        raise ValueError(
            "system.admin_control.path must not contain parent traversal"
        )

    resolution_base = os.path.abspath(base_dir or os.curdir)
    candidate = value
    if not os.path.isabs(candidate):
        candidate = os.path.join(resolution_base, candidate)
    resolved = os.path.abspath(candidate)
    if os.path.dirname(resolved) == resolved:
        raise ValueError(
            "system.admin_control.path must not resolve to a filesystem root"
        )
    if os.path.normcase(resolved) in {
        os.path.normcase(resolution_base),
        os.path.normcase(os.path.abspath(os.curdir)),
    }:
        raise ValueError(
            "system.admin_control.path must not resolve to the working directory "
            "or config directory"
        )
    if os.path.normcase(os.path.realpath(resolved)) != os.path.normcase(resolved):
        raise ValueError(
            "system.admin_control.path must not traverse a symlink or reparse point"
        )
    return resolved


def load_admin_control_config(path: str = "config.json") -> dict:
    """Read only the admin inbox location without loading Live credentials."""
    from infrastructure.config_scaling import load_config_document

    config_path = os.path.abspath(os.fspath(path))
    raw = load_config_document(config_path)

    system = raw.get("system", {})
    if system is None:
        system = {}
    if not isinstance(system, dict):
        raise ValueError("root config system must be a JSON object for admin access")
    admin_control = system.get("admin_control", {})
    if admin_control is None:
        admin_control = {}
    if not isinstance(admin_control, dict):
        raise ValueError("system.admin_control must be a JSON object")
    admin_path = _validate_admin_control_path(
        admin_control.get("path", DEFAULT_ADMIN_DIR),
        base_dir=os.path.dirname(config_path),
    )
    live_launch = raw.get("live_launch", {})
    if live_launch is None:
        live_launch = {}
    if not isinstance(live_launch, dict):
        raise ValueError("root config live_launch must be a JSON object")
    return {
        "live_launch": {
            "deployment_id": str(
                live_launch.get("deployment_id", "") or ""
            ).strip(),
        },
        "system": {
            "admin_control": {
                "path": admin_path,
                "command_ttl_sec": admin_control.get(
                    "command_ttl_sec",
                    DEFAULT_COMMAND_TTL_SEC,
                ),
                "session_max_age_sec": admin_control.get(
                    "session_max_age_sec",
                    DEFAULT_SESSION_MAX_AGE_SEC,
                ),
            },
        },
    }


def _ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)
    return path


def _sleep_before_atomic_replace_retry(delay_sec: float) -> None:
    time.sleep(delay_sec)


def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_utc_timestamp(value, field: str) -> float:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise ValueError(f"{field} must be an ISO-8601 UTC timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{field} must include an explicit UTC offset")
    return parsed.timestamp()


def _bounded_positive_float(value, *, field: str, maximum: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite positive number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite positive number") from exc
    if not math.isfinite(parsed) or parsed <= 0.0 or parsed > maximum:
        raise ValueError(
            f"{field} must be positive and no more than {maximum:g} seconds"
        )
    return parsed


def _bounded_positive_int(value, *, field: str, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > maximum
    ):
        raise ValueError(
            f"{field} must be a positive integer no more than {maximum}"
        )
    return value


def _admin_retention_settings(config: dict | None) -> tuple[int, int, float]:
    root = config or {}
    system = root.get("system", {}) or {}
    admin = system.get("admin_control", {}) if isinstance(system, dict) else {}
    admin = admin if isinstance(admin, dict) else {}
    max_results = _bounded_positive_int(
        admin.get("max_retained_results", DEFAULT_MAX_RETAINED_RESULTS),
        field="system.admin_control.max_retained_results",
        maximum=4096,
    )
    max_archives = _bounded_positive_int(
        admin.get("max_retained_archives", DEFAULT_MAX_RETAINED_ARCHIVES),
        field="system.admin_control.max_retained_archives",
        maximum=4096,
    )
    max_age_sec = _bounded_positive_float(
        admin.get(
            "retention_max_age_sec",
            DEFAULT_RETENTION_MAX_AGE_SEC,
        ),
        field="system.admin_control.retention_max_age_sec",
        maximum=31 * 24 * 60 * 60.0,
    )
    return max_results, max_archives, max_age_sec


def _prune_json_directory(
    directory: str,
    *,
    max_files: int,
    max_age_sec: float,
) -> int:
    cutoff = time.time() - max_age_sec
    retained = []
    removed = 0
    with os.scandir(directory) as entries:
        for entry in entries:
            if not entry.name.endswith(".json") or not entry.is_file(
                follow_symlinks=False
            ):
                continue
            stat = entry.stat(follow_symlinks=False)
            if stat.st_mtime < cutoff:
                os.remove(entry.path)
                removed += 1
            else:
                retained.append((stat.st_mtime_ns, entry.name, entry.path))
    retained.sort()
    for _mtime_ns, _name, path in retained[:-max_files]:
        os.remove(path)
        removed += 1
    return removed


def _admin_settings(config: dict | None) -> tuple[str, float, float]:
    root = config or {}
    live_launch = root.get("live_launch", {}) or {}
    deployment_id = str(
        live_launch.get("deployment_id", "")
        if isinstance(live_launch, dict)
        else ""
    ).strip()
    system = root.get("system", {}) or {}
    admin = system.get("admin_control", {}) if isinstance(system, dict) else {}
    admin = admin if isinstance(admin, dict) else {}
    command_ttl_sec = _bounded_positive_float(
        admin.get("command_ttl_sec", DEFAULT_COMMAND_TTL_SEC),
        field="system.admin_control.command_ttl_sec",
        maximum=30.0,
    )
    session_max_age_sec = _bounded_positive_float(
        admin.get("session_max_age_sec", DEFAULT_SESSION_MAX_AGE_SEC),
        field="system.admin_control.session_max_age_sec",
        maximum=5.0,
    )
    if session_max_age_sec >= command_ttl_sec:
        raise ValueError(
            "system.admin_control.session_max_age_sec must be less than "
            "command_ttl_sec"
        )
    return deployment_id, command_ttl_sec, session_max_age_sec


def _atomic_write_json(path: str, payload: dict) -> None:
    directory = os.path.dirname(path)
    _ensure_dir(directory)
    temporary_path = os.path.join(
        directory,
        f".{os.path.basename(path)}.{uuid.uuid4().hex}.tmp",
    )
    try:
        with open(temporary_path, "x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(ATOMIC_REPLACE_MAX_ATTEMPTS):
            try:
                os.replace(temporary_path, path)
                break
            except OSError as exc:
                retryable = isinstance(exc, PermissionError) or getattr(
                    exc,
                    "winerror",
                    None,
                ) in TRANSIENT_WINDOWS_REPLACE_ERRORS
                if not retryable or attempt + 1 >= ATOMIC_REPLACE_MAX_ATTEMPTS:
                    raise
                _sleep_before_atomic_replace_retry(
                    ATOMIC_REPLACE_RETRY_BASE_SEC * (2**attempt)
                )
    finally:
        try:
            os.remove(temporary_path)
        except FileNotFoundError:
            pass


def resolve_admin_paths(config: dict = None, override_dir: str = "") -> dict:
    base_dir = str(override_dir or "").strip()
    if not base_dir:
        base_dir = (
            ((config or {}).get("system", {}) or {})
            .get("admin_control", {})
            .get("path", DEFAULT_ADMIN_DIR)
        )
    base_dir = _validate_admin_control_path(base_dir)
    inbox_dir = _ensure_dir(os.path.join(base_dir, "inbox"))
    results_dir = _ensure_dir(os.path.join(base_dir, "results"))
    archive_dir = _ensure_dir(os.path.join(base_dir, "archive"))
    return {
        "base_dir": base_dir,
        "inbox_dir": inbox_dir,
        "results_dir": results_dir,
        "archive_dir": archive_dir,
        "session_path": os.path.join(base_dir, "active_session.json"),
    }


def _load_active_session(paths: dict, config: dict | None) -> dict:
    deployment_id, _command_ttl_sec, session_max_age_sec = _admin_settings(config)
    session_path = paths["session_path"]
    try:
        session = _load_strict_json_object(
            session_path,
            "admin-control session",
        )
    except ValueError as exc:
        raise RuntimeError("no valid running admin-control session") from exc
    if session.get("schema") != ADMIN_SESSION_SCHEMA:
        raise RuntimeError("admin-control session schema is invalid")
    session_id = str(session.get("session_id", "") or "")
    if not _COMMAND_ID_RE.fullmatch(session_id):
        raise RuntimeError("admin-control session identity is invalid")
    if str(session.get("deployment_id", "") or "") != deployment_id:
        raise RuntimeError("admin-control session belongs to another deployment")
    heartbeat_at = _parse_utc_timestamp(
        session.get("heartbeat_at"),
        "admin session heartbeat_at",
    )
    heartbeat_age = time.time() - heartbeat_at
    if heartbeat_age < -1.0 or heartbeat_age > session_max_age_sec:
        raise RuntimeError("no fresh running admin-control session")
    return session


def submit_admin_command(
    action: str,
    reason: str = "",
    config: dict = None,
    admin_dir: str = "",
    wait_timeout_sec: float = 5.0,
):
    paths = resolve_admin_paths(config, admin_dir)
    try:
        session = _load_active_session(paths, config)
    except (RuntimeError, ValueError) as exc:
        return {
            "id": "",
            "accepted": False,
            "status": "unavailable",
            "message": str(exc),
            "result_path": "",
        }
    deployment_id, _command_ttl_sec, _session_max_age_sec = _admin_settings(config)
    command_id = uuid.uuid4().hex
    payload = {
        "schema": ADMIN_COMMAND_SCHEMA,
        "id": command_id,
        "session_id": session["session_id"],
        "deployment_id": deployment_id,
        "action": str(action or "").strip().lower(),
        "reason": str(reason or "").strip(),
        "created_at": _utc_now_iso(),
    }
    command_path = os.path.join(paths["inbox_dir"], f"{command_id}.json")
    result_path = os.path.join(paths["results_dir"], f"{command_id}.json")

    _atomic_write_json(command_path, payload)

    deadline = time.time() + max(0.0, float(wait_timeout_sec or 0.0))
    while time.time() <= deadline:
        if os.path.exists(result_path):
            return _load_strict_json_object(
                result_path,
                "admin command result",
            )
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
        self.config = config or {}
        self.paths = resolve_admin_paths(self.config, admin_dir)
        (
            self.deployment_id,
            self.command_ttl_sec,
            self.session_max_age_sec,
        ) = _admin_settings(self.config)
        self.session_id = uuid.uuid4().hex
        self.started_at_wall = time.time()
        self.started_at_utc = _utc_now_iso()
        self.session_heartbeat_interval_sec = min(
            0.5,
            self.session_max_age_sec / 4.0,
        )
        (
            self.max_retained_results,
            self.max_retained_archives,
            self.retention_max_age_sec,
        ) = _admin_retention_settings(self.config)
        self.retention_evictions = 0
        self._closed = False
        self._last_session_publish_monotonic = 0.0
        self._prune_history()
        self._publish_session()

    def _prune_history(self) -> None:
        self.retention_evictions += _prune_json_directory(
            self.paths["results_dir"],
            max_files=self.max_retained_results,
            max_age_sec=self.retention_max_age_sec,
        )
        self.retention_evictions += _prune_json_directory(
            self.paths["archive_dir"],
            max_files=self.max_retained_archives,
            max_age_sec=self.retention_max_age_sec,
        )

    def _publish_session(self, now_monotonic=None):
        published_monotonic = (
            time.perf_counter()
            if now_monotonic is None
            else float(now_monotonic)
        )
        _atomic_write_json(
            self.paths["session_path"],
            {
                "schema": ADMIN_SESSION_SCHEMA,
                "session_id": self.session_id,
                "deployment_id": self.deployment_id,
                "process_id": os.getpid(),
                "started_at": self.started_at_utc,
                "heartbeat_at": _utc_now_iso(),
            },
        )
        self._last_session_publish_monotonic = published_monotonic

    def poll_once(self):
        if self._closed:
            return False
        now_monotonic = time.perf_counter()
        if (
            now_monotonic - self._last_session_publish_monotonic
            >= self.session_heartbeat_interval_sec
        ):
            self._publish_session(now_monotonic)
        inbox_dir = self.paths["inbox_dir"]
        for name in sorted(os.listdir(inbox_dir)):
            if not name.endswith(".json"):
                continue
            command_path = os.path.join(inbox_dir, name)
            self._process_command_file(command_path)
        return True

    def close(self) -> bool:
        """Withdraw only this process's advertised admin session."""
        self._closed = True
        session_path = self.paths["session_path"]
        try:
            session = _load_strict_json_object(
                session_path,
                "active admin session",
            )
        except ValueError:
            if not os.path.exists(session_path):
                return True
            raise
        if str(session.get("session_id", "") or "") != self.session_id:
            return True
        for attempt in range(ATOMIC_REPLACE_MAX_ATTEMPTS):
            try:
                os.remove(session_path)
                return True
            except FileNotFoundError:
                return True
            except OSError as exc:
                retryable = isinstance(exc, PermissionError) or getattr(
                    exc,
                    "winerror",
                    None,
                ) in TRANSIENT_WINDOWS_REPLACE_ERRORS
                if not retryable or attempt + 1 >= ATOMIC_REPLACE_MAX_ATTEMPTS:
                    raise
                _sleep_before_atomic_replace_retry(
                    ATOMIC_REPLACE_RETRY_BASE_SEC * (2**attempt)
                )
        return False

    def _process_command_file(self, command_path: str):
        try:
            command = _load_strict_json_object(
                command_path,
                "admin command",
            )
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

        try:
            file_id = os.path.splitext(os.path.basename(command_path))[0]
            if command.get("schema") != ADMIN_COMMAND_SCHEMA:
                raise ValueError("admin command schema is invalid")
            if not _COMMAND_ID_RE.fullmatch(command_id) or command_id != file_id:
                raise ValueError("admin command ID does not match its file name")
            if str(command.get("session_id", "") or "") != self.session_id:
                raise ValueError("admin command belongs to an inactive session")
            if str(command.get("deployment_id", "") or "") != self.deployment_id:
                raise ValueError("admin command belongs to another deployment")
            created_at = _parse_utc_timestamp(
                command.get("created_at"),
                "admin command created_at",
            )
            command_age = time.time() - created_at
            if created_at + 1.0 < self.started_at_wall:
                raise ValueError("admin command predates the active session")
            if command_age < -1.0 or command_age > self.command_ttl_sec:
                raise ValueError("admin command is outside its validity window")
        except ValueError as exc:
            self._write_result(
                command_id=(
                    command_id
                    if _COMMAND_ID_RE.fullmatch(command_id)
                    else os.path.splitext(os.path.basename(command_path))[0]
                ),
                accepted=False,
                status="invalid",
                message=str(exc),
                snapshot=self._status_snapshot(),
            )
            self._archive_command(command_path)
            return

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
            "admin_control_retention": {
                "max_results": self.max_retained_results,
                "max_archives": self.max_retained_archives,
                "max_age_sec": self.retention_max_age_sec,
                "evictions": self.retention_evictions,
            },
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
        _atomic_write_json(result_path, payload)

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
        self._prune_history()
