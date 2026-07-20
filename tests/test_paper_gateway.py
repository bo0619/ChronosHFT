import threading
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from event.engine import EventEngine
from event.type import (
    AggTradeData,
    CancelRequest,
    CommandOutcome,
    GatewayState,
    LifecycleState,
    OrderBook,
    OrderIntent,
    OrderRequest,
    OrderStatus,
    Side,
    TIF_GTC,
    TIF_GTX,
    TIF_IOC,
    TIF_RPI,
    EVENT_EXCHANGE_ACCOUNT_UPDATE,
    EVENT_EXCHANGE_ORDER_UPDATE,
    EVENT_SYSTEM_HEALTH,
)
from gateway.binance.paper_gateway import (
    BinancePaperGateway,
    PaperTruthSnapshotProvider,
)
from gateway.binance.ws_api import BinanceWsApi
from infrastructure.paper_trade import apply_paper_trade_mode
from infrastructure.truth_monitor import TruthMonitor
from infrastructure.venue_supervisor import VenueSupervisor
from main import build_gateway_bundle
from oms.engine import OMS


SYMBOL = "SOXLUSDT"


class DispatchingEngine:
    def __init__(self):
        self.events = []
        self.handlers = {}

    def put(self, event):
        self.events.append(event)
        for handler in list(self.handlers.get(event.type, [])):
            handler(event)

    def register(self, event_type, handler):
        self.handlers.setdefault(event_type, []).append(handler)

    register_execution = register
    register_market = register
    register_hot = register
    register_cold = register


class DummyPublicSession:
    def __init__(self):
        self.headers = {}
        self.closed = False

    def get(self, *_args, **_kwargs):
        raise AssertionError("offline Paper tests must not perform network I/O")

    def close(self):
        self.closed = True


def make_contract(*, supports_rpi=True):
    return SimpleNamespace(
        symbol=SYMBOL,
        supports_rpi=supports_rpi,
        min_qty=0.001,
        step_size=0.001,
        tick_size=0.1,
        min_notional=5.0,
    )


def make_book(*, bid_price=100.0, ask_price=101.0, bid_qty=1.0, ask_qty=1.0):
    now = time.time()
    return OrderBook(
        symbol=SYMBOL,
        exchange="BINANCE",
        datetime=datetime.fromtimestamp(now),
        bids={bid_price: bid_qty},
        asks={ask_price: ask_qty},
        top_bids=((bid_price, bid_qty),),
        top_asks=((ask_price, ask_qty),),
        best_bid_price=bid_price,
        best_bid_volume=bid_qty,
        best_ask_price=ask_price,
        best_ask_volume=ask_qty,
        received_timestamp=now,
        exchange_timestamp=now,
        depth_levels=1,
    )


def make_gateway_config(*, rpi_fill_model="disabled"):
    return {
        "execution": {"mode": "paper"},
        "testnet": False,
        "symbols": [SYMBOL],
        "paper_trade": {
            "enabled": True,
            "reset_on_start": True,
            "initial_balance_usdt": 100.0,
            "maker_fee": 0.0002,
            "taker_fee": 0.0005,
            "rpi_commission_rate": 0.0003,
            "rpi_fill_model": rpi_fill_model,
            "public_trade_proxy": rpi_fill_model == "public_trade_proxy",
            "command_timeout_sec": 1.0,
        },
        "account": {
            "initial_balance_usdt": 100.0,
            "leverage": 10,
            "margin_type": "ISOLATED",
            "position_mode": "ONE_WAY",
        },
        "system": {
            "market_data": {
                "environment": "production",
                "public_only": True,
                "publish_depth_levels": 5,
            }
        },
    }


def wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


