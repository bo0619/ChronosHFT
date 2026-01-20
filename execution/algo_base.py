# file: execution/algo_base.py

from event.type import OrderRequest, OrderData, TradeData, OrderBook
from event.type import Status_ALLTRADED, Status_CANCELLED, Status_REJECTED

class AlgoTemplate:
    """
    执行算法基类
    负责管理一组子订单 (Child Orders) 来完成一个大目标 (Parent Order)
    """
    def __init__(self, algo_id, symbol, direction, total_vol, engine, strategy):
        self.algo_id = algo_id
        self.symbol = symbol
        self.direction = direction # BUY/SELL
        self.total_vol = total_vol
        
        self.engine = engine
        self.strategy = strategy # 引用策略以便调用 buy/sell/cancel
        
        self.traded_vol = 0.0
        self.active_orders = {} # child_order_id -> vol
        self.finished = False

    def start(self):
        """启动算法"""
        pass

    def stop(self):
        """停止算法并撤销所有子单"""
        self.finished = True
        self.cancel_all()

    def on_tick(self, ob: OrderBook):
        """行情驱动"""
        pass

    def on_order(self, order: OrderData):
        """订单状态更新"""
        if order.order_id in self.active_orders:
            if order.status in [Status_ALLTRADED, Status_CANCELLED, Status_REJECTED]:
                del self.active_orders[order.order_id]
                
            if order.status == Status_ALLTRADED:
                self.traded_vol += order.volume
                if self.traded_vol >= self.total_vol - 1e-8:
                    self.finished = True
                    print(f"[{self.algo_id}] 算法执行完毕: {self.traded_vol}/{self.total_vol}")

    def cancel_all(self):
        for oid in list(self.active_orders.keys()):
            self.strategy.cancel_order(oid)