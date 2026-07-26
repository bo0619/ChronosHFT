import json
import sys
import threading
import time
import types
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

if "requests" not in sys.modules:
    requests_module = types.ModuleType("requests")

    class Request:
        def __init__(self, method, url, params=None, headers=None):
            self.method = method
            self.url = url
            self.params = params or {}
            self.headers = headers or {}

    class Session:
        def __init__(self):
            self.headers = {}

        def mount(self, *_args, **_kwargs):
            return None

        def close(self):
            return None

    requests_module.Request = Request
    requests_module.Session = Session
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

        def close(self):
            return None

    websocket_module.WebSocketApp = WebSocketApp
    sys.modules["websocket"] = websocket_module

from data.cache import data_cache
from data.orderbook import LocalOrderBook
from event.type import (
    CommandOutcome,
    EVENT_AGG_TRADE,
    EVENT_MARK_PRICE,
    EVENT_SYSTEM_HEALTH,
    GatewayState,
    MarkPriceData,
    OrderBookGapError,
    OrderRequest,
)
from gateway.binance.gateway import BinanceGateway
from gateway.binance.truth_provider import BinanceTruthSnapshotProvider
from infrastructure.venue_supervisor import VenueSupervisor


class DummyEngine:
    def __init__(self):
        self.events = []

    def put(self, event):
        self.events.append(event)


class DummySession:
    def __init__(self):
        self.headers = {}
        self.closed = False
        self.mount_calls = []

    def mount(self, prefix, adapter):
        self.mount_calls.append((prefix, adapter))

    def close(self):
        self.closed = True


class DummyResponse:
    def __init__(self, payload):
        self.status_code = 200
        self.payload = payload

    def json(self):
        return self.payload


class DummyRestApi:
    def __init__(self, api_key, api_secret, session, testnet):
        self.api_key = api_key
        self.api_secret = api_secret
        self.session = session
        self.testnet = testnet

    def get_account(self):
        return DummyResponse({"account": True})

    def get_positions(self):
        return DummyResponse([{"symbol": "BTCUSDT"}])

    def get_open_orders(self):
        return DummyResponse([{"symbol": "BTCUSDT", "orderId": 1}])

    def get_income_history(self, **kwargs):
        return DummyResponse([{"incomeType": "TRANSFER", "query": kwargs}])


class DummyOms:
    def __init__(self, venue_reason=""):
        self._venue_reason = venue_reason

    def get_venue_freeze_reason(self, _venue):
        return self._venue_reason


class DummyGateway:
    gateway_name = "BINANCE"

    def __init__(self, recover_result=True):
        self.calls = 0
        self.recover_result = recover_result

    def recover_connectivity(self):
        self.calls += 1
        return self.recover_result


class TruthProviderTests(unittest.TestCase):
    @patch("gateway.binance.truth_provider.requests.Session", return_value=DummySession())
    def test_truth_provider_owns_independent_session_and_closes_it(self, session_factory):
        provider = BinanceTruthSnapshotProvider(
            "key",
            "secret",
            testnet=True,
            rest_api_cls=DummyRestApi,
        )
        try:
            self.assertEqual(provider.get_account_info(), {"account": True})
            self.assertEqual(provider.get_all_positions(), [{"symbol": "BTCUSDT"}])
            self.assertEqual(provider.get_open_orders(), [{"symbol": "BTCUSDT", "orderId": 1}])
            self.assertEqual(
                provider.get_income_history(start_time=1000, limit=1000),
                [
                    {
                        "incomeType": "TRANSFER",
                        "query": {"start_time": 1000, "limit": 1000},
                    }
                ],
            )
            self.assertIs(provider.rest.session, provider.session)
            self.assertIsNotNone(session_factory.return_value.mount_calls)
        finally:
            provider.close()
        self.assertTrue(provider.session.closed)


