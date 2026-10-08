# file: alpha/signal.py

import functools
import math
from collections import deque

import numpy as np


class OnlineRidgePredictor:
    """
    单一尺度的在线岭回归模型 (RLS)

    forgetting_factor < 1 时为指数加权 RLS: 旧样本权重按 lambda^age 衰减,
    有效记忆长度约 1 / (1 - lambda) 个样本, 模型可以跟随市场状态漂移。
    max_p_trace 限制 P 的迹, 防止特征缺乏激励时 P / lambda 指数膨胀 (windup)。
    """
    def __init__(
        self,
        num_features,
        lambda_reg=1.0,
        forgetting_factor=1.0,
        max_p_trace=None,
        prediction_clip_bps=20.0,
    ):
        if not 0.0 < forgetting_factor <= 1.0:
            raise ValueError("forgetting_factor must be in (0, 1]")
        self.num_features = num_features
        self.forgetting_factor = float(forgetting_factor)
        self.prediction_clip_bps = float(prediction_clip_bps)
        # 权重向量 beta
        self.w = np.zeros((num_features, 1))
        # 协方差矩阵的逆 (P matrix)
        self.P = np.eye(num_features) / lambda_reg
        # 默认把 P 的迹限制在先验水平, 即模型不确定性永远不超过初始状态
        self.max_p_trace = (
            float(max_p_trace)
            if max_p_trace is not None
            else float(np.trace(self.P))
        )

        # 内部状态缓存 (用于 update_and_predict)
        self.last_features = None
        self.last_mid = None
        self.sample_count = 0

    def update(self, features, y_true):
        """核心学习步: (指数加权) Recursive Least Squares 更新"""
        X = np.array(features, dtype=float).reshape(-1, 1)
        lam = self.forgetting_factor

        # K = P * X / (lambda + X.T * P * X)
        PX = self.P @ X
        den = lam + (X.T @ PX)[0, 0]
        K = PX / den

        # Error = y - X.T * w
        err = y_true - (X.T @ self.w)[0, 0]

        # w = w + K * Error
        self.w += K * err

        # P = (P - K * X.T * P) / lambda, 并保持对称
        P = (self.P - K @ PX.T) / lam
        P = 0.5 * (P + P.T)
        trace = float(np.trace(P))
        if trace > self.max_p_trace > 0.0:
            P *= self.max_p_trace / trace
        self.P = P
        self.sample_count += 1

    def predict(self, features):
        """核心预测步"""
        X = np.array(features, dtype=float).reshape(-1, 1)
        pred = (X.T @ self.w)[0, 0]
        # 钳制异常值 (防止初期波动过大)
        clip = self.prediction_clip_bps
        return max(-clip, min(clip, pred))

    def update_and_predict(self, current_features, current_mid):
        """
        [NEW] 封装方法：自动处理历史状态回溯和标签计算
        1. 使用 (LastFeat, CurrentReturn) 更新模型
        2. 使用 (CurrentFeat) 预测 NextReturn
        """
        # 1. 学习 (如果有上一帧的状态)
        if self.last_features is not None and self.last_mid is not None and current_mid > 0:
            # Label: 这一秒产生的真实收益率 (bps)
            y_true = (current_mid / self.last_mid - 1.0) * 10000
            self.update(self.last_features, y_true)

        # 2. 更新状态缓存
        self.last_features = current_features
        self.last_mid = current_mid

        # 3. 预测未来
        return self.predict(current_features)


def _half_life_decay(half_life):
    half_life = float(half_life)
    if not half_life > 0.0:
        raise ValueError("half_life must be positive")
    return 0.5 ** (1.0 / half_life)


class PrequentialScore:
    """
    真正的样本外 (prequential) 评估: 每个预测在其标签成熟之前就已固定,
    只在标签到达时打分一次。基准是"零预测", 即 fair value = mid。

    r2 = 1 - SSE_model / SSE_zero (> 0 说明比直接用 mid 更好)
    ic = sum(pred * y) / sqrt(sum(pred^2) * sum(y^2))
    """
    def __init__(self, half_life=1000.0):
        self.decay = _half_life_decay(half_life)
        self.sse_model = 0.0
        self.sse_zero = 0.0
        self.sum_py = 0.0
        self.sum_pp = 0.0
        self.count = 0

    def update(self, pred, y):
        d = self.decay
        self.sse_model = d * self.sse_model + (y - pred) ** 2
        self.sse_zero = d * self.sse_zero + y * y
        self.sum_py = d * self.sum_py + pred * y
        self.sum_pp = d * self.sum_pp + pred * pred
        self.count += 1

    @property
    def r2(self):
        if self.sse_zero <= 0.0:
            return 0.0
        return 1.0 - self.sse_model / self.sse_zero

    @property
    def ic(self):
        denom = math.sqrt(self.sum_pp * self.sse_zero)
        if denom <= 0.0:
            return 0.0
        return self.sum_py / denom


