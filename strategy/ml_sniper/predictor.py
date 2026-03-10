import numpy as np
from collections import deque
from typing import Dict, List


class OnlineFeatureScaler:
    """Streaming z-score scaler with conservative clipping."""

    def __init__(self, num_features: int):
        self.num_features = num_features
        self.count = 0
        self.mean = np.zeros(num_features, dtype=float)
        self.m2 = np.zeros(num_features, dtype=float)

    def transform(self, features: List[float]) -> List[float]:
        try:
            x = np.array(features, dtype=float).reshape(-1)
            if x.shape[0] != self.num_features:
                return x.tolist()
            if self.count < 8:
                return np.clip(x, -10.0, 10.0).tolist()

            variance = np.maximum(self.m2 / max(self.count - 1, 1), 1e-6)
            z = (x - self.mean) / np.sqrt(variance)
            return np.clip(z, -6.0, 6.0).tolist()
        except Exception:
            return list(features)

    def partial_fit(self, features: List[float]):
        try:
            x = np.array(features, dtype=float).reshape(-1)
            if x.shape[0] != self.num_features:
                return

            self.count += 1
            delta = x - self.mean
            self.mean += delta / self.count
            delta2 = x - self.mean
            self.m2 += delta * delta2
        except Exception:
            return


class KalmanFilterRegressor:
    def __init__(self, num_features: int, R: float = 1.0, Q: float = 1e-5):
        self.num_features = num_features
        self.w = np.zeros((num_features, 1))
        self.P = np.eye(num_features)
        self.R = float(R)
        self.Q = np.eye(num_features) * Q
        self.I = np.eye(num_features)
        self.n_updates = 0

    def predict(self, features: List[float]) -> float:
        pred, _ = self.predict_with_uncertainty(features)
        return pred

    def predict_with_uncertainty(self, features: List[float]) -> tuple[float, float]:
        try:
            x = np.array(features, dtype=float).reshape(-1, 1)
            y_pred = float((x.T @ self.w).item())
            variance = float((x.T @ self.P @ x).item()) + self.R
            variance = max(1e-6, variance)
            return float(np.clip(y_pred, -50.0, 50.0)), float(np.sqrt(variance))
        except Exception:
            return 0.0, 10.0

    def update(self, features: List[float], y_true: float):
        try:
            x = np.array(features, dtype=float).reshape(-1, 1)
            self.P += self.Q

            px = self.P @ x
            s = float((x.T @ px).item()) + self.R
            k = px / s

            y_pred = float((x.T @ self.w).item())
            error = y_true - y_pred
            self.w += k * error

            ikx = self.I - k @ x.T
            self.P = ikx @ self.P @ ikx.T + self.R * (k @ k.T)
            self.n_updates += 1
        except Exception:
            return

    def get_weights(self) -> List[float]:
        return self.w.flatten().tolist()

    @property
    def is_warmed_up(self) -> bool:
        return self.n_updates >= 2


