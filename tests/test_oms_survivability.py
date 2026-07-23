import os
import sys
import tempfile
import threading
import time
import types
import unittest

from data.cache import data_cache

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
    EVENT_ACCOUNT_UPDATE,
    LifecycleState,
    OMSCapabilityMode,
    OrderIntent,
    OrderStatus,
    Side,
)
from infrastructure.system_health import handle_system_health_event
from infrastructure.single_writer_fence import (
    SingleWriterFence,
    SingleWriterFenceError,
)
from infrastructure.truth_monitor import TruthMonitor
from oms.engine import OMS
from oms.journal import OMSJournal
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
        self.cancel_response = None
        self.dead_man_requests = []
        self.dead_man_status_code = 200
        self.cancel_all_status_code = 200

    def send_order(self, req, client_oid):
        self.sent_orders.append((req, client_oid))
        return f"ex-order-{len(self.sent_orders)}"

    def cancel_order(self, req):
        self.cancel_requests.append(req)
        return self.cancel_response

    def cancel_all_orders(self, symbol):
        self.cancelled_symbols.append(symbol)
        return types.SimpleNamespace(
            status_code=self.cancel_all_status_code,
            json=lambda: {},
        )

    def set_countdown_cancel_all(self, symbol, countdown_time_ms):
        self.dead_man_requests.append((symbol, countdown_time_ms))
        return types.SimpleNamespace(status_code=self.dead_man_status_code)

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
        self.venue_verifications = []
        self.orderbook_clears = []

    def freeze_symbol(self, symbol, reason, cancel_active_orders=True):
        self.symbol_freezes.append((symbol, reason, cancel_active_orders))

    def clear_symbol_freeze(self, symbol, reason=""):
        self.cleared_symbols.append((symbol, reason))

    def clear_orderbook_freeze(self, symbol, recovery_token, reason=""):
        self.orderbook_clears.append((symbol, recovery_token, reason))
        return True

    def freeze_venue(self, venue, reason, cancel_active_orders=True):
        self.venue_freezes.append((venue, reason, cancel_active_orders))

    def clear_venue_freeze(self, venue, reason=""):
        self.cleared_venues.append((venue, reason))

    def request_venue_recovery_verification(self, venue, reason=""):
        self.venue_verifications.append((venue, reason))
        return True

    def freeze_strategy(self, strategy_id, reason, symbol="", cancel_active_orders=True):
        self.strategy_freezes.append((strategy_id, symbol, reason, cancel_active_orders))


class DummyResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


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

    def test_single_writer_fence_rejects_second_owner_and_releases(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fence_path = os.path.join(temp_dir, "oms.lock")
            first = SingleWriterFence(
                fence_path,
                owner_metadata={"component": "writer-one"},
            )
            second = SingleWriterFence(
                fence_path,
                owner_metadata={"component": "writer-two"},
            )
            self.assertTrue(first.acquire())
            self.assertTrue(first.health_snapshot()["held"])

            with self.assertRaisesRegex(
                SingleWriterFenceError,
                "writer-one",
            ):
                second.acquire()

            self.assertTrue(first.release())
            self.assertTrue(second.acquire())
            self.assertTrue(second.release())

    def test_oms_holds_single_writer_fence_until_stop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.make_config()
            config["oms"]["single_writer_fence"] = {
                "enabled": True,
                "path": os.path.join(temp_dir, "oms.lock"),
            }
            first = OMS(DummyEngine(), DummyGateway(), config)
            try:
                with self.assertRaises(SingleWriterFenceError):
                    OMS(DummyEngine(), DummyGateway(), config)
            finally:
                first.stop()

            replacement = OMS(DummyEngine(), DummyGateway(), config)
            replacement.stop()

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
                trade_id=1,
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

    def test_full_reset_imports_but_halts_on_off_config_position(self):
        gateway = DummyGateway()
        oms = OMS(DummyEngine(), gateway, self.make_config())
        snapshot = {
            "open_orders": [],
            "account": {
                "totalWalletBalance": "1000",
                "totalInitialMargin": "10",
                "availableBalance": "990",
                "totalMaintMargin": "1",
                "totalMarginBalance": "1000",
            },
            "positions": [
                {
                    "symbol": "ETHUSDT",
                    "positionAmt": "0.5",
                    "entryPrice": "2000",
                }
            ],
            "account_floor": 1.0,
            "positions_floor": 1.0,
            "end_time_ms": 1000,
        }
        try:
            oms._ensure_venue_dead_man_switch_armed = lambda *_args: True
            oms.query_open_orders = lambda: []
            oms._cancel_all_orders_unchecked = lambda *_args, **_kwargs: True
            oms._capture_stable_exchange_snapshot = lambda **_kwargs: snapshot
            oms._prime_trade_history_baseline = lambda *_args, **_kwargs: True
            oms._backfill_trade_history = lambda *_args, **_kwargs: True

            oms._perform_full_reset()

            self.assertEqual(oms.state, LifecycleState.HALTED)
            self.assertTrue(oms.manual_rearm_required)
            self.assertEqual(oms.exposure.net_positions["ETHUSDT"], 0.5)
            self.assertIn("Off-config nonzero positions", oms.last_halt_reason)
        finally:
            oms.stop()

    def test_full_reset_completion_cannot_overwrite_newer_halt(self):
        gateway = DummyGateway()
        oms = OMS(DummyEngine(), gateway, self.make_config())
        snapshot = {
            "open_orders": [],
            "account": {
                "totalWalletBalance": "1000",
                "totalInitialMargin": "0",
                "availableBalance": "1000",
                "totalMaintMargin": "0",
                "totalMarginBalance": "1000",
            },
            "positions": [],
            "account_floor": 1.0,
            "positions_floor": 1.0,
            "end_time_ms": 1000,
        }
        try:
            oms._ensure_venue_dead_man_switch_armed = lambda *_args: True
            oms.query_open_orders = lambda: []
            oms._cancel_all_orders_unchecked = lambda *_args, **_kwargs: True
            oms._capture_stable_exchange_snapshot = lambda **_kwargs: snapshot
            oms._prime_trade_history_baseline = lambda *_args, **_kwargs: True
            oms._backfill_trade_history = lambda *_args, **_kwargs: True
            halt_injected = False

            def halt_before_commit(_symbol):
                nonlocal halt_injected
                if not halt_injected:
                    halt_injected = True
                    oms.halt_system("newer reset halt")

            oms._emit_position_update = halt_before_commit

            oms._perform_full_reset()

            self.assertEqual(oms.state, LifecycleState.HALTED)
            self.assertTrue(oms.manual_rearm_required)
            self.assertEqual(oms.last_halt_reason, "newer reset halt")
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

    def test_stale_symbol_epoch_cannot_clear_newer_guard(self):
        gateway = DummyGateway()
        oms = OMS(DummyEngine(), gateway, self.make_config())
        try:
            first_epoch = oms.freeze_symbol(
                "BTCUSDT",
                "latency:first",
                cancel_active_orders=False,
            )
            second_epoch = oms.freeze_symbol(
                "BTCUSDT",
                "truth_plane:newer",
                cancel_active_orders=False,
            )

            self.assertTrue(
                oms.clear_symbol_freeze(
                    "BTCUSDT",
                    reason="stale recovery",
                    expected_epoch=first_epoch,
                    expected_reason="latency:first",
                )
            )
            self.assertEqual(
                oms.get_symbol_freeze_reason("BTCUSDT"),
                "truth_plane:newer",
            )
            self.assertTrue(
                oms.clear_symbol_freeze(
                    "BTCUSDT",
                    reason="current recovery",
                    expected_epoch=second_epoch,
                    expected_reason="truth_plane:newer",
                )
            )
        finally:
            oms.stop()

    def test_independent_symbol_guard_owners_must_each_recover(self):
        oms = OMS(DummyEngine(), DummyGateway(), self.make_config())
        try:
            book_epoch = oms.freeze_symbol(
                "BTCUSDT",
                "system_health:FATAL_GAP:41",
                cancel_active_orders=False,
            )
            latency_epoch = oms.freeze_symbol(
                "BTCUSDT",
                "latency:250ms>100ms",
                cancel_active_orders=False,
            )

            self.assertTrue(
                oms.clear_symbol_freeze(
                    "BTCUSDT",
                    reason="latency recovered",
                    expected_epoch=latency_epoch,
                    expected_reason="latency:250ms>100ms",
                )
            )
            self.assertEqual(
                oms.get_symbol_freeze_reason("BTCUSDT"),
                "system_health:FATAL_GAP:41",
            )
            self.assertEqual(
                oms.get_symbol_freeze_epoch("BTCUSDT"),
                book_epoch,
            )
            self.assertTrue(oms.clear_orderbook_freeze("BTCUSDT", "41"))
            self.assertEqual(oms.get_symbol_freeze_reason("BTCUSDT"), "")
        finally:
            oms.stop()

    def test_order_truth_resolver_only_clears_its_own_order(self):
        oms = OMS(DummyEngine(), DummyGateway(), self.make_config())
        try:
            oms.freeze_symbol(
                "BTCUSDT",
                "order_truth:submit_unknown:order-a",
                cancel_active_orders=False,
            )
            oms.freeze_symbol(
                "BTCUSDT",
                "order_truth:cancel_unknown:order-b",
                cancel_active_orders=False,
            )

            oms._clear_order_truth_guard("BTCUSDT", "order-a")

            owners = oms.get_symbol_freeze_owners("BTCUSDT")
            self.assertNotIn("order_truth:order-a", owners)
            self.assertEqual(
                owners["order_truth:order-b"]["reason"],
                "order_truth:cancel_unknown:order-b",
            )
        finally:
            oms.stop()

    def test_transient_cleanup_snapshot_cannot_clear_reasserted_guard(self):
        oms = OMS(DummyEngine(), DummyGateway(), self.make_config())
        try:
            oms.freeze_symbol(
                "BTCUSDT",
                "truth_plane:position_mismatch:old",
                cancel_active_orders=False,
            )
            with oms.lock:
                snapshot = oms._capture_guard_cleanup_snapshot_locked(
                    prefixes=("truth_plane:",)
                )
            oms.freeze_symbol(
                "BTCUSDT",
                "truth_plane:position_mismatch:new",
                cancel_active_orders=False,
            )

            self.assertEqual(
                oms.clear_transient_guards(
                    prefixes=("truth_plane:",),
                    guard_snapshot=snapshot,
                ),
                0,
            )
            self.assertEqual(
                oms.get_symbol_freeze_reason("BTCUSDT"),
                "truth_plane:position_mismatch:new",
            )
        finally:
            oms.stop()

    def test_reconcile_start_cannot_overwrite_concurrent_halt(self):
        oms = OMS(DummyEngine(), DummyGateway(), self.make_config())
        try:
            oms.state = LifecycleState.LIVE
            oms._sync_capability_mode("test_live")

            def halt_during_freeze(*_args, **_kwargs):
                oms.halt_system("newer halt")

            oms.freeze_system = halt_during_freeze

            self.assertFalse(oms.trigger_reconcile("raced reconcile"))
            self.assertEqual(oms.state, LifecycleState.HALTED)
            self.assertTrue(oms.manual_rearm_required)
            self.assertIsNone(oms._reconcile_thread)
        finally:
            oms.stop()

    def test_reconcile_completion_cannot_overwrite_newer_halt(self):
        oms = OMS(DummyEngine(), DummyGateway(), self.make_config())
        try:
            oms.account.force_sync(1000.0, 0.0, 1000.0)
            oms.state = LifecycleState.RECONCILING
            oms._sync_capability_mode("test_reconcile")
            oms.query_positions = lambda: []
            oms.query_open_orders = lambda: []
            oms.query_account_info = lambda: {"totalWalletBalance": "1000"}
            oms._backfill_trade_history = lambda *_args, **_kwargs: True

            def halt_then_confirm(_remote_orders):
                oms.halt_system("newer reconcile halt")
                return True

            oms._refresh_missing_local_order_terminals = halt_then_confirm

            oms._execute_reconcile(None)

            self.assertEqual(oms.state, LifecycleState.HALTED)
            self.assertTrue(oms.manual_rearm_required)
            self.assertEqual(oms.last_halt_reason, "newer reconcile halt")
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

    def test_out_of_order_new_ack_does_not_trigger_reconcile(self):
        gateway = DummyGateway()
        oms = OMS(DummyEngine(), gateway, self.make_config())
        try:
            order = Order(
                "oid-race",
                OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 1.0),
            )
            order.mark_submitting()
            oms.orders[order.client_oid] = order

            oms.on_exchange_update(
                Event(
                    EVENT_EXCHANGE_ORDER_UPDATE,
                    ExchangeOrderUpdate(
                        client_oid="oid-race",
                        exchange_oid="ex-race",
                        symbol="BTCUSDT",
                        status="NEW",
                        filled_qty=0.0,
                        filled_price=0.0,
                        cum_filled_qty=0.0,
                        update_time=2.0,
                        seq=1,
                    ),
                )
            )

            self.assertEqual(order.status, OrderStatus.NEW)
            self.assertEqual(order.exchange_oid, "ex-race")
            self.assertNotEqual(oms.state, LifecycleState.FROZEN)
        finally:
            oms.stop()

    def test_cancel_unknown_order_keeps_local_risk_until_truth_is_known(self):
        gateway = DummyGateway()
        gateway.cancel_response = DummyResponse(
            400,
            {"code": -2011, "msg": "Unknown order sent."},
        )
        oms = OMS(DummyEngine(), gateway, self.make_config())
        try:
            oms.state = LifecycleState.LIVE
            oms._sync_capability_mode("test_live")
            order = Order(
                "oid-cancel-unknown",
                OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 2.0),
            )
            order.mark_submitting()
            order.mark_pending_ack("ex-cancel-unknown")
            order.mark_new("ex-cancel-unknown", update_time=1.0, seq=1)
            oms.orders[order.client_oid] = order
            oms.exchange_id_map[order.exchange_oid] = order
            oms.exposure.update_open_orders(oms.orders)

            self.assertGreater(oms.exposure.open_buy_qty["BTCUSDT"], 0.0)
            self.assertTrue(oms.cancel_order(order.client_oid))

            self.assertEqual(order.status, OrderStatus.CANCEL_UNKNOWN)
            self.assertGreater(oms.exposure.open_buy_qty["BTCUSDT"], 0.0)
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

    def test_reduce_only_mode_blocks_opening_risk_and_allows_position_reduction(self):
        gateway = DummyGateway()
        oms = OMS(DummyEngine(), gateway, self.make_config())
        try:
            oms.state = LifecycleState.LIVE
            oms.exposure.force_sync("BTCUSDT", 1.0, 100.0)
            oms.account.force_sync(1000.0, 0.0, available=1000.0)
            oms.set_trading_mode(OMSCapabilityMode.REDUCE_ONLY, "margin_health:test")
            self.assertFalse(
                oms.set_trading_mode(OMSCapabilityMode.DEGRADED, "processing_lag:test")
            )

            blocked = oms.submit_order(
                OrderIntent(
                    "alpha",
                    "BTCUSDT",
                    Side.BUY,
                    100.0,
                    0.5,
                    is_post_only=True,
                    policy=ExecutionPolicy.PASSIVE,
                )
            )
            allowed = oms.submit_order(
                OrderIntent(
                    "alpha",
                    "BTCUSDT",
                    Side.SELL,
                    100.0,
                    0.5,
                    order_type="MARKET",
                    time_in_force="IOC",
                    is_post_only=False,
                    reduce_only=True,
                    policy=ExecutionPolicy.AGGRESSIVE,
                )
            )

            self.assertFalse(blocked.accepted)
            self.assertEqual(blocked.reason, "oms_mode_reduce_only")
            self.assertTrue(allowed.accepted)
            self.assertEqual(oms.capability_mode, OMSCapabilityMode.REDUCE_ONLY)
            sent_req, _client_oid = gateway.sent_orders[-1]
            self.assertTrue(sent_req.reduce_only)

            self.assertTrue(
                oms.clear_trading_mode(
                    reason="margin recovered",
                    prefixes=("margin_health:",),
                )
            )
            self.assertEqual(oms.capability_mode, OMSCapabilityMode.DEGRADED)
            self.assertTrue(oms.mode_override_reason.startswith("processing_lag:"))
        finally:
            oms.stop()

    def test_entering_reduce_only_cancels_existing_opening_orders(self):
        gateway = DummyGateway()
        oms = OMS(DummyEngine(), gateway, self.make_config())
        try:
            oms.state = LifecycleState.LIVE
            oms.exposure.check_risk = lambda *args, **kwargs: (True, "")
            submitted = oms.submit_order(
                OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 0.5)
            )
            self.assertTrue(submitted.accepted)

            oms.set_trading_mode(OMSCapabilityMode.REDUCE_ONLY, "margin_health:test")

            self.assertTrue(gateway.cancel_requests)
            self.assertEqual(gateway.cancel_requests[-1].symbol, "BTCUSDT")
        finally:
            oms.stop()

    def test_restart_restores_composable_trading_mode_constraints(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            journal_path = os.path.join(tmpdir, "oms_journal.jsonl")
            config = self.make_journaled_config(journal_path)
            oms = OMS(DummyEngine(), DummyGateway(), config)
            oms.state = LifecycleState.LIVE
            oms.set_trading_mode(OMSCapabilityMode.REDUCE_ONLY, "margin_health:test")
            oms.set_trading_mode(OMSCapabilityMode.PASSIVE_ONLY, "processing_lag:test")
            oms.stop()

            recovered = OMS(DummyEngine(), DummyGateway(), config)
            try:
                self.assertEqual(len(recovered.mode_constraints), 2)
                self.assertEqual(recovered.mode_override, OMSCapabilityMode.REDUCE_ONLY)

                recovered.state = LifecycleState.LIVE
                recovered._sync_capability_mode("test_live")
                self.assertTrue(
                    recovered.clear_trading_mode(
                        reason="margin recovered",
                        prefixes=("margin_health:",),
                    )
                )

                self.assertEqual(recovered.capability_mode, OMSCapabilityMode.PASSIVE_ONLY)
                self.assertTrue(recovered.mode_override_reason.startswith("processing_lag:"))
            finally:
                recovered.stop()

    def test_margin_snapshot_gate_is_fail_closed_but_allows_reduction(self):
        gateway = DummyGateway()
        config = self.make_config()
        config["risk"]["margin_health"] = {
            "enabled": True,
            "require_snapshot": True,
            "reduce_only_ratio": 0.70,
            "max_snapshot_age_sec": 1.0,
        }
        oms = OMS(DummyEngine(), gateway, config)
        try:
            oms.state = LifecycleState.LIVE
            oms.exposure.force_sync("BTCUSDT", 1.0, 100.0)
            oms.account.force_sync(1000.0, 0.0, available=1000.0)

            blocked = oms.submit_order(
                OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 0.5)
            )
            reduction = oms.submit_order(
                OrderIntent(
                    "alpha",
                    "BTCUSDT",
                    Side.SELL,
                    100.0,
                    0.5,
                    order_type="MARKET",
                    time_in_force="IOC",
                    reduce_only=True,
                    policy=ExecutionPolicy.AGGRESSIVE,
                )
            )

            self.assertFalse(blocked.accepted)
            self.assertEqual(blocked.reason, "margin_health_unavailable")
            self.assertTrue(reduction.accepted)
        finally:
            oms.stop()

    def test_stale_margin_snapshot_blocks_new_risk_at_submit_boundary(self):
        config = self.make_config()
        config["risk"]["margin_health"] = {
            "enabled": True,
            "require_snapshot": True,
            "reduce_only_ratio": 0.70,
            "max_snapshot_age_sec": 1.0,
        }
        oms = OMS(DummyEngine(), DummyGateway(), config)
        try:
            oms.state = LifecycleState.LIVE
            oms.account.sync_margin_health(
                100.0,
                1000.0,
                snapshot_time=time.time() - 5.0,
            )

            result = oms.submit_order(
                OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 0.5)
            )

            self.assertFalse(result.accepted)
            self.assertTrue(result.reason.startswith("margin_health_stale:"))
        finally:
            oms.stop()

    def test_risk_heartbeat_lease_fails_closed_but_allows_reduction(self):
        gateway = DummyGateway()
        config = self.make_config()
        config["risk"]["risk_control_heartbeat"] = {
            "enabled": True,
            "max_age_sec": 0.05,
        }
        oms = OMS(DummyEngine(), gateway, config)
        try:
            oms.state = LifecycleState.LIVE
            oms.exposure.force_sync("BTCUSDT", 1.0, 100.0)
            oms.account.force_sync(1000.0, 0.0, available=1000.0)
            data_cache.update_mark_price(
                types.SimpleNamespace(
                    symbol="BTCUSDT",
                    mark_price=100.0,
                    datetime=None,
                )
            )

            missing = oms.submit_order(
                OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 0.1)
            )
            self.assertFalse(missing.accepted)
            self.assertEqual(missing.reason, "risk_control_heartbeat_missing")

            self.assertTrue(oms.record_risk_control_heartbeat("test_risk_loop"))
            healthy = oms.submit_order(
                OrderIntent("alpha", "BTCUSDT", Side.BUY, 101.0, 0.1)
            )
            self.assertTrue(healthy.accepted)
            self.assertTrue(oms.get_risk_control_heartbeat_snapshot()["valid"])

            oms.last_risk_control_heartbeat_monotonic -= 1.0
            stale = oms.submit_order(
                OrderIntent("beta", "BTCUSDT", Side.BUY, 102.0, 0.1)
            )
            self.assertFalse(stale.accepted)
            self.assertTrue(stale.reason.startswith("risk_control_heartbeat_stale:"))

            reduction = oms.submit_order(
                OrderIntent(
                    "alpha",
                    "BTCUSDT",
                    Side.SELL,
                    100.0,
                    0.5,
                    order_type="MARKET",
                    time_in_force="IOC",
                    reduce_only=True,
                    policy=ExecutionPolicy.AGGRESSIVE,
                )
            )
            self.assertTrue(reduction.accepted)
        finally:
            oms.stop()
            with data_cache._lock:
                data_cache.mark_prices.pop("BTCUSDT", None)
                data_cache.mark_update_times.pop("BTCUSDT", None)

    def test_risk_heartbeat_rejects_untrusted_in_process_source(self):
        config = self.make_config()
        config["risk"]["risk_control_heartbeat"] = {
            "enabled": True,
            "max_age_sec": 2.0,
            "required_source": "independent_supervisor",
        }
        oms = OMS(DummyEngine(), DummyGateway(), config)
        try:
            self.assertFalse(
                oms.record_risk_control_heartbeat(
                    source="risk_manager",
                    healthy=True,
                )
            )
            rejected = oms.get_risk_control_heartbeat_snapshot()
            self.assertFalse(rejected["valid"])
            self.assertEqual(rejected["status"], "missing")
            self.assertEqual(
                rejected["required_source"],
                "independent_supervisor",
            )

            self.assertTrue(
                oms.record_risk_control_heartbeat(
                    source="independent_supervisor",
                    healthy=True,
                )
            )
            accepted = oms.get_risk_control_heartbeat_snapshot()
            self.assertTrue(accepted["valid"])
            self.assertEqual(accepted["source"], "independent_supervisor")
        finally:
            oms.stop()

    def test_venue_dead_man_switch_fails_closed_and_recovers(self):
        gateway = DummyGateway()
        config = self.make_config()
        config["oms"]["venue_dead_man_switch"] = {
            "enabled": True,
            "countdown_time_ms": 1000,
            "renewal_interval_sec": 0.05,
            "max_renewal_age_sec": 0.10,
            "recovery_checks": 2,
        }
        oms = OMS(DummyEngine(), gateway, config)
        try:
            oms.state = LifecycleState.LIVE
            oms.exposure.force_sync("BTCUSDT", 1.0, 100.0)
            oms.account.force_sync(1000.0, 0.0, available=1000.0)
            data_cache.update_mark_price(
                types.SimpleNamespace(
                    symbol="BTCUSDT",
                    mark_price=100.0,
                    datetime=None,
                )
            )

            missing = oms.submit_order(
                OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 0.1)
            )
            self.assertFalse(missing.accepted)
            self.assertTrue(
                missing.reason.startswith("venue_dead_man_switch:unarmed_symbols:")
            )

            self.assertTrue(oms.renew_venue_dead_man_switch(force=True))
            self.assertEqual(gateway.dead_man_requests, [("BTCUSDT", 1000)])
            healthy = oms.submit_order(
                OrderIntent("alpha", "BTCUSDT", Side.BUY, 101.0, 0.1)
            )
            self.assertTrue(healthy.accepted)
            self.assertTrue(
                oms.get_venue_dead_man_switch_snapshot()["valid"]
            )

            oms.last_venue_dead_man_success_monotonic -= 1.0
            stale = oms.submit_order(
                OrderIntent("beta", "BTCUSDT", Side.BUY, 102.0, 0.1)
            )
            self.assertFalse(stale.accepted)
            self.assertTrue(
                stale.reason.startswith("venue_dead_man_switch:renewal_stale:")
            )

            reduction = oms.submit_order(
                OrderIntent(
                    "alpha",
                    "BTCUSDT",
                    Side.SELL,
                    100.0,
                    0.5,
                    order_type="MARKET",
                    time_in_force="IOC",
                    reduce_only=True,
                    policy=ExecutionPolicy.AGGRESSIVE,
                )
            )
            self.assertTrue(reduction.accepted)

            gateway.dead_man_status_code = 500
            self.assertFalse(oms.renew_venue_dead_man_switch(force=True))
            self.assertEqual(oms.capability_mode, OMSCapabilityMode.REDUCE_ONLY)
            self.assertTrue(
                oms.has_trading_mode_constraint(("venue_dead_man_switch:",))
            )

            gateway.dead_man_status_code = 200
            self.assertTrue(oms.renew_venue_dead_man_switch(force=True))
            self.assertTrue(
                oms.has_trading_mode_constraint(("venue_dead_man_switch:",))
            )
            self.assertTrue(oms.renew_venue_dead_man_switch(force=True))
            self.assertFalse(
                oms.has_trading_mode_constraint(("venue_dead_man_switch:",))
            )
            self.assertEqual(oms.capability_mode, OMSCapabilityMode.LIVE)
        finally:
            oms.stop()
            with data_cache._lock:
                data_cache.mark_prices.pop("BTCUSDT", None)
                data_cache.mark_update_times.pop("BTCUSDT", None)

    def test_dead_man_renewal_request_does_not_block_risk_loop(self):
        gateway = DummyGateway()
        started = threading.Event()
        release = threading.Event()

        def slow_renew(symbol, countdown_time_ms):
            gateway.dead_man_requests.append((symbol, countdown_time_ms))
            started.set()
            release.wait(timeout=1.0)
            return types.SimpleNamespace(status_code=200)

        gateway.set_countdown_cancel_all = slow_renew
        config = self.make_config()
        config["oms"]["venue_dead_man_switch"] = {
            "enabled": True,
            "countdown_time_ms": 5000,
            "renewal_interval_sec": 0.05,
            "max_renewal_age_sec": 0.5,
        }
        oms = OMS(DummyEngine(), gateway, config)
        try:
            started_at = time.perf_counter()
            self.assertFalse(oms.request_venue_dead_man_switch_renewal())
            elapsed = time.perf_counter() - started_at

            self.assertLess(elapsed, 0.05)
            self.assertTrue(started.wait(timeout=0.5))
            self.assertTrue(oms.venue_dead_man_renewal_inflight)
            release.set()
            oms._venue_dead_man_renewal_thread.join(timeout=1.0)
            self.assertFalse(oms.venue_dead_man_renewal_inflight)
            self.assertTrue(oms.get_venue_dead_man_switch_snapshot()["valid"])
        finally:
            release.set()
            oms.stop()

    def test_mass_cancel_http_failure_remains_unknown_and_frozen(self):
        gateway = DummyGateway()
        gateway.cancel_all_status_code = 500
        oms = OMS(DummyEngine(), gateway, self.make_config())
        retries = []
        truth_checks = []
        oms._schedule_cancel_all_retry = (
            lambda symbol, source: retries.append((symbol, source)) or True
        )
        oms._on_order_truth_check = (
            lambda reason, suspicious_oid=None: truth_checks.append(reason)
        )
        try:
            acknowledged = oms._cancel_all_orders_unchecked(
                "BTCUSDT",
                source="test_failure",
            )

            self.assertFalse(acknowledged)
            self.assertIn("BTCUSDT", oms.symbol_guards)
            self.assertEqual(retries, [("BTCUSDT", "test_failure")])
            self.assertEqual(truth_checks, ["Mass cancel outcome unknown"])
        finally:
            oms.stop()

    def test_freeze_waits_for_inflight_risk_send_before_mass_cancel(self):
        gateway = DummyGateway()
        send_started = threading.Event()
        release_send = threading.Event()
        timeline = []

        def blocking_send(request, client_oid):
            gateway.sent_orders.append((request, client_oid))
            timeline.append("send_started")
            send_started.set()
            release_send.wait(timeout=2.0)
            timeline.append("send_returned")
            return "ex-blocked"

        def record_cancel_all(symbol):
            gateway.cancelled_symbols.append(symbol)
            timeline.append("cancel_all")
            return DummyResponse(200, {})

        gateway.send_order = blocking_send
        gateway.cancel_all_orders = record_cancel_all
        config = self.make_config()
        config["oms"]["outbound_gate_drain_timeout_sec"] = 1.0
        oms = OMS(DummyEngine(), gateway, config)
        submit_results = []
        try:
            oms.state = LifecycleState.LIVE
            oms._sync_capability_mode("test_live")
            oms.account.force_sync(1000.0, 0.0, available=1000.0)
            oms.validator.validate_params = lambda _intent: (True, "")
            oms.exposure.check_risk = lambda *_args, **_kwargs: (True, "")

            submit_thread = threading.Thread(
                target=lambda: submit_results.append(
                    oms.submit_order(
                        OrderIntent(
                            "alpha",
                            "BTCUSDT",
                            Side.BUY,
                            100.0,
                            0.1,
                        )
                    )
                )
            )
            submit_thread.start()
            self.assertTrue(send_started.wait(timeout=1.0))

            freeze_thread = threading.Thread(
                target=lambda: oms.freeze_system(
                    "test_inflight_race",
                    cancel_active_orders=True,
                )
            )
            freeze_thread.start()
            deadline = time.perf_counter() + 1.0
            while oms.get_outbound_gate_snapshot()["open"] and time.perf_counter() < deadline:
                time.sleep(0.005)

            snapshot = oms.get_outbound_gate_snapshot()
            self.assertFalse(snapshot["open"])
            self.assertEqual(snapshot["risk_sends_inflight"], 1)
            self.assertNotIn("cancel_all", timeline)

            blocked = oms.submit_order(
                OrderIntent("alpha", "BTCUSDT", Side.BUY, 101.0, 0.1)
            )
            self.assertFalse(blocked.accepted)
            self.assertEqual(len(gateway.sent_orders), 1)

            release_send.set()
            submit_thread.join(timeout=1.0)
            freeze_thread.join(timeout=1.0)

            self.assertFalse(submit_thread.is_alive())
            self.assertFalse(freeze_thread.is_alive())
            self.assertEqual(timeline, ["send_started", "send_returned", "cancel_all"])
            self.assertTrue(submit_results[0].accepted)
            self.assertEqual(oms.state, LifecycleState.FROZEN)
        finally:
            release_send.set()
            oms.stop()

    def test_symbol_freeze_cancels_twice_and_waits_only_for_target_symbol(self):
        gateway = DummyGateway()
        send_started = {
            "BTCUSDT": threading.Event(),
            "ETHUSDT": threading.Event(),
        }
        release_send = {
            "BTCUSDT": threading.Event(),
            "ETHUSDT": threading.Event(),
        }
        timeline = []
        timeline_lock = threading.Lock()

        def blocking_send(request, client_oid):
            symbol = request.symbol
            with timeline_lock:
                timeline.append(f"send_started:{symbol}")
            send_started[symbol].set()
            release_send[symbol].wait(timeout=2.0)
            with timeline_lock:
                timeline.append(f"send_returned:{symbol}")
            return f"ex-{client_oid}"

        def record_cancel_all(symbol):
            with timeline_lock:
                timeline.append(f"cancel_all:{symbol}")
            return DummyResponse(200, {})

        gateway.send_order = blocking_send
        gateway.cancel_all_orders = record_cancel_all
        config = self.make_config()
        config["symbols"] = ["BTCUSDT", "ETHUSDT"]
        config["oms"]["outbound_gate_drain_timeout_sec"] = 1.0
        oms = OMS(DummyEngine(), gateway, config)
        submit_threads = []
        freeze_thread = None
        try:
            oms.state = LifecycleState.LIVE
            oms._sync_capability_mode("test_live")
            oms.account.force_sync(1000.0, 0.0, available=1000.0)
            oms.validator.validate_params = lambda _intent: (True, "")
            oms.exposure.check_risk = lambda *_args, **_kwargs: (True, "")

            for symbol in ("BTCUSDT", "ETHUSDT"):
                thread = threading.Thread(
                    target=lambda target=symbol: oms.submit_order(
                        OrderIntent(
                            "alpha",
                            target,
                            Side.BUY,
                            100.0,
                            0.1,
                        )
                    )
                )
                submit_threads.append(thread)
                thread.start()
            self.assertTrue(send_started["BTCUSDT"].wait(timeout=1.0))
            self.assertTrue(send_started["ETHUSDT"].wait(timeout=1.0))

            freeze_thread = threading.Thread(
                target=lambda: oms.freeze_symbol(
                    "BTCUSDT",
                    "latency:test",
                    cancel_active_orders=True,
                )
            )
            freeze_thread.start()

            deadline = time.perf_counter() + 1.0
            while time.perf_counter() < deadline:
                with timeline_lock:
                    if timeline.count("cancel_all:BTCUSDT") >= 1:
                        break
                time.sleep(0.005)
            with timeline_lock:
                self.assertEqual(timeline.count("cancel_all:BTCUSDT"), 1)
                self.assertNotIn("cancel_all:ETHUSDT", timeline)
                self.assertNotIn("send_returned:BTCUSDT", timeline)
            self.assertTrue(freeze_thread.is_alive())

            release_send["BTCUSDT"].set()
            freeze_thread.join(timeout=1.0)
            self.assertFalse(freeze_thread.is_alive())
            self.assertTrue(submit_threads[1].is_alive())
            with timeline_lock:
                self.assertEqual(timeline.count("cancel_all:BTCUSDT"), 2)
                self.assertNotIn("cancel_all:ETHUSDT", timeline)
                first_cancel = timeline.index("cancel_all:BTCUSDT")
                send_returned = timeline.index("send_returned:BTCUSDT")
                second_cancel = len(timeline) - 1 - timeline[::-1].index(
                    "cancel_all:BTCUSDT"
                )
                self.assertLess(first_cancel, send_returned)
                self.assertLess(send_returned, second_cancel)

            release_send["ETHUSDT"].set()
            for thread in submit_threads:
                thread.join(timeout=1.0)
                self.assertFalse(thread.is_alive())
        finally:
            for event in release_send.values():
                event.set()
            if freeze_thread is not None:
                freeze_thread.join(timeout=1.0)
            for thread in submit_threads:
                thread.join(timeout=1.0)
            oms.stop()

    def test_shutdown_verified_cancel_discovers_off_config_orders(self):
        gateway = DummyGateway()
        gateway.open_orders = [
            {
                "symbol": "OLDUSDT",
                "orderId": 77,
                "clientOrderId": "legacy-order",
                "side": "BUY",
            }
        ]

        def cancel_and_remove(symbol):
            gateway.cancelled_symbols.append(symbol)
            gateway.open_orders = [
                order
                for order in gateway.open_orders
                if order.get("symbol") != symbol
            ]
            return DummyResponse(200, {})

        gateway.cancel_all_orders = cancel_and_remove
        oms = OMS(DummyEngine(), gateway, self.make_config())
        try:
            oms.state = LifecycleState.LIVE
            oms._sync_capability_mode("test_live")

            self.assertTrue(oms.begin_shutdown("test_shutdown"))
            self.assertTrue(
                oms.cancel_all_account_orders_verified(
                    gateway,
                    timeout_sec=1.0,
                    settle_interval_sec=0.01,
                )
            )

            self.assertIn("OLDUSDT", gateway.cancelled_symbols)
            self.assertTrue(
                oms.get_outbound_gate_snapshot()["shutdown_cancel_verified"]
            )
            self.assertEqual(gateway.open_orders, [])
        finally:
            oms.stop(clean_shutdown=True, reason="test_shutdown")

    def test_shutdown_requires_consecutive_empty_account_snapshots(self):
        gateway = DummyGateway()
        snapshots = iter([[], None, [], []])
        query_count = 0

        def get_open_orders():
            nonlocal query_count
            query_count += 1
            return next(snapshots)

        gateway.get_open_orders = get_open_orders
        oms = OMS(DummyEngine(), gateway, self.make_config())
        try:
            oms.state = LifecycleState.LIVE
            oms._sync_capability_mode("test_live")
            self.assertTrue(oms.begin_shutdown("test_snapshot_quorum"))

            verified = oms.cancel_all_account_orders_verified(
                gateway,
                timeout_sec=1.0,
                settle_interval_sec=0.01,
            )

            self.assertTrue(verified)
            self.assertEqual(query_count, 4)
        finally:
            oms.stop(clean_shutdown=True, reason="test_snapshot_quorum")

    def test_stale_venue_recovery_epoch_cannot_clear_new_fault(self):
        oms = OMS(DummyEngine(), DummyGateway(), self.make_config())
        try:
            oms.state = LifecycleState.LIVE
            oms._sync_capability_mode("test_live")
            oms.freeze_venue(
                "BINANCE",
                "system_health:WS_TRANSPORT_DROP:first",
                cancel_active_orders=False,
            )
            stale_epoch = oms.get_venue_freeze_epoch("BINANCE")
            oms.freeze_venue(
                "BINANCE",
                "system_health:WS_TRANSPORT_DROP:second",
                cancel_active_orders=False,
            )

            self.assertFalse(
                oms.clear_venue_freeze(
                    "BINANCE",
                    reason="stale_recovery",
                    expected_epoch=stale_epoch,
                )
            )
            self.assertEqual(
                oms.get_venue_freeze_reason("BINANCE"),
                "system_health:WS_TRANSPORT_DROP:second",
            )
        finally:
            oms.stop()

    def test_venue_recovery_reason_must_match_same_epoch_guard(self):
        oms = OMS(DummyEngine(), DummyGateway(), self.make_config())
        try:
            oms.state = LifecycleState.LIVE
            oms._sync_capability_mode("test_live")
            epoch = oms.freeze_venue(
                "BINANCE",
                "event_engine_backlog:market:first",
                cancel_active_orders=False,
            )

            self.assertFalse(
                oms.clear_venue_freeze(
                    "BINANCE",
                    reason="wrong owner recovery",
                    expected_epoch=epoch,
                    expected_reason="processing_lag:first",
                )
            )
            self.assertEqual(
                oms.get_venue_freeze_reason("BINANCE"),
                "event_engine_backlog:market:first",
            )
        finally:
            oms.stop()

    def test_old_recovery_context_cannot_verify_new_transport_epoch(self):
        oms = OMS(DummyEngine(), DummyGateway(), self.make_config())
        try:
            first_reason = "system_health:WS_TRANSPORT_DROP:first"
            first_epoch = oms.freeze_venue(
                "BINANCE",
                first_reason,
                cancel_active_orders=False,
            )
            oms.freeze_venue(
                "BINANCE",
                "system_health:WS_TRANSPORT_DROP:second",
                cancel_active_orders=False,
            )
            requests = []
            oms.trigger_reconcile = (
                lambda *args, **kwargs: requests.append((args, kwargs)) or True
            )

            self.assertFalse(
                oms.request_venue_recovery_verification(
                    "BINANCE",
                    expected_owner="system_health:transport",
                    expected_epoch=first_epoch,
                    expected_reason=first_reason,
                )
            )
            self.assertEqual(requests, [])
        finally:
            oms.stop()

    def test_transport_recovery_clears_only_exact_transport_owner(self):
        oms = OMS(DummyEngine(), DummyGateway(), self.make_config())
        try:
            lag_reason = "event_engine_backlog:market:first"
            oms.freeze_venue(
                "BINANCE",
                lag_reason,
                cancel_active_orders=False,
            )
            transport_reason = "system_health:WS_TRANSPORT_DROP:first"
            transport_epoch = oms.freeze_venue(
                "BINANCE",
                transport_reason,
                cancel_active_orders=False,
            )
            oms.state = LifecycleState.LIVE
            oms._sync_capability_mode("truth_verified")

            self.assertTrue(
                oms._complete_venue_recovery_verification(
                    "BINANCE",
                    transport_epoch,
                    "system_health:transport",
                    transport_reason,
                )
            )
            self.assertEqual(
                oms.get_venue_freeze_owners("BINANCE"),
                {
                    "event_engine_backlog": {
                        "reason": lag_reason,
                        "epoch": 1,
                    }
                },
            )
        finally:
            oms.stop()

    def test_orderbook_recovery_token_cannot_clear_unrelated_symbol_guard(self):
        oms = OMS(DummyEngine(), DummyGateway(), self.make_config())
        try:
            oms.state = LifecycleState.LIVE
            oms._sync_capability_mode("test_live")
            oms.freeze_symbol(
                "BTCUSDT",
                "truth_plane:position_mismatch",
                cancel_active_orders=False,
            )

            self.assertFalse(
                oms.clear_orderbook_freeze(
                    "BTCUSDT",
                    "17",
                    reason="system_health:ORDERBOOK_RESYNCED:17",
                )
            )
            self.assertEqual(
                oms.get_symbol_freeze_reason("BTCUSDT"),
                "truth_plane:position_mismatch",
            )

            oms.freeze_symbol(
                "BTCUSDT",
                "system_health:FATAL_GAP:18",
                cancel_active_orders=False,
            )
            self.assertTrue(
                oms.clear_orderbook_freeze(
                    "BTCUSDT",
                    "18",
                    reason="system_health:ORDERBOOK_RESYNCED:18",
                )
            )
            self.assertEqual(
                oms.get_symbol_freeze_reason("BTCUSDT"),
                "truth_plane:position_mismatch",
            )
        finally:
            oms.stop()

    def test_bootstrap_dead_man_failure_halts_and_retries_mass_cancel(self):
        gateway = DummyGateway()
        gateway.dead_man_status_code = 500
        config = self.make_config()
        config["oms"]["venue_dead_man_switch"] = {
            "enabled": True,
            "countdown_time_ms": 1000,
            "renewal_interval_sec": 0.05,
            "max_renewal_age_sec": 0.10,
            "recovery_checks": 2,
        }
        oms = OMS(DummyEngine(), gateway, config)
        try:
            self.assertFalse(oms.bootstrap())
            self.assertEqual(oms.state, LifecycleState.HALTED)
            self.assertTrue(oms.manual_rearm_required)
            self.assertEqual(gateway.dead_man_requests, [("BTCUSDT", 1000)])
            self.assertIn("BTCUSDT", gateway.cancelled_symbols)

            gateway.cancelled_symbols.clear()
            oms.halt_system("dead_man_still_unavailable")
            self.assertEqual(gateway.cancelled_symbols, ["BTCUSDT"])
        finally:
            oms.stop()

    def test_message_budget_reserves_capacity_for_reduce_and_cancel(self):
        gateway = DummyGateway()
        gateway.cancel_response = DummyResponse(200, {})
        config = self.make_config()
        config["oms"]["outbound_message_budget"] = {
            "enabled": True,
            "window_sec": 1.0,
            "max_total_messages_per_window": 4,
            "max_new_orders_per_window": 10,
            "max_reduce_orders_per_window": 4,
            "max_cancel_messages_per_window": 4,
            "reserved_risk_messages_per_window": 2,
        }
        oms = OMS(DummyEngine(), gateway, config)
        try:
            oms.state = LifecycleState.LIVE
            oms.exposure.force_sync("BTCUSDT", 1.0, 100.0)
            oms.account.force_sync(1000.0, 0.0, available=1000.0)
            data_cache.update_mark_price(
                types.SimpleNamespace(
                    symbol="BTCUSDT",
                    mark_price=100.0,
                    datetime=None,
                )
            )

            first = oms.submit_order(
                OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 0.1)
            )
            second = oms.submit_order(
                OrderIntent("alpha", "BTCUSDT", Side.BUY, 101.0, 0.1)
            )
            blocked = oms.submit_order(
                OrderIntent("alpha", "BTCUSDT", Side.BUY, 102.0, 0.1)
            )

            self.assertTrue(first.accepted)
            self.assertTrue(second.accepted)
            self.assertFalse(blocked.accepted)
            self.assertTrue(
                blocked.reason.startswith(
                    "outbound_message_budget:risk_capacity_reserved:"
                )
            )

            reduction = oms.submit_order(
                OrderIntent(
                    "alpha",
                    "BTCUSDT",
                    Side.SELL,
                    100.0,
                    0.5,
                    order_type="MARKET",
                    time_in_force="IOC",
                    reduce_only=True,
                    policy=ExecutionPolicy.AGGRESSIVE,
                )
            )
            self.assertTrue(reduction.accepted)
            self.assertTrue(oms.cancel_order(first.client_oid))

            snapshot = oms.get_outbound_message_budget_snapshot()
            self.assertEqual(snapshot["counts"][OMS.OUTBOUND_NEW_ORDER], 2)
            self.assertEqual(snapshot["counts"][OMS.OUTBOUND_REDUCE_ORDER], 1)
            self.assertEqual(snapshot["counts"][OMS.OUTBOUND_CANCEL], 1)
            self.assertEqual(len(gateway.cancel_requests), 1)
        finally:
            oms.stop()
            with data_cache._lock:
                data_cache.mark_prices.pop("BTCUSDT", None)
                data_cache.mark_update_times.pop("BTCUSDT", None)

    def test_strategy_budget_reservation_is_atomic_across_concurrent_submits(self):
        gateway = DummyGateway()
        config = self.make_config()
        config["oms"]["duplicate_intent_window_sec"] = 0.0
        config["risk"]["strategy_risk_budgets"] = {
            "enabled": True,
            "require_explicit_strategy": True,
            "budgets": {
                "alpha": {
                    "max_gross_notional": 500.0,
                    "max_symbol_notional": 500.0,
                }
            },
        }
        oms = OMS(DummyEngine(), gateway, config)
        results = []
        result_lock = threading.Lock()
        try:
            oms.state = LifecycleState.LIVE
            oms.account.force_sync(1000.0, 0.0, available=1000.0)
            data_cache.update_mark_price(
                types.SimpleNamespace(
                    symbol="BTCUSDT",
                    mark_price=100.0,
                    datetime=None,
                )
            )

            def submit(index):
                result = oms.submit_order(
                    OrderIntent(
                        "alpha",
                        "BTCUSDT",
                        Side.BUY,
                        100.0 + index * 0.01,
                        1.0,
                    )
                )
                with result_lock:
                    results.append(result)

            workers = [
                threading.Thread(target=submit, args=(index,))
                for index in range(20)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()

            accepted = [result for result in results if result.accepted]
            rejected = [result for result in results if not result.accepted]
            self.assertEqual(len(accepted), 5)
            self.assertEqual(len(rejected), 15)
            self.assertTrue(
                all(
                    result.reason.startswith("strategy_budget_limit:")
                    for result in rejected
                )
            )
            snapshot = oms.get_strategy_risk_budget_snapshot()
            self.assertEqual(
                snapshot["ledger"]["alpha"]["BTCUSDT"]["open_buy_qty"],
                5.0,
            )
        finally:
            oms.stop()
            with data_cache._lock:
                data_cache.mark_prices.pop("BTCUSDT", None)
                data_cache.mark_update_times.pop("BTCUSDT", None)

    def test_unconfigured_strategy_is_rejected_but_reduce_only_remains_allowed(self):
        gateway = DummyGateway()
        config = self.make_config()
        config["risk"]["strategy_risk_budgets"] = {
            "enabled": True,
            "require_explicit_strategy": True,
            "budgets": {
                "alpha": {
                    "max_gross_notional": 500.0,
                    "max_symbol_notional": 500.0,
                }
            },
        }
        oms = OMS(DummyEngine(), gateway, config)
        try:
            oms.state = LifecycleState.LIVE
            oms.exposure.force_sync("BTCUSDT", 1.0, 100.0)
            oms.exposure.on_strategy_fill(
                "alpha",
                "BTCUSDT",
                Side.BUY,
                1.0,
                100.0,
            )
            oms.account.force_sync(1000.0, 0.0, available=1000.0)
            data_cache.update_mark_price(
                types.SimpleNamespace(
                    symbol="BTCUSDT",
                    mark_price=100.0,
                    datetime=None,
                )
            )

            opening = oms.submit_order(
                OrderIntent("beta", "BTCUSDT", Side.BUY, 100.0, 0.1)
            )
            self.assertFalse(opening.accepted)
            self.assertEqual(opening.reason, "strategy_budget_unconfigured:beta")

            reduction = oms.submit_order(
                OrderIntent(
                    "beta",
                    "BTCUSDT",
                    Side.SELL,
                    100.0,
                    0.5,
                    order_type="MARKET",
                    time_in_force="IOC",
                    reduce_only=True,
                    policy=ExecutionPolicy.AGGRESSIVE,
                )
            )
            self.assertTrue(reduction.accepted)
        finally:
            oms.stop()
            with data_cache._lock:
                data_cache.mark_prices.pop("BTCUSDT", None)
                data_cache.mark_update_times.pop("BTCUSDT", None)

    def test_strategy_gross_budget_aggregates_across_symbols(self):
        gateway = DummyGateway()
        config = self.make_config()
        config["symbols"] = ["BTCUSDT", "ETHUSDT"]
        config["risk"]["strategy_risk_budgets"] = {
            "enabled": True,
            "require_explicit_strategy": True,
            "budgets": {
                "alpha": {
                    "max_gross_notional": 500.0,
                    "max_symbol_notional": 500.0,
                }
            },
        }
        oms = OMS(DummyEngine(), gateway, config)
        try:
            oms.state = LifecycleState.LIVE
            oms.account.force_sync(1000.0, 0.0, available=1000.0)
            for symbol in config["symbols"]:
                data_cache.update_mark_price(
                    types.SimpleNamespace(
                        symbol=symbol,
                        mark_price=100.0,
                        datetime=None,
                    )
                )

            btc = oms.submit_order(
                OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 3.0)
            )
            eth = oms.submit_order(
                OrderIntent("alpha", "ETHUSDT", Side.BUY, 100.0, 3.0)
            )

            self.assertTrue(btc.accepted)
            self.assertFalse(eth.accepted)
            self.assertIn("Strategy Gross Exposure", eth.reason)
        finally:
            oms.stop()
            with data_cache._lock:
                for symbol in config["symbols"]:
                    data_cache.mark_prices.pop(symbol, None)
                    data_cache.mark_update_times.pop(symbol, None)

    def test_strategy_fill_attribution_replays_from_durable_execution_log(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            journal_path = os.path.join(temp_dir, "oms_journal.jsonl")
            config = self.make_journaled_config(journal_path)
            config["risk"]["strategy_risk_budgets"] = {
                "enabled": True,
                "require_explicit_strategy": True,
                "budgets": {
                    "alpha": {
                        "max_gross_notional": 500.0,
                        "max_symbol_notional": 500.0,
                    }
                },
            }
            gateway = DummyGateway()
            oms = OMS(DummyEngine(), gateway, config)
            recovered = None
            try:
                oms.state = LifecycleState.LIVE
                oms.account.force_sync(1000.0, 0.0, available=1000.0)
                data_cache.update_mark_price(
                    types.SimpleNamespace(
                        symbol="BTCUSDT",
                        mark_price=100.0,
                        datetime=None,
                    )
                )
                submitted = oms.submit_order(
                    OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 1.0)
                )
                self.assertTrue(submitted.accepted)
                oms._apply_event(
                    Event(
                        EVENT_EXCHANGE_ORDER_UPDATE,
                        ExchangeOrderUpdate(
                            client_oid=submitted.client_oid,
                            exchange_oid="ex-order-1",
                            symbol="BTCUSDT",
                            status="FILLED",
                            filled_qty=1.0,
                            filled_price=100.0,
                            cum_filled_qty=1.0,
                            update_time=1000.0,
                            seq=1,
                            trade_id=901,
                        ),
                    )
                )
                self.assertEqual(
                    oms.exposure.strategy_net_positions[("alpha", "BTCUSDT")],
                    1.0,
                )
                oms.stop()

                recovered = OMS(DummyEngine(), DummyGateway(), config)
                self.assertEqual(
                    recovered.exposure.strategy_net_positions[
                        ("alpha", "BTCUSDT")
                    ],
                    1.0,
                )
                self.assertEqual(
                    recovered.exposure.strategy_avg_prices[
                        ("alpha", "BTCUSDT")
                    ],
                    100.0,
                )
            finally:
                if recovered is not None:
                    recovered.stop()
                elif getattr(oms, "_stopped", False) is False:
                    oms.stop()
                with data_cache._lock:
                    data_cache.mark_prices.pop("BTCUSDT", None)
                    data_cache.mark_update_times.pop("BTCUSDT", None)

    def test_unattributed_position_residual_is_assigned_to_recovery_bucket(self):
        owner = OMS(DummyEngine(), DummyGateway(), self.make_config())
        exposure = owner.exposure
        try:
            exposure.on_strategy_fill(
                "alpha",
                "BTCUSDT",
                Side.BUY,
                1.0,
                100.0,
            )
            residual = exposure.reconcile_strategy_position(
                "BTCUSDT",
                account_position=2.0,
                price=101.0,
            )
            self.assertEqual(residual, 1.0)
            self.assertEqual(
                exposure.strategy_net_positions[("exchange_recovery", "BTCUSDT")],
                1.0,
            )

            residual = exposure.reconcile_strategy_position(
                "BTCUSDT",
                account_position=0.0,
                price=102.0,
            )
            self.assertEqual(residual, -1.0)
        finally:
            owner.stop()

    def test_execution_without_local_order_replays_into_recovery_strategy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            journal_path = os.path.join(temp_dir, "oms_journal.jsonl")
            config = self.make_journaled_config(journal_path)
            journal = OMSJournal(config)
            journal.append(
                "execution_record",
                {
                    "execution_id": "BINANCE:BTCUSDT:external-1",
                    "venue": "BINANCE",
                    "client_oid": "",
                    "exchange_oid": "external-order",
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "fill_qty": 0.25,
                    "fill_price": 100.0,
                    "cum_filled_qty": 0.25,
                    "exchange_status": "FILLED",
                    "exchange_time": 1000.0,
                    "trade_id": 902,
                },
            )

            recovered = OMS(DummyEngine(), DummyGateway(), config)
            try:
                self.assertEqual(
                    recovered.exposure.strategy_net_positions[
                        ("exchange_recovery", "BTCUSDT")
                    ],
                    0.25,
                )
                self.assertIn(
                    "BINANCE:BTCUSDT:external-1",
                    recovered.execution_ids,
                )
            finally:
                recovered.stop()

    def test_message_budget_is_atomic_under_concurrent_submits(self):
        gateway = DummyGateway()
        config = self.make_config()
        config["oms"]["duplicate_intent_window_ms"] = 0.0
        config["oms"]["outbound_message_budget"] = {
            "enabled": True,
            "window_sec": 1.0,
            "max_total_messages_per_window": 100,
            "max_new_orders_per_window": 5,
            "max_reduce_orders_per_window": 10,
            "max_cancel_messages_per_window": 20,
            "reserved_risk_messages_per_window": 0,
        }
        oms = OMS(DummyEngine(), gateway, config)
        results = []
        barrier = threading.Barrier(20)
        try:
            oms.state = LifecycleState.LIVE
            oms.account.force_sync(1000.0, 0.0, available=1000.0)
            data_cache.update_mark_price(
                types.SimpleNamespace(
                    symbol="BTCUSDT",
                    mark_price=100.0,
                    datetime=None,
                )
            )

            def submit(index):
                barrier.wait()
                results.append(
                    oms.submit_order(
                        OrderIntent(
                            f"strategy-{index}",
                            "BTCUSDT",
                            Side.BUY,
                            100.0 + index * 0.01,
                            0.01,
                        )
                    )
                )

            workers = [
                threading.Thread(target=submit, args=(index,))
                for index in range(20)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=5.0)

            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(sum(result.accepted for result in results), 5)
            self.assertEqual(len(gateway.sent_orders), 5)
            self.assertEqual(
                oms.get_outbound_message_budget_snapshot()["counts"][
                    OMS.OUTBOUND_NEW_ORDER
                ],
                5,
            )
        finally:
            oms.stop()
            with data_cache._lock:
                data_cache.mark_prices.pop("BTCUSDT", None)
                data_cache.mark_update_times.pop("BTCUSDT", None)

    def test_cancel_is_deferred_and_retried_when_budget_is_temporarily_full(self):
        gateway = DummyGateway()
        gateway.cancel_response = DummyResponse(200, {})
        config = self.make_config()
        config["oms"]["outbound_message_budget"] = {
            "enabled": True,
            "window_sec": 0.05,
            "max_total_messages_per_window": 1,
            "max_new_orders_per_window": 1,
            "max_reduce_orders_per_window": 1,
            "max_cancel_messages_per_window": 1,
            "reserved_risk_messages_per_window": 0,
        }
        oms = OMS(DummyEngine(), gateway, config)
        try:
            oms.state = LifecycleState.LIVE
            oms.account.force_sync(1000.0, 0.0, available=1000.0)
            data_cache.update_mark_price(
                types.SimpleNamespace(
                    symbol="BTCUSDT",
                    mark_price=100.0,
                    datetime=None,
                )
            )
            submitted = oms.submit_order(
                OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 0.1)
            )
            self.assertTrue(submitted.accepted)

            self.assertTrue(oms.cancel_order(submitted.client_oid))
            self.assertEqual(gateway.cancel_requests, [])

            deadline = time.time() + 1.0
            while not gateway.cancel_requests and time.time() < deadline:
                time.sleep(0.01)

            self.assertEqual(len(gateway.cancel_requests), 1)
            self.assertNotIn(
                submitted.client_oid,
                oms._deferred_cancel_oids,
            )
        finally:
            oms.stop()
            with data_cache._lock:
                data_cache.mark_prices.pop("BTCUSDT", None)
                data_cache.mark_update_times.pop("BTCUSDT", None)

    def test_local_stp_rejects_crossing_order_and_sets_exchange_guard(self):
        gateway = DummyGateway()
        config = self.make_config()
        config["oms"]["self_trade_prevention"] = {
            "enabled": True,
            "local_cross_check": True,
            "exchange_mode": "EXPIRE_MAKER",
        }
        oms = OMS(DummyEngine(), gateway, config)
        try:
            oms.state = LifecycleState.LIVE
            oms.account.force_sync(1000.0, 0.0, available=1000.0)
            data_cache.update_mark_price(
                types.SimpleNamespace(
                    symbol="BTCUSDT",
                    mark_price=100.0,
                    datetime=None,
                )
            )

            resting_bid = oms.submit_order(
                OrderIntent("maker", "BTCUSDT", Side.BUY, 100.0, 0.1)
            )
            crossing_ask = oms.submit_order(
                OrderIntent("taker", "BTCUSDT", Side.SELL, 99.0, 0.1)
            )
            non_crossing_ask = oms.submit_order(
                OrderIntent("maker-2", "BTCUSDT", Side.SELL, 101.0, 0.1)
            )

            self.assertTrue(resting_bid.accepted)
            self.assertFalse(crossing_ask.accepted)
            self.assertTrue(
                crossing_ask.reason.startswith(
                    "self_trade_prevention:crossing_active_order:"
                )
            )
            self.assertTrue(non_crossing_ask.accepted)
            self.assertEqual(len(gateway.sent_orders), 2)
            self.assertTrue(
                all(
                    request.self_trade_prevention_mode == "EXPIRE_MAKER"
                    for request, _client_oid in gateway.sent_orders
                )
            )
        finally:
            oms.stop()
            with data_cache._lock:
                data_cache.mark_prices.pop("BTCUSDT", None)
                data_cache.mark_update_times.pop("BTCUSDT", None)

    def test_invalid_exchange_stp_mode_fails_during_oms_construction(self):
        config = self.make_config()
        config["oms"]["self_trade_prevention"] = {
            "enabled": True,
            "exchange_mode": "NONE",
        }

        with self.assertRaisesRegex(ValueError, "Unsupported Binance STP mode"):
            OMS(DummyEngine(), DummyGateway(), config)

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
            self.assertTrue(client_oid.startswith("EMG_"))
            self.assertLessEqual(len(client_oid), 36)
            self.assertEqual(sent_req.order_type, "MARKET")
            self.assertTrue(sent_req.reduce_only)
            self.assertEqual(sent_req.side, "SELL")
            self.assertTrue(oms.orders[client_oid].intent.reduce_only)
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
                self.assertFalse(recovered.rebuild_summary["clean_shutdown"])
            finally:
                recovered.stop()

    def test_restart_preserves_only_active_venue_guard_owners(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            journal_path = os.path.join(tmpdir, "oms_journal.jsonl")
            config = self.make_journaled_config(journal_path)

            oms = OMS(DummyEngine(), DummyGateway(), config)
            lag_epoch = oms.freeze_venue(
                "BINANCE",
                "event_engine_backlog:market:first",
                cancel_active_orders=False,
            )
            transport_epoch = oms.freeze_venue(
                "BINANCE",
                "system_health:WS_TRANSPORT_DROP:first",
                cancel_active_orders=False,
            )
            self.assertTrue(
                oms.clear_venue_freeze(
                    "BINANCE",
                    reason="processing recovered",
                    expected_owner="event_engine_backlog",
                    expected_epoch=lag_epoch,
                    expected_reason="event_engine_backlog:market:first",
                )
            )
            self.assertEqual(
                set(oms.get_venue_freeze_owners("BINANCE")),
                {"system_health:transport"},
            )
            oms.stop()

            recovered = OMS(DummyEngine(), DummyGateway(), config)
            try:
                self.assertEqual(
                    recovered.get_venue_freeze_owners("BINANCE"),
                    {
                        "system_health:transport": {
                            "reason": "system_health:WS_TRANSPORT_DROP:first",
                            "epoch": transport_epoch,
                        }
                    },
                )
                self.assertEqual(
                    recovered.get_venue_freeze_reason("BINANCE"),
                    "system_health:WS_TRANSPORT_DROP:first",
                )
            finally:
                recovered.stop()

    def test_guard_replay_ignores_stale_freeze_and_clear_epochs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            journal_path = os.path.join(tmpdir, "oms_journal.jsonl")
            config = self.make_journaled_config(journal_path)

            oms = OMS(DummyEngine(), DummyGateway(), config)
            oms._audit(
                "symbol_frozen",
                symbol="BTCUSDT",
                reason="latency:new",
                owner="latency",
                epoch=5,
            )
            oms._audit(
                "symbol_frozen",
                symbol="BTCUSDT",
                reason="latency:old",
                owner="latency",
                epoch=4,
            )
            oms._audit(
                "symbol_unfrozen",
                symbol="BTCUSDT",
                previous_reason="latency:old",
                owner="latency",
                epoch=4,
            )
            oms._audit(
                "venue_frozen",
                venue="BINANCE",
                reason="system_health:WS_TRANSPORT_DROP:new",
                owner="system_health:transport",
                epoch=9,
            )
            oms._audit(
                "venue_frozen",
                venue="BINANCE",
                reason="system_health:WS_TRANSPORT_DROP:old",
                owner="system_health:transport",
                epoch=8,
            )
            oms._audit(
                "venue_unfrozen",
                venue="BINANCE",
                previous_reason="system_health:WS_TRANSPORT_DROP:old",
                owner="system_health:transport",
                epoch=8,
            )
            oms.stop()

            recovered = OMS(DummyEngine(), DummyGateway(), config)
            try:
                self.assertEqual(
                    recovered.get_symbol_freeze_owners("BTCUSDT"),
                    {
                        "latency": {
                            "reason": "latency:new",
                            "epoch": 5,
                        }
                    },
                )
                self.assertEqual(
                    recovered.get_venue_freeze_owners("BINANCE"),
                    {
                        "system_health:transport": {
                            "reason": (
                                "system_health:WS_TRANSPORT_DROP:new"
                            ),
                            "epoch": 9,
                        }
                    },
                )
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

                recovered.state = LifecycleState.RECONCILING
                recovered._sync_capability_mode("test_reconcile")
                recovered._execute_reconcile(None)

                self.assertEqual(recovered.state, LifecycleState.LIVE)
                self.assertEqual(recovered.get_strategy_freeze_reason("alpha"), "")
                self.assertTrue(recovered.can_submit_for_strategy("alpha", "BTCUSDT"))
            finally:
                recovered.stop()

    def test_symbol_guard_owner_registry_survives_journal_replay(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            journal_path = os.path.join(tmpdir, "oms_journal.jsonl")
            config = self.make_journaled_config(journal_path)

            oms = OMS(DummyEngine(), DummyGateway(), config)
            oms.freeze_symbol(
                "BTCUSDT",
                "system_health:FATAL_GAP:77",
                cancel_active_orders=False,
            )
            latency_epoch = oms.freeze_symbol(
                "BTCUSDT",
                "latency:300ms>100ms",
                cancel_active_orders=False,
            )
            oms.clear_symbol_freeze(
                "BTCUSDT",
                reason="latency recovered",
                expected_epoch=latency_epoch,
                expected_reason="latency:300ms>100ms",
            )
            oms.stop()

            recovered = OMS(DummyEngine(), DummyGateway(), config)
            try:
                owners = recovered.get_symbol_freeze_owners("BTCUSDT")
                self.assertEqual(
                    set(owners),
                    {"system_health:orderbook"},
                )
                self.assertEqual(
                    owners["system_health:orderbook"]["reason"],
                    "system_health:FATAL_GAP:77",
                )
                self.assertEqual(recovered.state, LifecycleState.FROZEN)
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
        self.incomes = []
        self.income_queries = []

    def get_account_info(self):
        return self.account

    def get_all_positions(self):
        return self.positions

    def get_open_orders(self):
        return self.open_orders

    def get_income_history(self, **kwargs):
        self.income_queries.append(dict(kwargs))
        return list(self.incomes)


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

    def test_truth_monitor_api_recovery_requires_clean_poll_quorum(self):
        oms = OMS(DummyEngine(), DummyGateway(), self.make_config())
        provider = DummyTruthProvider()
        monitor = TruthMonitor(oms, provider, self.make_config(), start_thread=False)
        try:
            oms.state = LifecycleState.LIVE
            provider.account = None
            provider.positions = None
            provider.open_orders = None
            monitor.poll_once()
            monitor.poll_once()
            self.assertTrue(
                oms.get_venue_freeze_reason("BINANCE").startswith(
                    "truth_plane:api_unreachable"
                )
            )

            provider.account = {
                "totalWalletBalance": "1000",
                "totalInitialMargin": "0",
            }
            provider.positions = []
            provider.open_orders = []
            monitor.poll_once()
            self.assertTrue(oms.get_venue_freeze_reason("BINANCE"))

            monitor.poll_once()
            self.assertEqual(oms.get_venue_freeze_reason("BINANCE"), "")
        finally:
            oms.stop()

    def test_truth_monitor_syncs_exchange_maintenance_margin_truth(self):
        engine = DummyEngine()
        config = self.make_config()
        oms = OMS(engine, DummyGateway(), config)
        provider = DummyTruthProvider()
        provider.account.update(
            {
                "totalMaintMargin": "250.0",
                "totalMarginBalance": "500.0",
            }
        )
        monitor = TruthMonitor(oms, provider, config, start_thread=False)
        try:
            oms.state = LifecycleState.LIVE

            self.assertTrue(monitor.poll_once())

            self.assertTrue(oms.account.margin_snapshot_synced)
            self.assertAlmostEqual(oms.account.maintenance_margin, 250.0)
            self.assertAlmostEqual(oms.account.margin_balance, 500.0)
            self.assertAlmostEqual(oms.account.maintenance_margin_ratio, 0.5)
            account_events = [event for event in engine.events if event.type == EVENT_ACCOUNT_UPDATE]
            self.assertTrue(account_events)
            self.assertTrue(account_events[-1].data.margin_snapshot_synced)
        finally:
            oms.stop()

    def test_truth_monitor_syncs_external_cash_flow_through_independent_provider(self):
        engine = DummyEngine()
        config = self.make_config()
        config["risk"]["cash_flow_truth"] = {
            "enabled": True,
            "require_snapshot": True,
            "poll_interval_sec": 30.0,
            "external_income_types": ["TRANSFER"],
        }
        oms = OMS(engine, DummyGateway(), config)
        provider = DummyTruthProvider()
        provider.incomes = [
            {
                "incomeType": "TRANSFER",
                "tranId": "truth-plane-transfer-1",
                "asset": "USDT",
                "income": "125.0",
                "time": int(time.time() * 1000),
            }
        ]
        monitor = TruthMonitor(oms, provider, config, start_thread=False)
        try:
            oms.state = LifecycleState.LIVE

            self.assertTrue(monitor.poll_once())
            self.assertAlmostEqual(oms.account.external_cash_flow_total, 125.0)
            self.assertTrue(oms.account.cash_flow_snapshot_synced)
            self.assertEqual(len(provider.income_queries), 1)

            self.assertTrue(monitor.poll_once())
            self.assertEqual(len(provider.income_queries), 1)
        finally:
            oms.stop()

    def test_account_margin_health_rejects_non_finite_snapshot(self):
        oms = OMS(DummyEngine(), DummyGateway(), self.make_config())
        try:
            self.assertFalse(
                oms.account.sync_margin_health(float("nan"), 1000.0)
            )
            self.assertFalse(oms.account.margin_snapshot_synced)
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

    def test_orderbook_clear_event_uses_recovery_token(self):
        risk_controller = DummyRiskController()
        oms = DummyScopedOms()
        handle_system_health_event(
            Event(
                "eSystemHealth",
                "CLEAR_SYMBOL:BTCUSDT:ORDERBOOK_RESYNCED:42",
            ),
            risk_controller,
            oms,
        )
        self.assertEqual(oms.cleared_symbols, [])
        self.assertEqual(
            oms.orderbook_clears,
            [
                (
                    "BTCUSDT",
                    "42",
                    "system_health:ORDERBOOK_RESYNCED:42",
                )
            ],
        )

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

    def test_transport_recovery_requests_truth_verification_without_clearing(self):
        risk_controller = DummyRiskController()
        oms = DummyScopedOms()
        handle_system_health_event(
            Event("eSystemHealth", "VERIFY_VENUE:BINANCE:WS_RECOVERED"),
            risk_controller,
            oms,
        )
        self.assertEqual(risk_controller.reasons, [])
        self.assertEqual(oms.cleared_venues, [])
        self.assertEqual(
            oms.venue_verifications,
            [("BINANCE", "system_health:WS_RECOVERED")],
        )

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
