import sys
import types
import unittest
from unittest.mock import Mock

try:
    __import__("requests")
except ModuleNotFoundError:
    requests_stub = types.ModuleType("requests")
    requests_stub.get = lambda *args, **kwargs: None
    requests_stub.Session = lambda *args, **kwargs: None
    requests_stub.Request = object
    sys.modules["requests"] = requests_stub

from event.type import OrderIntent, Side
from oms.engine import OMS
from oms.validator import OrderValidator


class DummyEngine:
    def __init__(self):
        self.events = []

    def put(self, event):
        self.events.append(event)


class DummyGateway:
    def cancel_all_orders(self, symbol):
        return None


class OrderValidatorTests(unittest.TestCase):
    def make_validator(
        self,
        *,
        bid: float = 99.95,
        ask: float = 100.05,
        mark: float = 100.0,
    ):
        clock = Mock()
        clock.monotonic.return_value = 1.0
        market_cache = Mock()
        market_cache.get_risk_snapshot.return_value = {
            "mark_price": mark,
            "bid_price": bid,
            "ask_price": ask,
            "mark_age_ms": 0.0,
            "book_age_ms": 0.0,
        }
        market_cache.get_mark_price.return_value = mark
        market_cache.get_best_quote.return_value = (bid, ask)
        reference_data = Mock()
        reference_data.get_info.return_value = None
        return OrderValidator(
            {
                "risk": {
                    "limits": {
                        "max_order_qty": 10.0,
                        "max_order_notional": 100.0,
                    },
                    "price_sanity": {
                        "max_deviation_pct": 0.01,
                        "max_spread_pct": 0.015,
                    },
                    "tech_health": {
                        "max_order_count_per_sec": 1,
                    },
                }
            },
            clock=clock,
            market_cache=market_cache,
            reference_data=reference_data,
        )

    def test_rejects_order_notional_from_config(self):
        validator = self.make_validator(bid=99.5, ask=100.5)
        intent = OrderIntent("test", "BTCUSDT", Side.BUY, 50.0, 3.0)

        valid, reason = validator.validate_params(intent)

        self.assertFalse(valid)
        self.assertIn("notional_exceeded", reason)

    def test_rejects_price_deviation_from_config(self):
        validator = self.make_validator(bid=99.9, ask=100.1)
        intent = OrderIntent("test", "BTCUSDT", Side.BUY, 103.0, 0.5)

        valid, reason = validator.validate_params(intent)

        self.assertFalse(valid)
        self.assertIn("price_deviation", reason)

    def test_rejects_spread_from_config(self):
        validator = self.make_validator(bid=99.0, ask=101.0)
        intent = OrderIntent("test", "BTCUSDT", Side.BUY, 100.0, 0.5)

        valid, reason = validator.validate_params(intent)

        self.assertFalse(valid)
        self.assertIn("spread_too_wide", reason)

    def test_rejects_rate_limit_from_config(self):
        validator = self.make_validator()
        intent = OrderIntent("test", "BTCUSDT", Side.BUY, 100.0, 0.5)

        first_valid, _ = validator.validate_params(intent)
        second_valid, second_reason = validator.validate_params(intent)

        self.assertTrue(first_valid)
        self.assertFalse(second_valid)
        self.assertIn("rate_limit", second_reason)

    def test_reduce_only_has_an_independent_rate_limit_channel(self):
        validator = self.make_validator()
        opening = OrderIntent(
            "test",
            "BTCUSDT",
            Side.BUY,
            100.0,
            0.5,
        )
        reduction = OrderIntent(
            "test",
            "BTCUSDT",
            Side.SELL,
            100.0,
            0.5,
            reduce_only=True,
        )

        first_opening, _ = validator.validate_params(opening)
        reduce_valid, reduce_reason = validator.validate_params(reduction)
        second_opening, second_reason = validator.validate_params(opening)

        self.assertTrue(first_opening)
        self.assertTrue(reduce_valid, reduce_reason)
        self.assertFalse(second_opening)
        self.assertIn("rate_limit:risk", second_reason)


class OMSConfigTests(unittest.TestCase):
    def test_oms_uses_max_pos_notional_from_risk_limits(self):
        config = {
            "symbols": ["BTCUSDT"],
            "account": {
                "initial_balance_usdt": 1000.0,
                "leverage": 5,
            },
            "risk": {
                "limits": {
                    "max_pos_notional": 1234.0,
                    "max_account_gross_notional": 4321.0,
                }
            },
            "oms": {
                "journal_enabled": False,
                "replay_journal_on_startup": False,
            },
        }

        oms = OMS(DummyEngine(), DummyGateway(), config)
        try:
            self.assertEqual(oms.max_pos_notional, 1234.0)
            self.assertEqual(oms.max_account_gross_notional, 4321.0)
        finally:
            oms.stop()


if __name__ == "__main__":
    unittest.main()
