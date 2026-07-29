import json
import math
import sys
import threading
import types
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

try:
    __import__("requests")
except ModuleNotFoundError:
    requests_stub = types.ModuleType("requests")
    requests_stub.get = lambda *args, **kwargs: None
    sys.modules["requests"] = requests_stub

from event.type import (
    AccountData,
    AlertData,
    ApiLimitData,
    ExchangeOrderUpdate,
    LifecycleState,
    OrderBook,
    OrderIntent,
    OrderStateSnapshot,
    OrderStatus,
    Side,
    StrategyData,
    TIF_GTX,
    TIF_RPI,
    TradeData,
    EVENT_LOG,
    EVENT_ORDER_UPDATE,
    EVENT_STRATEGY_UPDATE,
)
from data.ref_data import ContractInfo, ref_data_manager
from alpha.factors import GLFTCalibrator
from oms.engine import OMS
from strategy.avellaneda_stoikov import AvellanedaStoikovStrategy
from strategy.base import StrategyTemplate
from strategy.glft import GLFTStrategy
from strategy.quote_math import (
    ADAPTIVE_GLFT_FORMULA_VERSION,
    PORTFOLIO_GLFT_FORMULA_VERSION,
)
from strategy.registry import (
    canonical_model_key,
    create_primary_strategy,
    strategy_id_for_model,
)
from ui.web_dashboard import LocalWebDashboard


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
        self.sent_requests = []

    def send_order(self, req, client_oid):
        self.sent_requests.append((req, client_oid))
        return self.send_order_result

    def cancel_order(self, req):
        return None

    def cancel_all_orders(self, symbol):
        return SimpleNamespace(status_code=200, json=lambda: {})

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


class PassiveQuoteOMS:
    def __init__(self):
        self.config = {
            "execution": {"mode": "paper"},
            "paper_trade": {"enabled": True},
            "backtest": {
                "maker_fee": 0.0002,
                "rpi_commission_rate": 0.0001,
                "rpi_commission_rates": {"LTCUSDT": 0.00015},
            }
        }
        self.exposure = SimpleNamespace(
            net_positions={},
            strategy_net_positions={},
        )
        self.account = SimpleNamespace(equity=1000.0, used_margin=0.0)
        self.submitted = []
        self.cancelled = []
        self.submit_allowed = True

    def can_submit_for_strategy(self, _strategy_id, _symbol=""):
        return self.submit_allowed

    def submit_order(self, intent):
        self.submitted.append(intent)
        return f"passive-{len(self.submitted)}"

    def cancel_order(self, client_oid):
        self.cancelled.append(client_oid)
        return True


