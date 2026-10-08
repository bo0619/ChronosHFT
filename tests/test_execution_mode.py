import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from data.ref_data import ContractInfo, ref_data_manager
from event.type import (
    EVENT_STRATEGY_UPDATE,
    TIF_IOC,
    ExecutionPolicy,
    OrderBook,
    OrderStateSnapshot,
    OrderStatus,
    Side,
    TradeData,
)
from infrastructure.rpi_policy import validate_live_rpi_policy
from strategy.execution_mode import (
    EXECUTION_MODE_MARKET,
    EXECUTION_MODE_POST_ONLY,
    MarketExecutionPolicy,
    decide_market_order,
    resolve_execution_mode,
)
from strategy.quote_math import QuoteOffsets
from tests.test_strategy_oms_coordination import (
    AvellanedaStoikovStrategy,
    DispatchingEngine,
    GLFTStrategy,
    PassiveQuoteOMS,
    StrategyTestClock,
)


def _contract():
    return ContractInfo(
        symbol="LTCUSDT",
        tick_size=0.1,
        step_size=0.001,
        min_qty=0.001,
        min_notional=5.0,
        price_precision=1,
        qty_precision=3,
        status="TRADING",
        permissions=frozenset(),
    )


def _orderbook(mid=100.0):
    return OrderBook(
        symbol="LTCUSDT",
        exchange="BINANCE",
        datetime=datetime.utcnow(),
        best_bid_price=mid - 0.1,
        best_bid_volume=1.0,
        best_ask_price=mid + 0.1,
        best_ask_volume=1.0,
    )


def _formula(center_offset_bps):
    return QuoteOffsets(
        bid_depth_bps=5.0 - center_offset_bps,
        ask_depth_bps=5.0 + center_offset_bps,
        center_offset_bps=center_offset_bps,
        half_spread_bps=5.0,
        bid_price=99.0,
        ask_price=101.0,
    )


class ResolveExecutionModeTests(unittest.TestCase):
    def test_absent_config_keeps_post_only(self):
        self.assertEqual(resolve_execution_mode({}), EXECUTION_MODE_POST_ONLY)
        self.assertEqual(
            resolve_execution_mode({"execution_modes": {}}),
            EXECUTION_MODE_POST_ONLY,
        )

    def test_binance_market_mode(self):
        self.assertEqual(
            resolve_execution_mode(
                {"execution_modes": {"binance": "market"}}
            ),
            EXECUTION_MODE_MARKET,
        )

    def test_rejects_unknown_mode_and_venue(self):
        with self.assertRaises(ValueError):
            resolve_execution_mode({"execution_modes": {"binance": "taker"}})
        with self.assertRaises(ValueError):
            resolve_execution_mode({"execution_modes": {"okx": "market"}})

    def test_market_policy_defaults_and_validation(self):
        policy = MarketExecutionPolicy.from_config({})
        self.assertEqual(policy.min_edge_bps, 1.0)
        self.assertEqual(policy.cooldown_ms, 1000.0)
        with self.assertRaises(ValueError):
            MarketExecutionPolicy.from_config(
                {"market_execution": {"min_edge_bps": -1.0}}
            )


class DecideMarketOrderTests(unittest.TestCase):
    def test_buys_when_reservation_clears_ask_by_fee_and_edge(self):
        decision = decide_market_order(
            reservation_price=100.2,
            best_bid=99.9,
            best_ask=100.0,
            taker_fee_bps=5.0,
            min_edge_bps=1.0,
        )
        self.assertEqual(decision.side, Side.BUY)
        self.assertAlmostEqual(
            decision.buy_edge_bps,
            0.2 / 100.2 * 10_000.0 - 5.0,
        )

    def test_sells_when_reservation_is_below_bid(self):
        decision = decide_market_order(
            reservation_price=99.7,
            best_bid=99.9,
            best_ask=100.0,
            taker_fee_bps=0.0,
            min_edge_bps=1.0,
        )
        self.assertEqual(decision.side, Side.SELL)

    def test_holds_inside_the_spread_or_below_min_edge(self):
        inside = decide_market_order(
            reservation_price=99.95,
            best_bid=99.9,
            best_ask=100.0,
            taker_fee_bps=0.0,
            min_edge_bps=0.0,
        )
        self.assertIsNone(inside.side)
        thin = decide_market_order(
            reservation_price=100.005,
            best_bid=99.9,
            best_ask=100.0,
            taker_fee_bps=0.0,
            min_edge_bps=1.0,
        )
        self.assertIsNone(thin.side)


