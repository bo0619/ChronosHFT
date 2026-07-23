import json
import multiprocessing
import os
import queue
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

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
    CommandOutcome,
    Event,
    ExchangeAccountUpdate,
    ExchangeOrderUpdate,
    ExecutionPolicy,
    GatewayState,
    LifecycleState,
    MarkPriceData,
    OMSCapabilityMode,
    OrderBook,
    OrderIntent,
    OrderRequest,
    Side,
    EVENT_ACCOUNT_UPDATE,
    EVENT_EXCHANGE_ACCOUNT_UPDATE,
    EVENT_EXCHANGE_ORDER_UPDATE,
    EVENT_SYSTEM_HEALTH,
)
from gateway.binance.gateway import BinanceGateway
from infrastructure.watchdog import emit_event_engine_backlog_if_needed
from oms.engine import OMS
from oms.journal import OMSJournal
from oms.order import Order
from risk.independent_supervisor import (
    BinanceRiskSidecarExchange,
    IndependentRiskSupervisor,
    RiskSidecarCore,
    run_sidecar_loop,
)
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
        return DummyResponse({}, status_code=200)

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
        self.symbol_clear_attempts = []
        self._symbol_freeze_reasons = {}
        self._symbol_freeze_epochs = {}
        self.frozen_venues = []
        self.unfrozen_venues = []
        self.venue_clear_attempts = []
        self._venue_freeze_reasons = {}
        self._venue_freeze_epochs = {}
        self.trading_modes = []
        self.cleared_trading_modes = []
        self.flatten_reasons = []
        self.risk_heartbeats = []
        self.dead_man_renewals = 0
        self.mode_override = None
        self.mode_override_reason = ""

    def halt_system(self, reason):
        self.halt_reasons.append(reason)

    def freeze_symbol(self, symbol, reason, cancel_active_orders=True):
        symbol = symbol.upper()
        epoch = self._symbol_freeze_epochs.get(symbol, 0) + 1
        self._symbol_freeze_epochs[symbol] = epoch
        self._symbol_freeze_reasons[symbol] = reason
        self.frozen_symbols.append((symbol, reason, cancel_active_orders))
        return epoch

    def clear_symbol_freeze(
        self,
        symbol,
        reason="",
        expected_epoch=None,
        expected_reason=None,
    ):
        symbol = symbol.upper()
        current_epoch = self._symbol_freeze_epochs.get(symbol, 0)
        current_reason = self._symbol_freeze_reasons.get(symbol, "")
        self.symbol_clear_attempts.append(
            (symbol, reason, expected_epoch, expected_reason)
        )
        if expected_epoch is not None and expected_epoch != current_epoch:
            return False
        if expected_reason is not None and expected_reason != current_reason:
            return False
        if not self._symbol_freeze_reasons.pop(symbol, ""):
            return False
        self.unfrozen_symbols.append((symbol, reason))
        return True

    def get_symbol_freeze_reason(self, symbol):
        return self._symbol_freeze_reasons.get(symbol.upper(), "")

    def get_symbol_freeze_epoch(self, symbol):
        return self._symbol_freeze_epochs.get(symbol.upper(), 0)

    def freeze_venue(self, venue, reason, cancel_active_orders=True):
        venue = venue.upper()
        epoch = self._venue_freeze_epochs.get(venue, 0) + 1
        self._venue_freeze_epochs[venue] = epoch
        self._venue_freeze_reasons[venue] = reason
        self.frozen_venues.append((venue, reason, cancel_active_orders))
        return epoch

    def clear_venue_freeze(
        self,
        venue,
        reason="",
        expected_epoch=None,
        expected_reason=None,
    ):
        venue = venue.upper()
        current_epoch = self._venue_freeze_epochs.get(venue, 0)
        current_reason = self._venue_freeze_reasons.get(venue, "")
        self.venue_clear_attempts.append(
            (venue, reason, expected_epoch, expected_reason)
        )
        if expected_epoch is not None and expected_epoch != current_epoch:
            return False
        if expected_reason is not None and expected_reason != current_reason:
            return False
        if not self._venue_freeze_reasons.pop(venue, ""):
            return False
        self.unfrozen_venues.append((venue, reason))
        return True

    def get_venue_freeze_reason(self, venue):
        return self._venue_freeze_reasons.get(venue.upper(), "")

    def get_venue_freeze_epoch(self, venue):
        return self._venue_freeze_epochs.get(venue.upper(), 0)

    def set_trading_mode(self, mode, reason):
        self.trading_modes.append((mode, reason))
        self.mode_override = mode
        self.mode_override_reason = reason

    def clear_trading_mode(self, reason="", prefixes=()):
        if prefixes and not any(self.mode_override_reason.startswith(prefix) for prefix in prefixes):
            return False
        self.cleared_trading_modes.append((reason, tuple(prefixes or ())))
        self.mode_override = None
        self.mode_override_reason = ""
        return True

    def has_trading_mode_constraint(self, prefixes=()):
        if self.mode_override is None:
            return False
        if not prefixes:
            return True
        return any(
            self.mode_override_reason.startswith(prefix) for prefix in prefixes
        )

    def emergency_reduce_only_flatten(self, reason):
        self.flatten_reasons.append(reason)
        return 0

    def record_risk_control_heartbeat(
        self,
        source="risk_manager",
        healthy=True,
        reason="",
    ):
        self.risk_heartbeats.append((source, healthy, reason))
        return healthy

    def renew_venue_dead_man_switch(self):
        self.dead_man_renewals += 1
        return True


class DummySidecarExchange:
    def __init__(self, healthy=True, reason="", risk_snapshot=None):
        self.healthy = healthy
        self.reason = reason
        self.risk_snapshot = risk_snapshot or {
            "account": {
                "totalMaintMargin": "0",
                "totalMarginBalance": "1000",
            },
            "positions": [],
            "open_orders": [],
        }
        self.health_checks = 0
        self.cancel_calls = []
        self.flatten_calls = 0
        self.closed = False

    def check_account_channel(self):
        self.health_checks += 1
        return self.healthy, self.reason

    def get_risk_snapshot(self):
        self.health_checks += 1
        return self.healthy, self.risk_snapshot, self.reason

    def emergency_cancel(self, symbols, countdown_time_ms):
        self.cancel_calls.append((tuple(symbols), countdown_time_ms))
        self.risk_snapshot["open_orders"] = []
        return True, ""

    def emergency_flatten(self):
        self.flatten_calls += 1
        submitted = sum(
            1
            for position in self.risk_snapshot.get("positions", [])
            if abs(float(position.get("positionAmt", 0.0) or 0.0)) > 1e-9
        )
        self.risk_snapshot["positions"] = []
        return True, submitted, ""

    def close(self):
        self.closed = True


class DummyResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class DummyRiskSidecarRest:
    def __init__(
        self,
        account=None,
        positions=None,
        open_orders=None,
        income_rows=None,
        server_time_ms=None,
        server_time_status=200,
    ):
        self.account = account or {
            "totalMaintMargin": "0",
            "totalMarginBalance": "1000",
        }
        self.positions = list(positions or [])
        self.open_orders = list(open_orders or [])
        self.income_rows = list(income_rows or [])
        self.server_time_ms = int(server_time_ms or time.time() * 1000)
        self.server_time_status = server_time_status
        self.new_orders = []

    def get_account(self):
        return DummyResponse(self.account)

    def get_positions(self):
        return DummyResponse(self.positions)

    def get_open_orders(self):
        return DummyResponse(self.open_orders)

    def get_income_history(self, **kwargs):
        return DummyResponse(self.income_rows)

    def get_server_time(self):
        return DummyResponse(
            {"serverTime": self.server_time_ms},
            status_code=self.server_time_status,
        )

    def new_order(self, request, client_oid):
        self.new_orders.append((request, client_oid))
        return DummyResponse({"orderId": len(self.new_orders)})


class DummyRiskManager:
    def __init__(self):
        self.kill_reasons = []

    def trigger_kill_switch(self, reason):
        self.kill_reasons.append(reason)


