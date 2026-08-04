from __future__ import annotations

from dataclasses import asdict

import pytest

from infrastructure.durability import DurabilityError
from risk.manager import RiskManager
from risk.state_repository import RiskStateRepository


class _Logger:
    def __init__(self) -> None:
        self.messages = []

    def critical(self, message: str) -> None:
        self.messages.append(message)


class _Journal:
    def __init__(self, records=()) -> None:
        self.records = list(records)
        self.appended = []
        self.respect_replay_policy = None

    def iter_records(self, *, respect_replay_policy=False):
        self.respect_replay_policy = respect_replay_policy
        return iter(self.records)

    def append(self, kind: str, payload: dict) -> int:
        self.appended.append((kind, payload))
        return len(self.appended)


class _FailingJournal(_Journal):
    def __init__(self, *, fail_on: str) -> None:
        super().__init__()
        self.fail_on = fail_on

    def iter_records(self, *, respect_replay_policy=False):
        if self.fail_on == "restore":
            raise DurabilityError("read failed")
        return super().iter_records(
            respect_replay_policy=respect_replay_policy
        )

    def append(self, kind: str, payload: dict) -> int:
        if self.fail_on == "persist":
            raise DurabilityError("write failed")
        return super().append(kind, payload)


def _payload(**overrides):
    payload = {
        "risk_day": "2026-08-03",
        "day_start_equity": 1000.0,
        "day_start_external_cash_flow_total": 25.0,
        "peak_equity": 1010.0,
        "last_equity": 980.0,
        "deployment_id": "deployment-a",
        "deployment_policy_fingerprint": "policy-a",
        "deployment_start_equity": 1000.0,
        "deployment_start_external_cash_flow_total": 20.0,
        "deployment_adjusted_equity": 975.0,
        "deployment_loss": 25.0,
        "kill_switch_triggered": True,
        "kill_state": "CANCEL_PENDING",
        "kill_reason": "restart",
    }
    payload.update(overrides)
    return payload


def _repository(
    journal,
    *,
    deployment_id="deployment-a",
    policy="policy-a",
    durability_failures=None,
    halts=None,
):
    durability_failures = (
        [] if durability_failures is None else durability_failures
    )
    halts = [] if halts is None else halts
    return RiskStateRepository(
        journal=journal,
        deployment_id=deployment_id,
        deployment_policy_fingerprint=policy,
        durability_failure_handler=lambda exc, context: (
            durability_failures.append((exc, context))
        ),
        halt_handler=halts.append,
        logger=_Logger(),
    )


def test_restore_uses_latest_record_and_persist_serializes_owned_state():
    journal = _Journal(
        [
            {"kind": "risk_state", "payload": _payload(last_equity=990.0)},
            {"kind": "other", "payload": {}},
            {"kind": "risk_state", "payload": _payload()},
        ]
    )
    repository = _repository(journal)

    assert repository.restore()
    assert journal.respect_replay_policy is True
    assert repository.state.last_equity == 980.0
    assert repository.state.deployment_loss == 25.0
    assert repository.state.kill_state == "CANCEL_PENDING"

    repository.state.last_equity = 970.0
    assert repository.persist("account_update")
    kind, persisted = journal.appended[-1]
    assert kind == "risk_state"
    assert persisted["last_equity"] == 970.0
    assert persisted["reason"] == "account_update"


@pytest.mark.parametrize(
    ("payload_changes", "reason"),
    [
        ({"deployment_id": ""}, "deployment_identity_missing_from_journal"),
        (
            {"deployment_id": "deployment-b"},
            "deployment_identity_mismatch:deployment-b!=deployment-a",
        ),
        (
            {"deployment_policy_fingerprint": ""},
            "deployment_policy_missing_from_journal",
        ),
        (
            {"deployment_policy_fingerprint": "policy-b"},
            "deployment_policy_mismatch",
        ),
    ],
)
def test_deployment_identity_or_policy_drift_latches_failed_state(
    payload_changes,
    reason,
):
    halts = []
    journal = _Journal(
        [{"kind": "risk_state", "payload": _payload(**payload_changes)}]
    )
    repository = _repository(journal, halts=halts)

    assert repository.restore()
    assert repository.state.kill_switch_triggered
    assert repository.state.kill_state == "FAILED"
    assert repository.state.kill_reason == reason
    assert halts == []


@pytest.mark.parametrize(
    ("payload_changes", "detail"),
    [
        ({"day_start_equity": "NaN"}, "non_finite"),
        ({"deployment_loss": -1.0}, "negative_deployment_loss"),
        ({"kill_switch_triggered": "yes"}, "kill_latch_not_boolean"),
        ({"kill_state": "UNKNOWN"}, "invalid_kill_state:UNKNOWN"),
        (
            {"kill_switch_triggered": False, "kill_state": "FAILED"},
            "inconsistent_kill_latch",
        ),
    ],
)
def test_corrupt_payload_fails_closed_and_halts_oms(
    payload_changes,
    detail,
):
    halts = []
    repository = _repository(
        _Journal(
            [
                {
                    "kind": "risk_state",
                    "payload": _payload(**payload_changes),
                }
            ]
        ),
        halts=halts,
    )

    assert not repository.restore()
    assert repository.state.kill_switch_triggered
    assert repository.state.kill_state == "FAILED"
    assert repository.state.kill_reason == f"risk_state_corrupt:{detail}"
    assert halts == [f"RiskManager: risk_state_corrupt:{detail}"]


@pytest.mark.parametrize(
    ("operation", "context"),
    [
        ("restore", "restore_risk_state"),
        ("persist", "persist_risk_state"),
    ],
)
def test_journal_failures_use_explicit_durability_handler(
    operation,
    context,
):
    failures = []
    halts = []
    repository = _repository(
        _FailingJournal(fail_on=operation),
        durability_failures=failures,
        halts=halts,
    )

    result = (
        repository.restore()
        if operation == "restore"
        else repository.persist("test")
    )

    assert not result
    assert len(failures) == 1
    assert isinstance(failures[0][0], DurabilityError)
    assert failures[0][1] == context
    assert halts == []


class _Engine:
    def register(self, _event_type, _handler):
        pass


def test_risk_manager_compatibility_fields_are_owned_by_repository():
    manager = RiskManager(_Engine(), {"risk": {"active": True}})

    manager.kill_switch_triggered = True
    manager.kill_state = "FAILED"
    manager.kill_reason = "test"
    manager.initial_equity = 123.0

    owned = asdict(manager.risk_state_repository.state)
    assert owned["kill_switch_triggered"] is True
    assert owned["kill_state"] == "FAILED"
    assert owned["kill_reason"] == "test"
    assert owned["initial_equity"] == 123.0
    assert "kill_switch_triggered" not in manager.__dict__
    assert "initial_equity" not in manager.__dict__
