import threading
import time
import tempfile
import unittest

from event.type import (
    CommandOutcome,
    Event,
    ExecutionPolicy,
    ExchangeOrderUpdate,
    GatewayCommandResult,
    LifecycleState,
    OrderIntent,
    OrderRequest,
    OrderStatus,
    Side,
    TIF_IOC,
    EVENT_EXCHANGE_ORDER_UPDATE,
    EVENT_ORDER_SUBMITTED,
)
from oms.engine import OMS
from oms.order import Order
from oms.order_manager import OrderManager


class DummyEngine:
    def __init__(self):
        self.events = []

    def put(self, event):
        self.events.append(event)


class DummyResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        return self.payload


class RecoveryGateway:
    gateway_name = "BINANCE"

    def __init__(self):
        self.send_result = "ex-default"
        self.cancel_response = DummyResponse(200, {"status": "CANCELED"})
        self.order_snapshot = None
        self.trades = []
        self.incomes = []
        self.positions = []
        self.open_orders = []
        self.account = {
            "totalWalletBalance": "1000",
            "totalInitialMargin": "0",
            "availableBalance": "1000",
        }
        self.position_snapshots = None
        self.position_query_count = 0

    def send_order(self, _req, _client_oid):
        return self.send_result

    def cancel_order(self, _req):
        return self.cancel_response

    def cancel_all_orders(self, _symbol):
        return DummyResponse(200, {})

    def get_order(self, _symbol, _order_id):
        return self.order_snapshot

    def get_user_trades(self, _symbol, from_id=None, **_kwargs):
        if from_id is None:
            return list(self.trades)
        return [trade for trade in self.trades if int(trade["id"]) >= int(from_id)]

    def get_income_history(self, **_kwargs):
        return list(self.incomes)

    def get_account_info(self):
        return dict(self.account)

    def get_all_positions(self):
        if self.position_snapshots is None:
            return list(self.positions)
        index = min(self.position_query_count, len(self.position_snapshots) - 1)
        self.position_query_count += 1
        return self.position_snapshots[index]

    def get_open_orders(self):
        return list(self.open_orders)


