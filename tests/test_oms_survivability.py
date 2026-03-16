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
