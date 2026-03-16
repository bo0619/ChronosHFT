import sys
import types
import unittest
from unittest.mock import patch

if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")
    requests_stub.get = lambda *args, **kwargs: None
    sys.modules["requests"] = requests_stub

from infrastructure.time_service import TimeService


class DummyResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class TimeServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = TimeService()
        self.service.stop()
        self.service.clear_listeners()
        self.service.offset = 0
        self.service.last_sync_time = 0.0
        self.service.last_rtt_ms = 0.0
        self.service.last_error = ""
        self.service.consecutive_failures = 0
        self.service._health_state = "healthy"
        self.service.configure(
            {
                "max_offset_ms": 100.0,
                "halt_offset_ms": 500.0,
                "max_rtt_ms": 5000.0,
                "max_consecutive_failures": 2,
            }
        )
        self.events = []
        self.service.register_listener(
            lambda severity, reason, details: self.events.append((severity, reason, details))
        )

    @patch("infrastructure.time_service.requests.get")
    @patch("infrastructure.time_service.time.time")
    def test_large_offset_emits_freeze_then_recovered(self, mock_time, mock_get):
        mock_time.side_effect = [1000.0, 1000.02, 1000.03, 2000.0, 2000.02, 2000.03]
        mock_get.side_effect = [
            DummyResponse({"serverTime": 1000.20 * 1000}),
            DummyResponse({"serverTime": 2000.03 * 1000}),
        ]

        self.service._sync()
        self.service._sync()

        self.assertEqual(self.events[0][0], "freeze")
        self.assertIn("clock offset", self.events[0][1])
        self.assertEqual(self.events[1][0], "recovered")

    @patch("infrastructure.time_service.requests.get")
    def test_repeated_sync_failures_emit_halt(self, mock_get):
        mock_get.side_effect = RuntimeError("network down")

        self.service._sync()
        self.service._sync()

        self.assertEqual(self.events[-1][0], "halt")
        self.assertIn("failed 2 times", self.events[-1][1])


if __name__ == "__main__":
    unittest.main()
