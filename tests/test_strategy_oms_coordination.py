import sys
import types
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")
    requests_stub.get = lambda *args, **kwargs: None
    sys.modules["requests"] = requests_stub

from event.type import (
    AccountData,
    Event,
    LifecycleState,
    OrderIntent,
    OrderStatus,
    Side,
    EVENT_ACCOUNT_UPDATE,
    EVENT_ORDER_UPDATE,
    EVENT_STRATEGY_UPDATE,
    EVENT_SYSTEM_HEALTH,
)
from oms.engine import OMS
from strategy.base import StrategyTemplate
from strategy.ml_sniper.ml_sniper import MLSniperStrategy


class DispatchingEngine:
    def __init__(self):
        self.events = []
        self.handlers = {}

    def put(self, event):
        self.events.append(event)
        for handler in self.handlers.get(event.type, []):
            handler(event)

    def register(self, event_type, handler):
        self.handlers.setdefault(event_type, []).append(handler)


class DummyGateway:
    def __init__(self, send_order_result="ex-order"):
        self.send_order_result = send_order_result

    def send_order(self, req, client_oid):
        return self.send_order_result

    def cancel_order(self, req):
        return None

    def cancel_all_orders(self, symbol):
        return None

    def get_account_info(self):
        return {
            "totalWalletBalance": "1000",
            "totalInitialMargin": "0",
            "availableBalance": "1000",
        }

    def get_all_positions(self):
        return []

    def get_open_orders(self):
        return []


class DummyStrategy(StrategyTemplate):
    def on_orderbook(self, orderbook):
        return None