class VenueSupervisorTests(unittest.TestCase):
    def make_config(self):
        return {
            "oms": {
                "venue_supervisor": {
                    "poll_interval_sec": 0.0,
                    "recovery_delay_sec": 0.0,
                    "max_attempts": 2,
                }
            }
        }

    def test_supervisor_recovers_on_recoverable_venue_freeze(self):
        supervisor = VenueSupervisor(
            DummyOms("system_health:WS_PARSE_ERROR"),
            DummyGateway(recover_result=True),
            self.make_config(),
            start_thread=False,
        )

        recovered = supervisor.poll_once()

        self.assertTrue(recovered)
        self.assertEqual(supervisor.gateway.calls, 1)

    def test_supervisor_recovers_on_transport_drop_venue_freeze(self):
        supervisor = VenueSupervisor(
            DummyOms("system_health:WS_TRANSPORT_DROP:UserWS:Connection to remote host was lost."),
            DummyGateway(recover_result=True),
            self.make_config(),
            start_thread=False,
        )

        recovered = supervisor.poll_once()

        self.assertTrue(recovered)
        self.assertEqual(supervisor.gateway.calls, 1)

    def test_supervisor_recovers_on_listen_key_keep_alive_failure(self):
        supervisor = VenueSupervisor(
            DummyOms(
                "system_health:USER_STREAM_KEEPALIVE_FAILED:status=503"
            ),
            DummyGateway(recover_result=True),
            self.make_config(),
            start_thread=False,
        )

        recovered = supervisor.poll_once()

        self.assertTrue(recovered)
        self.assertEqual(supervisor.gateway.calls, 1)

    def test_supervisor_ignores_non_recoverable_venue_freeze(self):
        supervisor = VenueSupervisor(
            DummyOms("truth_plane:api_unreachable:2"),
            DummyGateway(recover_result=True),
            self.make_config(),
            start_thread=False,
        )

        recovered = supervisor.poll_once()

        self.assertFalse(recovered)
        self.assertEqual(supervisor.gateway.calls, 0)


class MarketTimestampTests(unittest.TestCase):
    def test_buffered_depth_delta_preserves_websocket_ingress_timestamps(self):
        book = LocalOrderBook("BTCUSDT", publish_depth_levels=1)
        book.init_snapshot(
            {
                "lastUpdateId": 40,
                "bids": [["100.0", "1.0"]],
                "asks": [["100.5", "1.0"]],
            }
        )
        book.process_delta(
            {
                "U": 41,
                "u": 41,
                "pu": 40,
                "b": [["100.0", "2.0"]],
                "a": [],
                "E": 999_900,
                "_local_received_timestamp": 1_000.0,
                "_local_received_monotonic": 42.0,
            }
        )

        event_book = book.generate_event_data()

        self.assertEqual(event_book.exchange_timestamp, 999.9)
        self.assertEqual(event_book.received_timestamp, 1_000.0)
        self.assertEqual(event_book.received_monotonic, 42.0)
        self.assertGreater(event_book.dispatch_timestamp, 0.0)
        self.assertGreater(event_book.dispatch_monotonic, 42.0)

    def test_cache_freshness_uses_monotonic_ingress_not_exchange_wall_time(self):
        symbol = "CLOCKDOMAINUSDT"
        mark = MarkPriceData(
            symbol=symbol,
            mark_price=100.0,
            index_price=100.0,
            funding_rate=0.0,
            next_funding_time=datetime.fromtimestamp(10.0),
            datetime=datetime.fromtimestamp(1.0),
            exchange_timestamp=1.0,
            received_timestamp=2.0,
            received_monotonic=100.0,
        )
        try:
            data_cache.update_mark_price(mark)

            snapshot = data_cache.get_risk_snapshot(symbol, now=100.025)

            self.assertAlmostEqual(snapshot["mark_age_ms"], 25.0)
            self.assertEqual(snapshot["mark_update_time"], 100.0)
            self.assertEqual(snapshot["mark_update_wall_time"], 2.0)
            self.assertEqual(snapshot["update_clock"], "monotonic")
        finally:
            with data_cache._lock:
                data_cache.mark_prices.pop(symbol, None)
                data_cache.mark_update_times.pop(symbol, None)
                data_cache.mark_update_wall_times.pop(symbol, None)


