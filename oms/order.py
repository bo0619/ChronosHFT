# file: oms/order.py

import time
from event.type import OrderIntent, OrderStatus, Side, OrderStateSnapshot

class Order:
    """
    有状态的订单对象。
    只有 OMS 能修改它的状态。
    """
    def __init__(self, client_oid: str, intent: OrderIntent):
        self.client_oid = client_oid
        self.intent = intent
        
        # 核心状态
        self.exchange_oid = "" # 交易所确认后填入
        self.status = OrderStatus.CREATED
        
        # 成交统计
        self.filled_volume = 0.0
        self.avg_price = 0.0
        self.cumulative_cost = 0.0 # 用于计算均价
        
        # 时间戳
        self.created_at = time.time()
        self.updated_at = time.time()
        
        # 错误信息
        self.error_msg = ""

    def is_active(self):
        return self.status in [
            OrderStatus.SUBMITTING, 
            OrderStatus.PENDING_ACK, 
            OrderStatus.NEW, 
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.CANCELLING
        ]

    def to_snapshot(self) -> OrderStateSnapshot:
        return OrderStateSnapshot(
            client_oid=self.client_oid,
            exchange_oid=self.exchange_oid,
            symbol=self.intent.symbol,
            status=self.status,
            price=self.intent.price,
            volume=self.intent.volume,
            filled_volume=self.filled_volume,
            avg_price=self.avg_price,
            update_time=self.updated_at
        )

    # --- 状态流转方法 (由 OMS 调用) ---

    def mark_submitting(self):
        self.status = OrderStatus.SUBMITTING
        self.updated_at = time.time()

    def mark_new(self, exchange_oid):
        self.status = OrderStatus.NEW
        self.exchange_oid = exchange_oid
        self.updated_at = time.time()

    def add_fill(self, fill_qty, fill_price):
        if fill_qty <= 0: return
        
        self.cumulative_cost += fill_qty * fill_price
        self.filled_volume += fill_qty
        
        # 防止精度误差导致除零
        if self.filled_volume > 0:
            self.avg_price = self.cumulative_cost / self.filled_volume
            
        # 状态判断
        if self.filled_volume >= self.intent.volume - 1e-8:
            self.status = OrderStatus.FILLED
        else:
            self.status = OrderStatus.PARTIALLY_FILLED
        
        self.updated_at = time.time()

    def mark_cancelled(self):
        # 只有未完全成交的单子才能变 Cancelled
        if self.status != OrderStatus.FILLED:
            self.status = OrderStatus.CANCELLED
            self.updated_at = time.time()

    def mark_rejected(self, reason=""):
        self.status = OrderStatus.REJECTED
        self.error_msg = reason
        self.updated_at = time.time()