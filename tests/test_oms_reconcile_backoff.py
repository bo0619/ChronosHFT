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
    def make_config(self, failure_threshold=3, min_interval=5.0, cooldown=10.0):
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
                "reconcile_min_interval_sec": min_interval,
                "reconcile_api_cooldown_sec": cooldown,
            },
            "risk": {
                "limits": {
                    "max_pos_notional": 5000.0,
                }
            },
        }

    def test_transient_api_failure_keeps_system_frozen(self):
        oms = OMS(DummyEngine(), DummyGateway(), self.make_config(failure_threshold=3))
        try:
            scheduled = []
            oms._schedule_reconcile_retry = lambda reason, suspicious_oid=None, delay_sec=None: scheduled.append(
                (reason, suspicious_oid, delay_sec)
            )
            oms.state = LifecycleState.RECONCILING
            oms._execute_reconcile(None)
            self.assertEqual(oms.state, LifecycleState.FROZEN)
            self.assertEqual(oms.consecutive_reconcile_api_failures, 1)
            self.assertEqual(len(scheduled), 1)
        finally:
            oms.stop()

    def test_repeated_api_failure_eventually_halts(self):
        oms = OMS(
            DummyEngine(),
            DummyGateway(),
            self.make_config(failure_threshold=2, min_interval=0.0, cooldown=0.0),
        )
        try:
            oms._schedule_reconcile_retry = lambda reason, suspicious_oid=None, delay_sec=None: None
            oms.state = LifecycleState.RECONCILING
            oms._execute_reconcile(None)
            self.assertEqual(oms.state, LifecycleState.FROZEN)

            oms.state = LifecycleState.RECONCILING
            oms._execute_reconcile(None)
            self.assertEqual(oms.state, LifecycleState.HALTED)
            self.assertTrue(oms.manual_rearm_required)
        finally:
            oms.stop()


if __name__ == "__main__":
    unittest.main()
