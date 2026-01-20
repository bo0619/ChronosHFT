# file: strategy/market_maker.py

import json
from .base import StrategyTemplate
from event.type import OrderBook, TradeData
from data.ref_data import ref_data_manager

class MarketMakerStrategy(StrategyTemplate):
    def __init__(self, engine, gateway, risk_manager):
        super().__init__(engine, gateway, risk_manager, "OneWayMM")
        
        self.config = self._load_strategy_config()
        self.lot_multiplier = self.config.get("lot_multiplier", 1.0)
        self.spread_ratio = self.config.get("spread_ratio", 0.0005)
        self.skew_factor_usdt = self.config.get("skew_factor_usdt", 50.0)
        self.max_pos_usdt = self.config.get("max_pos_usdt", 2000.0)
        
        self.target_bid_price = 0.0
        self.target_ask_price = 0.0
        
        print(f"[{self.name}] 策略已启动 (单向持仓模式)")

    def _load_strategy_config(self):
        try:
            with open("config.json", "r") as f: return json.load(f).get("strategy", {})
        except: return {}

    def _calculate_safe_vol(self, symbol, price):
        info = ref_data_manager.get_info(symbol)
        if not info: return 0.0
        safe_min = max(5.0, info.min_notional) * 1.1
        qty_val = safe_min / price
        target = max(info.min_qty, qty_val) * self.lot_multiplier
        return ref_data_manager.round_qty(symbol, target)

    def on_orderbook(self, ob: OrderBook):
        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 == 0: return
        
        mid_price = (bid_1 + ask_1) / 2.0
        order_vol = self._calculate_safe_vol(ob.symbol, mid_price)
        if order_vol <= 0: return

        # Skew 计算 (self.pos 已经是带符号的净持仓)
        pos_value = self.pos * mid_price
        skew_val = (pos_value / 1000.0) * (self.skew_factor_usdt / 1000.0) * mid_price 
        reservation_price = mid_price - skew_val
        
        spread = mid_price * self.spread_ratio
        new_bid = reservation_price - spread / 2
        new_ask = reservation_price + spread / 2
        
        price_threshold = mid_price * 0.0005
        
        # 挂买单 (Buy)
        if abs(new_bid - self.target_bid_price) > price_threshold:
            self._cancel_side("BUY")
            if pos_value < self.max_pos_usdt:
                new_bid = ref_data_manager.round_price(ob.symbol, new_bid)
                self.buy(ob.symbol, new_bid, order_vol)
                self.target_bid_price = new_bid
            
        # 挂卖单 (Sell)
        if abs(new_ask - self.target_ask_price) > price_threshold:
            self._cancel_side("SELL")
            if pos_value > -self.max_pos_usdt:
                new_ask = ref_data_manager.round_price(ob.symbol, new_ask)
                self.sell(ob.symbol, new_ask, order_vol)
                self.target_ask_price = new_ask

    def _cancel_side(self, side_str):
        # side_str: "BUY" or "SELL"
        for oid, req in list(self.active_orders.items()):
            if req.side == side_str:
                self.cancel_order(oid)

    def on_trade(self, trade: TradeData):
        self.log(f"成交 [{trade.symbol}]: {trade.side} Vol={trade.volume} Pos={self.pos}")