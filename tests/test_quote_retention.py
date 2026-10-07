import unittest
from unittest.mock import patch

from strategy.quote_retention import (
    KEEP,
    REQUOTE,
    RISK_REQUOTE,
    QueueRetentionPolicy,
    QuoteOrderBudget,
)


class QueueRetentionPolicyTests(unittest.TestCase):
    def classify(self, policy=None, **overrides):
        request = {
            "is_bid": True,
            "resting_price": 100.0,
            "resting_volume": 1.0,
            "target_price": 100.0,
            "target_volume": 1.0,
            "tick_size": 0.1,
            "qty_step": 0.001,
            "rest_age_sec": 0.0,
        }
        request.update(overrides)
        return (policy or QueueRetentionPolicy()).classify(**request)

    def test_aggressive_price_drift_is_risk_on_both_sides(self):
        self.assertEqual(self.classify(target_price=99.9), RISK_REQUOTE)
        self.assertEqual(
            self.classify(is_bid=False, target_price=100.1),
            RISK_REQUOTE,
        )

    def test_conservative_drift_waits_for_tolerance_and_min_rest(self):
        self.assertEqual(
            self.classify(target_price=100.2, rest_age_sec=60.0),
            KEEP,
        )
        self.assertEqual(
            self.classify(target_price=100.3, rest_age_sec=4.9),
            KEEP,
        )
        self.assertEqual(
            self.classify(target_price=100.3, rest_age_sec=5.0),
            REQUOTE,
        )
        self.assertEqual(
            self.classify(
                is_bid=False,
                target_price=99.7,
                rest_age_sec=5.0,
            ),
            REQUOTE,
        )

    def test_bps_tolerance_widens_band_for_small_ticks(self):
        policy = QueueRetentionPolicy(conservative_tolerance_bps=1.0)
        # 1 bps of 100 is 0.01, i.e. ten 0.001 ticks.
        self.assertEqual(
            self.classify(
                policy,
                target_price=100.009,
                tick_size=0.001,
                rest_age_sec=60.0,
            ),
            KEEP,
        )
        self.assertEqual(
            self.classify(
                policy,
                target_price=100.011,
                tick_size=0.001,
                rest_age_sec=60.0,
            ),
            REQUOTE,
        )

    def test_size_jitter_keeps_queue_but_oversize_is_risk(self):
        self.assertEqual(self.classify(target_volume=0.95), KEEP)
        self.assertEqual(self.classify(target_volume=1.08), KEEP)
        self.assertEqual(self.classify(target_volume=0.85), RISK_REQUOTE)
        self.assertEqual(
            self.classify(target_volume=1.2, rest_age_sec=1.0),
            KEEP,
        )
        self.assertEqual(
            self.classify(target_volume=1.2, rest_age_sec=5.0),
            REQUOTE,
        )

    def test_zero_tolerance_restores_one_tick_requotes_after_min_rest(self):
        policy = QueueRetentionPolicy(
            conservative_tolerance_ticks=0.0,
            conservative_tolerance_bps=0.0,
            min_rest_sec=0.0,
            size_tolerance_ratio=0.0,
        )
        self.assertEqual(self.classify(policy, target_price=100.1), REQUOTE)
        self.assertEqual(self.classify(policy, target_price=100.05), KEEP)

    def test_from_config_rejects_invalid_values(self):
        with self.assertRaises(ValueError):
            QueueRetentionPolicy.from_config({"min_rest_sec": -1})
        with self.assertRaises(ValueError):
            QueueRetentionPolicy.from_config({"max_new_orders_per_symbol_per_10min": 0})
        with self.assertRaises(ValueError):
            QueueRetentionPolicy.from_config(
                {"max_new_orders_per_symbol_per_10min": 1.5}
            )
        self.assertEqual(
            QueueRetentionPolicy.from_config(None),
            QueueRetentionPolicy(),
        )


class QuoteOrderBudgetTests(unittest.TestCase):
    def test_counts_per_symbol_and_resets_on_utc_ten_minute_boundary(self):
        budget = QuoteOrderBudget(2)
        budget.record("BTCUSDT", 1_200.0)
        budget.record("BTCUSDT", 1_799.9)
        with patch("strategy.quote_retention.logger") as logger:
            self.assertFalse(budget.has_capacity("BTCUSDT", 1_799.9))
            self.assertFalse(budget.has_capacity("BTCUSDT", 1_799.9))
        self.assertEqual(logger.warning.call_count, 1)
        self.assertTrue(budget.has_capacity("ETHUSDT", 1_799.9))

        self.assertTrue(budget.has_capacity("BTCUSDT", 1_800.0))
        self.assertEqual(budget.count("BTCUSDT", 1_800.0), 0)


if __name__ == "__main__":
    unittest.main()
