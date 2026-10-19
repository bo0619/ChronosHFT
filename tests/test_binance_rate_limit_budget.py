import tempfile
import unittest
from pathlib import Path

from infrastructure.binance_rate_limit_budget import BinanceRateLimitBudget


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

if __name__ == "__main__":
    unittest.main()