class StrategyMonotonicTimingTests(unittest.TestCase):
    @staticmethod
    def make_contract(symbol="LTCUSDT"):
        return ContractInfo(
            symbol=symbol,
            tick_size=0.1,
            step_size=0.001,
            min_qty=0.001,
            min_notional=5.0,
            price_precision=1,
            qty_precision=3,
            status="TRADING",
            permissions=frozenset(),
        )

    @staticmethod
    def make_orderbook(symbol="LTCUSDT", mid=100.0):
        return OrderBook(
            symbol=symbol,
            exchange="BINANCE",
            datetime=datetime.utcnow(),
            best_bid_price=mid - 0.1,
            best_bid_volume=1.0,
            best_ask_price=mid + 0.1,
            best_ask_volume=1.0,
        )

    @staticmethod
    def make_clocked_book(
        mid,
        *,
        received_monotonic=None,
        exchange_timestamp=0.0,
    ):
        return SimpleNamespace(
            received_monotonic=received_monotonic,
            exchange_timestamp=exchange_timestamp,
            get_best_bid=lambda: (mid - 0.1, 1.0),
            get_best_ask=lambda: (mid + 0.1, 1.0),
        )

    def test_price_rounding_uses_tick_size_and_quote_direction(self):
        contract = ContractInfo(
            symbol="ODDTICK",
            tick_size=0.25,
            step_size=0.01,
            min_qty=0.01,
            min_notional=5.0,
            price_precision=2,
            qty_precision=2,
        )
        with patch.dict(
            ref_data_manager.contracts,
            {"ODDTICK": contract},
            clear=True,
        ):
            self.assertEqual(
                ref_data_manager.round_price("ODDTICK", 100.13),
                100.25,
            )
            self.assertEqual(
                ref_data_manager.round_price(
                    "ODDTICK",
                    100.13,
                    direction="down",
                ),
                100.0,
            )
            self.assertEqual(
                ref_data_manager.round_price(
                    "ODDTICK",
                    100.13,
                    direction="up",
                ),
                100.25,
            )

    def test_avellaneda_cycle_uses_monotonic_time(self):
        engine = DispatchingEngine()
        oms = PassiveQuoteOMS()
        strategy = AvellanedaStoikovStrategy(
            engine,
            oms,
            strategy_config={
                "cycle_interval": 1.0,
                "lot_multiplier": 1.0,
                "as_parameters": {
                    "gamma": 0.05,
                    "k": 1.5,
                    "vol_window": 5,
                },
            },
        )
        orderbook = self.make_orderbook()

        with (
            patch.dict(
                ref_data_manager.contracts,
                {"LTCUSDT": self.make_contract()},
                clear=True,
            ),
            patch(
                "strategy.avellaneda_stoikov.time.perf_counter",
                side_effect=[0.1, 0.5, 1.2],
            ),
            patch(
                "strategy.avellaneda_stoikov.time.time",
                side_effect=AssertionError("wall clock must not gate the cycle"),
            ),
        ):
            strategy.on_orderbook(orderbook)
            self.assertEqual(len(oms.submitted), 2)

            strategy.on_orderbook(orderbook)
            self.assertEqual(oms.cancelled, [])

            strategy.on_orderbook(orderbook)
            self.assertCountEqual(oms.cancelled, ["passive-1", "passive-2"])

    def test_glft_cooldown_and_fill_defense_use_monotonic_time(self):
        engine = DispatchingEngine()
        oms = PassiveQuoteOMS()
        strategy = GLFTStrategy(
            engine,
            oms,
            strategy_config={
                "cycle_interval": 0.0,
                "execution": {"min_spread_bps": 5.0},
            },
        )
        orderbook = self.make_orderbook()

        with (
            patch.dict(
                ref_data_manager.contracts,
                {"LTCUSDT": self.make_contract()},
                clear=True,
            ),
            patch(
                "strategy.glft.time.perf_counter",
                side_effect=[0.1, 0.2, 0.31],
            ),
            patch(
                "strategy.glft.time.time",
                side_effect=AssertionError("wall clock must not gate quote cooldown"),
            ),
        ):
            strategy._update_quotes("LTCUSDT", 99.0, 101.0, 0.1)
            self.assertEqual(len(oms.submitted), 2)

            strategy._update_quotes("LTCUSDT", 98.5, 101.5, 0.1)
            self.assertEqual(oms.cancelled, [])

            strategy._update_quotes("LTCUSDT", 98.5, 101.5, 0.1)
            self.assertCountEqual(oms.cancelled, ["passive-1", "passive-2"])

        calibrator = SimpleNamespace(
            on_orderbook=lambda _ob: None,
            sigma_bps=2.5,
            A=1.2,
            k=1.1,
        )
        observed_model_times = []
        model = SimpleNamespace(
            update_and_predict=lambda _features, _mid, now: (
                observed_model_times.append(now)
                or {"short": 0.0, "mid": 0.0, "long": 0.0}
            )
        )
        gate = SimpleNamespace(process=lambda value, _position: value)
        strategy._get_components = lambda _symbol: (calibrator, model, gate)
        strategy.feature_engine = SimpleNamespace(
            on_orderbook=lambda _ob: None,
            get_features=lambda _symbol: [0.0] * 9,
            reset_interval=lambda _symbol: None,
        )
        strategy._update_quotes = lambda *_args, **_kwargs: None
        engine.events.clear()

        with (
            patch.dict(
                ref_data_manager.contracts,
                {"LTCUSDT": self.make_contract()},
                clear=True,
            ),
            patch(
                "strategy.glft.time.perf_counter",
                side_effect=[0.1, 0.2, 1.0, 2.3],
            ),
            patch(
                "strategy.glft.time.time",
                side_effect=AssertionError("wall clock must not gate fill defense"),
            ),
        ):
            strategy.on_orderbook(orderbook)
            strategy.on_trade(
                TradeData(
                    symbol="LTCUSDT",
                    order_id="order-1",
                    trade_id="trade-1",
                    side="BUY",
                    price=100.0,
                    volume=0.1,
                    datetime=datetime.utcnow(),
                )
            )
            strategy.on_orderbook(orderbook)
            strategy.on_orderbook(orderbook)

        telemetry = [
            event.data.params
            for event in engine.events
            if event.type == EVENT_STRATEGY_UPDATE
        ]
        self.assertEqual(observed_model_times, [0.1, 1.0, 2.3])
        self.assertFalse(telemetry[0]["recent_fill_defense"])
        self.assertTrue(telemetry[1]["recent_fill_defense"])
        self.assertFalse(telemetry[2]["recent_fill_defense"])

    def test_glft_portfolio_risk_requires_fresh_full_universe(self):
        engine = DispatchingEngine()
        oms = PassiveQuoteOMS()
        oms.config["symbols"] = ["BTCUSDT", "ETHUSDT"]
        oms.exposure.net_positions["ETHUSDT"] = 2.0
        strategy = GLFTStrategy(
            engine,
            oms,
            strategy_config={
                "glft": {
                    "portfolio_risk": {
                        "enabled": True,
                        "require_full_universe": True,
                        "max_state_age_sec": 5.0,
                        "correlations": {"BTCUSDT|ETHUSDT": 0.8},
                    }
                }
            },
        )
        common = {
            "mid_price": 100.0,
            "fair_mid": 100.0,
            "sigma_bps_sqrt_s": 1.0,
            "gamma_per_bps": 0.05,
            "A_per_s": 2.0,
            "k_per_bps": 0.5,
            "order_size_lots": 1.0,
            "inventory_lot_notional_usdt": 100.0,
            "target_position_notional_usdt": 0.0,
        }

        with self.assertRaisesRegex(ValueError, "ETHUSDT"):
            strategy._calculate_formula_quote(
                symbol="BTCUSDT",
                inventory_lots=0.0,
                now_monotonic=1.0,
                **common,
            )
        strategy._calculate_formula_quote(
            symbol="ETHUSDT",
            inventory_lots=2.0,
            now_monotonic=1.1,
            **common,
        )
        quote, portfolio = strategy._calculate_formula_quote(
            symbol="BTCUSDT",
            inventory_lots=0.0,
            now_monotonic=1.2,
            **common,
        )

        self.assertEqual(
            portfolio["formula_version"],
            PORTFOLIO_GLFT_FORMULA_VERSION,
        )
        self.assertEqual(portfolio["symbols"], ["BTCUSDT", "ETHUSDT"])
        self.assertLess(quote.center_offset_bps, 0.0)
        self.assertGreater(
            portfolio["marginal_inventory_risk_bps"]["BTCUSDT"],
            0.0,
        )

    def test_glft_adaptive_quote_is_paper_only_and_reports_robust_scenario(self):
        engine = DispatchingEngine()
        oms = PassiveQuoteOMS()
        oms.config["symbols"] = ["BTCUSDT"]
        strategy = GLFTStrategy(
            engine,
            oms,
            strategy_config={
                "glft": {
                    "adaptive": {
                        "enabled": True,
                        "finite_horizon_s": 0.5,
                    }
                }
            },
        )
        quote, risk = strategy._calculate_formula_quote(
            symbol="BTCUSDT",
            mid_price=100.0,
            fair_mid=100.0,
            inventory_lots=1.0,
            sigma_bps_sqrt_s=2.0,
            gamma_per_bps=0.05,
            A_per_s=2.0,
            k_per_bps=0.5,
            order_size_lots=1.0,
            inventory_lot_notional_usdt=100.0,
            target_position_notional_usdt=0.0,
            now_monotonic=1.0,
            adaptive_context={
                "bid_A_per_s": 2.2,
                "ask_A_per_s": 1.8,
                "bid_k_per_bps": 0.45,
                "ask_k_per_bps": 0.55,
                "bid_adverse_cost_bps": 0.5,
                "ask_adverse_cost_bps": 0.75,
            },
        )

        self.assertEqual(risk["formula_version"], ADAPTIVE_GLFT_FORMULA_VERSION)
        self.assertTrue(risk["adaptive_enabled"])
        self.assertEqual(risk["scenario_count"], 12)
        self.assertGreater(quote.bid_depth_bps, 0.0)
        self.assertGreater(quote.ask_depth_bps, 0.0)
        self.assertLess(quote.bid_price, quote.ask_price)

    def test_glft_calibrator_prefers_event_clocks_then_monotonic(self):
        cases = (
            (
                "received_monotonic",
                self.make_clocked_book(
                    100.0,
                    received_monotonic=50.0,
                    exchange_timestamp=1000.0,
                ),
                self.make_clocked_book(
                    101.0,
                    received_monotonic=50.25,
                    exchange_timestamp=1100.0,
                ),
                [10.0, 999.0],
                0.25,
            ),
            (
                "exchange_timestamp",
                self.make_clocked_book(100.0, exchange_timestamp=2000.0),
                self.make_clocked_book(101.0, exchange_timestamp=2000.5),
                [20.0, 900.0],
                0.5,
            ),
            (
                "monotonic",
                self.make_clocked_book(100.0),
                self.make_clocked_book(101.0),
                [30.0, 30.25],
                0.25,
            ),
        )

        with patch(
            "alpha.factors.time.time",
            side_effect=AssertionError("calibrator must not read wall time"),
        ):
            for expected_source, first_book, second_book, local_times, dt in cases:
                with self.subTest(source=expected_source):
                    calibrator = GLFTCalibrator(window=20)
                    with patch(
                        "alpha.factors.time.perf_counter",
                        side_effect=local_times,
                    ):
                        calibrator.on_orderbook(first_book)
                        calibrator.on_orderbook(second_book)

                    expected_return = (
                        math.log(101.0 / 100.0)
                        * 10_000.0
                        / math.sqrt(dt)
                    )
                    self.assertEqual(calibrator.last_tick_source, expected_source)
                    self.assertEqual(len(calibrator.norm_returns), 1)
                    self.assertAlmostEqual(
                        calibrator.norm_returns[-1],
                        expected_return,
                    )

    def test_glft_calibrator_rejects_nonpositive_dt_and_rebases_large_gap(self):
        calibrator = GLFTCalibrator(
            window=20,
            config={
                "strategy": {
                    "calibrator": {
                        "max_tick_gap_sec": 1.0,
                    }
                }
            },
        )
        books = (
            self.make_clocked_book(100.0, exchange_timestamp=100.0),
            self.make_clocked_book(90.0, exchange_timestamp=99.0),
            self.make_clocked_book(102.0, exchange_timestamp=103.0),
            self.make_clocked_book(103.0, exchange_timestamp=103.25),
        )

        with (
            patch(
                "alpha.factors.time.perf_counter",
                side_effect=[1.0, 1.1, 1.2, 1.3],
            ),
            patch(
                "alpha.factors.time.time",
                side_effect=AssertionError("calibrator must not read wall time"),
            ),
        ):
            calibrator.on_orderbook(books[0])
            calibrator.on_orderbook(books[1])
            self.assertEqual(calibrator.last_mid, 100.0)
            self.assertEqual(calibrator.last_tick_time, 100.0)
            self.assertEqual(len(calibrator.norm_returns), 0)

            calibrator.on_orderbook(books[2])
            self.assertEqual(calibrator.last_mid, 102.0)
            self.assertEqual(calibrator.last_tick_time, 103.0)
            self.assertEqual(len(calibrator.norm_returns), 0)

            calibrator.on_orderbook(books[3])

        expected_return = (
            math.log(103.0 / 102.0) * 10_000.0 / math.sqrt(0.25)
        )
        self.assertEqual(len(calibrator.norm_returns), 1)
        self.assertAlmostEqual(calibrator.norm_returns[-1], expected_return)


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

    @staticmethod
    def make_contract(symbol, *, supports_rpi):
        return ContractInfo(
            symbol=symbol,
            tick_size=0.1,
            step_size=0.001,
            min_qty=0.001,
            min_notional=5.0,
            price_precision=1,
            qty_precision=3,
            status="TRADING",
            permissions=frozenset({"RPI"}) if supports_rpi else frozenset(),
        )

    def test_rejected_cancel_does_not_suppress_retry(self):
        class RejectingCancelOMS:
            def __init__(self):
                self.cancelled = []

            def cancel_order(self, client_oid):
                self.cancelled.append(client_oid)
                return False

        oms = RejectingCancelOMS()
        strategy = DummyStrategy(DispatchingEngine(), oms)
        intent = OrderIntent(
            "test",
            "BTCUSDT",
            Side.BUY,
            100.0,
            1.0,
        )
        strategy.active_orders["cancel-retry"] = intent

        self.assertFalse(strategy.cancel_order("cancel-retry"))
        self.assertFalse(strategy.cancel_order("cancel-retry"))

        self.assertEqual(oms.cancelled, ["cancel-retry", "cancel-retry"])
        self.assertNotIn("cancel-retry", strategy.orders_cancelling)

    def test_passive_router_is_symbol_gated_and_accounts_for_rpi_fee(self):
        engine = DispatchingEngine()
        oms = PassiveQuoteOMS()
        strategy = DummyStrategy(engine, oms)
        contracts = {
            "LTCUSDT": self.make_contract("LTCUSDT", supports_rpi=True),
            "SOLUSDT": self.make_contract("SOLUSDT", supports_rpi=False),
        }

        with patch.dict(ref_data_manager.contracts, contracts, clear=True):
            self.assertEqual(
                strategy.resolve_passive_time_in_force(
                    "LTCUSDT",
                    use_rpi=True,
                ),
                TIF_RPI,
            )
            self.assertEqual(
                strategy.resolve_passive_time_in_force(
                    "SOLUSDT",
                    use_rpi=True,
                    fallback_to_gtx=True,
                    route="test_quote",
                ),
                TIF_GTX,
            )
            self.assertEqual(
                strategy.resolve_passive_time_in_force(
                    "SOLUSDT",
                    use_rpi=True,
                    fallback_to_gtx=True,
                    route="test_quote",
                ),
                TIF_GTX,
            )
            self.assertEqual(
                strategy.resolve_passive_time_in_force(
                    "SOLUSDT",
                    use_rpi=True,
                    fallback_to_gtx=False,
                ),
                TIF_RPI,
            )

        fallback_logs = [event for event in engine.events if event.type == EVENT_LOG]
        self.assertEqual(len(fallback_logs), 1)
        self.assertAlmostEqual(
            strategy.passive_round_trip_fee_bps("LTCUSDT", TIF_RPI),
            3.0,
        )
        self.assertAlmostEqual(
            strategy.passive_round_trip_fee_bps("LTCUSDT", TIF_GTX),
            4.0,
        )

    def test_paper_quote_fee_uses_the_same_config_as_paper_execution(self):
        oms = PassiveQuoteOMS()
        oms.config = {
            "execution": {"mode": "paper"},
            "paper_trade": {
                "enabled": True,
                "maker_fee": 0.0003,
                "rpi_commission_rate": 0.0002,
            },
            "backtest": {
                "maker_fee": 0.0,
                "rpi_commission_rate": 0.0,
            },
        }
        strategy = DummyStrategy(DispatchingEngine(), oms)

        self.assertAlmostEqual(
            strategy.passive_round_trip_fee_bps("LTCUSDT", TIF_GTX),
            6.0,
        )
        self.assertAlmostEqual(
            strategy.passive_round_trip_fee_bps("LTCUSDT", TIF_RPI),
            4.0,
        )

    def test_market_makers_enforce_order_and_inventory_notionals(self):
        sizing_config = {
            "target_order_notional": 8.0,
            "max_pos_usdt": 18.0,
            "cycle_interval": 0.0,
        }
        strategies = (
            GLFTStrategy(
                DispatchingEngine(),
                PassiveQuoteOMS(),
                strategy_config=sizing_config,
            ),
            AvellanedaStoikovStrategy(
                DispatchingEngine(),
                PassiveQuoteOMS(),
                strategy_config=sizing_config,
            ),
        )

        with patch.dict(
            ref_data_manager.contracts,
            {"LTCUSDT": self.make_contract("LTCUSDT", supports_rpi=False)},
            clear=True,
        ):
            for strategy in strategies:
                with self.subTest(strategy=strategy.name):
                    self.assertEqual(
                        strategy._calculate_safe_vol("LTCUSDT", 100.0),
                        0.08,
                    )
                    self.assertEqual(
                        strategy._calculate_safe_vol(
                            "LTCUSDT",
                            100.0,
                            side=Side.BUY,
                            current_position=0.12,
                            reference_price=100.0,
                        ),
                        0.06,
                    )
                    self.assertEqual(
                        strategy._calculate_safe_vol(
                            "LTCUSDT",
                            100.0,
                            side=Side.SELL,
                            current_position=0.12,
                            reference_price=100.0,
                        ),
                        0.08,
                    )
                    self.assertEqual(
                        strategy._calculate_safe_vol(
                            "LTCUSDT",
                            100.0,
                            side=Side.BUY,
                            current_position=0.15,
                            reference_price=100.0,
                        ),
                        0.0,
                    )
                    self.assertEqual(
                        strategy._calculate_safe_vol(
                            "LTCUSDT",
                            100.0,
                            side=Side.BUY,
                            current_position=0.18,
                            reference_price=100.0,
                        ),
                        0.0,
                    )
                    self.assertEqual(
                        strategy._calculate_safe_vol(
                            "LTCUSDT",
                            100.0,
                            side=Side.SELL,
                            current_position=-0.18,
                            reference_price=100.0,
                        ),
                        0.0,
                    )
                    self.assertEqual(
                        strategy._calculate_safe_vol(
                            "LTCUSDT",
                            100.0,
                            side=Side.SELL,
                            current_position=0.20,
                            reference_price=100.0,
                        ),
                        0.08,
                    )

    def test_market_maker_sizing_keeps_legacy_lot_multiplier_fallback(self):
        strategies = (
            GLFTStrategy(
                DispatchingEngine(),
                PassiveQuoteOMS(),
                strategy_config={"lot_multiplier": 1.0},
            ),
            AvellanedaStoikovStrategy(
                DispatchingEngine(),
                PassiveQuoteOMS(),
                strategy_config={"lot_multiplier": 1.0},
            ),
        )

        with patch.dict(
            ref_data_manager.contracts,
            {"LTCUSDT": self.make_contract("LTCUSDT", supports_rpi=False)},
            clear=True,
        ):
            for strategy in strategies:
                with self.subTest(strategy=strategy.name):
                    self.assertEqual(
                        strategy._calculate_safe_vol("LTCUSDT", 100.0),
                        0.055,
                    )

    def test_market_maker_sizing_accepts_nested_capital_scaling_config(self):
        strategy = GLFTStrategy(
            DispatchingEngine(),
            PassiveQuoteOMS(),
            strategy_config={
                "capital_multiplier": 2.0,
                "capital_scaling": {"target_order_notional": 8.0},
            },
        )

        with patch.dict(
            ref_data_manager.contracts,
            {"LTCUSDT": self.make_contract("LTCUSDT", supports_rpi=False)},
            clear=True,
        ):
            self.assertEqual(
                strategy._calculate_safe_vol("LTCUSDT", 100.0),
                0.16,
            )

    def test_tracked_notional_sizing_maps_100_usdt_to_sndk_lot_step(self):
        strategy = GLFTStrategy(
            DispatchingEngine(),
            PassiveQuoteOMS(),
            strategy_config={
                "target_order_notional": 100.0,
                "max_pos_usdt": 500.0,
                "order_sizing": {"mode": "notional"},
                "glft": {},
            },
        )
        contract = ContractInfo(
            symbol="SNDKUSDT",
            tick_size=0.01,
            step_size=0.01,
            min_qty=0.01,
            min_notional=5.0,
            price_precision=2,
            qty_precision=2,
            status="TRADING",
            permissions=frozenset(),
        )

        with patch.dict(
            ref_data_manager.contracts,
            {"SNDKUSDT": contract},
            clear=True,
        ):
            self.assertEqual(
                strategy._calculate_safe_vol("SNDKUSDT", 1296.56),
                0.07,
            )
            self.assertEqual(strategy.inventory_lot_notional_usdt, 100.0)

    def test_paper_market_makers_submit_only_full_fixed_quantity(self):
        sizing_config = {
            "target_order_notional": 40_000.0,
            "max_pos_usdt": 90_000.0,
            "order_sizing": {
                "mode": "fixed_quantity",
                "fixed_quantity": 30.0,
            },
        }
        strategies = (
            GLFTStrategy(
                DispatchingEngine(),
                PassiveQuoteOMS(),
                strategy_config=sizing_config,
            ),
            AvellanedaStoikovStrategy(
                DispatchingEngine(),
                PassiveQuoteOMS(),
                strategy_config=sizing_config,
            ),
        )

        with patch.dict(
            ref_data_manager.contracts,
            {"LTCUSDT": self.make_contract("LTCUSDT", supports_rpi=False)},
            clear=True,
        ):
            for strategy in strategies:
                with self.subTest(strategy=strategy.name):
                    self.assertEqual(
                        strategy._calculate_safe_vol("LTCUSDT", 1_000.0),
                        30.0,
                    )
                    self.assertEqual(
                        strategy._calculate_safe_vol(
                            "LTCUSDT",
                            1_000.0,
                            side=Side.BUY,
                            current_position=60.0,
                            reference_price=1_000.0,
                        ),
                        30.0,
                    )
                    self.assertEqual(
                        strategy._calculate_safe_vol(
                            "LTCUSDT",
                            1_000.0,
                            side=Side.BUY,
                            current_position=75.0,
                            reference_price=1_000.0,
                        ),
                        0.0,
                    )
                    self.assertEqual(
                        strategy._calculate_safe_vol(
                            "LTCUSDT",
                            1_000.0,
                            side=Side.SELL,
                            current_position=75.0,
                            reference_price=1_000.0,
                        ),
                        30.0,
                    )

    def test_fixed_quantity_sizing_is_rejected_outside_paper(self):
        oms = PassiveQuoteOMS()
        oms.config = {"execution": {"mode": "live"}}

        with self.assertRaisesRegex(ValueError, "Paper-only"):
            DummyStrategy(DispatchingEngine(), oms).configure_quote_sizing(
                {
                    "order_sizing": {
                        "mode": "fixed_quantity",
                        "fixed_quantity": 30.0,
                    }
                }
            )

    @patch("oms.validator.ref_data_manager.get_info", return_value=None)
    @patch("oms.validator.data_cache.get_best_quote", return_value=(99.9, 100.1))
    @patch("oms.validator.data_cache.get_mark_price", return_value=100.0)
    def test_clock_health_gate_fails_closed_but_allows_reduce_only(self, *_mocks):
        config = self.make_config()
        config["system"] = {
            "time_sync": {"require_healthy_for_trading": True}
        }
        engine = DispatchingEngine()
        gateway = DummyGateway()
        oms = OMS(engine, gateway, config)
        oms.state = LifecycleState.LIVE
        try:
            with patch(
                "oms.order_policy.time_service.health_snapshot",
                return_value={
                    "ready": False,
                    "state": "halt",
                    "reason": "exchange clock is stale",
                },
            ):
                rejected = oms.submit_order(
                    OrderIntent("clocked", "BTCUSDT", Side.BUY, 100.0, 0.1)
                )
                self.assertFalse(rejected.accepted)
                self.assertIn("clock_health:halt", rejected.reason)
                self.assertEqual(gateway.sent_requests, [])

                oms.exposure.net_positions["BTCUSDT"] = 0.1
                reduce_result = oms.submit_order(
                    OrderIntent(
                        "clocked",
                        "BTCUSDT",
                        Side.SELL,
                        100.0,
                        0.1,
                        reduce_only=True,
                    )
                )
                self.assertTrue(reduce_result.accepted)
                self.assertEqual(len(gateway.sent_requests), 1)
        finally:
            oms.stop()

    def test_avellaneda_stoikov_quotes_rpi_and_waits_for_cancel_ack(self):
        engine = DispatchingEngine()
        oms = PassiveQuoteOMS()
        strategy = AvellanedaStoikovStrategy(
            engine,
            oms,
            strategy_config={
                "use_rpi": True,
                "use_rpi_for_avellaneda_stoikov": True,
                "rpi_fallback_to_gtx": True,
                "cycle_interval": 0.0,
                "lot_multiplier": 1.0,
                "target_order_notional": 8.0,
                "max_pos_usdt": 18.0,
                "as_parameters": {
                    "gamma": 0.05,
                    "k": 1.5,
                    "vol_window": 5,
                    "min_spread_ratio": 0.0002,
                },
            },
        )
        ob = OrderBook(
            symbol="LTCUSDT",
            exchange="BINANCE",
            datetime=datetime.utcnow(),
            best_bid_price=99.9,
            best_bid_volume=1.0,
            best_ask_price=100.1,
            best_ask_volume=1.0,
        )

        with patch.dict(
            ref_data_manager.contracts,
            {"LTCUSDT": self.make_contract("LTCUSDT", supports_rpi=True)},
            clear=True,
        ):
            strategy.on_orderbook(ob)
            self.assertEqual(len(oms.submitted), 2)
            self.assertTrue(all(order.time_in_force == TIF_RPI for order in oms.submitted))
            self.assertTrue(all(order.is_post_only for order in oms.submitted))
            telemetry = [
                event.data
                for event in engine.events
                if event.type == EVENT_STRATEGY_UPDATE
            ][-1]
            self.assertEqual(telemetry.symbol, "LTCUSDT")
            self.assertEqual(telemetry.params["schema"], "market_making.v1")
            self.assertEqual(telemetry.params["strategy"], strategy.name)
            self.assertEqual(telemetry.params["time_in_force"], TIF_RPI)
            self.assertEqual(telemetry.params["bid_order_id"], "passive-1")
            self.assertEqual(telemetry.params["ask_order_id"], "passive-2")
            self.assertEqual(telemetry.params["target_order_notional"], 8.0)
            self.assertEqual(telemetry.params["max_position_notional"], 18.0)
            self.assertTrue(
                all(order.price * order.volume <= 8.0 for order in oms.submitted)
            )
            self.assertIn("reservation_price", telemetry.params)
            self.assertIn("inventory_risk_adjustment", telemetry.params)
            self.assertIn("formula_half_spread_bps", telemetry.params)
            self.assertIn("sigma_bps", telemetry.params)
            self.assertIn("final_spread_bps", telemetry.params)
            json.dumps(telemetry.params)

            strategy.on_orderbook(ob)
            self.assertEqual(len(oms.submitted), 2)
            self.assertCountEqual(oms.cancelled, ["passive-1", "passive-2"])

            strategy.on_orderbook(ob)
            self.assertEqual(len(oms.submitted), 2)
            self.assertEqual(len(oms.cancelled), 2)

    def test_glft_quotes_rpi_and_does_not_overlap_cancel_replace(self):
        engine = DispatchingEngine()
        oms = PassiveQuoteOMS()
        strategy = GLFTStrategy(
            engine,
            oms,
            strategy_config={
                "use_rpi": True,
                "use_rpi_for_glft": True,
                "rpi_fallback_to_gtx": True,
                "execution": {"min_spread_bps": 5.0},
            },
        )
        strategy.cooldown_ms = 0

        with patch.dict(
            ref_data_manager.contracts,
            {"LTCUSDT": self.make_contract("LTCUSDT", supports_rpi=True)},
            clear=True,
        ):
            strategy._update_quotes("LTCUSDT", 99.0, 101.0, 0.1)
            self.assertEqual(len(oms.submitted), 2)
            self.assertTrue(all(order.time_in_force == TIF_RPI for order in oms.submitted))
            self.assertTrue(all(order.is_post_only for order in oms.submitted))

            strategy._update_quotes("LTCUSDT", 98.5, 101.5, 0.1)
            self.assertEqual(len(oms.submitted), 2)
            self.assertCountEqual(oms.cancelled, ["passive-1", "passive-2"])

            strategy._update_quotes("LTCUSDT", 98.5, 101.5, 0.1)
            self.assertEqual(len(oms.submitted), 2)
            self.assertEqual(len(oms.cancelled), 2)

            for oid in ("passive-1", "passive-2"):
                strategy.on_order(
                    OrderStateSnapshot(
                        client_oid=oid,
                        exchange_oid=f"exchange-{oid}",
                        symbol="LTCUSDT",
                        status=OrderStatus.CANCELLED,
                        price=0.0,
                        volume=0.1,
                        filled_volume=0.0,
                        avg_price=0.0,
                        update_time=0.0,
                    )
                )

            strategy._update_quotes("LTCUSDT", 98.5, 101.5, 0.1)
            self.assertEqual(len(oms.submitted), 4)
            self.assertTrue(all(order.time_in_force == TIF_RPI for order in oms.submitted))

    def test_glft_does_not_generate_quote_intents_while_oms_is_gated(self):
        oms = PassiveQuoteOMS()
        oms.submit_allowed = False
        strategy = GLFTStrategy(
            DispatchingEngine(),
            oms,
            strategy_config={"execution": {"min_spread_bps": 5.0}},
        )
        strategy.cooldown_ms = 0

        with patch.dict(
            ref_data_manager.contracts,
            {"LTCUSDT": self.make_contract("LTCUSDT", supports_rpi=False)},
            clear=True,
        ):
            strategy._update_quotes("LTCUSDT", 99.0, 101.0, 0.1)

        self.assertEqual(oms.submitted, [])
        self.assertEqual(oms.cancelled, [])

    def test_glft_cancels_a_same_price_side_when_inventory_disables_it(self):
        oms = PassiveQuoteOMS()
        strategy = GLFTStrategy(
            DispatchingEngine(),
            oms,
            strategy_config={"execution": {"min_spread_bps": 5.0}},
        )
        strategy.cooldown_ms = 0

        with patch.dict(
            ref_data_manager.contracts,
            {"LTCUSDT": self.make_contract("LTCUSDT", supports_rpi=False)},
            clear=True,
        ):
            strategy._update_quotes(
                "LTCUSDT",
                99.0,
                101.0,
                0.1,
                bid_volume=0.1,
                ask_volume=0.1,
            )
            strategy._update_quotes(
                "LTCUSDT",
                99.0,
                101.0,
                0.1,
                bid_volume=0.0,
                ask_volume=0.1,
            )

        self.assertEqual(len(oms.submitted), 2)
        self.assertEqual(oms.cancelled, ["passive-1"])

    def test_glft_on_orderbook_publishes_complete_params_telemetry(self):
        engine = DispatchingEngine()
        oms = PassiveQuoteOMS()
        strategy = GLFTStrategy(
            engine,
            oms,
            strategy_config={
                "use_rpi": True,
                "use_rpi_for_glft": True,
                "cycle_interval": 0.0,
                "target_order_notional": 8.0,
                "max_pos_usdt": 18.0,
                "execution": {"min_spread_bps": 5.0},
            },
        )
        strategy.cooldown_ms = 0
        calibrator = SimpleNamespace(
            on_orderbook=lambda _ob: None,
            sigma_bps=2.5,
            A=1.2,
            k=1.1,
        )
        model = SimpleNamespace(
            update_and_predict=lambda _features, _mid, _now: {
                "short": 2.0,
                "mid": 1.0,
                "long": 0.25,
            }
        )
        gate = SimpleNamespace(process=lambda value, _position: value)
        strategy._get_components = lambda _symbol: (calibrator, model, gate)
        strategy.feature_engine = SimpleNamespace(
            on_orderbook=lambda _ob: None,
            get_features=lambda _symbol: [0.0] * 9,
            reset_interval=lambda _symbol: None,
        )
        ob = OrderBook(
            symbol="LTCUSDT",
            exchange="BINANCE",
            datetime=datetime.utcnow(),
            best_bid_price=99.9,
            best_bid_volume=1.0,
            best_ask_price=100.1,
            best_ask_volume=1.0,
        )

        with patch.dict(
            ref_data_manager.contracts,
            {"LTCUSDT": self.make_contract("LTCUSDT", supports_rpi=True)},
            clear=True,
        ):
            strategy.on_orderbook(ob)

        telemetry = [
            event.data
            for event in engine.events
            if event.type == EVENT_STRATEGY_UPDATE
        ][-1]
        params = telemetry.params
        self.assertEqual(params["schema"], "market_making.v1")
        self.assertEqual(params["time_in_force"], TIF_RPI)
        self.assertEqual(params["signals"], {"short": 2.0, "mid": 1.0, "long": 0.25})
        self.assertEqual(params["target_position_notional"], 0.0)
        self.assertEqual(params["target_order_notional"], 8.0)
        self.assertEqual(params["max_position_notional"], 18.0)
        self.assertLessEqual(params["bid_quote_qty"] * params["target_bid"], 8.0)
        self.assertLessEqual(params["ask_quote_qty"] * params["target_ask"], 8.0)
        self.assertEqual(params["bid_order_id"], "passive-1")
        self.assertEqual(params["ask_order_id"], "passive-2")
        for field in (
            "gamma_per_bps",
            "k_per_bps",
            "A_per_s",
            "sigma_bps",
            "target_position_notional",
            "effective_position_notional",
            "inventory_lots",
            "formula_half_spread_bps",
            "inventory_center_offset_bps",
            "effective_min_spread_bps",
        ):
            self.assertIn(field, params)
        json.dumps(params)

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

    @patch("oms.validator.ref_data_manager.get_info", return_value=None)
    @patch("oms.validator.data_cache.get_best_quote", return_value=(99.9, 100.1))
    @patch("oms.validator.data_cache.get_mark_price", return_value=100.0)
    @patch("oms.exposure.data_cache.get_best_quote", return_value=(99.9, 100.1))
    @patch("oms.exposure.data_cache.get_mark_price", return_value=100.0)
    def test_concurrent_symbol_limit_rejects_a_new_risk_symbol(self, *_mocks):
        engine = DispatchingEngine()
        gateway = DummyGateway(send_order_result="ex-order")
        config = self.make_config()
        config["symbols"] = ["BTCUSDT", "ETHUSDT"]
        config["risk"]["limits"]["max_concurrent_symbols"] = 1
        oms = OMS(engine, gateway, config)
        strategy = DummyStrategy(engine, oms)
        engine.register(
            EVENT_ORDER_UPDATE,
            lambda event: strategy.on_order(event.data),
        )
        oms.state = LifecycleState.LIVE
        oms.exposure.net_positions["ETHUSDT"] = 0.5
        try:
            oid = strategy.send_intent(
                OrderIntent(
                    "dummy",
                    "BTCUSDT",
                    Side.BUY,
                    100.0,
                    0.1,
                )
            )

            self.assertIsNone(oid)
            self.assertIn(
                "Concurrent Symbol Limit",
                strategy.last_submit_reject_reason,
            )
            self.assertEqual(gateway.sent_requests, [])
        finally:
            oms.stop()

    @patch("oms.validator.ref_data_manager.get_info", return_value=None)
    @patch("oms.validator.data_cache.get_best_quote", return_value=(99.9, 100.1))
    @patch("oms.validator.data_cache.get_mark_price", return_value=100.0)
    def test_gateway_submit_commit_runs_after_durable_pending_ack(self, *_mocks):
        engine = DispatchingEngine()

        class StagedGateway(DummyGateway):
            def __init__(self):
                super().__init__(send_order_result="paper-exchange-order")
                self.oms = None
                self.commit_observations = []

            def commit_order_submission(self, client_oid):
                self.commit_observations.append(
                    {
                        "status": self.oms.orders[client_oid].status,
                        "exchange_oid": self.oms.orders[client_oid].exchange_oid,
                        "submitted_event_seen": any(
                            event.type == "eOrderSubmitted" for event in engine.events
                        ),
                    }
                )

        gateway = StagedGateway()
        oms = OMS(engine, gateway, self.make_config())
        gateway.oms = oms
        oms.state = LifecycleState.LIVE
        try:
            result = oms.submit_order(
                OrderIntent("dummy", "BTCUSDT", Side.BUY, 100.0, 0.1)
            )

            self.assertTrue(result.accepted)
            self.assertEqual(
                gateway.commit_observations,
                [
                    {
                        "status": OrderStatus.PENDING_ACK,
                        "exchange_oid": "paper-exchange-order",
                        "submitted_event_seen": True,
                    }
                ],
            )
        finally:
            oms.stop()

    @patch("oms.validator.ref_data_manager.get_info", return_value=None)
    @patch("oms.validator.data_cache.get_best_quote", return_value=(99.9, 100.1))
    @patch("oms.validator.data_cache.get_mark_price", return_value=100.0)
    def test_exit_orders_are_submitted_as_reduce_only(self, *_mocks):
        engine = DispatchingEngine()
        gateway = DummyGateway(send_order_result="ex-order")
        oms = OMS(engine, gateway, self.make_config())
        strategy = DummyStrategy(engine, oms)
        oms.state = LifecycleState.LIVE
        oms._sync_capability_mode("test_live")
        oms.exposure.force_sync("BTCUSDT", -1.0, 100.0)
        try:
            oid = strategy.exit_short("BTCUSDT", 100.0, 1.0)

            self.assertTrue(oid)
            self.assertEqual(len(gateway.sent_requests), 1)
            sent_req, _client_oid = gateway.sent_requests[0]
            self.assertTrue(sent_req.reduce_only)
            self.assertEqual(sent_req.side, "BUY")
        finally:
            oms.stop()


