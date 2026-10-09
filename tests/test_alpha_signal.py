import numpy as np
import pytest

from alpha.signal import (
    MultiHorizonPredictor,
    alpha_signal_report,
    predict_usable,
    OnlineRidgePredictor,
    predictor_kwargs_from_config,
    usable_predictions,
)


def _drive(predictor, features, returns_bps):
    mid = 100.0
    for index, (row, ret) in enumerate(zip(features, returns_bps)):
        predictor.update_and_predict(list(row), mid, float(index))
        mid *= 1.0 + ret / 10_000.0


def test_forgetting_factor_tracks_coefficient_regime_change():
    rng = np.random.default_rng(7)
    static = OnlineRidgePredictor(1, forgetting_factor=1.0)
    adaptive = OnlineRidgePredictor(1, forgetting_factor=0.99)
    for beta in (2.0, -2.0):
        for _ in range(3000):
            x = rng.normal()
            y = beta * x + rng.normal(scale=0.1)
            static.update([x], y)
            adaptive.update([x], y)

    assert adaptive.w[0, 0] == pytest.approx(-2.0, abs=0.1)
    assert abs(static.w[0, 0]) < 0.5


def test_forgetting_without_excitation_does_not_wind_up_covariance():
    model = OnlineRidgePredictor(3, lambda_reg=1.0, forgetting_factor=0.95)
    for _ in range(5000):
        model.update([1.0, 0.0, 0.0], 0.0)

    assert np.all(np.isfinite(model.P))
    assert np.trace(model.P) <= 3.0 + 1e-9


def test_noise_features_never_pass_out_of_sample_gate():
    rng = np.random.default_rng(3)
    predictor = MultiHorizonPredictor(
        num_features=3,
        horizons={"short": 1},
        min_oos_samples=200,
    )
    _drive(predictor, rng.normal(size=(3000, 3)), rng.normal(size=3000))

    report = predictor.oos_report()["short"]
    assert report["oos_samples"] > 200
    assert report["oos_r2"] <= 0.0
    assert not predictor.horizon_ready("short")
    assert usable_predictions(predictor, {"short": 1.5}) == {"short": 0.0}


def test_informative_features_with_nonzero_mean_pass_out_of_sample_gate():
    rng = np.random.default_rng(5)
    n = 4000
    signal = rng.normal(size=n)
    # One informative feature plus a nuisance feature with a large mean
    # (like log arrival rate); the intercept absorbs the mean.
    features = np.column_stack([signal, 5.0 + 0.5 * rng.normal(size=n)])
    returns = 0.8 * signal + rng.normal(scale=0.5, size=n)
    predictor = MultiHorizonPredictor(
        num_features=2,
        horizons={"short": 1},
        min_oos_samples=300,
    )
    _drive(predictor, features, returns)

    report = predictor.oos_report()["short"]
    assert report["oos_r2"] > 0.5
    assert predictor.horizon_ready("short")
    assert usable_predictions(predictor, {"short": 1.5}) == {"short": 1.5}


def test_out_of_sample_score_uses_prediction_made_before_label():
    predictor = MultiHorizonPredictor(
        num_features=1,
        horizons={"h": 3},
        fit_intercept=False,
    )
    for index in range(10):
        predictor.update_and_predict([1.0], 100.0 + index, float(index))

    # 10 snapshots with horizon 3 -> 7 matured labels, each scored against
    # the prediction stored when its features were first seen.
    assert predictor.scores["h"].count == 7
    assert predictor.sample_count == 7


def test_predictor_config_is_validated():
    assert predictor_kwargs_from_config(
        {"forgetting_factor": 0.995, "min_oos_samples": 50, "_comment": "x"}
    ) == {"forgetting_factor": 0.995, "min_oos_samples": 50}
    with pytest.raises(ValueError):
        predictor_kwargs_from_config({"forgetting_factor": 1.5})
    with pytest.raises(ValueError):
        predictor_kwargs_from_config({"min_oos_samples": 1.5})
    with pytest.raises(ValueError):
        predictor_kwargs_from_config({"ridge_lambda": 0.0})
    with pytest.raises(ValueError):
        predictor_kwargs_from_config({"min_oos_r2": 1.0})


def test_signal_report_keeps_raw_prediction_while_gate_zeroes_it():
    predictor = MultiHorizonPredictor(
        num_features=1,
        horizons={"short": 1},
        min_oos_samples=1_000,
    )
    used = {}
    for index in range(5):
        used = predict_usable(predictor, [float(index)], 100.0 + index, float(index))

    report = alpha_signal_report(predictor, used)["short"]
    assert used == {"short": 0.0}
    assert report["used_bps"] == 0.0
    assert report["prediction_bps"] == predictor.last_predictions["short"]
    assert report["ready"] is False
