# file: alpha/signal.py

import numpy as np
from collections import deque

class OnlineRidgePredictor:
    """
    单一尺度的在线岭回归模型 (RLS)
    """
    def __init__(self, num_features, lambda_reg=1.0):
        self.num_features = num_features
        # 权重向量 beta
        self.w = np.zeros((num_features, 1))
        # 协方差矩阵的逆 (P matrix)
        self.P = np.eye(num_features) / lambda_reg
        
        # 内部状态缓存 (用于 update_and_predict)
        self.last_features = None
        self.last_mid = None

    def update(self, features, y_true):
        """核心学习步: Recursive Least Squares 更新"""
        X = np.array(features).reshape(-1, 1)
        
        # K = P * X / (1 + X.T * P * X)
        num = self.P @ X
        den = 1.0 + (X.T @ self.P @ X)[0, 0]
        K = num / den
        
        # Error = y - X.T * w
        err = y_true - (X.T @ self.w)[0, 0]
        
        # w = w + K * Error
        self.w += K * err
        
        # P = (I - K * X.T) * P
        self.P = (np.eye(self.num_features) - K @ X.T) @ self.P

    def predict(self, features):
        """核心预测步"""
        X = np.array(features).reshape(-1, 1)
        pred = (X.T @ self.w)[0, 0]
        # 钳制异常值 (防止初期波动过大)
        return max(-20.0, min(20.0, pred))

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


class MultiHorizonPredictor:
    """
    多尺度预测器 (包装器)
    同时维护 Short/Mid/Long 三个周期的预测模型
    """
    def __init__(self, num_features=9):
        # 定义三个尺度
        self.horizons = {
            "short": 1,    # 1个tick后
            "mid":   10,   # 10个tick后
            "long":  60    # 60个tick后
        }
        
        # 实例化三个独立的 OnlineRidgePredictor
        self.models = {
            h: OnlineRidgePredictor(num_features) for h in self.horizons
        }
        
        # 历史缓冲区: (timestamp, mid_price, features_vector)
        self.history_buffer = deque(maxlen=100) 

    def update_and_predict(self, features: list, current_mid: float, timestamp: float):
        """
        返回: 字典 {"short": bps, "mid": bps, "long": bps}
        """
        results = {"short": 0.0, "mid": 0.0, "long": 0.0}
        
        if current_mid <= 0: return results
        
        # 1. 存入当前快照
        self.history_buffer.append({
            "ts": timestamp,
            "price": current_mid,
            "feats": features
        })
        
        # 2. 训练 (回溯历史)
        current_idx = len(self.history_buffer) - 1
        
        for name, horizon in self.horizons.items():
            past_idx = current_idx - horizon
            if past_idx >= 0:
                past_data = self.history_buffer[past_idx]
                
                # 计算多尺度 Label: (Price_Now - Price_Past) / Price_Past
                y_true = (current_mid / past_data["price"] - 1.0) * 10000
                
                # 训练对应的模型
                self.models[name].update(past_data["feats"], y_true)
        
        # 3. 预测
        for name, model in self.models.items():
            results[name] = model.predict(features)
            
        return results