class GatewayRecoveryTests(unittest.TestCase):
    @staticmethod
    def make_keep_alive_gateway():
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.event_engine = DummyEngine()
        gateway.gateway_name = "BINANCE"
        gateway._book_lock = threading.RLock()
        gateway._book_generation = 1
        gateway.keep_alive_generation = 1
        gateway.active = True
        gateway.state = GatewayState.READY
        gateway.set_state = lambda state: setattr(gateway, "state", state)
        gateway.ws = SimpleNamespace(close=lambda: None)
        return gateway

    def test_orderbook_recovery_freeze_and_clear_share_unique_token(self):
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.event_engine = DummyEngine()
        gateway.gateway_name = "BINANCE"
        gateway._book_lock = threading.RLock()
        gateway._book_generation = 3
        gateway._book_recovery_token = 0
        gateway.book_resyncing = set()
        gateway.book_recovery_generation = {}
        gateway.book_recovery_tokens = {}
        gateway.orderbooks = {}
        gateway.ws_buffer = {}
        gateway.book_resync_max_attempts = 1
        gateway.book_resync_retry_sec = 0.0
        gateway._new_local_orderbook = lambda symbol: LocalOrderBook(symbol)
        gateway._resync_book = lambda symbol, **_kwargs: True

        gateway._schedule_book_recovery("BTCUSDT", freeze_reason="FATAL_GAP")
        deadline = time.perf_counter() + 1.0
        while "BTCUSDT" in gateway.book_resyncing and time.perf_counter() < deadline:
            time.sleep(0.005)

        messages = [event.data for event in gateway.event_engine.events]
        self.assertEqual(
            messages,
            [
                "FREEZE_SYMBOL:BTCUSDT:FATAL_GAP:1",
                "CLEAR_SYMBOL:BTCUSDT:ORDERBOOK_RESYNCED:1",
            ],
        )

    def test_new_gap_supersedes_inflight_recovery_owner_before_clear(self):
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.event_engine = DummyEngine()
        gateway.gateway_name = "BINANCE"
        gateway._book_lock = threading.RLock()
        gateway._book_generation = 1
        gateway._book_recovery_token = 0
        gateway.book_resyncing = set()
        gateway.book_recovery_generation = {}
        gateway.book_recovery_tokens = {}
        gateway.orderbooks = {}
        gateway.ws_buffer = {}
        gateway.book_resync_max_attempts = 1
        gateway.book_resync_retry_sec = 0.0
        gateway._new_local_orderbook = lambda symbol: LocalOrderBook(symbol)
        first_started = threading.Event()
        second_started = threading.Event()
        first_returned = threading.Event()
        release_first = threading.Event()
        release_second = threading.Event()

        def blocked_resync(_symbol, *, recovery_token=None, **_kwargs):
            if recovery_token == 1:
                first_started.set()
                release_first.wait(timeout=1.0)
                first_returned.set()
            else:
                second_started.set()
                release_second.wait(timeout=1.0)
            return True

        gateway._resync_book = blocked_resync
        gateway._schedule_book_recovery("BTCUSDT", freeze_reason="FATAL_GAP")
        self.assertTrue(first_started.wait(timeout=1.0))
        gateway._schedule_book_recovery("BTCUSDT", freeze_reason="FATAL_GAP")
        self.assertTrue(second_started.wait(timeout=1.0))

        release_first.set()
        self.assertTrue(first_returned.wait(timeout=1.0))
        time.sleep(0.02)
        messages = [event.data for event in gateway.event_engine.events]
        self.assertNotIn(
            "CLEAR_SYMBOL:BTCUSDT:ORDERBOOK_RESYNCED:1",
            messages,
        )
        self.assertEqual(gateway.book_recovery_tokens["BTCUSDT"], 2)

        release_second.set()
        deadline = time.perf_counter() + 1.0
        while "BTCUSDT" in gateway.book_resyncing and time.perf_counter() < deadline:
            time.sleep(0.005)
        messages = [event.data for event in gateway.event_engine.events]
        self.assertIn(
            "CLEAR_SYMBOL:BTCUSDT:ORDERBOOK_RESYNCED:2",
            messages,
        )

    def test_gap_claims_new_owner_before_recovery_launch(self):
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.event_engine = DummyEngine()
        gateway.gateway_name = "BINANCE"
        gateway._book_lock = threading.RLock()
        gateway._book_generation = 1
        gateway._book_recovery_token = 1
        gateway.book_resyncing = {"BTCUSDT"}
        gateway.book_recovery_generation = {"BTCUSDT": 1}
        gateway.book_recovery_tokens = {"BTCUSDT": 1}
        gateway.ws_buffer = {"BTCUSDT": None}
        gateway.max_book_buffer = 100
        gateway._new_local_orderbook = lambda symbol: LocalOrderBook(symbol)

        class BrokenBook:
            def process_delta(self, _delta):
                raise OrderBookGapError("forced gap")

        gateway.orderbooks = {"BTCUSDT": BrokenBook()}
        launch_entered = threading.Event()
        allow_launch = threading.Event()
        launched = []

        def blocked_launch(recovery):
            launched.append(recovery)
            launch_entered.set()
            allow_launch.wait(timeout=1.0)

        gateway._launch_book_recovery = blocked_launch
        worker = threading.Thread(
            target=lambda: gateway._process_book(
                "BTCUSDT",
                {"U": 2, "u": 2, "pu": 0, "b": [], "a": []},
                expected_generation=1,
            )
        )
        worker.start()
        self.assertTrue(launch_entered.wait(timeout=1.0))

        with gateway._book_lock:
            self.assertEqual(gateway.book_recovery_tokens["BTCUSDT"], 2)
            self.assertFalse(
                gateway._release_book_recovery_locked("BTCUSDT", 1, 1)
            )

        allow_launch.set()
        worker.join(timeout=1.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(launched[0][2], 2)

    def test_stale_live_transport_callbacks_cannot_touch_current_connection(self):
        engine = DummyEngine()
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.event_engine = engine
        gateway.gateway_name = "BINANCE"
        gateway._book_lock = threading.RLock()
        gateway._book_generation = 2
        gateway.latency_stats = {"rest_rtt": 0.0, "ws_delay": 0.0}
        gateway.active = True
        gateway.state = GatewayState.READY
        gateway.set_state = lambda state: setattr(gateway, "state", state)
        closed = []
        gateway.ws = SimpleNamespace(close=lambda: closed.append(True))
        payload = json.dumps(
            {
                "stream": "btcusdt@aggTrade",
                "data": {
                    "E": 1_000,
                    "s": "BTCUSDT",
                    "a": 1,
                    "p": "100.0",
                    "q": "1.0",
                    "T": 1_000,
                    "m": True,
                },
            }
        )

        gateway.on_ws_message(payload, expected_generation=1)
        gateway.on_ws_error(
            {"stream": "OldWS", "kind": "transport_drop", "detail": "late"},
            expected_generation=1,
        )

        self.assertEqual(engine.events, [])
        self.assertEqual(gateway.state, GatewayState.READY)
        self.assertTrue(gateway.active)
        self.assertEqual(closed, [])

        original_emit_fault = gateway._emit_ws_fault

        def advance_generation_then_emit(*args, **kwargs):
            with gateway._book_lock:
                gateway._book_generation += 1
            return original_emit_fault(*args, **kwargs)

        gateway._emit_ws_fault = advance_generation_then_emit
        gateway.on_ws_error(
            {"stream": "CurrentWS", "kind": "transport_drop", "detail": "race"},
            expected_generation=2,
        )

        self.assertEqual(engine.events, [])
        self.assertEqual(gateway.state, GatewayState.READY)
        self.assertTrue(gateway.active)
        self.assertEqual(closed, [])

    def test_listen_key_keep_alive_failure_freezes_current_transport(self):
        gateway = self.make_keep_alive_gateway()
        closed = []
        gateway.ws = SimpleNamespace(close=lambda: closed.append(True))
        gateway.rest = SimpleNamespace(
            keep_alive_listen_key=lambda: SimpleNamespace(status_code=503)
        )

        self.assertFalse(
            gateway._keep_alive_once(1, transport_generation=1)
        )

        self.assertFalse(gateway.active)
        self.assertEqual(gateway.state, GatewayState.ERROR)
        self.assertEqual(closed, [True])
        self.assertEqual(
            gateway.event_engine.events[-1].data,
            "FREEZE_VENUE:BINANCE:USER_STREAM_KEEPALIVE_FAILED: status=503",
        )

    def test_stale_keep_alive_failure_cannot_fault_replacement_transport(self):
        gateway = self.make_keep_alive_gateway()
        replacement_closed = []
        gateway.ws = SimpleNamespace(
            close=lambda: replacement_closed.append(True)
        )

        def stale_failure():
            with gateway._book_lock:
                gateway._book_generation = 2
                gateway.keep_alive_generation = 2
            return SimpleNamespace(status_code=503)

        gateway.rest = SimpleNamespace(
            keep_alive_listen_key=stale_failure
        )

        self.assertFalse(
            gateway._keep_alive_once(1, transport_generation=1)
        )

        self.assertTrue(gateway.active)
        self.assertEqual(gateway.state, GatewayState.READY)
        self.assertEqual(gateway.event_engine.events, [])
        self.assertEqual(replacement_closed, [])

    def test_resync_serializes_delta_at_snapshot_cutover(self):
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.event_engine = DummyEngine()
        gateway.gateway_name = "BINANCE"
        gateway._book_lock = threading.RLock()
        gateway._book_generation = 1
        gateway.max_book_buffer = 100
        gateway.book_resyncing = set()
        gateway.book_recovery_generation = {}
        gateway.orderbooks = {"BTCUSDT": LocalOrderBook("BTCUSDT")}
        gateway.ws_buffer = {
            "BTCUSDT": [
                {
                    "U": 101,
                    "u": 101,
                    "pu": 100,
                    "b": [["100.0", "2.0"]],
                    "a": [],
                }
            ]
        }
        gateway.rest = SimpleNamespace(
            get_depth_snapshot=lambda _symbol: {
                "lastUpdateId": 100,
                "bids": [["100.0", "1.0"]],
                "asks": [["100.5", "1.0"]],
            }
        )
        gateway._dispatch_market_data = lambda *_args, **_kwargs: None

        second_delta = {
            "U": 102,
            "u": 102,
            "pu": 101,
            "b": [["99.5", "1.0"]],
            "a": [],
        }
        worker_done = threading.Event()
        workers = []
        book = gateway.orderbooks["BTCUSDT"]
        original_generate = book.generate_event_data

        def generate_with_cutover_delta():
            if not workers:
                worker = threading.Thread(
                    target=lambda: (
                        gateway._process_book("BTCUSDT", second_delta),
                        worker_done.set(),
                    )
                )
                workers.append(worker)
                worker.start()
                worker_done.wait(0.05)
            return original_generate()

        book.generate_event_data = generate_with_cutover_delta

        self.assertTrue(
            gateway._resync_book("BTCUSDT", expected_generation=1)
        )
        workers[0].join(timeout=1.0)

        self.assertTrue(worker_done.is_set())
        self.assertEqual(book.last_update_id, 102)
        self.assertIsNone(gateway.ws_buffer["BTCUSDT"])

    @patch(
        "gateway.binance.gateway.time_service.capture_timestamp",
        return_value=(1000.0, 50.0, 1000.0, 0.0),
    )
    @patch("gateway.binance.gateway.time.perf_counter", return_value=50.005)
    @patch("gateway.binance.gateway.time.time", return_value=1000.01)
    def test_market_event_timestamps_begin_at_websocket_callback(
        self,
        _time,
        _monotonic,
        _capture,
    ):
        engine = DummyEngine()
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.event_engine = engine
        gateway.gateway_name = "BINANCE"
        gateway.latency_stats = {"rest_rtt": 0.0, "ws_delay": 0.0}
        payload = json.dumps(
            {
                "stream": "btcusdt@aggTrade",
                "data": {
                    "e": "aggTrade",
                    "E": 999_950,
                    "s": "BTCUSDT",
                    "a": 7,
                    "p": "100.0",
                    "q": "0.5",
                    "T": 999_975,
                    "m": True,
                },
            }
        )

        gateway.on_ws_message(payload)

        self.assertEqual(engine.events[-1].type, EVENT_AGG_TRADE)
        trade = engine.events[-1].data
        self.assertEqual(trade.exchange_timestamp, 999.975)
        self.assertEqual(trade.received_timestamp, 1000.0)
        self.assertEqual(trade.received_monotonic, 50.0)
        self.assertEqual(trade.dispatch_timestamp, 1000.01)
        self.assertEqual(trade.dispatch_monotonic, 50.005)

    @patch(
        "gateway.binance.gateway.time_service.capture_timestamp",
        return_value=(1_700_000_000.0, 60.0, 1_700_000_000.0, 0.0),
    )
    @patch("gateway.binance.gateway.time.perf_counter", return_value=60.003)
    @patch(
        "gateway.binance.gateway.time.time",
        return_value=1_700_000_000.006,
    )
    def test_mark_price_separates_event_time_from_next_funding_time(
        self,
        _time,
        _monotonic,
        _capture,
    ):
        engine = DummyEngine()
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.event_engine = engine
        gateway.gateway_name = "BINANCE"
        gateway.latency_stats = {"rest_rtt": 0.0, "ws_delay": 0.0}
        payload = json.dumps(
            {
                "stream": "btcusdt@markPrice",
                "data": {
                    "e": "markPriceUpdate",
                    "E": 1_699_999_999_950,
                    "s": "BTCUSDT",
                    "p": "100.0",
                    "i": "99.9",
                    "r": "0.0001",
                    "T": 1_700_003_600_000,
                },
            }
        )

        gateway.on_ws_message(payload)

        self.assertEqual(engine.events[-1].type, EVENT_MARK_PRICE)
        mark = engine.events[-1].data
        self.assertEqual(mark.exchange_timestamp, 1_699_999_999.95)
        self.assertEqual(mark.next_funding_time.timestamp(), 1_700_003_600.0)
        self.assertEqual(mark.received_timestamp, 1_700_000_000.0)
        self.assertEqual(mark.received_monotonic, 60.0)
        self.assertEqual(mark.dispatch_timestamp, 1_700_000_000.006)
        self.assertEqual(mark.dispatch_monotonic, 60.003)

    def test_transport_drop_fault_freezes_venue(self):
        engine = DummyEngine()
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.event_engine = engine
        gateway.gateway_name = "BINANCE"
        gateway.testnet = True
        gateway.symbols = ["BTCUSDT"]
        gateway.orderbooks = {}
        gateway.ws_buffer = {}
        gateway.book_resyncing = set()
        gateway.book_recovery_generation = {}
        gateway._book_generation = 0
        gateway._book_lock = threading.RLock()
        gateway.active = True
        gateway.listen_key = ""
        gateway.target_leverage = 0
        gateway.target_margin_type = "ISOLATED"
        gateway.target_position_mode = "ONE_WAY"
        gateway.recovery_lock = threading.Lock()
        gateway.keep_alive_generation = 0
        gateway.state = GatewayState.READY
        gateway.ws = SimpleNamespace(close=lambda: None)
        gateway.set_state = lambda state: setattr(gateway, "state", state)

        gateway.on_ws_error(
            {
                "stream": "UserWS",
                "kind": "transport_drop",
                "detail": "Connection to remote host was lost.",
            }
        )

        self.assertEqual(gateway.state, GatewayState.ERROR)
        self.assertEqual(engine.events[-1].type, EVENT_SYSTEM_HEALTH)
        self.assertEqual(
            engine.events[-1].data,
            "FREEZE_VENUE:BINANCE:WS_TRANSPORT_DROP: UserWS:Connection to remote host was lost.",
        )

    @patch("gateway.binance.gateway.BinanceWsApi")
    def test_recover_connectivity_requests_truth_verification(self, ws_type):
        engine = DummyEngine()
        ws_type.return_value.wait_until_connected.return_value = True
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.event_engine = engine
        gateway.gateway_name = "BINANCE"
        gateway.testnet = True
        gateway.symbols = ["BTCUSDT"]
        gateway.orderbooks = {}
        gateway.ws_buffer = {}
        gateway.book_resyncing = set()
        gateway.book_recovery_generation = {}
        gateway.book_recovery_tokens = {}
        gateway._book_recovery_token = 0
        gateway._book_generation = 0
        gateway._book_lock = threading.RLock()
        gateway.active = False
        gateway.listen_key = ""
        gateway.target_leverage = 0
        gateway.target_margin_type = "ISOLATED"
        gateway.target_position_mode = "ONE_WAY"
        gateway.recovery_lock = threading.Lock()
        gateway.keep_alive_generation = 0
        gateway.stream_ready_timeout_sec = 1.0
        gateway.state = GatewayState.ERROR
        gateway.ws = SimpleNamespace(close=lambda: None)
        gateway._start_streams = lambda **_kwargs: True
        gateway._resync_book = lambda symbol, **_kwargs: True

        recovered = gateway.recover_connectivity()

        self.assertTrue(recovered)
        self.assertEqual(engine.events[-1].type, EVENT_SYSTEM_HEALTH)
        self.assertEqual(engine.events[-1].data, "VERIFY_VENUE:BINANCE:WS_RECOVERED")

    @patch("gateway.binance.gateway.BinanceWsApi")
    def test_recover_connectivity_carries_exact_guard_context(self, ws_type):
        engine = DummyEngine()
        ws_type.return_value.wait_until_connected.return_value = True
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.event_engine = engine
        gateway.gateway_name = "BINANCE"
        gateway.testnet = True
        gateway.symbols = ["BTCUSDT"]
        gateway.orderbooks = {}
        gateway.ws_buffer = {}
        gateway.book_resyncing = set()
        gateway.book_recovery_generation = {}
        gateway.book_recovery_tokens = {}
        gateway._book_recovery_token = 0
        gateway._book_generation = 0
        gateway._book_lock = threading.RLock()
        gateway.active = False
        gateway.listen_key = ""
        gateway.target_leverage = 0
        gateway.target_margin_type = "ISOLATED"
        gateway.target_position_mode = "ONE_WAY"
        gateway.recovery_lock = threading.Lock()
        gateway.keep_alive_generation = 0
        gateway.stream_ready_timeout_sec = 1.0
        gateway.state = GatewayState.ERROR
        gateway.ws = SimpleNamespace(close=lambda: None)
        gateway._start_streams = lambda **_kwargs: True
        gateway._resync_book = lambda symbol, **_kwargs: True

        recovered = gateway.recover_connectivity(
            recovery_context={
                "owner": "system_health:transport",
                "epoch": 7,
                "reason": "system_health:WS_TRANSPORT_DROP:first",
            }
        )

        self.assertTrue(recovered)
        self.assertEqual(
            engine.events[-1].data,
            "VERIFY_VENUE:BINANCE:7:system_health:transport",
        )

    @patch("gateway.binance.gateway.BinanceWsApi")
    def test_recovery_cannot_overwrite_concurrent_transport_fault(self, ws_type):
        engine = DummyEngine()
        ws_type.return_value.wait_until_connected.return_value = True
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.event_engine = engine
        gateway.gateway_name = "BINANCE"
        gateway.testnet = True
        gateway.symbols = ["BTCUSDT"]
        gateway.orderbooks = {}
        gateway.ws_buffer = {}
        gateway.book_resyncing = set()
        gateway.book_recovery_generation = {}
        gateway.book_recovery_tokens = {}
        gateway._book_recovery_token = 0
        gateway._book_generation = 0
        gateway._book_lock = threading.RLock()
        gateway.active = False
        gateway.listen_key = ""
        gateway.target_leverage = 0
        gateway.target_margin_type = "ISOLATED"
        gateway.target_position_mode = "ONE_WAY"
        gateway.recovery_lock = threading.Lock()
        gateway.keep_alive_generation = 0
        gateway.stream_ready_timeout_sec = 1.0
        gateway.state = GatewayState.ERROR
        gateway.ws = SimpleNamespace(close=lambda: None)
        gateway._start_streams = lambda **_kwargs: True

        def fault_during_resync(_symbol, **_kwargs):
            gateway.active = False
            gateway.set_state(GatewayState.ERROR)
            return True

        gateway._resync_book = fault_during_resync

        self.assertFalse(
            gateway.recover_connectivity(
                recovery_context={
                    "owner": "system_health:transport",
                    "epoch": 7,
                }
            )
        )
        self.assertEqual(gateway.state, GatewayState.ERROR)
        self.assertFalse(
            any(
                str(event.data).startswith("VERIFY_VENUE:")
                for event in engine.events
            )
        )

    @patch("gateway.binance.gateway.BinanceWsApi")
    def test_close_cannot_be_overwritten_by_inflight_recovery(self, ws_type):
        engine = DummyEngine()
        entered_resync = threading.Event()
        release_resync = threading.Event()
        ws_type.return_value.wait_until_connected.return_value = True
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.event_engine = engine
        gateway.gateway_name = "BINANCE"
        gateway.testnet = True
        gateway.symbols = ["BTCUSDT"]
        gateway.orderbooks = {}
        gateway.ws_buffer = {}
        gateway.book_resyncing = set()
        gateway.book_recovery_generation = {}
        gateway.book_recovery_tokens = {}
        gateway._book_recovery_token = 0
        gateway._book_generation = 0
        gateway._book_lock = threading.RLock()
        gateway.active = False
        gateway.listen_key = ""
        gateway.target_leverage = 0
        gateway.target_margin_type = "ISOLATED"
        gateway.target_position_mode = "ONE_WAY"
        gateway.recovery_lock = threading.Lock()
        gateway.keep_alive_generation = 0
        gateway.stream_ready_timeout_sec = 1.0
        gateway.state = GatewayState.ERROR
        gateway.ws = SimpleNamespace(close=lambda: None)
        gateway.session = SimpleNamespace(close=lambda: None)
        gateway._closing = False
        gateway._start_streams = lambda **_kwargs: True

        def blocked_resync(_symbol, **_kwargs):
            entered_resync.set()
            release_resync.wait(timeout=1.0)
            return False

        gateway._resync_book = blocked_resync
        recovery_results = []
        recovery_thread = threading.Thread(
            target=lambda: recovery_results.append(
                gateway.recover_connectivity()
            )
        )
        close_thread = threading.Thread(target=gateway.close)
        try:
            recovery_thread.start()
            self.assertTrue(entered_resync.wait(timeout=1.0))
            close_thread.start()
            deadline = time.perf_counter() + 1.0
            while (
                not gateway._closing
                and time.perf_counter() < deadline
            ):
                time.sleep(0.005)
            self.assertTrue(gateway._closing)

            release_resync.set()
            recovery_thread.join(timeout=1.0)
            close_thread.join(timeout=1.0)

            self.assertFalse(recovery_thread.is_alive())
            self.assertFalse(close_thread.is_alive())
            self.assertEqual(recovery_results, [False])
            self.assertEqual(gateway.state, GatewayState.DISCONNECTED)
            self.assertFalse(gateway.active)
            self.assertFalse(
                any(
                    str(event.data).startswith("VERIFY_VENUE:")
                    for event in engine.events
                )
            )
        finally:
            release_resync.set()
            recovery_thread.join(timeout=1.0)
            close_thread.join(timeout=1.0)

    def test_risk_increasing_send_rejected_while_book_resyncing(self):
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.gateway_name = "BINANCE"
        gateway.active = True
        gateway.state = GatewayState.READY
        gateway._book_lock = threading.RLock()
        gateway.book_resyncing = {"BTCUSDT"}
        gateway.ws_buffer = {"BTCUSDT": []}
        gateway.require_healthy_clock = False
        rest_calls = []
        gateway.rest = SimpleNamespace(
            new_order=lambda *_args, **_kwargs: rest_calls.append(True)
        )

        result = gateway.send_order(
            OrderRequest(
                symbol="BTCUSDT",
                price=100.0,
                volume=1.0,
                side="BUY",
            ),
            "book-gap",
        )

        self.assertEqual(result.outcome, CommandOutcome.REJECTED)
        self.assertEqual(result.error_code, "ORDERBOOK_NOT_READY")
        self.assertEqual(rest_calls, [])

    def test_superseded_book_launch_cannot_emit_stale_freeze(self):
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.event_engine = DummyEngine()
        gateway._book_lock = threading.RLock()
        gateway._book_generation = 3
        gateway.book_resyncing = {"BTCUSDT"}
        gateway.book_recovery_generation = {"BTCUSDT": 3}
        gateway.book_recovery_tokens = {"BTCUSDT": 2}

        self.assertFalse(
            gateway._launch_book_recovery(
                ("BTCUSDT", 3, 1, "FATAL_GAP")
            )
        )
        self.assertEqual(gateway.event_engine.events, [])

    def test_connect_fails_closed_when_user_stream_cannot_start(self):
        engine = DummyEngine()
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.event_engine = engine
        gateway.gateway_name = "BINANCE"
        gateway.testnet = True
        gateway.symbols = []
        gateway.orderbooks = {}
        gateway.ws_buffer = {}
        gateway.book_resyncing = set()
        gateway.book_recovery_generation = {}
        gateway.book_recovery_tokens = {}
        gateway._book_recovery_token = 0
        gateway._book_generation = 0
        gateway._book_lock = threading.RLock()
        gateway.active = False
        gateway.listen_key = ""
        gateway.target_leverage = 0
        gateway.target_margin_type = "ISOLATED"
        gateway.target_position_mode = "ONE_WAY"
        gateway.recovery_lock = threading.Lock()
        gateway.keep_alive_generation = 0
        gateway.state = GatewayState.DISCONNECTED
        gateway.rest = SimpleNamespace(
            response_succeeded=lambda *_args, **_kwargs: True,
            set_position_mode=lambda *_args, **_kwargs: None,
            set_margin_type=lambda *_args, **_kwargs: None,
            set_leverage=lambda *_args, **_kwargs: None,
        )
        gateway.ws = SimpleNamespace(close=lambda: None)
        gateway._start_streams = lambda **_kwargs: False

        gateway.connect(["BTCUSDT"])

        self.assertEqual(gateway.state, GatewayState.ERROR)
        self.assertEqual(engine.events[-1].type, EVENT_SYSTEM_HEALTH)
        self.assertEqual(engine.events[-1].data, "FREEZE_VENUE:BINANCE:USER_STREAM_START_FAILED")

    def test_connect_fails_closed_when_account_configuration_cannot_be_applied(self):
        engine = DummyEngine()
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.event_engine = engine
        gateway.gateway_name = "BINANCE"
        gateway.testnet = True
        gateway.symbols = []
        gateway.orderbooks = {}
        gateway.ws_buffer = {}
        gateway.book_resyncing = set()
        gateway.book_recovery_generation = {}
        gateway.book_recovery_tokens = {}
        gateway._book_recovery_token = 0
        gateway._book_generation = 0
        gateway._book_lock = threading.RLock()
        gateway.active = False
        gateway.listen_key = ""
        gateway.target_leverage = 8
        gateway.target_margin_type = "ISOLATED"
        gateway.target_position_mode = "ONE_WAY"
        gateway.recovery_lock = threading.Lock()
        gateway.keep_alive_generation = 0
        gateway.state = GatewayState.DISCONNECTED

        class FailedResponse:
            status_code = 400

            def json(self):
                return {"code": -9999, "msg": "config failed"}

        gateway.rest = SimpleNamespace(
            response_succeeded=lambda response, accepted_error_codes=None: False,
            set_position_mode=lambda *_args, **_kwargs: FailedResponse(),
            set_margin_type=lambda *_args, **_kwargs: FailedResponse(),
            set_leverage=lambda *_args, **_kwargs: FailedResponse(),
        )
        gateway.ws = SimpleNamespace(close=lambda: None)
        gateway._start_streams = lambda **_kwargs: True

        gateway.connect(["BTCUSDT"])

        self.assertEqual(gateway.state, GatewayState.ERROR)
        self.assertEqual(engine.events[-1].type, EVENT_SYSTEM_HEALTH)
        self.assertEqual(engine.events[-1].data, "FREEZE_VENUE:BINANCE:ACCOUNT_CONFIG_FAILED")


if __name__ == "__main__":
    unittest.main()
