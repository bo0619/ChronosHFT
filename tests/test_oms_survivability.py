import sys
import types
import unittest

if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")
    requests_stub.get = lambda *args, **kwargs: None
    sys.modules["requests"] = requests_stub

from event.type import (
    Event,
    ExchangeOrderUpdate,
    ExecutionPolicy,
    EVENT_EXCHANGE_ORDER_UPDATE,
    LifecycleState,
    OrderIntent,
    Side,
)
from infrastructure.system_health import handle_system_health_event
from oms.engine import OMS
from oms.order import Order


class DummyEngine:
    def __init__(self):
        self.events = []

    def put(self, event):
        self.events.append(event)

    def register(self, _event_type, _handler):
        return None


class DummyGateway:
    def __init__(self):
        self.open_orders = []
        self.positions = []
        self.account = {
            "totalWalletBalance": "1000",
            "totalInitialMargin": "0",
        }
        self.cancelled_symbols = []

    def send_order(self, req, client_oid):
        return "ex-order"

    def cancel_order(self, req):
        return None

    def cancel_all_orders(self, symbol):
        self.cancelled_symbols.append(symbol)
        return None

    def get_account_info(self):
        return self.account

    def get_all_positions(self):
        return self.positions

    def get_open_orders(self):
        return self.open_orders


class DummyRiskController:
    def __init__(self):
        self.reasons = []

    def trigger_kill_switch(self, reason):
        self.reasons.append(reason)


class OMSSurvivabilityTests(unittest.TestCase):
    def make_config(self):
        return {
            "symbols": ["BTCUSDT"],
            "account": {
                "initial_balance_usdt": 1000.0,
                "leverage": 10,
            },
            "backtest": {
                "taker_fee": 0.0,
                "maker_fee": 0.0,
            },
            "oms": {
                "journal_enabled": False,
                "replay_journal_on_startup": False,
            },
            "risk": {
                "limits": {
                    "max_pos_notional": 5000.0,
                }
            },
        }

    def test_filled_close_books_realized_pnl_into_balance(self):
        gateway = DummyGateway()
        oms = OMS(DummyEngine(), gateway, self.make_config())
        try:
            oms.exposure.force_sync("BTCUSDT", 1.0, 100.0)
            oms.account.force_sync(1000.0, 0.0)

            intent = OrderIntent(
                "test",
                "BTCUSDT",
                Side.SELL,
                90.0,
                1.0,
                is_post_only=False,
                policy=ExecutionPolicy.AGGRESSIVE,
            )
            order = Order("oid-close", intent)
            order.mark_submitting()
            order.mark_pending_ack("ex-close")
            order.mark_new("ex-close", update_time=1.0, seq=1)
            oms.orders[order.client_oid] = order
            oms.exchange_id_map[order.exchange_oid] = order

            update = ExchangeOrderUpdate(
                client_oid="oid-close",
                exchange_oid="ex-close",
                symbol="BTCUSDT",
                status="FILLED",
                filled_qty=1.0,
                filled_price=90.0,
                cum_filled_qty=1.0,
                update_time=2.0,
                seq=2,
            )

            oms._apply_event(Event(EVENT_EXCHANGE_ORDER_UPDATE, update))

            self.assertAlmostEqual(oms.account.balance, 990.0)
            self.assertAlmostEqual(oms.account.equity, 990.0)
            self.assertAlmostEqual(oms.exposure.net_positions["BTCUSDT"], 0.0)
        finally:
            oms.stop()

    def test_full_reset_halts_if_remote_open_orders_survive_cancel_all(self):
        gateway = DummyGateway()
        gateway.open_orders = [
            {
                "symbol": "BTCUSDT",
                "orderId": 123,
                "clientOrderId": "orphan-1",
                "side": "BUY",
            }
        ]
        gateway.positions = []
        oms = OMS(DummyEngine(), gateway, self.make_config())
        try:
            oms._perform_full_reset()
            self.assertEqual(oms.state, LifecycleState.HALTED)
            self.assertIn("BTCUSDT", gateway.cancelled_symbols)
        finally:
            oms.stop()

    def test_reconcile_resets_on_orphan_remote_open_order(self):
        gateway = DummyGateway()
        gateway.positions = []
        gateway.open_orders = [
            {
                "symbol": "BTCUSDT",
                "orderId": 456,
                "clientOrderId": "ghost-456",
                "side": "SELL",
            }
        ]
        oms = OMS(DummyEngine(), gateway, self.make_config())
        try:
            called = []
            oms._perform_full_reset = lambda: called.append("reset")
            oms._execute_reconcile(None)
            self.assertEqual(called, ["reset"])
        finally:
            oms.stop()


class SystemHealthHandlerTests(unittest.TestCase):
    def test_non_halt_health_event_triggers_kill_switch(self):
        risk_controller = DummyRiskController()
        handle_system_health_event(Event("eSystemHealth", "FATAL_GAP"), risk_controller)
        self.assertEqual(risk_controller.reasons, ["SystemHealth: FATAL_GAP"])

    def test_halt_echo_does_not_retrigger_kill_switch(self):
        risk_controller = DummyRiskController()
        handle_system_health_event(Event("eSystemHealth", "HALT:already_halted"), risk_controller)
        self.assertEqual(risk_controller.reasons, [])


if __name__ == "__main__":
    unittest.main()
