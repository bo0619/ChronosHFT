import os
import sys
import tempfile
import types
import unittest

if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")
    requests_stub.get = lambda *args, **kwargs: None
    requests_stub.Session = lambda *args, **kwargs: None
    requests_stub.Request = object
    sys.modules["requests"] = requests_stub

from event.type import (
    Event,
    ExchangeOrderUpdate,
    ExecutionPolicy,
    EVENT_EXCHANGE_ORDER_UPDATE,
    LifecycleState,
    OMSCapabilityMode,
    OrderIntent,
    Side,
)
from infrastructure.system_health import handle_system_health_event
from infrastructure.truth_monitor import TruthMonitor
from oms.engine import OMS
from oms.order import Order


class DummyEngine:
    def __init__(self):
        self.events = []

    def put(self, event):
        self.events.append(event)

    def register(self, _event_type, _handler):
        return None


class DummyGateway:
    def __init__(self):
        self.gateway_name = "BINANCE"
        self.open_orders = []
        self.positions = []
        self.account = {
            "totalWalletBalance": "1000",
            "totalInitialMargin": "0",
        }
        self.cancelled_symbols = []
        self.cancel_requests = []
        self.sent_orders = []

    def send_order(self, req, client_oid):
        self.sent_orders.append((req, client_oid))
        return f"ex-order-{len(self.sent_orders)}"

    def cancel_order(self, req):
        self.cancel_requests.append(req)
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


class DummyRiskController:
    def __init__(self):
        self.reasons = []

    def trigger_kill_switch(self, reason):
        self.reasons.append(reason)


class DummyScopedOms:
    def __init__(self):
        self.gateway = types.SimpleNamespace(gateway_name="BINANCE")
        self.symbol_freezes = []
        self.venue_freezes = []
        self.strategy_freezes = []
        self.cleared_symbols = []
        self.cleared_venues = []

    def freeze_symbol(self, symbol, reason, cancel_active_orders=True):
        self.symbol_freezes.append((symbol, reason, cancel_active_orders))

    def clear_symbol_freeze(self, symbol, reason=""):
        self.cleared_symbols.append((symbol, reason))

    def freeze_venue(self, venue, reason, cancel_active_orders=True):
        self.venue_freezes.append((venue, reason, cancel_active_orders))

    def clear_venue_freeze(self, venue, reason=""):
        self.cleared_venues.append((venue, reason))

    def freeze_strategy(self, strategy_id, reason, symbol="", cancel_active_orders=True):
        self.strategy_freezes.append((strategy_id, symbol, reason, cancel_active_orders))


