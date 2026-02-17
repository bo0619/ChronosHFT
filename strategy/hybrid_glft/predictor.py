# file: strategy/hybrid_glft/predictor.py

import numpy as np
import time
from collections import deque
from typing import List, Optional, Dict, Tuple
from dataclasses import dataclass

@dataclass
class MLPrediction:
    p_trend: float          # 趋势发生的概率 (0.5~1.0)
    predicted_bps: float    # 预测的未来收益率 (bps)
    momentum_strength: float # 动能强度 (0.0~1.0)
    direction: str          # "UP", "DOWN", "NEUTRAL"
    timestamp: float

class MLTrendPredictor:
    def __init__(self, enabled: bool = True, horizon_sec: float = 5.0):
        self.enabled = enabled
        self.horizon_sec = horizon_sec
        self.trained = False
        
        # 缓冲区：存储 (timestamp, features, mid_price)
        # 用于等待 horizon_sec 秒后计算“未来的真实价格”作为 Label
        self.training_buffer = deque(maxlen=5000)
        
        # 特征标准化参数 (在线增量更新)
        self.means = np.zeros(3)
        self.stds = np.ones(3)
        self.n_samples = 0

        # 在线模型初始化
        try:
            from sklearn.linear_model import SGDClassifier, SGDRegressor
            # 逻辑回归：预测方向 (UP/DOWN/NEUTRAL)
            self.clf = SGDClassifier(loss='log_loss', warm_start=True, penalty='l2', alpha=0.01)
            # 线性回归：预测数值 (bps)
            self.reg = SGDRegressor(warm_start=True, penalty='l2', alpha=0.01)
            self.has_model = True
        except ImportError:
            self.has_model = False
            print("[Predictor] Scikit-learn not found. ML mode disabled.")

    def _update_scaler(self, x: np.ndarray):
        """在线更新特征的均值和标准差 (Welford's Algorithm)"""
        self.n_samples += 1
        last_means = self.means.copy()
        self.means += (x - self.means) / self.n_samples
        # 简化版在线方差估计
        if self.n_samples > 1:
            self.stds = np.sqrt(((self.stds**2 * (self.n_samples-1)) + (x - last_means)*(x - self.means)) / self.n_samples)
            self.stds = np.clip(self.stds, 1e-4, 1e6) # 防止除零

    def _standardize(self, x: List[float]) -> np.ndarray:
        x_arr = np.array(x)
        self._update_scaler(x_arr)
        return (x_arr - self.means) / self.stds

    def add_tick(self, features: List[float], mid_price: float, now: float):
        """
        每一帧更新：
        1. 将当前特征和价格入队
        2. 检查队列头部，如果时间差超过 horizon_sec，取出作为训练样本
        """
        if not self.enabled or not self.has_model:
            return

        # 1. 缓存当前状态
        scaled_features = self._standardize(features)
        self.training_buffer.append({
            "ts": now,
            "x": scaled_features,
            "price": mid_price
        })

        # 2. 尝试学习（回溯历史）
        # 检查队列中是否有样本已经观察到了“未来的结果”
        while self.training_buffer and (now - self.training_buffer[0]["ts"] >= self.horizon_sec):
            past_sample = self.training_buffer.popleft()
            
            # 计算真实收益率 (Label)
            # y_real = (未来价格 - 过去价格) / 过去价格 * 10000
            y_real_bps = (mid_price / past_sample["price"] - 1.0) * 10000
            
            # 分类标签：1(涨), -1(跌), 0(震荡)
            # 门槛：1.5bps (过滤噪音)
            if y_real_bps > 1.5: label = 1
            elif y_real_bps < -1.5: label = -1
            else: label = 0
            
            # 在线训练模型
            X = past_sample["x"].reshape(1, -1)
            try:
                self.clf.partial_fit(X, [label], classes=[-1, 0, 1])
                self.reg.partial_fit(X, [y_real_bps])
                self.trained = True
            except:
                pass

    def predict(self, features: List[float], now: float) -> Optional[MLPrediction]:
        """执行推理"""
        if not self.enabled:
            return None
        
        # 如果模型没准备好，返回中性预测
        if not self.trained or not self.has_model:
            return MLPrediction(0.5, 0.0, 0.0, "NEUTRAL", now)

        try:
            X = ((np.array(features) - self.means) / self.stds).reshape(1, -1)
            
            # 1. 预测方向概率
            probs = self.clf.predict_proba(X)[0] # [-1, 0, 1] 对应的概率
            idx = np.argmax(probs)
            p_max = probs[idx]
            
            # 2. 映射方向
            classes = [-1, 0, 1]
            pred_class = classes[idx]
            direction = "UP" if pred_class == 1 else ("DOWN" if pred_class == -1 else "NEUTRAL")
            
            # 3. 预测具体收益率数值
            pred_bps = self.reg.predict(X)[0]
            
            return MLPrediction(
                p_trend=p_max,
                predicted_bps=pred_bps,
                momentum_strength=abs(p_max - 0.33), # 偏离均等分布的程度
                direction=direction,
                timestamp=now
            )
        except:
            return MLPrediction(0.5, 0.0, 0.0, "NEUTRAL", now)

    def get_weights(self) -> List[float]:
        """暴露系数供 UI 展示 [OrderFlow, Momentum, Depth]"""
        if not self.trained or not self.has_model:
            return [0.0, 0.0, 0.0]
        try:
            # 返回分类器对“上涨”类别的系数
            # self.clf.coef_ 形状是 (n_classes, n_features)
            # classes 为 [-1, 0, 1]，所以索引 2 是上涨类别的权重
            return self.clf.coef_[2].tolist()
        except:
            return [0.0, 0.0, 0.0]