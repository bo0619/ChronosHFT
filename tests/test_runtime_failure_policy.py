from types import SimpleNamespace

import pytest

from infrastructure.runtime_failure_policy import RuntimeFailurePolicy


class _OMS:
    def __init__(self):
        self.calls = []
        self.state = SimpleNamespace(value="LIVE")
        self.last_freeze_reason = ""
        self.freeze_error = None

    def freeze_system(self, reason, *, cancel_active_orders):
        self.calls.append(("freeze_system", reason, cancel_active_orders))

    def halt_system(self, reason):
        self.calls.append(("halt_system", reason))

    def trigger_reconcile(self, reason):
        self.calls.append(("reconcile", reason))

    def freeze_strategy(self, name, reason, *, cancel_active_orders):
        if self.freeze_error is not None:
            raise self.freeze_error
        self.calls.append(
            ("freeze_strategy", name, reason, cancel_active_orders)
        )

    def freeze_venue(self, name, reason, *, cancel_active_orders):
        if self.freeze_error is not None:
            raise self.freeze_error
        self.calls.append(("freeze_venue", name, reason, cancel_active_orders))


class _Risk:
    def __init__(self):
        self.kills = []

    def trigger_kill_switch(self, reason):
        self.kills.append(reason)


class _Logger:
    def __init__(self):
        self.critical_messages = []

    def critical(self, message):
        self.critical_messages.append(message)


def _policy(*, strategy_name="maker"):
    oms = _OMS()
    risk = _Risk()
    logger = _Logger()
    policy = RuntimeFailurePolicy(
        oms=oms,
        risk_controller=risk,
        strategy_name=strategy_name,
        gateway_name="BINANCE",
        logger=logger,
    )
    return policy, oms, risk, logger


def test_clock_health_uses_startup_halt_then_runtime_kill():
    policy, oms, risk, _ = _policy()

    policy.on_time_service_health("freeze", "offset", {})
    policy.on_time_service_health("halt", "stale", {})
    policy.arm_clock_runtime()
    policy.on_time_service_health("halt", "lost", {})

    assert oms.calls == [
        ("freeze_system", "TimeSync: offset", True),
        ("halt_system", "TimeSync startup: stale"),
    ]
    assert risk.kills == ["TimeSync: lost"]


def test_clock_recovery_only_reconciles_its_own_freeze():
    policy, oms, _, _ = _policy()
    oms.state.value = "FROZEN"

    oms.last_freeze_reason = "Other: reason"
    policy.on_time_service_health("recovered", "ok", {})
    oms.last_freeze_reason = "TimeSync: offset"
    policy.on_time_service_health("recovered", "ok", {})

    assert oms.calls == [("reconcile", "Time sync recovered")]


def test_event_and_strategy_failures_target_the_correct_scope():
    policy, oms, _, _ = _policy()

    policy.on_event_engine_failure(
        {
            "lane": "cold",
            "event_type": "eOrderBook",
            "kind": "handler_exception",
            "handler_name": "on_book",
        }
    )
    policy.on_event_engine_failure(
        {
            "lane": "execution",
            "event_type": "eOrderUpdate",
            "kind": "queue_full",
            "handler_name": "on_order",
        }
    )
    policy.on_strategy_runtime_failure(
        {
            "phase": "dispatch",
            "kind": "handler_exception",
            "handler_name": "on_trade",
        }
    )

    assert oms.calls == [
        (
            "freeze_strategy",
            "maker",
            "event_engine_failure:handler_exception:cold:eOrderBook:on_book",
            True,
        ),
        (
            "freeze_venue",
            "BINANCE",
            "event_engine_failure:queue_full:execution:eOrderUpdate:on_order",
            True,
        ),
        (
            "freeze_strategy",
            "maker",
            "strategy_runtime_failure:dispatch:handler_exception:on_trade",
            True,
        ),
    ]


def test_freeze_failure_escalates_to_kill_switch():
    policy, oms, risk, logger = _policy()
    oms.freeze_error = RuntimeError("freeze failed")

    policy.on_strategy_runtime_failure({})

    assert risk.kills == [
        "strategy_runtime_failure:unknown:unknown:unavailable:"
        "freeze_failed:RuntimeError"
    ]
    assert "freeze failed" in logger.critical_messages[0]


def test_strategy_binding_is_explicit_and_single_target():
    policy, oms, risk, _ = _policy(strategy_name="")

    policy.on_event_engine_failure({"lane": "cold"})
    assert oms.calls == []
    assert risk.kills[-1].endswith("freeze_failed:RuntimeError")

    policy.bind_strategy("maker")
    policy.bind_strategy("maker")
    with pytest.raises(RuntimeError, match="already bound"):
        policy.bind_strategy("other")
