import math
import unittest

from risk.funding_guard import (
    ALLOW,
    REDUCE_ONLY,
    FundingGuardPolicy,
    FundingGuardState,
    FundingObservation,
    evaluate_funding_guard,
    parse_binance_premium_index_payload,
)


class FundingGuardTests(unittest.TestCase):
    def setUp(self):
        self.policy = FundingGuardPolicy(
            max_snapshot_age_ms=3_000.0,
            pre_funding_reduce_only_sec=600.0,
            post_funding_hold_sec=120.0,
            max_abs_funding_rate=0.0005,
            max_next_funding_horizon_sec=32_400.0,
            recovery_updates=3,
        )
        self.exchange_epoch = 1_800_000_000.0
        self.monotonic = 10_000.0

    def observation(
        self,
        observation_id,
        *,
        funding_rate=0.0001,
        seconds_to_funding=3_600.0,
        received_monotonic=None,
        corrected_received_epoch=None,
        next_funding_epoch=None,
        clock_healthy=True,
    ):
        return FundingObservation(
            observation_id=observation_id,
            funding_rate=funding_rate,
            next_funding_epoch=(
                self.exchange_epoch + seconds_to_funding
                if next_funding_epoch is None
                else next_funding_epoch
            ),
            corrected_received_epoch=(
                self.exchange_epoch
                if corrected_received_epoch is None
                else corrected_received_epoch
            ),
            received_monotonic=(
                self.monotonic
                if received_monotonic is None
                else received_monotonic
            ),
            clock_healthy=clock_healthy,
        )

    def evaluate(self, observation, state=None, *, now=None):
        return evaluate_funding_guard(
            self.policy,
            observation,
            state,
            now_monotonic=self.monotonic if now is None else now,
        )

    def recover(self, state=None, *, first_id=1):
        decision = None
        for offset in range(self.policy.recovery_updates):
            now = self.monotonic + offset
            observation = self.observation(
                f"healthy-{first_id + offset}",
                received_monotonic=now,
                corrected_received_epoch=self.exchange_epoch + offset,
            )
            decision, state = self.evaluate(
                observation,
                state,
                now=now,
            )
        return decision, state

    def test_premium_index_parser_binds_symbol_and_exchange_time(self):
        observation = parse_binance_premium_index_payload(
            {
                "symbol": "XAUUSDT",
                "lastFundingRate": "0.0001",
                "nextFundingTime": 1_800_003_600_000,
                "time": 1_800_000_000_000,
            },
            expected_symbol="xauusdt",
            corrected_received_epoch=self.exchange_epoch + 0.250,
            received_monotonic=self.monotonic,
            max_source_age_ms=3_000.0,
        )

        self.assertEqual(
            observation.observation_id,
            "XAUUSDT:1800000000000:1800003600000:"
            "0.0001",
        )
        self.assertEqual(observation.funding_rate, 0.0001)
        self.assertEqual(
            observation.next_funding_epoch,
            self.exchange_epoch + 3_600.0,
        )
        self.assertEqual(
            observation.corrected_received_epoch,
            self.exchange_epoch + 0.250,
        )
        self.assertEqual(
            observation.received_monotonic,
            self.monotonic,
        )

    def test_premium_index_parser_rejects_untrusted_source_data(self):
        baseline = {
            "symbol": "XAUUSDT",
            "lastFundingRate": "0.0001",
            "nextFundingTime": 1_800_003_600_000,
            "time": 1_800_000_000_000,
        }
        cases = (
            (
                {**baseline, "symbol": "BTCUSDT"},
                self.exchange_epoch,
                "symbol mismatch",
            ),
            (
                {**baseline, "lastFundingRate": "nan"},
                self.exchange_epoch,
                "lastFundingRate must be finite",
            ),
            (
                {**baseline, "nextFundingTime": True},
                self.exchange_epoch,
                "nextFundingTime must be finite",
            ),
            (
                baseline,
                self.exchange_epoch + 3.001,
                "source time is stale",
            ),
            (
                {**baseline, "time": 1_800_000_004_000},
                self.exchange_epoch,
                "source time is from the future",
            ),
        )
        for payload, corrected_epoch, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    parse_binance_premium_index_payload(
                        payload,
                        expected_symbol="XAUUSDT",
                        corrected_received_epoch=corrected_epoch,
                        received_monotonic=self.monotonic,
                        max_source_age_ms=3_000.0,
                    )

    def test_starts_fail_closed_and_requires_distinct_healthy_updates(self):
        decision, state = self.evaluate(None)

        self.assertEqual(decision.action, REDUCE_ONLY)
        self.assertEqual(
            decision.reason,
            "funding_guard:snapshot_unavailable",
        )
        self.assertTrue(decision.blocks_open_risk)
        self.assertTrue(decision.allows_reduce_only)

        decision, state = self.recover(state)
        self.assertEqual(decision.action, ALLOW)
        self.assertTrue(decision.healthy)
        self.assertFalse(state.blocked)

    def test_duplicate_observation_does_not_advance_recovery(self):
        first = self.observation("same")
        decision, state = self.evaluate(first)
        self.assertEqual(decision.consecutive_healthy_updates, 1)

        decision, state = self.evaluate(first, state, now=self.monotonic + 1.0)
        self.assertEqual(decision.action, REDUCE_ONLY)
        self.assertFalse(decision.observation_advanced)
        self.assertEqual(decision.consecutive_healthy_updates, 1)

        second = self.observation(
            "second",
            received_monotonic=self.monotonic + 2.0,
            corrected_received_epoch=self.exchange_epoch + 2.0,
        )
        decision, state = self.evaluate(
            second,
            state,
            now=self.monotonic + 2.0,
        )
        self.assertEqual(decision.consecutive_healthy_updates, 2)

    def test_monotonic_age_advances_corrected_exchange_anchor(self):
        observation = self.observation(
            "anchor",
            seconds_to_funding=602.5,
        )
        decision, _ = self.evaluate(
            observation,
            FundingGuardState(
                blocked=False,
                reason="",
                last_next_funding_epoch=observation.next_funding_epoch,
            ),
            now=self.monotonic + 2.5,
        )

        self.assertAlmostEqual(
            decision.exchange_now_epoch,
            self.exchange_epoch + 2.5,
        )
        self.assertAlmostEqual(decision.seconds_to_funding, 600.0)
        self.assertEqual(decision.action, REDUCE_ONLY)
        self.assertEqual(
            decision.reason,
            "funding_guard:pre_funding_window",
        )

    def test_snapshot_age_boundary_is_inclusive(self):
        state = FundingGuardState(
            blocked=False,
            reason="",
            last_next_funding_epoch=self.exchange_epoch + 3_600.0,
        )
        observation = self.observation("age-boundary")
        decision, state = self.evaluate(
            observation,
            state,
            now=self.monotonic + 3.0,
        )
        self.assertEqual(decision.action, ALLOW)

        decision, _ = self.evaluate(
            observation,
            state,
            now=self.monotonic + 3.001,
        )
        self.assertEqual(decision.action, REDUCE_ONLY)
        self.assertEqual(decision.reason, "funding_guard:snapshot_stale")

    def test_pre_funding_boundary_and_horizon_fail_closed(self):
        state = FundingGuardState(blocked=False, reason="")
        at_boundary = self.observation(
            "at-window",
            seconds_to_funding=600.0,
        )
        decision, _ = self.evaluate(at_boundary, state)
        self.assertEqual(decision.action, REDUCE_ONLY)

        outside_window = self.observation(
            "outside-window",
            seconds_to_funding=600.001,
        )
        decision, _ = self.evaluate(outside_window, state)
        self.assertEqual(decision.action, ALLOW)

        too_far = self.observation(
            "too-far",
            seconds_to_funding=32_400.001,
        )
        decision, _ = self.evaluate(too_far, state)
        self.assertEqual(
            decision.reason,
            "funding_guard:next_funding_horizon_exceeded",
        )

    def test_positive_and_negative_rate_boundaries_block(self):
        state = FundingGuardState(blocked=False, reason="")
        for index, rate in enumerate((0.0005, -0.0005)):
            with self.subTest(rate=rate):
                observation = self.observation(
                    f"rate-{index}",
                    funding_rate=rate,
                )
                decision, _ = self.evaluate(observation, state)
                self.assertEqual(decision.action, REDUCE_ONLY)
                self.assertEqual(
                    decision.reason,
                    "funding_guard:funding_rate_limit",
                )

        observation = self.observation(
            "rate-inside",
            funding_rate=-0.000499999,
        )
        decision, _ = self.evaluate(observation, state)
        self.assertEqual(decision.action, ALLOW)

    def test_invalid_values_clock_and_elapsed_schedule_fail_closed(self):
        state = FundingGuardState(blocked=False, reason="")
        cases = (
            (
                self.observation("nan-rate", funding_rate=math.nan),
                "funding_guard:funding_rate_invalid",
            ),
            (
                self.observation(
                    "inf-next",
                    next_funding_epoch=math.inf,
                ),
                "funding_guard:next_funding_time_invalid",
            ),
            (
                self.observation(
                    "bad-anchor",
                    corrected_received_epoch=math.nan,
                ),
                "funding_guard:exchange_time_anchor_invalid",
            ),
            (
                self.observation("bad-clock", clock_healthy=False),
                "funding_guard:exchange_clock_unhealthy",
            ),
            (
                self.observation(
                    "elapsed",
                    seconds_to_funding=-0.001,
                ),
                "funding_guard:next_funding_time_elapsed",
            ),
        )
        for observation, reason in cases:
            with self.subTest(reason=reason):
                decision, _ = self.evaluate(observation, state)
                self.assertEqual(decision.action, REDUCE_ONLY)
                self.assertEqual(decision.reason, reason)

    def test_rollover_near_old_funding_starts_post_hold(self):
        old_next = self.exchange_epoch + 1.0
        state = FundingGuardState(
            blocked=True,
            reason="funding_guard:pre_funding_window",
            last_observation_id="old",
            last_next_funding_epoch=old_next,
        )
        rolled = self.observation(
            "rolled",
            corrected_received_epoch=self.exchange_epoch + 2.0,
            received_monotonic=self.monotonic + 2.0,
            next_funding_epoch=self.exchange_epoch + 28_800.0,
        )
        decision, state = self.evaluate(
            rolled,
            state,
            now=self.monotonic + 2.0,
        )

        self.assertEqual(decision.action, REDUCE_ONLY)
        self.assertEqual(
            decision.reason,
            "funding_guard:post_funding_hold",
        )
        self.assertAlmostEqual(decision.post_hold_remaining_sec, 120.0)

        during_hold = self.observation(
            "during-hold",
            corrected_received_epoch=self.exchange_epoch + 121.0,
            received_monotonic=self.monotonic + 121.0,
            next_funding_epoch=self.exchange_epoch + 28_800.0,
        )
        decision, state = self.evaluate(
            during_hold,
            state,
            now=self.monotonic + 121.0,
        )
        self.assertEqual(decision.action, REDUCE_ONLY)
        self.assertEqual(
            decision.reason,
            "funding_guard:post_funding_hold",
        )

        after_hold = self.observation(
            "after-hold",
            corrected_received_epoch=self.exchange_epoch + 122.0,
            received_monotonic=self.monotonic + 122.0,
            next_funding_epoch=self.exchange_epoch + 28_800.0,
        )
        decision, state = self.evaluate(
            after_hold,
            state,
            now=self.monotonic + 122.0,
        )
        self.assertEqual(decision.action, REDUCE_ONLY)
        self.assertEqual(decision.consecutive_healthy_updates, 1)

    def test_unknown_process_start_can_enforce_full_post_funding_hold(self):
        state = FundingGuardState(
            reason="funding_guard:startup_hold",
            post_hold_until_monotonic=(
                self.monotonic + self.policy.post_funding_hold_sec
            ),
        )
        first = self.observation("startup-1")
        decision, state = self.evaluate(first, state)

        self.assertEqual(decision.action, REDUCE_ONLY)
        self.assertEqual(
            decision.reason,
            "funding_guard:post_funding_hold",
        )
        self.assertAlmostEqual(
            decision.post_hold_remaining_sec,
            self.policy.post_funding_hold_sec,
        )

        after_hold = self.observation(
            "startup-2",
            received_monotonic=(
                self.monotonic + self.policy.post_funding_hold_sec
            ),
            corrected_received_epoch=(
                self.exchange_epoch + self.policy.post_funding_hold_sec
            ),
        )
        decision, _ = self.evaluate(
            after_hold,
            state,
            now=self.monotonic + self.policy.post_funding_hold_sec,
        )
        self.assertEqual(decision.action, REDUCE_ONLY)
        self.assertEqual(decision.consecutive_healthy_updates, 1)

    def test_far_schedule_adjustment_does_not_start_post_hold(self):
        old_next = self.exchange_epoch + 7_200.0
        state = FundingGuardState(
            blocked=False,
            reason="",
            last_observation_id="old",
            last_next_funding_epoch=old_next,
        )
        adjusted = self.observation(
            "adjusted",
            next_funding_epoch=self.exchange_epoch + 10_800.0,
        )

        decision, state = self.evaluate(adjusted, state)

        self.assertEqual(decision.action, ALLOW)
        self.assertEqual(state.post_hold_until_monotonic, 0.0)

    def test_schedule_regression_and_new_breach_reset_recovery(self):
        state = FundingGuardState(
            blocked=True,
            reason="funding_guard:snapshot_stale",
            last_next_funding_epoch=self.exchange_epoch + 3_600.0,
        )
        healthy = self.observation("healthy-1")
        decision, state = self.evaluate(healthy, state)
        self.assertEqual(decision.consecutive_healthy_updates, 1)

        breach = self.observation(
            "rate-breach",
            funding_rate=0.001,
        )
        decision, state = self.evaluate(breach, state)
        self.assertEqual(decision.consecutive_healthy_updates, 0)

        regressed = self.observation(
            "regressed",
            next_funding_epoch=self.exchange_epoch + 3_000.0,
        )
        decision, _ = self.evaluate(regressed, state)
        self.assertEqual(
            decision.reason,
            "funding_guard:next_funding_time_regressed",
        )

    def test_policy_validation_rejects_unsafe_or_ambiguous_values(self):
        invalid = (
            {"enabled": "true"},
            {"max_snapshot_age_ms": math.inf},
            {"max_abs_funding_rate": 0.0},
            {
                "pre_funding_reduce_only_sec": 600.0,
                "max_next_funding_horizon_sec": 600.0,
            },
            {"recovery_updates": True},
        )
        for values in invalid:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    FundingGuardPolicy(**values)

    def test_disabled_policy_allows_without_observation(self):
        policy = FundingGuardPolicy(enabled=False)
        decision, state = evaluate_funding_guard(
            policy,
            None,
            now_monotonic=self.monotonic,
        )

        self.assertEqual(decision.action, ALLOW)
        self.assertFalse(state.blocked)

    def test_snapshot_requirement_can_be_explicitly_disabled(self):
        policy = FundingGuardPolicy(require_snapshot=False)
        decision, state = evaluate_funding_guard(
            policy,
            None,
            now_monotonic=self.monotonic,
        )

        self.assertEqual(decision.action, ALLOW)
        self.assertFalse(state.blocked)


if __name__ == "__main__":
    unittest.main()
