# file: alpha/signal.py

import os
import numpy as np

class OnlineRidgePredictor:
    """
    冷启动神器：在线岭回归 (Online Ridge Regression)
    利用 Recursive Least Squares (RLS) 实时更新特征权重。
    预测目标：下一秒的价格变化方向 (Return)
    """
    def __init__(self, num_features=9, lambda_reg=1.0):
        self.num_features = num_features
        # 权重向量 beta
        self.w = np.zeros((num_features, 1)) 
        # 协方差矩阵的逆 (P matrix in RLS)
        self.P = np.eye(num_features) / lambda_reg 
        
        self.last_features = None
        self.last_mid = None

    def update_and_predict(self, current_features: list, current_mid: float):
        """
        1. 用上一秒的特征和当前的价格变化，更新模型权重 (Learn)
        2. 用当前的特征，预测下一秒的价格变化 (Predict)
        """
        X = np.array(current_features).reshape(-1, 1)
        
        # 1. Learn
        if self.last_features is not None and self.last_mid is not None and current_mid > 0:
            X_prev = np.array(self.last_features).reshape(-1, 1)
            y_true = (current_mid / self.last_mid - 1.0) * 10000 
            
            num = self.P @ X_prev
            den = 1.0 + (X_prev.T @ self.P @ X_prev)[0, 0]
            K = num / den
            
            err = y_true - (X_prev.T @ self.w)[0, 0]
            
            self.w += K * err
            self.P = (np.eye(self.num_features) - K @ X_prev.T) @ self.P

        # Update State
        self.last_features = current_features
        self.last_mid = current_mid
        
        # 2. Predict
        pred_bps = (X.T @ self.w)[0, 0]
        return max(-5.0, min(5.0, pred_bps))