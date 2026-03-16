import sys
import threading
import types
import unittest
from datetime import datetime, timedelta

if "requests" not in sys.modules:
    requests_module = types.ModuleType("requests")
    requests_module.Session = lambda: None
    requests_module.Request = object
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

from event.type import (
    AccountData,
    Event,
    ExchangeAccountUpdate,
    ExchangeOrderUpdate,
    ExecutionPolicy,
    GatewayState,
    MarkPriceData,
    OMSCapabilityMode,
    OrderBook,
    OrderIntent,
    Side,
    EVENT_ACCOUNT_UPDATE,
    EVENT_EXCHANGE_ACCOUNT_UPDATE,
    EVENT_EXCHANGE_ORDER_UPDATE,
    EVENT_SYSTEM_HEALTH,
)
from gateway.binance.gateway import BinanceGateway
from oms.engine import OMS
from oms.order import Order
from risk.manager import RiskManager


class DummyEngine:
    def __init__(self):
        self.events = []
        self.handlers = {}

    def put(self, event):
        self.events.append(event)

    def register(self, event_type, handler):
        self.handlers.setdefault(event_type, []).append(handler)


class DummyGateway:
    def __init__(self):
        self.gateway_name = "BINANCE"
        self.cancelled_symbols = []
        self.open_orders = []
        self.positions = []
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
        self.cancelled_symbols.append(symbol)
        return None

    def get_account_info(self):
        return self.account

    def get_all_positions(self):
        return self.positions

    def get_open_orders(self):
        return self.open_orders


class DummyOMS:
    def __init__(self):
        self.config = {"symbols": ["BTCUSDT"]}
        self.exposure = types.SimpleNamespace(net_positions={})
        self.halt_reasons = []
        self.frozen_symbols = []
        self.unfrozen_symbols = []
        self.frozen_venues = []
        self.unfrozen_venues = []
        self.trading_modes = []
        self.cleared_trading_modes = []
        self.flatten_reasons = []

    def halt_system(self, reason):
        self.halt_reasons.append(reason)

    def freeze_symbol(self, symbol, reason, cancel_active_orders=True):
        self.frozen_symbols.append((symbol, reason, cancel_active_orders))

    def clear_symbol_freeze(self, symbol, reason=""):
        self.unfrozen_symbols.append((symbol, reason))
        return True

    def freeze_venue(self, venue, reason, cancel_active_orders=True):
        self.frozen_venues.append((venue, reason, cancel_active_orders))

    def clear_venue_freeze(self, venue, reason=""):
        self.unfrozen_venues.append((venue, reason))
        return True

    def set_trading_mode(self, mode, reason):
        self.trading_modes.append((mode, reason))

    def clear_trading_mode(self, reason="", prefixes=()):
        self.cleared_trading_modes.append((reason, tuple(prefixes or ())))
        return True

    def emergency_reduce_only_flatten(self, reason):
        self.flatten_reasons.append(reason)
        return 0


