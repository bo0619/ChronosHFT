from __future__ import annotations

import threading

import pytest

from oms.submission_transaction import (
    SubmissionAdmissionPolicy,
    SubmissionFinalizationError,
    SubmissionState,
    SubmissionTerminalOutcome,
    SubmissionTransaction,
    SubmissionTransitionError,
)


def _transaction(*, fault_injector=None) -> SubmissionTransaction:
    return SubmissionTransaction(
        transaction_id="SUBMIT:oid-1",
        client_oid="oid-1",
        admission_policy=SubmissionAdmissionPolicy.STRATEGY,
        fault_injector=fault_injector,
    )


def _advance_to(
    transaction: SubmissionTransaction,
    target: SubmissionState,
    released: list[str],
) -> None:
    if target is SubmissionState.CREATED:
        return
    transaction.prepared_durable()
    if target is SubmissionState.PREPARED_DURABLE:
        return
    transaction.permit_acquired(7, lambda: released.append("permit"))
    if target is SubmissionState.PERMIT_ACQUIRED:
        return
    transaction.dispatched()
    if target is SubmissionState.DISPATCHED:
        return
    transaction.settled_durable()


def test_successful_submission_has_one_explicit_linear_history():
    released: list[str] = []
    transaction = _transaction()

    _advance_to(transaction, SubmissionState.SETTLED_DURABLE, released)
    transaction.finalize(SubmissionTerminalOutcome.ACKNOWLEDGED)

    snapshot = transaction.snapshot()
    assert snapshot.history == tuple(SubmissionState)
    assert snapshot.permit_epoch == 7
    assert snapshot.terminal_outcome is SubmissionTerminalOutcome.ACKNOWLEDGED
    assert snapshot.finalized
    assert released == ["permit"]


@pytest.mark.parametrize(
    "target",
    [
        SubmissionState.PREPARED_DURABLE,
        SubmissionState.PERMIT_ACQUIRED,
        SubmissionState.DISPATCHED,
        SubmissionState.SETTLED_DURABLE,
        SubmissionState.TERMINAL,
    ],
)
def test_each_phase_exposes_a_barrier_fault_injection_point(target):
    entered = threading.Event()
    resume = threading.Event()
    released: list[str] = []

    def injector(next_state: SubmissionState) -> None:
        if next_state is target:
            entered.set()
            assert resume.wait(2.0)

    transaction = _transaction(fault_injector=injector)

    def run() -> None:
        _advance_to(transaction, SubmissionState.SETTLED_DURABLE, released)
        transaction.finalize(SubmissionTerminalOutcome.ACKNOWLEDGED)

    worker = threading.Thread(target=run)
    worker.start()
    assert entered.wait(2.0)
    assert transaction.state is not target
    resume.set()
    worker.join(2.0)

    assert not worker.is_alive()
    assert transaction.state is SubmissionState.TERMINAL
    assert released == ["permit"]


@pytest.mark.parametrize(
    "failure_state",
    [
        SubmissionState.PREPARED_DURABLE,
        SubmissionState.PERMIT_ACQUIRED,
        SubmissionState.DISPATCHED,
        SubmissionState.SETTLED_DURABLE,
    ],
)
def test_fault_at_every_durable_phase_can_fail_closed_once(failure_state):
    released: list[str] = []

    def injector(next_state: SubmissionState) -> None:
        if next_state is failure_state:
            raise OSError(f"fault:{next_state.value}")

    transaction = _transaction(fault_injector=injector)
    with pytest.raises(OSError, match=failure_state.value):
        _advance_to(transaction, SubmissionState.SETTLED_DURABLE, released)

    transaction.finalize(SubmissionTerminalOutcome.FAILED_CLOSED)
    transaction.finalize(SubmissionTerminalOutcome.REJECTED)

    assert transaction.snapshot().terminal_outcome is (
        SubmissionTerminalOutcome.FAILED_CLOSED
    )
    expected_release = (
        ["permit"]
        if failure_state
        in {
            SubmissionState.PERMIT_ACQUIRED,
            SubmissionState.DISPATCHED,
            SubmissionState.SETTLED_DURABLE,
        }
        else []
    )
    assert released == expected_release


