import json
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
    MarkPriceData,
    OrderBook,
    OrderBookGapError,
    OrderIntent,
    OrderRequest,
    OrderStatus,
    Side,
    TIF_GTC,
    TIF_GTX,
    TIF_IOC,
    TIF_RPI,
    EVENT_AGG_TRADE,
    EVENT_EXCHANGE_ACCOUNT_UPDATE,
    EVENT_EXCHANGE_ORDER_UPDATE,
    EVENT_MARK_PRICE,
    EVENT_ORDERBOOK,
    EVENT_SYSTEM_HEALTH,
)
from gateway.binance.paper_gateway import (
    BinancePaperGateway,
    PaperTruthSnapshotProvider,
)
from gateway.binance.ws_api import BinanceWsApi
from infrastructure.paper_trade import apply_paper_trade_mode
from infrastructure.system_health import handle_system_health_event
from infrastructure.truth_monitor import TruthMonitor
from infrastructure.venue_supervisor import VenueSupervisor
from main import build_gateway_bundle
from oms.engine import OMS
from oms.guard_manager import OMSGuardManager


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


def make_mark_payload(*, symbol=SYMBOL, mark_price="100.5"):
    now_ms = int(time.time() * 1000)
    return {
        "symbol": symbol,
        "markPrice": mark_price,
        "indexPrice": "100.4",
        "lastFundingRate": "0.0001",
        "nextFundingTime": now_ms + 3_600_000,
        "time": now_ms,
    }


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
        self.gateway._call_worker(
            "book",
            (self.gateway._book_generation, book or make_book()),
        )
        return self.gateway

    def order_updates(self, client_oid):
        return [
            event.data
            for event in self.engine.events
            if event.type == EVENT_EXCHANGE_ORDER_UPDATE
            and event.data.client_oid == client_oid
        ]

    @patch(
        "gateway.binance.paper_gateway.time_service.capture_timestamp",
        return_value=(2000.0, 70.0, 2000.0, 0.0),
    )
    @patch(
        "gateway.binance.paper_gateway.time.perf_counter",
        return_value=70.004,
    )
    @patch(
        "gateway.binance.paper_gateway.time.time",
        return_value=2000.008,
    )
    def test_public_market_timestamps_begin_at_websocket_callback(
        self,
        _time,
        _monotonic,
        _capture,
    ):
        self.gateway = BinancePaperGateway(self.engine, make_gateway_config())
        self.gateway.orderbooks = {SYMBOL: object()}
        self.gateway._submit_worker = lambda *_args, **_kwargs: True
        payload = json.dumps(
            {
                "stream": f"{SYMBOL.lower()}@aggTrade",
                "data": {
                    "E": 1_999_950,
                    "s": SYMBOL,
                    "a": 11,
                    "p": "100.0",
                    "q": "0.1",
                    "T": 1_999_975,
                    "m": False,
                },
            }
        )

        self.gateway.on_ws_message(payload)

        event = next(
            event
            for event in reversed(self.engine.events)
            if event.type == EVENT_AGG_TRADE
        )
        trade = event.data
        self.assertEqual(trade.exchange_timestamp, 1_999.975)
        self.assertEqual(trade.received_timestamp, 2_000.0)
        self.assertEqual(trade.received_monotonic, 70.0)
        self.assertEqual(trade.dispatch_timestamp, 2_000.008)
        self.assertEqual(trade.dispatch_monotonic, 70.004)

    @patch(
        "gateway.binance.paper_gateway.time_service.capture_timestamp",
        return_value=(2000.0, 70.0, 2000.0, 0.0),
    )
    def test_stale_public_trade_is_rejected_before_queues_and_faults_transport(
        self,
        _capture,
    ):
        config = make_gateway_config()
        config["system"]["market_data"][
            "max_market_event_ingress_age_ms"
        ] = 1200.0
        self.gateway = BinancePaperGateway(self.engine, config)
        self.gateway.orderbooks = {SYMBOL: object()}
        self.gateway.active = True
        self.gateway.state = GatewayState.READY
        self.gateway._submit_worker = lambda *_args, **_kwargs: True
        payload = json.dumps(
            {
                "stream": f"{SYMBOL.lower()}@aggTrade",
                "data": {
                    "E": 1_997_000,
                    "s": SYMBOL,
                    "a": 11,
                    "p": "100.0",
                    "q": "0.1",
                    "T": 1_997_000,
                    "m": False,
                },
            }
        )

        self.gateway.on_ws_message(payload)

        self.assertFalse(
            any(event.type == EVENT_AGG_TRADE for event in self.engine.events)
        )
        self.assertFalse(self.gateway.active)
        self.assertEqual(self.gateway.state, GatewayState.ERROR)
        self.assertTrue(
            any(
                event.type == EVENT_SYSTEM_HEALTH
                and str(event.data).startswith(
                    "FREEZE_VENUE:BINANCE_PAPER:"
                    "MARKET_DATA_STALE:PUBLIC_TRADE:"
                )
                for event in self.engine.events
            )
        )

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
            self.gateway.rest,
            "get_premium_index",
            return_value=make_mark_payload(),
        ) as premium_index, patch.object(
            BinanceWsApi,
            "start_market_stream",
        ) as market_stream, patch.object(
            BinanceWsApi,
            "wait_until_connected",
            return_value=True,
        ) as wait_until_connected, patch.object(
            BinanceWsApi,
            "start_user_stream",
            side_effect=AssertionError("paper mode must never start a user stream"),
        ) as user_stream:
            self.assertTrue(self.gateway.connect([SYMBOL]))

        depth.assert_called_once_with(SYMBOL)
        premium_index.assert_called_once_with(SYMBOL, timeout_sec=2.0)
        market_stream.assert_called_once_with([SYMBOL])
        wait_until_connected.assert_called_once_with(
            names=("PublicWS", "MarketWS"),
            timeout_sec=10.0,
        )
        user_stream.assert_not_called()
        self.assertFalse(self.gateway.testnet)
        self.assertEqual(self.gateway.environment, "PAPER_LIVE_DATA")
        self.assertFalse(hasattr(self.gateway, "api_key"))
        self.assertFalse(hasattr(self.gateway, "api_secret"))
        self.assertFalse(hasattr(self.gateway.rest, "api_key"))
        self.assertFalse(hasattr(self.gateway.rest, "api_secret"))
        self.assertNotIn("X-MBX-APIKEY", self.gateway.rest.session.headers)
        mark_event = next(
            event
            for event in self.engine.events
            if event.type == EVENT_MARK_PRICE
        )
        self.assertEqual(mark_event.data.symbol, SYMBOL)
        self.assertEqual(mark_event.data.mark_price, 100.5)
        self.assertTrue(
            wait_until(
                lambda: self.gateway._marks.get(SYMBOL) == 100.5
            )
        )

    def test_connect_fails_closed_when_initial_mark_is_unavailable(self):
        self.gateway = BinancePaperGateway(self.engine, make_gateway_config())
        self.gateway.mark_startup_timeout_sec = 0.01
        snapshot = {
            "lastUpdateId": 10,
            "bids": [["100", "1"]],
            "asks": [["101", "1"]],
        }

        with patch.object(
            self.gateway.rest,
            "get_depth_snapshot",
            return_value=snapshot,
        ), patch.object(
            self.gateway.rest,
            "get_premium_index",
            return_value=None,
        ), patch.object(
            BinanceWsApi,
            "start_market_stream",
        ), patch.object(
            BinanceWsApi,
            "wait_until_connected",
            return_value=True,
        ):
            self.assertFalse(self.gateway.connect([SYMBOL]))

        self.assertEqual(self.gateway.state, GatewayState.ERROR)
        self.assertFalse(self.gateway._accepting_orders)
        self.assertFalse(self.gateway.active)
        self.assertTrue(
            any(
                event.type == EVENT_SYSTEM_HEALTH
                and event.data
                == "FREEZE_VENUE:BINANCE_PAPER:PUBLIC_MARK_INITIALIZATION_FAILED"
                for event in self.engine.events
            )
        )

    def test_connect_fails_closed_before_snapshot_when_market_stream_is_unready(self):
        self.gateway = BinancePaperGateway(self.engine, make_gateway_config())

        with patch.object(
            self.gateway.rest,
            "get_depth_snapshot",
        ) as depth, patch.object(
            BinanceWsApi,
            "start_market_stream",
        ), patch.object(
            BinanceWsApi,
            "wait_until_connected",
            return_value=False,
        ) as wait_until_connected:
            self.assertFalse(self.gateway.connect([SYMBOL]))

        wait_until_connected.assert_called_once_with(
            names=("PublicWS", "MarketWS"),
            timeout_sec=10.0,
        )
        depth.assert_not_called()
        self.assertEqual(self.gateway.state, GatewayState.ERROR)
        self.assertFalse(self.gateway._accepting_orders)
        self.assertFalse(self.gateway.active)
        self.assertTrue(
            any(
                event.type == EVENT_SYSTEM_HEALTH
                and event.data
                == "FREEZE_VENUE:BINANCE_PAPER:PUBLIC_STREAM_READY_TIMEOUT"
                for event in self.engine.events
            )
        )

    def test_rest_mark_fallback_refreshes_stale_websocket_mark(self):
        gateway = self.start_offline()
        gateway.mark_rest_poll_interval_sec = 0.01
        gateway.mark_ws_stale_after_sec = 0.01
        generation = gateway._book_generation

        with patch.object(
            gateway.rest,
            "get_all_premium_indexes",
            return_value=[make_mark_payload()],
        ) as premium_index:
            gateway._start_mark_fallback(generation)
            self.assertTrue(
                wait_until(
                    lambda: gateway._marks.get(SYMBOL) == 100.5
                )
            )
            gateway._mark_fallback_stop.set()

        premium_index.assert_called()
        self.assertTrue(
            any(
                event.type == EVENT_MARK_PRICE
                and event.data.symbol == SYMBOL
                and event.data.mark_price == 100.5
                for event in self.engine.events
            )
        )

    def test_rest_mark_fallback_refreshes_all_stale_symbols_in_one_batch(self):
        gateway = self.start_offline()
        second_symbol = "XAUUSDT"
        gateway.symbols = [SYMBOL, second_symbol]
        gateway.mark_rest_poll_interval_sec = 0.1
        gateway.mark_ws_stale_after_sec = 0.01
        generation = gateway._book_generation
        payloads = [
            make_mark_payload(),
            make_mark_payload(symbol=second_symbol, mark_price="200.5"),
        ]

        with patch.object(
            gateway.rest,
            "get_all_premium_indexes",
            return_value=payloads,
        ) as premium_indexes, patch.object(
            gateway.rest,
            "get_premium_index",
        ) as single_premium_index:
            gateway._start_mark_fallback(generation)
            self.assertTrue(
                wait_until(
                    lambda: gateway._marks.get(SYMBOL) == 100.5
                    and gateway._marks.get(second_symbol) == 200.5
                )
            )
            gateway._mark_fallback_stop.set()

        premium_indexes.assert_called_once_with(
            timeout_sec=gateway.mark_rest_request_timeout_sec,
        )
        single_premium_index.assert_not_called()

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

    def test_recovery_cannot_overwrite_concurrent_paper_fault(self):
        gateway = self.start_offline()
        gateway._wait_for_initial_marks = lambda _generation: True
        entered_resync = threading.Event()
        release_resync = threading.Event()
        recovery_ws = SimpleNamespace(
            start_market_stream=lambda _symbols: None,
            wait_until_connected=lambda **_kwargs: True,
            close=lambda: None,
        )
        gateway._new_public_ws = lambda _generation: recovery_ws

        def blocked_resync(_symbol, **_kwargs):
            entered_resync.set()
            release_resync.wait(timeout=1.0)
            return True

        gateway._resync_book = blocked_resync
        results = []
        thread = threading.Thread(
            target=lambda: results.append(gateway.recover_connectivity())
        )
        try:
            thread.start()
            self.assertTrue(entered_resync.wait(timeout=1.0))
            gateway._fault("PAPER_COMMAND_QUEUE_FULL")
            release_resync.set()
            thread.join(timeout=1.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual(results, [False])
            self.assertEqual(gateway.state, GatewayState.ERROR)
            self.assertFalse(gateway.active)
            self.assertFalse(gateway._accepting_orders)
            self.assertFalse(
                any(
                    str(event.data).startswith(
                        ("VERIFY_VENUE:", "CLEAR_VENUE:")
                    )
                    for event in self.engine.events
                )
            )
        finally:
            release_resync.set()
            thread.join(timeout=1.0)

    def test_close_cannot_be_overwritten_by_paper_recovery(self):
        gateway = self.start_offline()
        gateway._wait_for_initial_marks = lambda _generation: True
        entered_resync = threading.Event()
        release_resync = threading.Event()
        recovery_ws = SimpleNamespace(
            start_market_stream=lambda _symbols: None,
            wait_until_connected=lambda **_kwargs: True,
            close=lambda: None,
        )
        gateway._new_public_ws = lambda _generation: recovery_ws

        def blocked_resync(_symbol, **_kwargs):
            entered_resync.set()
            release_resync.wait(timeout=1.0)
            return True

        gateway._resync_book = blocked_resync
        results = []
        recovery_thread = threading.Thread(
            target=lambda: results.append(gateway.recover_connectivity())
        )
        close_thread = threading.Thread(target=gateway.close)
        try:
            recovery_thread.start()
            self.assertTrue(entered_resync.wait(timeout=1.0))
            close_thread.start()
            self.assertTrue(wait_until(lambda: gateway._closing))
            release_resync.set()
            recovery_thread.join(timeout=1.0)
            close_thread.join(timeout=1.0)

            self.assertFalse(recovery_thread.is_alive())
            self.assertFalse(close_thread.is_alive())
            self.assertEqual(results, [False])
            self.assertEqual(gateway.state, GatewayState.DISCONNECTED)
            self.assertFalse(gateway.active)
            self.assertFalse(gateway._accepting_orders)
            self.assertFalse(
                any(
                    str(event.data).startswith(
                        ("VERIFY_VENUE:", "CLEAR_VENUE:")
                    )
                    for event in self.engine.events
                )
            )
        finally:
            release_resync.set()
            recovery_thread.join(timeout=1.0)
            close_thread.join(timeout=1.0)

    def test_book_publish_propagates_matching_queue_failure(self):
        gateway = self.start_offline()
        symbol = SYMBOL
        book = object()
        gateway.orderbooks[symbol] = book
        generation = gateway._book_generation
        events_before = len(self.engine.events)

        def fail_enqueue(*_args, **_kwargs):
            gateway._fault("PAPER_COMMAND_QUEUE_FULL")
            return False

        with patch.object(gateway, "_submit_worker", side_effect=fail_enqueue):
            published = gateway._publish_book_update(
                generation,
                symbol=symbol,
                expected_book=book,
                event_book=None,
                matching_book=object(),
            )

        self.assertFalse(published)
        self.assertEqual(gateway.state, GatewayState.ERROR)
        self.assertFalse(gateway.active)
        self.assertFalse(gateway._accepting_orders)
        self.assertGreater(len(self.engine.events), events_before)
        self.assertEqual(
            self.engine.events[-1].data,
            "FREEZE_VENUE:BINANCE_PAPER:PAPER_COMMAND_QUEUE_FULL",
        )

    def test_supervisor_recovers_masked_transport_owner_and_resets_epoch_budget(self):
        owners = {
            "system_health:transport": {
                "reason": "system_health:WS_TRANSPORT_DROP:first",
                "epoch": 1,
            },
            "event_engine_backlog": {
                "reason": "event_engine_backlog:market:newer",
                "epoch": 2,
            },
        }
        oms = SimpleNamespace(
            get_venue_freeze_reason=lambda _venue: (
                "event_engine_backlog:market:newer"
            ),
            get_venue_freeze_owners=lambda _venue: dict(owners),
        )
        recover_calls = []
        gateway = SimpleNamespace(
            gateway_name="BINANCE",
            recover_connectivity=lambda **kwargs: recover_calls.append(
                kwargs
            )
            or True,
        )
        supervisor = VenueSupervisor(
            oms,
            gateway,
            {
                "oms": {
                    "venue_supervisor": {
                        "poll_interval_sec": 0.0,
                        "recovery_delay_sec": 0.0,
                        "max_attempts": 1,
                    }
                }
            },
            start_thread=False,
        )

        self.assertTrue(supervisor.poll_once())
        self.assertFalse(supervisor.poll_once())
        self.assertEqual(
            recover_calls[0]["recovery_context"],
            {
                "venue": "BINANCE",
                "owner": "system_health:transport",
                "epoch": 1,
                "reason": "system_health:WS_TRANSPORT_DROP:first",
            },
        )

        owners["system_health:transport"] = {
            "reason": "system_health:WS_TRANSPORT_DROP:second",
            "epoch": 3,
        }
        self.assertTrue(supervisor.poll_once())
        self.assertEqual(
            recover_calls[-1]["recovery_context"]["epoch"],
            3,
        )

    def test_exact_venue_verification_preserves_owner_epoch_and_reason(self):
        requests = []
        oms = SimpleNamespace(
            get_venue_freeze_owners=lambda _venue: {
                "system_health:transport": {
                    "reason": "system_health:WS_TRANSPORT_DROP:first",
                    "epoch": 7,
                }
            },
            request_venue_recovery_verification=lambda venue, **kwargs: (
                requests.append((venue, kwargs))
            ),
        )

        handle_system_health_event(
            SimpleNamespace(
                data="VERIFY_VENUE:BINANCE:7:system_health:transport"
            ),
            None,
            oms,
        )

        self.assertEqual(
            requests,
            [
                (
                    "BINANCE",
                    {
                        "reason": "system_health:TRANSPORT_RECOVERED",
                        "expected_owner": "system_health:transport",
                        "expected_epoch": 7,
                        "expected_reason": (
                            "system_health:WS_TRANSPORT_DROP:first"
                        ),
                    },
                )
            ],
        )

    def test_paper_risk_increasing_send_rejected_during_book_resync(self):
        gateway = self.start_offline()
        with gateway._book_lock:
            gateway.book_resyncing.add(SYMBOL)
            gateway.ws_buffer[SYMBOL] = []

        result = gateway.send_order(
            OrderRequest(
                SYMBOL,
                99.0,
                1.0,
                "BUY",
                time_in_force=TIF_GTX,
                post_only=True,
            ),
            "paper-book-gap",
        )

        self.assertEqual(result.outcome, CommandOutcome.REJECTED)
        self.assertEqual(result.error_code, "PAPER_ORDERBOOK_NOT_READY")

    def test_stale_book_recovery_cannot_clear_newer_symbol_guard(self):
        self.gateway = BinancePaperGateway(self.engine, make_gateway_config())
        gateway = self.gateway
        first_started = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()
        release_second = threading.Event()
        first_returned = threading.Event()
        call_lock = threading.Lock()
        call_count = 0

        def blocked_resync(_symbol, **_kwargs):
            nonlocal call_count
            with call_lock:
                call_count += 1
                recovery_number = call_count
            if recovery_number == 1:
                first_started.set()
                release_first.wait(timeout=1.0)
                first_returned.set()
            else:
                second_started.set()
                release_second.wait(timeout=1.0)
            return True

        oms = OMS.__new__(OMS)
        oms.lock = threading.RLock()
        oms.symbol_guards = {}
        oms.symbol_guard_epochs = {}
        oms._refresh_outbound_gate_locked = lambda *_args: None
        oms._audit = lambda *_args, **_kwargs: None
        oms._wait_for_outbound_risk_sends = (
            lambda *_args, **_kwargs: True
        )
        oms._cancel_all_orders_unchecked = (
            lambda *_args, **_kwargs: True
        )
        oms.guard_manager = OMSGuardManager(oms)
        self.engine.register(
            EVENT_SYSTEM_HEALTH,
            lambda event: handle_system_health_event(event, None, oms),
        )

        gateway._resync_book = blocked_resync
        try:
            gateway._schedule_book_recovery(SYMBOL, freeze_reason="FATAL_GAP")
            self.assertTrue(first_started.wait(timeout=1.0))

            gateway._reset_public_books()
            gateway._schedule_book_recovery(SYMBOL, freeze_reason="FATAL_GAP")
            self.assertTrue(second_started.wait(timeout=1.0))
            self.assertEqual(
                oms.get_symbol_freeze_reason(SYMBOL),
                "system_health:FATAL_GAP:2",
            )

            release_first.set()
            self.assertTrue(first_returned.wait(timeout=1.0))
            time.sleep(0.02)
            self.assertFalse(
                any(
                    event.data
                    == f"CLEAR_SYMBOL:{SYMBOL}:ORDERBOOK_RESYNCED:1"
                    for event in self.engine.events
                    if event.type == EVENT_SYSTEM_HEALTH
                )
            )
            self.assertEqual(
                oms.get_symbol_freeze_reason(SYMBOL),
                "system_health:FATAL_GAP:2",
            )

            release_second.set()
            self.assertTrue(
                wait_until(lambda: oms.get_symbol_freeze_reason(SYMBOL) == "")
            )
            self.assertTrue(wait_until(lambda: SYMBOL not in gateway.book_resyncing))
        finally:
            release_first.set()
            release_second.set()

    def test_old_recovery_thread_cannot_mutate_book_after_reset(self):
        self.gateway = BinancePaperGateway(self.engine, make_gateway_config())
        gateway = self.gateway
        gateway.symbols = [SYMBOL]
        gateway._reset_public_books()
        snapshot_started = threading.Event()
        release_snapshot = threading.Event()
        recovery_finished = threading.Event()

        def blocked_snapshot(_symbol):
            snapshot_started.set()
            release_snapshot.wait(timeout=1.0)
            return {
                "lastUpdateId": 10,
                "bids": [["100.0", "1.0"]],
                "asks": [["100.5", "1.0"]],
            }

        original_resync = gateway._resync_book

        def observed_resync(*args, **kwargs):
            try:
                return original_resync(*args, **kwargs)
            finally:
                recovery_finished.set()

        gateway.rest.get_depth_snapshot = blocked_snapshot
        gateway._resync_book = observed_resync
        gateway._schedule_book_recovery(SYMBOL, freeze_reason="FATAL_GAP")
        self.assertTrue(snapshot_started.wait(timeout=1.0))

        gateway._reset_public_books()
        replacement_book = gateway.orderbooks[SYMBOL]
        release_snapshot.set()
        self.assertTrue(recovery_finished.wait(timeout=1.0))
        time.sleep(0.02)

        self.assertIs(gateway.orderbooks[SYMBOL], replacement_book)
        self.assertFalse(replacement_book.initialized)
        self.assertFalse(
            any(
                event.data == f"CLEAR_SYMBOL:{SYMBOL}:ORDERBOOK_RESYNCED:1"
                for event in self.engine.events
                if event.type == EVENT_SYSTEM_HEALTH
            )
        )

    def test_paper_gap_claims_new_owner_before_recovery_launch(self):
        self.gateway = BinancePaperGateway(self.engine, make_gateway_config())
        gateway = self.gateway
        gateway.symbols = [SYMBOL]
        generation = gateway._reset_public_books()
        gateway._book_recovery_token = 1
        gateway.book_resyncing = {SYMBOL}
        gateway.book_recovery_generation = {SYMBOL: generation}
        gateway.book_recovery_tokens = {SYMBOL: 1}
        gateway.ws_buffer[SYMBOL] = None

        class BrokenBook:
            def process_delta(self, _delta):
                raise OrderBookGapError("forced paper gap")

        gateway.orderbooks[SYMBOL] = BrokenBook()
        launch_entered = threading.Event()
        allow_launch = threading.Event()
        launched = []

        def blocked_launch(recovery):
            launched.append(recovery)
            launch_entered.set()
            allow_launch.wait(timeout=1.0)

        gateway._launch_book_recovery = blocked_launch
        worker = threading.Thread(
            target=lambda: gateway._process_book_delta(
                SYMBOL,
                {"U": 2, "u": 2, "pu": 0, "b": [], "a": []},
                expected_generation=generation,
            )
        )
        worker.start()
        self.assertTrue(launch_entered.wait(timeout=1.0))

        with gateway._book_lock:
            self.assertEqual(gateway.book_recovery_tokens[SYMBOL], 2)
            self.assertFalse(
                gateway._release_book_recovery_locked(SYMBOL, generation, 1)
            )

        allow_launch.set()
        worker.join(timeout=1.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(launched[0][2], 2)

    def test_superseded_paper_book_cannot_publish_with_same_generation(self):
        self.gateway = BinancePaperGateway(self.engine, make_gateway_config())
        gateway = self.gateway
        gateway.symbols = [SYMBOL]
        generation = gateway._reset_public_books()
        gateway._book_recovery_token = 1
        gateway.book_resyncing = {SYMBOL}
        gateway.book_recovery_generation = {SYMBOL: generation}
        gateway.book_recovery_tokens = {SYMBOL: 1}
        gateway.rest.get_depth_snapshot = lambda _symbol: {
            "lastUpdateId": 10,
            "bids": [["100.0", "1.0"]],
            "asks": [["100.5", "1.0"]],
        }
        submitted = []
        gateway._submit_worker = lambda kind, payload: submitted.append(
            (kind, payload)
        ) or True
        publish_entered = threading.Event()
        allow_publish = threading.Event()
        original_publish = gateway._publish_book_update

        def blocked_publish(*args, **kwargs):
            publish_entered.set()
            allow_publish.wait(timeout=1.0)
            return original_publish(*args, **kwargs)

        gateway._publish_book_update = blocked_publish
        results = []
        worker = threading.Thread(
            target=lambda: results.append(
                gateway._resync_book(
                    SYMBOL,
                    expected_generation=generation,
                    recovery_token=1,
                )
            )
        )
        worker.start()
        self.assertTrue(publish_entered.wait(timeout=1.0))

        with gateway._book_lock:
            replacement = gateway._begin_book_recovery_locked(
                SYMBOL,
                freeze_reason="FATAL_GAP",
                expected_generation=generation,
            )
        self.assertIsNotNone(replacement)
        allow_publish.set()
        worker.join(timeout=1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [False])
        self.assertEqual(submitted, [])
        self.assertFalse(
            any(event.type == EVENT_ORDERBOOK for event in self.engine.events)
        )

    def test_stale_public_trade_and_mark_cannot_publish_or_reach_matching(self):
        self.gateway = BinancePaperGateway(self.engine, make_gateway_config())
        gateway = self.gateway
        gateway.symbols = [SYMBOL]
        submitted = []
        gateway._submit_worker = lambda kind, payload: submitted.append(
            (kind, payload)
        ) or True

        cases = [
            (
                f"{SYMBOL.lower()}@aggTrade",
                {
                    "E": 1_000,
                    "s": SYMBOL,
                    "a": 101,
                    "p": "100.0",
                    "q": "1.0",
                    "T": 1_000,
                    "m": True,
                },
            ),
            (
                f"{SYMBOL.lower()}@markPrice",
                {
                    "E": 2_000,
                    "s": SYMBOL,
                    "p": "101.0",
                    "i": "100.5",
                    "r": "0.0001",
                    "T": 3_000,
                },
            ),
        ]
        for stream, data in cases:
            with self.subTest(stream=stream):
                generation = gateway._reset_public_books()
                publish_entered = threading.Event()
                allow_publish = threading.Event()
                original_publish = gateway._publish_public_market_update

                def blocked_publish(*args, **kwargs):
                    publish_entered.set()
                    allow_publish.wait(timeout=1.0)
                    return original_publish(*args, **kwargs)

                gateway._publish_public_market_update = blocked_publish
                before_events = len(self.engine.events)
                before_submissions = len(submitted)
                worker = threading.Thread(
                    target=lambda: gateway._handle_market_message(
                        stream,
                        data,
                        received_timestamp=float(data["E"]) / 1000.0,
                        received_monotonic=1.0,
                        corrected_received_timestamp=(
                            float(data["E"]) / 1000.0
                        ),
                        clock_offset_ms=0.0,
                        expected_generation=generation,
                    )
                )
                worker.start()
                self.assertTrue(publish_entered.wait(timeout=1.0))
                gateway._reset_public_books()
                allow_publish.set()
                worker.join(timeout=1.0)
                gateway._publish_public_market_update = original_publish

                self.assertFalse(worker.is_alive())
                self.assertEqual(len(self.engine.events), before_events)
                self.assertEqual(len(submitted), before_submissions)

        stale_generation = gateway._book_generation - 1
        stale_trade = AggTradeData(
            SYMBOL,
            202,
            100.0,
            1.0,
            True,
            datetime.now(),
        )
        with patch.object(gateway, "_on_market_trade") as match_trade:
            self.assertFalse(
                gateway._dispatch_command(
                    "market_trade",
                    (stale_generation, stale_trade),
                )
            )
        match_trade.assert_not_called()

        stale_mark = MarkPriceData(
            symbol=SYMBOL,
            mark_price=101.0,
            index_price=100.5,
            funding_rate=0.0001,
            next_funding_time=datetime.now(),
            datetime=datetime.now(),
        )
        self.assertFalse(
            gateway._dispatch_command(
                "mark",
                (stale_generation, stale_mark),
            )
        )
        self.assertNotIn(SYMBOL, gateway._marks)

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
                (
                    gateway._book_generation,
                    make_book(bid_price=98.0, ask_price=99.0),
                ),
            )
            self.assertTrue(gateway.commit_order_submission("gtx-revalidate"))

        updates = self.order_updates("gtx-revalidate")
        self.assertEqual([update.status for update in updates], ["REJECTED"])
        self.assertEqual(gateway.get_open_orders(), [])
        self.assertEqual(
            gateway.get_order(SYMBOL, "gtx-revalidate")["status"],
            "REJECTED",
        )

    def test_commit_rechecks_clock_after_staging(self):
        gateway = self.start_offline()
        gateway.require_healthy_clock = True
        request = OrderRequest(
            symbol=SYMBOL,
            price=100.0,
            volume=0.1,
            side="BUY",
            time_in_force=TIF_GTX,
            post_only=True,
        )
        clock_states = [
            {"ready": True, "state": "healthy", "reason": ""},
            {
                "ready": False,
                "state": "degraded",
                "reason": "calibration stale",
            },
        ]
        with (
            patch(
                "gateway.binance.paper_gateway.ref_data_manager.get_info",
                return_value=make_contract(),
            ),
            patch(
                "gateway.binance.paper_gateway.time_service.health_snapshot",
                side_effect=clock_states,
            ),
        ):
            staged = gateway.send_order(request, "clock-recheck")
            self.assertEqual(staged.outcome, CommandOutcome.ACKNOWLEDGED)
            self.assertTrue(gateway.commit_order_submission("clock-recheck"))

        updates = self.order_updates("clock-recheck")
        self.assertEqual([update.status for update in updates], ["REJECTED"])
        order = gateway.get_order(SYMBOL, "clock-recheck")
        self.assertEqual(order["status"], "REJECTED")
        self.assertIn("CLOCK_UNHEALTHY", order["_paperTerminalReason"])

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
                (
                    gateway._book_generation,
                    AggTradeData(
                        SYMBOL,
                        1,
                        100.0,
                        1.2,
                        True,
                        datetime.now(),
                    ),
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
                (
                    gateway._book_generation,
                    AggTradeData(
                        SYMBOL,
                        2,
                        100.0,
                        0.3,
                        True,
                        datetime.now(),
                    ),
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
                (
                    gateway._book_generation,
                    AggTradeData(
                        SYMBOL,
                        3,
                        99.0,
                        100.0,
                        True,
                        datetime.now(),
                    ),
                ),
            )

        rpi_order = gateway.get_order(SYMBOL, "rpi-disabled")
        self.assertEqual(rpi_order["status"], "NEW")
        self.assertEqual(float(rpi_order["executedQty"]), 0.0)
        self.assertEqual(rpi_order["_paperFillModel"], "rpi_disabled")

    def test_rpi_proxy_fill_is_simulated_and_uses_final_rpi_fee(self):
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
                (
                    gateway._book_generation,
                    AggTradeData(
                        SYMBOL,
                        11,
                        99.0,
                        1.0,
                        True,
                        datetime.now(),
                    ),
                ),
            )

        order = gateway.get_order(SYMBOL, "rpi-proxy")
        self.assertEqual(order["status"], "FILLED")
        self.assertEqual(order["_paperFillModel"], "rpi_public_trade_proxy")
        trade = gateway.get_user_trades(SYMBOL)[0]
        self.assertTrue(trade["maker"])
        self.assertTrue(trade["_simulated"])
        self.assertEqual(trade["_fillModel"], "rpi_public_trade_proxy")
        self.assertAlmostEqual(float(trade["commission"]), 0.003)
        self.assertAlmostEqual(
            float(gateway.get_account_info()["totalWalletBalance"]),
            99.997,
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
                (
                    gateway._book_generation,
                    AggTradeData(
                        SYMBOL,
                        21,
                        100.0,
                        0.5,
                        True,
                        datetime.now(),
                    ),
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
                (
                    gateway._book_generation,
                    AggTradeData(
                        SYMBOL,
                        22,
                        100.0,
                        0.5,
                        True,
                        datetime.now(),
                    ),
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

    def test_shutdown_latch_keeps_truth_queries_and_target_cancel_available(self):
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
            result = gateway.send_order(request, "shutdown-target-cancel")
            self.assertEqual(result.outcome, CommandOutcome.ACKNOWLEDGED)
            self.assertTrue(
                gateway.commit_order_submission("shutdown-target-cancel")
            )

        open_orders = gateway.get_open_orders()
        self.assertEqual(len(open_orders), 1)
        exchange_oid = open_orders[0]["orderId"]

        self.assertTrue(gateway.begin_shutdown())
        self.assertTrue(gateway._closing)
        self.assertFalse(gateway.active)
        self.assertEqual(gateway.state, GatewayState.DISCONNECTED)
        self.assertEqual(len(gateway.get_open_orders()), 1)

        cancel = gateway.cancel_order(CancelRequest(SYMBOL, exchange_oid))
        self.assertEqual(cancel.status_code, 200)
        self.assertEqual(cancel.json()["status"], "CANCELED")
        self.assertEqual(gateway.get_open_orders(), [])
        self.assertEqual(
            gateway.get_order(SYMBOL, exchange_oid)["status"],
            "CANCELED",
        )

        rejected = gateway.send_order(request, "shutdown-reject-new-order")
        self.assertEqual(rejected.outcome, CommandOutcome.REJECTED)
        self.assertEqual(rejected.error_code, "PAPER_NOT_READY")


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

    def test_live_gateway_factory_shares_one_host_rate_limit_budget(self):
        config = {
            "execution": {"mode": "live"},
            "paper_trade": {"enabled": False},
            "api_key": "main-key",
            "api_secret": "main-secret",
            "testnet": False,
            "symbols": ["BTCUSDT"],
            "system": {
                "binance_rest_rate_limit": {
                    "enabled": True,
                    "request_weight_limit": 2400,
                    "trading_reserve": 300,
                    "emergency_reserve": 300,
                    "full_open_orders_audit_interval_sec": 45.0,
                }
            },
        }
        engine = DispatchingEngine()
        shared_budget = object()
        gateway_instance = object()
        truth_instance = object()

        with (
            patch(
                "main.BinanceRateLimitBudget.from_config",
                return_value=shared_budget,
            ) as budget_factory,
            patch(
                "main.BinanceGateway",
                return_value=gateway_instance,
            ) as gateway_type,
            patch(
                "main.BinanceTruthSnapshotProvider",
                return_value=truth_instance,
            ) as truth_type,
        ):
            gateway, truth = build_gateway_bundle(
                engine,
                config,
                {"environment": "production"},
            )

        self.assertIs(gateway, gateway_instance)
        self.assertIs(truth, truth_instance)
        budget_factory.assert_called_once_with(
            config["system"]["binance_rest_rate_limit"]
        )
        self.assertIs(
            gateway_type.call_args.kwargs["rate_limit_budget"],
            shared_budget,
        )
        self.assertIs(
            truth_type.call_args.kwargs["rate_limit_budget"],
            shared_budget,
        )
        self.assertEqual(
            truth_type.call_args.kwargs[
                "full_open_orders_audit_interval_sec"
            ],
            45.0,
        )

    def test_live_gateway_factory_rejects_disabled_host_budget(self):
        config = {
            "execution": {"mode": "live"},
            "paper_trade": {"enabled": False},
            "api_key": "main-key",
            "api_secret": "main-secret",
            "testnet": False,
            "symbols": ["BTCUSDT"],
            "system": {
                "binance_rest_rate_limit": {"enabled": False}
            },
        }

        with (
            patch("main.BinanceGateway") as gateway_type,
            self.assertRaisesRegex(
                RuntimeError,
                "cannot be disabled",
            ),
        ):
            build_gateway_bundle(
                DispatchingEngine(),
                config,
                {"environment": "production"},
            )

        gateway_type.assert_not_called()

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
        gateway._call_worker(
            "book",
            (gateway._book_generation, make_book()),
        )
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
        gateway._call_worker(
            "book",
            (gateway._book_generation, make_book()),
        )
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
        gateway._call_worker(
            "book",
            (gateway._book_generation, make_book()),
        )
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
