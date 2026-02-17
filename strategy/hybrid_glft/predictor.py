# file: strategy/hybrid_glft/predictor.py

import numpy as np
import time
from collections import deque
from typing import List, Optional
from dataclasses import dataclass

# 分类 label 门槛（bps），过滤盘口噪音
LABEL_THRESHOLD_BPS: float = 4.0


@dataclass
class MLPrediction:
    p_trend: float           # 最高类别的置信概率
    predicted_bps: float     # 回归器预测的未来收益率 (bps)
    momentum_strength: float # 偏离均等分布的程度 (0~0.67)
    direction: str           # "UP", "DOWN", "NEUTRAL"
    timestamp: float


class MLTrendPredictor:
    def __init__(self, enabled: bool = True, horizon_sec: float = 5.0):
        self.enabled = enabled
        self.horizon_sec = horizon_sec
        self.trained = False

        self.training_buffer = deque(maxlen=5000)

        # Scaler 统计量，仅由 _fit_transform() 更新
        self.means = np.zeros(3)
        self.stds = np.ones(3)
        self.n_samples = 0

        try:
            from sklearn.linear_model import SGDClassifier, SGDRegressor
            self.clf = SGDClassifier(
                loss='log_loss', warm_start=True, penalty='l2', alpha=0.01
            )
            self.reg = SGDRegressor(warm_start=True, penalty='l2', alpha=0.01)
            self.has_model = True
        except ImportError:
            self.has_model = False
            print("[Predictor] Scikit-learn not found. ML mode disabled.")

    # ----------------------------------------------------------
    # Scaler 核心：fit+transform 与 transform-only 严格分离
    # ----------------------------------------------------------

    def _update_scaler(self, x: np.ndarray):
        """Welford 在线方差估计，只从这里修改统计量"""
        self.n_samples += 1
        last_means = self.means.copy()
        self.means += (x - self.means) / self.n_samples
        if self.n_samples > 1:
            self.stds = np.sqrt(
                ((self.stds ** 2 * (self.n_samples - 1))
                 + (x - last_means) * (x - self.means))
                / self.n_samples
            )
            self.stds = np.clip(self.stds, 1e-4, 1e6)

    def _fit_transform(self, x: List[float]) -> np.ndarray:
        """
        [训练专用] 更新 scaler 并返回标准化结果。
        仅在 add_tick() 内调用。
        """
        x_arr = np.array(x, dtype=float)
        self._update_scaler(x_arr)
        return (x_arr - self.means) / self.stds

    def _transform(self, x: List[float]) -> np.ndarray:
        """
        [推理专用] 只应用当前 scaler，不修改任何统计量。
        仅在 predict() 内调用。
        冷启动时 stds=1, means=0，等价于恒等变换，安全。
        """
        x_arr = np.array(x, dtype=float)
        return (x_arr - self.means) / self.stds

    # ----------------------------------------------------------
    # 在线训练（每 tick 调用，在频率门控之前）
    # ----------------------------------------------------------

    def add_tick(self, features: List[float], mid_price: float, now: float):
        """
        每帧行情到来时调用（不受 cycle_interval 限制，最大化训练密度）。
        流程：
          1. fit_transform 特征，同时更新 scaler
          2. 入队（带时间戳和原始 mid 价格）
          3. 检查队头是否已观测到 horizon_sec 后的价格 → 若是，生成 label 训练
        """
        if not self.enabled or not self.has_model:
            return

        scaled_features = self._fit_transform(features)  # [FIX-8] 只在这里更新 scaler
        self.training_buffer.append({
            "ts": now,
            "x": scaled_features,
            "price": mid_price
        })

        # 回溯标注：消费所有已到期的历史样本
        while (self.training_buffer
               and now - self.training_buffer[0]["ts"] >= self.horizon_sec):
            past = self.training_buffer.popleft()

            y_bps = (mid_price / past["price"] - 1.0) * 10000

            if y_bps > LABEL_THRESHOLD_BPS:
                label = 1
            elif y_bps < -LABEL_THRESHOLD_BPS:
                label = -1
            else:
                label = 0

            X = past["x"].reshape(1, -1)
            try:
                self.clf.partial_fit(X, [label], classes=[-1, 0, 1])
                self.reg.partial_fit(X, [y_bps])
                self.trained = True
            except Exception:
                pass

    # ----------------------------------------------------------
    # 推理（按 cycle_interval 调用，在频率门控之后）
    # ----------------------------------------------------------

    def predict(self, features: List[float], now: float) -> Optional[MLPrediction]:
        """
        按策略周期调用。
        使用 _transform() 而非 _standardize()，不修改 scaler 统计量。
        """
        if not self.enabled:
            return None

        if not self.trained or not self.has_model:
            return MLPrediction(0.5, 0.0, 0.0, "NEUTRAL", now)

        try:
            X = self._transform(features).reshape(1, -1)  # [FIX-8] 不更新 scaler

            probs = self.clf.predict_proba(X)[0]
            idx = int(np.argmax(probs))
            p_max = float(probs[idx])

            classes = [-1, 0, 1]
            direction = ("UP" if classes[idx] == 1
                         else "DOWN" if classes[idx] == -1
                         else "NEUTRAL")

            pred_bps = float(self.reg.predict(X)[0])

            return MLPrediction(
                p_trend=p_max,
                predicted_bps=pred_bps,
                momentum_strength=abs(p_max - 0.33),
                direction=direction,
                timestamp=now
            )
        except Exception:
            return MLPrediction(0.5, 0.0, 0.0, "NEUTRAL", now)

    # ----------------------------------------------------------
    # 可解释性接口（供 dashboard 展示）
    # ----------------------------------------------------------

    def get_weights(self) -> List[float]:
        """分类器对"UP"类别的权重 [w_OFI, w_Mom, w_Depth]"""
        if not self.trained or not self.has_model:
            return [0.0, 0.0, 0.0]
        try:
            # coef_ shape: (n_classes, n_features)；classes=[-1,0,1]，idx=2 为 UP
            return [float(w) for w in self.clf.coef_[2]]
        except Exception:
            return [0.0, 0.0, 0.0]

    def get_reg_weights(self) -> List[float]:
        """回归器权重 [w_OFI, w_Mom, w_Depth]，表示各特征对 bps 预测的贡献"""
        if not self.trained or not self.has_model:
            return [0.0, 0.0, 0.0]
        try:
            return [float(w) for w in self.reg.coef_]
        except Exception:
            return [0.0, 0.0, 0.0]

    def get_stats(self) -> dict:
        """
        返回模型状态快照，供 dashboard 实时展示。
        字段说明：
          trained      - 模型是否已完成至少一次 partial_fit
          n_samples    - scaler 已见过的训练样本数
          buffer_size  - 等待标注的样本队列长度（最大 5000）
          clf_weights  - 分类器对 UP 方向的 [OFI, Mom, Depth] 权重
          reg_weights  - 回归器的 [OFI, Mom, Depth] 权重
          means        - 当前特征均值
          stds         - 当前特征标准差
        """
        return {
            "trained": self.trained,
            "n_samples": self.n_samples,
            "buffer_size": len(self.training_buffer),
            "clf_weights": self.get_weights(),
            "reg_weights": self.get_reg_weights(),
            "means": self.means.tolist(),
            "stds": self.stds.tolist(),
        }