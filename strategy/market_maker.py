# file: strategy/market_maker.py

import json
from .base import StrategyTemplate
from event.type import OrderBook, TradeData
from data.ref_data import ref_data_manager

# 引入 Alpha 模块
from alpha.engine import FeatureEngine
from alpha.signal import MockSignal

class MarketMakerStrategy(StrategyTemplate):
    def __init__(self, engine, gateway, risk_manager):
        super().__init__(engine, gateway, risk_manager, "SmartMM")
        
        self.config = self._load_strategy_config()
        self.lot_multiplier = self.config.get("lot_multiplier", 1.0)
        self.spread_ratio = self.config.get("spread_ratio", 0.0005)
        self.skew_factor_usdt = self.config.get("skew_factor_usdt", 50.0)
        self.max_pos_usdt = self.config.get("max_pos_usdt", 2000.0)
        
        # Alpha 组件初始化
        self.feature_engine = FeatureEngine()
        self.signal_gen = MockSignal() 
        
        # [修改] 信号强度改为“比例系数”
        # 例如 0.0005 表示：信号为1时，偏移 0.05% 的价格
        # 如果信号满格 10，则偏移 0.5%
        self.alpha_strength = 0.0005 
        
        self.target_bid_price = 0.0
        self.target_ask_price = 0.0
        
        print(f"[{self.name}] 策略已启动 (Alpha驱动 + 相对比例版)")

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
        # 1. 更新特征工程
        self.feature_engine.on_orderbook(ob)
        
        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 == 0: return
        
        mid_price = (bid_1 + ask_1) / 2.0
        order_vol = self._calculate_safe_vol(ob.symbol, mid_price)
        if order_vol <= 0: return

        # 2. 获取预测信号 (Range: -10 ~ 10)
        alpha_signal = self.signal_gen.predict(self.feature_engine)
        
        # 3. 计算综合 Skew (全部改为相对比例计算)
        
        # A. 库存 Skew
        # 逻辑：持仓价值每增加 1000U，Skew 偏移 50U (也就是 5%)
        # 公式改为比例：skew_ratio = (NetPosValue / 1000) * (Factor / 1000)
        pos_value = self.pos * mid_price
        # 这里的 50/1000 = 0.05，即持仓1000U偏移5%。
        inventory_skew = (pos_value / 1000.0) * (self.skew_factor_usdt / 1000.0) * mid_price
        
        # B. Alpha Skew [修复点]
        # 公式：Signal * Strength * Price
        # 例：10 * 0.0005 * 134 = 0.67 USD (偏移很合理)
        alpha_skew = alpha_signal * self.alpha_strength * mid_price
        
        # 最终定价中心
        reservation_price = mid_price - inventory_skew + alpha_skew
        
        # [NEW] 安全钳 (Safety Clamp)
        # 强制限制 Reservation Price 不偏离 Mid Price 超过 3%
        # 防止极端信号或极端持仓导致价格触发交易所 Price Filter
        upper_bound = mid_price * 1.03
        lower_bound = mid_price * 0.97
        reservation_price = max(lower_bound, min(upper_bound, reservation_price))
        
        spread = mid_price * self.spread_ratio
        new_bid = reservation_price - spread / 2
        new_ask = reservation_price + spread / 2
        
        # 4. 打印调试信息 (查看修复效果)
        if abs(alpha_signal) > 2.0:
            # self.log(f"Sig:{alpha_signal:.1f} Skew:{alpha_skew:.2f} Prc:{reservation_price:.2f}")
            pass

        # 5. 挂单逻辑
        price_threshold = mid_price * 0.0005
        
        if abs(new_bid - self.target_bid_price) > price_threshold:
            self._cancel_side("BUY")
            if pos_value < self.max_pos_usdt:
                new_bid = ref_data_manager.round_price(ob.symbol, new_bid)
                self.buy(ob.symbol, new_bid, order_vol)
                self.target_bid_price = new_bid
            
        if abs(new_ask - self.target_ask_price) > price_threshold:
            self._cancel_side("SELL")
            if pos_value > -self.max_pos_usdt:
                new_ask = ref_data_manager.round_price(ob.symbol, new_ask)
                self.sell(ob.symbol, new_ask, order_vol)
                self.target_ask_price = new_ask

    def _cancel_side(self, side_str):
        for oid, req in list(self.active_orders.items()):
            if req.side == side_str:
                self.cancel_order(oid)

    def on_trade(self, trade: TradeData):
        self.feature_engine.on_trade(trade)
        self.log(f"成交 [{trade.symbol}]: {trade.side} Vol={trade.volume}")