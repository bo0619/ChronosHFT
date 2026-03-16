import json
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, mock_open, patch, call

if "requests" not in sys.modules:
    requests_module = types.ModuleType("requests")

    class Session:
        def __init__(self):
            self.headers = {}

        def mount(self, *args, **kwargs):
            return None

        def close(self):
            return None

    class Request:
        def __init__(self, *args, **kwargs):
            pass

    requests_module.Session = Session
    requests_module.Request = Request
    requests_module.get = lambda *args, **kwargs: None
    sys.modules["requests"] = requests_module

if "requests.adapters" not in sys.modules:
    adapters_module = types.ModuleType("requests.adapters")

    class HTTPAdapter:
        def __init__(self, *args, **kwargs):
            pass

        def init_poolmanager(self, *args, **kwargs):
            return None

    adapters_module.HTTPAdapter = HTTPAdapter
    sys.modules["requests.adapters"] = adapters_module

if "websocket" not in sys.modules:
    websocket_module = types.ModuleType("websocket")

    class WebSocketApp:
        def __init__(self, *args, **kwargs):
            pass

        def run_forever(self, *args, **kwargs):
            return None

    websocket_module.WebSocketApp = WebSocketApp
    sys.modules["websocket"] = websocket_module

from event.type import LifecycleState
from gateway.binance.gateway import BinanceGateway
from infrastructure.config_scaling import apply_capital_scaling
from oms.engine import OMS
from strategy.ml_sniper.config_loader import load_sniper_config
from strategy.ml_sniper.ml_sniper import MLSniperStrategy


class DummyEngine:
    def __init__(self):
        self.events = []

    def put(self, event):
        self.events.append(event)


class DummyGateway:
    def cancel_all_orders(self, symbol):
        return None


class DummyOMS:
    def __init__(self, leverage=5, max_order_notional=200.0):
        self.state = LifecycleState.LIVE
        self.config = {
            "account": {"leverage": leverage},
            "risk": {"limits": {"max_order_notional": max_order_notional}},
            "backtest": {
                "maker_fee": 0.0,
                "taker_fee": 0.0,
            },
        }
        self.exposure = SimpleNamespace(net_positions={})

    def cancel_order(self, client_oid):
        return None

    def cancel_all_orders(self, symbol):
        return None


class DummySession:
    def __init__(self):
        self.headers = {}

    def mount(self, *args, **kwargs):
        return None

    def close(self):
        return None