class TimeHorizonPredictor:
    def __init__(self, num_features: int = 9, label_config: Dict | None = None):
        self.horizons = {
            "1s": 1.0,
            "10s": 10.0,
            "30s": 30.0,
        }
        self.models: Dict[str, KalmanFilterRegressor] = {
            "1s": KalmanFilterRegressor(num_features, R=5.0, Q=1e-4),
            "10s": KalmanFilterRegressor(num_features, R=1.5, Q=1e-5),
            "30s": KalmanFilterRegressor(num_features, R=0.5, Q=1e-6),
        }
        self.scaler = OnlineFeatureScaler(num_features)
        self.buffer: deque = deque(maxlen=2000)
        self.last_trained_ts: Dict[str, float] = {h: 0.0 for h in self.horizons}
        self.last_diagnostics: Dict[str, Dict[str, float]] = {
            h: {"pred": 0.0, "std": 10.0, "confidence": 0.0}
            for h in self.horizons
        }

        label_config = label_config or {}
        maker_share_cfg = label_config.get("maker_share_by_horizon", {})
        carry_cfg = label_config.get("horizon_penalty_bps", {})
        self.max_label_abs_bps = float(label_config.get("max_abs_label_bps", 100.0))
        self.min_tradeable_cost_bps = float(label_config.get("min_tradeable_cost_bps", 0.25))
        self.label_maker_fee_bps = float(label_config.get("maker_fee_bps", 0.0))
        self.label_taker_fee_bps = float(label_config.get("taker_fee_bps", 5.0))
        self.label_maker_spread_weight = float(label_config.get("maker_spread_weight", 0.20))
        self.label_taker_spread_weight = float(label_config.get("taker_spread_weight", 0.55))
        self.label_maker_sigma_weight = float(label_config.get("maker_sigma_weight", 0.10))
        self.label_taker_sigma_weight = float(label_config.get("taker_sigma_weight", 0.35))
        self.maker_share_by_horizon = {
            "1s": float(maker_share_cfg.get("1s", 0.25)),
            "10s": float(maker_share_cfg.get("10s", 0.55)),
            "30s": float(maker_share_cfg.get("30s", 0.75)),
        }
        self.horizon_penalty_bps = {
            "1s": float(carry_cfg.get("1s", 0.10)),
            "10s": float(carry_cfg.get("10s", 0.35)),
            "30s": float(carry_cfg.get("30s", 0.75)),
        }

    def _approx_trade_cost_bps(self, horizon: str, snapshot: Dict[str, float]) -> float:
        spread_bps = float(max(0.0, snapshot.get("spread_bps", 0.0)))
        sigma_bps = float(max(0.0, snapshot.get("sigma_bps", 0.0)))
        maker_share = float(self.maker_share_by_horizon.get(horizon, 0.5))

        maker_cost = self.label_maker_fee_bps + spread_bps * self.label_maker_spread_weight + sigma_bps * self.label_maker_sigma_weight
        taker_cost = self.label_taker_fee_bps + spread_bps * self.label_taker_spread_weight + sigma_bps * self.label_taker_sigma_weight
        blended_cost = maker_share * maker_cost + (1.0 - maker_share) * taker_cost
        return blended_cost + float(self.horizon_penalty_bps.get(horizon, 0.0)) + self.min_tradeable_cost_bps

    def _to_tradable_label(self, gross_bps: float, cost_bps: float) -> float:
        if gross_bps == 0.0:
            return 0.0
        direction = 1.0 if gross_bps > 0 else -1.0
        return direction * max(abs(gross_bps) - cost_bps, 0.0)

    def update_and_predict(
        self,
        features: List[float],
        current_mid: float,
        now: float,
        spread_bps: float = 0.0,
        sigma_bps: float = 0.0,
    ) -> Dict[str, float]:
        res = {h: 0.0 for h in self.horizons}
        if current_mid <= 0:
            return res

        scaled_features = self.scaler.transform(features)
        self.buffer.append(
            {
                "ts": now,
                "price": current_mid,
                "feats": scaled_features,
                "spread_bps": float(max(0.0, spread_bps)),
                "sigma_bps": float(max(0.0, sigma_bps)),
            }
        )
        buf_list = list(self.buffer)

        for horizon, horizon_sec in self.horizons.items():
            for past_data in reversed(buf_list):
                if not isinstance(past_data, dict):
                    continue

                elapsed = now - past_data["ts"]
                if elapsed < horizon_sec:
                    continue
                if past_data["ts"] <= self.last_trained_ts[horizon]:
                    break

                gross_bps = (current_mid / past_data["price"] - 1.0) * 10000.0
                cost_bps = self._approx_trade_cost_bps(horizon, past_data)
                net_bps = self._to_tradable_label(gross_bps, cost_bps)
                if abs(net_bps) < self.max_label_abs_bps:
                    self.models[horizon].update(past_data["feats"], net_bps)

                self.last_trained_ts[horizon] = past_data["ts"]
                break

        for horizon, model in self.models.items():
            pred, std = model.predict_with_uncertainty(scaled_features)
            confidence = float(np.clip(abs(pred) / max(std, 1.0) / 3.0, 0.0, 1.0))
            res[horizon] = pred
            self.last_diagnostics[horizon] = {
                "pred": pred,
                "std": std,
                "confidence": confidence,
            }

        self.scaler.partial_fit(features)
        return res

    def get_model_weights(self, horizon: str) -> List[float]:
        if horizon in self.models:
            return self.models[horizon].get_weights()
        return []

    @property
    def is_warmed_up(self) -> bool:
        return all(model.is_warmed_up for model in self.models.values())

    def warmup_progress(self) -> Dict[str, int]:
        return {h: model.n_updates for h, model in self.models.items()}

    def get_last_diagnostics(self) -> Dict[str, Dict[str, float]]:
        return {
            horizon: dict(values)
            for horizon, values in self.last_diagnostics.items()
        }
