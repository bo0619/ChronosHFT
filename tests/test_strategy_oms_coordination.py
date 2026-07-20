import json
import sys
import threading
import types
import unittest
from datetime import datetime
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
    Event,
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
    EVENT_ACCOUNT_UPDATE,
    EVENT_LOG,
    EVENT_ORDER_UPDATE,
    EVENT_STRATEGY_UPDATE,
    EVENT_SYSTEM_HEALTH,
)
from data.ref_data import ContractInfo, ref_data_manager
from oms.engine import OMS
from strategy.avellaneda_stoikov import AvellanedaStoikovStrategy
from strategy.base import StrategyTemplate
from strategy.glft import GLFTStrategy
from strategy.ml_sniper.ml_sniper import MLSniperStrategy
from strategy.predictive_glft import PredictiveGLFTStrategy
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


class PassiveQuoteOMS:
    def __init__(self):
        self.config = {
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

    def submit_order(self, intent):
        self.submitted.append(intent)
        return f"passive-{len(self.submitted)}"

    def cancel_order(self, client_oid):
        self.cancelled.append(client_oid)


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
            7.0,
        )
        self.assertAlmostEqual(
            strategy.passive_round_trip_fee_bps("LTCUSDT", TIF_GTX),
            4.0,
        )

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
            self.assertIn("reservation_price", telemetry.params)
            self.assertIn("inventory_risk_adjustment", telemetry.params)
            self.assertIn("optimal_spread", telemetry.params)
            self.assertIn("volatility_adjustment", telemetry.params)
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
        self.assertEqual(params["bid_order_id"], "passive-1")
        self.assertEqual(params["ask_order_id"], "passive-2")
        for field in (
            "gamma",
            "k",
            "A",
            "sigma_bps",
            "target_position_notional",
            "effective_position_notional",
            "q_norm",
            "base_half_spread_bps",
            "inventory_skew_bps",
            "effective_min_spread_bps",
        ):
            self.assertIn(field, params)
        json.dumps(params)

    def test_predictive_glft_uses_params_and_publishes_model_telemetry(self):
        engine = DispatchingEngine()
        oms = PassiveQuoteOMS()
        with patch.object(
            PredictiveGLFTStrategy,
            "_load_strategy_config",
            return_value={
                "strategy": {
                    "gamma": 0.1,
                    "lot_multiplier": 1.0,
                }
            },
        ):
            strategy = PredictiveGLFTStrategy(engine, oms)
        strategy.cycle_interval = 0.0
        strategy.cooldown_ms = 0
        calibrator = SimpleNamespace(
            on_orderbook=lambda _ob: None,
            sigma_bps=2.0,
            A=1.3,
            k=1.2,
        )
        model = SimpleNamespace(
            update_and_predict=lambda _features, _mid: 8.0,
        )
        strategy._get_components = lambda _symbol: (calibrator, model)
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
        self.assertEqual(params["time_in_force"], TIF_GTX)
        self.assertEqual(params["raw_prediction_bps"], 8.0)
        self.assertEqual(params["filtered_prediction_bps"], 8.0)
        self.assertEqual(params["bid_order_id"], "passive-1")
        self.assertEqual(params["ask_order_id"], "passive-2")
        for field in (
            "adjusted_prediction_bps",
            "prediction_confidence",
            "fee_threshold_bps",
            "inventory_ratio",
            "gamma_multiplier",
            "gamma",
            "margin_usage",
            "k",
            "A",
            "sigma_bps",
            "q_norm",
            "half_spread_bps",
            "inventory_skew_bps",
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
                    "ml_sniper",
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
            ["glft", "avellaneda_stoikov", "ml_sniper"],
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


class StrategyRegistryTests(unittest.TestCase):
    @staticmethod
    def make_root_config(primary_model):
        return {
            "system": {
                "strategy_runtime": {
                    "alpha_process": {
                        "enabled": False,
                        "processes": 7,
                    }
                }
            },
            "strategy": {
                "registered_models": [
                    "ML_Sniper",
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
                    "ml_sniper": {
                        "entry": {"maker_entry_threshold_bps": 4.25},
                        "execution": {"tick_interval_sec": 0.2},
                    },
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
            "ML_Sniper": ("ml_sniper", "ML_Sniper"),
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
            ("ML_Sniper", MLSniperStrategy, "ml_sniper", "ML_Sniper"),
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
                    ("ml_sniper", "glft", "avellaneda_stoikov"),
                )
                self.assertEqual(strategy.execution_role, "primary")
                if model_key == "ml_sniper":
                    self.assertFalse(strategy.alpha_process.enabled)
                    self.assertEqual(strategy.alpha_process.worker_count, 7)

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

        ml_sniper = create_primary_strategy(
            DispatchingEngine(),
            PassiveQuoteOMS(),
            self.make_root_config("ml_sniper"),
            alpha_process_config={"enabled": False, "processes": 3},
        )
        self.assertEqual(ml_sniper.lot_multiplier, 2.5)
        self.assertEqual(ml_sniper.base_maker_entry_threshold, 4.25)
        self.assertEqual(ml_sniper.tick_interval, 0.2)
        self.assertEqual(ml_sniper.cycle_interval, 2.0)
        self.assertFalse(ml_sniper.alpha_process.enabled)
        self.assertEqual(ml_sniper.alpha_process.worker_count, 3)

    def test_registry_fails_fast_for_unknown_or_unregistered_primary(self):
        unknown = self.make_root_config("ML_Sniper")
        unknown["strategy"]["registered_models"] = ["ML_Sniper", "not-a-model"]
        with self.assertRaisesRegex(ValueError, "Unknown strategy model"):
            create_primary_strategy(
                DispatchingEngine(),
                PassiveQuoteOMS(),
                unknown,
            )

        unregistered = self.make_root_config("as")
        unregistered["strategy"]["registered_models"] = ["ml_sniper", "glft"]
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