class GLFTMarketExecutionTests(unittest.TestCase):
    def make_strategy(self, oms, execution_modes, clock_values):
        engine = DispatchingEngine()
        strategy = GLFTStrategy(
            engine,
            oms,
            clock=StrategyTestClock(clock_values),
            strategy_config={
                "cycle_interval": 0.0,
                "execution": {"min_spread_bps": 5.0},
                "execution_modes": execution_modes,
                "market_execution": {
                    "min_edge_bps": 1.0,
                    "cooldown_ms": 500.0,
                },
            },
        )
        calibrator = SimpleNamespace(
            on_orderbook=lambda _ob: None,
            sigma_bps=2.5,
            A=1.2,
            k=1.1,
        )
        model = SimpleNamespace(
            update_and_predict=lambda *_args: {
                "short": 0.0,
                "mid": 0.0,
                "long": 0.0,
            }
        )
        gate = SimpleNamespace(process=lambda value, _position: value)
        strategy._get_components = lambda _symbol: (calibrator, model, gate)
        strategy.feature_engine = SimpleNamespace(
            on_orderbook=lambda _ob: None,
            get_features=lambda _symbol: [0.0] * 9,
            reset_interval=lambda _symbol: None,
        )
        return engine, strategy

    def run_cycles(self, strategy, center_offset_bps, cycles=1):
        strategy._calculate_formula_quote = lambda **_kwargs: (
            _formula(center_offset_bps),
            {},
        )
        with patch.dict(
            ref_data_manager.contracts,
            {"LTCUSDT": _contract()},
            clear=True,
        ):
            for _ in range(cycles):
                strategy.on_orderbook(_orderbook())

    def test_market_mode_sends_market_buy_and_waits_for_terminal(self):
        oms = PassiveQuoteOMS()
        oms.config["backtest"]["taker_fee"] = 0.0
        engine, strategy = self.make_strategy(
            oms,
            {"binance": "market"},
            [1.0, 1.1, 2.0, 2.1, 3.0, 3.1],
        )

        # Reservation 30 bps above mid clears the 10 bps half spread.
        self.run_cycles(strategy, 30.0, cycles=2)

        self.assertEqual(len(oms.submitted), 1)
        intent = oms.submitted[0]
        self.assertEqual(intent.side, Side.BUY)
        self.assertEqual(intent.order_type, "MARKET")
        self.assertEqual(intent.time_in_force, TIF_IOC)
        self.assertFalse(intent.is_post_only)
        self.assertEqual(intent.policy, ExecutionPolicy.AGGRESSIVE)
        params = [
            event.data.params
            for event in engine.events
            if event.type == EVENT_STRATEGY_UPDATE
        ]
        self.assertEqual(params[0]["market_action"], "MARKET_BUY")
        self.assertEqual(params[1]["market_action"], "ORDER_IN_FLIGHT")

        strategy.on_order(
            OrderStateSnapshot(
                client_oid="passive-1",
                exchange_oid="ex-1",
                symbol="LTCUSDT",
                status=OrderStatus.FILLED,
                price=100.1,
                volume=intent.volume,
                filled_volume=intent.volume,
                avg_price=100.1,
                update_time=0.0,
            )
        )
        self.run_cycles(strategy, 30.0)
        self.assertEqual(len(oms.submitted), 2)

    def test_market_mode_sells_and_never_rests_quotes(self):
        oms = PassiveQuoteOMS()
        oms.config["backtest"]["taker_fee"] = 0.0
        _, strategy = self.make_strategy(
            oms,
            {"binance": "market"},
            [1.0, 1.1],
        )
        self.run_cycles(strategy, -30.0)
        self.assertEqual([i.side for i in oms.submitted], [Side.SELL])
        self.assertTrue(all(i.order_type == "MARKET" for i in oms.submitted))

    def test_market_mode_holds_when_taker_fee_eats_the_edge(self):
        oms = PassiveQuoteOMS()
        oms.config["backtest"]["taker_fee"] = 0.0005
        engine, strategy = self.make_strategy(
            oms,
            {"binance": "market"},
            [1.0, 1.1],
        )
        # 12 bps reservation - 10 bps half spread - 5 bps fee < 1 bps.
        self.run_cycles(strategy, 12.0)
        self.assertEqual(oms.submitted, [])
        params = [
            event.data.params
            for event in engine.events
            if event.type == EVENT_STRATEGY_UPDATE
        ]
        self.assertEqual(params[-1]["market_action"], "HOLD")
        self.assertEqual(params[-1]["taker_fee_bps"], 5.0)

    def test_post_only_default_is_unchanged(self):
        oms = PassiveQuoteOMS()
        _, strategy = self.make_strategy(oms, {}, [1.0, 1.1, 1.2])
        self.run_cycles(strategy, 30.0)
        self.assertEqual(len(oms.submitted), 2)
        self.assertTrue(all(i.is_post_only for i in oms.submitted))
        self.assertTrue(all(i.order_type == "LIMIT" for i in oms.submitted))

    def test_market_fills_do_not_train_passive_markout(self):
        oms = PassiveQuoteOMS()
        _, strategy = self.make_strategy(oms, {"binance": "market"}, None)
        recorded = []
        strategy.adaptive_enabled = True
        strategy.adaptive_markout = SimpleNamespace(
            record_fill=lambda **kwargs: recorded.append(kwargs["client_oid"])
        )
        strategy.market_order_oids["mkt-1"] = True
        for oid in ("mkt-1", "passive-9"):
            strategy.on_trade(
                TradeData(
                    symbol="LTCUSDT",
                    order_id=oid,
                    trade_id=f"t-{oid}",
                    side="BUY",
                    price=100.0,
                    volume=0.1,
                    datetime=datetime.utcnow(),
                )
            )
        self.assertEqual(recorded, ["passive-9"])

    def test_avellaneda_rejects_market_mode(self):
        with self.assertRaises(ValueError):
            AvellanedaStoikovStrategy(
                DispatchingEngine(),
                PassiveQuoteOMS(),
                strategy_config={
                    "execution_modes": {"binance": "market"},
                    "avellaneda_stoikov": {"gamma": 0.05, "k": 1.5},
                },
            )


class ExecutionModeConfigTests(unittest.TestCase):
    def test_live_rpi_policy_rejects_market_mode(self):
        config = {
            "strategy": {
                "use_rpi": True,
                "rpi_fallback_to_gtx": False,
                "rpi_live_policy": {"require_zero_commission": True},
                "execution_modes": {"binance": "market"},
            }
        }
        with self.assertRaisesRegex(ValueError, "execution_modes"):
            validate_live_rpi_policy(
                config,
                ["LTCUSDT"],
                {"LTCUSDT": True},
                {"LTCUSDT": 0.0},
            )

    def test_live_strategy_rejects_market_mode(self):
        _, strategy = GLFTMarketExecutionTests().make_strategy(
            PassiveQuoteOMS(),
            {},
            None,
        )
        with self.assertRaisesRegex(ValueError, "Paper-only"):
            strategy.init_execution_mode(
                {"execution_modes": {"binance": "market"}},
                live_mode=True,
            )
