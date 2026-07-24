import math
import unittest

from risk.deployment_loss import (
    deployed_capital_equity_ratio,
    deployed_capital_within_equity_limit,
    deployment_policy_fingerprint,
    deployment_loss_action,
    update_deployment_loss,
)


class DeploymentLossTests(unittest.TestCase):
    def test_policy_fingerprint_is_stable_and_sensitive_to_risk_envelope(self):
        inputs = {
            "deployment_id": "canary-001",
            "symbols": ["btcusdt"],
            "declared_account_equity": 10_000.0,
            "max_deployed_capital": 100.0,
            "maximum_loss": 5.0,
            "reduce_only_fraction": 0.8,
        }
        first = deployment_policy_fingerprint(**inputs)
        second = deployment_policy_fingerprint(
            **{**inputs, "symbols": ["BTCUSDT"]}
        )
        changed = deployment_policy_fingerprint(
            **{**inputs, "maximum_loss": 6.0}
        )

        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, changed)

    def test_initializes_once_and_tracks_cash_flow_adjusted_loss(self):
        state = update_deployment_loss(
            equity=10_000.0,
            external_cash_flow_total=0.0,
            start_equity=0.0,
            start_external_cash_flow_total=0.0,
        )
        self.assertEqual(state, (10_000.0, 0.0, 10_000.0, 0.0))

        state = update_deployment_loss(
            equity=10_096.0,
            external_cash_flow_total=100.0,
            start_equity=state[0],
            start_external_cash_flow_total=state[1],
        )
        self.assertEqual(state, (10_000.0, 0.0, 9_996.0, 4.0))

        state = update_deployment_loss(
            equity=9_946.0,
            external_cash_flow_total=50.0,
            start_equity=state[0],
            start_external_cash_flow_total=state[1],
        )
        self.assertEqual(state, (10_000.0, 0.0, 9_896.0, 104.0))

    def test_deployed_capital_is_bound_to_current_account_equity(self):
        self.assertEqual(
            deployed_capital_equity_ratio(
                equity=10_000.0,
                max_deployed_capital=100.0,
            ),
            0.01,
        )
        self.assertTrue(
            deployed_capital_within_equity_limit(
                equity=5_000.0,
                max_deployed_capital=100.0,
            )
        )
        self.assertFalse(
            deployed_capital_within_equity_limit(
                equity=4_999.0,
                max_deployed_capital=100.0,
            )
        )

    def test_policy_transitions_at_canary_thresholds(self):
        policy = {
            "maximum_loss": 5.0,
            "reduce_only_fraction": 0.8,
        }
        self.assertEqual(
            deployment_loss_action(loss=3.99, **policy),
            "NONE",
        )
        self.assertEqual(
            deployment_loss_action(loss=4.0, **policy),
            "REDUCE_ONLY",
        )
        self.assertEqual(
            deployment_loss_action(loss=5.0, **policy),
            "KILL",
        )

    def test_nonfinite_inputs_fail_closed(self):
        with self.assertRaises(ValueError):
            update_deployment_loss(
                equity=math.nan,
                external_cash_flow_total=0.0,
                start_equity=10_000.0,
                start_external_cash_flow_total=0.0,
            )
        with self.assertRaises(ValueError):
            deployment_loss_action(
                loss=0.0,
                maximum_loss=math.inf,
                reduce_only_fraction=0.8,
            )
        with self.assertRaises(ValueError):
            deployed_capital_within_equity_limit(
                equity=0.0,
                max_deployed_capital=100.0,
            )


if __name__ == "__main__":
    unittest.main()