class ThreadProcessHandle:
    def __init__(self, worker):
        self.worker = worker
        self.pid = None

    def is_alive(self):
        return self.worker.is_alive()

    def join(self, timeout=None):
        self.worker.join(timeout)

    def terminate(self):
        return None


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
                trade_id=1,
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
                    "t": 987,
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
        self.assertEqual(order_event.data.trade_id, 987)
        self.assertEqual(order_event.data.seq, 0)
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
        gateway._book_lock = threading.RLock()
        gateway._book_generation = 0
        gateway.active = True
        gateway.ws = None

        gateway.on_ws_message("{bad-json")

        self.assertEqual(engine.events[-1].type, EVENT_SYSTEM_HEALTH)
        self.assertIn("WS_PARSE_ERROR", engine.events[-1].data)

    def test_gateway_classifies_missing_submit_response_as_unknown(self):
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.gateway_name = "BINANCE"
        gateway.require_healthy_clock = False
        gateway._book_lock = threading.RLock()
        gateway.active = True
        gateway.state = GatewayState.READY
        gateway.book_resyncing = set()
        gateway.ws_buffer = {"BTCUSDT": None}
        gateway.rest = types.SimpleNamespace(new_order=lambda _req, _client_oid: None)
        request = OrderRequest(
            symbol="BTCUSDT",
            price=100.0,
            volume=1.0,
            side="BUY",
        )

        result = gateway.send_order(request, "oid-timeout")

        self.assertEqual(result.outcome, CommandOutcome.UNKNOWN)
        self.assertEqual(result.exchange_oid, "")

    def test_gateway_classifies_binance_unknown_execution_as_unknown(self):
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.gateway_name = "BINANCE"
        gateway.require_healthy_clock = False
        gateway._book_lock = threading.RLock()
        gateway.active = True
        gateway.state = GatewayState.READY
        gateway.book_resyncing = set()
        gateway.ws_buffer = {"BTCUSDT": None}
        gateway.rest = types.SimpleNamespace(
            new_order=lambda _req, _client_oid: DummyResponse(
                {"code": -1006, "msg": "Unexpected response from message bus"},
                status_code=400,
            )
        )
        request = OrderRequest(
            symbol="BTCUSDT",
            price=100.0,
            volume=1.0,
            side="BUY",
        )

        result = gateway.send_order(request, "oid-unknown-execution")

        self.assertEqual(result.outcome, CommandOutcome.UNKNOWN)
        self.assertEqual(result.error_code, "-1006")

    @patch(
        "gateway.binance.gateway.time_service.health_snapshot",
        return_value={
            "ready": False,
            "state": "degraded",
            "reason": "clock uncertainty",
        },
    )
    def test_gateway_clock_gate_is_final_and_allows_reduce_only(
        self,
        _clock_health,
    ):
        calls = []
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.gateway_name = "BINANCE"
        gateway.require_healthy_clock = True
        gateway._book_lock = threading.RLock()
        gateway.active = True
        gateway.state = GatewayState.READY
        gateway.book_resyncing = set()
        gateway.ws_buffer = {"BTCUSDT": None}
        gateway.rest = types.SimpleNamespace(
            new_order=lambda req, client_oid: calls.append((req, client_oid))
        )
        opening = OrderRequest(
            symbol="BTCUSDT",
            price=100.0,
            volume=1.0,
            side="BUY",
        )
        reducing = OrderRequest(
            symbol="BTCUSDT",
            price=100.0,
            volume=1.0,
            side="SELL",
            reduce_only=True,
        )

        rejected = gateway.send_order(opening, "clock-rejected")
        reduced = gateway.send_order(reducing, "clock-reduce")

        self.assertEqual(rejected.outcome, CommandOutcome.REJECTED)
        self.assertEqual(rejected.error_code, "CLOCK_UNHEALTHY")
        self.assertEqual(reduced.outcome, CommandOutcome.UNKNOWN)
        self.assertEqual(len(calls), 1)

    def test_gateway_keeps_position_only_account_update(self):
        engine = DummyEngine()
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.event_engine = engine
        gateway.gateway_name = "BINANCE"
        gateway.symbols = ["BTCUSDT"]

        gateway._handle_account_update(
            {
                "e": "ACCOUNT_UPDATE",
                "T": 3000,
                "a": {
                    "m": "MARGIN_TYPE_CHANGE",
                    "B": [],
                    "P": [
                        {
                            "s": "BTCUSDT",
                            "pa": "0.25",
                            "ep": "102.0",
                            "up": "0.5",
                        }
                    ],
                },
            }
        )

        self.assertEqual(len(engine.events), 1)
        account_update = engine.events[0].data
        self.assertEqual(account_update.asset, "")
        self.assertEqual(account_update.balances, {})
        self.assertEqual(account_update.event_time, 3.0)
        self.assertEqual(account_update.positions["BTCUSDT"]["volume"], 0.25)

    def test_position_only_account_update_does_not_overwrite_balance(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        oms = OMS(engine, gateway, self.make_config())
        try:
            oms.account.force_sync(1000.0, 0.0, 900.0)
            active_order = Order(
                "oid-position-only",
                OrderIntent("test", "BTCUSDT", Side.BUY, 102.0, 0.25),
            )
            active_order.mark_submitting()
            oms.orders[active_order.client_oid] = active_order

            update = ExchangeAccountUpdate(
                asset="",
                wallet_balance=0.0,
                available_balance=None,
                balances={},
                positions={
                    "BTCUSDT": {
                        "volume": 0.25,
                        "entry_price": 102.0,
                        "unrealized_pnl": 0.0,
                    }
                },
                reason="MARGIN_TYPE_CHANGE",
                event_time=3.0,
            )
            oms.on_exchange_account_update(Event(EVENT_EXCHANGE_ACCOUNT_UPDATE, update))

            self.assertAlmostEqual(oms.exposure.net_positions["BTCUSDT"], 0.25)
            self.assertAlmostEqual(oms.account.balance, 1000.0)
        finally:
            oms.stop()

    def test_transfer_account_update_invalidates_cash_flow_truth_immediately(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        config = self.make_config()
        config["risk"]["cash_flow_truth"] = {
            "enabled": True,
            "require_snapshot": True,
        }
        oms = OMS(engine, gateway, config)
        try:
            self.assertTrue(
                oms.account.sync_external_cash_flow_truth(
                    0.0,
                    snapshot_time=time.time(),
                )
            )
            self.assertTrue(oms.account.cash_flow_snapshot_synced)

            update = ExchangeAccountUpdate(
                asset="USDT",
                wallet_balance=1100.0,
                available_balance=1100.0,
                balances={
                    "USDT": {
                        "wallet_balance": 1100.0,
                        "available_balance": 1100.0,
                    }
                },
                positions={},
                reason="TRANSFER",
                event_time=time.time(),
            )
            oms.on_exchange_account_update(
                Event(EVENT_EXCHANGE_ACCOUNT_UPDATE, update)
            )

            self.assertFalse(oms.account.cash_flow_snapshot_synced)
        finally:
            oms.stop()

    def test_partial_exchange_account_update_does_not_flatten_absent_position(self):
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

            self.assertEqual(called, [])
            self.assertAlmostEqual(oms.exposure.net_positions["BTCUSDT"], 1.0)
        finally:
            oms.stop()

    def test_exchange_account_position_sync_with_active_order_is_idempotent(self):
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
                positions={
                    "BTCUSDT": {
                        "volume": 2.0,
                        "entry_price": 100.0,
                        "unrealized_pnl": 0.0,
                    }
                },
                reason="ORDER",
                event_time=2.0,
            )
            oms.on_exchange_account_update(Event(EVENT_EXCHANGE_ACCOUNT_UPDATE, update))

            self.assertEqual(called, [])
            self.assertAlmostEqual(oms.exposure.net_positions["BTCUSDT"], 2.0)
            self.assertTrue(
                any(
                    event.type == "ePositionUpdate"
                    and event.data.symbol == "BTCUSDT"
                    and event.data.volume == 2.0
                    for event in engine.events
                )
            )
        finally:
            oms.stop()

    def test_unexpected_exchange_position_is_synced_before_reconcile(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        oms = OMS(engine, gateway, self.make_config())
        try:
            called = []
            oms.trigger_reconcile = lambda reason, suspicious_oid=None: called.append((reason, suspicious_oid))
            update = ExchangeAccountUpdate(
                asset="USDT",
                wallet_balance=1000.0,
                available_balance=900.0,
                balances={"USDT": {"wallet_balance": 1000.0, "available_balance": 900.0}},
                positions={
                    "BTCUSDT": {
                        "volume": 1.0,
                        "entry_price": 101.0,
                        "unrealized_pnl": 0.0,
                    }
                },
                reason="ORDER",
                event_time=2.0,
            )

            oms.on_exchange_account_update(Event(EVENT_EXCHANGE_ACCOUNT_UPDATE, update))

            self.assertAlmostEqual(oms.exposure.net_positions["BTCUSDT"], 1.0)
            self.assertAlmostEqual(oms.exposure.avg_prices["BTCUSDT"], 101.0)
            self.assertEqual(
                called,
                [("Unexpected exchange account position correction", None)],
            )
        finally:
            oms.stop()

    def test_account_update_before_fill_does_not_double_apply_position_or_balance(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        oms = OMS(engine, gateway, self.make_config())
        try:
            intent = OrderIntent("test", "BTCUSDT", Side.BUY, 100.0, 1.0)
            order = Order("oid-buy", intent)
            order.mark_submitting()
            order.mark_pending_ack("ex-buy")
            order.mark_new("ex-buy", update_time=1.0, seq=1)
            oms.orders[order.client_oid] = order
            oms.exchange_id_map[order.exchange_oid] = order

            account_update = ExchangeAccountUpdate(
                asset="USDT",
                wallet_balance=999.5,
                available_balance=899.5,
                balances={"USDT": {"wallet_balance": 999.5, "available_balance": 899.5}},
                positions={
                    "BTCUSDT": {
                        "volume": 1.0,
                        "entry_price": 100.0,
                        "unrealized_pnl": 0.0,
                    }
                },
                reason="ORDER",
                event_time=2.0,
            )
            oms.on_exchange_account_update(Event(EVENT_EXCHANGE_ACCOUNT_UPDATE, account_update))

            fill_update = ExchangeOrderUpdate(
                client_oid="oid-buy",
                exchange_oid="ex-buy",
                symbol="BTCUSDT",
                status="FILLED",
                filled_qty=1.0,
                filled_price=100.0,
                cum_filled_qty=1.0,
                update_time=2.0,
                seq=2,
                commission=0.5,
                commission_asset="USDT",
                realized_pnl=0.0,
                is_maker=True,
                trade_id=1,
            )
            oms._apply_event(Event(EVENT_EXCHANGE_ORDER_UPDATE, fill_update))

            self.assertAlmostEqual(oms.exposure.net_positions["BTCUSDT"], 1.0)
            self.assertAlmostEqual(oms.account.balance, 999.5)
            self.assertAlmostEqual(order.filled_volume, 1.0)
        finally:
            oms.stop()

    def test_stale_account_position_cannot_overwrite_newer_fill(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        oms = OMS(engine, gateway, self.make_config())
        try:
            intent = OrderIntent("test", "BTCUSDT", Side.BUY, 100.0, 1.0)
            order = Order("oid-newer", intent)
            order.mark_submitting()
            order.mark_pending_ack("ex-newer")
            order.mark_new("ex-newer", update_time=1.0, seq=1)
            oms.orders[order.client_oid] = order
            oms.exchange_id_map[order.exchange_oid] = order

            fill_update = ExchangeOrderUpdate(
                client_oid="oid-newer",
                exchange_oid="ex-newer",
                symbol="BTCUSDT",
                status="FILLED",
                filled_qty=1.0,
                filled_price=100.0,
                cum_filled_qty=1.0,
                update_time=3.0,
                seq=2,
                commission=0.0,
                realized_pnl=0.0,
                trade_id=1,
            )
            oms._apply_event(Event(EVENT_EXCHANGE_ORDER_UPDATE, fill_update))

            stale_account_update = ExchangeAccountUpdate(
                asset="USDT",
                wallet_balance=1000.0,
                available_balance=1000.0,
                balances={"USDT": {"wallet_balance": 1000.0, "available_balance": 1000.0}},
                positions={
                    "BTCUSDT": {
                        "volume": 0.0,
                        "entry_price": 0.0,
                        "unrealized_pnl": 0.0,
                    }
                },
                reason="ORDER",
                event_time=2.0,
            )
            oms.on_exchange_account_update(Event(EVENT_EXCHANGE_ACCOUNT_UPDATE, stale_account_update))

            self.assertAlmostEqual(oms.exposure.net_positions["BTCUSDT"], 1.0)
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

    def test_daily_loss_adjusts_for_external_cash_flow(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        oms = DummyOMS()
        config = self.make_risk_config()
        config["risk"]["limits"]["max_daily_loss"] = 50.0
        config["risk"]["cash_flow_truth"] = {
            "enabled": True,
            "require_snapshot": True,
            "max_snapshot_age_sec": 60.0,
            "recovery_checks": 1,
        }
        risk = RiskManager(engine, config, oms=oms, gateway=gateway)

        def account(equity, external_flow):
            return AccountData(
                balance=equity,
                equity=equity,
                available=equity,
                used_margin=0.0,
                datetime=datetime.now(),
                external_cash_flow_total=external_flow,
                cash_flow_snapshot_time=time.time(),
                cash_flow_snapshot_synced=True,
            )

        risk.on_account_update(Event(EVENT_ACCOUNT_UPDATE, account(1000.0, 0.0)))
        risk.on_account_update(Event(EVENT_ACCOUNT_UPDATE, account(1100.0, 100.0)))
        self.assertFalse(risk.kill_switch_triggered)

        risk.on_account_update(Event(EVENT_ACCOUNT_UPDATE, account(1040.0, 100.0)))
        self.assertTrue(risk.kill_switch_triggered)
        self.assertIn("Daily loss", risk.kill_reason)

    def test_missing_cash_flow_truth_enters_reduce_only_until_recovered(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        oms = DummyOMS()
        config = self.make_risk_config()
        config["risk"]["cash_flow_truth"] = {
            "enabled": True,
            "require_snapshot": True,
            "max_snapshot_age_sec": 60.0,
            "recovery_checks": 2,
        }
        risk = RiskManager(engine, config, oms=oms, gateway=gateway)

        missing = AccountData(1000.0, 1000.0, 1000.0, 0.0, datetime.now())
        risk.on_account_update(Event(EVENT_ACCOUNT_UPDATE, missing))
        self.assertEqual(oms.trading_modes[-1][0], OMSCapabilityMode.REDUCE_ONLY)
        self.assertTrue(oms.trading_modes[-1][1].startswith("daily_pnl_truth:"))

        healthy = AccountData(
            1000.0,
            1000.0,
            1000.0,
            0.0,
            datetime.now(),
            cash_flow_snapshot_time=time.time(),
            cash_flow_snapshot_synced=True,
        )
        risk.on_account_update(Event(EVENT_ACCOUNT_UPDATE, healthy))
        self.assertFalse(oms.cleared_trading_modes)
        risk.on_account_update(Event(EVENT_ACCOUNT_UPDATE, healthy))
        self.assertTrue(oms.cleared_trading_modes)

    def test_live_risk_cycle_renews_oms_heartbeat_lease(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        oms = DummyOMS()
        config = self.make_risk_config()
        config["risk"]["market_data_freshness"] = {"enabled": False}
        risk = RiskManager(engine, config, oms=oms, gateway=gateway)
        oms.risk_heartbeats.clear()

        self.assertTrue(risk.check_market_data_freshness())

        self.assertEqual(
            oms.risk_heartbeats,
            [("risk_manager", True, "risk_live_loop")],
        )
        self.assertEqual(oms.dead_man_renewals, 1)

    def test_risk_status_snapshot_exposes_cash_flow_adjusted_pnl_and_margin(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        oms = DummyOMS()
        oms.account = types.SimpleNamespace(
            equity=980.0,
            external_cash_flow_total=105.0,
            maintenance_margin_ratio=0.12,
            margin_snapshot_synced=True,
            margin_snapshot_time=time.time(),
            cash_flow_snapshot_synced=True,
            cash_flow_snapshot_time=time.time(),
        )
        risk = RiskManager(
            engine,
            self.make_risk_config(),
            oms=oms,
            gateway=gateway,
        )
        risk.risk_day = "2026-07-20"
        risk.initial_equity = 1000.0
        risk.initial_external_cash_flow_total = 100.0
        risk.peak_equity = 1010.0

        snapshot = risk.get_status_snapshot()

        self.assertEqual(snapshot["risk_day"], "2026-07-20")
        self.assertEqual(snapshot["cash_flow_adjusted_equity"], 975.0)
        self.assertEqual(snapshot["cash_flow_adjusted_daily_pnl"], -25.0)
        self.assertAlmostEqual(snapshot["peak_drawdown_pct"], 35.0 / 1010.0)
        self.assertEqual(snapshot["maintenance_margin_ratio"], 0.12)
        self.assertTrue(snapshot["margin_snapshot_synced"])

    def test_margin_health_degrades_then_enters_reduce_only_and_recovers(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        oms = DummyOMS()
        config = self.make_risk_config()
        config["risk"]["margin_health"] = {
            "enabled": True,
            "degraded_ratio": 0.50,
            "reduce_only_ratio": 0.70,
            "kill_ratio": 0.90,
            "recovery_ratio": 0.40,
            "max_snapshot_age_sec": 15.0,
            "recovery_checks": 2,
        }
        risk = RiskManager(engine, config, oms=oms, gateway=gateway)

        def account_at(ratio):
            return AccountData(
                balance=1000.0,
                equity=1000.0,
                available=500.0,
                used_margin=500.0,
                datetime=datetime.now(),
                maintenance_margin=ratio * 1000.0,
                margin_balance=1000.0,
                maintenance_margin_ratio=ratio,
                margin_snapshot_time=time.time(),
                margin_snapshot_synced=True,
            )

        risk.on_account_update(Event(EVENT_ACCOUNT_UPDATE, account_at(0.55)))
        self.assertEqual(oms.trading_modes[-1][0], OMSCapabilityMode.DEGRADED)

        risk.on_account_update(Event(EVENT_ACCOUNT_UPDATE, account_at(0.75)))
        self.assertEqual(oms.trading_modes[-1][0], OMSCapabilityMode.REDUCE_ONLY)

        risk.on_account_update(Event(EVENT_ACCOUNT_UPDATE, account_at(0.30)))
        self.assertEqual(oms.cleared_trading_modes, [])
        risk.on_account_update(Event(EVENT_ACCOUNT_UPDATE, account_at(0.30)))
        self.assertTrue(oms.cleared_trading_modes)

    def test_stale_margin_snapshot_enters_reduce_only(self):
        oms = DummyOMS()
        risk = RiskManager(
            DummyEngine(),
            self.make_risk_config(),
            oms=oms,
            gateway=DummyGateway(),
        )
        account = AccountData(
            balance=1000.0,
            equity=1000.0,
            available=900.0,
            used_margin=100.0,
            datetime=datetime.now(),
            maintenance_margin=100.0,
            margin_balance=1000.0,
            maintenance_margin_ratio=0.10,
            margin_snapshot_time=time.time() - 60.0,
            margin_snapshot_synced=True,
        )

        risk.on_account_update(Event(EVENT_ACCOUNT_UPDATE, account))

        self.assertEqual(oms.trading_modes[-1][0], OMSCapabilityMode.REDUCE_ONLY)
        self.assertTrue(oms.trading_modes[-1][1].startswith("margin_health:stale_snapshot:"))

    def test_margin_recovery_clears_only_its_composable_constraint(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        config = {
            "symbols": ["BTCUSDT"],
            "account": {"initial_balance_usdt": 1000.0, "leverage": 10},
            "backtest": {"taker_fee": 0.0, "maker_fee": 0.0},
            "oms": {"journal_enabled": False, "replay_journal_on_startup": False},
            "risk": self.make_risk_config()["risk"],
        }
        config["risk"]["margin_health"] = {
            "enabled": True,
            "degraded_ratio": 0.50,
            "reduce_only_ratio": 0.70,
            "kill_ratio": 0.90,
            "recovery_ratio": 0.40,
            "recovery_checks": 2,
        }
        oms = OMS(engine, gateway, config)
        try:
            oms.state = LifecycleState.LIVE
            oms.set_trading_mode(OMSCapabilityMode.DEGRADED, "margin_health:test")
            oms.set_trading_mode(OMSCapabilityMode.PASSIVE_ONLY, "processing_lag:test")
            risk = RiskManager(engine, config, oms=oms, gateway=gateway)
            healthy = AccountData(
                balance=1000.0,
                equity=1000.0,
                available=900.0,
                used_margin=100.0,
                datetime=datetime.now(),
                maintenance_margin=300.0,
                margin_balance=1000.0,
                maintenance_margin_ratio=0.30,
                margin_snapshot_time=time.time(),
                margin_snapshot_synced=True,
            )

            risk.on_account_update(Event(EVENT_ACCOUNT_UPDATE, healthy))
            risk.on_account_update(Event(EVENT_ACCOUNT_UPDATE, healthy))

            self.assertFalse(oms.has_trading_mode_constraint(("margin_health:",)))
            self.assertTrue(oms.has_trading_mode_constraint(("processing_lag:",)))
            self.assertEqual(oms.capability_mode, OMSCapabilityMode.PASSIVE_ONLY)
        finally:
            oms.stop()

    def test_critical_maintenance_margin_ratio_triggers_kill_switch(self):
        oms = DummyOMS()
        risk = RiskManager(
            DummyEngine(),
            self.make_risk_config(),
            oms=oms,
            gateway=DummyGateway(),
        )
        account = AccountData(
            balance=1000.0,
            equity=1000.0,
            available=100.0,
            used_margin=900.0,
            datetime=datetime.now(),
            maintenance_margin=950.0,
            margin_balance=1000.0,
            maintenance_margin_ratio=0.95,
            margin_snapshot_time=time.time(),
            margin_snapshot_synced=True,
        )

        risk.on_account_update(Event(EVENT_ACCOUNT_UPDATE, account))

        self.assertTrue(risk.kill_switch_triggered)
        self.assertIn("Maintenance margin ratio", risk.kill_reason)

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

    def test_venue_latency_applies_exchange_clock_offset_and_preserves_negative_skew(self):
        from infrastructure.time_service import time_service

        original_offset = time_service.offset
        received_timestamp = time.time()
        try:
            time_service.offset = 250.0
            corrected_oms = DummyOMS()
            corrected_risk = RiskManager(
                DummyEngine(),
                self.make_risk_config(),
                oms=corrected_oms,
                gateway=DummyGateway(),
            )
            corrected_book = OrderBook(
                symbol="BTCUSDT",
                exchange="BINANCE",
                datetime=datetime.fromtimestamp(received_timestamp),
                exchange_timestamp=received_timestamp + 0.25,
                received_timestamp=received_timestamp,
                received_monotonic=time.perf_counter(),
            )

            corrected_risk.on_orderbook(Event("eOrderBook", corrected_book))
            corrected_risk.on_orderbook(Event("eOrderBook", corrected_book))

            self.assertFalse(corrected_oms.frozen_symbols)
            self.assertAlmostEqual(corrected_risk.last_market_latency_ms, 0.0, places=3)

            time_service.offset = 0.0
            skewed_oms = DummyOMS()
            skewed_risk = RiskManager(
                DummyEngine(),
                self.make_risk_config(),
                oms=skewed_oms,
                gateway=DummyGateway(),
            )
            skewed_risk.on_orderbook(Event("eOrderBook", corrected_book))
            skewed_risk.on_orderbook(Event("eOrderBook", corrected_book))

            self.assertLess(skewed_risk.last_market_latency_ms, -200.0)
            self.assertTrue(skewed_oms.frozen_symbols)
            self.assertIn("latency:-", skewed_oms.frozen_symbols[-1][1])
        finally:
            time_service.offset = original_offset

    def test_processing_lag_prefers_monotonic_ingress_over_stale_wall_clock(self):
        from infrastructure.time_service import time_service

        original_offset = time_service.offset
        try:
            time_service.offset = 0.0
            oms = DummyOMS()
            risk = RiskManager(
                DummyEngine(),
                self.make_risk_config(),
                oms=oms,
                gateway=DummyGateway(),
            )
            stale_wall = time.time() - 30.0
            book = OrderBook(
                symbol="BTCUSDT",
                exchange="BINANCE",
                datetime=datetime.fromtimestamp(stale_wall),
                exchange_timestamp=stale_wall,
                received_timestamp=stale_wall,
                received_monotonic=time.perf_counter(),
            )

            risk.on_orderbook(Event("eOrderBook", book))
            risk.on_orderbook(Event("eOrderBook", book))

            self.assertFalse(oms.frozen_venues)
            self.assertLess(risk.last_processing_lag_ms, 100.0)
        finally:
            time_service.offset = original_offset

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

    def test_stale_risk_symbol_recovery_cannot_clear_newer_guard(self):
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
        owned_reason = risk.frozen_symbols["BTCUSDT"]
        owned_epoch = risk.symbol_freeze_epochs["BTCUSDT"]
        newer_epoch = oms.freeze_symbol(
            "BTCUSDT",
            "truth_plane:newer",
            cancel_active_orders=False,
        )

        risk.on_orderbook(Event("eOrderBook", fresh_book))
        risk.on_orderbook(Event("eOrderBook", fresh_book))

        self.assertGreater(newer_epoch, owned_epoch)
        self.assertEqual(oms.get_symbol_freeze_reason("BTCUSDT"), "truth_plane:newer")
        self.assertNotIn("BTCUSDT", risk.frozen_symbols)
        self.assertIn(
            ("BTCUSDT", "latency recovered after 2 healthy updates", owned_epoch, owned_reason),
            oms.symbol_clear_attempts,
        )

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

    def test_processing_lag_stale_recovery_cannot_clear_new_venue_fault(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        oms = DummyOMS()
        risk = RiskManager(engine, self.make_risk_config(), oms=oms, gateway=gateway)

        risk._freeze_venue("BINANCE", "processing_lag:first")
        stale_epoch = risk.venue_freeze_epochs["BINANCE"]
        oms.freeze_venue(
            "BINANCE",
            "system_health:new_fault",
            cancel_active_orders=False,
        )

        for _ in range(risk.venue_freeze_recovery_updates):
            risk._recover_venue_if_stable("BINANCE", prefix="processing_lag:")

        self.assertEqual(
            oms.get_venue_freeze_reason("BINANCE"),
            "system_health:new_fault",
        )
        self.assertFalse(oms.unfrozen_venues)
        self.assertEqual(oms.venue_clear_attempts[-1][2], stale_epoch)
        self.assertEqual(
            oms.venue_clear_attempts[-1][3],
            "processing_lag:first",
        )

    def test_watchdog_stale_recovery_cannot_clear_new_venue_fault(self):
        oms = DummyOMS()
        snapshot = {
            "lanes": {
                "market": {
                    "depth": 101,
                    "oldest_queued_ms": 0.0,
                }
            }
        }
        event_engine = types.SimpleNamespace(
            get_metrics_snapshot=lambda: snapshot,
        )
        config = {"recovery_checks": 2}

        state = emit_event_engine_backlog_if_needed(
            event_engine,
            oms,
            "BINANCE",
            {},
            config,
        )
        stale_epoch = state["venue_freeze_epoch"]
        original_reason = state["venue_freeze_reason"]
        oms.freeze_venue(
            "BINANCE",
            "system_health:new_fault",
            cancel_active_orders=False,
        )
        snapshot["lanes"] = {}

        for _ in range(config["recovery_checks"]):
            state = emit_event_engine_backlog_if_needed(
                event_engine,
                oms,
                "BINANCE",
                state,
                config,
            )

        self.assertEqual(
            oms.get_venue_freeze_reason("BINANCE"),
            "system_health:new_fault",
        )
        self.assertFalse(oms.unfrozen_venues)
        self.assertEqual(
            oms.venue_clear_attempts[-1][2:],
            (stale_epoch, original_reason),
        )

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

    def test_recovered_kill_sequence_resumes_to_flat_verified(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self.make_risk_config()
            config["oms"] = {
                "journal_enabled": True,
                "replay_journal_on_startup": True,
                "journal_fsync": True,
                "journal_integrity_check": True,
                "journal_path": os.path.join(tmpdir, "oms_journal.jsonl"),
            }
            writer = OMSJournal(config)
            writer.append(
                "risk_state",
                {
                    "risk_day": "2026-07-20",
                    "day_start_equity": 1000.0,
                    "peak_equity": 1000.0,
                    "last_equity": 900.0,
                    "kill_switch_triggered": True,
                    "kill_state": "CANCEL_PENDING",
                    "kill_reason": "restart_test",
                    "reason": "process_crashed",
                },
            )

            oms = DummyOMS()
            oms.journal = OMSJournal(config)
            risk = RiskManager(
                DummyEngine(),
                config,
                oms=oms,
                gateway=DummyGateway(),
            )

            self.assertTrue(risk.kill_switch_triggered)
            self.assertEqual(risk.kill_state, "CANCEL_PENDING")
            self.assertTrue(risk.resume_kill_switch_supervision())
            self.assertEqual(risk.kill_state, "FLAT_VERIFIED")
            self.assertTrue(risk.kill_switch_triggered)

            recovered_records = oms.journal.load()
            self.assertEqual(recovered_records[-1]["kind"], "risk_state")
            self.assertEqual(
                recovered_records[-1]["payload"]["kill_state"],
                "FLAT_VERIFIED",
            )

    def test_rearm_cannot_clear_kill_before_flat_verified(self):
        oms = DummyOMS()
        oms.state = types.SimpleNamespace(value="LIVE")
        oms.manual_rearm_required = False
        risk = RiskManager(
            DummyEngine(),
            self.make_risk_config(),
            oms=oms,
            gateway=DummyGateway(),
        )
        risk.kill_switch_triggered = True
        risk.kill_reason = "test_kill"
        risk.kill_state = "FLATTENING"

        risk._refresh_rearm_state()

        self.assertTrue(risk.kill_switch_triggered)
        self.assertEqual(risk.kill_state, "FLATTENING")

        risk.kill_state = "FLAT_VERIFIED"
        risk._refresh_rearm_state()

        self.assertFalse(risk.kill_switch_triggered)
        self.assertEqual(risk.kill_state, "ARMED")


class IndependentRiskSupervisorTests(unittest.TestCase):
    def make_settings(self, **overrides):
        settings = {
            "symbols": ["BTCUSDT", "ETHUSDT"],
            "parent_heartbeat_timeout_sec": 1.0,
            "exchange_poll_interval_sec": 1.0,
            "exchange_max_age_sec": 2.0,
            "cancel_retry_sec": 1.0,
            "orphan_exit_sec": 2.0,
            "emergency_countdown_time_ms": 1000,
            "max_account_gross_notional": 500.0,
            "gross_kill_multiplier": 1.25,
            "margin_reduce_only_ratio": 0.70,
            "margin_kill_ratio": 0.90,
            "flatten_enabled": True,
            "parent_loss_flatten_delay_sec": 0.5,
            "flatten_retry_sec": 0.5,
            "flat_verification_checks": 1,
        }
        settings.update(overrides)
        return settings

    @staticmethod
    def make_daily_snapshot(equity, cash_flow, captured_at):
        return {
            "account": {
                "totalMaintMargin": "0",
                "totalMarginBalance": str(equity),
            },
            "positions": [],
            "open_orders": [],
            "external_cash_flow_total": cash_flow,
            "captured_at": captured_at,
        }

    def test_sidecar_stale_parent_cancels_and_exits_after_orphan_window(self):
        exchange = DummySidecarExchange(healthy=True)
        core = RiskSidecarCore(exchange, self.make_settings(), now=100.0)
        self.assertTrue(core.receive_parent_heartbeat(1, now=100.1))

        healthy, keep_running = core.step(now=100.1)
        self.assertTrue(healthy["healthy"])
        self.assertTrue(keep_running)
        self.assertFalse(exchange.cancel_calls)

        stale, keep_running = core.step(now=101.2)
        self.assertFalse(stale["healthy"])
        self.assertEqual(stale["reason"], "parent_heartbeat_stale")
        self.assertTrue(keep_running)
        self.assertEqual(
            exchange.cancel_calls,
            [(("BTCUSDT", "ETHUSDT"), 1000)],
        )

        final, keep_running = core.step(now=103.3)
        self.assertFalse(final["healthy"])
        self.assertFalse(keep_running)
        self.assertEqual(final["risk_action"], "KILL")
        self.assertTrue(final["kill_latched"])
        self.assertEqual(final["stage"], "FLAT_VERIFIED")
        self.assertEqual(len(exchange.cancel_calls), 2)

    def test_sidecar_exchange_failure_is_unhealthy_and_cancels_immediately(self):
        exchange = DummySidecarExchange(False, "account_status=503")
        core = RiskSidecarCore(exchange, self.make_settings(), now=200.0)
        core.receive_parent_heartbeat(1, now=200.1)

        status, keep_running = core.step(now=200.1)

        self.assertFalse(status["healthy"])
        self.assertEqual(status["reason"], "account_status=503")
        self.assertTrue(keep_running)
        self.assertEqual(len(exchange.cancel_calls), 1)

    def test_sidecar_projected_gross_breach_enters_reduce_only(self):
        exchange = DummySidecarExchange(
            risk_snapshot={
                "account": {
                    "totalMaintMargin": "10",
                    "totalMarginBalance": "1000",
                },
                "positions": [
                    {
                        "symbol": "BTCUSDT",
                        "positionAmt": "3",
                        "markPrice": "100",
                    }
                ],
                "open_orders": [
                    {
                        "symbol": "BTCUSDT",
                        "origQty": "2",
                        "executedQty": "0",
                        "price": "100",
                        "reduceOnly": False,
                    }
                ],
            }
        )
        core = RiskSidecarCore(
            exchange,
            self.make_settings(max_account_gross_notional=450.0),
            now=250.0,
        )
        core.receive_parent_heartbeat(1, now=250.1)

        status, keep_running = core.step(now=250.1)

        self.assertFalse(status["healthy"])
        self.assertTrue(keep_running)
        self.assertEqual(status["risk_action"], "REDUCE_ONLY")
        self.assertIn("gross_notional_reduce_only", status["reason"])
        self.assertEqual(
            status["risk_metrics"]["projected_gross_notional"],
            500.0,
        )
        self.assertEqual(len(exchange.cancel_calls), 1)
        self.assertEqual(exchange.flatten_calls, 0)

    def test_sidecar_margin_kill_flattens_and_verifies_exchange_truth(self):
        exchange = DummySidecarExchange(
            risk_snapshot={
                "account": {
                    "totalMaintMargin": "95",
                    "totalMarginBalance": "100",
                },
                "positions": [
                    {
                        "symbol": "BTCUSDT",
                        "positionAmt": "1.5",
                        "markPrice": "100",
                    }
                ],
                "open_orders": [
                    {
                        "symbol": "ETHUSDT",
                        "origQty": "1",
                        "executedQty": "0",
                        "price": "50",
                        "reduceOnly": False,
                    }
                ],
            }
        )
        core = RiskSidecarCore(exchange, self.make_settings(), now=300.0)
        core.receive_parent_heartbeat(1, now=300.1)

        triggered, keep_running = core.step(now=300.1)

        self.assertFalse(triggered["healthy"])
        self.assertTrue(keep_running)
        self.assertEqual(triggered["risk_action"], "KILL")
        self.assertTrue(triggered["kill_latched"])
        self.assertEqual(triggered["stage"], "FLATTENING")
        self.assertEqual(exchange.flatten_calls, 1)
        self.assertEqual(triggered["last_flatten_count"], 1)

        verified, keep_running = core.step(now=301.2)

        self.assertTrue(keep_running)
        self.assertEqual(verified["risk_action"], "KILL")
        self.assertEqual(verified["stage"], "FLAT_VERIFIED")
        self.assertEqual(verified["flat_verification_count"], 1)
        self.assertEqual(exchange.flatten_calls, 1)

    def test_sidecar_reopens_flattening_if_exposure_reappears(self):
        exchange = DummySidecarExchange(
            risk_snapshot={
                "account": {
                    "totalMaintMargin": "95",
                    "totalMarginBalance": "100",
                },
                "positions": [
                    {
                        "symbol": "BTCUSDT",
                        "positionAmt": "1",
                        "markPrice": "100",
                    }
                ],
                "open_orders": [],
            }
        )
        core = RiskSidecarCore(exchange, self.make_settings(), now=350.0)
        core.receive_parent_heartbeat(1, now=350.1)
        core.step(now=350.1)
        verified, _ = core.step(now=351.2)
        self.assertEqual(verified["stage"], "FLAT_VERIFIED")

        exchange.risk_snapshot["positions"] = [
            {
                "symbol": "ETHUSDT",
                "positionAmt": "-2",
                "markPrice": "50",
            }
        ]
        reopened, _ = core.step(now=352.3)

        self.assertEqual(reopened["stage"], "FLATTENING")
        self.assertEqual(exchange.flatten_calls, 2)

    def test_sidecar_parent_loss_escalates_from_cancel_to_flatten(self):
        exchange = DummySidecarExchange(
            risk_snapshot={
                "account": {
                    "totalMaintMargin": "0",
                    "totalMarginBalance": "1000",
                },
                "positions": [
                    {
                        "symbol": "BTCUSDT",
                        "positionAmt": "1",
                        "markPrice": "100",
                    }
                ],
                "open_orders": [],
            }
        )
        core = RiskSidecarCore(exchange, self.make_settings(), now=400.0)
        core.receive_parent_heartbeat(1, now=400.1)
        core.step(now=400.1)

        stale, _ = core.step(now=401.2)
        self.assertEqual(stale["risk_action"], "REDUCE_ONLY")
        self.assertEqual(exchange.flatten_calls, 0)

        killed, _ = core.step(now=401.8)
        self.assertEqual(killed["risk_action"], "KILL")
        self.assertEqual(killed["reason"], "parent_heartbeat_stale_flatten")
        self.assertEqual(exchange.flatten_calls, 1)

        verified, keep_running = core.step(now=403.3)
        self.assertEqual(verified["stage"], "FLAT_VERIFIED")
        self.assertFalse(keep_running)

    def test_sidecar_rejects_non_finite_risk_limits(self):
        with self.assertRaisesRegex(ValueError, "gross_kill_multiplier"):
            RiskSidecarCore(
                DummySidecarExchange(),
                self.make_settings(gross_kill_multiplier=float("inf")),
            )

    def test_sidecar_daily_loss_is_adjusted_for_external_cash_flow(self):
        day_time = datetime(
            2026,
            7,
            20,
            1,
            tzinfo=timezone.utc,
        ).timestamp()
        exchange = DummySidecarExchange(
            risk_snapshot=self.make_daily_snapshot(1000.0, 0.0, day_time)
        )
        core = RiskSidecarCore(
            exchange,
            self.make_settings(
                daily_loss_enabled=True,
                max_daily_loss=100.0,
                max_drawdown_pct=0.0,
                daily_loss_reduce_only_fraction=0.5,
            ),
            now=700.0,
        )
        core.receive_parent_heartbeat(1, now=700.1)
        initial, _ = core.step(now=700.1)
        self.assertTrue(initial["healthy"])

        exchange.risk_snapshot.update(
            self.make_daily_snapshot(1040.0, 100.0, day_time + 60)
        )
        core.receive_parent_heartbeat(2, now=701.1)
        reduced, _ = core.step(now=701.2)
        self.assertEqual(reduced["risk_action"], "REDUCE_ONLY")
        self.assertEqual(
            reduced["risk_metrics"]["cash_flow_adjusted_equity"],
            940.0,
        )
        self.assertEqual(
            reduced["risk_metrics"]["cash_flow_adjusted_daily_loss"],
            60.0,
        )

        exchange.risk_snapshot.update(
            self.make_daily_snapshot(1090.0, 200.0, day_time + 120)
        )
        core.receive_parent_heartbeat(3, now=702.1)
        killed, _ = core.step(now=702.3)
        self.assertEqual(killed["risk_action"], "KILL")
        self.assertIn("daily_loss_kill", killed["reason"])
        self.assertTrue(killed["kill_latched"])

    def test_sidecar_peak_drawdown_triggers_from_intraday_high(self):
        day_time = datetime(
            2026,
            7,
            20,
            1,
            tzinfo=timezone.utc,
        ).timestamp()
        exchange = DummySidecarExchange(
            risk_snapshot=self.make_daily_snapshot(1000.0, 0.0, day_time)
        )
        core = RiskSidecarCore(
            exchange,
            self.make_settings(
                daily_loss_enabled=True,
                max_daily_loss=1000.0,
                max_drawdown_pct=0.05,
                daily_loss_reduce_only_fraction=0.8,
            ),
            now=720.0,
        )
        core.receive_parent_heartbeat(1, now=720.1)
        core.step(now=720.1)
        exchange.risk_snapshot.update(
            self.make_daily_snapshot(1100.0, 0.0, day_time + 60)
        )
        core.receive_parent_heartbeat(2, now=721.1)
        core.step(now=721.2)
        exchange.risk_snapshot.update(
            self.make_daily_snapshot(1040.0, 0.0, day_time + 120)
        )
        core.receive_parent_heartbeat(3, now=722.1)

        killed, _ = core.step(now=722.3)

        self.assertEqual(killed["risk_action"], "KILL")
        self.assertIn("peak_drawdown_kill", killed["reason"])
        self.assertAlmostEqual(
            killed["risk_metrics"]["peak_drawdown_pct"],
            60.0 / 1100.0,
        )

    def test_sidecar_daily_baseline_resets_at_utc_day_boundary(self):
        day_one = datetime(
            2026,
            7,
            20,
            23,
            59,
            tzinfo=timezone.utc,
        ).timestamp()
        day_two = datetime(
            2026,
            7,
            21,
            0,
            1,
            tzinfo=timezone.utc,
        ).timestamp()
        exchange = DummySidecarExchange(
            risk_snapshot=self.make_daily_snapshot(1000.0, 0.0, day_one)
        )
        core = RiskSidecarCore(
            exchange,
            self.make_settings(
                daily_loss_enabled=True,
                max_daily_loss=50.0,
                max_drawdown_pct=0.0,
            ),
            now=740.0,
        )
        core.receive_parent_heartbeat(1, now=740.1)
        core.step(now=740.1)
        exchange.risk_snapshot.update(
            self.make_daily_snapshot(900.0, 0.0, day_two)
        )
        core.receive_parent_heartbeat(2, now=741.1)

        reset, _ = core.step(now=741.2)

        self.assertTrue(reset["healthy"])
        self.assertEqual(reset["risk_metrics"]["risk_day"], "2026-07-21")
        self.assertEqual(core.day_start_equity, 900.0)

    def test_sidecar_daily_baseline_and_peak_survive_restart(self):
        day_time = datetime(
            2026,
            7,
            20,
            1,
            tzinfo=timezone.utc,
        ).timestamp()
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "sidecar-state.json")
            settings = self.make_settings(
                state_path=state_path,
                state_required=True,
                daily_loss_enabled=True,
                max_daily_loss=100.0,
                max_drawdown_pct=0.0,
            )
            exchange = DummySidecarExchange(
                risk_snapshot=self.make_daily_snapshot(
                    1000.0,
                    0.0,
                    day_time,
                )
            )
            first = RiskSidecarCore(exchange, settings, now=760.0)
            first.receive_parent_heartbeat(1, now=760.1)
            first.step(now=760.1)
            exchange.risk_snapshot.update(
                self.make_daily_snapshot(980.0, 0.0, day_time + 60)
            )
            first.receive_parent_heartbeat(2, now=761.1)
            first.step(now=761.2)

            exchange.risk_snapshot.update(
                self.make_daily_snapshot(890.0, 0.0, day_time + 120)
            )
            recovered = RiskSidecarCore(exchange, settings, now=762.0)
            recovered.receive_parent_heartbeat(1, now=762.1)
            killed, _ = recovered.step(now=762.1)

            self.assertTrue(recovered.state_recovered)
            self.assertEqual(recovered.day_start_equity, 1000.0)
            self.assertEqual(killed["risk_action"], "KILL")
            self.assertIn("daily_loss_kill", killed["reason"])

    def test_sidecar_daily_risk_fails_closed_without_cash_flow_truth(self):
        snapshot = {
            "account": {
                "totalMaintMargin": "0",
                "totalMarginBalance": "1000",
            },
            "positions": [],
            "open_orders": [],
            "captured_at": time.time(),
        }
        exchange = DummySidecarExchange(risk_snapshot=snapshot)
        core = RiskSidecarCore(
            exchange,
            self.make_settings(daily_loss_enabled=True),
            now=780.0,
        )
        core.receive_parent_heartbeat(1, now=780.1)

        status, _ = core.step(now=780.1)

        self.assertEqual(status["risk_action"], "REDUCE_ONLY")
        self.assertEqual(status["reason"], "daily_equity_snapshot_invalid")

    def test_sidecar_clock_phase_error_has_soft_and_hard_risk_stages(self):
        base_snapshot = {
            "account": {
                "totalMaintMargin": "0",
                "totalMarginBalance": "1000",
            },
            "positions": [],
            "open_orders": [],
            "clock_offset_ms": -800.0,
            "clock_rtt_ms": 10.0,
            "clock_uncertainty_ms": 6.0,
            "clock_offset_dispersion_ms": 1.0,
        }
        settings = self.make_settings(
            clock_sync_enabled=True,
            # Legacy offset keys remain accepted, but now define phase limits.
            clock_reduce_only_offset_ms=25.0,
            clock_kill_offset_ms=100.0,
            clock_max_rtt_ms=200.0,
            clock_max_uncertainty_ms=50.0,
            clock_max_offset_dispersion_ms=10.0,
        )
        stable_exchange = DummySidecarExchange(
            risk_snapshot={**base_snapshot, "clock_phase_error_ms": 0.0}
        )
        stable = RiskSidecarCore(stable_exchange, settings, now=790.0)
        stable.receive_parent_heartbeat(1, now=790.1)
        stable_status, _ = stable.step(now=790.1)
        self.assertEqual(stable_status["risk_action"], "NONE")

        soft_exchange = DummySidecarExchange(
            risk_snapshot={**base_snapshot, "clock_phase_error_ms": 30.0}
        )
        soft = RiskSidecarCore(soft_exchange, settings, now=800.0)
        soft.receive_parent_heartbeat(1, now=800.1)
        soft_status, _ = soft.step(now=800.1)
        self.assertEqual(soft_status["risk_action"], "REDUCE_ONLY")
        self.assertIn("clock_phase_error_reduce_only", soft_status["reason"])

        hard_exchange = DummySidecarExchange(
            risk_snapshot={**base_snapshot, "clock_phase_error_ms": -120.0}
        )
        hard = RiskSidecarCore(hard_exchange, settings, now=810.0)
        hard.receive_parent_heartbeat(1, now=810.1)
        hard_status, _ = hard.step(now=810.1)
        self.assertEqual(hard_status["risk_action"], "KILL")
        self.assertIn("clock_phase_error_kill", hard_status["reason"])

    def test_sidecar_hard_clock_failure_kills_without_private_snapshot(self):
        exchange = DummySidecarExchange(
            healthy=False,
            reason="clock_phase_error_kill:120.000ms",
        )
        core = RiskSidecarCore(
            exchange,
            self.make_settings(clock_sync_enabled=True),
            now=815.0,
        )
        core.receive_parent_heartbeat(1, now=815.1)

        status, _ = core.step(now=815.1)

        self.assertEqual(status["risk_action"], "KILL")
        self.assertTrue(status["kill_latched"])
        self.assertIn("clock_phase_error_kill", status["reason"])
        self.assertEqual(len(exchange.cancel_calls), 1)

    def test_sidecar_clock_quality_or_missing_snapshot_fails_closed(self):
        settings = self.make_settings(
            clock_sync_enabled=True,
            clock_reduce_only_phase_error_ms=25.0,
            clock_kill_phase_error_ms=100.0,
            clock_max_rtt_ms=200.0,
            clock_max_uncertainty_ms=50.0,
            clock_max_offset_dispersion_ms=10.0,
        )
        base_snapshot = {
            "account": {
                "totalMaintMargin": "0",
                "totalMarginBalance": "1000",
            },
            "positions": [],
            "open_orders": [],
            "clock_offset_ms": -800.0,
            "clock_phase_error_ms": 0.0,
            "clock_rtt_ms": 10.0,
            "clock_uncertainty_ms": 6.0,
            "clock_offset_dispersion_ms": 1.0,
        }
        cases = (
            (
                {"clock_rtt_ms": 250.0},
                "clock_rtt_reduce_only",
            ),
            (
                {"clock_uncertainty_ms": 60.0},
                "clock_uncertainty_reduce_only",
            ),
            (
                {"clock_offset_dispersion_ms": 12.0},
                "clock_dispersion_reduce_only",
            ),
        )
        for index, (override, expected_reason) in enumerate(cases):
            exchange = DummySidecarExchange(
                risk_snapshot={**base_snapshot, **override}
            )
            core = RiskSidecarCore(
                exchange,
                settings,
                now=820.0 + index,
            )
            core.receive_parent_heartbeat(1, now=820.1 + index)
            status, _ = core.step(now=820.1 + index)
            self.assertEqual(status["risk_action"], "REDUCE_ONLY")
            self.assertIn(expected_reason, status["reason"])

        invalid_exchange = DummySidecarExchange(
            risk_snapshot={
                "account": {
                    "totalMaintMargin": "0",
                    "totalMarginBalance": "1000",
                },
                "positions": [],
                "open_orders": [],
                "clock_offset_ms": -800.0,
                "clock_phase_error_ms": float("nan"),
                "clock_rtt_ms": 10.0,
                "clock_uncertainty_ms": 6.0,
                "clock_offset_dispersion_ms": 1.0,
            }
        )
        invalid = RiskSidecarCore(
            invalid_exchange,
            settings,
            now=829.0,
        )
        invalid.receive_parent_heartbeat(1, now=829.1)
        invalid_status, _ = invalid.step(now=829.1)
        self.assertEqual(invalid_status["risk_action"], "REDUCE_ONLY")
        self.assertEqual(invalid_status["reason"], "clock_snapshot_invalid")

        missing_exchange = DummySidecarExchange()
        missing = RiskSidecarCore(missing_exchange, settings, now=830.0)
        missing.receive_parent_heartbeat(1, now=830.1)
        missing_status, _ = missing.step(now=830.1)
        self.assertEqual(missing_status["risk_action"], "REDUCE_ONLY")
        self.assertEqual(missing_status["reason"], "clock_snapshot_invalid")

    def test_sidecar_long_liquidation_proximity_enters_reduce_only(self):
        exchange = DummySidecarExchange(
            risk_snapshot={
                "account": {
                    "totalMaintMargin": "10",
                    "totalMarginBalance": "1000",
                },
                "positions": [
                    {
                        "symbol": "BTCUSDT",
                        "positionAmt": "1",
                        "markPrice": "100",
                        "liquidationPrice": "96",
                    }
                ],
                "open_orders": [],
            }
        )
        core = RiskSidecarCore(
            exchange,
            self.make_settings(
                liquidation_proximity_enabled=True,
                require_liquidation_price=True,
                liquidation_reduce_only_distance_pct=0.05,
                liquidation_kill_distance_pct=0.02,
            ),
            now=840.0,
        )
        core.receive_parent_heartbeat(1, now=840.1)

        status, _ = core.step(now=840.1)

        self.assertEqual(status["risk_action"], "REDUCE_ONLY")
        self.assertIn("liquidation_distance_reduce_only", status["reason"])
        self.assertAlmostEqual(
            status["risk_metrics"]["minimum_liquidation_distance_pct"],
            0.04,
        )

    def test_sidecar_short_liquidation_proximity_triggers_kill(self):
        exchange = DummySidecarExchange(
            risk_snapshot={
                "account": {
                    "totalMaintMargin": "10",
                    "totalMarginBalance": "1000",
                },
                "positions": [
                    {
                        "symbol": "ETHUSDT",
                        "positionAmt": "-2",
                        "markPrice": "100",
                        "liquidationPrice": "101.5",
                    }
                ],
                "open_orders": [],
            }
        )
        core = RiskSidecarCore(
            exchange,
            self.make_settings(
                liquidation_proximity_enabled=True,
                require_liquidation_price=True,
                liquidation_reduce_only_distance_pct=0.05,
                liquidation_kill_distance_pct=0.02,
            ),
            now=850.0,
        )
        core.receive_parent_heartbeat(1, now=850.1)

        status, _ = core.step(now=850.1)

        self.assertEqual(status["risk_action"], "KILL")
        self.assertIn("liquidation_distance_kill:ETHUSDT", status["reason"])
        self.assertEqual(exchange.flatten_calls, 1)

    def test_sidecar_missing_required_liquidation_price_fails_closed(self):
        exchange = DummySidecarExchange(
            risk_snapshot={
                "account": {
                    "totalMaintMargin": "10",
                    "totalMarginBalance": "1000",
                },
                "positions": [
                    {
                        "symbol": "BTCUSDT",
                        "positionAmt": "1",
                        "markPrice": "100",
                        "liquidationPrice": "0",
                    }
                ],
                "open_orders": [],
            }
        )
        core = RiskSidecarCore(
            exchange,
            self.make_settings(
                liquidation_proximity_enabled=True,
                require_liquidation_price=True,
            ),
            now=860.0,
        )
        core.receive_parent_heartbeat(1, now=860.1)

        status, _ = core.step(now=860.1)

        self.assertEqual(status["risk_action"], "REDUCE_ONLY")
        self.assertEqual(
            status["reason"],
            "liquidation_price_unavailable:BTCUSDT",
        )
        self.assertEqual(
            status["risk_metrics"]["nonzero_position_count"],
            1,
        )

    def test_sidecar_notional_overflow_kills_without_non_finite_metrics(self):
        exchange = DummySidecarExchange(
            risk_snapshot={
                "account": {
                    "totalMaintMargin": "0",
                    "totalMarginBalance": "1000",
                },
                "positions": [
                    {
                        "symbol": "BTCUSDT",
                        "positionAmt": "1e308",
                        "markPrice": "1e308",
                    }
                ],
                "open_orders": [],
            }
        )
        core = RiskSidecarCore(exchange, self.make_settings(), now=870.0)
        core.receive_parent_heartbeat(1, now=870.1)

        status, _ = core.step(now=870.1)

        self.assertEqual(status["risk_action"], "KILL")
        self.assertEqual(
            status["reason"],
            "position_notional_overflow:BTCUSDT",
        )
        json.dumps(status, allow_nan=False)

    def test_sidecar_kill_latch_survives_restart_until_two_phase_rearm(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "sidecar-state.json")
            settings = self.make_settings(
                state_path=state_path,
                state_required=True,
                state_fsync=True,
            )
            exchange = DummySidecarExchange(
                risk_snapshot={
                    "account": {
                        "totalMaintMargin": "95",
                        "totalMarginBalance": "100",
                    },
                    "positions": [
                        {
                            "symbol": "BTCUSDT",
                            "positionAmt": "1",
                            "markPrice": "100",
                        }
                    ],
                    "open_orders": [],
                }
            )
            first = RiskSidecarCore(exchange, settings, now=500.0)
            first.receive_parent_heartbeat(1, now=500.1)
            triggered, _ = first.step(now=500.1)

            self.assertTrue(triggered["kill_latched"])
            self.assertTrue(os.path.exists(state_path))
            with open(state_path, "r", encoding="utf-8") as handle:
                persisted = json.load(handle)
            self.assertTrue(persisted["payload"]["kill_latched"])

            exchange.risk_snapshot["account"] = {
                "totalMaintMargin": "0",
                "totalMarginBalance": "1000",
            }
            recovered = RiskSidecarCore(exchange, settings, now=501.0)
            self.assertTrue(recovered.state_recovered)
            self.assertTrue(recovered.kill_latched)
            self.assertEqual(recovered.stage, "FLATTENING")
            recovered.receive_parent_heartbeat(1, now=501.1)
            verified, _ = recovered.step(now=501.1)
            self.assertEqual(verified["stage"], "FLAT_VERIFIED")

            prepared, token, reason = recovered.prepare_rearm(
                "prepare-1",
                "operator_ack",
                now=501.2,
            )
            self.assertTrue(prepared, reason)
            committed, reason = recovered.commit_rearm(
                "commit-1",
                token,
                now=501.3,
            )
            self.assertTrue(committed, reason)
            self.assertFalse(recovered.kill_latched)
            self.assertEqual(recovered.stage, "ARMED")

            clean_restart = RiskSidecarCore(exchange, settings, now=502.0)
            self.assertTrue(clean_restart.state_recovered)
            self.assertFalse(clean_restart.kill_latched)
            self.assertEqual(clean_restart.stage, "ARMED")

    def test_sidecar_commit_rechecks_exchange_and_refuses_new_position(self):
        exchange = DummySidecarExchange(
            risk_snapshot={
                "account": {
                    "totalMaintMargin": "95",
                    "totalMarginBalance": "100",
                },
                "positions": [
                    {
                        "symbol": "BTCUSDT",
                        "positionAmt": "1",
                        "markPrice": "100",
                    }
                ],
                "open_orders": [],
            }
        )
        core = RiskSidecarCore(exchange, self.make_settings(), now=550.0)
        core.receive_parent_heartbeat(1, now=550.1)
        core.step(now=550.1)
        exchange.risk_snapshot["account"] = {
            "totalMaintMargin": "0",
            "totalMarginBalance": "1000",
        }
        core.receive_parent_heartbeat(2, now=551.15)
        core.step(now=551.2)
        prepared, token, reason = core.prepare_rearm(
            "prepare-2",
            "operator_ack",
            now=551.3,
        )
        self.assertTrue(prepared, reason)

        exchange.risk_snapshot["positions"] = [
            {
                "symbol": "ETHUSDT",
                "positionAmt": "1",
                "markPrice": "50",
            }
        ]
        committed, reason = core.commit_rearm(
            "commit-2",
            token,
            now=551.4,
        )

        self.assertFalse(committed)
        self.assertEqual(reason, "positions_remain")
        self.assertTrue(core.kill_latched)

    def test_sidecar_corrupt_durable_state_recovers_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "sidecar-state.json")
            with open(state_path, "w", encoding="utf-8") as handle:
                handle.write('{"payload":{"kill_latched":false}}')
            settings = self.make_settings(
                state_path=state_path,
                state_required=True,
            )

            core = RiskSidecarCore(
                DummySidecarExchange(),
                settings,
                now=600.0,
            )

            self.assertTrue(core.kill_latched)
            self.assertEqual(core.stage, "FAILED")
            self.assertIn("state_checksum_mismatch", core.state_load_error)
            self.assertTrue(
                any(".corrupt." in name for name in os.listdir(tmpdir))
            )

    def test_binance_sidecar_snapshot_and_reduce_only_flatten(self):
        rest = DummyRiskSidecarRest(
            positions=[
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "2",
                    "markPrice": "100",
                },
                {
                    "symbol": "ETHUSDT",
                    "positionAmt": "-3",
                    "markPrice": "50",
                },
            ],
            open_orders=[{"symbol": "BTCUSDT", "orderId": 1}],
        )
        exchange = BinanceRiskSidecarExchange.__new__(
            BinanceRiskSidecarExchange
        )
        exchange.rest = rest

        ok, snapshot, reason = exchange.get_risk_snapshot()
        self.assertTrue(ok, reason)
        self.assertEqual(len(snapshot["positions"]), 2)
        self.assertEqual(len(snapshot["open_orders"]), 1)
        self.assertIn("captured_at", snapshot)

        ok, submitted, reason = exchange.emergency_flatten()
        self.assertTrue(ok, reason)
        self.assertEqual(submitted, 2)
        self.assertEqual(len(rest.new_orders), 2)
        long_close, long_oid = rest.new_orders[0]
        short_close, short_oid = rest.new_orders[1]
        self.assertEqual(long_close.side, "SELL")
        self.assertEqual(long_close.volume, 2.0)
        self.assertEqual(short_close.side, "BUY")
        self.assertEqual(short_close.volume, 3.0)
        for request, client_oid in rest.new_orders:
            self.assertEqual(request.order_type, "MARKET")
            self.assertTrue(request.reduce_only)
            self.assertLessEqual(len(client_oid), 36)
        self.assertNotEqual(long_oid, short_oid)

    def test_binance_sidecar_cash_flow_truth_deduplicates_transfers(self):
        rest = DummyRiskSidecarRest(
            income_rows=[
                {
                    "incomeType": "TRANSFER",
                    "tranId": 101,
                    "asset": "USDT",
                    "income": "100",
                    "time": 1,
                },
                {
                    "incomeType": "TRANSFER",
                    "tranId": 101,
                    "asset": "USDT",
                    "income": "100",
                    "time": 1,
                },
                {
                    "incomeType": "FUNDING_FEE",
                    "tranId": 102,
                    "asset": "USDT",
                    "income": "-2",
                    "time": 2,
                },
            ]
        )
        exchange = BinanceRiskSidecarExchange.__new__(
            BinanceRiskSidecarExchange
        )
        exchange.rest = rest
        exchange.daily_loss_enabled = True
        exchange.cash_flow_income_types = {"TRANSFER"}
        exchange.cash_flow_assets = {"USDT"}
        exchange.cash_flow_max_pages = 2

        ok, snapshot, reason = exchange.get_risk_snapshot()

        self.assertTrue(ok, reason)
        self.assertEqual(snapshot["external_cash_flow_total"], 100.0)

    def test_binance_sidecar_syncs_clock_before_emergency_flatten(self):
        from infrastructure.time_service import time_service

        original_offset = time_service.offset
        server_time_ms = int(time.time() * 1000) + 750
        rest = DummyRiskSidecarRest(
            positions=[
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "1",
                }
            ],
            server_time_ms=server_time_ms,
        )
        exchange = BinanceRiskSidecarExchange.__new__(
            BinanceRiskSidecarExchange
        )
        exchange.rest = rest
        exchange.clock_sync_enabled = True
        exchange.clock_sync_interval_sec = 30.0
        exchange.last_clock_sync_monotonic = 0.0
        exchange.clock_offset_ms = 0.0
        exchange.clock_rtt_ms = 0.0
        exchange.clock_reason = "clock_sync_missing"
        try:
            ok, submitted, reason = exchange.emergency_flatten()

            self.assertTrue(ok, reason)
            self.assertEqual(submitted, 1)
            self.assertGreater(exchange.last_clock_sync_monotonic, 0.0)
            self.assertAlmostEqual(
                time_service.offset,
                exchange.clock_offset_ms,
            )
            self.assertGreater(exchange.clock_offset_ms, 500.0)
        finally:
            time_service.offset = original_offset

    def test_binance_sidecar_clock_sync_failure_blocks_risk_snapshot(self):
        rest = DummyRiskSidecarRest(server_time_status=503)
        exchange = BinanceRiskSidecarExchange.__new__(
            BinanceRiskSidecarExchange
        )
        exchange.rest = rest
        exchange.clock_sync_enabled = True
        exchange.clock_sync_interval_sec = 30.0
        exchange.last_clock_sync_monotonic = 0.0
        exchange.clock_offset_ms = 0.0
        exchange.clock_rtt_ms = 0.0
        exchange.clock_reason = "clock_sync_missing"

        ok, snapshot, reason = exchange.get_risk_snapshot()

        self.assertFalse(ok)
        self.assertEqual(snapshot, {})
        self.assertEqual(reason, "server_time_status=503")

    def test_binance_sidecar_clock_failure_bypasses_success_cache(self):
        exchange = BinanceRiskSidecarExchange.__new__(
            BinanceRiskSidecarExchange
        )
        exchange.clock_sync_enabled = True
        exchange.clock_sync_interval_sec = 30.0
        exchange.last_clock_sync_monotonic = time.perf_counter()
        exchange.clock_reason = "clock_rtt_exceeded:250ms"

        with patch.object(
            exchange,
            "sync_exchange_clock",
            return_value=(False, exchange.clock_reason),
        ) as sync_clock:
            ok, reason = exchange._ensure_exchange_clock()

        self.assertFalse(ok)
        self.assertEqual(reason, "clock_rtt_exceeded:250ms")
        sync_clock.assert_called_once_with()

    def test_binance_sidecar_clock_uses_low_rtt_median(self):
        rtts_ms = [100.0, 1.0, 2.0, 3.0, 80.0]
        offsets_ms = [900.0, 20.0, 21.0, 19.0, -700.0]
        wall_values = []
        monotonic_values = []
        server_values = []
        for index, (rtt_ms, offset_ms) in enumerate(
            zip(rtts_ms, offsets_ms, strict=True)
        ):
            wall_start = 1_000.0 + index
            mono_start = 10.0 + index
            wall_values.extend([wall_start, wall_start + rtt_ms / 1000.0])
            monotonic_values.extend(
                [mono_start, mono_start + rtt_ms / 1000.0]
            )
            server_values.append(
                wall_start * 1000.0 + rtt_ms / 2.0 + offset_ms
            )

        exchange = BinanceRiskSidecarExchange.__new__(
            BinanceRiskSidecarExchange
        )
        exchange.clock_sample_count = 5
        exchange.clock_min_successful_samples = 3
        exchange.clock_low_rtt_sample_count = 3
        exchange.clock_sample_spacing_ms = 0.0
        exchange.clock_max_wall_step_ms = 20.0
        server_iter = iter(server_values)
        exchange.rest = types.SimpleNamespace(
            get_server_time=lambda: DummyResponse(
                {"serverTime": next(server_iter)}
            )
        )

        with (
            patch(
                "risk.independent_supervisor.time.time",
                side_effect=wall_values,
            ),
            patch(
                "risk.independent_supervisor.time.perf_counter",
                side_effect=monotonic_values,
            ),
        ):
            sample, reason = exchange._collect_clock_samples()

        self.assertEqual(reason, "")
        self.assertAlmostEqual(sample["offset_ms"], 20.0, places=6)
        self.assertAlmostEqual(sample["rtt_ms"], 2.0, places=6)
        self.assertAlmostEqual(sample["dispersion_ms"], 1.0, places=6)
        self.assertAlmostEqual(sample["uncertainty_ms"], 2.0, places=6)

    def test_binance_sidecar_tracks_phase_against_previous_anchor(self):
        from infrastructure.time_service import time_service

        exchange = BinanceRiskSidecarExchange.__new__(
            BinanceRiskSidecarExchange
        )
        exchange.clock_max_rtt_ms = 200.0
        exchange.clock_max_uncertainty_ms = 50.0
        exchange.clock_max_offset_dispersion_ms = 10.0
        exchange.clock_max_initial_offset_ms = 5000.0
        exchange.clock_reduce_only_phase_error_ms = 25.0
        exchange.clock_kill_phase_error_ms = 100.0
        exchange._clock_anchor_epoch_ms = 0.0
        exchange._clock_anchor_monotonic = 0.0
        exchange.clock_reason = "clock_sync_missing"
        stable = {
            "offset_ms": -800.0,
            "rtt_ms": 2.0,
            "dispersion_ms": 1.0,
            "uncertainty_ms": 2.0,
        }
        shifted = {**stable, "offset_ms": -770.0}
        original_offset = time_service.offset
        try:
            with (
                patch.object(exchange, "_collect_clock_samples", return_value=(stable, "")),
                patch("risk.independent_supervisor.time.perf_counter", return_value=10.0),
                patch("risk.independent_supervisor.time.time", return_value=1000.0),
            ):
                self.assertEqual(exchange.sync_exchange_clock(), (True, ""))
            self.assertEqual(exchange.clock_phase_error_ms, 0.0)

            with (
                patch.object(exchange, "_collect_clock_samples", return_value=(stable, "")),
                patch("risk.independent_supervisor.time.perf_counter", return_value=20.0),
                patch("risk.independent_supervisor.time.time", return_value=1010.0),
            ):
                self.assertEqual(exchange.sync_exchange_clock(), (True, ""))
            self.assertAlmostEqual(exchange.clock_phase_error_ms, 0.0)
            stable_anchor = (
                exchange._clock_anchor_epoch_ms,
                exchange._clock_anchor_monotonic,
            )

            with (
                patch.object(exchange, "_collect_clock_samples", return_value=(shifted, "")),
                patch("risk.independent_supervisor.time.perf_counter", return_value=30.0),
                patch("risk.independent_supervisor.time.time", return_value=1020.0),
            ):
                self.assertEqual(
                    exchange.sync_exchange_clock(),
                    (False, "clock_phase_error_reduce_only:30.000ms"),
                )
            self.assertAlmostEqual(exchange.clock_phase_error_ms, 30.0)
            self.assertEqual(exchange.clock_offset_ms, -800.0)
            self.assertEqual(
                (
                    exchange._clock_anchor_epoch_ms,
                    exchange._clock_anchor_monotonic,
                ),
                stable_anchor,
            )

            # The rejected candidate must not become the next baseline.
            with (
                patch.object(exchange, "_collect_clock_samples", return_value=(shifted, "")),
                patch("risk.independent_supervisor.time.perf_counter", return_value=40.0),
                patch("risk.independent_supervisor.time.time", return_value=1030.0),
            ):
                self.assertEqual(
                    exchange.sync_exchange_clock(),
                    (False, "clock_phase_error_reduce_only:30.000ms"),
                )
            self.assertEqual(
                (
                    exchange._clock_anchor_epoch_ms,
                    exchange._clock_anchor_monotonic,
                ),
                stable_anchor,
            )

            hard_shifted = {**stable, "offset_ms": -680.0}
            with (
                patch.object(
                    exchange,
                    "_collect_clock_samples",
                    return_value=(hard_shifted, ""),
                ),
                patch("risk.independent_supervisor.time.perf_counter", return_value=50.0),
                patch("risk.independent_supervisor.time.time", return_value=1040.0),
            ):
                self.assertEqual(
                    exchange.sync_exchange_clock(),
                    (False, "clock_phase_error_kill:120.000ms"),
                )
            self.assertEqual(
                (
                    exchange._clock_anchor_epoch_ms,
                    exchange._clock_anchor_monotonic,
                ),
                stable_anchor,
            )
        finally:
            time_service.offset = original_offset

    def test_binance_sidecar_rejects_uncertain_clock_candidate(self):
        exchange = BinanceRiskSidecarExchange.__new__(
            BinanceRiskSidecarExchange
        )
        exchange.clock_offset_ms = 7.0
        exchange.clock_max_rtt_ms = 200.0
        exchange.clock_max_uncertainty_ms = 50.0
        exchange.clock_max_offset_dispersion_ms = 10.0
        candidate = {
            "offset_ms": 25.0,
            "rtt_ms": 120.0,
            "dispersion_ms": 1.0,
            "uncertainty_ms": 61.0,
        }

        with patch.object(
            exchange,
            "_collect_clock_samples",
            return_value=(candidate, ""),
        ):
            ok, reason = exchange.sync_exchange_clock()

        self.assertFalse(ok)
        self.assertEqual(reason, "clock_uncertainty_exceeded:61.000ms")
        self.assertEqual(exchange.clock_offset_ms, 7.0)

    def test_binance_sidecar_rejects_wall_clock_step_during_sample(self):
        exchange = BinanceRiskSidecarExchange.__new__(
            BinanceRiskSidecarExchange
        )
        exchange.clock_sample_count = 1
        exchange.clock_min_successful_samples = 1
        exchange.clock_low_rtt_sample_count = 1
        exchange.clock_sample_spacing_ms = 0.0
        exchange.clock_max_wall_step_ms = 20.0
        exchange.rest = types.SimpleNamespace(
            get_server_time=lambda: DummyResponse({"serverTime": 1_000_001.0})
        )

        with (
            patch(
                "risk.independent_supervisor.time.time",
                side_effect=[1000.0, 1001.0],
            ),
            patch(
                "risk.independent_supervisor.time.perf_counter",
                side_effect=[10.0, 10.001],
            ),
        ):
            sample, reason = exchange._collect_clock_samples()

        self.assertIsNone(sample)
        self.assertIn("clock_wall_step", reason)

    def test_binance_sidecar_rejects_non_positive_server_time(self):
        exchange = BinanceRiskSidecarExchange.__new__(
            BinanceRiskSidecarExchange
        )
        exchange.clock_sample_count = 1
        exchange.clock_min_successful_samples = 1
        exchange.clock_low_rtt_sample_count = 1
        exchange.clock_sample_spacing_ms = 0.0
        exchange.clock_max_wall_step_ms = 20.0
        exchange.rest = types.SimpleNamespace(
            get_server_time=lambda: DummyResponse({"serverTime": 0.0})
        )

        with (
            patch(
                "risk.independent_supervisor.time.time",
                side_effect=[1000.0, 1000.001],
            ),
            patch(
                "risk.independent_supervisor.time.perf_counter",
                side_effect=[10.0, 10.001],
            ),
        ):
            sample, reason = exchange._collect_clock_samples()

        self.assertIsNone(sample)
        self.assertEqual(reason, "server_time_non_positive")

    def test_sidecar_loop_consumes_heartbeats_then_cancels_when_they_stop(self):
        exchange = DummySidecarExchange(healthy=True)
        command_queue = queue.Queue()
        status_queue = queue.Queue()
        settings = self.make_settings(
            session_id="test-session",
            status_interval_sec=0.02,
            parent_heartbeat_timeout_sec=0.10,
            cancel_retry_sec=0.05,
            orphan_exit_sec=0.20,
        )
        command_queue.put(
            {
                "type": "HEARTBEAT",
                "session_id": "test-session",
                "sequence": 1,
            }
        )
        worker = threading.Thread(
            target=run_sidecar_loop,
            args=(command_queue, status_queue, settings, exchange),
            daemon=True,
        )
        worker.start()

        healthy_seen = False
        deadline = time.time() + 1.0
        while time.time() < deadline and worker.is_alive():
            try:
                status = status_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            healthy_seen = healthy_seen or bool(status.get("healthy"))

        worker.join(1.0)
        self.assertTrue(healthy_seen)
        self.assertFalse(worker.is_alive())
        self.assertTrue(exchange.cancel_calls)
        self.assertTrue(exchange.closed)

    def test_sidecar_loop_runs_across_a_spawned_process_boundary(self):
        context = multiprocessing.get_context("spawn")
        command_queue = context.Queue(maxsize=8)
        status_queue = context.Queue(maxsize=8)
        settings = self.make_settings(
            session_id="spawn-session",
            status_interval_sec=0.05,
            parent_heartbeat_timeout_sec=1.0,
            orphan_exit_sec=2.0,
        )
        command_queue.put(
            {
                "type": "HEARTBEAT",
                "session_id": "spawn-session",
                "sequence": 1,
            }
        )
        process = context.Process(
            target=run_sidecar_loop,
            args=(
                command_queue,
                status_queue,
                settings,
                DummySidecarExchange(healthy=True),
            ),
        )
        process.start()
        try:
            status = status_queue.get(timeout=5.0)
            self.assertTrue(status["healthy"])
            self.assertEqual(status["parent_sequence"], 1)
            command_queue.put(
                {
                    "type": "STOP",
                    "session_id": "spawn-session",
                    "cancel_orders": False,
                }
            )
            process.join(5.0)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(2.0)
            command_queue.close()
            status_queue.close()

    def test_parent_supervisor_requires_distinct_healthy_recovery_statuses(self):
        oms = DummyOMS()
        config = {
            "symbols": ["BTCUSDT"],
            "risk": {
                "risk_control_heartbeat": {
                    "enabled": True,
                    "required_source": "independent_supervisor",
                },
                "independent_supervisor": {
                    "enabled": True,
                    "recovery_checks": 2,
                },
            },
        }
        supervisor = IndependentRiskSupervisor(oms, config)

        self.assertFalse(
            supervisor._apply_oms_health(False, "supervisor_process_down")
        )
        self.assertEqual(oms.mode_override, OMSCapabilityMode.REDUCE_ONLY)

        supervisor.last_status = {"sequence": 1}
        self.assertFalse(supervisor._apply_oms_health(True, ""))
        self.assertEqual(oms.mode_override, OMSCapabilityMode.REDUCE_ONLY)

        supervisor.last_status = {"sequence": 2}
        self.assertTrue(supervisor._apply_oms_health(True, ""))
        self.assertIsNone(oms.mode_override)
        self.assertEqual(
            oms.risk_heartbeats[-1],
            ("independent_supervisor", True, ""),
        )

    def test_parent_supervisor_propagates_hard_breach_to_kill_switch(self):
        oms = DummyOMS()
        risk_manager = DummyRiskManager()
        config = {
            "symbols": ["BTCUSDT"],
            "risk": {
                "risk_control_heartbeat": {
                    "enabled": True,
                    "required_source": "independent_supervisor",
                },
                "independent_supervisor": {"enabled": True},
            },
        }
        supervisor = IndependentRiskSupervisor(
            oms,
            config,
            risk_manager=risk_manager,
        )
        supervisor.last_status = {
            "sequence": 7,
            "risk_action": "KILL",
        }

        self.assertFalse(
            supervisor._apply_oms_health(
                False,
                "maintenance_margin_kill:0.950000",
            )
        )
        self.assertEqual(len(risk_manager.kill_reasons), 1)
        self.assertIn("maintenance_margin_kill", risk_manager.kill_reasons[0])
        self.assertEqual(oms.mode_override, OMSCapabilityMode.REDUCE_ONLY)

    def test_parent_supervisor_executes_two_phase_rearm_over_sidecar_channel(self):
        oms = DummyOMS()
        config = {
            "symbols": ["BTCUSDT"],
            "risk": {
                "risk_control_heartbeat": {
                    "enabled": True,
                    "required_source": "independent_supervisor",
                },
                "independent_supervisor": {
                    "enabled": True,
                    "status_interval_sec": 0.02,
                    "rearm_command_timeout_sec": 1.0,
                },
            },
        }
        supervisor = IndependentRiskSupervisor(oms, config)
        supervisor.command_queue = queue.Queue()
        supervisor.status_queue = queue.Queue()
        settings = dict(supervisor.settings)
        exchange = DummySidecarExchange()
        worker = threading.Thread(
            target=run_sidecar_loop,
            args=(
                supervisor.command_queue,
                supervisor.status_queue,
                settings,
                exchange,
            ),
            daemon=True,
        )
        worker.start()
        supervisor.process = ThreadProcessHandle(worker)

        prepared = supervisor.prepare_rearm("operator_ack")
        self.assertTrue(prepared["accepted"], prepared["reason"])
        self.assertTrue(prepared["token"])
        committed = supervisor.commit_rearm(prepared["token"])
        self.assertTrue(committed["accepted"], committed["reason"])

        supervisor.stop(cancel_orders=False)
        worker.join(1.0)
        self.assertFalse(worker.is_alive())

if __name__ == "__main__":
    unittest.main()
