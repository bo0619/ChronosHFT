import json
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

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
from infrastructure.config_scaling import (
    CASH_FLOW_TRUTH_DEFAULTS,
    INDEPENDENT_RISK_SUPERVISOR_DEFAULTS,
    MARKET_DATA_FRESHNESS_DEFAULTS,
    OUTBOUND_MESSAGE_BUDGET_DEFAULTS,
    RISK_CONTROL_HEARTBEAT_DEFAULTS,
    SELF_TRADE_PREVENTION_DEFAULTS,
    SINGLE_WRITER_FENCE_DEFAULTS,
    STRATEGY_RISK_BUDGET_DEFAULTS,
    VENUE_DEAD_MAN_SWITCH_DEFAULTS,
    apply_capital_scaling,
    apply_production_safety_defaults,
    finalize_strategy_risk_budgets,
    load_config_document,
    load_root_config,
    normalize_strategy_registration,
    normalize_root_config_preapproval,
    resolve_runtime_secrets,
)
from infrastructure.paper_trade import apply_paper_trade_mode, is_paper_trade
from main import run_live_risk_checks, wait_for_initial_market_data_readiness
from oms.engine import OMS


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
            "account": {"leverage": leverage, "margin_type": "ISOLATED", "position_mode": "ONE_WAY"},
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
    @staticmethod
    def normalize_runtime_config(payload):
        return resolve_runtime_secrets(
            normalize_root_config_preapproval(payload)
        )

    def test_live_risk_checks_poll_market_data_freshness(self):
        risk = SimpleNamespace(freshness_checks=0)

        def check_market_data_freshness():
            risk.freshness_checks += 1

        risk.check_market_data_freshness = check_market_data_freshness
        risk.check_funding_guard = lambda: True

        run_live_risk_checks(risk)

        self.assertEqual(risk.freshness_checks, 1)

    def test_live_risk_checks_ticks_independent_supervisor_first(self):
        calls = []
        supervisor = SimpleNamespace(tick=lambda: calls.append("supervisor") or True)
        risk = SimpleNamespace(
            check_funding_guard=lambda: calls.append("funding") or True,
            check_market_data_freshness=lambda: calls.append("risk") or True
        )

        self.assertTrue(run_live_risk_checks(risk, supervisor))
        self.assertEqual(calls, ["supervisor", "funding", "risk"])

    def test_initial_market_data_readiness_waits_for_recovery(self):
        readiness = MagicMock(
            side_effect=[
                {"BTCUSDT": "stale_market_data:book_age=1600.0ms"},
                {},
            ]
        )
        risk = SimpleNamespace(
            market_data_readiness_failures=readiness,
            check_funding_guard=MagicMock(return_value=True),
            check_market_data_freshness=MagicMock(return_value=True),
        )
        supervisor = SimpleNamespace(tick=MagicMock(return_value=True))

        with patch("main.time.sleep") as sleep:
            ready = wait_for_initial_market_data_readiness(
                risk,
                supervisor,
                timeout_sec=1.0,
                poll_interval_sec=0.01,
            )

        self.assertTrue(ready)
        self.assertEqual(readiness.call_count, 2)
        self.assertEqual(risk.check_funding_guard.call_count, 2)
        self.assertEqual(risk.check_market_data_freshness.call_count, 2)
        self.assertEqual(supervisor.tick.call_count, 2)
        sleep.assert_called_once_with(0.01)

    def test_initial_market_data_readiness_times_out_fail_closed(self):
        failures = {"BTCUSDT": "stale_market_data:book_unavailable"}
        risk = SimpleNamespace(
            market_data_readiness_failures=MagicMock(return_value=failures),
            check_funding_guard=MagicMock(return_value=True),
            check_market_data_freshness=MagicMock(return_value=True),
        )

        with patch("main.logger.error") as log_error:
            ready = wait_for_initial_market_data_readiness(
                risk,
                timeout_sec=0.0,
            )

        self.assertFalse(ready)
        risk.market_data_readiness_failures.assert_called_once_with()
        log_error.assert_called_once_with(
            f"Initial market-data readiness timed out: {failures}"
        )

    def test_root_config_injects_fail_closed_market_freshness_defaults(self):
        payload = {"symbols": ["BTCUSDT"], "risk": {"limits": {}}}

        loaded = self.normalize_runtime_config(payload)

        self.assertEqual(
            loaded["risk"]["market_data_freshness"],
            MARKET_DATA_FRESHNESS_DEFAULTS,
        )

    def test_root_config_preserves_explicit_market_freshness_overrides(self):
        payload = {
            "risk": {
                "market_data_freshness": {
                    "enabled": False,
                    "max_book_age_ms": 750.0,
                    "breach_checks": 4,
                }
            }
        }

        loaded = self.normalize_runtime_config(payload)

        freshness = loaded["risk"]["market_data_freshness"]
        self.assertFalse(freshness["enabled"])
        self.assertEqual(freshness["max_book_age_ms"], 750.0)
        self.assertEqual(freshness["breach_checks"], 4)
        self.assertEqual(freshness["require_mark_price"], True)
        self.assertEqual(freshness["recovery_checks"], 5)

    def test_root_config_injects_fail_closed_cash_flow_truth_defaults(self):
        payload = {"symbols": ["BTCUSDT"], "risk": {"limits": {}}}

        loaded = self.normalize_runtime_config(payload)

        self.assertEqual(
            loaded["risk"]["cash_flow_truth"],
            CASH_FLOW_TRUTH_DEFAULTS,
        )

    def test_safety_defaults_preserve_explicit_cash_flow_truth_overrides(self):
        payload = {
            "risk": {
                "cash_flow_truth": {
                    "enabled": False,
                    "max_snapshot_age_sec": 10.0,
                    "external_income_types": ["TRANSFER", "WELCOME_BONUS"],
                }
            }
        }

        loaded = apply_production_safety_defaults(payload)

        cash_flow = loaded["risk"]["cash_flow_truth"]
        self.assertFalse(cash_flow["enabled"])
        self.assertEqual(cash_flow["max_snapshot_age_sec"], 10.0)
        self.assertEqual(
            cash_flow["external_income_types"],
            ["TRANSFER", "WELCOME_BONUS"],
        )
        self.assertTrue(cash_flow["require_snapshot"])
        self.assertEqual(cash_flow["recovery_checks"], 2)

    def test_root_config_injects_fail_closed_risk_heartbeat_defaults(self):
        payload = {"symbols": ["BTCUSDT"], "risk": {"limits": {}}}

        loaded = self.normalize_runtime_config(payload)

        self.assertEqual(
            loaded["risk"]["risk_control_heartbeat"],
            RISK_CONTROL_HEARTBEAT_DEFAULTS,
        )

    def test_safety_defaults_preserve_explicit_risk_heartbeat_overrides(self):
        payload = {
            "risk": {
                "risk_control_heartbeat": {
                    "enabled": False,
                    "max_age_sec": 10.0,
                }
            }
        }

        loaded = apply_production_safety_defaults(payload)

        heartbeat = loaded["risk"]["risk_control_heartbeat"]
        self.assertFalse(heartbeat["enabled"])
        self.assertEqual(heartbeat["max_age_sec"], 10.0)

    def test_root_config_enables_independent_risk_supervisor(self):
        payload = {"symbols": ["BTCUSDT"], "risk": {"limits": {}}}

        loaded = self.normalize_runtime_config(payload)

        supervisor = loaded["risk"]["independent_supervisor"]
        self.assertEqual(
            {
                key: supervisor[key]
                for key in INDEPENDENT_RISK_SUPERVISOR_DEFAULTS
            },
            INDEPENDENT_RISK_SUPERVISOR_DEFAULTS,
        )
        self.assertEqual(supervisor["api_key"], "")
        self.assertEqual(supervisor["api_secret"], "")
        self.assertEqual(
            loaded["risk"]["risk_control_heartbeat"]["required_source"],
            "independent_supervisor",
        )

    def test_safety_defaults_use_local_heartbeat_when_supervisor_disabled(self):
        payload = {
            "risk": {
                "independent_supervisor": {"enabled": False},
            }
        }

        loaded = apply_production_safety_defaults(payload)

        self.assertEqual(
            loaded["risk"]["risk_control_heartbeat"]["required_source"],
            "risk_manager",
        )

    def test_root_config_resolves_binance_credentials_from_environment(self):
        payload = {
            "api_key_env": "CHRONOS_TEST_API_KEY",
            "api_secret_env": "CHRONOS_TEST_API_SECRET",
        }

        with patch.dict(
            "os.environ",
            {
                "CHRONOS_TEST_API_KEY": "environment-key",
                "CHRONOS_TEST_API_SECRET": "environment-secret",
            },
        ):
            loaded = self.normalize_runtime_config(payload)

        self.assertEqual(loaded["api_key"], "environment-key")
        self.assertEqual(loaded["api_secret"], "environment-secret")

    def test_root_config_resolves_separate_risk_sidecar_credentials(self):
        payload = {
            "risk": {
                "independent_supervisor": {
                    "api_key_env": "CHRONOS_TEST_RISK_KEY",
                    "api_secret_env": "CHRONOS_TEST_RISK_SECRET",
                }
            }
        }

        with patch.dict(
            "os.environ",
            {
                "CHRONOS_TEST_RISK_KEY": "risk-only-key",
                "CHRONOS_TEST_RISK_SECRET": "risk-only-secret",
            },
        ):
            loaded = self.normalize_runtime_config(payload)

        supervisor = loaded["risk"]["independent_supervisor"]
        self.assertEqual(supervisor["api_key"], "risk-only-key")
        self.assertEqual(supervisor["api_secret"], "risk-only-secret")

    def test_paper_manifest_does_not_embed_api_credentials(self):
        configured = load_config_document("config.json")

        self.assertNotIn("api_key", configured)
        self.assertNotIn("api_secret", configured)
        self.assertNotIn("api_key_env", configured)
        self.assertNotIn("api_secret_env", configured)

    def test_root_config_injects_outbound_message_budget_defaults(self):
        payload = {"symbols": ["BTCUSDT"], "oms": {}, "risk": {}}

        loaded = self.normalize_runtime_config(payload)

        self.assertEqual(
            loaded["oms"]["outbound_message_budget"],
            OUTBOUND_MESSAGE_BUDGET_DEFAULTS,
        )

    def test_root_config_preserves_message_budget_overrides(self):
        payload = {
            "oms": {
                "outbound_message_budget": {
                    "enabled": False,
                    "max_new_orders_per_window": 3,
                }
            }
        }

        loaded = self.normalize_runtime_config(payload)

        budget = loaded["oms"]["outbound_message_budget"]
        self.assertFalse(budget["enabled"])
        self.assertEqual(budget["max_new_orders_per_window"], 3)
        self.assertEqual(budget["reserved_risk_messages_per_window"], 5)

    def test_root_config_injects_self_trade_prevention_defaults(self):
        payload = {"symbols": ["BTCUSDT"], "oms": {}, "risk": {}}

        loaded = self.normalize_runtime_config(payload)

        self.assertEqual(
            loaded["oms"]["self_trade_prevention"],
            SELF_TRADE_PREVENTION_DEFAULTS,
        )

    def test_root_config_preserves_self_trade_prevention_overrides(self):
        payload = {
            "oms": {
                "self_trade_prevention": {
                    "enabled": False,
                    "exchange_mode": "EXPIRE_BOTH",
                }
            }
        }

        loaded = self.normalize_runtime_config(payload)

        stp = loaded["oms"]["self_trade_prevention"]
        self.assertFalse(stp["enabled"])
        self.assertEqual(stp["exchange_mode"], "EXPIRE_BOTH")
        self.assertTrue(stp["local_cross_check"])

    def test_root_config_enables_single_writer_fence(self):
        payload = {"symbols": ["BTCUSDT"], "oms": {}, "risk": {}}

        loaded = self.normalize_runtime_config(payload)

        self.assertEqual(
            loaded["oms"]["single_writer_fence"],
            SINGLE_WRITER_FENCE_DEFAULTS,
        )

    def test_root_config_enables_venue_dead_man_switch(self):
        payload = {"symbols": ["BTCUSDT"], "oms": {}, "risk": {}}

        loaded = self.normalize_runtime_config(payload)

        self.assertEqual(
            loaded["oms"]["venue_dead_man_switch"],
            VENUE_DEAD_MAN_SWITCH_DEFAULTS,
        )

    def test_root_config_creates_explicit_budget_for_configured_strategy(self):
        payload = {
            "symbols": ["BTCUSDT"],
            "strategy": {
                "name": "GLFT_MultiScale",
                "registered_models": ["GLFT", "as"],
            },
            "risk": {
                "limits": {
                    "max_pos_notional": 250.0,
                    "max_account_gross_notional": 500.0,
                }
            },
        }

        loaded = self.normalize_runtime_config(payload)

        budget_config = loaded["risk"]["strategy_risk_budgets"]
        self.assertTrue(budget_config["enabled"])
        self.assertTrue(budget_config["require_explicit_strategy"])
        self.assertEqual(
            budget_config["budgets"]["GLFT_MultiScale"]["max_gross_notional"],
            500.0,
        )
        self.assertEqual(
            budget_config["budgets"]["GLFT_MultiScale"]["max_symbol_notional"],
            250.0,
        )
        self.assertEqual(loaded["strategy"]["primary_model"], "glft")
        self.assertEqual(
            loaded["strategy"]["registered_models"],
            ["glft", "avellaneda_stoikov"],
        )
        self.assertEqual(
            {
                key: budget_config[key]
                for key in STRATEGY_RISK_BUDGET_DEFAULTS
                if key != "budgets"
            },
            {
                key: value
                for key, value in STRATEGY_RISK_BUDGET_DEFAULTS.items()
                if key != "budgets"
            },
        )

    def test_strategy_registration_rejects_multiple_execution_owners(self):
        with self.assertRaisesRegex(ValueError, "single_primary"):
            normalize_strategy_registration(
                {
                    "strategy": {
                        "primary_model": "glft",
                        "registered_models": ["glft", "as"],
                        "execution_policy": "concurrent",
                    }
                }
            )

    def test_strategy_registration_rekeys_budget_to_canonical_primary_id(self):
        configured = normalize_strategy_registration(
            {
                "strategy": {
                    "name": "as",
                    "registered_models": ["GLFT", "as"],
                },
                "risk": {
                    "limits": {
                        "max_pos_notional": 25.0,
                        "max_account_gross_notional": 50.0,
                    },
                    "strategy_risk_budgets": {
                        "enabled": True,
                        "budgets": {},
                    },
                },
            }
        )
        configured = finalize_strategy_risk_budgets(configured)

        self.assertEqual(configured["strategy"]["name"], "AvellanedaStoikov")
        self.assertEqual(
            configured["risk"]["strategy_risk_budgets"]["budgets"][
                "AvellanedaStoikov"
            ],
            {
                "auto_scale": True,
                "max_gross_notional": 50.0,
                "max_symbol_notional": 25.0,
            },
        )

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
        self.assertEqual(scaled["account"]["trading_budget_total"], 200.0)
        self.assertEqual(scaled["account"]["trading_budget_by_asset"], {"USDT": 200.0})
        self.assertEqual(scaled["backtest"]["initial_capital"], 200.0)
        self.assertEqual(scaled["risk"]["limits"]["max_order_notional"], 16.0)
        self.assertEqual(scaled["risk"]["limits"]["max_pos_notional"], 32.0)
        self.assertEqual(scaled["risk"]["limits"]["max_account_gross_notional"], 90.0)
        self.assertEqual(scaled["risk"]["limits"]["max_daily_loss"], 10.0)
        self.assertEqual(scaled["risk"]["limits"]["max_order_qty"], 20000.0)
        self.assertEqual(scaled["risk"]["limits"]["max_concurrent_symbols"], 3)
        self.assertAlmostEqual(scaled["strategy"]["lot_multiplier"], 16.0 / 5.5, places=8)
        self.assertEqual(scaled["strategy"]["target_order_notional"], 16.0)
        self.assertEqual(scaled["strategy"]["max_pos_usdt"], 32.0)

    def test_capital_scaling_keeps_paper_venue_balance_in_sync(self):
        payload = {
            "execution": {"mode": "paper"},
            "paper_trade": {
                "enabled": True,
                "initial_balance_usdt": 100.0,
            },
            "symbols": ["BTCUSDT"],
            "account": {"leverage": 8, "initial_balance_usdt": 100.0},
            "backtest": {"initial_capital": 100.0},
            "risk": {"limits": {"max_order_notional": 8.0}},
            "strategy": {
                "capital_multiplier": 20.0,
                "capital_scaling": {
                    "enabled": True,
                    "reference_capital_usdt": 100.0,
                    "target_order_notional": 8.0,
                    "target_total_risk_notional": 36.0,
                    "target_concurrent_symbols": 1,
                },
            },
        }

        scaled = apply_capital_scaling(payload)

        self.assertEqual(scaled["account"]["initial_balance_usdt"], 2000.0)
        self.assertEqual(scaled["backtest"]["initial_capital"], 2000.0)
        self.assertEqual(scaled["paper_trade"]["initial_balance_usdt"], 2000.0)

    def test_capital_scaling_splits_budget_across_usdt_and_usdc_symbols(self):
        payload = {
            "symbols": ["BTCUSDT", "SOLUSDC"],
            "account": {"leverage": 8, "initial_balance_usdt": 100.0},
            "backtest": {"initial_capital": 100.0},
            "risk": {"limits": {"max_order_notional": 8.0, "max_pos_notional": 16.0}},
            "strategy": {
                "capital_multiplier": 2.0,
                "capital_scaling": {
                    "enabled": True,
                    "reference_capital_usdt": 100.0,
                    "target_order_notional": 8.0,
                    "target_total_risk_notional": 45.0,
                    "target_concurrent_symbols": 2,
                    "reference_min_notional": 5.0,
                    "notional_buffer": 1.1,
                },
            },
        }

        scaled = apply_capital_scaling(payload)

        self.assertEqual(
            scaled["account"]["trading_budget_by_asset"],
            {"USDT": 100.0, "USDC": 100.0},
        )

    def test_capital_scaling_honors_budget_asset_weights(self):
        payload = {
            "symbols": ["BTCUSDT", "SOLUSDC"],
            "account": {"leverage": 8, "initial_balance_usdt": 100.0},
            "backtest": {"initial_capital": 100.0},
            "risk": {"limits": {"max_order_notional": 8.0, "max_pos_notional": 16.0}},
            "strategy": {
                "capital_multiplier": 2.0,
                "capital_scaling": {
                    "enabled": True,
                    "reference_capital_usdt": 100.0,
                    "target_order_notional": 8.0,
                    "target_total_risk_notional": 45.0,
                    "target_concurrent_symbols": 2,
                    "reference_min_notional": 5.0,
                    "notional_buffer": 1.1,
                    "budget_asset_weights": {"USDC": 3.0, "USDT": 1.0},
                },
            },
        }

        scaled = apply_capital_scaling(payload)

        self.assertEqual(
            scaled["account"]["trading_budget_by_asset"],
            {"USDT": 50.0, "USDC": 150.0},
        )

    def test_capital_scaling_can_raise_order_notional_limit_above_base_target(self):
        payload = {
            "symbols": ["BTCUSDT", "ETHUSDT"],
            "account": {"leverage": 8, "initial_balance_usdt": 100.0},
            "backtest": {"initial_capital": 100.0},
            "risk": {"limits": {"max_order_notional": 8.0, "max_pos_notional": 16.0}},
            "strategy": {
                "capital_multiplier": 1.0,
                "capital_scaling": {
                    "enabled": True,
                    "reference_capital_usdt": 100.0,
                    "target_order_notional": 8.0,
                    "order_notional_limit_factor": 1.5,
                    "target_total_risk_notional": 36.0,
                    "target_concurrent_symbols": 2,
                    "reference_min_notional": 5.0,
                    "notional_buffer": 1.1,
                },
            },
        }

        scaled = apply_capital_scaling(payload)

        self.assertEqual(scaled["risk"]["limits"]["max_order_notional"], 12.0)
        self.assertAlmostEqual(scaled["strategy"]["lot_multiplier"], 8.0 / 5.5, places=8)
        self.assertEqual(scaled["strategy"]["target_order_notional"], 8.0)

    def test_oms_sets_gateway_target_leverage_from_account_config(self):
        config = {
            "symbols": ["BTCUSDT"],
            "account": {
                "initial_balance_usdt": 1000.0,
                "leverage": 7,
                "margin_type": "ISOLATED",
                "position_mode": "ONE_WAY",
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
            self.assertEqual(gateway.target_margin_type, "ISOLATED")
            self.assertEqual(gateway.target_position_mode, "ONE_WAY")
            self.assertEqual(oms.max_account_gross_notional, 4321.0)
        finally:
            oms.stop()

    def test_oms_rejects_hedge_mode_before_mutating_gateway(self):
        config = {
            "symbols": ["BTCUSDT"],
            "account": {
                "initial_balance_usdt": 1000.0,
                "leverage": 7,
                "margin_type": "ISOLATED",
                "position_mode": "HEDGE",
            },
            "risk": {"limits": {"max_pos_notional": 1234.0}},
            "oms": {
                "journal_enabled": False,
                "replay_journal_on_startup": False,
            },
        }
        gateway = DummyGateway()

        with self.assertRaisesRegex(ValueError, "one-way net-position ledger"):
            OMS(DummyEngine(), gateway, config)

        self.assertFalse(hasattr(gateway, "target_position_mode"))

    def test_gateway_refuses_hedge_mode_before_exchange_configuration(self):
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.gateway_name = "BINANCE"
        gateway.target_position_mode = "HEDGE"
        gateway.target_margin_type = "ISOLATED"
        gateway.target_leverage = 10
        gateway.symbols = ["BTCUSDT"]
        gateway.rest = MagicMock()

        self.assertFalse(gateway._apply_account_trading_configuration())
        gateway.rest.set_position_mode.assert_not_called()
        gateway.rest.set_margin_type.assert_not_called()
        gateway.rest.set_leverage.assert_not_called()

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
            gateway.target_margin_type = "ISOLATED"
            gateway.target_position_mode = "ONE_WAY"
            gateway.connect(["BTCUSDT", "ETHUSDT"])

            rest.set_position_mode.assert_called_once_with("ONE_WAY")
            self.assertEqual(rest.set_leverage.call_args_list, [call("BTCUSDT", 9), call("ETHUSDT", 9)])
            self.assertEqual(rest.set_margin_type.call_args_list, [call("BTCUSDT", "ISOLATED"), call("ETHUSDT", "ISOLATED")])
            ws.start_market_stream.assert_called_once_with(["BTCUSDT", "ETHUSDT"])
            gateway.close()


class PaperTradeConfigTests(unittest.TestCase):
    def test_tracked_paper_profile_uses_inventory_bounded_10k_sizing(self):
        configured = load_root_config("config.json")

        self.assertEqual(configured["symbols"], ["SNDKUSDT"])
        self.assertEqual(configured["account"]["initial_balance_usdt"], 10_000.0)
        self.assertEqual(configured["account"]["trading_budget_total"], 10_000.0)
        self.assertEqual(
            configured["strategy"]["order_sizing"],
            {"mode": "notional"},
        )
        self.assertEqual(configured["strategy"]["target_order_notional"], 100.0)
        self.assertEqual(configured["strategy"]["max_pos_usdt"], 500.0)
        self.assertNotIn(
            "inventory_lot_notional_usdt",
            configured["strategy"]["glft"],
        )
        self.assertNotIn(
            "inventory_lot_notional_usdt",
            configured["strategy"]["avellaneda_stoikov"],
        )
        self.assertEqual(configured["risk"]["limits"]["max_order_notional"], 110.0)
        self.assertEqual(configured["risk"]["limits"]["max_pos_notional"], 500.0)
        self.assertEqual(
            configured["risk"]["limits"]["max_account_gross_notional"],
            500.0,
        )
        self.assertEqual(configured["risk"]["limits"]["max_daily_loss"], 100.0)
        self.assertEqual(configured["risk"]["limits"]["max_concurrent_symbols"], 1)

    def test_paper_mode_is_type_selected_and_scrubs_every_private_credential(self):
        raw = {
            "execution": {"mode": "paper"},
            "testnet": True,
            "api_key": "live-key-must-disappear",
            "api_secret": "live-secret-must-disappear",
            "api_key_env": "BINANCE_API_KEY",
            "api_secret_env": "BINANCE_API_SECRET",
            "system": {"market_data": {}},
            "oms": {
                "journal_path": "storage/oms/live.jsonl",
                "single_writer_fence": {"path": "storage/oms/live.lock"},
            },
            "risk": {
                "cash_flow_truth": {"enabled": True},
                "independent_supervisor": {
                    "enabled": True,
                    "api_key": "risk-key-must-disappear",
                    "api_secret": "risk-secret-must-disappear",
                },
            },
        }

        configured = apply_paper_trade_mode(raw)

        self.assertTrue(is_paper_trade(configured))
        self.assertFalse(configured["testnet"])
        self.assertEqual(configured["api_key"], "")
        self.assertEqual(configured["api_secret"], "")
        self.assertEqual(configured["api_key_env"], "")
        self.assertEqual(configured["api_secret_env"], "")
        self.assertEqual(
            configured["system"]["market_data"]["environment"],
            "production",
        )
        self.assertEqual(
            configured["oms"]["journal_path"],
            "storage/paper/oms_journal.jsonl",
        )
        self.assertEqual(
            configured["oms"]["single_writer_fence"]["path"],
            "storage/paper/oms_journal.jsonl.lock",
        )
        self.assertFalse(configured["oms"]["replay_journal_on_startup"])
        self.assertTrue(configured["paper_trade"]["reset_on_start"])
        self.assertFalse(configured["risk"]["independent_supervisor"]["enabled"])
        self.assertFalse(configured["risk"]["cash_flow_truth"]["enabled"])
        self.assertEqual(
            configured["risk"]["risk_control_heartbeat"]["required_source"],
            "risk_manager",
        )
        rendered = json.dumps(configured)
        self.assertNotIn("must-disappear", rendered)

    def test_live_mode_cannot_be_combined_with_legacy_dry_run(self):
        with self.assertRaisesRegex(ValueError, "Conflicting execution"):
            is_paper_trade(
                {
                    "execution": {"mode": "live"},
                    "system": {"dry_run": True},
                }
            )

    def test_paper_mode_rejects_unsupported_persistent_venue_state(self):
        with self.assertRaisesRegex(ValueError, "reset_on_start must be true"):
            apply_paper_trade_mode(
                {
                    "execution": {"mode": "paper"},
                    "paper_trade": {"reset_on_start": False},
                }
            )

    def test_paper_mode_validates_and_normalizes_fixed_quantity_sizing(self):
        configured = apply_paper_trade_mode(
            {
                "execution": {"mode": "paper"},
                "paper_trade": {"enabled": True},
                "strategy": {
                    "order_sizing": {
                        "mode": "FIXED_QUANTITY",
                        "fixed_quantity": "30",
                    }
                },
            }
        )

        self.assertEqual(
            configured["strategy"]["order_sizing"],
            {"mode": "fixed_quantity", "fixed_quantity": 30.0},
        )

    def test_paper_mode_rejects_invalid_fixed_quantity(self):
        with self.assertRaisesRegex(ValueError, "positive and finite"):
            apply_paper_trade_mode(
                {
                    "execution": {"mode": "paper"},
                    "paper_trade": {"enabled": True},
                    "strategy": {
                        "order_sizing": {
                            "mode": "fixed_quantity",
                            "fixed_quantity": 0,
                        }
                    },
                }
            )


if __name__ == "__main__":
    unittest.main()
