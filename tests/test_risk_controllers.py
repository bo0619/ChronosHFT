from risk.account_risk import AccountRiskController
from risk.kill_switch import RiskKillSwitchController
from risk.manager import RiskManager
from risk.market_risk import MarketRiskController
from risk.scope_guards import RiskScopeGuardController


class _Engine:
    def register(self, *_args, **_kwargs):
        return None

    def put(self, *_args, **_kwargs):
        return None


def test_kill_and_scope_runtime_state_is_not_owned_by_manager():
    manager = RiskManager(_Engine(), {"risk": {"active": True}})

    assert isinstance(manager.kill_switch, RiskKillSwitchController)
    assert isinstance(manager.scope_guards, RiskScopeGuardController)
    assert isinstance(manager.market_risk, MarketRiskController)
    assert isinstance(manager.account_risk, AccountRiskController)
    assert "_kill_supervisor_thread" not in vars(manager)
    assert "_kill_empty_order_snapshots" not in vars(manager)
    assert "frozen_symbols" not in vars(manager)
    assert "symbol_freeze_owners" not in vars(manager)
    assert "latency_breach_count" not in vars(manager)

    manager._kill_empty_order_snapshots = 2
    manager.frozen_symbols["BTCUSDT"] = "latency:test"

    assert manager.kill_switch.runtime.empty_order_snapshots == 2
    assert manager.scope_guards.frozen_symbols == {
        "BTCUSDT": "latency:test"
    }


def test_extracted_controllers_have_no_manager_or_owner_back_reference():
    manager = RiskManager(_Engine(), {"risk": {"active": True}})

    for controller in (
        manager.kill_switch,
        manager.scope_guards,
        manager.market_risk,
        manager.account_risk,
    ):
        assert not hasattr(controller, "manager")
        assert not hasattr(controller, "owner")
        assert not hasattr(controller, "_owner")
