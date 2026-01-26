# file: strategy/market_maker.py

import json
from .base import StrategyTemplate
from event.type import OrderBook, TradeData
from data.ref_data import ref_data_manager
from alpha.engine import FeatureEngine
from alpha.signal import MockSignal

class MarketMakerStrategy(StrategyTemplate):
    # [修改] 参数列表适配基类
    def __init__(self, engine, oms):
        super().__init__(engine, oms, "SmartMM")
        
        self.config = self._load_strategy_config()
        self.lot_multiplier = self.config.get("lot_multiplier", 1.0)
        self.spread_ratio = self.config.get("spread_ratio", 0.0005)
        self.skew_factor_usdt = self.config.get("skew_factor_usdt", 50.0)
        self.max_pos_usdt = self.config.get("max_pos_usdt", 2000.0)
        
        # Alpha
        self.feature_engine = FeatureEngine()
        self.signal_gen = MockSignal() 
        self.alpha_strength = 0.0005 
        
        self.target_bid_price = 0.0
        self.target_ask_price = 0.0
        
        print(f"[{self.name}] 策略已启动 (OMS驱动版)")

    def _load_strategy_config(self):
        try:
            with open("config.json", "r") as f: return json.load(f).get("strategy", {})
        except: return {}

    def _calculate_safe_vol(self, symbol, price):
        # ... (逻辑保持不变) ...
        info = ref_data_manager.get_info(symbol)
        if not info: return 0.0
        safe_min = max(5.0, info.min_notional) * 1.1
        qty_val = safe_min / price
        target = max(info.min_qty, qty_val) * self.lot_multiplier
        return ref_data_manager.round_qty(symbol, target)

    def on_orderbook(self, ob: OrderBook):
        # ... (Alpha 计算逻辑保持不变) ...
        self.feature_engine.on_orderbook(ob)
        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 == 0: return
        
        mid_price = (bid_1 + ask_1) / 2.0
        order_vol = self._calculate_safe_vol(ob.symbol, mid_price)
        if order_vol <= 0: return

        alpha_signal = self.signal_gen.predict(self.feature_engine)
        
        pos_value = self.pos * mid_price
        inventory_skew = (pos_value / 1000.0) * (self.skew_factor_usdt / 1000.0) * mid_price 
        alpha_skew = alpha_signal * self.alpha_strength * mid_price
        
        reservation_price = mid_price - inventory_skew + alpha_skew
        
        upper = mid_price * 1.03
        lower = mid_price * 0.97
        reservation_price = max(lower, min(upper, reservation_price))
        
        spread = mid_price * self.spread_ratio
        new_bid = reservation_price - spread / 2
        new_ask = reservation_price + spread / 2
        
        # 规整化价格 (重要: Strategy需要自己规整，或者OMS Validator会报错)
        # 建议在发单前规整。由于 send_order_safe 移到了基类，基类里没有做规整？
        # 让我们看基类：基类 send_order_safe 之前有规整，但现在直接转 OrderIntent 了。
        # [修正] 我们需要在策略层或者 OMS Validator 层做规整。
        # 最佳实践：策略层做规整，OMS Validator 做检查。
        
        new_bid = ref_data_manager.round_price(ob.symbol, new_bid)
        new_ask = ref_data_manager.round_price(ob.symbol, new_ask)
        
        price_threshold = mid_price * 0.0005
        
        # 挂单
        if abs(new_bid - self.target_bid_price) > price_threshold:
            self._cancel_side("BUY")
            if pos_value < self.max_pos_usdt:
                self.buy(ob.symbol, new_bid, order_vol)
                self.target_bid_price = new_bid
            
        if abs(new_ask - self.target_ask_price) > price_threshold:
            self._cancel_side("SELL")
            if pos_value > -self.max_pos_usdt:
                self.sell(ob.symbol, new_ask, order_vol)
                self.target_ask_price = new_ask

    def _cancel_side(self, side_str):
        for oid, req in list(self.active_orders.items()):
            # req 是 OrderIntent 对象
            if req.side.value == side_str: # 枚举值比较
                self.cancel_order(oid)

    def on_trade(self, trade: TradeData):
        self.feature_engine.on_trade(trade)
        self.log(f"成交 [{trade.symbol}]: {trade.side} Vol={trade.volume}")