class StrategyOmsCoordinationTests(unittest.TestCase):
    def make_config(self, max_order_notional=5000.0, max_account_gross_notional=0.0):
        return {
            "symbols": ["BTCUSDT"],
            "account": {
                "initial_balance_usdt": 1000.0,
                "leverage": 10,
            },
            "risk": {
                "limits": {
                    "max_order_qty": 100.0,
                    "max_order_notional": max_order_notional,
                    "max_pos_notional": 5000.0,
                    "max_account_gross_notional": max_account_gross_notional,
                },
                "price_sanity": {
                    "max_deviation_pct": 0.05,
                    "max_spread_pct": 0.05,
                },
                "tech_health": {
                    "max_order_count_per_sec": 10,
                },
            },
            "oms": {
                "journal_enabled": False,
                "replay_journal_on_startup": False,
            },
            "backtest": {
                "maker_fee": 0.0,
                "taker_fee": 0.0,
            },
        }

    @patch("oms.validator.ref_data_manager.get_info", return_value=None)
    @patch("oms.validator.data_cache.get_best_quote", return_value=(99.9, 100.1))
    @patch("oms.validator.data_cache.get_mark_price", return_value=100.0)
    def test_oms_validation_reject_reason_reaches_strategy(self, *_mocks):
        engine = DispatchingEngine()
        gateway = DummyGateway(send_order_result="ex-order")
        oms = OMS(engine, gateway, self.make_config(max_order_notional=50.0))
        strategy = DummyStrategy(engine, oms)
        engine.register(EVENT_ORDER_UPDATE, lambda event: strategy.on_order(event.data))
        oms.state = LifecycleState.LIVE
        try:
            oid = strategy.send_intent(OrderIntent("dummy", "BTCUSDT", Side.BUY, 100.0, 1.0))

            self.assertIsNone(oid)
            self.assertIn("notional_exceeded", strategy.last_submit_reject_reason)
            order_updates = [event.data for event in engine.events if event.type == EVENT_ORDER_UPDATE]
            self.assertEqual(len(order_updates), 1)
            self.assertEqual(order_updates[0].status, OrderStatus.REJECTED_LOCALLY)
            self.assertIn("notional_exceeded", order_updates[0].error_msg)
        finally:
            oms.stop()

    @patch("oms.validator.ref_data_manager.get_info", return_value=None)
    @patch("oms.validator.data_cache.get_best_quote", return_value=(99.9, 100.1))
    @patch("oms.validator.data_cache.get_mark_price", return_value=100.0)
    def test_gateway_send_failed_reason_reaches_strategy(self, *_mocks):
        engine = DispatchingEngine()
        gateway = DummyGateway(send_order_result=None)
        oms = OMS(engine, gateway, self.make_config())
        strategy = DummyStrategy(engine, oms)
        engine.register(EVENT_ORDER_UPDATE, lambda event: strategy.on_order(event.data))
        oms.state = LifecycleState.LIVE
        try:
            oid = strategy.send_intent(OrderIntent("dummy", "BTCUSDT", Side.BUY, 100.0, 1.0))

            self.assertIsNone(oid)
            self.assertEqual(strategy.last_submit_reject_reason, "gateway_send_failed")
            order_updates = [event.data for event in engine.events if event.type == EVENT_ORDER_UPDATE]
            self.assertEqual(len(order_updates), 1)
            self.assertEqual(order_updates[0].status, OrderStatus.REJECTED_LOCALLY)
            self.assertEqual(order_updates[0].error_msg, "gateway_send_failed")
        finally:
            oms.stop()

    def test_ml_sniper_publishes_account_health_and_reject_context(self):
        engine = DispatchingEngine()
        oms = SimpleNamespace(
            state=LifecycleState.LIVE,
            config={"backtest": {"maker_fee": 0.0, "taker_fee": 0.0}},
            exposure=SimpleNamespace(net_positions={}),
        )
        strategy = MLSniperStrategy(engine, oms)

        engine.register(EVENT_ACCOUNT_UPDATE, lambda event: strategy.on_account_update(event.data))
        engine.register(EVENT_SYSTEM_HEALTH, lambda event: strategy.on_system_health(event.data))

        engine.put(
            Event(
                EVENT_ACCOUNT_UPDATE,
                AccountData(
                    balance=1000.0,
                    equity=995.0,
                    available=900.0,
                    used_margin=95.0,
                    datetime=datetime.utcnow(),
                ),
            )
        )
        engine.put(Event(EVENT_SYSTEM_HEALTH, "HALT:test_gateway"))
        strategy.last_submit_reject_by_symbol["BTCUSDT"] = "insufficient_margin"

        predictor = strategy._get_predictor("BTCUSDT")
        strategy._publish_state(
            "BTCUSDT",
            mid=100.0,
            bid_1=99.9,
            ask_1=100.1,
            signal=2.0,
            velocity=0.0,
            preds={"1s": 1.0, "10s": 2.0, "30s": 1.5},
            predictor=predictor,
        )

        update = [event.data for event in engine.events if event.type == EVENT_STRATEGY_UPDATE][-1]
        self.assertEqual(update.params["Avail"], "900.0")
        self.assertEqual(update.params["Health"], "HALT:test_gateway")
        self.assertEqual(update.params["Reject"], "insufficient_margin")

    @patch("oms.validator.ref_data_manager.get_info", return_value=None)
    @patch("oms.validator.data_cache.get_best_quote", return_value=(99.9, 100.1))
    @patch("oms.validator.data_cache.get_mark_price", return_value=100.0)
    @patch("oms.exposure.data_cache.get_best_quote", return_value=(99.9, 100.1))
    @patch("oms.exposure.data_cache.get_mark_price", return_value=100.0)
    def test_account_gross_limit_rejects_when_total_multi_symbol_risk_is_full(self, *_mocks):
        engine = DispatchingEngine()
        gateway = DummyGateway(send_order_result="ex-order")
        config = self.make_config(max_order_notional=5000.0, max_account_gross_notional=150.0)
        config["symbols"] = ["BTCUSDT", "ETHUSDT"]
        oms = OMS(engine, gateway, config)
        strategy = DummyStrategy(engine, oms)
        engine.register(EVENT_ORDER_UPDATE, lambda event: strategy.on_order(event.data))
        oms.state = LifecycleState.LIVE
        oms.exposure.net_positions["ETHUSDT"] = 1.2
        try:
            oid = strategy.send_intent(OrderIntent("dummy", "BTCUSDT", Side.BUY, 100.0, 0.4))

            self.assertIsNone(oid)
            self.assertIn("Account Gross Exposure", strategy.last_submit_reject_reason)
            order_updates = [event.data for event in engine.events if event.type == EVENT_ORDER_UPDATE]
            self.assertEqual(len(order_updates), 1)
            self.assertEqual(order_updates[0].status, OrderStatus.REJECTED_LOCALLY)
            self.assertIn("Account Gross Exposure", order_updates[0].error_msg)
        finally:
            oms.stop()


if __name__ == "__main__":
    unittest.main()
