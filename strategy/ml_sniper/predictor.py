# file: strategy/ml_sniper/predictor.py

import numpy as np
from collections import deque
from typing import List, Dict

class KalmanFilterRegressor:
    def __init__(self, num_features: int, R: float = 1.0, Q: float = 1e-5):
        self.w = np.zeros((num_features, 1))
        self.P = np.eye(num_features) * 1.0
        self.R = R
        self.Q = np.eye(num_features) * Q
        self.I = np.eye(num_features)

    def predict(self, features: List) -> float:
        try:
            x = np.array(features, dtype=float).reshape(-1, 1)
            y_pred = (x.T @ self.w).item()
            return max(-50.0, min(50.0, float(y_pred)))
        except:
            return 0.0

    def update(self, features: List, y_true: float):
        try:
            x = np.array(features, dtype=float).reshape(-1, 1)
            self.P += self.Q
            S = (x.T @ self.P @ x).item() + self.R
            K = (self.P @ x) / S
            y_pred = (x.T @ self.w).item()
            error = y_true - y_pred
            self.w += K * error
            self.P = (self.I - K @ x.T) @ self.P
        except:
            pass

    def get_weights(self) -> List[float]:
        return self.w.flatten().tolist()

class TimeHorizonPredictor:
    def __init__(self, num_features: int = 9):
        self.horizons = {"1s": 1.0, "10s": 10.0, "30s": 30.0}
        self.models = {
            "1s":  KalmanFilterRegressor(num_features, R=10.0, Q=1e-4),
            "10s": KalmanFilterRegressor(num_features, R=10.0, Q=1e-5),
            "30s": KalmanFilterRegressor(num_features, R=10.0, Q=1e-6)
        }
        self.buffer = deque(maxlen=2000)
        self.last_trained_ts = {h: 0.0 for h in self.horizons}

    def update_and_predict(self, features: List, current_mid: float, now: float) -> Dict[str, float]:
        res = {h: 0.0 for h in self.horizons}
        if current_mid <= 0: return res
        
        # 存入快照
        self.buffer.append({"ts": now, "price": current_mid, "feats": features})
        
        # 训练逻辑
        # 使用 list(self.buffer) 防止多线程操作 deque 导致的迭代错误
        for past_data in list(self.buffer):
            # 防御性检查：确保 past_data 是字典
            if not isinstance(past_data, dict): continue
            
            elapsed = now - past_data["ts"]
            if elapsed < 0.9: break # 数据太新

            for h_name, h_sec in self.horizons.items():
                if elapsed >= h_sec and past_data["ts"] > self.last_trained_ts[h_name]:
                    y_bps = (current_mid / past_data["price"] - 1.0) * 10000.0
                    if abs(y_bps) < 500.0:
                        self.models[h_name].update(past_data["feats"], y_bps)
                    self.last_trained_ts[h_name] = past_data["ts"]

        # 预测结果
        for h_name, model in self.models.items():
            res[h_name] = float(model.predict(features))
        return res

    def get_model_weights(self, horizon: str) -> List[float]:
        if horizon in self.models:
            return self.models[horizon].get_weights()
        return []