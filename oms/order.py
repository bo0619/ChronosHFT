# file: oms/order.py

import time
from event.type import OrderIntent, OrderStatus, OrderStateSnapshot

class Order:
    def __init__(self, client_oid: str, intent: OrderIntent):
        self.client_oid = client_oid
        self.intent = intent
        
        self.exchange_oid = "" 
        self.status = OrderStatus.CREATED
        
        self.filled_volume = 0.0
        self.avg_price = 0.0
        self.cumulative_cost = 0.0 
        
        self.created_at = time.time()
        self.updated_at = time.time()
        self.error_msg = ""

    def is_active(self):
        return self.status in

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