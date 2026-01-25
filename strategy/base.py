# file: strategy/base.py

import time
# [修复] 移除 Side_BUY, Side_SELL，改为导入 Side
from event.type import OrderBook, TradeData, OrderData, PositionData, OrderRequest, CancelRequest, Event, EVENT_LOG
from event.type import Side, EVENT_ORDER_SUBMITTED, OrderSubmitted
from event.type import Status_ALLTRADED, Status_CANCELLED, Status_REJECTED
from data.ref_data import ref_data_manager

class StrategyTemplate:
    def __init__(self, engine, gateway, risk_manager, name="Strategy"):
        self.engine = engine
        self.gateway = gateway
        self.risk_manager = risk_manager
        self.name = name
        
        self.pos = 0.0
        self.active_orders = {} 
        self.orders_cancelling = set()

    def on_orderbook(self, orderbook: OrderBook): raise NotImplementedError
    def on_trade(self, trade: TradeData): pass
    
    def on_order(self, order: OrderData):
        if order.status in [Status_ALLTRADED, Status_CANCELLED, Status_REJECTED]:
            if order.order_id in self.active_orders:
                del self.active_orders[order.order_id]
            if order.order_id in self.orders_cancelling:
                self.orders_cancelling.remove(order.order_id)
    
    def on_position(self, pos: PositionData):
        self.pos = pos.volume

    def log(self, msg): self.engine.put(Event(EVENT_LOG, f"[{self.name}] {msg}"))

    def send_order_safe(self, req: OrderRequest):
        # 1. 规整化
        req.price = ref_data_manager.round_price(req.symbol, req.price)
        req.volume = ref_data_manager.round_qty(req.symbol, req.volume)
        
        # 2. 最小名义价值
        info = ref_data_manager.get_info(req.symbol)
        if info:
            notional = req.price * req.volume
            min_notional = max(info.min_notional, 5.0) 
            if notional < min_notional:
                return None

        # 3. 风控检查
        if not self.risk_manager.check_order(req): 
            return None
            
        # 4. 发送给网关
        order_id = self.gateway.send_order(req)
        
        if order_id:
            # 5. 发送成功，广播通知
            event_data = OrderSubmitted(req, order_id, time.time())
            self.engine.put(Event(EVENT_ORDER_SUBMITTED, event_data))
            
            self.active_orders[order_id] = req 
            
        return order_id

    # [修复] 使用 Side.BUY 和 Side.SELL
    def buy(self, symbol, price, volume):
        return self.send_order_safe(OrderRequest(symbol, price, volume, Side.BUY))

    def sell(self, symbol, price, volume):
        return self.send_order_safe(OrderRequest(symbol, price, volume, Side.SELL))

    def cancel_order(self, order_id: str):
        if order_id not in self.active_orders: return
        if order_id in self.orders_cancelling: return 
        
        self.orders_cancelling.add(order_id)
        
        req = self.active_orders[order_id]
        cancel_req = CancelRequest(req.symbol, order_id)
        self.gateway.cancel_order(cancel_req)

    def cancel_all(self):
        if not self.active_orders: return
        for order_id in list(self.active_orders.keys()):
            self.cancel_order(order_id)