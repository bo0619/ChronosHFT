import unittest

from strategy.ml_sniper.predictor import TimeHorizonPredictor


class PredictorNetLabelTests(unittest.TestCase):
    def make_predictor(self):
        return TimeHorizonPredictor(
            num_features=1,
            label_config={
                "maker_fee_bps": 0.0,
                "taker_fee_bps": 0.0,
                "maker_spread_weight": 0.0,
                "taker_spread_weight": 1.0,
                "maker_sigma_weight": 0.0,
                "taker_sigma_weight": 0.0,
                "min_tradeable_cost_bps": 1.0,
                "maker_share_by_horizon": {"1s": 0.0, "10s": 0.0, "30s": 0.0},
                "horizon_penalty_bps": {"1s": 0.0, "10s": 0.0, "30s": 0.0},
            },
        )

    def test_small_move_inside_cost_band_trains_to_zero(self):
        predictor = self.make_predictor()
        predictor.update_and_predict([1.0], 100.0, 0.1, spread_bps=1.0, sigma_bps=0.0)
        predictor.update_and_predict([1.0], 100.015, 1.1, spread_bps=1.0, sigma_bps=0.0)

        self.assertAlmostEqual(predictor.models["1s"].get_weights()[0], 0.0, places=8)

    def test_large_move_above_cost_band_updates_model(self):
        predictor = self.make_predictor()
        predictor.update_and_predict([1.0], 100.0, 0.1, spread_bps=1.0, sigma_bps=0.0)
        predictor.update_and_predict([1.0], 100.05, 1.1, spread_bps=1.0, sigma_bps=0.0)

        self.assertGreater(predictor.models["1s"].get_weights()[0], 0.0)


if __name__ == "__main__":
    unittest.main()
