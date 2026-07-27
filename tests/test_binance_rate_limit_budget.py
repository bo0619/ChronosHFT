import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from gateway.binance.constants import EP_ACCOUNT
from gateway.binance.constants import EP_ALL_ORDERS
from gateway.binance.constants import EP_ORDER
from gateway.binance.constants import EP_USER_TRADES
from gateway.binance.rate_limit_budget import BinanceRateLimitBudget
from gateway.binance.rate_limit_budget import RateLimitDecision
from gateway.binance.rest_api import BinanceRestApi


class DummyRequest:
    def __init__(self, method, url, params=None, headers=None):
        self.method = method
        self.url = url
        self.params = params or {}
        self.headers = headers or {}


class DummyResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class SequenceSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.send_count = 0

    def prepare_request(self, request):
        return request

    def send(self, _request, timeout=None):
        self.send_count += 1
        return self.responses.pop(0)


class RecordingBudget:
    def __init__(self):
        self.calls = []

    def acquire(self, weight, **kwargs):
        self.calls.append((weight, kwargs))
        return RateLimitDecision(True)

    def record_response(self, *_args, **_kwargs):
        return None

    def snapshot(self):
        return {"enabled": True}


class BinanceRateLimitBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_path = (
            Path(self.temporary_directory.name) / "rate-limit.sqlite3"
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def make_budget(self):
        return BinanceRateLimitBudget(
            self.state_path,
            request_weight_limit=100,
            trading_reserve=20,
            emergency_reserve=10,
        )

    def test_three_tiers_preserve_trading_and_emergency_capacity(self):
        budget = self.make_budget()
        now = 120.25

        self.assertTrue(
            budget.acquire(70, priority="background", now_epoch=now).allowed
        )
        background = budget.acquire(
            1,
            priority="background",
            now_epoch=now,
        )
        self.assertFalse(background.allowed)
        self.assertEqual(
            background.reason,
            "trading_request_weight_reserve_protected",
        )
        self.assertTrue(
            budget.acquire(20, priority="trading", now_epoch=now).allowed
        )
        trading = budget.acquire(
            1,
            priority="trading",
            now_epoch=now,
        )
        self.assertFalse(trading.allowed)
        self.assertEqual(
            trading.reason,
            "emergency_request_weight_reserve_protected",
        )
        self.assertTrue(
            budget.acquire(10, priority="emergency", now_epoch=now).allowed
        )

    def test_separate_instances_share_the_same_host_budget(self):
        first = self.make_budget()
        second = self.make_budget()
        now = 180.5

        self.assertTrue(
            first.acquire(60, priority="background", now_epoch=now).allowed
        )
        decision = second.acquire(
            11,
            priority="background",
            now_epoch=now,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(
            second.snapshot(now)["effective_used_weight"],
            60,
        )

    def test_exchange_headers_and_retry_after_are_shared(self):
        first = self.make_budget()
        second = self.make_budget()
        now = 240.0

        first.record_response(
            {
                "X-MBX-USED-WEIGHT-1M": "75",
                "Retry-After": "2.5",
            },
            status_code=429,
            now_epoch=now,
        )

        blocked = second.acquire(
            1,
            priority="emergency",
            now_epoch=now + 1.0,
        )
        self.assertFalse(blocked.allowed)
        self.assertEqual(
            blocked.reason,
            "exchange_retry_after_status_429",
        )
        self.assertAlmostEqual(blocked.retry_after_sec, 1.5)

        after_retry = second.acquire(
            5,
            priority="trading",
            now_epoch=now + 3.0,
        )
        self.assertTrue(after_retry.allowed)
        background = second.acquire(
            1,
            priority="background",
            now_epoch=now + 3.0,
        )
        self.assertFalse(background.allowed)

    @patch("gateway.binance.rest_api.requests.Request", DummyRequest)
    def test_rest_client_records_exchange_weight_before_next_request(self):
        budget = self.make_budget()
        session = SequenceSession(
            [
                DummyResponse(
                    headers={"X-MBX-USED-WEIGHT-1M": "70"},
                )
            ]
        )
        api = BinanceRestApi(
            "key",
            "secret",
            session,
            testnet=True,
            rate_limit_budget=budget,
        )
        api.min_signed_interval_sec = 0.0
        api.endpoint_intervals[EP_ACCOUNT] = 0.0

        first = api.get_account()
        second = api.get_account()

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(
            second.json()["code"],
            "RATE_LIMIT_BUDGET_EXHAUSTED",
        )
        self.assertEqual(session.send_count, 1)

    @patch("gateway.binance.rest_api.requests.Request", DummyRequest)
    def test_rest_client_reserves_emergency_capacity_for_risk_truth(self):
        budget = RecordingBudget()
        session = SequenceSession(
            [
                DummyResponse(payload=[]),
                DummyResponse(payload={"serverTime": 1}),
            ]
        )
        api = BinanceRestApi(
            "key",
            "secret",
            session,
            testnet=True,
            rate_limit_budget=budget,
        )
        api.min_signed_interval_sec = 0.0
        api.min_public_interval_sec = 0.0
        api.endpoint_intervals = {}

        api.get_positions(emergency=True)
        api.get_server_time(emergency=True)

        self.assertEqual(
            [call[1]["priority"] for call in budget.calls],
            ["emergency", "emergency"],
        )
        self.assertEqual([call[0] for call in budget.calls], [5, 1])

    @patch("gateway.binance.rest_api.requests.Request", DummyRequest)
    def test_order_truth_reads_use_protected_trading_capacity(self):
        budget = RecordingBudget()
        session = SequenceSession(
            [
                DummyResponse(payload={}),
                DummyResponse(payload=[]),
                DummyResponse(payload=[]),
            ]
        )
        api = BinanceRestApi(
            "key",
            "secret",
            session,
            testnet=True,
            rate_limit_budget=budget,
        )
        api.min_signed_interval_sec = 0.0
        api.endpoint_intervals = {
            EP_ORDER: 0.0,
            EP_ALL_ORDERS: 0.0,
            EP_USER_TRADES: 0.0,
        }

        api.query_order("BTCUSDT", "1")
        api.get_all_orders("BTCUSDT")
        api.get_user_trades("BTCUSDT")

        self.assertEqual(
            [call[1]["priority"] for call in budget.calls],
            ["trading", "trading", "trading"],
        )

    def test_emergency_request_bypasses_normal_local_endpoint_cooldown(self):
        api = BinanceRestApi(
            "key",
            "secret",
            SequenceSession([]),
            testnet=True,
        )
        api.min_signed_interval_sec = 0.0
        api.endpoint_intervals[EP_ORDER] = 0.0
        api.endpoint_cooldown_until[EP_ORDER] = (
            time.perf_counter() + 10.0
        )

        with patch("gateway.binance.rest_api.time.sleep") as sleep:
            api._throttle(
                EP_ORDER,
                True,
                {"reduceOnly": "true"},
                priority="emergency",
            )

        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