class PaperGatewayTests(unittest.TestCase):
    def setUp(self):
        self.session_patch = patch(
            "gateway.binance.paper_gateway.requests.Session",
            side_effect=DummyPublicSession,
        )
        self.session_patch.start()
        self.engine = DispatchingEngine()
        self.gateway = None

    def tearDown(self):
        try:
            if self.gateway is not None:
                if self.gateway._worker_running:
                    self.gateway.close()
                else:
                    self.gateway.rest.close()
        finally:
            self.session_patch.stop()

    def start_offline(self, *, rpi_fill_model="disabled", book=None):
        self.gateway = BinancePaperGateway(
            self.engine,
            make_gateway_config(rpi_fill_model=rpi_fill_model),
        )
        self.gateway._start_worker()
        self.gateway.active = True
        self.gateway._accepting_orders = True
        self.gateway.state = GatewayState.READY
        self.gateway._call_worker("book", book or make_book())
        return self.gateway

    def order_updates(self, client_oid):
        return [
            event.data
            for event in self.engine.events
            if event.type == EVENT_EXCHANGE_ORDER_UPDATE
            and event.data.client_oid == client_oid
        ]

    def test_connect_uses_only_production_public_market_capabilities(self):
        self.gateway = BinancePaperGateway(self.engine, make_gateway_config())
        snapshot = {
            "lastUpdateId": 10,
            "bids": [["100", "1"]],
            "asks": [["101", "1"]],
        }

        with patch.object(
            self.gateway.rest,
            "get_depth_snapshot",
            return_value=snapshot,
        ) as depth, patch.object(
            BinanceWsApi,
            "start_market_stream",
        ) as market_stream, patch.object(
            BinanceWsApi,
            "start_user_stream",
            side_effect=AssertionError("paper mode must never start a user stream"),
        ) as user_stream:
            self.assertTrue(self.gateway.connect([SYMBOL]))

        depth.assert_called_once_with(SYMBOL)
        market_stream.assert_called_once_with([SYMBOL])
        user_stream.assert_not_called()
        self.assertFalse(self.gateway.testnet)
        self.assertEqual(self.gateway.environment, "PAPER_LIVE_DATA")
        self.assertFalse(hasattr(self.gateway, "api_key"))
        self.assertFalse(hasattr(self.gateway, "api_secret"))
        self.assertFalse(hasattr(self.gateway.rest, "api_key"))
        self.assertFalse(hasattr(self.gateway.rest, "api_secret"))
        self.assertNotIn("X-MBX-APIKEY", self.gateway.rest.session.headers)

    def test_public_ws_fault_uses_supervisor_recoverable_reason(self):
        gateway = self.start_offline()
        gateway.on_ws_error(
            {
                "stream": "MarketWS",
                "kind": "transport_drop",
                "detail": "offline-test",
            }
        )

        self.assertEqual(gateway.state, GatewayState.ERROR)
        self.assertFalse(gateway._accepting_orders)
        freeze_event = next(
            event
            for event in reversed(self.engine.events)
            if event.type == EVENT_SYSTEM_HEALTH
            and str(event.data).startswith("FREEZE_VENUE:")
        )
        self.assertEqual(
            freeze_event.data,
            "FREEZE_VENUE:BINANCE_PAPER:"
            "WS_TRANSPORT_DROP:PUBLIC:MarketWS:transport_drop:offline-test",
        )
        freeze_reason = f"system_health:{freeze_event.data.split(':', 2)[2]}"
        oms = SimpleNamespace(
            get_venue_freeze_reason=lambda _venue: freeze_reason,
        )
        supervisor = VenueSupervisor(
            oms,
            gateway,
            {
                "oms": {
                    "venue_supervisor": {
                        "poll_interval_sec": 0.0,
                        "recovery_delay_sec": 0.0,
                    }
                }
            },
            start_thread=False,
        )
        with patch.object(
            gateway,
            "recover_connectivity",
            return_value=True,
        ) as recover:
            self.assertTrue(supervisor.poll_once())
        recover.assert_called_once_with()

    def test_staged_order_emits_nothing_before_oms_commit_barrier(self):
        gateway = self.start_offline()
        request = OrderRequest(
            symbol=SYMBOL,
            price=100.0,
            volume=0.1,
            side="BUY",
            time_in_force=TIF_GTX,
            post_only=True,
        )
        with patch(
            "gateway.binance.paper_gateway.ref_data_manager.get_info",
            return_value=make_contract(),
        ):
            result = gateway.send_order(request, "paper-stage")
            self.assertEqual(result.outcome, CommandOutcome.ACKNOWLEDGED)
            self.assertEqual(self.order_updates("paper-stage"), [])
            self.assertEqual(gateway.get_open_orders(), [])
            staged = gateway.get_order(SYMBOL, "paper-stage")
            self.assertFalse(staged["_paperCommitted"])
            self.assertEqual(staged["status"], "STAGED")
            cancel = gateway.cancel_order(
                CancelRequest(SYMBOL, staged["orderId"])
            )
            self.assertEqual(cancel.status_code, 200)
            self.assertEqual(cancel.json()["status"], "CANCELED")
            self.assertTrue(cancel.json()["_paperPendingCancel"])
            self.assertEqual(self.order_updates("paper-stage"), [])

            self.assertTrue(gateway.commit_order_submission("paper-stage"))
            self.assertTrue(
                wait_until(lambda: len(self.order_updates("paper-stage")) == 1)
            )

        update = self.order_updates("paper-stage")[0]
        self.assertEqual(update.status, "CANCELED")
        self.assertEqual(update.time_in_force, TIF_GTX)

    def test_commit_revalidates_post_only_against_the_latest_book(self):
        gateway = self.start_offline()
        request = OrderRequest(
            symbol=SYMBOL,
            price=100.0,
            volume=0.1,
            side="BUY",
            time_in_force=TIF_GTX,
            post_only=True,
        )
        with patch(
            "gateway.binance.paper_gateway.ref_data_manager.get_info",
            return_value=make_contract(),
        ):
            staged = gateway.send_order(request, "gtx-revalidate")
            self.assertEqual(staged.outcome, CommandOutcome.ACKNOWLEDGED)
            gateway._call_worker(
                "book",
                make_book(bid_price=98.0, ask_price=99.0),
            )
            self.assertTrue(gateway.commit_order_submission("gtx-revalidate"))

        updates = self.order_updates("gtx-revalidate")
        self.assertEqual([update.status for update in updates], ["REJECTED"])
        self.assertEqual(gateway.get_open_orders(), [])
        self.assertEqual(
            gateway.get_order(SYMBOL, "gtx-revalidate")["status"],
            "REJECTED",
        )

    def test_cancel_all_between_stage_and_commit_is_a_rejection_barrier(self):
        gateway = self.start_offline()
        request = OrderRequest(
            symbol=SYMBOL,
            price=100.0,
            volume=0.1,
            side="BUY",
            time_in_force=TIF_GTX,
            post_only=True,
        )
        with patch(
            "gateway.binance.paper_gateway.ref_data_manager.get_info",
            return_value=make_contract(),
        ):
            staged = gateway.send_order(request, "cancel-all-staged")
            self.assertEqual(staged.outcome, CommandOutcome.ACKNOWLEDGED)
            canceled = gateway.cancel_all_orders(SYMBOL)
            self.assertEqual(canceled.status_code, 200)
            self.assertEqual(canceled.json(), [])
            self.assertEqual(self.order_updates("cancel-all-staged"), [])
            self.assertTrue(gateway.commit_order_submission("cancel-all-staged"))

        updates = self.order_updates("cancel-all-staged")
        self.assertEqual([update.status for update in updates], ["CANCELED"])
        order = gateway.get_order(SYMBOL, "cancel-all-staged")
        self.assertEqual(order["status"], "CANCELED")
        self.assertEqual(
            order["_paperTerminalReason"],
            "PAPER_CANCEL_ALL_BARRIER",
        )
        self.assertEqual(gateway.get_open_orders(), [])

    def test_dms_expiry_between_stage_and_commit_cannot_escape_mass_cancel(self):
        gateway = self.start_offline()
        request = OrderRequest(
            symbol=SYMBOL,
            price=100.0,
            volume=0.1,
            side="BUY",
            time_in_force=TIF_GTX,
            post_only=True,
        )
        with patch(
            "gateway.binance.paper_gateway.ref_data_manager.get_info",
            return_value=make_contract(),
        ):
            staged = gateway.send_order(request, "dms-staged")
            self.assertEqual(staged.outcome, CommandOutcome.ACKNOWLEDGED)
            dms = gateway.set_countdown_cancel_all(SYMBOL, 10)
            self.assertEqual(dms.status_code, 200)
            self.assertTrue(
                wait_until(
                    lambda: any(
                        event.type == EVENT_SYSTEM_HEALTH
                        and event.data == f"PAPER_DMS_TRIGGERED:{SYMBOL}"
                        for event in self.engine.events
                    ),
                    timeout=1.0,
                )
            )
            self.assertEqual(self.order_updates("dms-staged"), [])
            self.assertTrue(gateway.commit_order_submission("dms-staged"))

        updates = self.order_updates("dms-staged")
        self.assertEqual([update.status for update in updates], ["CANCELED"])
        self.assertEqual(gateway.get_open_orders(), [])

    def test_timed_out_stage_is_rolled_back_without_a_ghost_order(self):
        gateway = self.start_offline()
        gateway.command_timeout_sec = 0.05
        request = OrderRequest(
            symbol=SYMBOL,
            price=100.0,
            volume=0.1,
            side="BUY",
            time_in_force=TIF_GTX,
            post_only=True,
        )
        original_stage = gateway._stage_order

        def delayed_stage(*args, **kwargs):
            time.sleep(0.10)
            return original_stage(*args, **kwargs)

        with patch(
            "gateway.binance.paper_gateway.ref_data_manager.get_info",
            return_value=make_contract(),
        ), patch.object(gateway, "_stage_order", side_effect=delayed_stage):
            result = gateway.send_order(request, "stage-timeout")

        self.assertEqual(result.outcome, CommandOutcome.UNKNOWN)
        self.assertTrue(
            wait_until(
                lambda: gateway.get_order(SYMBOL, "stage-timeout").get(
                    "_query_status"
                )
                == "NOT_FOUND",
                timeout=1.0,
            )
        )
        self.assertEqual(gateway.get_open_orders(), [])
        self.assertEqual(self.order_updates("stage-timeout"), [])

    def test_gtx_cross_rejects_and_ioc_partially_fills_then_expires(self):
        gateway = self.start_offline(book=make_book(ask_qty=0.4))
        with patch(
            "gateway.binance.paper_gateway.ref_data_manager.get_info",
            return_value=make_contract(),
        ):
            rejected = gateway.send_order(
                OrderRequest(
                    symbol=SYMBOL,
                    price=101.0,
                    volume=0.1,
                    side="BUY",
                    time_in_force=TIF_GTX,
                    post_only=True,
                ),
                "gtx-cross",
            )
            self.assertEqual(rejected.outcome, CommandOutcome.REJECTED)
            self.assertIn("post_only_would_cross", rejected.error_message)

            acknowledged = gateway.send_order(
                OrderRequest(
                    symbol=SYMBOL,
                    price=101.0,
                    volume=1.0,
                    side="BUY",
                    time_in_force=TIF_IOC,
                ),
                "ioc-partial",
            )
            self.assertEqual(acknowledged.outcome, CommandOutcome.ACKNOWLEDGED)
            gateway.commit_order_submission("ioc-partial")
            self.assertTrue(
                wait_until(
                    lambda: gateway.get_order(SYMBOL, "ioc-partial")["status"]
                    == "EXPIRED"
                )
            )

        statuses = [item.status for item in self.order_updates("ioc-partial")]
        self.assertEqual(statuses, ["NEW", "PARTIALLY_FILLED", "EXPIRED"])
        order = gateway.get_order(SYMBOL, "ioc-partial")
        self.assertAlmostEqual(float(order["executedQty"]), 0.4)
        position = gateway.get_all_positions()[0]
        self.assertAlmostEqual(float(position["positionAmt"]), 0.4)
        trade = gateway.get_user_trades(SYMBOL)[0]
        self.assertFalse(trade["maker"])
        self.assertTrue(trade["_simulated"])

    def test_passive_queue_partial_fill_and_rpi_default_no_fill(self):
        gateway = self.start_offline()
        with patch(
            "gateway.binance.paper_gateway.ref_data_manager.get_info",
            return_value=make_contract(),
        ):
            passive = gateway.send_order(
                OrderRequest(
                    symbol=SYMBOL,
                    price=100.0,
                    volume=0.5,
                    side="BUY",
                    time_in_force=TIF_GTX,
                    post_only=True,
                ),
                "passive-queue",
            )
            self.assertEqual(passive.outcome, CommandOutcome.ACKNOWLEDGED)
            gateway.commit_order_submission("passive-queue")
            self.assertTrue(wait_until(lambda: bool(self.order_updates("passive-queue"))))

            gateway._call_worker(
                "market_trade",
                AggTradeData(
                    SYMBOL,
                    1,
                    100.0,
                    1.2,
                    True,
                    datetime.now(),
                ),
            )
            self.assertEqual(
                gateway.get_order(SYMBOL, "passive-queue")["status"],
                "PARTIALLY_FILLED",
            )
            self.assertAlmostEqual(
                float(gateway.get_order(SYMBOL, "passive-queue")["executedQty"]),
                0.2,
            )
            gateway._call_worker(
                "market_trade",
                AggTradeData(
                    SYMBOL,
                    2,
                    100.0,
                    0.3,
                    True,
                    datetime.now(),
                ),
            )
            self.assertEqual(
                gateway.get_order(SYMBOL, "passive-queue")["status"],
                "FILLED",
            )

            rpi = gateway.send_order(
                OrderRequest(
                    symbol=SYMBOL,
                    price=100.0,
                    volume=0.1,
                    side="BUY",
                    time_in_force=TIF_RPI,
                    post_only=True,
                ),
                "rpi-disabled",
            )
            self.assertEqual(rpi.outcome, CommandOutcome.ACKNOWLEDGED)
            gateway.commit_order_submission("rpi-disabled")
            self.assertTrue(wait_until(lambda: bool(self.order_updates("rpi-disabled"))))
            gateway._call_worker(
                "market_trade",
                AggTradeData(
                    SYMBOL,
                    3,
                    99.0,
                    100.0,
                    True,
                    datetime.now(),
                ),
            )

        rpi_order = gateway.get_order(SYMBOL, "rpi-disabled")
        self.assertEqual(rpi_order["status"], "NEW")
        self.assertEqual(float(rpi_order["executedQty"]), 0.0)
        self.assertEqual(rpi_order["_paperFillModel"], "rpi_disabled")

    def test_rpi_proxy_fill_is_explicitly_simulated_and_charges_local_fee(self):
        gateway = self.start_offline(rpi_fill_model="public_trade_proxy")
        with patch(
            "gateway.binance.paper_gateway.ref_data_manager.get_info",
            return_value=make_contract(),
        ):
            result = gateway.send_order(
                OrderRequest(
                    symbol=SYMBOL,
                    price=100.0,
                    volume=0.1,
                    side="BUY",
                    time_in_force=TIF_RPI,
                    post_only=True,
                ),
                "rpi-proxy",
            )
            self.assertEqual(result.outcome, CommandOutcome.ACKNOWLEDGED)
            gateway.commit_order_submission("rpi-proxy")
            self.assertTrue(wait_until(lambda: bool(self.order_updates("rpi-proxy"))))
            gateway._call_worker(
                "market_trade",
                AggTradeData(
                    SYMBOL,
                    11,
                    99.0,
                    1.0,
                    True,
                    datetime.now(),
                ),
            )

        order = gateway.get_order(SYMBOL, "rpi-proxy")
        self.assertEqual(order["status"], "FILLED")
        self.assertEqual(order["_paperFillModel"], "rpi_public_trade_proxy")
        trade = gateway.get_user_trades(SYMBOL)[0]
        self.assertTrue(trade["maker"])
        self.assertTrue(trade["_simulated"])
        self.assertEqual(trade["_fillModel"], "rpi_public_trade_proxy")
        self.assertAlmostEqual(float(trade["commission"]), 0.005)
        self.assertAlmostEqual(
            float(gateway.get_account_info()["totalWalletBalance"]),
            99.995,
        )

    def test_rpi_always_queues_behind_same_price_non_rpi_even_if_it_arrived_first(self):
        gateway = self.start_offline(
            rpi_fill_model="public_trade_proxy",
            book=make_book(bid_qty=0.0),
        )
        with patch(
            "gateway.binance.paper_gateway.ref_data_manager.get_info",
            return_value=make_contract(),
        ):
            rpi = gateway.send_order(
                OrderRequest(
                    symbol=SYMBOL,
                    price=100.0,
                    volume=0.5,
                    side="BUY",
                    time_in_force=TIF_RPI,
                    post_only=True,
                ),
                "rpi-first",
            )
            self.assertEqual(rpi.outcome, CommandOutcome.ACKNOWLEDGED)
            self.assertTrue(gateway.commit_order_submission("rpi-first"))

            non_rpi = gateway.send_order(
                OrderRequest(
                    symbol=SYMBOL,
                    price=100.0,
                    volume=0.5,
                    side="BUY",
                    time_in_force=TIF_GTX,
                    post_only=True,
                ),
                "non-rpi-second",
            )
            self.assertEqual(non_rpi.outcome, CommandOutcome.ACKNOWLEDGED)
            self.assertTrue(gateway.commit_order_submission("non-rpi-second"))

            self.assertAlmostEqual(
                float(gateway.get_order(SYMBOL, "non-rpi-second")["_paperQueueAhead"]),
                0.0,
            )
            self.assertAlmostEqual(
                float(gateway.get_order(SYMBOL, "rpi-first")["_paperQueueAhead"]),
                0.5,
            )
            gateway._call_worker(
                "market_trade",
                AggTradeData(
                    SYMBOL,
                    21,
                    100.0,
                    0.5,
                    True,
                    datetime.now(),
                ),
            )

            self.assertEqual(
                gateway.get_order(SYMBOL, "non-rpi-second")["status"],
                "FILLED",
            )
            self.assertEqual(
                gateway.get_order(SYMBOL, "rpi-first")["status"],
                "NEW",
            )
            gateway._call_worker(
                "market_trade",
                AggTradeData(
                    SYMBOL,
                    22,
                    100.0,
                    0.5,
                    True,
                    datetime.now(),
                ),
            )

        self.assertEqual(
            gateway.get_order(SYMBOL, "rpi-first")["status"],
            "FILLED",
        )

    def test_cancel_and_dead_man_switch_are_entirely_local(self):
        gateway = self.start_offline()
        with patch(
            "gateway.binance.paper_gateway.ref_data_manager.get_info",
            return_value=make_contract(),
        ):
            for client_oid in ("cancel-local", "dms-local"):
                result = gateway.send_order(
                    OrderRequest(
                        symbol=SYMBOL,
                        price=100.0,
                        volume=0.1,
                        side="BUY",
                        time_in_force=TIF_GTX,
                        post_only=True,
                    ),
                    client_oid,
                )
                self.assertEqual(result.outcome, CommandOutcome.ACKNOWLEDGED)
                gateway.commit_order_submission(client_oid)
            self.assertTrue(
                wait_until(lambda: len(gateway.get_open_orders()) == 2)
            )

            exchange_oid = gateway.get_order(SYMBOL, "cancel-local")["orderId"]
            response = gateway.cancel_order(CancelRequest(SYMBOL, exchange_oid))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "CANCELED")

            dms_response = gateway.set_countdown_cancel_all(SYMBOL, 20)
            self.assertEqual(dms_response.status_code, 200)
            self.assertTrue(
                wait_until(
                    lambda: gateway.get_order(SYMBOL, "dms-local")["status"]
                    == "CANCELED",
                    timeout=1.0,
                )
            )
        self.assertEqual(gateway.get_open_orders(), [])


class PaperRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.session_patch = patch(
            "gateway.binance.paper_gateway.requests.Session",
            side_effect=DummyPublicSession,
        )
        self.session_patch.start()

    def tearDown(self):
        self.session_patch.stop()

    def test_gateway_factory_scrubs_credentials_and_never_builds_live_components(self):
        config = apply_paper_trade_mode(
            {
                **make_gateway_config(),
                "api_key": "must-not-reach-a-component",
                "api_secret": "must-not-reach-a-component",
            }
        )
        engine = DispatchingEngine()
        with patch("main.BinanceGateway", side_effect=AssertionError("live gateway built")), patch(
            "main.BinanceTruthSnapshotProvider",
            side_effect=AssertionError("live truth provider built"),
        ):
            gateway, truth = build_gateway_bundle(
                engine,
                config,
                config["system"]["market_data"],
            )
        try:
            self.assertIsInstance(gateway, BinancePaperGateway)
            self.assertIsInstance(truth, PaperTruthSnapshotProvider)
            self.assertFalse(hasattr(gateway, "api_key"))
            self.assertFalse(hasattr(gateway, "api_secret"))
            self.assertEqual(config["api_key"], "")
            self.assertEqual(config["api_secret"], "")
        finally:
            gateway.rest.close()

    def test_gateway_shutdown_cancellations_are_drainable_before_oms_stop(self):
        engine = EventEngine()
        updates = []
        engine.register_execution(
            EVENT_EXCHANGE_ORDER_UPDATE,
            lambda event: updates.append(event.data),
        )
        gateway = BinancePaperGateway(engine, make_gateway_config())
        engine.start()
        gateway._start_worker()
        gateway.active = True
        gateway._accepting_orders = True
        gateway.state = GatewayState.READY
        gateway._call_worker("book", make_book())
        try:
            with patch(
                "gateway.binance.paper_gateway.ref_data_manager.get_info",
                return_value=make_contract(),
            ):
                for index in range(3):
                    client_oid = f"shutdown-drain-{index}"
                    result = gateway.send_order(
                        OrderRequest(
                            symbol=SYMBOL,
                            price=100.0,
                            volume=0.1,
                            side="BUY",
                            time_in_force=TIF_GTX,
                            post_only=True,
                        ),
                        client_oid,
                    )
                    self.assertEqual(
                        result.outcome,
                        CommandOutcome.ACKNOWLEDGED,
                    )
                    self.assertTrue(gateway.commit_order_submission(client_oid))
            self.assertTrue(engine.wait_until_idle(timeout_sec=1.0))
            updates.clear()

            gateway.close()
            self.assertTrue(engine.wait_until_idle(timeout_sec=1.0))
            self.assertEqual(
                [update.status for update in updates],
                ["CANCELED", "CANCELED", "CANCELED"],
            )
            self.assertEqual(engine.get_queue_snapshot()["pending_work"], 0)
        finally:
            if gateway._worker_running:
                gateway.close()
            engine.stop()

    def test_oms_target_cancel_during_submit_commit_window_never_goes_live(self):
        engine = DispatchingEngine()
        config = make_gateway_config()
        config["risk"] = {
            "limits": {
                "max_order_qty": 100.0,
                "max_order_notional": 1000.0,
                "max_pos_notional": 1000.0,
                "max_account_gross_notional": 1000.0,
            },
            "price_sanity": {
                "max_deviation_pct": 0.05,
                "max_spread_pct": 0.05,
            },
            "risk_control_heartbeat": {"enabled": False},
            "cash_flow_truth": {"enabled": False},
            "independent_supervisor": {"enabled": False},
            "margin_health": {"enabled": False},
        }
        config["oms"] = {
            "journal_enabled": False,
            "replay_journal_on_startup": False,
        }
        gateway = BinancePaperGateway(engine, config)
        gateway._start_worker()
        gateway.active = True
        gateway._accepting_orders = True
        gateway.state = GatewayState.READY
        gateway._call_worker("book", make_book())
        oms = OMS(engine, gateway, config)
        engine.register(EVENT_EXCHANGE_ORDER_UPDATE, oms.on_exchange_update)
        engine.register(EVENT_EXCHANGE_ACCOUNT_UPDATE, oms.on_exchange_account_update)
        oms.state = LifecycleState.LIVE

        commit_entered = threading.Event()
        release_commit = threading.Event()
        submit_result = []
        real_commit = gateway.commit_order_submission

        def blocked_commit(client_oid):
            commit_entered.set()
            release_commit.wait(timeout=1.0)
            return real_commit(client_oid)

        def submit():
            submit_result.append(
                oms.submit_order(
                    OrderIntent(
                        strategy_id="paper-cancel-race",
                        symbol=SYMBOL,
                        side=Side.BUY,
                        price=100.0,
                        volume=0.1,
                        time_in_force=TIF_GTX,
                        is_post_only=True,
                    )
                )
            )

        try:
            with patch(
                "gateway.binance.paper_gateway.ref_data_manager.get_info",
                return_value=make_contract(),
            ), patch(
                "oms.validator.data_cache.get_best_quote",
                return_value=(100.0, 101.0),
            ), patch(
                "oms.validator.data_cache.get_mark_price",
                return_value=100.5,
            ), patch.object(
                gateway,
                "commit_order_submission",
                side_effect=blocked_commit,
            ):
                submit_thread = threading.Thread(target=submit)
                submit_thread.start()
                self.assertTrue(commit_entered.wait(timeout=1.0))
                self.assertEqual(len(oms.orders), 1)
                client_oid = next(iter(oms.orders))
                self.assertEqual(
                    oms.orders[client_oid].status,
                    OrderStatus.PENDING_ACK,
                )

                self.assertTrue(oms.cancel_order(client_oid))
                self.assertEqual(
                    oms.orders[client_oid].status,
                    OrderStatus.CANCELLED,
                )
                release_commit.set()
                submit_thread.join(timeout=1.0)
                self.assertFalse(submit_thread.is_alive())

            self.assertEqual(len(submit_result), 1)
            self.assertTrue(submit_result[0].accepted)
            self.assertEqual(oms.orders[client_oid].status, OrderStatus.CANCELLED)
            self.assertEqual(gateway.get_open_orders(), [])
            self.assertEqual(gateway.get_order(SYMBOL, client_oid)["status"], "CANCELED")
            self.assertEqual(oms.get_symbol_freeze_reason(SYMBOL), "")
        finally:
            release_commit.set()
            oms.stop()
            gateway.close()

    def test_oms_to_paper_gateway_fill_and_local_truth_stay_consistent(self):
        engine = DispatchingEngine()
        config = make_gateway_config()
        config["account"]["initial_balance_usdt"] = 100.0
        config["risk"] = {
            "limits": {
                "max_order_qty": 100.0,
                "max_order_notional": 1000.0,
                "max_pos_notional": 1000.0,
                "max_account_gross_notional": 1000.0,
            },
            "price_sanity": {
                "max_deviation_pct": 0.05,
                "max_spread_pct": 0.05,
            },
            "risk_control_heartbeat": {"enabled": False},
            "cash_flow_truth": {"enabled": False},
            "independent_supervisor": {"enabled": False},
            "margin_health": {"enabled": True, "require_snapshot": True},
        }
        config["oms"] = {
            "journal_enabled": False,
            "replay_journal_on_startup": False,
            "truth_monitor": {
                "poll_interval_sec": 0,
                "account_balance_tolerance": 1e-8,
                "balance_drift_trigger_count": 1,
            },
        }
        gateway = BinancePaperGateway(engine, config)
        gateway._start_worker()
        gateway.active = True
        gateway._accepting_orders = True
        gateway.state = GatewayState.READY
        gateway._call_worker("book", make_book())
        oms = OMS(engine, gateway, config)
        engine.register(EVENT_EXCHANGE_ORDER_UPDATE, oms.on_exchange_update)
        engine.register(EVENT_EXCHANGE_ACCOUNT_UPDATE, oms.on_exchange_account_update)
        oms.state = LifecycleState.LIVE
        truth = PaperTruthSnapshotProvider(gateway)
        oms.sync_account_margin_health(
            truth.get_account_info(),
            snapshot_time=time.time(),
        )

        try:
            with patch(
                "gateway.binance.paper_gateway.ref_data_manager.get_info",
                return_value=make_contract(),
            ), patch(
                "oms.validator.data_cache.get_best_quote",
                return_value=(100.0, 101.0),
            ), patch(
                "oms.validator.data_cache.get_mark_price",
                return_value=100.5,
            ), patch(
                "oms.exposure.data_cache.get_best_quote",
                return_value=(100.0, 101.0),
            ), patch(
                "oms.exposure.data_cache.get_mark_price",
                return_value=100.5,
            ):
                result = oms.submit_order(
                    OrderIntent(
                        strategy_id="paper-integration",
                        symbol=SYMBOL,
                        side=Side.BUY,
                        price=101.0,
                        volume=0.1,
                        time_in_force=TIF_GTC,
                        is_post_only=False,
                    )
                )
                self.assertTrue(result.accepted, result.reason)
                self.assertTrue(
                    wait_until(
                        lambda: oms.orders[result.client_oid].status
                        == OrderStatus.FILLED,
                        timeout=1.0,
                    )
                )

            remote_position = float(truth.get_all_positions()[0]["positionAmt"])
            remote_balance = float(truth.get_account_info()["totalWalletBalance"])
            self.assertAlmostEqual(oms.exposure.net_positions[SYMBOL], remote_position)
            self.assertAlmostEqual(oms.account.balance, remote_balance)
            self.assertEqual(truth.get_open_orders(), [])

            monitor = TruthMonitor(oms, truth, config, start_thread=False)
            self.assertTrue(monitor.poll_once())
        finally:
            oms.stop()
            gateway.close()


if __name__ == "__main__":
    unittest.main()
