import sys
import types
import unittest
from unittest.mock import patch

if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")
    requests_stub.get = lambda *args, **kwargs: None
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
    def make_validator(self):
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
            }
        )

    @patch("oms.validator.ref_data_manager.get_info", return_value=None)
    @patch("oms.validator.data_cache.get_best_quote", return_value=(99.5, 100.5))
    @patch("oms.validator.data_cache.get_mark_price", return_value=100.0)
    def test_rejects_order_notional_from_config(self, *_mocks):
        validator = self.make_validator()
        intent = OrderIntent("test", "BTCUSDT", Side.BUY, 50.0, 3.0)

        valid, reason = validator.validate_params(intent)

        self.assertFalse(valid)
        self.assertIn("notional_exceeded", reason)

    @patch("oms.validator.ref_data_manager.get_info", return_value=None)
    @patch("oms.validator.data_cache.get_best_quote", return_value=(99.9, 100.1))
    @patch("oms.validator.data_cache.get_mark_price", return_value=100.0)
    def test_rejects_price_deviation_from_config(self, *_mocks):
        validator = self.make_validator()
        intent = OrderIntent("test", "BTCUSDT", Side.BUY, 103.0, 0.5)

        valid, reason = validator.validate_params(intent)

        self.assertFalse(valid)
        self.assertIn("price_deviation", reason)

    @patch("oms.validator.ref_data_manager.get_info", return_value=None)
    @patch("oms.validator.data_cache.get_best_quote", return_value=(99.0, 101.0))
    @patch("oms.validator.data_cache.get_mark_price", return_value=100.0)
    def test_rejects_spread_from_config(self, *_mocks):
        validator = self.make_validator()
        intent = OrderIntent("test", "BTCUSDT", Side.BUY, 100.0, 0.5)

        valid, reason = validator.validate_params(intent)

        self.assertFalse(valid)
        self.assertIn("spread_too_wide", reason)

    @patch("oms.validator.ref_data_manager.get_info", return_value=None)
    @patch("oms.validator.data_cache.get_best_quote", return_value=(99.95, 100.05))
    @patch("oms.validator.data_cache.get_mark_price", return_value=100.0)
    def test_rejects_rate_limit_from_config(self, *_mocks):
        validator = self.make_validator()
        intent = OrderIntent("test", "BTCUSDT", Side.BUY, 100.0, 0.5)

        first_valid, _ = validator.validate_params(intent)
        second_valid, second_reason = validator.validate_params(intent)

        self.assertTrue(first_valid)
        self.assertFalse(second_valid)
        self.assertIn("rate_limit", second_reason)


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
        finally:
            oms.stop()


if __name__ == "__main__":
    unittest.main()
