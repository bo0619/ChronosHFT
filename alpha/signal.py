# file: alpha/signal.py

import os
# import lightgbm as lgb  # 实际使用时解开
import numpy as np

class SignalGenerator:
    def predict(self, feature_engine):
        raise NotImplementedError

class MockSignal(SignalGenerator):
    """
    [模拟] 线性加权信号生成器
    用于在没有 ML 模型时测试流程
    Signal = w1 * Imbalance + w2 * OFI
    """
    def __init__(self):
        # 手动赋予的权重 (专家经验)
        self.weights = {
            "BookImbalance": 5.0,  # 静态失衡权重
            "OFI": 2.0,            # 动态流权重
            "Volatility": 0.0      # 波动率暂不影响方向，只影响Spread(策略层处理)
        }

    def predict(self, feature_engine):
        features = feature_engine.get_feature_dict()
        
        signal = 0.0
        signal += features.get("BookImbalance", 0) * self.weights["BookImbalance"]
        signal += features.get("OFI", 0) * self.weights["OFI"]
        
        # 限制信号范围 [-10, 10]
        return max(-10, min(10, signal))

# class LightGBMSignal(SignalGenerator):
#     """
#     [真实] LightGBM 推理包装器
#     """
#     def __init__(self, model_path):
#         import lightgbm as lgb
#         if not os.path.exists(model_path):
#             raise FileNotFoundError(f"Model not found: {model_path}")
#         self.model = lgb.Booster(model_file=model_path)

#     def predict(self, feature_engine):
#         # 获取特征向量 (注意顺序必须和训练时一致)
#         # 例如: [Imbalance, OFI, Volatility]
#         vec = feature_engine.get_feature_vector()
        
#         # 预测
#         # LightGBM predict 返回的是列表，取第一个值
#         # 假设模型预测的是未来 10秒 的价格 Returns 或 Skew
#         pred = self.model.predict([vec])[0]
#         return pred