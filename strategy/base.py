# file: strategy/base.py

import time
from event.type import OrderBook, TradeData, OrderStateSnapshot, PositionData, CancelRequest, Event, EVENT_LOG
from event.type import Side, OrderIntent, OrderStatus
from data.ref_data import ref_data_manager

class StrategyTemplate:
    """
    [最终版] 策略基类
    - 完全与 OMS 解耦
    - 不再直接依赖 Gateway 或 RiskManager
    """
    def __init__(self, engine, oms, name="Strategy"):
        self.engine = engine
        self.oms = oms # [核心] 策略只与 OMS 对话
        self.name = name
        
        self.pos = 0.0
        # client_oid -> OrderIntent
        self.active_orders = {} 
        self.orders_cancelling = set()

    def on_orderbook(self, orderbook: OrderBook): raise NotImplementedError
    def on_trade(self, trade: TradeData): pass
    
    def on_order(self, snapshot: OrderStateSnapshot):
        """
        处理 OMS 推送的订单状态快照
        """

        # 如果订单终结，从本地活跃列表中移除
        if snapshot.status in [OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED]:
            if snapshot.client_oid in self.active_orders:
                del self.active_orders[snapshot.client_oid]
            if snapshot.client_oid in self.orders_cancelling:
                self.orders_cancelling.remove(snapshot.client_oid)
    
    def on_position(self, pos: PositionData):
        self.pos = pos.volume

    def log(self, msg): self.engine.put(Event(EVENT_LOG, f"[{self.name}] {msg}"))

    # --- 核心下单逻辑 (发送意图给 OMS) ---
    def send_intent(self, intent: OrderIntent):
        """
        发送交易意图给 OMS
        """
        # 1. 规整化 (策略层负责意图的合理性)
        intent.price = ref_data_manager.round_price(intent.symbol, intent.price)
        intent.volume = ref_data_manager.round_qty(intent.symbol, intent.volume)
        
        # 2. 检查最小名义价值 (本地快速过滤)
        info = ref_data_manager.get_info(intent.symbol)
        if info:
            notional = intent.price * intent.volume
            min_notional = max(info.min_notional, 5.0) 
            if notional < min_notional:
                # self.log(f"Intent Rejected (Local): Notional {notional:.2f} < {min_notional}")
                return None

        # 3. 提交给 OMS (OMS 会进行最终风控)
        client_oid = self.oms.submit_order(intent)
        
        if client_oid:
            self.active_orders[client_oid] = intent
            
        return client_oid

    # --- 便捷指令 ---
    def entry_long(self, symbol, price, volume):
        intent = OrderIntent(self.name, symbol, Side.BUY, price, volume)
        return self.send_intent(intent)

    def exit_long(self, symbol, price, volume):
        intent = OrderIntent(self.name, symbol, Side.SELL, price, volume)
        return self.send_intent(intent)

    def entry_short(self, symbol, price, volume):
        intent = OrderIntent(self.name, symbol, Side.SELL, price, volume)
        return self.send_intent(intent)

    def exit_short(self, symbol, price, volume):
        intent = OrderIntent(self.name, symbol, Side.BUY, price, volume)
        return self.send_intent(intent)
        
    # [兼容] 旧的 buy/sell 接口
    def buy(self, symbol, price, volume): return self.entry_long(symbol, price, volume)
    def sell(self, symbol, price, volume): return self.entry_short(symbol, price, volume)

    # --- 撤单逻辑 (通过 OMS) ---
    def cancel_order(self, client_oid: str):
        """撤销指定订单"""
        if client_oid not in self.active_orders: return
        if client_oid in self.orders_cancelling: return 
        
        self.orders_cancelling.add(client_oid)
        self.oms.cancel_order(client_oid)

    def cancel_all(self, symbol: str):
        """撤销该币种所有订单"""
        # [修改] 直接调用 OMS 的 cancel_all
        self.oms.cancel_all_orders(symbol)
        
        # 暴力清空本地记录
        to_remove = [oid for oid, intent in self.active_orders.items() if intent.symbol == symbol]
        for oid in to_remove:
            del self.active_orders[oid]
            if oid in self.orders_cancelling:
                self.orders_cancelling.remove(oid)