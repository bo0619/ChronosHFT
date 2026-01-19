# file: strategy/base.py

from event.type import OrderBook, TradeData, OrderData, PositionData, OrderRequest, CancelRequest, Event, EVENT_LOG
from event.type import Direction_LONG, Direction_SHORT, Action_OPEN, Action_CLOSE
from event.type import Status_ALLTRADED, Status_CANCELLED, Status_REJECTED

class StrategyTemplate:
    def __init__(self, engine, gateway, risk_manager, name="Strategy"):
        self.engine = engine
        self.gateway = gateway
        self.risk_manager = risk_manager
        self.name = name
        
        self.long_pos = 0.0
        self.short_pos = 0.0
        
        # [NEW] 自动维护活跃订单字典 {order_id: original_req}
        self.active_orders = {} 

    def on_orderbook(self, orderbook: OrderBook): raise NotImplementedError
    def on_trade(self, trade: TradeData): pass
    
    def on_order(self, order: OrderData):
        """
        基础订单状态维护
        """
        # 如果订单结束，从活跃列表中移除
        if order.status in [Status_ALLTRADED, Status_CANCELLED, Status_REJECTED]:
            if order.order_id in self.active_orders:
                del self.active_orders[order.order_id]
    
    def on_position(self, pos: PositionData):
        if pos.direction == Direction_LONG: self.long_pos = pos.volume
        else: self.short_pos = pos.volume

    def log(self, msg): self.engine.put(Event(EVENT_LOG, f"[{self.name}] {msg}"))

    # --- 下单 ---
    def send_order_safe(self, req: OrderRequest):
        if not self.risk_manager.check_order(req): return None
        order_id = self.gateway.send_order(req)
        if order_id:
            self.active_orders[order_id] = req # 记录
        return order_id

    def buy(self, symbol, price, volume): return self.send_order_safe(OrderRequest(symbol, price, volume, Direction_LONG, Action_OPEN))
    def sell(self, symbol, price, volume): return self.send_order_safe(OrderRequest(symbol, price, volume, Direction_LONG, Action_CLOSE))
    def short(self, symbol, price, volume): return self.send_order_safe(OrderRequest(symbol, price, volume, Direction_SHORT, Action_OPEN))
    def cover(self, symbol, price, volume): return self.send_order_safe(OrderRequest(symbol, price, volume, Direction_SHORT, Action_CLOSE))

    # --- [NEW] 撤单 ---
    def cancel_order(self, order_id: str):
        """撤销指定订单"""
        # 检查是否是我们策略发出的单
        if order_id not in self.active_orders:
            return
        
        req = self.active_orders[order_id]
        cancel_req = CancelRequest(req.symbol, order_id)
        self.gateway.cancel_order(cancel_req)

    def cancel_all(self):
        """撤销本策略所有活跃订单"""
        if not self.active_orders: return
        
        # 复制 keys 列表，防止遍历时字典改变
        for order_id in list(self.active_orders.keys()):
            self.cancel_order(order_id)