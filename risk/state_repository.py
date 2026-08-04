"""Durable RiskManager state and its journal boundary."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from infrastructure.durability import DurabilityError


RESUMABLE_KILL_STATES = frozenset(
    {
        "TRIGGERED",
        "CANCEL_PENDING",
        "CANCEL_VERIFIED",
        "FLATTENING",
        "FAILED",
    }
)
VALID_KILL_STATES = RESUMABLE_KILL_STATES | {
    "ARMED",
    "FLAT_VERIFIED",
}


class RiskStateJournal(Protocol):
    """Journal operations required by the risk-state repository."""

    def append(self, kind: str, payload: dict) -> int: ...

    def load(self) -> list[dict]: ...

    def iter_records(
        self,
        *,
        respect_replay_policy: bool = False,
    ) -> Iterable[dict]: ...


@dataclass(slots=True)
class RiskDurableState:
    """Mutable risk state whose source of truth is the durable journal."""

    risk_day: str = ""
    initial_equity: float = 0.0
    initial_external_cash_flow_total: float = 0.0
    peak_equity: float = 0.0
    last_equity: float = 0.0
    deployment_id: str = ""
    deployment_policy_fingerprint: str = ""
    deployment_start_equity: float = 0.0
    deployment_start_external_cash_flow_total: float = 0.0
    deployment_adjusted_equity: float = 0.0
    deployment_loss: float = 0.0
    kill_switch_triggered: bool = False
    kill_state: str = "ARMED"
    kill_reason: str = ""

    def fail_closed(self, reason: str) -> None:
        self.kill_switch_triggered = True
        self.kill_state = "FAILED"
        self.kill_reason = str(reason or "risk_state_failure")

    def journal_payload(self, reason: str) -> dict[str, Any]:
        return {
            "risk_day": self.risk_day,
            "day_start_equity": self.initial_equity,
            "day_start_external_cash_flow_total": (
                self.initial_external_cash_flow_total
            ),
            "peak_equity": self.peak_equity,
            "last_equity": self.last_equity,
            "deployment_id": self.deployment_id,
            "deployment_policy_fingerprint": (
                self.deployment_policy_fingerprint
            ),
            "deployment_start_equity": self.deployment_start_equity,
            "deployment_start_external_cash_flow_total": (
                self.deployment_start_external_cash_flow_total
            ),
            "deployment_adjusted_equity": self.deployment_adjusted_equity,
            "deployment_loss": self.deployment_loss,
            "kill_switch_triggered": self.kill_switch_triggered,
            "kill_state": self.kill_state,
            "kill_reason": self.kill_reason,
            "reason": str(reason or "risk_state_changed"),
        }


class RiskStateField:
    """Explicit compatibility field backed by repository-owned state."""

    __slots__ = ("_field_name",)

    def __init__(self, field_name: str) -> None:
        if field_name not in RiskDurableState.__dataclass_fields__:
            raise ValueError(f"Unknown durable risk-state field: {field_name}")
        self._field_name = field_name

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        state = instance.risk_state_repository.state
        return getattr(state, self._field_name)

    def __set__(self, instance, value) -> None:
        state = instance.risk_state_repository.state
        setattr(state, self._field_name, value)


class _RiskStatePayloadError(ValueError):
    pass


class RiskStateRepository:
    """Restore, validate, and persist identity-bound risk state."""

    def __init__(
        self,
        *,
        journal: RiskStateJournal | None,
        deployment_id: str,
        deployment_policy_fingerprint: str,
        durability_failure_handler: Callable[[Exception, str], Any] | None,
        halt_handler: Callable[[str], Any] | None,
        logger,
    ) -> None:
        self._journal = journal
        self._durability_failure_handler = durability_failure_handler
        self._halt_handler = halt_handler
        self._logger = logger
        self._expected_deployment_id = str(deployment_id or "").strip()
        self._expected_policy_fingerprint = str(
            deployment_policy_fingerprint or ""
        ).strip()
        self.state = RiskDurableState(
            deployment_id=self._expected_deployment_id,
            deployment_policy_fingerprint=(
                self._expected_policy_fingerprint
            ),
        )

    def restore(self) -> bool:
        """Restore the latest risk record, failing closed on invalid state."""
        if self._journal is None:
            return False
        try:
            latest = self._latest_payload()
        except DurabilityError as exc:
            self._handle_durability_failure(exc, "restore_risk_state")
            return False
        except _RiskStatePayloadError as exc:
            self._fail_closed_on_corruption(str(exc))
            return False

        if not latest:
            return False
        try:
            self.state = self._decode(latest)
        except _RiskStatePayloadError as exc:
            self._fail_closed_on_corruption(str(exc))
            return False
        return True

    def persist(self, reason: str) -> bool:
        if self._journal is None:
            return False
        try:
            self._journal.append(
                "risk_state",
                self.state.journal_payload(reason),
            )
        except DurabilityError as exc:
            self._handle_durability_failure(exc, "persist_risk_state")
            return False
        return True

    def _latest_payload(self) -> Mapping[str, Any] | None:
        stream_records = getattr(self._journal, "iter_records", None)
        records = (
            stream_records(respect_replay_policy=True)
            if callable(stream_records)
            else iter(self._journal.load())
        )
        latest = None
        for record in records:
            if not isinstance(record, Mapping):
                raise _RiskStatePayloadError("journal_record_not_mapping")
            if record.get("kind") != "risk_state":
                continue
            latest = record.get("payload", {})
        if latest and not isinstance(latest, Mapping):
            raise _RiskStatePayloadError("payload_not_mapping")
        return latest

    def _decode(self, payload: Mapping[str, Any]) -> RiskDurableState:
        try:
            numbers = {
                "initial_equity": float(
                    payload.get("day_start_equity", 0.0) or 0.0
                ),
                "initial_external_cash_flow_total": float(
                    payload.get(
                        "day_start_external_cash_flow_total",
                        0.0,
                    )
                    or 0.0
                ),
                "peak_equity": float(
                    payload.get("peak_equity", 0.0) or 0.0
                ),
                "last_equity": float(
                    payload.get("last_equity", 0.0) or 0.0
                ),
                "deployment_start_equity": float(
                    payload.get("deployment_start_equity", 0.0) or 0.0
                ),
                "deployment_start_external_cash_flow_total": float(
                    payload.get(
                        "deployment_start_external_cash_flow_total",
                        0.0,
                    )
                    or 0.0
                ),
                "deployment_adjusted_equity": float(
                    payload.get("deployment_adjusted_equity", 0.0) or 0.0
                ),
                "deployment_loss": float(
                    payload.get("deployment_loss", 0.0) or 0.0
                ),
            }
        except (TypeError, ValueError) as exc:
            raise _RiskStatePayloadError(
                f"non_numeric:{type(exc).__name__}"
            ) from exc
        if not all(math.isfinite(value) for value in numbers.values()):
            raise _RiskStatePayloadError("non_finite")
        if numbers["deployment_loss"] < 0.0:
            raise _RiskStatePayloadError("negative_deployment_loss")

        kill_state = str(payload.get("kill_state", "ARMED") or "ARMED")
        kill_latch = payload.get("kill_switch_triggered", False)
        if not isinstance(kill_latch, bool):
            raise _RiskStatePayloadError("kill_latch_not_boolean")
        if kill_state not in VALID_KILL_STATES:
            raise _RiskStatePayloadError(f"invalid_kill_state:{kill_state}")
        if (kill_latch and kill_state == "ARMED") or (
            not kill_latch and kill_state != "ARMED"
        ):
            raise _RiskStatePayloadError("inconsistent_kill_latch")

        restored = RiskDurableState(
            risk_day=str(payload.get("risk_day", "") or ""),
            initial_equity=numbers["initial_equity"],
            initial_external_cash_flow_total=numbers[
                "initial_external_cash_flow_total"
            ],
            peak_equity=numbers["peak_equity"],
            last_equity=numbers["last_equity"],
            deployment_id=self._expected_deployment_id,
            deployment_policy_fingerprint=(
                self._expected_policy_fingerprint
            ),
            kill_switch_triggered=kill_latch,
            kill_state=kill_state,
            kill_reason=str(payload.get("kill_reason", "") or ""),
        )

        stored_deployment_id = str(
            payload.get("deployment_id", "") or ""
        ).strip()
        if self._expected_deployment_id and not stored_deployment_id:
            restored.fail_closed("deployment_identity_missing_from_journal")
            return restored
        if (
            stored_deployment_id
            and self._expected_deployment_id
            and stored_deployment_id != self._expected_deployment_id
        ):
            restored.fail_closed(
                "deployment_identity_mismatch:"
                f"{stored_deployment_id}!={self._expected_deployment_id}"
            )
            return restored
        if not self._expected_deployment_id:
            restored.deployment_id = stored_deployment_id

        stored_policy_fingerprint = str(
            payload.get("deployment_policy_fingerprint", "") or ""
        ).strip()
        if (
            self._expected_policy_fingerprint
            and not stored_policy_fingerprint
        ):
            restored.fail_closed("deployment_policy_missing_from_journal")
            return restored
        if (
            stored_policy_fingerprint
            and self._expected_policy_fingerprint
            and stored_policy_fingerprint
            != self._expected_policy_fingerprint
        ):
            restored.fail_closed("deployment_policy_mismatch")
            return restored

        restored.deployment_start_equity = numbers[
            "deployment_start_equity"
        ]
        restored.deployment_start_external_cash_flow_total = numbers[
            "deployment_start_external_cash_flow_total"
        ]
        restored.deployment_adjusted_equity = numbers[
            "deployment_adjusted_equity"
        ]
        restored.deployment_loss = numbers["deployment_loss"]
        return restored

    def _handle_durability_failure(
        self,
        exc: DurabilityError,
        context: str,
    ) -> None:
        action = "restore" if context == "restore_risk_state" else "persist"
        self._logger.critical(
            f"[Risk] Failed to {action} durable risk state: {exc}"
        )
        if callable(self._durability_failure_handler):
            self._durability_failure_handler(exc, context)

    def _fail_closed_on_corruption(self, detail: str) -> None:
        reason = f"risk_state_corrupt:{detail}"
        self.state.fail_closed(reason)
        self._logger.critical(f"[Risk] {reason}")
        if not callable(self._halt_handler):
            return
        try:
            self._halt_handler(f"RiskManager: {reason}")
        except Exception as exc:
            self._logger.critical(
                "[Risk] Could not halt OMS after risk-state corruption: "
                f"{type(exc).__name__}:{exc}"
            )
