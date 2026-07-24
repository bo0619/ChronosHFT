import unittest

from infrastructure.commission_truth import (
    parse_commission_rate_payload,
    resolve_passive_fee_rate,
)


class CommissionTruthTests(unittest.TestCase):
    def test_rpi_final_rate_replaces_standard_maker_rate(self):
        rate = resolve_passive_fee_rate(
            maker_rate=0.0002,
            symbol="XAUUSDT",
            is_rpi=True,
            rpi_commission_rates={"XAUUSDT": 0.0},
            default_rpi_commission_rate=0.0003,
        )

        self.assertEqual(rate, 0.0)

    def test_standard_maker_and_symbol_specific_rpi_rates_remain_distinct(self):
        maker_rate = resolve_passive_fee_rate(
            maker_rate=0.0002,
            symbol="XAUUSDT",
            is_rpi=False,
            rpi_commission_rates={"XAUUSDT": 0.0001},
        )
        rpi_rate = resolve_passive_fee_rate(
            maker_rate=0.0002,
            symbol="xauusdt",
            is_rpi=True,
            rpi_commission_rates={"XAUUSDT": 0.0001},
        )

        self.assertEqual(maker_rate, 0.0002)
        self.assertEqual(rpi_rate, 0.0001)

    def test_account_commission_payload_is_bound_to_requested_symbol(self):
        parsed = parse_commission_rate_payload(
            {
                "symbol": "XAUUSDT",
                "makerCommissionRate": "0.0002",
                "takerCommissionRate": "0.0005",
                "rpiCommissionRate": "0",
            },
            symbol="xauusdt",
        )

        self.assertEqual(str(parsed["rpiCommissionRate"]), "0")

        with self.assertRaisesRegex(ValueError, "symbol mismatch"):
            parse_commission_rate_payload(
                {
                    "symbol": "BTCUSDT",
                    "makerCommissionRate": "0.0002",
                    "takerCommissionRate": "0.0005",
                    "rpiCommissionRate": "0",
                },
                symbol="XAUUSDT",
            )

    def test_account_commission_payload_rejects_missing_or_invalid_rates(self):
        valid = {
            "symbol": "XAUUSDT",
            "makerCommissionRate": "0.0002",
            "takerCommissionRate": "0.0005",
            "rpiCommissionRate": "0",
        }
        invalid_payloads = (
            None,
            {key: value for key, value in valid.items() if key != "rpiCommissionRate"},
            {**valid, "rpiCommissionRate": "nan"},
            {**valid, "rpiCommissionRate": "0.010001"},
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    parse_commission_rate_payload(
                        payload,
                        symbol="XAUUSDT",
                    )
