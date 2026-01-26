# file: strategy/market_maker.py

import json
from .base import StrategyTemplate
from event.type import OrderBook, TradeData, Side
from data.ref_data import ref_data_manager

# 引入 Alpha 模块
from alpha.engine import FeatureEngine
from alpha.signal import MockSignal

class MarketMakerStrategy(StrategyTemplate):
    def __init__(self, engine, oms):
        # 初始化基类
        super().__init__(engine, oms, "SmartMM")
        
        # 1. 加载配置
        self.config = self._load_strategy_config()
        
        # 2. 策略参数
        # 基础风控与资金参数
        self.lot_multiplier = self.config.get("lot_multiplier", 1.0)
        self.max_pos_usdt = self.config.get("max_pos_usdt", 2000.0)
        
        # 定价参数
        self.spread_ratio = self.config.get("spread_ratio", 0.0005) # 基础价差 (万5)
        self.skew_factor_usdt = self.config.get("skew_factor_usdt", 50.0) # 库存敏感度
        
        # Alpha 参数
        self.alpha_strength = 0.0005 # 信号强度 (信号1 = 偏移万5)
        
        # RPI 开关
        self.use_rpi = self.config.get("use_rpi", False)
        
        # 3. 初始化 Alpha 引擎
        self.feature_engine = FeatureEngine()
        self.signal_gen = MockSignal() 
        
        # 4. 运行时状态
        self.target_bid_price = 0.0
        self.target_ask_price = 0.0
        
        mode_str = "RPI ONLY" if self.use_rpi else "NORMAL MAKER"
        print(f"[{self.name}] 策略已启动. 模式: {mode_str}, Multiplier: {self.lot_multiplier}x")

    def _load_strategy_config(self):
        try:
            with open("config.json", "r") as f:
                return json.load(f).get("strategy", {})
        except:
            return {}

    def _calculate_safe_vol(self, symbol, price):
        """
        动态计算下单量
        逻辑: max(物理最小, 金额最小) * 倍数
        """
        info = ref_data_manager.get_info(symbol)
        if not info: return 0.0
        
        # 安全金额缓冲 10%
        safe_min_notional = max(5.0, info.min_notional) * 1.1
        qty_by_notional = safe_min_notional / price
        
        base_min_qty = max(info.min_qty, qty_by_notional)
        target_qty = base_min_qty * self.lot_multiplier
        
        return ref_data_manager.round_qty(symbol, target_qty)

    # --- 智能下单路由 (单向持仓逻辑) ---
    
    def _smart_buy(self, symbol, price, vol):
        """
        盘口买入逻辑：
        优先平空 (Exit Short)，如果没空单则开多 (Entry Long)
        传递 is_rpi 参数
        """
        # 注意: self.pos 为净持仓，负数表示空头
        # 如果持有空单 (pos < 0)，买入是平空
        # 我们用 abs(pos) 来比较数量
        if self.pos < 0 and abs(self.pos) >= vol:
            return self.exit_short(symbol, price, vol, is_rpi=self.use_rpi) 
        else:
            return self.entry_long(symbol, price, vol, is_rpi=self.use_rpi) 

    def _smart_sell(self, symbol, price, vol):
        """
        盘口卖出逻辑：
        优先平多 (Exit Long)，如果没多单则开空 (Entry Short)
        传递 is_rpi 参数
        """
        if self.pos > 0 and self.pos >= vol:
            return self.exit_long(symbol, price, vol, is_rpi=self.use_rpi)
        else:
            return self.entry_short(symbol, price, vol, is_rpi=self.use_rpi)

    # --- 核心 Tick 驱动逻辑 ---

    def on_orderbook(self, ob: OrderBook):
        # 1. 更新特征工程
        self.feature_engine.on_orderbook(ob)
        
        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 == 0: return
        
        mid_price = (bid_1 + ask_1) / 2.0
        
        # 2. 计算下单量
        order_vol = self._calculate_safe_vol(ob.symbol, mid_price)
        if order_vol <= 0: return

        # 3. 获取 Alpha 信号
        alpha_signal = self.signal_gen.predict(self.feature_engine)
        
        # 4. 计算综合 Skew (偏移量)
        # Inventory Skew: 持仓多 -> 价格下移
        pos_value = self.pos * mid_price
        inventory_skew = (pos_value / 1000.0) * (self.skew_factor_usdt / 1000.0) * mid_price 
        
        # Alpha Skew: 预测涨 -> 价格上移
        alpha_skew = alpha_signal * self.alpha_strength * mid_price
        
        # 5. 计算保留价格 (Reservation Price)
        reservation_price = mid_price - inventory_skew + alpha_skew
        
        # [安全钳] 限制偏离中价不超过 3%
        upper_bound = mid_price * 1.03
        lower_bound = mid_price * 0.97
        reservation_price = max(lower_bound, min(upper_bound, reservation_price))
        
        # 6. 计算挂单价格
        spread = mid_price * self.spread_ratio
        raw_bid = reservation_price - spread / 2
        raw_ask = reservation_price + spread / 2
        
        # 规整化价格 (重要：防止精度错误)
        new_bid = ref_data_manager.round_price(ob.symbol, raw_bid)
        new_ask = ref_data_manager.round_price(ob.symbol, raw_ask)
        
        # 挂单阈值 (万分之五变动才改单，防止 Spam)
        price_threshold = mid_price * 0.0005
        
        # --- 执行逻辑 ---
        
        # 买单处理 (Bid Side)
        if abs(new_bid - self.target_bid_price) > price_threshold:
            # 撤销所有买方向的单子 (包括 OpenLong 和 CloseShort)
            self._cancel_side(Side.BUY)
            
            # 检查最大持仓限制 (只在开仓方向限制，平仓不限制)
            # 如果是平空(pos<0)，允许买入；如果是开多(pos>=0)，检查上限
            is_opening_long = (self.pos >= 0)
            if not is_opening_long or (pos_value < self.max_pos_usdt):
                self._smart_buy(ob.symbol, new_bid, order_vol)
                self.target_bid_price = new_bid
            
        # 卖单处理 (Ask Side)
        if abs(new_ask - self.target_ask_price) > price_threshold:
            # 撤销所有卖方向的单子
            self._cancel_side(Side.SELL)
            
            # 检查最大持仓限制
            # 如果是平多(pos>0)，允许卖出；如果是开空(pos<=0)，检查上限
            is_opening_short = (self.pos <= 0)
            # 注意 pos_value 此时为负或0，比较时要注意方向
            # 这里简单用绝对值判断
            if not is_opening_short or (abs(pos_value) < self.max_pos_usdt):
                self._smart_sell(ob.symbol, new_ask, order_vol)
                self.target_ask_price = new_ask

    def _cancel_side(self, target_side: Side):
        """
        撤销指定方向的所有挂单
        target_side: Side.BUY (买单) 或 Side.SELL (卖单)
        """
        # 遍历本地维护的 active_orders
        # key: client_oid, value: OrderIntent
        for oid, intent in list(self.active_orders.items()):
            if intent.side == target_side:
                self.cancel_order(oid)

    def on_trade(self, trade: TradeData):
        # 更新特征工程中的 Trade 相关因子
        self.feature_engine.on_trade(trade)
        
        direction_str = "BUY" if trade.side == Side.BUY else "SELL"
        self.log(f"成交 [{trade.symbol}]: {direction_str} Vol={trade.volume} Price={trade.price}")