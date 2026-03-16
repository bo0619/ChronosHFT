import sys
import types
import unittest
from unittest.mock import patch

if "requests" not in sys.modules:
    requests_module = types.ModuleType("requests")

    class Request:
        def __init__(self, method, url, params=None, headers=None):
            self.method = method
            self.url = url
            self.params = params or {}
            self.headers = headers or {}

    requests_module.Request = Request
    sys.modules["requests"] = requests_module

from gateway.binance.constants import EP_POSITION_RISK
from gateway.binance.rest_api import BinanceRestApi


class DummySession:
    def prepare_request(self, req):
        return req

    def send(self, _prepped, timeout=None):
        raise RuntimeError("proxy_down")


class DummyResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        return self.payload


class SequenceSession:
    def __init__(self, responses):
        self.responses = list(responses)

    def prepare_request(self, req):
        return req

    def send(self, _prepped, timeout=None):
        return self.responses.pop(0)


class DummyRequest:
    def __init__(self, method, url, params=None, headers=None):
        self.method = method
        self.url = url
        self.params = params or {}
        self.headers = headers or {}


class RestApiThrottleTests(unittest.TestCase):
    def test_failed_endpoint_enters_cooldown(self):
        api = BinanceRestApi("key", "secret", DummySession(), testnet=True)
        api.max_retries = 1
        api.retry_backoff_sec = 0.01
        api.request("GET", EP_POSITION_RISK, signed=True)
        self.assertGreater(api.endpoint_cooldown_until.get(EP_POSITION_RISK, 0.0), 0.0)

    @patch("gateway.binance.rest_api.requests.Request", DummyRequest)
    @patch("gateway.binance.rest_api.time_service._sync", return_value=True)
    def test_timestamp_error_resyncs_and_retries(self, sync_mock):
        session = SequenceSession(
            [
                DummyResponse(400, {"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."}),
                DummyResponse(200, {"ok": True}),
            ]
        )
        api = BinanceRestApi("key", "secret", session, testnet=True)
        api.max_retries = 2

        response = api.request("GET", EP_POSITION_RISK, signed=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(sync_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
