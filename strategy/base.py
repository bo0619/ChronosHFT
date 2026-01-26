# file: strategy/base.py

import time
from event.type import OrderBook, TradeData, OrderData, PositionData, OrderRequest, CancelRequest, Event, EVENT_LOG
from event.type import Side, OrderIntent, OrderStatus, ExecutionPolicy
from event.type import EVENT_ORDER_SUBMITTED, OrderSubmitted
from event.type import Status_ALLTRADED, Status_CANCELLED, Status_REJECTED
from data.ref_data import ref_data_manager

class StrategyTemplate:
    def __init__(self, engine, oms, name="Strategy"):
        self.engine = engine
        self.oms = oms
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

    # --- 核心下单逻辑 ---
    
    def send_order_safe(self, symbol, side, price, volume, **kwargs):
        # 提取参数
        is_rpi = kwargs.get("is_rpi", False)
        
        # [修复] RPI 必须强制开启 PostOnly
        is_post_only = kwargs.get("is_post_only", False)
        if is_rpi:
            is_post_only = True

        # 1. 构造意图
        intent = OrderIntent(
            strategy_id=self.name,
            symbol=symbol,
            side=side,
            price=price,
            volume=volume,
            order_type="LIMIT",
            time_in_force="GTC",
            is_rpi=is_rpi,           # 传递 RPI
            is_post_only=is_post_only, # 传递强制后的 PostOnly
            policy=kwargs.get("policy", ExecutionPolicy.PASSIVE)
        )
        
        # 2. 规整化
        intent.price = ref_data_manager.round_price(symbol, intent.price)
        intent.volume = ref_data_manager.round_qty(symbol, intent.volume)
        
        # 3. 最小名义价值检查
        info = ref_data_manager.get_info(symbol)
        if info:
            notional = intent.price * intent.volume
            min_notional = max(info.min_notional, 5.0) 
            if notional < min_notional:
                return None

        # 4. 调用 OMS 发单
        # OMS 内部会进行 Validator检查、Account保证金检查、Exposure敞口检查
        client_oid = self.oms.submit_order(intent)
        
        if client_oid:
            self.active_orders[client_oid] = intent
            
        return client_oid

    # 基础指令
    def buy(self, symbol, price, volume, **kwargs):
        return self.send_order_safe(symbol, Side.BUY, price, volume, **kwargs)

    def sell(self, symbol, price, volume, **kwargs):
        return self.send_order_safe(symbol, Side.SELL, price, volume, **kwargs)

    # 智能指令
    def entry_long(self, symbol, price, volume, **kwargs):
        return self.send_order_safe(symbol, Side.BUY, price, volume, **kwargs)

    def exit_long(self, symbol, price, volume, **kwargs):
        return self.send_order_safe(symbol, Side.SELL, price, volume, **kwargs)

    def entry_short(self, symbol, price, volume, **kwargs):
        return self.send_order_safe(symbol, Side.SELL, price, volume, **kwargs)

    def exit_short(self, symbol, price, volume, **kwargs):
        return self.send_order_safe(symbol, Side.BUY, price, volume, **kwargs)

    # --- 撤单逻辑 ---
    def cancel_order(self, client_oid: str):
        if client_oid not in self.active_orders: return
        if client_oid in self.orders_cancelling: return 
        
        self.orders_cancelling.add(client_oid)
        self.oms.cancel_order(client_oid)

    def cancel_all(self):
        for oid in list(self.active_orders.keys()):
            self.cancel_order(oid)