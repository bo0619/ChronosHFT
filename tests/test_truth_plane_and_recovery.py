import sys
import time
import types
import unittest
from datetime import datetime
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
from event.type import MarkPriceData
from infrastructure.venue_supervisor import VenueSupervisor


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

    def test_supervisor_keeps_retrying_with_backoff_after_fast_attempts(self):
        config = self.make_config()
        config["oms"]["venue_supervisor"].update(
            {"recovery_delay_sec": 1.0, "max_backoff_sec": 4.0}
        )
        supervisor = VenueSupervisor(
            DummyOms("system_health:WS_TRANSPORT_DROP:MarketWS:lost"),
            DummyGateway(recover_result=False),
            config,
            start_thread=False,
        )
        now = [100.0]
        with patch(
            "infrastructure.venue_supervisor.time.perf_counter",
            side_effect=lambda: now[0],
        ):
            attempt_times = []
            for _ in range(40):
                calls = supervisor.gateway.calls
                supervisor.poll_once()
                if supervisor.gateway.calls != calls:
                    attempt_times.append(now[0])
                now[0] += 0.5

        gaps = [b - a for a, b in zip(attempt_times, attempt_times[1:])]
        # Two fast attempts 1s apart, then 2s, then capped at 4s, and never
        # a permanent give-up.
        self.assertEqual(gaps[:4], [1.0, 2.0, 4.0, 4.0])
        self.assertGreater(len(attempt_times), 6)

    def test_supervisor_recovers_paper_public_stream_recovery_timeout(self):
        supervisor = VenueSupervisor(
            DummyOms("system_health:PUBLIC_STREAM_RECOVERY_TIMEOUT"),
            DummyGateway(recover_result=True),
            self.make_config(),
            start_thread=False,
        )

        self.assertTrue(supervisor.poll_once())

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


if __name__ == "__main__":
    unittest.main()
