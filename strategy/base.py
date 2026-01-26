# file: strategy/base.py

import time
from event.type import OrderBook, TradeData, OrderData, PositionData, OrderRequest, CancelRequest, Event, EVENT_LOG
from event.type import Side, OrderIntent, OrderStateSnapshot, OrderStatus
from event.type import Status_ALLTRADED, Status_CANCELLED, Status_REJECTED

class StrategyTemplate:
    # [修改] 参数变更为 engine, oms, name
    def __init__(self, engine, oms, name="Strategy"):
        self.engine = engine
        self.oms = oms # [核心] 现在策略只认识 OMS
        self.name = name
        
        self.pos = 0.0
        self.active_orders = {} 
        self.orders_cancelling = set()

    def on_orderbook(self, orderbook: OrderBook): raise NotImplementedError
    def on_trade(self, trade: TradeData): pass
    
    def on_order(self, order: OrderData):
        """
        注意：现在的 OrderData 来自 OMS 的 standardized update
        """
        # 维护本地 active_orders 列表，用于撤单
        if order.status in [Status_ALLTRADED, Status_CANCELLED, Status_REJECTED]:
            if order.order_id in self.active_orders:
                del self.active_orders[order.order_id]
            if order.order_id in self.orders_cancelling:
                self.orders_cancelling.remove(order.order_id)
    
    def on_position(self, pos: PositionData):
        self.pos = pos.volume

    def log(self, msg): self.engine.put(Event(EVENT_LOG, f"[{self.name}] {msg}"))

    # --- 核心下单逻辑 (对接 OMS) ---
    def send_order_safe(self, symbol, side, price, volume):
        # 构造意图 Intent
        intent = OrderIntent(
            strategy_id=self.name,
            symbol=symbol,
            side=side,
            price=price,
            volume=volume,
            order_type="LIMIT",
            time_in_force="GTC"
        )
        
        # [修改] 调用 OMS 发单
        # OMS 内部会进行 Validator检查、Account保证金检查、Exposure敞口检查
        client_oid = self.oms.submit_order(intent)
        
        if client_oid:
            # 记录本地映射 (client_oid -> Intent/Request)
            # 为了兼容旧逻辑，这里存 intent
            self.active_orders[client_oid] = intent
            
        return client_oid

    def buy(self, symbol, price, volume):
        return self.send_order_safe(symbol, Side.BUY, price, volume)

    def sell(self, symbol, price, volume):
        return self.send_order_safe(symbol, Side.SELL, price, volume)

    # --- 撤单逻辑 ---
    def cancel_order(self, client_oid: str):
        if client_oid not in self.active_orders: return
        if client_oid in self.orders_cancelling: return 
        
        self.orders_cancelling.add(client_oid)
        
        # [修改] 调用 OMS 撤单
        self.oms.cancel_order(client_oid)

    def cancel_all(self):
        # 复制 keys 防止迭代时修改
        for oid in list(self.active_orders.keys()):
            self.cancel_order(oid)