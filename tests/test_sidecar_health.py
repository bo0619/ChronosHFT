from types import SimpleNamespace

from event.type import OMSCapabilityMode
from risk.sidecar_health import SidecarOmsHealth


class _Oms:
    def __init__(self, constrained=False):
        self.constrained = constrained
        self.heartbeats = []
        self.mode_changes = []
        self.clear_calls = []
        self.halt_reasons = []

    def record_risk_control_heartbeat(self, **payload):
        self.heartbeats.append(payload)
        return True

    def has_trading_mode_constraint(self, prefixes):
        assert prefixes == ("independent_supervisor:",)
        return self.constrained

    def set_trading_mode(self, mode, reason):
        self.mode_changes.append((mode, reason))

    def clear_trading_mode(self, **payload):
        self.clear_calls.append(payload)

    def halt_system(self, reason):
        self.halt_reasons.append(reason)


class _Owner:
    def __init__(self, oms=None):
        self.oms = oms or _Oms()
        self.risk_manager = None
        self.last_status = {}
        self.recovery_count = 0
        self.last_recovery_snapshot_sequence = 0
        self.recovery_checks = 2
        self.recovery_snapshot_max_age_sec = 2.0

    def _record_oms_heartbeat(self, healthy, reason):
        return SidecarOmsHealth.record_heartbeat(
            self,
            healthy,
            reason,
            "independent_supervisor",
        )

    def _reset_recovery_progress(self):
        SidecarOmsHealth.reset_recovery_progress(self)


def test_unhealthy_kill_propagates_and_forces_reduce_only():
    owner = _Owner()
    kill_reasons = []
    owner.risk_manager = SimpleNamespace(
        trigger_kill_switch=kill_reasons.append
    )
    owner.last_status = {
        "risk_action": "KILL",
        "risk_snapshot_sequence": 7,
    }
    owner.recovery_count = 1

    result = SidecarOmsHealth.apply(
        owner,
        False,
        "maintenance_margin_kill",
        lambda: 10.0,
        lambda value: True,
    )

    assert result is False
    assert owner.oms.heartbeats == [
        {
            "source": "independent_supervisor",
            "healthy": False,
            "reason": "maintenance_margin_kill",
        }
    ]
    assert kill_reasons == [
        "IndependentSupervisor: maintenance_margin_kill"
    ]
    assert owner.oms.mode_changes == [
        (
            OMSCapabilityMode.REDUCE_ONLY,
            "independent_supervisor:maintenance_margin_kill",
        )
    ]
    assert owner.recovery_count == 0
    assert owner.last_recovery_snapshot_sequence == 7


def test_recovery_requires_distinct_fresh_snapshot_sequences():
    owner = _Owner(_Oms(constrained=True))
    owner.last_status = {
        "risk_snapshot_sequence": 1,
        "risk_snapshot_captured_monotonic": 10.0,
    }

    first = SidecarOmsHealth.apply(
        owner,
        True,
        "",
        lambda: 10.1,
        lambda value: True,
    )
    repeated = SidecarOmsHealth.apply(
        owner,
        True,
        "",
        lambda: 10.2,
        lambda value: True,
    )
    owner.last_status = {
        "risk_snapshot_sequence": 2,
        "risk_snapshot_captured_monotonic": 10.3,
    }
    recovered = SidecarOmsHealth.apply(
        owner,
        True,
        "",
        lambda: 10.4,
        lambda value: True,
    )

    assert first is False
    assert repeated is False
    assert recovered is True
    assert owner.oms.clear_calls == [
        {
            "reason": "independent risk supervisor recovered",
            "prefixes": ("independent_supervisor:",),
        }
    ]
    assert owner.recovery_count == 0
    assert owner.last_recovery_snapshot_sequence == 2


def test_invalid_recovery_snapshot_resets_progress():
    owner = _Owner(_Oms(constrained=True))
    owner.recovery_count = 1
    owner.last_recovery_snapshot_sequence = 5
    owner.last_status = {
        "risk_snapshot_sequence": 6,
        "risk_snapshot_captured_monotonic": 7.0,
    }

    recovered = SidecarOmsHealth.apply(
        owner,
        True,
        "",
        lambda: 10.0,
        lambda value: True,
    )

    assert recovered is False
    assert owner.recovery_count == 0
    assert owner.last_recovery_snapshot_sequence == 6
    assert owner.oms.clear_calls == []


class _Process:
    def __init__(self, alive=True):
        self.alive = alive

    def is_alive(self):
        return self.alive


def _tick_owner(**overrides):
    health_calls = []
    heartbeat_calls = []
    drain_calls = []
    owner = SimpleNamespace(
        enabled=True,
        process=_Process(),
        last_status={"healthy": True, "reason": ""},
        last_status_received_at=9.0,
        last_status_protocol_error="",
        status_max_age_sec=2.0,
        _send_heartbeat=heartbeat_calls.append,
        _drain_status=drain_calls.append,
        _apply_oms_health=lambda healthy, reason: (
            health_calls.append((healthy, reason)) or healthy
        ),
    )
    for field, value in overrides.items():
        setattr(owner, field, value)
    return owner, health_calls, heartbeat_calls, drain_calls


def test_tick_uses_facades_and_applies_fresh_child_status():
    owner, health_calls, heartbeat_calls, drain_calls = _tick_owner()

    result = SidecarOmsHealth.tick(owner, lambda: 10.0)

    assert result is True
    assert heartbeat_calls == [10.0]
    assert drain_calls == [10.0]
    assert health_calls == [(True, "")]


def test_tick_fails_closed_for_protocol_error_and_stale_status():
    owner, health_calls, _, _ = _tick_owner(
        last_status={},
        last_status_protocol_error="status_healthy_invalid",
    )

    assert SidecarOmsHealth.tick(owner, lambda: 10.0) is False
    assert health_calls == [
        (False, "supervisor_status_invalid:status_healthy_invalid")
    ]

    stale, stale_calls, _, _ = _tick_owner(last_status_received_at=7.0)
    assert SidecarOmsHealth.tick(stale, lambda: 10.0) is False
    assert stale_calls == [(False, "supervisor_status_stale")]


def test_wait_until_healthy_calls_owner_tick_until_recovery():
    tick_results = iter((False, True))
    sleeps = []
    owner = SimpleNamespace(
        enabled=True,
        tick=lambda: next(tick_results),
    )

    assert SidecarOmsHealth.wait_until_healthy(
        owner,
        1.0,
        lambda: 0.0,
        sleeps.append,
    )
    assert sleeps == [0.05]
