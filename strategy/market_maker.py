# file: strategy/market_maker.py

import json
import time
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
        self.alpha_strength = 0.0005 

        # 4. 运行时状态
        self.target_bid_price = 0.0
        self.target_ask_price = 0.0

        # 5. 频率控制
        self.last_quote_time = 0
        self.quote_interval = 1.0 # 1秒做一次决策
        
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
        # 1. 频率控制 (1秒1次)
        if time.time() - self.last_quote_time < self.quote_interval:
            return
        self.last_quote_time = time.time()

        # 2. 特征更新
        self.feature_engine.on_orderbook(ob)
        
        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 == 0: return
        
        # 3. [规范操作] 先撤单！(清场)
        # 无论价格变没变，我们都假设上一秒的单子已经“过期”了。
        # 使用 Cancel All 接口，一键清除该币种所有挂单。
        self.cancel_all(ob.symbol)
        
        # 4. 计算新参数
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
        
        # 规整化
        new_bid = ref_data_manager.round_price(ob.symbol, new_bid)
        new_ask = ref_data_manager.round_price(ob.symbol, new_ask)
        
        # 5. [规范操作] 挂新单
        # 只有在持仓允许的情况下才挂单
        
        # Buy Side
        if pos_value < self.max_pos_usdt:
            # 智能判断：如果我有空单，这是平空；如果没空单，这是开多
            if self.pos < 0:
                self.exit_short(ob.symbol, new_bid, order_vol)
            else:
                self.buy(ob.symbol, new_bid, order_vol)
            
        # Sell Side
        if pos_value > -self.max_pos_usdt:
            # 智能判断：如果我有多单，这是平多；如果没多单，这是开空
            if self.pos > 0:
                self.exit_long(ob.symbol, new_ask, order_vol)
            else:
                self.sell(ob.symbol, new_ask, order_vol)

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
        self.feature_engine.on_trade(trade)
        self.log(f"成交 [{trade.symbol}]: {trade.side} Vol={trade.volume}")