import math
import unittest

from strategy.quote_math import (
    AS_FORMULA_VERSION,
    GLFT_FORMULA_VERSION,
    UNITS_VERSION,
    QuoteOffsets,
    as_quote_offsets,
    depths_bps_to_prices,
    glft_quote_offsets,
)


class QuoteMathTests(unittest.TestCase):
    def test_formula_and_units_versions_are_explicit(self):
        self.assertEqual(
            UNITS_VERSION,
            "chronoshft.log_bps_seconds_fixed_notional_lot.v1",
        )
        self.assertEqual(
            AS_FORMULA_VERSION,
            "avellaneda_stoikov.log_bps_finite_horizon.v1",
        )
        self.assertEqual(
            GLFT_FORMULA_VERSION,
            "glft.log_bps_asymptotic_model_a.v2",
        )

    def test_as_finite_horizon_numeric_fixture(self):
        quote = as_quote_offsets(
            mid_price=100.0,
            inventory_lots=2.25,
            sigma_bps_sqrt_s=1.0,
            gamma_per_bps=0.05,
            k_per_bps=0.5,
            horizon_s=2.0,
        )

        self.assertIsInstance(quote, QuoteOffsets)
        self.assertAlmostEqual(quote.center_offset_bps, -0.225, places=12)
        self.assertAlmostEqual(quote.half_spread_bps, 1.9562035961, places=10)
        self.assertAlmostEqual(quote.bid_depth_bps, 2.1812035961, places=10)
        self.assertAlmostEqual(quote.ask_depth_bps, 1.7312035961, places=10)
        self.assertAlmostEqual(quote.bid_price, 99.9781903427, places=10)
        self.assertAlmostEqual(quote.ask_price, 100.0173135346, places=10)

    def test_glft_asymptotic_model_a_numeric_fixture(self):
        quote = glft_quote_offsets(
            mid_price=100.0,
            inventory_lots=2.25,
            sigma_bps_sqrt_s=1.0,
            gamma_per_bps=0.05,
            A_per_s=2.0,
            k_per_bps=0.5,
            order_size_lots=1.0,
        )
        expected_c1 = 1.9062035961
        expected_c2 = 0.2670728696
        recovered_c2 = -quote.center_offset_bps / 2.25
        recovered_c1 = quote.half_spread_bps - 0.5 * recovered_c2

        self.assertAlmostEqual(recovered_c1, expected_c1, places=10)
        self.assertAlmostEqual(recovered_c2, expected_c2, places=10)
        self.assertAlmostEqual(quote.half_spread_bps, 2.0397400309, places=10)
        self.assertAlmostEqual(quote.bid_depth_bps, 2.6406539874, places=10)
        self.assertAlmostEqual(quote.ask_depth_bps, 1.4388260744, places=10)

    def test_log_bps_depth_conversion_is_exponential(self):
        bid, ask = depths_bps_to_prices(
            mid_price=100.0,
            bid_depth_bps=2.5,
            ask_depth_bps=3.5,
        )

        self.assertEqual(bid, 100.0 * math.exp(-2.5 / 10_000.0))
        self.assertEqual(ask, 100.0 * math.exp(3.5 / 10_000.0))

    def test_price_scale_does_not_change_bps_offsets(self):
        as_small = as_quote_offsets(100.0, 2.25, 1.0, 0.05, 0.5, 2.0)
        as_large = as_quote_offsets(10_000.0, 2.25, 1.0, 0.05, 0.5, 2.0)
        glft_small = glft_quote_offsets(100.0, 2.25, 1.0, 0.05, 2.0, 0.5)
        glft_large = glft_quote_offsets(
            10_000.0,
            2.25,
            1.0,
            0.05,
            2.0,
            0.5,
        )

        for small, large in ((as_small, as_large), (glft_small, glft_large)):
            self.assertEqual(small.bid_depth_bps, large.bid_depth_bps)
            self.assertEqual(small.ask_depth_bps, large.ask_depth_bps)
            self.assertEqual(small.center_offset_bps, large.center_offset_bps)
            self.assertEqual(small.half_spread_bps, large.half_spread_bps)
            self.assertAlmostEqual(
                large.bid_price / small.bid_price,
                100.0,
                places=12,
            )
            self.assertAlmostEqual(
                large.ask_price / small.ask_price,
                100.0,
                places=12,
            )

    def test_positive_inventory_moves_both_models_quotes_down(self):
        as_flat = as_quote_offsets(100.0, 0.0, 1.0, 0.05, 0.5, 2.0)
        as_long = as_quote_offsets(100.0, 2.0, 1.0, 0.05, 0.5, 2.0)
        glft_flat = glft_quote_offsets(100.0, 0.0, 1.0, 0.05, 2.0, 0.5)
        glft_long = glft_quote_offsets(100.0, 2.0, 1.0, 0.05, 2.0, 0.5)

        for flat, long in ((as_flat, as_long), (glft_flat, glft_long)):
            self.assertGreater(long.bid_depth_bps, flat.bid_depth_bps)
            self.assertLess(long.ask_depth_bps, flat.ask_depth_bps)
            self.assertLess(long.bid_price, flat.bid_price)
            self.assertLess(long.ask_price, flat.ask_price)
            self.assertLess(long.center_offset_bps, 0.0)

    def test_negative_inventory_moves_both_models_quotes_up(self):
        as_flat = as_quote_offsets(100.0, 0.0, 1.0, 0.05, 0.5, 2.0)
        as_short = as_quote_offsets(100.0, -2.0, 1.0, 0.05, 0.5, 2.0)
        glft_flat = glft_quote_offsets(100.0, 0.0, 1.0, 0.05, 2.0, 0.5)
        glft_short = glft_quote_offsets(100.0, -2.0, 1.0, 0.05, 2.0, 0.5)

        for flat, short in ((as_flat, as_short), (glft_flat, glft_short)):
            self.assertLess(short.bid_depth_bps, flat.bid_depth_bps)
            self.assertGreater(short.ask_depth_bps, flat.ask_depth_bps)
            self.assertGreater(short.bid_price, flat.bid_price)
            self.assertGreater(short.ask_price, flat.ask_price)
            self.assertGreater(short.center_offset_bps, 0.0)

    def test_nonpositive_physical_inputs_are_rejected(self):
        as_inputs = {
            "mid_price": 100.0,
            "inventory_lots": 0.0,
            "sigma_bps_sqrt_s": 1.0,
            "gamma_per_bps": 0.05,
            "k_per_bps": 0.5,
            "horizon_s": 2.0,
        }
        glft_inputs = {
            "mid_price": 100.0,
            "inventory_lots": 0.0,
            "sigma_bps_sqrt_s": 1.0,
            "gamma_per_bps": 0.05,
            "A_per_s": 2.0,
            "k_per_bps": 0.5,
            "order_size_lots": 1.0,
        }

        for name in (
            "mid_price",
            "sigma_bps_sqrt_s",
            "gamma_per_bps",
            "k_per_bps",
            "horizon_s",
        ):
            with self.subTest(model="as", name=name):
                with self.assertRaises(ValueError):
                    as_quote_offsets(**{**as_inputs, name: 0.0})
        for name in (
            "mid_price",
            "sigma_bps_sqrt_s",
            "gamma_per_bps",
            "A_per_s",
            "k_per_bps",
            "order_size_lots",
        ):
            with self.subTest(model="glft", name=name):
                with self.assertRaises(ValueError):
                    glft_quote_offsets(**{**glft_inputs, name: -1.0})

    def test_nonfinite_and_nonreal_inputs_are_rejected(self):
        for bad in (math.nan, math.inf, -math.inf, True, "1.0"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    as_quote_offsets(100.0, bad, 1.0, 0.05, 0.5, 2.0)
                with self.assertRaises(ValueError):
                    glft_quote_offsets(100.0, bad, 1.0, 0.05, 2.0, 0.5)
                with self.assertRaises(ValueError):
                    depths_bps_to_prices(100.0, bad, 1.0)

    def test_extreme_finite_inputs_fail_closed_on_invalid_outputs(self):
        with self.assertRaises(ValueError):
            as_quote_offsets(
                mid_price=100.0,
                inventory_lots=1.0,
                sigma_bps_sqrt_s=1e308,
                gamma_per_bps=0.05,
                k_per_bps=0.5,
                horizon_s=2.0,
            )
        with self.assertRaises(ValueError):
            glft_quote_offsets(
                mid_price=100.0,
                inventory_lots=1e308,
                sigma_bps_sqrt_s=1e308,
                gamma_per_bps=0.05,
                A_per_s=2.0,
                k_per_bps=0.5,
            )
        with self.assertRaises(ValueError):
            depths_bps_to_prices(100.0, -1e308, 1.0)


if __name__ == "__main__":
    unittest.main()
