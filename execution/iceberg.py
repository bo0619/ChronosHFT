# file: execution/iceberg.py

from .algo_base import AlgoTemplate

class IcebergAlgo(AlgoTemplate):
    def __init__(self, algo_id, symbol, direction, total_vol, engine, strategy, visible_vol, price_limit):
        super().__init__(algo_id, symbol, direction, total_vol, engine, strategy)
        self.visible_vol = visible_vol # 每次暴露多少
        self.price_limit = price_limit # 价格上限/下限

    def start(self):
        self.replenish()

    def on_order(self, order):
        super().on_order(order)
        # 如果子单完全成交，且还有剩余量，补单
        if order.status == "ALLTRADED" and not self.finished:
            self.replenish()

    def replenish(self):
        if self.finished: return
        
        left_vol = self.total_vol - self.traded_vol
        # 不能超过剩余量，也不能超过每次可见量
        order_vol = min(left_vol, self.visible_vol)
        
        if order_vol <= 0: return
        
        # 简单实现：挂 Limit 单在限价位
        # 进阶实现：可以挂在买一价，或者 Pegging
        if self.direction == "BUY":
            oid = self.strategy.buy(self.symbol, self.price_limit, order_vol)
        else:
            oid = self.strategy.sell(self.symbol, self.price_limit, order_vol)
            
        if oid:
            self.active_orders[oid] = order_vol