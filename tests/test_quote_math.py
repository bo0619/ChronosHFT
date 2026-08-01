import math
import unittest

from strategy.quote_math import (
    ADAPTIVE_AS_FORMULA_VERSION,
    ADAPTIVE_GLFT_FORMULA_VERSION,
    ASQuoteScenario,
    AS_FORMULA_VERSION,
    GLFT_FORMULA_VERSION,
    GLFTQuoteScenario,
    PORTFOLIO_AS_FORMULA_VERSION,
    PORTFOLIO_GLFT_FORMULA_VERSION,
    UNITS_VERSION,
    PortfolioASQuoteSolution,
    PortfolioQuoteSolution,
    QuoteOffsets,
    adaptive_portfolio_as_quote_offsets,
    adaptive_portfolio_glft_quote_offsets,
    as_quote_offsets,
    depths_bps_to_prices,
    glft_quote_offsets,
    portfolio_glft_quote_offsets,
    robust_adaptive_portfolio_as_quote_offsets,
    robust_adaptive_portfolio_glft_quote_offsets,
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
            PORTFOLIO_AS_FORMULA_VERSION,
            "avellaneda_stoikov.log_bps_finite_horizon_portfolio.v1",
        )
        self.assertEqual(
            ADAPTIVE_AS_FORMULA_VERSION,
            "avellaneda_stoikov.log_bps_finite_horizon_adaptive_portfolio.v1",
        )
        self.assertEqual(
            GLFT_FORMULA_VERSION,
            "glft.log_bps_asymptotic_model_a.v2",
        )
        self.assertEqual(
            PORTFOLIO_GLFT_FORMULA_VERSION,
            "glft.log_bps_riccati_portfolio_model_a.v1",
        )
        self.assertEqual(
            ADAPTIVE_GLFT_FORMULA_VERSION,
            "glft.log_bps_finite_horizon_adaptive_portfolio_model_a.v1",
        )

    def test_portfolio_as_one_asset_exactly_recovers_scalar_as(self):
        scalar = as_quote_offsets(
            mid_price=100.0,
            inventory_lots=2.25,
            sigma_bps_sqrt_s=1.0,
            gamma_per_bps=0.05,
            k_per_bps=0.5,
            horizon_s=2.0,
        )
        portfolio = adaptive_portfolio_as_quote_offsets(
            mid_prices=[100.0],
            inventory_lots=[2.25],
            covariance_bps2_per_s=[[1.0]],
            gamma_per_bps=0.05,
            bid_k_per_bps=[0.5],
            ask_k_per_bps=[0.5],
            order_size_lots=[1.0],
            bid_adverse_cost_bps=[0.0],
            ask_adverse_cost_bps=[0.0],
            horizon_s=2.0,
        )

        self.assertIsInstance(portfolio, PortfolioASQuoteSolution)
        for field in (
            "bid_depth_bps",
            "ask_depth_bps",
            "center_offset_bps",
            "half_spread_bps",
            "bid_price",
            "ask_price",
        ):
            self.assertAlmostEqual(
                getattr(portfolio.quotes[0], field),
                getattr(scalar, field),
                places=12,
            )

    def test_portfolio_as_uses_exact_inventory_potential_differences(self):
        inventory = [1.25, -0.5]
        order_sizes = [0.75, 1.2]
        solution = adaptive_portfolio_as_quote_offsets(
            mid_prices=[100.0, 200.0],
            inventory_lots=inventory,
            covariance_bps2_per_s=[[4.0, 1.0], [1.0, 9.0]],
            gamma_per_bps=0.05,
            bid_k_per_bps=[0.5, 0.9],
            ask_k_per_bps=[0.5, 0.9],
            order_size_lots=order_sizes,
            bid_adverse_cost_bps=[0.0, 0.0],
            ask_adverse_cost_bps=[0.0, 0.0],
            horizon_s=2.0,
        )
        curvature = solution.risk_curvature_bps

        def penalty(position):
            return 0.5 * sum(
                position[row]
                * sum(
                    curvature[row][column] * position[column]
                    for column in range(2)
                )
                for row in range(2)
            )

        for index in range(2):
            buy_inventory = list(inventory)
            sell_inventory = list(inventory)
            buy_inventory[index] += order_sizes[index]
            sell_inventory[index] -= order_sizes[index]
            bid_risk = (
                penalty(buy_inventory) - penalty(inventory)
            ) / order_sizes[index]
            ask_risk = (
                penalty(sell_inventory) - penalty(inventory)
            ) / order_sizes[index]
            self.assertAlmostEqual(
                solution.quotes[index].bid_depth_bps,
                solution.bid_liquidity_depth_bps[index] + bid_risk,
                places=11,
            )
            self.assertAlmostEqual(
                solution.quotes[index].ask_depth_bps,
                solution.ask_liquidity_depth_bps[index] + ask_risk,
                places=11,
            )

    def test_robust_as_takes_per_side_scenario_envelope(self):
        baseline = ASQuoteScenario(
            name="BASE",
            bid_k_per_bps=[0.5],
            ask_k_per_bps=[0.5],
            covariance_bps2_per_s=[[1.0]],
            bid_adverse_cost_bps=[0.0],
            ask_adverse_cost_bps=[0.0],
        )
        adverse = ASQuoteScenario(
            name="ADVERSE",
            bid_k_per_bps=[0.5],
            ask_k_per_bps=[0.5],
            covariance_bps2_per_s=[[1.0]],
            bid_adverse_cost_bps=[2.0],
            ask_adverse_cost_bps=[3.0],
        )
        robust = robust_adaptive_portfolio_as_quote_offsets(
            mid_prices=[100.0],
            inventory_lots=[0.0],
            gamma_per_bps=0.05,
            order_size_lots=[1.0],
            horizon_s=1.0,
            scenarios=(baseline, adverse),
        )

        self.assertEqual(robust.selected_bid_scenario, ("ADVERSE",))
        self.assertEqual(robust.selected_ask_scenario, ("ADVERSE",))
        self.assertAlmostEqual(
            robust.quotes[0].bid_depth_bps,
            robust.scenario_solutions[0].quotes[0].bid_depth_bps + 2.0,
        )
        self.assertAlmostEqual(
            robust.quotes[0].ask_depth_bps,
            robust.scenario_solutions[0].quotes[0].ask_depth_bps + 3.0,
        )

    def test_adaptive_symmetric_asymptote_recovers_portfolio_glft(self):
        common = {
            "mid_prices": [100.0, 200.0],
            "inventory_lots": [1.25, -0.5],
            "covariance_bps2_per_s": [[4.0, 1.0], [1.0, 9.0]],
            "gamma_per_bps": 0.05,
            "order_size_lots": [0.75, 1.2],
        }
        original = portfolio_glft_quote_offsets(
            **common,
            A_per_s=[2.0, 1.5],
            k_per_bps=[0.5, 0.9],
        )
        adaptive = adaptive_portfolio_glft_quote_offsets(
            **common,
            bid_A_per_s=[2.0, 1.5],
            ask_A_per_s=[2.0, 1.5],
            bid_k_per_bps=[0.5, 0.9],
            ask_k_per_bps=[0.5, 0.9],
            bid_adverse_cost_bps=[0.0, 0.0],
            ask_adverse_cost_bps=[0.0, 0.0],
            horizon_s=None,
        )

        for adaptive_quote, original_quote in zip(
            adaptive.quotes,
            original.quotes,
            strict=True,
        ):
            for field in (
                "bid_depth_bps",
                "ask_depth_bps",
                "center_offset_bps",
                "half_spread_bps",
            ):
                self.assertAlmostEqual(
                    getattr(adaptive_quote, field),
                    getattr(original_quote, field),
                    places=11,
                )

    def test_finite_horizon_riccati_has_exact_scalar_tanh_solution(self):
        solution = adaptive_portfolio_glft_quote_offsets(
            mid_prices=[100.0],
            inventory_lots=[2.0],
            covariance_bps2_per_s=[[4.0]],
            gamma_per_bps=0.05,
            bid_A_per_s=[2.0],
            ask_A_per_s=[2.0],
            bid_k_per_bps=[0.5],
            ask_k_per_bps=[0.5],
            order_size_lots=[1.0],
            bid_adverse_cost_bps=[0.0],
            ask_adverse_cost_bps=[0.0],
            horizon_s=0.25,
        )
        c2 = solution.effective_c2_sqrt_s[0]
        expected_curvature = 2.0 * c2 * math.tanh(2.0 * 0.25 / c2)
        self.assertAlmostEqual(
            solution.risk_curvature_bps[0][0],
            expected_curvature,
            places=12,
        )
        asymptotic = 2.0 * c2
        self.assertGreater(solution.risk_curvature_bps[0][0], 0.0)
        self.assertLess(solution.risk_curvature_bps[0][0], asymptotic)

    def test_robust_quote_takes_per_side_scenario_envelope(self):
        baseline = GLFTQuoteScenario(
            name="BASE",
            bid_A_per_s=[2.0],
            ask_A_per_s=[2.0],
            bid_k_per_bps=[0.5],
            ask_k_per_bps=[0.5],
            covariance_bps2_per_s=[[1.0]],
            bid_adverse_cost_bps=[0.0],
            ask_adverse_cost_bps=[0.0],
        )
        adverse = GLFTQuoteScenario(
            name="ADVERSE",
            bid_A_per_s=[2.0],
            ask_A_per_s=[2.0],
            bid_k_per_bps=[0.5],
            ask_k_per_bps=[0.5],
            covariance_bps2_per_s=[[1.0]],
            bid_adverse_cost_bps=[2.0],
            ask_adverse_cost_bps=[3.0],
        )
        robust = robust_adaptive_portfolio_glft_quote_offsets(
            mid_prices=[100.0],
            inventory_lots=[0.0],
            gamma_per_bps=0.05,
            order_size_lots=[1.0],
            horizon_s=1.0,
            scenarios=(baseline, adverse),
        )

        self.assertEqual(robust.selected_bid_scenario, ("ADVERSE",))
        self.assertEqual(robust.selected_ask_scenario, ("ADVERSE",))
        self.assertAlmostEqual(
            robust.quotes[0].bid_depth_bps,
            robust.scenario_solutions[0].quotes[0].bid_depth_bps + 2.0,
        )
        self.assertAlmostEqual(
            robust.quotes[0].ask_depth_bps,
            robust.scenario_solutions[0].quotes[0].ask_depth_bps + 3.0,
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

    def test_portfolio_glft_one_asset_is_exactly_scalar_glft(self):
        scalar = glft_quote_offsets(
            mid_price=100.0,
            inventory_lots=2.25,
            sigma_bps_sqrt_s=1.0,
            gamma_per_bps=0.05,
            A_per_s=2.0,
            k_per_bps=0.5,
            order_size_lots=1.0,
        )
        portfolio = portfolio_glft_quote_offsets(
            mid_prices=[100.0],
            inventory_lots=[2.25],
            covariance_bps2_per_s=[[1.0]],
            gamma_per_bps=0.05,
            A_per_s=[2.0],
            k_per_bps=[0.5],
            order_size_lots=[1.0],
        )

        self.assertIsInstance(portfolio, PortfolioQuoteSolution)
        self.assertEqual(portfolio.quotes[0], scalar)
        self.assertAlmostEqual(
            portfolio.risk_curvature_bps[0][0],
            0.2670728696,
            places=10,
        )

    def test_portfolio_glft_diagonal_covariance_matches_independent_books(self):
        inputs = {
            "mid_prices": [100.0, 250.0],
            "inventory_lots": [2.25, -1.5],
            "covariance_bps2_per_s": [[1.0, 0.0], [0.0, 4.0]],
            "gamma_per_bps": 0.05,
            "A_per_s": [2.0, 3.0],
            "k_per_bps": [0.5, 0.7],
            "order_size_lots": [1.0, 0.75],
        }
        portfolio = portfolio_glft_quote_offsets(**inputs)

        for index in range(2):
            scalar = glft_quote_offsets(
                mid_price=inputs["mid_prices"][index],
                inventory_lots=inputs["inventory_lots"][index],
                sigma_bps_sqrt_s=math.sqrt(
                    inputs["covariance_bps2_per_s"][index][index]
                ),
                gamma_per_bps=inputs["gamma_per_bps"],
                A_per_s=inputs["A_per_s"][index],
                k_per_bps=inputs["k_per_bps"][index],
                order_size_lots=inputs["order_size_lots"][index],
            )
            for field in (
                "bid_depth_bps",
                "ask_depth_bps",
                "center_offset_bps",
                "half_spread_bps",
                "bid_price",
                "ask_price",
            ):
                self.assertAlmostEqual(
                    getattr(portfolio.quotes[index], field),
                    getattr(scalar, field),
                    places=12,
                )

    def test_portfolio_glft_prices_correlated_inventory_on_both_books(self):
        flat = portfolio_glft_quote_offsets(
            mid_prices=[100.0, 100.0],
            inventory_lots=[0.0, 0.0],
            covariance_bps2_per_s=[[1.0, 0.8], [0.8, 1.0]],
            gamma_per_bps=0.05,
            A_per_s=[2.0, 2.0],
            k_per_bps=[0.5, 0.5],
            order_size_lots=[1.0, 1.0],
        )
        long_second = portfolio_glft_quote_offsets(
            mid_prices=[100.0, 100.0],
            inventory_lots=[0.0, 2.0],
            covariance_bps2_per_s=[[1.0, 0.8], [0.8, 1.0]],
            gamma_per_bps=0.05,
            A_per_s=[2.0, 2.0],
            k_per_bps=[0.5, 0.5],
            order_size_lots=[1.0, 1.0],
        )

        self.assertLess(long_second.quotes[0].center_offset_bps, 0.0)
        self.assertLess(
            long_second.quotes[0].bid_price,
            flat.quotes[0].bid_price,
        )
        self.assertLess(
            long_second.quotes[0].ask_price,
            flat.quotes[0].ask_price,
        )

    def test_portfolio_glft_risk_charge_is_exact_potential_difference(self):
        inventory = [1.25, -0.5]
        order_sizes = [0.75, 1.2]
        solution = portfolio_glft_quote_offsets(
            mid_prices=[100.0, 200.0],
            inventory_lots=inventory,
            covariance_bps2_per_s=[[4.0, 1.0], [1.0, 9.0]],
            gamma_per_bps=0.05,
            A_per_s=[2.0, 1.5],
            k_per_bps=[0.5, 0.9],
            order_size_lots=order_sizes,
        )
        curvature = solution.risk_curvature_bps

        def penalty(position):
            return 0.5 * sum(
                position[row]
                * sum(
                    curvature[row][column] * position[column]
                    for column in range(2)
                )
                for row in range(2)
            )

        for index in range(2):
            buy_inventory = list(inventory)
            sell_inventory = list(inventory)
            buy_inventory[index] += order_sizes[index]
            sell_inventory[index] -= order_sizes[index]
            bid_risk = (
                penalty(buy_inventory) - penalty(inventory)
            ) / order_sizes[index]
            ask_risk = (
                penalty(sell_inventory) - penalty(inventory)
            ) / order_sizes[index]
            self.assertAlmostEqual(
                solution.quotes[index].bid_depth_bps,
                solution.c1_bps[index] + bid_risk,
                places=11,
            )
            self.assertAlmostEqual(
                solution.quotes[index].ask_depth_bps,
                solution.c1_bps[index] + ask_risk,
                places=11,
            )

    def test_portfolio_glft_rejects_invalid_covariance(self):
        inputs = {
            "mid_prices": [100.0, 200.0],
            "inventory_lots": [0.0, 0.0],
            "gamma_per_bps": 0.05,
            "A_per_s": [2.0, 2.0],
            "k_per_bps": [0.5, 0.5],
            "order_size_lots": [1.0, 1.0],
        }
        for covariance in (
            [[1.0, 0.1]],
            [[1.0, 0.2], [0.1, 1.0]],
            [[1.0, 2.0], [2.0, 1.0]],
            [[1.0, 0.0], [0.0, 0.0]],
        ):
            with self.subTest(covariance=covariance):
                with self.assertRaises(ValueError):
                    portfolio_glft_quote_offsets(
                        covariance_bps2_per_s=covariance,
                        **inputs,
                    )

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