class LocalWebDashboardTests(unittest.TestCase):
    def make_config(self):
        return {
            "testnet": True,
            "api_key": "dashboard-must-not-leak-this-key",
            "api_secret": "dashboard-must-not-leak-this-secret",
            "symbols": ["BTCUSDT"],
            "system": {
                "web_dashboard": {
                    "host": "127.0.0.1",
                    "port": 0,
                    "orders_limit": 20,
                    "trades_limit": 20,
                    "logs_limit": 20,
                    "history_limit": 20,
                }
            },
            "strategy": {
                "name": "GLFT_MultiScale",
                "primary_model": "glft",
                "registered_models": [
                    "glft",
                    "avellaneda_stoikov",
                ],
                "execution_policy": "single_primary",
                "use_rpi": True,
                "rpi_fallback_to_gtx": True,
            },
            "risk": {"limits": {"max_order_notional": 25.0}},
        }

    def test_local_dashboard_serves_complete_redacted_rpi_snapshot(self):
        dashboard = LocalWebDashboard(config=self.make_config())
        dashboard.update_account(
            AccountData(
                balance=100.0,
                equity=101.0,
                available=90.0,
                used_margin=11.0,
                datetime=datetime.utcnow(),
                budget_balance=100.0,
                budget_available=89.0,
            )
        )
        dashboard.update_market(
            OrderBook(
                symbol="BTCUSDT",
                exchange="BINANCE",
                datetime=datetime.utcnow(),
                best_bid_price=100.0,
                best_bid_volume=2.0,
                best_ask_price=100.2,
                best_ask_volume=1.0,
                received_timestamp=datetime.now().timestamp(),
            )
        )
        dashboard.update_strategy(
            StrategyData(
                symbol="BTCUSDT",
                fair_value=100.15,
                alpha_bps=1.5,
                params={"strategy": "A-S", "gamma": 0.1, "State": "QUOTE"},
            )
        )
        dashboard.update_order(
            OrderStateSnapshot(
                client_oid="client-order-dashboard-001",
                exchange_oid="exchange-order-dashboard-001",
                symbol="BTCUSDT",
                status=OrderStatus.PARTIALLY_FILLED,
                price=100.0,
                volume=1.0,
                filled_volume=0.25,
                avg_price=100.0,
                update_time=datetime.now().timestamp(),
                time_in_force=TIF_RPI,
                is_post_only=True,
                is_rpi=True,
            )
        )
        dashboard.update_exchange_order(
            ExchangeOrderUpdate(
                client_oid="client-order-dashboard-001",
                exchange_oid="exchange-order-dashboard-001",
                symbol="BTCUSDT",
                status="PARTIALLY_FILLED",
                filled_qty=0.25,
                filled_price=100.0,
                cum_filled_qty=0.25,
                update_time=datetime.now().timestamp(),
                commission=0.001,
                commission_asset="USDT",
                realized_pnl=0.05,
                is_maker=True,
                trade_id=123,
                order_type="LIMIT",
                time_in_force=TIF_RPI,
            )
        )
        dashboard.update_api_limit(
            ApiLimitData(weight_used_1m=12, timestamp=datetime.now().timestamp())
        )
        dashboard.update_alert(
            AlertData(level="WARNING", msg="test alert", timestamp=datetime.now().timestamp())
        )
        dashboard.add_log("[INFO] signature=must-not-leak")
        dashboard.publish_snapshot(
            {
                "event_engine": {"queues": {"market_depth": 0}},
                "strategy_runtime": {"control_depth": 0},
            },
            force=True,
        )

        snapshot = dashboard.get_snapshot()
        rendered = json.dumps(snapshot)
        self.assertEqual(snapshot["meta"]["schema_version"], 1)
        self.assertEqual(snapshot["meta"]["strategy"], "GLFT_MultiScale")
        self.assertEqual(snapshot["meta"]["primary_strategy_model"], "glft")
        self.assertEqual(
            snapshot["meta"]["registered_strategy_models"],
            ["glft", "avellaneda_stoikov"],
        )
        self.assertEqual(
            snapshot["meta"]["strategy_execution_policy"],
            "single_primary",
        )
        self.assertEqual(snapshot["symbols"][0]["strategy"]["params"]["gamma"], 0.1)
        self.assertEqual(snapshot["rpi"]["total_orders"], 1)
        self.assertEqual(snapshot["rpi"]["fill_count"], 1)
        self.assertAlmostEqual(snapshot["rpi"]["total_commission"], 0.001)
        self.assertFalse(snapshot["rpi"]["depth"]["available"])
        self.assertEqual(snapshot["runtime"]["api_limit"]["weight_used_1m"], 12)
        self.assertNotIn("dashboard-must-not-leak", rendered)
        self.assertNotIn("must-not-leak", rendered)

        try:
            base_url = dashboard.start()
            with urlopen(base_url, timeout=2.0) as response:
                html = response.read().decode("utf-8")
            self.assertIn("ChronosHFT", html)
            self.assertIn("RPI", html)

            with urlopen(f"{base_url}api/snapshot", timeout=2.0) as response:
                remote_snapshot = json.loads(response.read().decode("utf-8"))
            self.assertEqual(remote_snapshot["meta"]["schema_version"], 1)

            request = Request(f"{base_url}api/snapshot", headers={"Host": "example.com"})
            with self.assertRaises(HTTPError) as rejected:
                urlopen(request, timeout=2.0)
            self.assertEqual(rejected.exception.code, 400)
        finally:
            dashboard.stop()

    def test_startup_blocked_dashboard_separates_liveness_from_readiness(self):
        class UnhealthyClock:
            active = True

            def __init__(self):
                self.notify_listener_args = []

            def health_snapshot(self, *, notify_listeners=True):
                self.notify_listener_args.append(notify_listeners)
                return {
                    "state": "unsynchronized",
                    "ready": False,
                    "reason": "exchange clock quorum failed",
                    "last_error": "connect timeout",
                }

        clock = UnhealthyClock()
        dashboard = LocalWebDashboard(
            config=self.make_config(),
            time_service=clock,
        )
        dashboard.set_startup_status(
            state="STARTUP_BLOCKED",
            operating_mode="OBSERVE_ONLY",
            startup_blocked=True,
            execution_enabled=False,
            restart_required=True,
            reason="exchange clock quorum failed",
        )
        dashboard.publish_snapshot(force=True)

        snapshot = dashboard.get_snapshot()
        self.assertTrue(snapshot["startup"]["startup_blocked"])
        self.assertFalse(snapshot["startup"]["execution_enabled"])
        self.assertTrue(snapshot["startup"]["restart_required"])
        self.assertEqual(snapshot["system"]["startup"], snapshot["startup"])
        self.assertEqual(snapshot["system"]["time_service"]["health"], "unsynchronized")
        self.assertTrue(clock.notify_listener_args)
        self.assertTrue(all(value is False for value in clock.notify_listener_args))

        try:
            base_url = dashboard.start()
            with urlopen(f"{base_url}healthz", timeout=2.0) as response:
                health = json.loads(response.read().decode("utf-8"))
            self.assertEqual(health["status"], "ok")
            self.assertEqual(health["engine_status"], "STARTUP_BLOCKED")
            self.assertTrue(health["startup_blocked"])

            with self.assertRaises(HTTPError) as rejected:
                urlopen(f"{base_url}readyz", timeout=2.0)
            self.assertEqual(rejected.exception.code, 503)
            readiness = json.loads(rejected.exception.read().decode("utf-8"))
            self.assertEqual(readiness["status"], "not_ready")
            self.assertFalse(readiness["execution_enabled"])
            self.assertTrue(readiness["restart_required"])
        finally:
            dashboard.stop()

    def test_paper_dashboard_never_labels_simulated_execution_as_live_money(self):
        config = self.make_config()
        config["testnet"] = False
        config["execution"] = {"mode": "paper"}
        config["paper_trade"] = {
            "enabled": True,
            "market_data_environment": "production",
            "rpi_fill_model": "disabled",
        }

        dashboard = LocalWebDashboard(config=config)
        snapshot = dashboard.get_snapshot()

        self.assertEqual(snapshot["meta"]["execution_mode"], "paper")
        self.assertEqual(snapshot["meta"]["environment"], "PAPER_LIVE_DATA")
        self.assertEqual(
            snapshot["meta"]["environment_label"],
            "PAPER · LIVE DATA",
        )
        self.assertEqual(
            snapshot["meta"]["market_data_environment"],
            "BINANCE_MAINNET_PUBLIC",
        )
        self.assertEqual(snapshot["meta"]["execution_venue"], "LOCAL_SIMULATOR")
        self.assertTrue(snapshot["meta"]["simulated_execution"])
        self.assertTrue(snapshot["meta"]["simulated_funds"])
        self.assertFalse(snapshot["meta"]["private_api_enabled"])
        self.assertTrue(snapshot["orders"]["simulated"])
        self.assertTrue(snapshot["trades"]["simulated"])
        self.assertTrue(snapshot["rpi"]["simulated"])
        self.assertFalse(snapshot["rpi"]["real_binance_retail_counterparty_verified"])

    def test_dashboard_derives_nonzero_position_pnl_from_oms_snapshot(self):
        oms = SimpleNamespace(
            lock=threading.RLock(),
            orders={},
            state=LifecycleState.LIVE,
            manual_rearm_required=False,
            last_freeze_reason="",
            last_halt_reason="",
            exposure=SimpleNamespace(
                net_positions={"BTCUSDT": 2.0},
                avg_prices={"BTCUSDT": 100.0},
                open_buy_qty={"BTCUSDT": 0.0},
                open_sell_qty={"BTCUSDT": 0.0},
                reduce_only_buy_qty={"BTCUSDT": 0.0},
                reduce_only_sell_qty={"BTCUSDT": 0.0},
            ),
            account=SimpleNamespace(
                balance=100.0,
                equity=104.0,
                available=90.0,
                used_margin=14.0,
            ),
        )
        dashboard = LocalWebDashboard(oms=oms, config=self.make_config())
        dashboard.update_market(
            OrderBook(
                symbol="BTCUSDT",
                exchange="BINANCE",
                datetime=datetime.utcnow(),
                best_bid_price=101.9,
                best_bid_volume=1.0,
                best_ask_price=102.1,
                best_ask_volume=1.0,
                received_timestamp=datetime.now().timestamp(),
            )
        )
        dashboard.publish_snapshot(force=True)

        symbol = dashboard.get_snapshot()["symbols"][0]
        self.assertAlmostEqual(symbol["position"]["unrealized_pnl"], 4.0)
        self.assertEqual(symbol["position"]["unrealized_pnl_source"], "derived_mark_to_market")


