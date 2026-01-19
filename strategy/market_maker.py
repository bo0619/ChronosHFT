# file: strategy/market_maker.py

from .base import StrategyTemplate
from event.type import OrderBook, TradeData, PositionData

class MarketMakerStrategy(StrategyTemplate):
    def __init__(self, engine, gateway, risk_manager):
        super().__init__(engine, gateway, risk_manager, "ActiveMM")
        self.spread = 10.0 
        self.vol = 0.002
        self.skew_factor = 50.0
        
        # 记录我们当前想挂的价格，如果市场变了，就撤单重挂
        self.target_bid_price = 0.0
        self.target_ask_price = 0.0

    def on_orderbook(self, ob: OrderBook):
        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 == 0: return
        
        mid_price = (bid_1 + ask_1) / 2.0
        net_pos = self.long_pos - abs(self.short_pos)
        reservation_price = mid_price - (net_pos * self.skew_factor)
        
        new_bid = int(reservation_price - self.spread / 2)
        new_ask = int(reservation_price + self.spread / 2)
        
        # --- 核心重挂逻辑 ---
        
        # 1. 检查买单是否需要重挂
        # 如果新价格与旧价格偏差超过阈值(比如2块钱)，或者当前没挂单
        if abs(new_bid - self.target_bid_price) > 2:
            # 撤销所有旧的买单 (这里简化，实际应该精准撤销特定的单)
            self._cancel_side("BUY")
            
            # 挂新单
            self.buy(ob.symbol, new_bid, self.vol)
            self.target_bid_price = new_bid
            
        # 2. 检查卖单是否需要重挂
        if abs(new_ask - self.target_ask_price) > 2:
            self._cancel_side("SELL")
            self.sell(ob.symbol, new_ask, self.vol)
            self.target_ask_price = new_ask

    def _cancel_side(self, side_str):
        """辅助函数：撤销指定方向的所有单子"""
        for oid, req in list(self.active_orders.items()):
            is_buy = (req.direction == "LONG" and req.action == "OPEN") or \
                     (req.direction == "SHORT" and req.action == "CLOSE")
            
            if side_str == "BUY" and is_buy:
                self.cancel_order(oid)
            elif side_str == "SELL" and not is_buy:
                self.cancel_order(oid)

    def on_trade(self, trade: TradeData):
        self.log(f"成交: {trade.direction} {trade.action} @ {trade.price}")# file: strategy/market_maker.py

from .base import StrategyTemplate
from event.type import OrderBook, TradeData, PositionData

class MarketMakerStrategy(StrategyTemplate):
    def __init__(self, engine, gateway, risk_manager):
        super().__init__(engine, gateway, risk_manager, "ActiveMM")
        self.spread = 10.0 
        self.vol = 0.002
        self.skew_factor = 50.0
        
        # 记录我们当前想挂的价格，如果市场变了，就撤单重挂
        self.target_bid_price = 0.0
        self.target_ask_price = 0.0

    def on_orderbook(self, ob: OrderBook):
        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 == 0: return
        
        mid_price = (bid_1 + ask_1) / 2.0
        net_pos = self.long_pos - abs(self.short_pos)
        reservation_price = mid_price - (net_pos * self.skew_factor)
        
        new_bid = int(reservation_price - self.spread / 2)
        new_ask = int(reservation_price + self.spread / 2)
        
        # --- 核心重挂逻辑 ---
        
        # 1. 检查买单是否需要重挂
        # 如果新价格与旧价格偏差超过阈值(比如2块钱)，或者当前没挂单
        if abs(new_bid - self.target_bid_price) > 2:
            # 撤销所有旧的买单 (这里简化，实际应该精准撤销特定的单)
            self._cancel_side("BUY")
            
            # 挂新单
            self.buy(ob.symbol, new_bid, self.vol)
            self.target_bid_price = new_bid
            
        # 2. 检查卖单是否需要重挂
        if abs(new_ask - self.target_ask_price) > 2:
            self._cancel_side("SELL")
            self.sell(ob.symbol, new_ask, self.vol)
            self.target_ask_price = new_ask

    def _cancel_side(self, side_str):
        """辅助函数：撤销指定方向的所有单子"""
        for oid, req in list(self.active_orders.items()):
            is_buy = (req.direction == "LONG" and req.action == "OPEN") or \
                     (req.direction == "SHORT" and req.action == "CLOSE")
            
            if side_str == "BUY" and is_buy:
                self.cancel_order(oid)
            elif side_str == "SELL" and not is_buy:
                self.cancel_order(oid)

    def on_trade(self, trade: TradeData):
        self.log(f"成交: {trade.direction} {trade.action} @ {trade.price}")