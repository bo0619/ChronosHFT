import sys
import types
import unittest

if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")
    requests_stub.get = lambda *args, **kwargs: None
    requests_stub.Session = lambda *args, **kwargs: None
    requests_stub.Request = object
    sys.modules["requests"] = requests_stub

from event.type import LifecycleState
from oms.engine import OMS


class DummyEngine:
    def __init__(self):
        self.events = []

    def put(self, event):
        self.events.append(event)

    def register(self, _event_type, _handler):
        return None


class DummyGateway:
    def __init__(self):
        self.open_orders = None
        self.positions = None
        self.account = {
            "totalWalletBalance": "1000",
            "totalInitialMargin": "0",
            "availableBalance": "1000",
        }

    def send_order(self, req, client_oid):
        return "ex-order"

    def cancel_order(self, req):
        return None

    def cancel_all_orders(self, symbol):
        return None

    def get_account_info(self):
        return self.account

    def get_all_positions(self):
        return self.positions

    def get_open_orders(self):
        return self.open_orders


class OMSReconcileBackoffTests(unittest.TestCase):
    def make_config(self, failure_threshold=3):
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
                "reconcile_api_failure_threshold": failure_threshold,
            },
            "risk": {
                "limits": {
                    "max_pos_notional": 5000.0,
                }
            },
        }

    def test_transient_api_failure_returns_live(self):
        oms = OMS(DummyEngine(), DummyGateway(), self.make_config(failure_threshold=3))
        try:
            oms.state = LifecycleState.RECONCILING
            oms._execute_reconcile(None)
            self.assertEqual(oms.state, LifecycleState.LIVE)
        finally:
            oms.stop()

    def test_repeated_api_failure_eventually_halts(self):
        oms = OMS(DummyEngine(), DummyGateway(), self.make_config(failure_threshold=2))
        try:
            oms.state = LifecycleState.RECONCILING
            oms._execute_reconcile(None)
            self.assertEqual(oms.state, LifecycleState.LIVE)

            oms.state = LifecycleState.RECONCILING
            oms._execute_reconcile(None)
            self.assertEqual(oms.state, LifecycleState.HALTED)
        finally:
            oms.stop()


if __name__ == "__main__":
    unittest.main()