class LiveAcceptanceDashboardTests(unittest.TestCase):
    @staticmethod
    def make_live_config():
        return {
            "testnet": False,
            "execution": {"mode": "live"},
            "record_data": True,
            "symbols": ["XAUUSDT"],
            "alert": {"active": True},
            "system": {
                "evidence_recorder": {"enabled": True},
                "web_dashboard": {"host": "127.0.0.1", "port": 0},
            },
            "strategy": {"primary_model": "glft", "use_rpi": True},
            "risk": {"limits": {}},
        }

    @staticmethod
    def make_calibration_oms():
        oms = object.__new__(OMS)
        oms.orders = {}
        oms._rpi_calibration = {
            "enabled": True,
            "permit_id": "permit-live-001",
            "permit_sha256": "1" * 64,
            "deployment_id": "deployment-live-001",
            "symbol": "XAUUSDT",
            "max_active_orders": 1,
            "max_order_count": 10,
            "min_order_notional_microu": 5_000_000,
            "max_order_notional_microu": 8_000_000,
            "max_cumulative_notional_microu": 80_000_000,
            "max_calibration_loss_microu": 2_000_000,
            "fixed_depths_bps": (1.0, 1.25, 1.5),
            "order_ttl_sec": 30.0,
            "min_order_interval_sec": 30.0,
            "not_before": "2026-07-24T12:00:00Z",
            "expires_at": "2026-07-24T12:10:00Z",
            "calibration_config_sha256": "2" * 64,
            "target_deployment_config_sha256": "3" * 64,
            "strategy_policy_sha256": "4" * 64,
            "implementation_sha256": "5" * 64,
        }
        oms._rpi_calibration_expired = False
        oms._rpi_calibration_expiry_reason = ""
        oms._rpi_calibration_restart_rearm_blocked = False
        oms._rpi_calibration_budget_exhausted = False
        oms._rpi_calibration_permit_activated = True
        oms._rpi_calibration_reserved_order_count = 2
        oms._rpi_calibration_permit_start_order_count = 0
        oms._rpi_calibration_cumulative_notional_microu = 16_000_000
        oms._rpi_calibration_permit_start_notional_microu = 0
        oms._rpi_calibration_last_reserved_exchange_ns = 1_753_358_430_000_000_000
        oms._rpi_calibration_effective_loss_cap_microu = 2_000_000
        oms._rpi_calibration_peak_observed_loss_microu = 100_000
        oms._rpi_calibration_start_equity_microu = 10_000_000_000
        return oms

    def test_oms_calibration_snapshot_carries_all_binding_digests(self):
        snapshot = self.make_calibration_oms()._rpi_calibration_snapshot_locked()

        self.assertEqual(snapshot["stage"], "rpi_calibration_canary")
        self.assertTrue(snapshot["permit_activated"])
        self.assertEqual(snapshot["permit_sha256"], "1" * 64)
        self.assertEqual(snapshot["calibration_config_sha256"], "2" * 64)
        self.assertEqual(snapshot["target_deployment_config_sha256"], "3" * 64)
        self.assertEqual(snapshot["strategy_policy_sha256"], "4" * 64)
        self.assertEqual(snapshot["implementation_sha256"], "5" * 64)

    def test_live_runtime_health_blocks_only_after_explicit_report(self):
        dashboard = LocalWebDashboard(config=self.make_live_config())
        initial_reasons = dashboard.get_snapshot()["readiness"]["reasons"]
        self.assertFalse(
            any(reason.startswith("external_alerts_unhealthy") for reason in initial_reasons)
        )
        self.assertFalse(
            any(reason.startswith("live_evidence_unhealthy") for reason in initial_reasons)
        )

        dashboard.publish_snapshot(
            {
                "external_alerts": {
                    "available": True,
                    "enabled": True,
                    "healthy": False,
                    "reason": "delivery_failed",
                },
                "live_evidence": {
                    "available": True,
                    "enabled": True,
                    "healthy": False,
                    "failure_reason": "fsync_failed",
                },
            },
            force=True,
        )
        reasons = dashboard.get_snapshot()["readiness"]["reasons"]
        self.assertIn("external_alerts_unhealthy:delivery_failed", reasons)
        self.assertIn("live_evidence_unhealthy:fsync_failed", reasons)

    def test_paper_runtime_health_does_not_block_readiness(self):
        config = self.make_live_config()
        config["execution"] = {"mode": "paper"}
        config["paper_trade"] = {"enabled": True}
        dashboard = LocalWebDashboard(config=config)
        dashboard.publish_snapshot(
            {
                "external_alerts": {"available": True, "healthy": False},
                "live_evidence": {"available": True, "healthy": False},
            },
            force=True,
        )

        reasons = dashboard.get_snapshot()["readiness"]["reasons"]
        self.assertFalse(any("external_alerts_unhealthy" in reason for reason in reasons))
        self.assertFalse(any("live_evidence_unhealthy" in reason for reason in reasons))

    def test_dashboard_html_renders_live_acceptance_sources(self):
        html = (
            Path(__file__).resolve().parents[1] / "web" / "dashboard.html"
        ).read_text(encoding="utf-8")

        for marker in (
            'id="acceptanceMetrics"',
            'id="acceptancePermit"',
            'id="acceptanceAlerts"',
            'id="acceptanceEvidence"',
            'id="acceptanceDigests"',
            "system.oms.capability.outbound_gate.rpi_calibration",
            "terminal_convergence_verified",
            "runtime.external_alerts",
            "runtime.live_evidence",
            "strategy_policy_sha256",
            "implementation_sha256",
        ):
            self.assertIn(marker, html)


