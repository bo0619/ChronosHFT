import unittest

from infrastructure.rpi_policy import (
    effective_rpi_route_enabled,
    validate_live_rpi_policy,
)


class LiveRpiPolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "strategy": {
                "use_rpi": True,
                "rpi_fallback_to_gtx": False,
                "rpi_live_policy": {
                    "require_zero_commission": True,
                },
            },
        }
        self.symbols = ["BTCUSDT", "ETHUSDT"]
        self.support = {
            "BTCUSDT": True,
            "ETHUSDT": True,
        }
        self.rates = {
            "BTCUSDT": 0.0,
            "ETHUSDT": 0.0,
        }

    def test_effective_glft_route_honors_model_switch(self):
        config = {
            "strategy": {
                **self.config["strategy"],
                "name": "GLFT_MultiScale",
                "primary_model": "glft",
                "registered_models": ["glft"],
                "execution_policy": "single_primary",
                "use_rpi_for_glft": False,
            },
        }

        self.assertFalse(effective_rpi_route_enabled(config))
        with self.assertRaisesRegex(
            ValueError,
            "effective RPI route must be enabled",
        ):
            validate_live_rpi_policy(
                config,
                ["ETHUSDT"],
                {"ETHUSDT": True},
                {"ETHUSDT": 0},
            )

    def test_accepts_complete_zero_fee_rpi_truth(self):
        result = validate_live_rpi_policy(
            self.config,
            self.symbols,
            self.support,
            self.rates,
        )

        self.assertEqual(result["symbols"], ("BTCUSDT", "ETHUSDT"))
        self.assertEqual(result["rpi_commission_rates"], self.rates)
        self.assertTrue(result["require_zero_commission"])

    def test_accepts_nonzero_account_truth_when_zero_fee_is_not_required(self):
        config = {
            "strategy": {
                **self.config["strategy"],
                "rpi_live_policy": {
                    "require_zero_commission": False,
                },
            },
        }
        rates = {
            **self.rates,
            "ETHUSDT": 0.0001,
        }

        result = validate_live_rpi_policy(
            config,
            self.symbols,
            self.support,
            rates,
        )

        self.assertEqual(result["rpi_commission_rates"], rates)
        self.assertFalse(result["require_zero_commission"])

    def test_rejects_each_fail_open_rpi_policy_case(self):
        cases = {
            "rpi_disabled": (
                {
                    "strategy": {
                        **self.config["strategy"],
                        "use_rpi": False,
                    },
                },
                self.symbols,
                self.support,
                self.rates,
                "strategy.use_rpi",
            ),
            "gtx_fallback": (
                {
                    "strategy": {
                        **self.config["strategy"],
                        "rpi_fallback_to_gtx": True,
                    },
                },
                self.symbols,
                self.support,
                self.rates,
                "rpi_fallback_to_gtx",
            ),
            "unsupported_symbol": (
                self.config,
                self.symbols,
                {**self.support, "ETHUSDT": False},
                self.rates,
                "ETHUSDT must be TRADING with RPI permission",
            ),
            "missing_symbol_rate": (
                self.config,
                self.symbols,
                self.support,
                {"BTCUSDT": 0.0},
                "rpiCommissionRate is missing for ETHUSDT",
            ),
            "nonzero_symbol_rate": (
                self.config,
                self.symbols,
                self.support,
                {**self.rates, "ETHUSDT": 0.0001},
                "rpiCommissionRate must be zero for ETHUSDT",
            ),
        }

        for case, (
            config,
            symbols,
            support,
            rates,
            message,
        ) in cases.items():
            with self.subTest(case=case):
                with self.assertRaisesRegex(ValueError, message):
                    validate_live_rpi_policy(
                        config,
                        symbols,
                        support,
                        rates,
                    )
