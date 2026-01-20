# file: alpha/engine.py

from .factors import BookImbalance, OrderFlowImbalance, RealizedVolatility
from event.type import OrderBook, TradeData

class FeatureEngine:
    def __init__(self):
        # 注册需要的因子
        self.factors = [
            BookImbalance(),
            OrderFlowImbalance(),
            RealizedVolatility(window=50)
        ]
        # 缓存最新特征向量
        self.current_features = {}

    def on_orderbook(self, ob: OrderBook):
        """
        行情更新时，驱动所有因子计算
        """
        for factor in self.factors:
            factor.on_orderbook(ob)
            self.current_features[factor.name] = factor.value

    def on_trade(self, trade: TradeData):
        for factor in self.factors:
            factor.on_trade(trade)
            self.current_features[factor.name] = factor.value

    def get_feature_vector(self):
        """
        获取给 ML 模型输入的向量
        顺序必须固定！与训练时保持一致
        """
        return [f.value for f in self.factors]
    
    def get_feature_dict(self):
        return self.current_features