class MultiHorizonPredictor:
    """
    多尺度预测器 (包装器)
    同时维护 Short/Mid/Long 三个周期的预测模型

    训练流程 (每个策略周期一次):
    1. 原始特征 (+ 截距项) 存入历史缓冲
    2. 对每个 horizon h: 取 h 个周期前的特征和当时做出的预测,
       用已成熟的收益标签先给"当时的预测"打样本外分, 再用该标签训练

    注: 离线回放显示在线标准化和标签截断都会降低样本外 R^2, 所以不做。
    3. 用当前特征给出各 horizon 的预测
    horizon_ready(name) 只有在样本外 R^2 达标后才为 True。
    """
    DEFAULT_HORIZONS = {
        "short": 1,    # 1个策略周期后
        "mid":   10,   # 10个策略周期后
        "long":  60    # 60个策略周期后
    }

    def __init__(
        self,
        num_features=9,
        horizons=None,
        *,
        forgetting_factor=0.999,
        ridge_lambda=1.0,
        fit_intercept=True,
        oos_half_life=1000.0,
        min_oos_samples=300,
        min_oos_r2=0.0,
    ):
        self.horizons = dict(horizons or self.DEFAULT_HORIZONS)
        if not self.horizons or any(
            int(h) != h or h < 1 for h in self.horizons.values()
        ):
            raise ValueError("horizons must be positive integers")
        if not (math.isfinite(float(ridge_lambda)) and float(ridge_lambda) > 0.0):
            raise ValueError("ridge_lambda must be positive")
        if int(min_oos_samples) < 1:
            raise ValueError("min_oos_samples must be at least 1")
        if not -1.0 < float(min_oos_r2) < 1.0:
            raise ValueError("min_oos_r2 must be in (-1, 1)")
        self.fit_intercept = bool(fit_intercept)
        self.min_oos_samples = int(min_oos_samples)
        self.min_oos_r2 = float(min_oos_r2)

        model_dim = num_features + (1 if self.fit_intercept else 0)
        # 实例化三个独立的 OnlineRidgePredictor
        self.models = {
            h: OnlineRidgePredictor(
                model_dim,
                lambda_reg=ridge_lambda,
                forgetting_factor=forgetting_factor,
            )
            for h in self.horizons
        }
        self.scores = {h: PrequentialScore(oos_half_life) for h in self.horizons}
        # 历史缓冲区: (timestamp, mid_price, 特征, 当时的预测)
        self.history_buffer = deque(maxlen=max(self.horizons.values()) + 1)

    @property
    def sample_count(self):
        return min(
            (model.sample_count for model in self.models.values()),
            default=0,
        )

    def horizon_ready(self, name):
        score = self.scores[name]
        return score.count >= self.min_oos_samples and score.r2 > self.min_oos_r2

    def oos_report(self):
        return {
            name: {
                "oos_samples": score.count,
                "oos_r2": float(score.r2),
                "oos_ic": float(score.ic),
                "ready": self.horizon_ready(name),
            }
            for name, score in self.scores.items()
        }

    def update_and_predict(self, features: list, current_mid: float, timestamp: float):
        """
        返回: 字典 {"short": bps, "mid": bps, "long": bps}
        """
        results = {name: 0.0 for name in self.horizons}

        if not current_mid > 0 or not math.isfinite(current_mid):
            return results

        z = np.asarray(features, dtype=float)
        if self.fit_intercept:
            z = np.append(z, 1.0)

        # 1. 存入当前快照
        entry = {
            "ts": timestamp,
            "price": current_mid,
            "feats": z,
            "preds": None,
        }
        self.history_buffer.append(entry)

        # 2. 样本外打分 + 训练 (回溯历史)
        current_idx = len(self.history_buffer) - 1

        for name, horizon in self.horizons.items():
            past_idx = current_idx - horizon
            if past_idx >= 0:
                past_data = self.history_buffer[past_idx]

                # 计算多尺度 Label: (Price_Now - Price_Past) / Price_Past
                y_true = (current_mid / past_data["price"] - 1.0) * 10000

                # 用当时实际给出的预测打分 (预测时还看不到这个标签)
                if past_data["preds"] is not None:
                    self.scores[name].update(past_data["preds"][name], y_true)

                # 训练对应的模型
                self.models[name].update(past_data["feats"], y_true)

        # 3. 预测
        for name, model in self.models.items():
            results[name] = model.predict(z)
        entry["preds"] = dict(results)

        return results


_PREDICTOR_FLOAT_KEYS = (
    "forgetting_factor",
    "ridge_lambda",
    "oos_half_life",
    "min_oos_r2",
)


def predictor_kwargs_from_config(config):
    """Parse strategy.glft.alpha.predictor; fails fast on bad values."""
    if not isinstance(config, dict):
        raise ValueError("alpha.predictor must be an object")
    kwargs = {}
    for key, value in config.items():
        if key == "min_oos_samples":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("alpha.predictor.min_oos_samples must be an integer")
            kwargs[key] = value
        elif key in _PREDICTOR_FLOAT_KEYS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"alpha.predictor.{key} must be a number")
            kwargs[key] = float(value)
    MultiHorizonPredictor(**kwargs)
    return kwargs


def predictor_from_config(config, num_features=9):
    """Validated factory for per-symbol MultiHorizonPredictor instances."""
    return functools.partial(
        MultiHorizonPredictor,
        num_features,
        **predictor_kwargs_from_config(config),
    )


def usable_predictions(model, predictions):
    """
    只放行样本外 R^2 已跑赢 fair_value=mid 基准的 horizon, 其余置 0。
    没有 horizon_ready 的模型 (测试替身) 原样放行。
    """
    horizon_ready = getattr(model, "horizon_ready", None)
    if horizon_ready is None:
        return dict(predictions)
    return {
        name: value if horizon_ready(name) else 0.0
        for name, value in predictions.items()
    }
