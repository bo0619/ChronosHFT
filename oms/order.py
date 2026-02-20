# file: oms/order.py

import time
from event.type import OrderIntent, OrderStatus, Side, OrderStateSnapshot, ExecutionPolicy

class Order:
    """
    [Updated] 支持 RPI 属性的订单对象
    """
    def __init__(self, client_oid: str, intent: OrderIntent):
        self.client_oid = client_oid
        self.intent = intent
        
        # 核心状态
        self.exchange_oid = "" 
        self.status = OrderStatus.CREATED
        
        # 属性缓存 (方便快速访问，不用每次都查 intent)
        self.is_rpi = intent.is_rpi or (intent.policy == ExecutionPolicy.RPI)
        
        # 成交统计
        self.filled_volume = 0.0
        self.avg_price = 0.0
        self.cumulative_cost = 0.0 
        
        # 时间戳
        self.created_at = time.time()
        self.updated_at = time.time()
        
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
            update_time=self.updated_at,
            is_rpi=self.is_rpi # [NEW] 传递下去
        )

    # --- 状态流转方法 ---
    def mark_submitting(self):
        self.status = OrderStatus.SUBMITTING
        self.updated_at = time.time()

    def mark_new(self, exchange_oid):
        self.status = OrderStatus.NEW
        if exchange_oid:
            self.exchange_oid = exchange_oid
        self.updated_at = time.time()

    def add_fill(self, fill_qty, fill_price):
        if fill_qty <= 0: return
        self.cumulative_cost += fill_qty * fill_price
        self.filled_volume += fill_qty
        if self.filled_volume > 0:
            self.avg_price = self.cumulative_cost / self.filled_volume
            
        if self.filled_volume >= self.intent.volume - 1e-8:
            self.status = OrderStatus.FILLED
        else:
            self.status = OrderStatus.PARTIALLY_FILLED
        self.updated_at = time.time()

    def mark_cancelled(self):
        if self.status != OrderStatus.FILLED:
            self.status = OrderStatus.CANCELLED
            self.updated_at = time.time()

    def mark_rejected(self, reason=""):
        self.status = OrderStatus.REJECTED
        self.error_msg = reason
        self.updated_at = time.time()