class StrategyRegistryTests(unittest.TestCase):
    @staticmethod
    def make_root_config(primary_model):
        return {
            "strategy": {
                "registered_models": [
                    "GLFT_MultiScale",
                    "as",
                ],
                "primary_model": primary_model,
                "shared": {
                    "lot_multiplier": 2.5,
                    "cycle_interval": 3.0,
                    "use_rpi": True,
                    "execution": {
                        "cycle_interval_sec": 2.0,
                        "min_spread_bps": 9.0,
                    },
                },
                "models": {
                    "GLFT": {
                        "gamma": 0.42,
                        "execution": {"min_spread_bps": 7.5},
                    },
                    "AvellanedaStoikov": {
                        "gamma": 0.23,
                        "k": 2.75,
                        "vol_window": 17,
                    },
                },
            },
        }

    def test_aliases_resolve_to_canonical_models_and_stable_strategy_ids(self):
        aliases = {
            "GLFT_MultiScale": ("glft", "GLFT_MultiScale"),
            "as": ("avellaneda_stoikov", "AvellanedaStoikov"),
            "AvellanedaStoikov": (
                "avellaneda_stoikov",
                "AvellanedaStoikov",
            ),
        }

        for alias, (model_key, strategy_id) in aliases.items():
            with self.subTest(alias=alias):
                self.assertEqual(canonical_model_key(alias), model_key)
                self.assertEqual(strategy_id_for_model(alias), strategy_id)

    def test_registry_constructs_each_primary_but_only_one_execution_instance(self):
        cases = (
            ("GLFT_MultiScale", GLFTStrategy, "glft", "GLFT_MultiScale"),
            (
                "as",
                AvellanedaStoikovStrategy,
                "avellaneda_stoikov",
                "AvellanedaStoikov",
            ),
        )

        for primary, expected_type, model_key, strategy_id in cases:
            with self.subTest(primary=primary):
                strategy = create_primary_strategy(
                    DispatchingEngine(),
                    PassiveQuoteOMS(),
                    self.make_root_config(primary),
                )

                self.assertIsInstance(strategy, expected_type)
                self.assertEqual(strategy.name, strategy_id)
                self.assertEqual(strategy.strategy_id, strategy_id)
                self.assertEqual(strategy.model_key, model_key)
                self.assertEqual(
                    strategy.registered_models,
                    ("glft", "avellaneda_stoikov"),
                )
                self.assertEqual(strategy.execution_role, "primary")

    def test_registry_deep_merges_shared_and_model_parameters(self):
        glft = create_primary_strategy(
            DispatchingEngine(),
            PassiveQuoteOMS(),
            self.make_root_config("GLFT"),
        )
        self.assertEqual(glft.lot_multiplier, 2.5)
        self.assertEqual(glft.cycle_interval, 3.0)
        self.assertEqual(glft.gamma_base, 0.42)
        self.assertEqual(glft.min_spread_bps, 7.5)

        avellaneda_stoikov = create_primary_strategy(
            DispatchingEngine(),
            PassiveQuoteOMS(),
            self.make_root_config("AvellanedaStoikov"),
        )
        self.assertEqual(avellaneda_stoikov.lot_multiplier, 2.5)
        self.assertEqual(avellaneda_stoikov.interval, 3.0)
        self.assertEqual(avellaneda_stoikov.gamma, 0.23)
        self.assertEqual(avellaneda_stoikov.k, 2.75)
        self.assertEqual(avellaneda_stoikov.vol_window, 17)

    def test_registry_fails_fast_for_unknown_or_unregistered_primary(self):
        unknown = self.make_root_config("GLFT")
        unknown["strategy"]["registered_models"] = ["GLFT", "not-a-model"]
        with self.assertRaisesRegex(ValueError, "Unknown strategy model"):
            create_primary_strategy(
                DispatchingEngine(),
                PassiveQuoteOMS(),
                unknown,
            )

        unregistered = self.make_root_config("as")
        unregistered["strategy"]["registered_models"] = ["glft"]
        with self.assertRaisesRegex(ValueError, "not present"):
            create_primary_strategy(
                DispatchingEngine(),
                PassiveQuoteOMS(),
                unregistered,
            )

        concurrent = self.make_root_config("glft")
        concurrent["strategy"]["execution_policy"] = "concurrent"
        with self.assertRaisesRegex(ValueError, "single_primary"):
            create_primary_strategy(
                DispatchingEngine(),
                PassiveQuoteOMS(),
                concurrent,
            )


if __name__ == "__main__":
    unittest.main()
