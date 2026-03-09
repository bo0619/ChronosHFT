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
    MarkPriceData,
    OrderBook,
    OrderIntent,
    Side,
    EVENT_ACCOUNT_UPDATE,
    EVENT_EXCHANGE_ACCOUNT_UPDATE,
    EVENT_EXCHANGE_ORDER_UPDATE,
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

    def halt_system(self, reason):
        self.halt_reasons.append(reason)


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
                positions={},
                reason="ORDER",
                event_time=1.0,
            )
            oms.on_exchange_account_update(Event(EVENT_EXCHANGE_ACCOUNT_UPDATE, update))

            self.assertAlmostEqual(oms.account.balance, 950.0)
            self.assertAlmostEqual(oms.account.available, 900.0)
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
                    "B": [{"a": "USDT", "wb": "980.5", "cw": "950.0"}],
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
        self.assertIn("BTCUSDT", account_event.data.positions)

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
                    "max_order_count_per_sec": 20,
                    "consecutive_error_limit": 2,
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

        self.assertTrue(risk.kill_switch_triggered)
        self.assertTrue(oms.halt_reasons)

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

        self.assertTrue(risk.kill_switch_triggered)
        self.assertIn("divergence", risk.kill_reason)


if __name__ == "__main__":
    unittest.main()