class ExchangeTruthTests(unittest.TestCase):
    def make_config(self):
        return {
            "symbols": ["BTCUSDT"],
            "account": {
                "initial_balance_usdt": 1000.0,
                "leverage": 10,
            },
            "backtest": {
                "taker_fee": 0.02,
                "maker_fee": 0.01,
            },
            "oms": {
                "journal_enabled": False,
                "replay_journal_on_startup": False,
            },
            "risk": {
                "limits": {
                    "max_pos_notional": 5000.0,
                }
            },
        }

    def test_exchange_fill_uses_realized_pnl_and_commission(self):
        gateway = DummyGateway()
        oms = OMS(DummyEngine(), gateway, self.make_config())
        try:
            oms.exposure.force_sync("BTCUSDT", 1.0, 100.0)
            oms.account.force_sync(1000.0, 0.0)

            intent = OrderIntent(
                "test",
                "BTCUSDT",
                Side.SELL,
                90.0,
                1.0,
                is_post_only=False,
                policy=ExecutionPolicy.AGGRESSIVE,
            )
            order = Order("oid-close", intent)
            order.mark_submitting()
            order.mark_pending_ack("ex-close")
            order.mark_new("ex-close", update_time=1.0, seq=1)
            oms.orders[order.client_oid] = order
            oms.exchange_id_map[order.exchange_oid] = order

            update = ExchangeOrderUpdate(
                client_oid="oid-close",
                exchange_oid="ex-close",
                symbol="BTCUSDT",
                status="FILLED",
                filled_qty=1.0,
                filled_price=90.0,
                cum_filled_qty=1.0,
                update_time=2.0,
                seq=2,
                commission=1.5,
                commission_asset="USDT",
                realized_pnl=-8.0,
                is_maker=False,
            )

            oms._apply_event(Event(EVENT_EXCHANGE_ORDER_UPDATE, update))

            self.assertAlmostEqual(oms.account.balance, 990.5)
            self.assertAlmostEqual(oms.account.equity, 990.5)
        finally:
            oms.stop()

    def test_exchange_account_update_syncs_wallet_balance(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        oms = OMS(engine, gateway, self.make_config())
        try:
            update = ExchangeAccountUpdate(
                asset="USDT",
                wallet_balance=950.0,
                available_balance=900.0,
                balances={
                    "USDT": {"wallet_balance": 950.0, "available_balance": 900.0},
                    "USDC": {"wallet_balance": 125.0, "available_balance": 100.0},
                },
                positions={},
                reason="ORDER",
                event_time=1.0,
            )
            oms.on_exchange_account_update(Event(EVENT_EXCHANGE_ACCOUNT_UPDATE, update))

            self.assertAlmostEqual(oms.account.balance, 950.0)
            self.assertAlmostEqual(oms.account.available, 900.0)
            self.assertEqual(oms.account.balances["USDT"], 950.0)
            self.assertEqual(oms.account.balances["USDC"], 125.0)
            self.assertEqual(oms.account.available_balances["USDC"], 100.0)
            self.assertTrue(any(event.type == EVENT_ACCOUNT_UPDATE for event in engine.events))
        finally:
            oms.stop()

    def test_gateway_parses_user_stream_realized_and_account_updates(self):
        engine = DummyEngine()
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.event_engine = engine
        gateway.gateway_name = "BINANCE"
        gateway.global_sequence_id = 0
        gateway.seq_lock = threading.Lock()
        gateway.symbols = ["BTCUSDT"]

        gateway._handle_user_update(
            {
                "e": "ORDER_TRADE_UPDATE",
                "o": {
                    "c": "oid-1",
                    "i": 12345,
                    "s": "BTCUSDT",
                    "X": "PARTIALLY_FILLED",
                    "l": "0.1",
                    "L": "101.5",
                    "z": "0.1",
                    "T": 1000,
                    "n": "0.02",
                    "N": "USDT",
                    "rp": "1.23",
                    "m": True,
                },
            }
        )
        gateway._handle_account_update(
            {
                "e": "ACCOUNT_UPDATE",
                "E": 2000,
                "a": {
                    "m": "ORDER",
                    "B": [{"a": "USDT", "wb": "980.5", "cw": "950.0"}, {"a": "USDC", "wb": "210.0", "cw": "205.5"}],
                    "P": [{"s": "BTCUSDT", "pa": "0.1", "ep": "101.5", "up": "1.23"}],
                },
            }
        )

        order_event = engine.events[0]
        account_event = engine.events[1]
        self.assertEqual(order_event.data.realized_pnl, 1.23)
        self.assertEqual(order_event.data.commission, 0.02)
        self.assertTrue(order_event.data.is_maker)
        self.assertEqual(account_event.data.wallet_balance, 980.5)
        self.assertEqual(account_event.data.available_balance, 950.0)
        self.assertEqual(account_event.data.balances["USDT"]["wallet_balance"], 980.5)
        self.assertEqual(account_event.data.balances["USDC"]["available_balance"], 205.5)
        self.assertIn("BTCUSDT", account_event.data.positions)

    def test_gateway_ws_parse_failure_emits_system_health_event(self):
        engine = DummyEngine()
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.event_engine = engine
        gateway.gateway_name = "BINANCE"
        gateway.state = GatewayState.READY

        gateway.on_ws_message("{bad-json")

        self.assertEqual(engine.events[-1].type, EVENT_SYSTEM_HEALTH)
        self.assertIn("WS_PARSE_ERROR", engine.events[-1].data)

    def test_exchange_account_position_drift_triggers_reconcile_without_active_orders(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        oms = OMS(engine, gateway, self.make_config())
        try:
            oms.exposure.force_sync("BTCUSDT", 1.0, 100.0)
            called = []
            oms.trigger_reconcile = lambda reason, suspicious_oid=None: called.append((reason, suspicious_oid))

            update = ExchangeAccountUpdate(
                asset="USDT",
                wallet_balance=1000.0,
                available_balance=1000.0,
                balances={"USDT": {"wallet_balance": 1000.0, "available_balance": 1000.0}},
                positions={},
                reason="ORDER",
                event_time=1.0,
            )
            oms.on_exchange_account_update(Event(EVENT_EXCHANGE_ACCOUNT_UPDATE, update))

            self.assertEqual(called, [("Exchange account position drift", None)])
        finally:
            oms.stop()

    def test_exchange_account_position_drift_triggers_reconcile_with_active_orders(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        oms = OMS(engine, gateway, self.make_config())
        try:
            oms.exposure.force_sync("BTCUSDT", 1.0, 100.0)
            active_order = Order(
                "oid-active",
                OrderIntent("test", "BTCUSDT", Side.BUY, 100.0, 1.0),
            )
            active_order.mark_submitting()
            oms.orders[active_order.client_oid] = active_order

            called = []
            oms.trigger_reconcile = lambda reason, suspicious_oid=None: called.append((reason, suspicious_oid))

            update = ExchangeAccountUpdate(
                asset="USDT",
                wallet_balance=1000.0,
                available_balance=1000.0,
                balances={"USDT": {"wallet_balance": 1000.0, "available_balance": 1000.0}},
                positions={},
                reason="ORDER",
                event_time=1.0,
            )
            oms.on_exchange_account_update(Event(EVENT_EXCHANGE_ACCOUNT_UPDATE, update))

            self.assertEqual(called, [("Exchange account position drift", None)])
        finally:
            oms.stop()

class RiskExecutionTests(unittest.TestCase):
    def make_risk_config(self):
        return {
            "risk": {
                "active": True,
                "limits": {
                    "max_order_qty": 1000.0,
                    "max_order_notional": 5000.0,
                    "max_pos_notional": 10000.0,
                    "max_daily_loss": 1000.0,
                    "max_drawdown_pct": 0.02,
                },
                "price_sanity": {
                    "max_deviation_pct": 0.05,
                },
                "tech_health": {
                    "max_latency_ms": 100,
                    "max_processing_lag_ms": 100,
                    "max_order_count_per_sec": 20,
                    "consecutive_error_limit": 2,
                    "degraded_error_limit": 1,
                    "passive_only_error_limit": 2,
                },
                "black_swan": {
                    "volatility_halt_threshold": 0.05,
                },
            }
        }

    def test_latency_limit_triggers_kill_switch(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        oms = DummyOMS()
        risk = RiskManager(engine, self.make_risk_config(), oms=oms, gateway=gateway)

        stale_book = OrderBook(
            symbol="BTCUSDT",
            exchange="BINANCE",
            datetime=datetime.now() - timedelta(milliseconds=250),
        )
        risk.on_orderbook(Event("eOrderBook", stale_book))
        self.assertFalse(risk.kill_switch_triggered)
        risk.on_orderbook(Event("eOrderBook", stale_book))

        self.assertFalse(risk.kill_switch_triggered)
        self.assertEqual(len(oms.frozen_symbols), 1)
        self.assertTrue(oms.frozen_symbols[0][1].startswith("latency:"))

    def test_drawdown_pct_limit_triggers_kill_switch(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        oms = DummyOMS()
        risk = RiskManager(engine, self.make_risk_config(), oms=oms, gateway=gateway)

        risk.on_account_update(Event(EVENT_ACCOUNT_UPDATE, AccountData(1000.0, 1000.0, 1000.0, 0.0, datetime.now())))
        risk.on_account_update(Event(EVENT_ACCOUNT_UPDATE, AccountData(970.0, 970.0, 970.0, 0.0, datetime.now())))

        self.assertTrue(risk.kill_switch_triggered)
        self.assertIn("Drawdown", risk.kill_reason)

    def test_volatility_threshold_triggers_kill_switch(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        oms = DummyOMS()
        risk = RiskManager(engine, self.make_risk_config(), oms=oms, gateway=gateway)

        risk.on_mark_price(
            Event(
                "eMarkPrice",
                MarkPriceData(
                    symbol="BTCUSDT",
                    mark_price=106.0,
                    index_price=100.0,
                    funding_rate=0.0,
                    next_funding_time=datetime.now(),
                    datetime=datetime.now(),
                ),
            )
        )
        risk.on_mark_price(
            Event(
                "eMarkPrice",
                MarkPriceData(
                    symbol="BTCUSDT",
                    mark_price=106.0,
                    index_price=100.0,
                    funding_rate=0.0,
                    next_funding_time=datetime.now(),
                    datetime=datetime.now(),
                ),
            )
        )

        self.assertFalse(risk.kill_switch_triggered)
        self.assertTrue(oms.frozen_symbols)
        self.assertIn("divergence:", oms.frozen_symbols[-1][1])


    def test_latency_limit_uses_exchange_timestamp_over_local_datetime(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        oms = DummyOMS()
        risk = RiskManager(engine, self.make_risk_config(), oms=oms, gateway=gateway)

        fresh_local_time = datetime.now()
        exchange_ts = (fresh_local_time - timedelta(milliseconds=250)).timestamp()
        stale_book = OrderBook(
            symbol="BTCUSDT",
            exchange="BINANCE",
            datetime=fresh_local_time,
            exchange_timestamp=exchange_ts,
        )
        risk.on_orderbook(Event("eOrderBook", stale_book))
        risk.on_orderbook(Event("eOrderBook", stale_book))

        self.assertFalse(risk.kill_switch_triggered)
        self.assertTrue(oms.frozen_symbols)

    def test_symbol_freeze_escalates_when_multiple_symbols_are_frozen(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        oms = DummyOMS()
        oms.config = {"symbols": ["BTCUSDT", "ETHUSDT"]}
        risk = RiskManager(engine, self.make_risk_config(), oms=oms, gateway=gateway)

        stale_btc = OrderBook(
            symbol="BTCUSDT",
            exchange="BINANCE",
            datetime=datetime.now() - timedelta(milliseconds=250),
        )
        stale_eth = OrderBook(
            symbol="ETHUSDT",
            exchange="BINANCE",
            datetime=datetime.now() - timedelta(milliseconds=250),
        )

        risk.on_orderbook(Event("eOrderBook", stale_btc))
        risk.on_orderbook(Event("eOrderBook", stale_btc))
        self.assertFalse(risk.kill_switch_triggered)

        risk.on_orderbook(Event("eOrderBook", stale_eth))
        risk.on_orderbook(Event("eOrderBook", stale_eth))

        self.assertTrue(risk.kill_switch_triggered)
        self.assertTrue(oms.halt_reasons)

    def test_symbol_freeze_clears_after_stable_market_updates(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        oms = DummyOMS()
        risk = RiskManager(engine, self.make_risk_config(), oms=oms, gateway=gateway)

        stale_book = OrderBook(
            symbol="BTCUSDT",
            exchange="BINANCE",
            datetime=datetime.now() - timedelta(milliseconds=250),
        )
        fresh_book = OrderBook(
            symbol="BTCUSDT",
            exchange="BINANCE",
            datetime=datetime.now(),
            exchange_timestamp=datetime.now().timestamp(),
        )

        risk.on_orderbook(Event("eOrderBook", stale_book))
        risk.on_orderbook(Event("eOrderBook", stale_book))
        self.assertTrue(oms.frozen_symbols)

        risk.on_orderbook(Event("eOrderBook", fresh_book))
        risk.on_orderbook(Event("eOrderBook", fresh_book))

        self.assertTrue(oms.unfrozen_symbols)

    def test_processing_lag_freezes_venue_instead_of_symbol(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        oms = DummyOMS()
        risk = RiskManager(engine, self.make_risk_config(), oms=oms, gateway=gateway)

        delayed_book = OrderBook(
            symbol="BTCUSDT",
            exchange="BINANCE",
            datetime=datetime.now(),
            exchange_timestamp=(datetime.now() - timedelta(milliseconds=50)).timestamp(),
            received_timestamp=(datetime.now() - timedelta(milliseconds=250)).timestamp(),
        )

        risk.on_orderbook(Event("eOrderBook", delayed_book))
        risk.on_orderbook(Event("eOrderBook", delayed_book))

        self.assertFalse(oms.frozen_symbols)
        self.assertTrue(oms.frozen_venues)
        self.assertIn("processing_lag:", oms.frozen_venues[-1][1])

    def test_processing_lag_venue_freeze_clears_after_stable_updates(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        oms = DummyOMS()
        risk = RiskManager(engine, self.make_risk_config(), oms=oms, gateway=gateway)

        delayed_book = OrderBook(
            symbol="BTCUSDT",
            exchange="BINANCE",
            datetime=datetime.now(),
            exchange_timestamp=(datetime.now() - timedelta(milliseconds=50)).timestamp(),
            received_timestamp=(datetime.now() - timedelta(milliseconds=250)).timestamp(),
        )
        fresh_book = OrderBook(
            symbol="BTCUSDT",
            exchange="BINANCE",
            datetime=datetime.now(),
            exchange_timestamp=(datetime.now() - timedelta(milliseconds=20)).timestamp(),
            received_timestamp=datetime.now().timestamp(),
        )

        risk.on_orderbook(Event("eOrderBook", delayed_book))
        risk.on_orderbook(Event("eOrderBook", delayed_book))
        self.assertTrue(oms.frozen_venues)

        risk.on_orderbook(Event("eOrderBook", fresh_book))
        risk.on_orderbook(Event("eOrderBook", fresh_book))

        self.assertTrue(oms.unfrozen_venues)

    def test_processing_lag_degrades_before_venue_freeze(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        oms = DummyOMS()
        config = self.make_risk_config()
        config["risk"]["tech_health"]["consecutive_error_limit"] = 3
        risk = RiskManager(engine, config, oms=oms, gateway=gateway)

        delayed_book = OrderBook(
            symbol="BTCUSDT",
            exchange="BINANCE",
            datetime=datetime.now(),
            exchange_timestamp=(datetime.now() - timedelta(milliseconds=50)).timestamp(),
            received_timestamp=(datetime.now() - timedelta(milliseconds=250)).timestamp(),
        )

        risk.on_orderbook(Event("eOrderBook", delayed_book))
        risk.on_orderbook(Event("eOrderBook", delayed_book))

        self.assertTrue(any(mode == OMSCapabilityMode.DEGRADED for mode, _reason in oms.trading_modes))
        self.assertTrue(any(mode == OMSCapabilityMode.PASSIVE_ONLY for mode, _reason in oms.trading_modes))
        self.assertFalse(oms.frozen_venues)

        risk.on_orderbook(Event("eOrderBook", delayed_book))
        self.assertTrue(oms.frozen_venues)

    def test_kill_switch_requests_emergency_flatten(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        oms = DummyOMS()
        oms.exposure.net_positions = {"BTCUSDT": 1.0}
        risk = RiskManager(engine, self.make_risk_config(), oms=oms, gateway=gateway)

        risk.trigger_kill_switch("test_kill")

        self.assertTrue(oms.halt_reasons)
        self.assertEqual(oms.flatten_reasons, ["KillSwitch: test_kill"])

if __name__ == "__main__":
    unittest.main()
