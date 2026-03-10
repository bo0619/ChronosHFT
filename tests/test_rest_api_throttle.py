import sys
import types
import unittest

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


class RestApiThrottleTests(unittest.TestCase):
    def test_failed_endpoint_enters_cooldown(self):
        api = BinanceRestApi("key", "secret", DummySession(), testnet=True)
        api.max_retries = 1
        api.retry_backoff_sec = 0.01
        api.request("GET", EP_POSITION_RISK, signed=True)
        self.assertGreater(api.endpoint_cooldown_until.get(EP_POSITION_RISK, 0.0), 0.0)


if __name__ == "__main__":
    unittest.main()