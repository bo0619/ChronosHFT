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
    for removed_facade in (
        "_kill_supervisor_thread",
        "_kill_empty_order_snapshots",
        "frozen_symbols",
        "symbol_freeze_owners",
        "latency_breach_count",
        "last_market_latency_ms",
        "on_mark_price",
        "on_orderbook",
        "on_account_update",
    ):
        assert not hasattr(manager, removed_facade)

    manager.kill_switch._kill_empty_order_snapshots = 2
    manager.scope_guards.frozen_symbols["BTCUSDT"] = "latency:test"

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