class InstitutionalRecoveryTests(unittest.TestCase):
    def make_config(self):
        return {
            "symbols": ["BTCUSDT"],
            "account": {"initial_balance_usdt": 1000.0, "leverage": 10},
            "backtest": {"maker_fee": 0.0, "taker_fee": 0.0},
            "oms": {
                "journal_enabled": False,
                "replay_journal_on_startup": False,
                "unknown_order_resolution_timeout_sec": 1.0,
                "unknown_order_min_not_found": 2,
                "snapshot_stability_required": 2,
                "snapshot_max_attempts": 5,
                "snapshot_settle_interval_sec": 0.01,
            },
            "risk": {"limits": {"max_pos_notional": 5000.0}},
        }

    def make_live_oms(self, gateway=None):
        gateway = gateway or RecoveryGateway()
        oms = OMS(DummyEngine(), gateway, self.make_config())
        oms.state = LifecycleState.LIVE
        oms._sync_capability_mode("test_live")
        return oms, gateway

    def add_active_order(self, oms, client_oid="oid-1", exchange_oid="ex-1", volume=1.0):
        order = Order(
            client_oid,
            OrderIntent("test", "BTCUSDT", Side.BUY, 100.0, volume),
        )
        order.mark_submitting()
        order.mark_pending_ack(exchange_oid)
        order.mark_new(exchange_oid, update_time=1.0)
        oms.orders[client_oid] = order
        oms.exchange_id_map[exchange_oid] = order
        oms.exposure.update_open_orders(oms.orders)
        return order

    def test_submit_transport_timeout_remains_tracked_as_unknown(self):
        oms, gateway = self.make_live_oms()
        try:
            gateway.send_result = GatewayCommandResult(
                CommandOutcome.UNKNOWN,
                error_message="transport response unavailable",
            )
            oms._on_order_truth_check = lambda *_args, **_kwargs: None
            oms.validator.validate_params = lambda _intent: (True, "")
            oms.exposure.check_risk = lambda *_args, **_kwargs: (True, "")

            result = oms.submit_order(
                OrderIntent("test", "BTCUSDT", Side.BUY, 100.0, 1.0)
            )

            self.assertTrue(result.accepted)
            self.assertEqual(result.reason, "submit_outcome_unknown")
            self.assertEqual(oms.orders[result.client_oid].status, OrderStatus.SUBMIT_UNKNOWN)
            self.assertIn(result.client_oid, oms.orders)
            self.assertTrue(oms.get_symbol_freeze_reason("BTCUSDT").startswith("order_truth:"))
        finally:
            oms.stop()

    def test_user_stream_truth_before_rest_ack_never_regresses_order(self):
        cases = (
            ("NEW", 0.0, OrderStatus.NEW, True),
            ("PARTIALLY_FILLED", 0.4, OrderStatus.PARTIALLY_FILLED, True),
            ("FILLED", 1.0, OrderStatus.FILLED, False),
        )
        for exchange_status, cumulative, expected, monitored in cases:
            with self.subTest(exchange_status=exchange_status):
                gateway = RecoveryGateway()
                oms, _ = self.make_live_oms(gateway)
                gateway.send_result = GatewayCommandResult(
                    CommandOutcome.ACKNOWLEDGED,
                    exchange_oid="ex-early",
                )
                oms.validator.validate_params = lambda _intent: (True, "")
                oms.exposure.check_risk = lambda *_args, **_kwargs: (True, "")
                oms._schedule_trade_tail_verification = (
                    lambda *_args, **_kwargs: True
                )

                def send_with_early_truth(_request, client_oid):
                    oms.on_exchange_update(
                        Event(
                            EVENT_EXCHANGE_ORDER_UPDATE,
                            ExchangeOrderUpdate(
                                client_oid=client_oid,
                                exchange_oid="ex-early",
                                symbol="BTCUSDT",
                                status=exchange_status,
                                filled_qty=cumulative,
                                filled_price=100.0 if cumulative else 0.0,
                                cum_filled_qty=cumulative,
                                update_time=time.time(),
                                trade_id=7 if cumulative else -1,
                            ),
                        )
                    )
                    return gateway.send_result

                gateway.send_order = send_with_early_truth
                try:
                    result = oms.submit_order(
                        OrderIntent(
                            "test",
                            "BTCUSDT",
                            Side.BUY,
                            100.0,
                            1.0,
                        )
                    )

                    order = oms.orders[result.client_oid]
                    self.assertTrue(result.accepted)
                    self.assertEqual(order.status, expected)
                    self.assertEqual(order.exchange_oid, "ex-early")
                    self.assertEqual(
                        result.client_oid in oms.order_monitor.monitored_orders,
                        monitored,
                    )
                    submitted = [
                        event.data
                        for event in oms.event_engine.events
                        if event.type == EVENT_ORDER_SUBMITTED
                    ]
                    self.assertEqual(submitted[-1].status, expected)
                finally:
                    oms.stop()

    def test_user_stream_new_resolves_transport_unknown_without_freeze(self):
        gateway = RecoveryGateway()
        oms, _ = self.make_live_oms(gateway)
        gateway.send_result = GatewayCommandResult(
            CommandOutcome.UNKNOWN,
            error_message="response lost",
        )
        oms.validator.validate_params = lambda _intent: (True, "")
        oms.exposure.check_risk = lambda *_args, **_kwargs: (True, "")
        truth_checks = []
        oms._on_order_truth_check = (
            lambda *args, **kwargs: truth_checks.append((args, kwargs))
        )

        def send_with_early_new(_request, client_oid):
            oms.on_exchange_update(
                Event(
                    EVENT_EXCHANGE_ORDER_UPDATE,
                    ExchangeOrderUpdate(
                        client_oid=client_oid,
                        exchange_oid="ex-early-unknown",
                        symbol="BTCUSDT",
                        status="NEW",
                        filled_qty=0.0,
                        filled_price=0.0,
                        cum_filled_qty=0.0,
                        update_time=time.time(),
                    ),
                )
            )
            return gateway.send_result

        gateway.send_order = send_with_early_new
        try:
            result = oms.submit_order(
                OrderIntent("test", "BTCUSDT", Side.BUY, 100.0, 1.0)
            )

            self.assertTrue(result.accepted)
            self.assertEqual(
                result.reason,
                "transport_unknown_resolved_by_exchange_truth",
            )
            self.assertEqual(
                oms.orders[result.client_oid].status,
                OrderStatus.NEW,
            )
            self.assertEqual(oms.get_symbol_freeze_reason("BTCUSDT"), "")
            self.assertEqual(truth_checks, [])
        finally:
            oms.stop()

    def test_shutdown_latch_allows_only_internal_emergency_reduce_send(self):
        gateway = RecoveryGateway()
        oms, _ = self.make_live_oms(gateway)
        gateway.send_result = GatewayCommandResult(
            CommandOutcome.ACKNOWLEDGED,
            exchange_oid="ex-shutdown-flatten",
        )
        oms._schedule_trade_tail_verification = (
            lambda *_args, **_kwargs: True
        )

        def send_with_early_fill(_request, client_oid):
            oms.on_exchange_update(
                Event(
                    EVENT_EXCHANGE_ORDER_UPDATE,
                    ExchangeOrderUpdate(
                        client_oid=client_oid,
                        exchange_oid="ex-shutdown-flatten",
                        symbol="BTCUSDT",
                        status="FILLED",
                        filled_qty=1.0,
                        filled_price=100.0,
                        cum_filled_qty=1.0,
                        update_time=time.time(),
                        trade_id=8,
                        order_type="MARKET",
                        time_in_force=TIF_IOC,
                    ),
                )
            )
            return gateway.send_result

        gateway.send_order = send_with_early_fill
        intent = OrderIntent(
            "system_emergency",
            "BTCUSDT",
            Side.SELL,
            100.0,
            1.0,
            order_type="MARKET",
            time_in_force=TIF_IOC,
            reduce_only=True,
            policy=ExecutionPolicy.AGGRESSIVE,
            tag="reduce_only_flatten:test_shutdown_retry",
        )
        request = OrderRequest(
            symbol="BTCUSDT",
            price=100.0,
            volume=1.0,
            side="SELL",
            order_type="MARKET",
            time_in_force=TIF_IOC,
            reduce_only=True,
        )
        try:
            self.assertTrue(oms.begin_shutdown("test"))
            self.assertTrue(
                oms._submit_internal_order(
                    intent,
                    request,
                    "EMG_shutdown_test",
                    "test_shutdown_emergency",
                    "test_shutdown_emergency",
                )
            )
            order = oms.orders["EMG_shutdown_test"]
            self.assertEqual(order.status, OrderStatus.FILLED)
            self.assertNotIn(
                order.client_oid,
                oms.order_monitor.monitored_orders,
            )
        finally:
            oms.stop()

    def test_expired_calibration_rechecks_late_position_until_flat(self):
        oms, gateway = self.make_live_oms()
        oms._rpi_calibration = {
            "enabled": True,
            "permit_id": "permit-test",
            "deployment_id": "deployment-test",
            "symbol": "BTCUSDT",
        }
        oms._rpi_calibration_expired = True
        oms._rpi_calibration_expiry_reason = "max_order_count_exhausted"
        oms._rpi_calibration_permit_activated = True
        oms._rpi_calibration_terminal_cancel_sweep_completed = False
        oms._rpi_calibration_terminal_empty_snapshots = 0
        oms._rpi_calibration_terminal_verified = False
        oms._rpi_calibration_terminal_pending_reason = ""
        gateway.position_snapshots = [
            [{"symbol": "BTCUSDT", "positionAmt": "0.1"}],
            [{"symbol": "BTCUSDT", "positionAmt": "0"}],
            [{"symbol": "BTCUSDT", "positionAmt": "0"}],
        ]
        flatten_calls = []
        oms.emergency_reduce_only_flatten = (
            lambda reason, symbol="": flatten_calls.append((reason, symbol))
            or 1
        )
        try:
            self.assertFalse(
                oms._enforce_rpi_calibration_terminal_once()
            )
            self.assertFalse(
                oms._enforce_rpi_calibration_terminal_once()
            )
            self.assertFalse(
                oms._enforce_rpi_calibration_terminal_once()
            )
            self.assertTrue(
                oms._enforce_rpi_calibration_terminal_once()
            )
            self.assertTrue(oms._rpi_calibration_terminal_verified)

            oms.exposure.force_sync("BTCUSDT", 0.1, 100.0)
            oms._rpi_calibration_enforcement_inflight = True
            self.assertFalse(
                oms._schedule_rpi_calibration_runtime_enforcement(
                    terminal_truth_changed=True,
                )
            )
            self.assertFalse(oms._rpi_calibration_terminal_verified)
            self.assertFalse(
                oms._enforce_rpi_calibration_terminal_once()
            )
            self.assertEqual(
                flatten_calls,
                [
                    (
                        "rpi_calibration_expired:"
                        "max_order_count_exhausted",
                        "",
                    ),
                    (
                        "rpi_calibration_expired:"
                        "max_order_count_exhausted",
                        "",
                    ),
                ],
            )

            oms.exposure.force_sync("BTCUSDT", 0.0, 0.0)
            oms._rpi_calibration_terminal_verified = True
            gateway.get_open_orders = lambda: None
            self.assertFalse(
                oms._enforce_rpi_calibration_terminal_once()
            )
            self.assertFalse(oms._rpi_calibration_terminal_verified)
            self.assertEqual(
                oms._rpi_calibration_terminal_pending_reason,
                "open_order_truth_unavailable",
            )

            gateway.get_open_orders = lambda: []
            gateway.position_snapshots = [[{"symbol": "BTCUSDT"}]]
            gateway.position_query_count = 0
            self.assertFalse(
                oms._enforce_rpi_calibration_terminal_once()
            )
            self.assertEqual(
                oms._rpi_calibration_terminal_pending_reason,
                "position_truth_invalid",
            )
        finally:
            oms._rpi_calibration_enforcement_inflight = False
            oms.stop()

    def test_shutdown_waits_for_calibration_enforcer_before_main_proof_handoff(self):
        oms, _gateway = self.make_live_oms()
        oms._rpi_calibration = {
            "enabled": True,
            "permit_id": "permit-test",
            "deployment_id": "deployment-test",
            "symbol": "BTCUSDT",
        }
        oms._rpi_calibration_expired = True
        oms._rpi_calibration_permit_activated = True
        oms._rpi_calibration_terminal_verified = False

        real_thread = threading.Thread
        enforcer_entered = threading.Event()
        allow_enforcer_finish = threading.Event()
        shutdown_entered = threading.Event()
        shutdown_returned = threading.Event()
        schedule_results = []
        shutdown_results = []

        def blocking_enforce():
            enforcer_entered.set()
            allow_enforcer_finish.wait(timeout=2.0)
            return False

        def schedule_enforcer():
            schedule_results.append(
                oms._schedule_rpi_calibration_runtime_enforcement()
            )

        def begin_shutdown():
            shutdown_entered.set()
            shutdown_results.append(oms.begin_shutdown("test_handoff"))
            shutdown_returned.set()

        scheduler = real_thread(target=schedule_enforcer)
        shutdown = real_thread(target=begin_shutdown)
        returned_before_enforcer_started = False
        try:
            oms._enforce_rpi_calibration_terminal_once = blocking_enforce
            scheduler.start()
            self.assertTrue(enforcer_entered.wait(timeout=1.0))

            shutdown.start()
            self.assertTrue(shutdown_entered.wait(timeout=1.0))
            returned_before_enforcer_started = shutdown_returned.wait(
                timeout=0.1
            )
            allow_enforcer_finish.set()

            scheduler.join(timeout=2.0)
            shutdown.join(timeout=2.0)

            self.assertFalse(returned_before_enforcer_started)
            self.assertFalse(scheduler.is_alive())
            self.assertFalse(shutdown.is_alive())
            self.assertEqual(schedule_results, [True])
            self.assertEqual(shutdown_results, [True])
            # main.py begins the final account shutdown proof only after this
            # call returns, so no permit enforcer may remain at this handoff.
            self.assertIsNone(oms._rpi_calibration_enforcement_thread)
            self.assertFalse(oms._rpi_calibration_enforcement_inflight)
        finally:
            allow_enforcer_finish.set()
            scheduler.join(timeout=2.0)
            if shutdown.ident is not None:
                shutdown.join(timeout=2.0)
            oms.stop()

    def test_targeted_query_resolves_submit_unknown_to_new(self):
        oms, gateway = self.make_live_oms()
        try:
            order = Order(
                "oid-unknown",
                OrderIntent("test", "BTCUSDT", Side.BUY, 100.0, 1.0),
            )
            order.mark_submitting()
            order.mark_submit_unknown()
            oms.orders[order.client_oid] = order
            oms.freeze_symbol(
                "BTCUSDT",
                "order_truth:submit_unknown:oid-unknown",
                cancel_active_orders=False,
            )
            gateway.order_snapshot = {
                "symbol": "BTCUSDT",
                "orderId": 123,
                "clientOrderId": "oid-unknown",
                "status": "NEW",
                "side": "BUY",
                "type": "LIMIT",
                "timeInForce": "GTC",
                "origQty": "1",
                "executedQty": "0",
                "price": "100",
                "avgPrice": "0",
                "updateTime": 2000,
            }

            oms._resolve_order_truth("oid-unknown", "test")

            self.assertEqual(order.status, OrderStatus.NEW)
            self.assertEqual(order.exchange_oid, "123")
            self.assertEqual(oms.get_symbol_freeze_reason("BTCUSDT"), "")
        finally:
            oms.stop()

    def test_submit_unknown_requires_repeated_absence_before_rejection(self):
        oms, gateway = self.make_live_oms()
        try:
            order = Order(
                "oid-absent",
                OrderIntent("test", "BTCUSDT", Side.BUY, 100.0, 1.0),
            )
            order.mark_submitting()
            order.mark_submit_unknown()
            order.updated_at = time.time() - 2.0
            order.updated_monotonic = time.perf_counter() - 2.0
            oms.orders[order.client_oid] = order
            oms.freeze_symbol(
                "BTCUSDT",
                "order_truth:submit_unknown:oid-absent",
                cancel_active_orders=False,
            )
            gateway.order_snapshot = {"_query_status": "NOT_FOUND", "code": "-2013"}

            oms._resolve_order_truth(order.client_oid, "test")
            self.assertEqual(order.status, OrderStatus.SUBMIT_UNKNOWN)

            oms._resolve_order_truth(order.client_oid, "test")
            self.assertEqual(order.status, OrderStatus.REJECTED_LOCALLY)
            self.assertEqual(order.error_msg, "exchange_confirmed_order_absent")
            self.assertEqual(oms.get_symbol_freeze_reason("BTCUSDT"), "")
        finally:
            oms.stop()

    def test_trade_history_backfill_is_idempotent_by_trade_id(self):
        oms, gateway = self.make_live_oms()
        try:
            order = self.add_active_order(oms)
            oms.account.force_sync(1000.0, 0.0)
            gateway.trades = [
                {
                    "symbol": "BTCUSDT",
                    "id": 10,
                    "orderId": 1,
                    "side": "BUY",
                    "price": "100",
                    "qty": "0.4",
                    "realizedPnl": "0",
                    "commission": "0.1",
                    "commissionAsset": "USDT",
                    "time": 2000,
                    "maker": True,
                },
                {
                    "symbol": "BTCUSDT",
                    "id": 11,
                    "orderId": 1,
                    "side": "BUY",
                    "price": "101",
                    "qty": "0.6",
                    "realizedPnl": "0",
                    "commission": "0.2",
                    "commissionAsset": "USDT",
                    "time": 3000,
                    "maker": False,
                },
            ]
            oms.exchange_id_map["1"] = order
            order.exchange_oid = "1"

            self.assertTrue(oms._backfill_trade_history(end_time_ms=4000))
            self.assertEqual(order.status, OrderStatus.FILLED)
            self.assertAlmostEqual(oms.exposure.net_positions["BTCUSDT"], 1.0)
            self.assertAlmostEqual(oms.account.balance, 999.7)
            self.assertEqual(oms.trade_cursors["BTCUSDT"], 11)

            self.assertTrue(oms._backfill_trade_history(end_time_ms=5000))
            self.assertAlmostEqual(oms.exposure.net_positions["BTCUSDT"], 1.0)
            self.assertAlmostEqual(oms.account.balance, 999.7)
        finally:
            oms.stop()

    def test_cumulative_fill_gap_waits_for_exact_rest_trades(self):
        oms, gateway = self.make_live_oms()
        try:
            order = self.add_active_order(oms)
            oms.account.force_sync(1000.0, 0.0)
            oms._schedule_trade_tail_verification = (
                lambda *_args, **_kwargs: True
            )
            reconcile_requests = []
            oms.trigger_reconcile = (
                lambda *args, **kwargs: reconcile_requests.append(
                    (args, kwargs)
                )
                or True
            )
            oms._advance_trade_cursor(
                "BTCUSDT",
                9,
                1.0,
                source="rest_backfill",
            )
            gateway.trades = [
                {
                    "symbol": "BTCUSDT",
                    "id": 11,
                    "orderId": "ex-1",
                    "side": "BUY",
                    "price": "101",
                    "qty": "0.6",
                    "realizedPnl": "0",
                    "commission": "0.2",
                    "commissionAsset": "USDT",
                    "time": 2000,
                    "maker": False,
                },
                {
                    "symbol": "BTCUSDT",
                    "id": 10,
                    "orderId": "ex-1",
                    "side": "BUY",
                    "price": "100",
                    "qty": "0.4",
                    "realizedPnl": "0",
                    "commission": "0.1",
                    "commissionAsset": "USDT",
                    "time": 3000,
                    "maker": True,
                },
            ]

            oms._append_and_process(
                Event(
                    EVENT_EXCHANGE_ORDER_UPDATE,
                    ExchangeOrderUpdate(
                        client_oid=order.client_oid,
                        exchange_oid=order.exchange_oid,
                        symbol="BTCUSDT",
                        status="FILLED",
                        filled_qty=0.6,
                        filled_price=101.0,
                        cum_filled_qty=1.0,
                        update_time=2.0,
                        trade_id=11,
                        commission=0.2,
                        commission_asset="USDT",
                    ),
                )
            )

            self.assertEqual(order.filled_volume, 0.0)
            self.assertAlmostEqual(oms.account.balance, 1000.0)
            self.assertEqual(oms.trade_cursors["BTCUSDT"], 9)
            self.assertEqual(oms.state, LifecycleState.FROZEN)

            self.assertTrue(oms._backfill_trade_history(end_time_ms=4000))
            self.assertEqual(order.status, OrderStatus.FILLED)
            self.assertAlmostEqual(order.avg_price, 100.6)
            self.assertAlmostEqual(oms.exposure.net_positions["BTCUSDT"], 1.0)
            self.assertAlmostEqual(oms.account.balance, 999.7)
            self.assertEqual(oms.trade_cursors["BTCUSDT"], 11)
        finally:
            oms.stop()

    def test_fill_without_trade_id_is_not_double_booked(self):
        oms, gateway = self.make_live_oms()
        try:
            order = self.add_active_order(oms)
            oms.account.force_sync(1000.0, 0.0)
            oms._schedule_trade_tail_verification = (
                lambda *_args, **_kwargs: True
            )
            oms.trigger_reconcile = lambda *_args, **_kwargs: True
            gateway.trades = [
                {
                    "symbol": "BTCUSDT",
                    "id": 10,
                    "orderId": "ex-1",
                    "side": "BUY",
                    "price": "100",
                    "qty": "0.4",
                    "realizedPnl": "0",
                    "commission": "0.2",
                    "commissionAsset": "USDT",
                    "time": 2000,
                    "maker": True,
                }
            ]

            oms._append_and_process(
                Event(
                    EVENT_EXCHANGE_ORDER_UPDATE,
                    ExchangeOrderUpdate(
                        client_oid=order.client_oid,
                        exchange_oid=order.exchange_oid,
                        symbol="BTCUSDT",
                        status="PARTIALLY_FILLED",
                        filled_qty=0.4,
                        filled_price=100.0,
                        cum_filled_qty=0.4,
                        update_time=2.0,
                        trade_id=-1,
                        commission=0.2,
                        commission_asset="USDT",
                    ),
                )
            )
            self.assertEqual(order.filled_volume, 0.0)
            self.assertAlmostEqual(oms.account.balance, 1000.0)

            self.assertTrue(oms._backfill_trade_history(end_time_ms=3000))
            self.assertAlmostEqual(order.filled_volume, 0.4)
            self.assertAlmostEqual(oms.exposure.net_positions["BTCUSDT"], 0.4)
            self.assertAlmostEqual(oms.account.balance, 999.8)
            self.assertEqual(oms.trade_cursors["BTCUSDT"], 10)
        finally:
            oms.stop()

    def test_rest_duplicate_confirms_cursor_without_reapplying_execution(self):
        oms, gateway = self.make_live_oms()
        try:
            order = self.add_active_order(oms)
            oms._schedule_trade_tail_verification = (
                lambda *_args, **_kwargs: True
            )
            update = ExchangeOrderUpdate(
                client_oid=order.client_oid,
                exchange_oid=order.exchange_oid,
                symbol="BTCUSDT",
                status="PARTIALLY_FILLED",
                filled_qty=0.4,
                filled_price=100.0,
                cum_filled_qty=0.4,
                update_time=2.0,
                trade_id=42,
            )
            self.assertTrue(
                oms._record_execution(
                    order,
                    update,
                    fill_qty=0.4,
                    fee=0.0,
                )
            )
            order.add_fill(
                0.4,
                100.0,
                update_time=2.0,
                exchange_status="PARTIALLY_FILLED",
            )
            gateway.trades = [
                {
                    "symbol": "BTCUSDT",
                    "id": 42,
                    "orderId": "ex-1",
                    "side": "BUY",
                    "price": "100",
                    "qty": "0.4",
                    "realizedPnl": "0",
                    "commission": "0",
                    "commissionAsset": "USDT",
                    "time": 2000,
                    "maker": True,
                }
            ]

            self.assertTrue(oms._backfill_trade_history(end_time_ms=3000))
            self.assertAlmostEqual(order.filled_volume, 0.4)
            self.assertEqual(oms.trade_cursors["BTCUSDT"], 42)
        finally:
            oms.stop()

    def test_user_stream_fill_cursor_advances_only_after_rest_confirmation(self):
        oms, gateway = self.make_live_oms()
        try:
            order = self.add_active_order(oms)
            oms.account.force_sync(1000.0, 0.0)
            oms.trade_tail_verification_delay_sec = 0.0
            oms.trade_tail_verification_retry_sec = 0.01
            oms.trade_tail_verification_attempts = 2
            gateway.trades = [
                {
                    "symbol": "BTCUSDT",
                    "id": 10,
                    "orderId": "ex-1",
                    "side": "BUY",
                    "price": "100",
                    "qty": "0.4",
                    "realizedPnl": "0",
                    "commission": "0.2",
                    "commissionAsset": "USDT",
                    "time": 2000,
                    "maker": True,
                }
            ]

            oms._append_and_process(
                Event(
                    EVENT_EXCHANGE_ORDER_UPDATE,
                    ExchangeOrderUpdate(
                        client_oid=order.client_oid,
                        exchange_oid=order.exchange_oid,
                        symbol="BTCUSDT",
                        status="PARTIALLY_FILLED",
                        filled_qty=0.4,
                        filled_price=100.0,
                        cum_filled_qty=0.4,
                        update_time=2.0,
                        trade_id=10,
                        commission=0.2,
                        commission_asset="USDT",
                    ),
                )
            )
            deadline = time.perf_counter() + 1.0
            while (
                oms.trade_cursors.get("BTCUSDT", -1) < 10
                and time.perf_counter() < deadline
            ):
                time.sleep(0.005)

            self.assertAlmostEqual(order.filled_volume, 0.4)
            self.assertAlmostEqual(oms.exposure.net_positions["BTCUSDT"], 0.4)
            self.assertAlmostEqual(oms.account.balance, 999.8)
            self.assertEqual(oms.trade_cursors["BTCUSDT"], 10)
        finally:
            oms.stop()

    def test_cancel_timeout_remains_unknown_until_query_confirms_terminal(self):
        oms, gateway = self.make_live_oms()
        try:
            order = self.add_active_order(oms)
            gateway.cancel_response = None
            oms._on_order_truth_check = lambda *_args, **_kwargs: None

            self.assertTrue(oms.cancel_order(order.client_oid))
            self.assertEqual(order.status, OrderStatus.CANCEL_UNKNOWN)
            self.assertGreater(oms.exposure.open_buy_qty["BTCUSDT"], 0.0)

            gateway.order_snapshot = {
                "symbol": "BTCUSDT",
                "orderId": "ex-1",
                "clientOrderId": order.client_oid,
                "status": "CANCELED",
                "side": "BUY",
                "type": "LIMIT",
                "timeInForce": "GTC",
                "origQty": "1",
                "executedQty": "0",
                "price": "100",
                "avgPrice": "0",
                "updateTime": 3000,
            }
            oms._resolve_order_truth(order.client_oid, "test")

            self.assertEqual(order.status, OrderStatus.CANCELLED)
            self.assertAlmostEqual(oms.exposure.open_buy_qty["BTCUSDT"], 0.0)
        finally:
            oms.stop()

    def test_terminal_fill_arriving_before_cancel_error_does_not_freeze_symbol(self):
        oms, gateway = self.make_live_oms()
        try:
            order = self.add_active_order(oms)
            oms._schedule_trade_tail_verification = lambda *_args, **_kwargs: True

            def fill_before_cancel_error(_request):
                oms.on_exchange_update(
                    Event(
                        EVENT_EXCHANGE_ORDER_UPDATE,
                        ExchangeOrderUpdate(
                            client_oid=order.client_oid,
                            exchange_oid=order.exchange_oid,
                            symbol="BTCUSDT",
                            status="FILLED",
                            filled_qty=1.0,
                            filled_price=100.0,
                            cum_filled_qty=1.0,
                            update_time=2.0,
                            trade_id=17,
                        ),
                    )
                )
                return DummyResponse(
                    404,
                    {"code": -2011, "msg": "Unknown order sent."},
                )

            gateway.cancel_order = fill_before_cancel_error

            self.assertTrue(oms.cancel_order(order.client_oid))

            self.assertEqual(order.status, OrderStatus.FILLED)
            self.assertEqual(oms.get_symbol_freeze_reason("BTCUSDT"), "")
            self.assertEqual(oms.state, LifecycleState.LIVE)
            self.assertFalse(oms.manual_rearm_required)
        finally:
            oms.stop()

    def test_terminal_update_clears_existing_order_truth_guard(self):
        oms, _gateway = self.make_live_oms()
        try:
            order = self.add_active_order(oms)
            order.add_fill(1.0, 100.0, update_time=2.0)
            oms.freeze_symbol(
                "BTCUSDT",
                f"order_truth:cancel_unknown:{order.client_oid}",
                cancel_active_orders=False,
            )

            oms._resolve_order_truth(order.client_oid, "terminal update raced truth check")

            self.assertEqual(order.status, OrderStatus.FILLED)
            self.assertEqual(oms.get_symbol_freeze_reason("BTCUSDT"), "")
            self.assertTrue(oms.can_open_new_risk())
        finally:
            oms.stop()

    def test_terminal_snapshot_waits_for_exact_trade_history(self):
        oms, gateway = self.make_live_oms()
        try:
            order = self.add_active_order(oms)
            oms.account.force_sync(1000.0, 0.0)
            gateway.order_snapshot = {
                "symbol": "BTCUSDT",
                "orderId": "ex-1",
                "clientOrderId": order.client_oid,
                "status": "CANCELED",
                "side": "BUY",
                "type": "LIMIT",
                "timeInForce": "GTC",
                "origQty": "1",
                "executedQty": "0.4",
                "price": "100",
                "avgPrice": "100",
                "updateTime": 3000,
            }

            self.assertFalse(
                oms._apply_exchange_order_snapshot(
                    gateway.order_snapshot,
                    source="test_trade_lag",
                )
            )
            self.assertEqual(order.status, OrderStatus.NEW)
            self.assertAlmostEqual(order.filled_volume, 0.0)
            self.assertAlmostEqual(oms.exposure.net_positions["BTCUSDT"], 0.0)
            self.assertEqual(oms.state, LifecycleState.FROZEN)
        finally:
            oms.stop()

    def test_late_trade_backfill_repairs_already_terminal_order(self):
        oms, gateway = self.make_live_oms()
        try:
            order = self.add_active_order(oms)
            order.mark_cancelled(update_time=4.0)
            oms.account.force_sync(1000.0, 0.0)
            gateway.trades = [
                {
                    "symbol": "BTCUSDT",
                    "id": 21,
                    "orderId": "ex-1",
                    "side": "BUY",
                    "price": "100",
                    "qty": "0.4",
                    "realizedPnl": "0",
                    "commission": "0.1",
                    "commissionAsset": "USDT",
                    "time": 3000,
                    "maker": True,
                }
            ]

            self.assertTrue(oms._backfill_trade_history(end_time_ms=5000))
            self.assertEqual(order.status, OrderStatus.CANCELLED)
            self.assertAlmostEqual(order.filled_volume, 0.4)
            self.assertAlmostEqual(oms.exposure.net_positions["BTCUSDT"], 0.4)
            self.assertAlmostEqual(oms.account.balance, 999.9)
        finally:
            oms.stop()

    def test_non_contiguous_local_seq_does_not_create_false_gap(self):
        oms, _gateway = self.make_live_oms()
        try:
            order = self.add_active_order(oms)
            oms._append_and_process(
                Event(
                    EVENT_EXCHANGE_ORDER_UPDATE,
                    ExchangeOrderUpdate(
                        client_oid=order.client_oid,
                        exchange_oid=order.exchange_oid,
                        symbol="BTCUSDT",
                        status="CANCELED",
                        filled_qty=0.0,
                        filled_price=0.0,
                        cum_filled_qty=0.0,
                        update_time=2.0,
                        seq=500,
                    ),
                )
            )
            self.assertEqual(order.status, OrderStatus.CANCELLED)
        finally:
            oms.stop()

    def test_stable_snapshot_requires_two_matching_observations(self):
        oms, gateway = self.make_live_oms()
        try:
            gateway.position_snapshots = [
                [],
                [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}],
                [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}],
            ]
            snapshot = oms._capture_stable_exchange_snapshot()
            self.assertEqual(snapshot["attempt"], 3)
            self.assertEqual(gateway.position_query_count, 3)
        finally:
            oms.stop()

    def test_order_manager_rechecks_stuck_cancelling_state(self):
        calls = []
        manager = OrderManager(
            DummyEngine(),
            RecoveryGateway(),
            lambda reason, suspicious_oid=None: calls.append((reason, suspicious_oid)),
            {"cancel_timeout_sec": 1.0},
            start_thread=False,
        )
        try:
            manager.monitored_orders["oid-cancel"] = {
                "symbol": "BTCUSDT",
                "submit_time": 1.0,
                "last_ack_time": 2.0,
                "status": OrderStatus.CANCELLING,
                "ack_timeout_reported": False,
                "last_timeout_reported_at": 0.0,
            }
            manager._check_once(now=4.0)
            self.assertEqual(
                calls,
                [("Order cancel outcome unknown", "oid-cancel")],
            )
        finally:
            manager.stop()

    def test_trade_cursor_is_recovered_from_journal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.make_config()
            config["oms"].update(
                {
                    "journal_enabled": True,
                    "replay_journal_on_startup": True,
                    "journal_path": f"{temp_dir}/oms.jsonl",
                }
            )
            first = OMS(DummyEngine(), RecoveryGateway(), config)
            first._advance_trade_cursor("BTCUSDT", 77, 1.0, source="test")
            first.stop()

            second = OMS(DummyEngine(), RecoveryGateway(), config)
            try:
                self.assertEqual(second.trade_cursors["BTCUSDT"], 77)
            finally:
                second.stop()

    def test_external_cash_flow_is_idempotent_and_recovered_from_journal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.make_config()
            config["risk"]["cash_flow_truth"] = {
                "enabled": True,
                "require_snapshot": True,
            }
            config["oms"].update(
                {
                    "journal_enabled": True,
                    "replay_journal_on_startup": True,
                    "journal_path": f"{temp_dir}/oms.jsonl",
                }
            )
            gateway = RecoveryGateway()
            gateway.incomes = [
                {
                    "incomeType": "TRANSFER",
                    "tranId": "transfer-1",
                    "asset": "USDT",
                    "income": "100.0",
                    "time": 2000,
                }
            ]
            first = OMS(DummyEngine(), gateway, config)
            first.state = LifecycleState.LIVE
            first._sync_capability_mode("test_live")
            self.assertTrue(first.backfill_external_cash_flow_history(end_time_ms=3000))
            self.assertAlmostEqual(first.account.external_cash_flow_total, 100.0)
            self.assertTrue(first.account.cash_flow_snapshot_synced)
            first.stop()

            second = OMS(DummyEngine(), gateway, config)
            self.assertAlmostEqual(second.account.external_cash_flow_total, 100.0)
            self.assertFalse(second.account.cash_flow_snapshot_synced)
            self.assertTrue(second.backfill_external_cash_flow_history(end_time_ms=4000))
            self.assertAlmostEqual(second.account.external_cash_flow_total, 100.0)
            second.stop()


if __name__ == "__main__":
    unittest.main()
