"""Explicit lifecycle and resource ownership for an OMS submission."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from threading import Condition, Lock
from typing import ClassVar


class SubmissionState(str, Enum):
    CREATED = "CREATED"
    PREPARED_DURABLE = "PREPARED_DURABLE"
    PERMIT_ACQUIRED = "PERMIT_ACQUIRED"
    DISPATCHED = "DISPATCHED"
    SETTLED_DURABLE = "SETTLED_DURABLE"
    TERMINAL = "TERMINAL"


class SubmissionAdmissionPolicy(str, Enum):
    STRATEGY = "strategy"
    INTERNAL = "internal"


class SubmissionTerminalOutcome(str, Enum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    UNKNOWN = "UNKNOWN"
    REJECTED = "REJECTED"
    FAILED_CLOSED = "FAILED_CLOSED"


class SubmissionTransitionError(RuntimeError):
    """Raised when a caller attempts to skip or repeat a durable phase."""


class SubmissionFinalizationError(RuntimeError):
    """Raised after all resource cleanup was attempted and one step failed."""

    def __init__(self, errors: tuple[BaseException, ...]):
        detail = "; ".join(
            f"{type(error).__name__}:{error}" for error in errors
        )
        super().__init__(f"submission cleanup failed: {detail}")


@dataclass(frozen=True, slots=True)
class SubmissionStateSnapshot:
    transaction_id: str
    client_oid: str
    admission_policy: SubmissionAdmissionPolicy
    state: SubmissionState
    history: tuple[SubmissionState, ...]
    permit_epoch: int | None
    terminal_outcome: SubmissionTerminalOutcome | None
    finalized: bool


FaultInjector = Callable[[SubmissionState], None]
Cleanup = Callable[[], None]
GateCleanup = Callable[[str, BaseException], None]


class SubmissionTransaction:
    """Own one submission's durable phases and outbound permit.

    Normal progress is deliberately linear. Failure may terminate from any
    phase, but permit release always runs through :meth:`finalize` exactly
    once.
    """

    _NEXT_STATE: ClassVar[dict[SubmissionState, SubmissionState]] = {
        SubmissionState.CREATED: SubmissionState.PREPARED_DURABLE,
        SubmissionState.PREPARED_DURABLE: SubmissionState.PERMIT_ACQUIRED,
        SubmissionState.PERMIT_ACQUIRED: SubmissionState.DISPATCHED,
        SubmissionState.DISPATCHED: SubmissionState.SETTLED_DURABLE,
        SubmissionState.SETTLED_DURABLE: SubmissionState.TERMINAL,
    }

    def __init__(
        self,
        *,
        transaction_id: str,
        client_oid: str,
        admission_policy: SubmissionAdmissionPolicy,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self._transaction_id = transaction_id
        self._client_oid = client_oid
        self._admission_policy = admission_policy
        self._fault_injector = fault_injector
        self._state = SubmissionState.CREATED
        self._history = [SubmissionState.CREATED]
        self._permit_epoch: int | None = None
        self._terminal_outcome: SubmissionTerminalOutcome | None = None
        self._permit_release: Cleanup | None = None
        self._gate_cleanup: GateCleanup | None = None
        self._gate_failure: tuple[str, BaseException] | None = None
        self._finalized = False
        self._finalizing = False
        self._cleanup_complete = False
        self._lock = Lock()
        self._condition = Condition(self._lock)

    @property
    def state(self) -> SubmissionState:
        with self._lock:
            return self._state

    @property
    def permit_epoch(self) -> int | None:
        with self._lock:
            return self._permit_epoch

    @property
    def admission_policy(self) -> SubmissionAdmissionPolicy:
        return self._admission_policy

    @property
    def finalized(self) -> bool:
        with self._lock:
            return self._cleanup_complete

    def snapshot(self) -> SubmissionStateSnapshot:
        with self._lock:
            return SubmissionStateSnapshot(
                transaction_id=self._transaction_id,
                client_oid=self._client_oid,
                admission_policy=self._admission_policy,
                state=self._state,
                history=tuple(self._history),
                permit_epoch=self._permit_epoch,
                terminal_outcome=self._terminal_outcome,
                finalized=self._cleanup_complete,
            )

    def bind_gate_cleanup(self, cleanup: GateCleanup) -> None:
        with self._lock:
            if self._finalized:
                raise SubmissionTransitionError(
                    "cannot bind gate cleanup after finalization"
                )
            if self._gate_cleanup is not None:
                raise SubmissionTransitionError("gate cleanup already bound")
            self._gate_cleanup = cleanup

    def record_gate_failure(
        self,
        context: str,
        error: BaseException,
    ) -> None:
        normalized_context = str(context).strip()
        if not normalized_context:
            raise ValueError("gate failure context is required")
        with self._lock:
            if self._finalized:
                raise SubmissionTransitionError(
                    "cannot record gate failure after finalization"
                )
            if self._gate_failure is None:
                self._gate_failure = (normalized_context, error)

    def prepared_durable(self) -> None:
        self._advance(SubmissionState.PREPARED_DURABLE)

    def permit_acquired(
        self,
        permit_epoch: int,
        release: Cleanup,
    ) -> None:
        if permit_epoch is None:
            raise SubmissionTransitionError("permit epoch is required")
        with self._lock:
            if self._state is not SubmissionState.PREPARED_DURABLE:
                self._raise_bad_transition(SubmissionState.PERMIT_ACQUIRED)
            if self._permit_release is not None:
                raise SubmissionTransitionError("permit already acquired")
            self._permit_epoch = int(permit_epoch)
            self._permit_release = release
        # Register the externally acquired permit before the phase barrier.
        # A fault at the barrier can then be finalized without leaking it.
        self._inject(SubmissionState.PERMIT_ACQUIRED)
        with self._lock:
            if self._finalized:
                self._raise_bad_transition(SubmissionState.PERMIT_ACQUIRED)
            if self._state is not SubmissionState.PREPARED_DURABLE:
                self._raise_bad_transition(SubmissionState.PERMIT_ACQUIRED)
            self._state = SubmissionState.PERMIT_ACQUIRED
            self._history.append(self._state)

    def dispatched(self) -> None:
        self._advance(SubmissionState.DISPATCHED)

    def settled_durable(self) -> None:
        self._advance(SubmissionState.SETTLED_DURABLE)

    def finalize(
        self,
        outcome: SubmissionTerminalOutcome,
        *,
        gate_failure_context: str = "",
        gate_failure: BaseException | None = None,
    ) -> None:
        """Close the gate when requested and release the outbound permit.

        Cleanup is idempotent so exception handlers and an outer ``finally``
        may both call it. The first call owns the terminal outcome.
        """

        if bool(gate_failure_context) != (gate_failure is not None):
            raise ValueError(
                "gate failure context and exception must be supplied together"
            )

        with self._condition:
            if self._finalized or self._finalizing:
                while not self._cleanup_complete:
                    self._condition.wait()
                return
            self._finalizing = True

        injection_error: BaseException | None = None
        try:
            self._inject(SubmissionState.TERMINAL)
        except BaseException as exc:
            # Terminal fault injection must never bypass resource release.
            injection_error = exc

        with self._condition:
            gate_cleanup = self._gate_cleanup
            recorded_gate_failure = self._gate_failure
            permit_release = self._permit_release
            self._permit_release = None
            self._terminal_outcome = outcome
            self._state = SubmissionState.TERMINAL
            self._history.append(self._state)
            self._finalized = True

        errors: list[BaseException] = []
        gate_cleanup_attempted = False
        try:
            effective_gate_failure = (
                (gate_failure_context, gate_failure)
                if gate_failure is not None
                else recorded_gate_failure
            )
            if effective_gate_failure is not None and gate_cleanup is not None:
                gate_cleanup_attempted = True
                try:
                    gate_cleanup(*effective_gate_failure)
                except BaseException as exc:
                    errors.append(exc)
            if permit_release is not None:
                try:
                    permit_release()
                except BaseException as exc:
                    errors.append(exc)
            if errors and gate_cleanup is not None and not gate_cleanup_attempted:
                try:
                    gate_cleanup("submission_resource_cleanup", errors[0])
                except BaseException as exc:
                    errors.append(exc)
        finally:
            with self._condition:
                self._cleanup_complete = True
                self._finalizing = False
                self._condition.notify_all()
        if errors:
            cleanup_error = SubmissionFinalizationError(tuple(errors))
            if injection_error is not None:
                raise cleanup_error from injection_error
            raise cleanup_error
        if injection_error is not None:
            raise injection_error

    def _advance(self, target: SubmissionState) -> None:
        self._inject(target)
        with self._lock:
            if self._finalized:
                self._raise_bad_transition(target)
            expected = self._NEXT_STATE.get(self._state)
            if expected is not target:
                self._raise_bad_transition(target)
            self._state = target
            self._history.append(target)

    def _inject(self, target: SubmissionState) -> None:
        if self._fault_injector is not None:
            self._fault_injector(target)

    def _raise_bad_transition(self, target: SubmissionState) -> None:
        raise SubmissionTransitionError(
            f"invalid submission transition {self._state.value} -> "
            f"{target.value}"
        )


@dataclass(frozen=True, slots=True)
class StrategySubmissionReturnAdapter:
    client_oid: str
    state_value: Callable[[], str]
    result_type: Callable[..., object]

    def result(self, *, accepted: bool, reason: str = ""):
        return self.result_type(
            accepted=accepted,
            client_oid=self.client_oid,
            reason=reason,
            state=self.state_value(),
        )


@dataclass(frozen=True, slots=True)
class InternalSubmissionReturnAdapter:
    def result(self, *, accepted: bool, reason: str = "") -> bool:
        del reason
        return accepted