class LeverageAndLotMultiplierTests(unittest.TestCase):
    def test_load_sniper_config_inherits_top_level_strategy_fields(self):
        payload = {
            "strategy": {
                "name": "ML_Sniper",
                "lot_multiplier": 10.0,
                "ml_sniper": {
                    "weights": {"1s": 0.1, "10s": 0.5, "30s": 0.4}
                },
            }
        }

        with patch("builtins.open", mock_open(read_data=json.dumps(payload))):
            config = load_sniper_config()

        self.assertEqual(config["lot_multiplier"], 10.0)
        self.assertIn("weights", config)

    def test_capital_scaling_derives_runtime_limits_from_single_multiplier(self):
        payload = {
            "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"],
            "account": {"leverage": 5, "initial_balance_usdt": 100.0},
            "backtest": {"initial_capital": 100.0},
            "risk": {
                "limits": {
                    "max_order_qty": 10000.0,
                    "max_order_notional": 8.0,
                    "max_pos_notional": 16.0,
                    "max_account_gross_notional": 45.0,
                    "max_daily_loss": 5.0,
                }
            },
            "strategy": {
                "capital_multiplier": 2.0,
                "capital_scaling": {
                    "enabled": True,
                    "reference_capital_usdt": 100.0,
                    "target_order_notional": 8.0,
                    "target_total_risk_notional": 45.0,
                    "target_concurrent_symbols": 3,
                    "target_daily_loss": 5.0,
                    "max_order_qty": 10000.0,
                    "position_buffer_orders": 2.0,
                    "reference_min_notional": 5.0,
                    "notional_buffer": 1.1,
                },
            },
        }

        scaled = apply_capital_scaling(payload)

        self.assertEqual(scaled["account"]["initial_balance_usdt"], 200.0)
        self.assertEqual(scaled["backtest"]["initial_capital"], 200.0)
        self.assertEqual(scaled["risk"]["limits"]["max_order_notional"], 16.0)
        self.assertEqual(scaled["risk"]["limits"]["max_pos_notional"], 32.0)
        self.assertEqual(scaled["risk"]["limits"]["max_account_gross_notional"], 90.0)
        self.assertEqual(scaled["risk"]["limits"]["max_daily_loss"], 10.0)
        self.assertEqual(scaled["risk"]["limits"]["max_order_qty"], 20000.0)
        self.assertAlmostEqual(scaled["strategy"]["lot_multiplier"], 16.0 / 27.5, places=8)
        self.assertEqual(scaled["strategy"]["max_pos_usdt"], 32.0)

    def test_load_sniper_config_applies_capital_scaling_before_merge(self):
        payload = {
            "account": {"leverage": 5, "initial_balance_usdt": 100.0},
            "backtest": {"initial_capital": 100.0},
            "risk": {
                "limits": {
                    "max_order_qty": 10000.0,
                    "max_order_notional": 8.0,
                    "max_pos_notional": 16.0,
                    "max_account_gross_notional": 45.0,
                    "max_daily_loss": 5.0,
                }
            },
            "strategy": {
                "capital_multiplier": 2.0,
                "capital_scaling": {
                    "enabled": True,
                    "reference_capital_usdt": 100.0,
                    "target_order_notional": 8.0,
                    "target_total_risk_notional": 45.0,
                    "target_concurrent_symbols": 3,
                    "position_buffer_orders": 2.0,
                    "reference_min_notional": 5.0,
                    "notional_buffer": 1.1,
                },
                "ml_sniper": {
                    "weights": {"1s": 0.1, "10s": 0.5, "30s": 0.4}
                },
            },
        }

        with patch("builtins.open", mock_open(read_data=json.dumps(payload))):
            config = load_sniper_config()

        self.assertAlmostEqual(config["lot_multiplier"], 16.0 / 27.5, places=8)
        self.assertIn("weights", config)

    @patch("strategy.ml_sniper.ml_sniper.load_sniper_config", return_value={"lot_multiplier": 10.0})
    @patch("strategy.ml_sniper.ml_sniper.ref_data_manager.round_qty", side_effect=lambda symbol, qty: round(qty, 2))
    @patch("strategy.ml_sniper.ml_sniper.ref_data_manager.get_info", return_value=SimpleNamespace(min_qty=0.01, min_notional=5.0))
    def test_calc_vol_uses_lot_multiplier_and_leverage_with_risk_cap(self, _get_info, _round_qty, _load_cfg):
        strategy = MLSniperStrategy(DummyEngine(), DummyOMS(leverage=5, max_order_notional=200.0))

        qty = strategy._calc_vol("BTCUSDT", 100.0)

        self.assertEqual(strategy.lot_multiplier, 10.0)
        self.assertEqual(strategy.account_leverage, 5.0)
        self.assertEqual(qty, 1.9)

    def test_oms_sets_gateway_target_leverage_from_account_config(self):
        config = {
            "symbols": ["BTCUSDT"],
            "account": {
                "initial_balance_usdt": 1000.0,
                "leverage": 7,
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

        gateway = DummyGateway()
        oms = OMS(DummyEngine(), gateway, config)
        try:
            self.assertEqual(gateway.target_leverage, 7)
            self.assertEqual(oms.max_account_gross_notional, 4321.0)
        finally:
            oms.stop()

    def test_gateway_connect_applies_target_leverage_to_each_symbol(self):
        with patch("gateway.binance.gateway.requests.Session", return_value=DummySession()), patch(
            "gateway.binance.gateway.BinanceRestApi"
        ) as rest_cls, patch("gateway.binance.gateway.BinanceWsApi") as ws_cls:
            rest = rest_cls.return_value
            rest.create_listen_key.return_value = None
            ws = ws_cls.return_value
            ws.start_market_stream = MagicMock()

            gateway = BinanceGateway(DummyEngine(), "key", "secret", testnet=True)
            gateway._init_books = lambda: None
            gateway.target_leverage = 9
            gateway.connect(["BTCUSDT", "ETHUSDT"])

            self.assertEqual(rest.set_leverage.call_args_list, [call("BTCUSDT", 9), call("ETHUSDT", 9)])
            self.assertEqual(rest.set_margin_type.call_args_list, [call("BTCUSDT", "CROSSED"), call("ETHUSDT", "CROSSED")])
            ws.start_market_stream.assert_called_once_with(["BTCUSDT", "ETHUSDT"])
            gateway.close()


if __name__ == "__main__":
    unittest.main()
