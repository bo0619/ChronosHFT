import sys
import threading
import types
import unittest
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

from event.type import EVENT_SYSTEM_HEALTH, GatewayState
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


class GatewayRecoveryTests(unittest.TestCase):
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
        gateway.active = True
        gateway.listen_key = ""
        gateway.target_leverage = 0
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

    @patch("gateway.binance.gateway.time.sleep", return_value=None)
    def test_recover_connectivity_emits_clear_venue(self, _sleep):
        engine = DummyEngine()
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.event_engine = engine
        gateway.gateway_name = "BINANCE"
        gateway.testnet = True
        gateway.symbols = ["BTCUSDT"]
        gateway.orderbooks = {}
        gateway.ws_buffer = {}
        gateway.book_resyncing = set()
        gateway.active = False
        gateway.listen_key = ""
        gateway.target_leverage = 0
        gateway.recovery_lock = threading.Lock()
        gateway.keep_alive_generation = 0
        gateway.state = GatewayState.ERROR
        gateway.ws = SimpleNamespace(close=lambda: None)
        gateway._start_streams = lambda: True
        gateway._resync_book = lambda symbol: True

        recovered = gateway.recover_connectivity()

        self.assertTrue(recovered)
        self.assertEqual(engine.events[-1].type, EVENT_SYSTEM_HEALTH)
        self.assertEqual(engine.events[-1].data, "CLEAR_VENUE:BINANCE:WS_RECOVERED")

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
        gateway.active = False
        gateway.listen_key = ""
        gateway.target_leverage = 0
        gateway.recovery_lock = threading.Lock()
        gateway.keep_alive_generation = 0
        gateway.state = GatewayState.DISCONNECTED
        gateway.rest = SimpleNamespace(set_margin_type=lambda *_args, **_kwargs: None, set_leverage=lambda *_args, **_kwargs: None)
        gateway.ws = SimpleNamespace(close=lambda: None)
        gateway._start_streams = lambda: False

        gateway.connect(["BTCUSDT"])

        self.assertEqual(gateway.state, GatewayState.ERROR)
        self.assertEqual(engine.events[-1].type, EVENT_SYSTEM_HEALTH)
        self.assertEqual(engine.events[-1].data, "FREEZE_VENUE:BINANCE:USER_STREAM_START_FAILED")


if __name__ == "__main__":
    unittest.main()
