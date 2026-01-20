# file: strategy/base.py

from event.type import OrderBook, TradeData, OrderData, PositionData, OrderRequest, CancelRequest, Event, EVENT_LOG
from event.type import Side_BUY, Side_SELL
from event.type import Status_ALLTRADED, Status_CANCELLED, Status_REJECTED
from data.ref_data import ref_data_manager

class StrategyTemplate:
    def __init__(self, engine, gateway, risk_manager, name="Strategy"):
        self.engine = engine
        self.gateway = gateway
        self.risk_manager = risk_manager
        self.name = name
        
        # [NEW] 净持仓 (正多负空)
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

    # --- 下单 ---
    def send_order_safe(self, req: OrderRequest):
        req.price = ref_data_manager.round_price(req.symbol, req.price)
        req.volume = ref_data_manager.round_qty(req.symbol, req.volume)
        
        info = ref_data_manager.get_info(req.symbol)
        if info:
            notional = req.price * req.volume
            min_notional = max(info.min_notional, 5.0) 
            if notional < min_notional:
                return None

        if not self.risk_manager.check_order(req): 
            return None
            
        order_id = self.gateway.send_order(req)
        if order_id:
            self.active_orders[order_id] = req 
        return order_id

    # [NEW] 只有 Buy 和 Sell
    def buy(self, symbol, price, volume):
        return self.send_order_safe(OrderRequest(symbol, price, volume, Side_BUY))

    def sell(self, symbol, price, volume):
        return self.send_order_safe(OrderRequest(symbol, price, volume, Side_SELL))

    # --- 撤单 ---
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