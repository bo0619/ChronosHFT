"""Durable kill/rearm state for the independent risk sidecar."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from typing import Callable, Protocol


class SidecarDurableStateOwner(Protocol):
    state_path: str
    state_required: bool
    state_fsync: bool
    state_generation: int
    state_recovered: bool
    state_load_error: str
    state_persist_error: str
    _last_persisted_fingerprint: tuple | None

    def _state_checksum(self, payload: dict) -> str: ...

    def _durable_fingerprint(self): ...

    def _fail_closed_on_state_error(self, reason: str): ...

    def _quarantine_corrupt_state(self): ...

    def _persist_durable_state(
        self,
        event: str,
        force: bool = False,
    ) -> bool: ...


class SidecarDurableState:
    """Load and atomically persist identity-bound sidecar state."""

    __slots__ = ("_finite_float", "_owner", "_replace_state_file")

    def __init__(
        self,
        owner: SidecarDurableStateOwner,
        finite_float: Callable[[object, str], float],
        replace_state_file: Callable[[str, str], None],
    ):
        self._owner = owner
        self._finite_float = finite_float
        self._replace_state_file = replace_state_file

    @staticmethod
    def checksum(payload: dict) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def fingerprint(self):
        owner = self._owner
        return (
            bool(owner.kill_latched),
            str(owner.kill_reason or ""),
            str(owner.stage or ""),
            bool(owner.quiesced),
            str(owner.quiesce_reason or ""),
            float(owner.quiesced_at),
            str(owner.risk_day or ""),
            float(owner.day_start_equity),
            float(owner.day_start_external_cash_flow_total),
            float(owner.peak_adjusted_equity),
            float(owner.last_equity),
            str(owner.deployment_id or ""),
            float(owner.deployment_start_equity),
            float(owner.deployment_start_external_cash_flow_total),
            float(owner.deployment_adjusted_equity),
            float(owner.deployment_loss),
            float(owner.declared_account_equity),
            float(owner.max_deployed_capital),
            str(owner.deployment_policy_fingerprint or ""),
            str(owner.account_key_fingerprint or ""),
        )

    def fail_closed(self, reason: str):
        owner = self._owner
        owner.kill_latched = True
        owner.kill_reason = str(reason or "sidecar_state_error")
        owner.stage = "FAILED"

    def quarantine_corrupt(self):
        owner = self._owner
        if not owner.state_path or not os.path.exists(owner.state_path):
            return
        quarantine_path = (
            f"{owner.state_path}.corrupt.{int(time.time() * 1000)}"
        )
        try:
            self._replace_state_file(owner.state_path, quarantine_path)
        except OSError:
            pass

    def load(self):
        owner = self._owner
        if not owner.state_path:
            if owner.state_required:
                owner.state_load_error = "state_path_missing"
                owner._fail_closed_on_state_error(owner.state_load_error)
            return
        if not os.path.exists(owner.state_path):
            owner._persist_durable_state("state_initialized", force=True)
            return
        try:
            with open(owner.state_path, "r", encoding="utf-8") as handle:
                record = json.load(handle)
            if not isinstance(record, dict):
                raise ValueError("state_record_invalid")
            payload = record.get("payload")
            checksum = str(record.get("sha256", "") or "")
            if not isinstance(payload, dict):
                raise ValueError("state_payload_invalid")
            if checksum != owner._state_checksum(payload):
                raise ValueError("state_checksum_mismatch")
            if int(payload.get("schema_version", 0) or 0) != 1:
                raise ValueError("state_schema_unsupported")
            generation = int(payload.get("generation", 0) or 0)
            if generation < 0:
                raise ValueError("state_generation_invalid")
            stored_account_fingerprint = str(
                payload.get("account_key_fingerprint", "") or ""
            ).strip()
            if owner.account_key_fingerprint and not stored_account_fingerprint:
                raise ValueError("state_account_identity_missing")
            if (
                stored_account_fingerprint
                and owner.account_key_fingerprint
                and not secrets.compare_digest(
                    stored_account_fingerprint,
                    owner.account_key_fingerprint,
                )
            ):
                raise ValueError("state_account_identity_mismatch")
            if not owner.account_key_fingerprint:
                owner.account_key_fingerprint = stored_account_fingerprint
            stored_deployment_id = str(
                payload.get("deployment_id", "") or ""
            ).strip()
            if owner.deployment_id and not stored_deployment_id:
                raise ValueError("state_deployment_identity_missing")
            if (
                stored_deployment_id
                and owner.deployment_id
                and not secrets.compare_digest(
                    stored_deployment_id,
                    owner.deployment_id,
                )
            ):
                raise ValueError("state_deployment_identity_mismatch")
            if not owner.deployment_id:
                owner.deployment_id = stored_deployment_id
            stored_policy_fingerprint = str(
                payload.get("deployment_policy_fingerprint", "") or ""
            ).strip()
            if (
                owner.deployment_policy_fingerprint
                and not stored_policy_fingerprint
            ):
                raise ValueError("state_deployment_policy_missing")
            if (
                stored_policy_fingerprint
                and owner.deployment_policy_fingerprint
                and not secrets.compare_digest(
                    stored_policy_fingerprint,
                    owner.deployment_policy_fingerprint,
                )
            ):
                raise ValueError("state_deployment_policy_mismatch")
            kill_latched = payload.get("kill_latched", False)
            if not isinstance(kill_latched, bool):
                raise ValueError("state_kill_latch_invalid")
            quiesced = payload.get("quiesced", False)
            if not isinstance(quiesced, bool):
                raise ValueError("state_quiesced_invalid")
            quiesced_at = self._finite_float(
                payload.get("quiesced_at", 0.0) or 0.0,
                "state.quiesced_at",
            )
            if quiesced_at < 0.0:
                raise ValueError("state.quiesced_at must be non-negative")
            if quiesced and quiesced_at <= 0.0:
                raise ValueError("state.quiesced_at missing for quiesced state")
            owner.state_generation = generation
            owner.state_recovered = True
            owner.kill_latched = kill_latched
            owner.kill_reason = str(payload.get("kill_reason", "") or "")
            owner.quiesced = quiesced
            owner.quiesce_reason = str(
                payload.get("quiesce_reason", "") or ""
            )
            owner.quiesced_at = quiesced_at
            owner.risk_day = str(payload.get("risk_day", "") or "")
            owner.day_start_equity = self._finite_float(
                payload.get("day_start_equity", 0.0) or 0.0,
                "state.day_start_equity",
            )
            owner.day_start_external_cash_flow_total = self._finite_float(
                payload.get(
                    "day_start_external_cash_flow_total",
                    0.0,
                )
                or 0.0,
                "state.day_start_external_cash_flow_total",
            )
            owner.peak_adjusted_equity = self._finite_float(
                payload.get("peak_adjusted_equity", 0.0) or 0.0,
                "state.peak_adjusted_equity",
            )
            owner.last_equity = self._finite_float(
                payload.get("last_equity", 0.0) or 0.0,
                "state.last_equity",
            )
            owner.deployment_start_equity = self._finite_float(
                payload.get("deployment_start_equity", 0.0) or 0.0,
                "state.deployment_start_equity",
            )
            owner.deployment_start_external_cash_flow_total = (
                self._finite_float(
                    payload.get(
                        "deployment_start_external_cash_flow_total",
                        0.0,
                    )
                    or 0.0,
                    "state.deployment_start_external_cash_flow_total",
                )
            )
            owner.deployment_adjusted_equity = self._finite_float(
                payload.get("deployment_adjusted_equity", 0.0) or 0.0,
                "state.deployment_adjusted_equity",
            )
            deployment_loss = self._finite_float(
                payload.get("deployment_loss", 0.0) or 0.0,
                "state.deployment_loss",
            )
            if deployment_loss < 0.0:
                raise ValueError(
                    "state.deployment_loss must be non-negative"
                )
            owner.deployment_loss = deployment_loss
            if owner.quiesced:
                owner.stage = "QUIESCED"
            else:
                owner.stage = "FLATTENING" if kill_latched else "ARMED"
            owner._last_persisted_fingerprint = (
                owner._durable_fingerprint()
            )
        except Exception as exc:
            owner.state_load_error = (
                f"state_load_failed:{type(exc).__name__}:{exc}"
            )
            identity_error = str(exc).startswith(
                (
                    "state_account_identity_",
                    "state_deployment_identity_",
                    "state_deployment_policy_",
                )
            )
            if not identity_error:
                owner._quarantine_corrupt_state()
            owner._fail_closed_on_state_error(owner.state_load_error)

    def persist(self, event: str, force: bool = False) -> bool:
        owner = self._owner
        if not owner.state_path:
            if owner.state_required:
                owner.state_persist_error = "state_path_missing"
                owner._fail_closed_on_state_error(owner.state_persist_error)
                return False
            return True
        fingerprint = owner._durable_fingerprint()
        if not force and fingerprint == owner._last_persisted_fingerprint:
            return True
        next_generation = owner.state_generation + 1
        payload = {
            "schema_version": 1,
            "generation": next_generation,
            "kill_latched": bool(owner.kill_latched),
            "kill_reason": str(owner.kill_reason or ""),
            "stage": str(owner.stage or ""),
            "quiesced": bool(owner.quiesced),
            "quiesce_reason": str(owner.quiesce_reason or ""),
            "quiesced_at": float(owner.quiesced_at),
            "risk_day": str(owner.risk_day or ""),
            "day_start_equity": float(owner.day_start_equity),
            "day_start_external_cash_flow_total": float(
                owner.day_start_external_cash_flow_total
            ),
            "peak_adjusted_equity": float(owner.peak_adjusted_equity),
            "last_equity": float(owner.last_equity),
            "deployment_id": str(owner.deployment_id or ""),
            "deployment_start_equity": float(
                owner.deployment_start_equity
            ),
            "deployment_start_external_cash_flow_total": float(
                owner.deployment_start_external_cash_flow_total
            ),
            "deployment_adjusted_equity": float(
                owner.deployment_adjusted_equity
            ),
            "deployment_loss": float(owner.deployment_loss),
            "declared_account_equity": float(
                owner.declared_account_equity
            ),
            "max_deployed_capital": float(owner.max_deployed_capital),
            "deployment_policy_fingerprint": str(
                owner.deployment_policy_fingerprint or ""
            ),
            "account_key_fingerprint": str(
                owner.account_key_fingerprint or ""
            ),
            "event": str(event or "state_changed"),
            "updated_at": time.time(),
            "writer_pid": os.getpid(),
        }
        record = {
            "payload": payload,
            "sha256": owner._state_checksum(payload),
        }
        absolute_path = os.path.abspath(owner.state_path)
        state_dir = os.path.dirname(absolute_path)
        temp_path = (
            f"{absolute_path}.tmp.{os.getpid()}.{secrets.token_hex(6)}"
        )
        try:
            os.makedirs(state_dir, exist_ok=True)
            with open(temp_path, "x", encoding="utf-8") as handle:
                json.dump(
                    record,
                    handle,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                handle.flush()
                if owner.state_fsync:
                    os.fsync(handle.fileno())
            self._replace_state_file(temp_path, absolute_path)
            if owner.state_fsync and os.name != "nt":
                directory_fd = os.open(state_dir, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            owner.state_generation = next_generation
            owner.state_persist_error = ""
            owner._last_persisted_fingerprint = fingerprint
            return True
        except Exception as exc:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
            owner.state_persist_error = (
                f"state_persist_failed:{type(exc).__name__}:{exc}"
            )
            owner._fail_closed_on_state_error(owner.state_persist_error)
            return False