class OMSSurvivabilityTests(unittest.TestCase):
    def make_config(self):
        return {
            "symbols": ["BTCUSDT"],
            "account": {
                "initial_balance_usdt": 1000.0,
                "leverage": 10,
            },
            "backtest": {
                "taker_fee": 0.0,
                "maker_fee": 0.0,
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

    def make_journaled_config(self, journal_path):
        config = self.make_config()
        config["oms"] = {
            "journal_enabled": True,
            "replay_journal_on_startup": True,
            "journal_path": journal_path,
        }
        return config

    def test_filled_close_books_realized_pnl_into_balance(self):
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
            )

            oms._apply_event(Event(EVENT_EXCHANGE_ORDER_UPDATE, update))

            self.assertAlmostEqual(oms.account.balance, 990.0)
            self.assertAlmostEqual(oms.account.equity, 990.0)
            self.assertAlmostEqual(oms.exposure.net_positions["BTCUSDT"], 0.0)
        finally:
            oms.stop()

    def test_full_reset_halts_if_remote_open_orders_survive_cancel_all(self):
        gateway = DummyGateway()
        gateway.open_orders = [
            {
                "symbol": "BTCUSDT",
                "orderId": 123,
                "clientOrderId": "orphan-1",
                "side": "BUY",
            }
        ]
        gateway.positions = []
        oms = OMS(DummyEngine(), gateway, self.make_config())
        try:
            oms._perform_full_reset()
            self.assertEqual(oms.state, LifecycleState.HALTED)
            self.assertIn("BTCUSDT", gateway.cancelled_symbols)
        finally:
            oms.stop()

    def test_reconcile_resets_on_orphan_remote_open_order(self):
        gateway = DummyGateway()
        gateway.positions = []
        gateway.open_orders = [
            {
                "symbol": "BTCUSDT",
                "orderId": 456,
                "clientOrderId": "ghost-456",
                "side": "SELL",
            }
        ]
        oms = OMS(DummyEngine(), gateway, self.make_config())
        try:
            called = []
            oms._perform_full_reset = lambda: called.append("reset")
            oms._execute_reconcile(None)
            self.assertEqual(called, ["reset"])
        finally:
            oms.stop()

    def test_manual_rearm_requires_explicit_reset_path(self):
        gateway = DummyGateway()
        oms = OMS(DummyEngine(), gateway, self.make_config())
        try:
            oms.halt_system("operator_test")

            self.assertEqual(oms.state, LifecycleState.HALTED)
            self.assertTrue(oms.manual_rearm_required)

            rearmed = oms.rearm_system("operator_ack")

            self.assertTrue(rearmed)
            self.assertEqual(oms.state, LifecycleState.LIVE)
            self.assertFalse(oms.manual_rearm_required)
        finally:
            oms.stop()

    def test_symbol_freeze_blocks_new_orders_without_halting_account(self):
        gateway = DummyGateway()
        oms = OMS(DummyEngine(), gateway, self.make_config())
        try:
            oms.state = LifecycleState.LIVE
            oms.freeze_symbol("BTCUSDT", "latency:test")

            result = oms.submit_order(
                OrderIntent(
                    "test",
                    "BTCUSDT",
                    Side.BUY,
                    100.0,
                    1.0,
                )
            )

            self.assertFalse(result.accepted)
            self.assertIn("symbol_frozen", result.reason)
            self.assertEqual(oms.state, LifecycleState.LIVE)
        finally:
            oms.stop()

    def test_strategy_freeze_blocks_only_targeted_strategy(self):
        gateway = DummyGateway()
        oms = OMS(DummyEngine(), gateway, self.make_config())
        try:
            oms.state = LifecycleState.LIVE
            oms.exposure.check_risk = lambda *args, **kwargs: (True, "")
            oms.freeze_strategy("alpha", "manual:test")

            blocked = oms.submit_order(OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 1.0))
            allowed = oms.submit_order(OrderIntent("beta", "BTCUSDT", Side.BUY, 100.0, 1.0))

            self.assertFalse(blocked.accepted)
            self.assertIn("strategy_frozen", blocked.reason)
            self.assertTrue(allowed.accepted)
        finally:
            oms.stop()

    def test_venue_freeze_blocks_new_orders_without_halting_account(self):
        gateway = DummyGateway()
        oms = OMS(DummyEngine(), gateway, self.make_config())
        try:
            oms.state = LifecycleState.LIVE
            oms.exposure.check_risk = lambda *args, **kwargs: (True, "")
            oms.freeze_venue("BINANCE", "manual:test", cancel_active_orders=False)

            result = oms.submit_order(OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 1.0))

            self.assertFalse(result.accepted)
            self.assertIn("venue_frozen", result.reason)
            self.assertEqual(oms.state, LifecycleState.LIVE)
        finally:
            oms.stop()

    def test_halted_state_is_cancel_only(self):
        gateway = DummyGateway()
        oms = OMS(DummyEngine(), gateway, self.make_config())
        try:
            active_order = Order(
                "oid-active",
                OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 1.0),
            )
            active_order.mark_submitting()
            active_order.mark_pending_ack("ex-active")
            active_order.mark_new("ex-active", update_time=1.0, seq=1)
            oms.orders[active_order.client_oid] = active_order
            oms.exchange_id_map[active_order.exchange_oid] = active_order

            oms.halt_system("kill:test")

            self.assertEqual(oms.capability_mode, OMSCapabilityMode.CANCEL_ONLY)
            self.assertTrue(oms.can_query_exchange())
            self.assertTrue(oms.can_cancel_orders())
            self.assertFalse(oms.can_open_new_risk())
            self.assertEqual(oms.query_open_orders(), [])

            blocked = oms.submit_order(OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 1.0))
            cancelled = oms.cancel_order("oid-active")

            self.assertFalse(blocked.accepted)
            self.assertIn("open_risk_blocked:CANCEL_ONLY", blocked.reason)
            self.assertTrue(cancelled)
            self.assertEqual(len(gateway.cancel_requests), 1)
        finally:
            oms.stop()

    def test_reconciling_state_is_read_only_for_public_controls(self):
        gateway = DummyGateway()
        gateway.open_orders = [{"symbol": "BTCUSDT", "orderId": 1, "clientOrderId": "ghost-1", "side": "BUY"}]
        oms = OMS(DummyEngine(), gateway, self.make_config())
        try:
            active_order = Order(
                "oid-active",
                OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 1.0),
            )
            active_order.mark_submitting()
            active_order.mark_pending_ack("ex-active")
            active_order.mark_new("ex-active", update_time=1.0, seq=1)
            oms.orders[active_order.client_oid] = active_order
            oms.exchange_id_map[active_order.exchange_oid] = active_order

            oms.state = LifecycleState.RECONCILING

            self.assertEqual(oms.get_capability_snapshot()["mode"], OMSCapabilityMode.READ_ONLY.value)
            self.assertTrue(oms.can_query_exchange())
            self.assertFalse(oms.can_cancel_orders())
            self.assertFalse(oms.can_open_new_risk())
            self.assertEqual(oms.query_open_orders(), gateway.open_orders)
            self.assertFalse(oms.cancel_order("oid-active"))
        finally:
            oms.stop()

    def test_exchange_updates_continue_while_halted(self):
        gateway = DummyGateway()
        oms = OMS(DummyEngine(), gateway, self.make_config())
        try:
            order = Order(
                "oid-active",
                OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 1.0),
            )
            order.mark_submitting()
            order.mark_pending_ack("ex-active")
            order.mark_new("ex-active", update_time=1.0, seq=1)
            oms.orders[order.client_oid] = order
            oms.exchange_id_map[order.exchange_oid] = order

            oms.halt_system("operator:test")
            oms.on_exchange_update(
                Event(
                    EVENT_EXCHANGE_ORDER_UPDATE,
                    ExchangeOrderUpdate(
                        client_oid="oid-active",
                        exchange_oid="ex-active",
                        symbol="BTCUSDT",
                        status="CANCELED",
                        filled_qty=0.0,
                        filled_price=0.0,
                        cum_filled_qty=0.0,
                        update_time=2.0,
                        seq=2,
                    ),
                )
            )

            self.assertEqual(oms.orders["oid-active"].status.value, "CANCELLED")
        finally:
            oms.stop()

    def test_degraded_mode_converts_aggressive_orders_to_passive(self):
        gateway = DummyGateway()
        oms = OMS(DummyEngine(), gateway, self.make_config())
        try:
            oms.state = LifecycleState.LIVE
            oms.set_trading_mode(OMSCapabilityMode.DEGRADED, "processing_lag:test")
            oms.exposure.check_risk = lambda *args, **kwargs: (True, "")

            result = oms.submit_order(
                OrderIntent(
                    "alpha",
                    "BTCUSDT",
                    Side.BUY,
                    100.0,
                    1.0,
                    order_type="LIMIT",
                    time_in_force="IOC",
                    is_post_only=False,
                    policy=ExecutionPolicy.AGGRESSIVE,
                )
            )

            self.assertTrue(result.accepted)
            sent_req, _client_oid = gateway.sent_orders[-1]
            self.assertTrue(sent_req.post_only)
            self.assertEqual(sent_req.time_in_force, "GTX")
            self.assertEqual(sent_req.order_type, "LIMIT")
        finally:
            oms.stop()

    def test_passive_only_blocks_aggressive_orders_but_allows_post_only(self):
        gateway = DummyGateway()
        oms = OMS(DummyEngine(), gateway, self.make_config())
        try:
            oms.state = LifecycleState.LIVE
            oms.set_trading_mode(OMSCapabilityMode.PASSIVE_ONLY, "processing_lag:test")
            oms.exposure.check_risk = lambda *args, **kwargs: (True, "")

            blocked = oms.submit_order(
                OrderIntent(
                    "alpha",
                    "BTCUSDT",
                    Side.BUY,
                    100.0,
                    1.0,
                    order_type="LIMIT",
                    time_in_force="IOC",
                    is_post_only=False,
                    policy=ExecutionPolicy.AGGRESSIVE,
                )
            )
            allowed = oms.submit_order(
                OrderIntent(
                    "alpha",
                    "BTCUSDT",
                    Side.BUY,
                    100.0,
                    1.0,
                    order_type="LIMIT",
                    time_in_force="GTX",
                    is_post_only=True,
                    policy=ExecutionPolicy.PASSIVE,
                )
            )

            self.assertFalse(blocked.accepted)
            self.assertEqual(blocked.reason, "oms_mode_passive_only")
            self.assertTrue(allowed.accepted)
        finally:
            oms.stop()

    def test_emergency_flatten_submits_reduce_only_market_order(self):
        gateway = DummyGateway()
        gateway.positions = [{"symbol": "BTCUSDT", "positionAmt": "1.5", "entryPrice": "100.0"}]
        oms = OMS(DummyEngine(), gateway, self.make_config())
        try:
            oms.exposure.force_sync("BTCUSDT", 1.5, 100.0)
            oms.halt_system("kill:test")

            submitted = oms.emergency_reduce_only_flatten("kill:test")

            self.assertEqual(submitted, 1)
            sent_req, client_oid = gateway.sent_orders[-1]
            self.assertTrue(client_oid.startswith("EMERGENCY_"))
            self.assertEqual(sent_req.order_type, "MARKET")
            self.assertTrue(sent_req.reduce_only)
            self.assertEqual(sent_req.side, "SELL")
        finally:
            oms.stop()

    def test_duplicate_active_intent_is_rejected(self):
        gateway = DummyGateway()
        config = self.make_config()
        config["oms"].update(
            {
                "duplicate_intent_window_ms": 1000,
                "max_total_active_orders": 100,
                "max_symbol_active_orders": 100,
                "max_strategy_active_orders": 100,
                "max_strategy_symbol_active_orders": 100,
            }
        )
        oms = OMS(DummyEngine(), gateway, config)
        try:
            oms.state = LifecycleState.LIVE
            oms.exposure.check_risk = lambda *args, **kwargs: (True, "")

            first = oms.submit_order(OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 1.0))
            second = oms.submit_order(OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 1.0))

            self.assertTrue(first.accepted)
            self.assertFalse(second.accepted)
            self.assertIn("duplicate_active_intent", second.reason)
        finally:
            oms.stop()

    def test_strategy_symbol_active_order_cap_rejects_runaway_submissions(self):
        gateway = DummyGateway()
        config = self.make_config()
        config["oms"].update(
            {
                "duplicate_intent_window_ms": 0,
                "max_total_active_orders": 100,
                "max_symbol_active_orders": 100,
                "max_strategy_active_orders": 100,
                "max_strategy_symbol_active_orders": 1,
            }
        )
        oms = OMS(DummyEngine(), gateway, config)
        try:
            oms.state = LifecycleState.LIVE
            oms.exposure.check_risk = lambda *args, **kwargs: (True, "")

            first = oms.submit_order(OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 1.0))
            second = oms.submit_order(OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.1, 1.0))

            self.assertTrue(first.accepted)
            self.assertFalse(second.accepted)
            self.assertIn("active_order_limit:strategy_symbol", second.reason)
        finally:
            oms.stop()

    def test_restart_after_halt_requires_manual_rearm_before_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            journal_path = os.path.join(tmpdir, "oms_journal.jsonl")
            config = self.make_journaled_config(journal_path)

            oms = OMS(DummyEngine(), DummyGateway(), config)
            oms.halt_system("fatal:test")
            oms.stop()

            recovered = OMS(DummyEngine(), DummyGateway(), config)
            try:
                full_reset_calls = []
                recovered._perform_full_reset = lambda: full_reset_calls.append("reset")

                self.assertEqual(recovered.state, LifecycleState.HALTED)
                self.assertTrue(recovered.manual_rearm_required)
                self.assertFalse(recovered.bootstrap())
                self.assertEqual(full_reset_calls, [])

                recovered._perform_full_reset = lambda: setattr(recovered, "state", LifecycleState.LIVE)
                self.assertTrue(recovered.rearm_system("operator_ack"))
                self.assertFalse(recovered.manual_rearm_required)
            finally:
                recovered.stop()

    def test_bootstrap_blocked_halt_still_refreshes_account_snapshot(self):
        gateway = DummyGateway()
        gateway.account = {
            "totalWalletBalance": "4999.342098",
            "totalInitialMargin": "0",
            "availableBalance": "4999.342098",
            "assets": [
                {
                    "asset": "USDC",
                    "walletBalance": "4999.342098",
                    "availableBalance": "4999.342098",
                }
            ],
        }
        oms = OMS(DummyEngine(), gateway, self.make_config())
        try:
            oms.halt_system("processing_lag:test")

            self.assertFalse(oms.bootstrap())
            self.assertEqual(oms.state, LifecycleState.HALTED)
            self.assertAlmostEqual(oms.account.balance, 4999.342098)
            self.assertAlmostEqual(oms.account.balances["USDC"], 4999.342098)
            self.assertTrue(oms.account.exchange_balance_synced)
        finally:
            oms.stop()

    def test_restart_restores_scoped_guards_from_journal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            journal_path = os.path.join(tmpdir, "oms_journal.jsonl")
            config = self.make_journaled_config(journal_path)

            oms = OMS(DummyEngine(), DummyGateway(), config)
            oms.freeze_symbol("BTCUSDT", "latency:test", cancel_active_orders=False)
            oms.freeze_venue("BINANCE", "system_health:WS_PARSE_ERROR", cancel_active_orders=False)
            oms.freeze_strategy("alpha", "manual:test", cancel_active_orders=False)
            oms.freeze_strategy("beta", "manual:symbol", symbol="BTCUSDT", cancel_active_orders=False)
            oms.stop()

            recovered = OMS(DummyEngine(), DummyGateway(), config)
            try:
                self.assertEqual(recovered.get_symbol_freeze_reason("BTCUSDT"), "latency:test")
                self.assertEqual(
                    recovered.get_venue_freeze_reason("BINANCE"),
                    "system_health:WS_PARSE_ERROR",
                )
                self.assertEqual(recovered.get_strategy_freeze_reason("alpha"), "manual:test")
                self.assertEqual(
                    recovered.get_strategy_freeze_reason("beta", "BTCUSDT"),
                    "manual:symbol",
                )
                self.assertEqual(recovered.state, LifecycleState.FROZEN)
                self.assertTrue(recovered.rebuild_summary["clean_shutdown"])
            finally:
                recovered.stop()

    def test_dirty_shutdown_boots_into_frozen_reconcile_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            journal_path = os.path.join(tmpdir, "oms_journal.jsonl")
            config = self.make_journaled_config(journal_path)

            crashed = OMS(DummyEngine(), DummyGateway(), config)
            crashed.state = LifecycleState.LIVE
            crashed._audit("lifecycle", state=LifecycleState.LIVE.value, reason="simulated_live")
            crashed.order_monitor.stop()

            recovered = OMS(DummyEngine(), DummyGateway(), config)
            try:
                reconcile_calls = []
                recovered.trigger_reconcile = (
                    lambda reason, suspicious_oid=None: reconcile_calls.append((reason, suspicious_oid))
                )

                self.assertTrue(recovered.rebuild_summary["dirty_shutdown"])
                self.assertEqual(recovered.state, LifecycleState.FROZEN)
                self.assertEqual(recovered.last_freeze_reason, "Recovered unclean shutdown")
                self.assertTrue(recovered.bootstrap())
                self.assertEqual(reconcile_calls, [("Recovered guarded state", None)])
            finally:
                recovered.stop()

    def test_recovered_guards_are_cleared_after_successful_reconcile(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            journal_path = os.path.join(tmpdir, "oms_journal.jsonl")
            config = self.make_journaled_config(journal_path)

            oms = OMS(DummyEngine(), DummyGateway(), config)
            oms.freeze_strategy("alpha", "manual:test", cancel_active_orders=False)
            oms.stop()

            recovered = OMS(DummyEngine(), DummyGateway(), config)
            try:
                self.assertEqual(recovered.state, LifecycleState.FROZEN)
                self.assertEqual(recovered.get_strategy_freeze_reason("alpha"), "manual:test")
                self.assertFalse(recovered.can_submit_for_strategy("alpha", "BTCUSDT"))

                recovered._execute_reconcile(None)

                self.assertEqual(recovered.state, LifecycleState.LIVE)
                self.assertEqual(recovered.get_strategy_freeze_reason("alpha"), "")
                self.assertTrue(recovered.can_submit_for_strategy("alpha", "BTCUSDT"))
            finally:
                recovered.stop()


class DummyTruthProvider:
    gateway_name = "BINANCE"

    def __init__(self):
        self.account = {
            "totalWalletBalance": "1000",
            "totalInitialMargin": "0",
            "availableBalance": "1000",
        }
        self.positions = []
        self.open_orders = []

    def get_account_info(self):
        return self.account

    def get_all_positions(self):
        return self.positions

    def get_open_orders(self):
        return self.open_orders


class TruthMonitorTests(unittest.TestCase):
    def make_config(self):
        return {
            "testnet": False,
            "symbols": ["BTCUSDT"],
            "account": {
                "initial_balance_usdt": 1000.0,
                "leverage": 10,
            },
            "backtest": {
                "taker_fee": 0.0,
                "maker_fee": 0.0,
            },
            "oms": {
                "journal_enabled": False,
                "replay_journal_on_startup": False,
                "truth_monitor": {
                    "poll_interval_sec": 0.0,
                    "api_freeze_threshold": 2,
                    "api_halt_threshold": 3,
                    "clean_polls_to_clear": 2,
                },
            },
            "risk": {
                "limits": {
                    "max_pos_notional": 5000.0,
                }
            },
        }

    def test_truth_monitor_freezes_symbol_and_reconciles_on_remote_order_mismatch(self):
        oms = OMS(DummyEngine(), DummyGateway(), self.make_config())
        provider = DummyTruthProvider()
        provider.open_orders = [
            {
                "symbol": "BTCUSDT",
                "orderId": 999,
                "clientOrderId": "ghost-999",
                "side": "BUY",
            }
        ]
        monitor = TruthMonitor(oms, provider, self.make_config(), start_thread=False)
        try:
            oms.state = LifecycleState.LIVE
            called = []
            oms.trigger_reconcile = lambda reason, suspicious_oid=None: called.append((reason, suspicious_oid))

            monitor.poll_once()

            self.assertTrue(oms.get_symbol_freeze_reason("BTCUSDT").startswith("truth_plane:open_order_mismatch"))
            self.assertEqual(called, [("Truth plane open order mismatch", None)])
        finally:
            oms.stop()

    def test_truth_monitor_clears_transient_guards_after_clean_polls(self):
        oms = OMS(DummyEngine(), DummyGateway(), self.make_config())
        provider = DummyTruthProvider()
        monitor = TruthMonitor(oms, provider, self.make_config(), start_thread=False)
        try:
            oms.state = LifecycleState.LIVE
            oms.freeze_symbol("BTCUSDT", "truth_plane:position_mismatch", cancel_active_orders=False)

            monitor.poll_once()
            monitor.poll_once()

            self.assertEqual(oms.get_symbol_freeze_reason("BTCUSDT"), "")
        finally:
            oms.stop()

    def test_truth_monitor_freezes_venue_then_halts_on_api_blindness(self):
        oms = OMS(DummyEngine(), DummyGateway(), self.make_config())
        provider = DummyTruthProvider()
        provider.account = None
        provider.positions = None
        provider.open_orders = None
        monitor = TruthMonitor(oms, provider, self.make_config(), start_thread=False)
        try:
            oms.state = LifecycleState.LIVE

            monitor.poll_once()
            self.assertEqual(oms.get_venue_freeze_reason("BINANCE"), "")

            monitor.poll_once()
            self.assertTrue(oms.get_venue_freeze_reason("BINANCE").startswith("truth_plane:api_unreachable"))

            monitor.poll_once()
            self.assertEqual(oms.state, LifecycleState.HALTED)
        finally:
            oms.stop()

    def test_truth_monitor_requires_consecutive_balance_drift_before_reconcile(self):
        config = self.make_config()
        config["oms"]["truth_monitor"]["balance_drift_trigger_count"] = 2
        oms = OMS(DummyEngine(), DummyGateway(), config)
        provider = DummyTruthProvider()
        provider.account["totalWalletBalance"] = "1010"
        monitor = TruthMonitor(oms, provider, config, start_thread=False)
        reconcile_calls = []
        try:
            oms.state = LifecycleState.LIVE
            oms.account.force_sync(
                1000.0,
                0.0,
                available=1000.0,
                asset="USDT",
                balances={"USDT": {"wallet_balance": 1000.0, "available_balance": 1000.0}},
            )
            oms.trigger_reconcile = lambda reason, suspicious_oid=None: reconcile_calls.append((reason, suspicious_oid))

            monitor.poll_once()
            self.assertEqual(oms.get_venue_freeze_reason("BINANCE"), "")
            self.assertEqual(reconcile_calls, [])

            monitor.poll_once()
            self.assertTrue(oms.get_venue_freeze_reason("BINANCE").startswith("truth_plane:balance_drift"))
            self.assertEqual(reconcile_calls, [("Truth plane account balance drift", None)])
        finally:
            oms.stop()

    def test_truth_monitor_ignores_flat_balance_drift_on_testnet(self):
        config = self.make_config()
        config["testnet"] = True
        oms = OMS(DummyEngine(), DummyGateway(), config)
        provider = DummyTruthProvider()
        provider.account["totalWalletBalance"] = "1007"
        monitor = TruthMonitor(oms, provider, config, start_thread=False)
        try:
            oms.state = LifecycleState.LIVE
            reconcile_calls = []
            oms.trigger_reconcile = lambda reason, suspicious_oid=None: reconcile_calls.append((reason, suspicious_oid))

            monitor.poll_once()

            self.assertEqual(oms.get_venue_freeze_reason("BINANCE"), "")
            self.assertEqual(reconcile_calls, [])
            self.assertEqual(monitor.consecutive_balance_drifts, 0)
        finally:
            oms.stop()

    def test_truth_monitor_prefers_tracked_asset_balance_over_total_wallet(self):
        config = self.make_config()
        config["symbols"] = ["SOLUSDC"]
        oms = OMS(DummyEngine(), DummyGateway(), config)
        provider = DummyTruthProvider()
        provider.account = {
            "totalWalletBalance": "1200",
            "totalInitialMargin": "0",
            "availableBalance": "1200",
            "assets": [
                {"asset": "USDC", "walletBalance": "1000", "availableBalance": "1000"},
                {"asset": "BNB", "walletBalance": "200", "availableBalance": "200"},
            ],
        }
        monitor = TruthMonitor(oms, provider, config, start_thread=False)
        try:
            oms.state = LifecycleState.LIVE
            oms.account.force_sync(
                1000.0,
                0.0,
                available=1000.0,
                asset="USDC",
                balances={"USDC": {"wallet_balance": 1000.0, "available_balance": 1000.0}},
            )

            monitor.poll_once()

            self.assertEqual(oms.get_venue_freeze_reason("BINANCE"), "")
            self.assertEqual(monitor.consecutive_balance_drifts, 0)
        finally:
            oms.stop()

    def test_truth_monitor_skips_balance_drift_when_local_asset_snapshot_is_unsynced(self):
        config = self.make_config()
        config["symbols"] = ["SOLUSDC"]
        config["account"]["initial_balance_usdt"] = 2000.0
        oms = OMS(DummyEngine(), DummyGateway(), config)
        provider = DummyTruthProvider()
        provider.account = {
            "totalWalletBalance": "4999.342098",
            "totalInitialMargin": "0",
            "availableBalance": "4999.342098",
            "assets": [
                {"asset": "USDC", "walletBalance": "4999.342098", "availableBalance": "4999.342098"},
            ],
        }
        monitor = TruthMonitor(oms, provider, config, start_thread=False)
        try:
            oms.state = LifecycleState.HALTED
            oms.manual_rearm_required = True

            monitor.poll_once()

            self.assertEqual(oms.get_venue_freeze_reason("BINANCE"), "")
            self.assertEqual(monitor.consecutive_balance_drifts, 0)
        finally:
            oms.stop()

    def test_account_margin_checks_use_trading_budget_not_full_wallet_balance(self):
        config = self.make_config()
        config["account"]["trading_budget_total"] = 2000.0
        config["account"]["trading_budget_by_asset"] = {"USDC": 2000.0}
        oms = OMS(DummyEngine(), DummyGateway(), config)
        try:
            oms.account.force_sync(
                4999.342098,
                0.0,
                available=4999.342098,
                balances={"USDC": {"wallet_balance": 4999.342098, "available_balance": 4999.342098}},
            )

            self.assertAlmostEqual(oms.account.budget_balance, 2000.0)
            self.assertAlmostEqual(oms.account.budget_available, 2000.0)
            self.assertTrue(oms.account.check_margin(19999.0))
            self.assertFalse(oms.account.check_margin(25000.0))
        finally:
            oms.stop()


class SystemHealthHandlerTests(unittest.TestCase):
    def test_non_halt_health_event_triggers_kill_switch(self):
        risk_controller = DummyRiskController()
        handle_system_health_event(Event("eSystemHealth", "FATAL_GAP"), risk_controller)
        self.assertEqual(risk_controller.reasons, ["SystemHealth: FATAL_GAP"])

    def test_halt_echo_does_not_retrigger_kill_switch(self):
        risk_controller = DummyRiskController()
        handle_system_health_event(Event("eSystemHealth", "HALT:already_halted"), risk_controller)
        self.assertEqual(risk_controller.reasons, [])

    def test_scoped_symbol_health_event_freezes_symbol_without_kill(self):
        risk_controller = DummyRiskController()
        oms = DummyScopedOms()
        handle_system_health_event(
            Event("eSystemHealth", "FREEZE_SYMBOL:BTCUSDT:FATAL_GAP"),
            risk_controller,
            oms,
        )
        self.assertEqual(risk_controller.reasons, [])
        self.assertEqual(oms.symbol_freezes, [("BTCUSDT", "system_health:FATAL_GAP", True)])

    def test_scoped_venue_health_event_freezes_venue_without_kill(self):
        risk_controller = DummyRiskController()
        oms = DummyScopedOms()
        handle_system_health_event(
            Event("eSystemHealth", "FREEZE_VENUE:BINANCE:WS_PARSE_ERROR"),
            risk_controller,
            oms,
        )
        self.assertEqual(risk_controller.reasons, [])
        self.assertEqual(oms.venue_freezes, [("BINANCE", "system_health:WS_PARSE_ERROR", True)])

    def test_generic_stale_market_data_freezes_venue_without_kill(self):
        risk_controller = DummyRiskController()
        oms = DummyScopedOms()
        handle_system_health_event(
            Event("eSystemHealth", "MARKET_DATA_STALE:last=10.0 now=80.0"),
            risk_controller,
            oms,
        )
        self.assertEqual(risk_controller.reasons, [])
        self.assertEqual(
            oms.venue_freezes,
            [("BINANCE", "system_health:MARKET_DATA_STALE:last=10.0 now=80.0", True)],
        )


if __name__ == "__main__":
    unittest.main()