def test_terminal_fault_cannot_skip_permit_release():
    released: list[str] = []

    def injector(next_state: SubmissionState) -> None:
        if next_state is SubmissionState.TERMINAL:
            raise OSError("terminal fault")

    transaction = _transaction(fault_injector=injector)
    _advance_to(transaction, SubmissionState.SETTLED_DURABLE, released)

    with pytest.raises(OSError, match="terminal fault"):
        transaction.finalize(SubmissionTerminalOutcome.ACKNOWLEDGED)

    snapshot = transaction.snapshot()
    assert snapshot.state is SubmissionState.TERMINAL
    assert snapshot.finalized
    assert released == ["permit"]


def test_concurrent_finalizers_wait_for_one_cleanup_owner():
    release_entered = threading.Event()
    release_resume = threading.Event()
    actions: list[str] = []
    failures: list[BaseException] = []
    transaction = _transaction()
    transaction.prepared_durable()

    def release() -> None:
        actions.append("release")
        release_entered.set()
        assert release_resume.wait(2.0)

    transaction.permit_acquired(3, release)

    def finalize(outcome: SubmissionTerminalOutcome) -> None:
        try:
            transaction.finalize(outcome)
            actions.append(f"done:{outcome.value}")
        except BaseException as exc:
            failures.append(exc)

    first = threading.Thread(
        target=finalize,
        args=(SubmissionTerminalOutcome.UNKNOWN,),
    )
    second = threading.Thread(
        target=finalize,
        args=(SubmissionTerminalOutcome.REJECTED,),
    )
    first.start()
    assert release_entered.wait(2.0)
    second.start()
    second.join(0.05)
    assert second.is_alive()

    release_resume.set()
    first.join(2.0)
    second.join(2.0)

    assert not failures
    assert actions.count("release") == 1
    assert transaction.snapshot().terminal_outcome is (
        SubmissionTerminalOutcome.UNKNOWN
    )


def test_finalizer_closes_gate_then_releases_leases_in_reverse_order():
    actions: list[str] = []
    transaction = _transaction()
    transaction.bind_gate_cleanup(
        lambda context, error: actions.append(
            f"gate:{context}:{type(error).__name__}"
        )
    )
    transaction.acquire_lease("calibration", lambda: actions.append("calibration"))
    transaction.prepared_durable()
    transaction.permit_acquired(4, lambda: actions.append("permit"))

    transaction.finalize(
        SubmissionTerminalOutcome.FAILED_CLOSED,
        gate_failure_context="dispatch",
        gate_failure=TimeoutError("lost response"),
    )

    assert actions == ["gate:dispatch:TimeoutError", "permit", "calibration"]


def test_finalizer_attempts_all_cleanup_before_reporting_errors():
    actions: list[str] = []
    transaction = _transaction()
    transaction.acquire_lease(
        "first",
        lambda: (_ for _ in ()).throw(OSError("first")),
    )
    transaction.acquire_lease("second", lambda: actions.append("second"))

    with pytest.raises(SubmissionFinalizationError, match="first"):
        transaction.finalize(SubmissionTerminalOutcome.FAILED_CLOSED)

    assert actions == ["second"]
    assert transaction.finalized


def test_recorded_gate_failure_is_applied_once_by_the_finalizer():
    actions: list[str] = []
    transaction = _transaction()
    transaction.bind_gate_cleanup(
        lambda context, error: actions.append(
            f"gate:{context}:{type(error).__name__}"
        )
    )
    transaction.record_gate_failure("settlement", TimeoutError("timeout"))
    transaction.record_gate_failure("later", OSError("ignored"))

    transaction.finalize(SubmissionTerminalOutcome.UNKNOWN)
    transaction.finalize(SubmissionTerminalOutcome.REJECTED)

    assert actions == ["gate:settlement:TimeoutError"]


def test_illegal_phase_skip_is_rejected_without_mutation():
    transaction = _transaction()

    with pytest.raises(SubmissionTransitionError, match="CREATED -> DISPATCHED"):
        transaction.dispatched()

    assert transaction.snapshot().history == (SubmissionState.CREATED,)
