# file: strategy/hybrid_glft/main.py

import time, math
from collections import defaultdict
from datetime import datetime
from ..base import StrategyTemplate
from event.type import (
    OrderBook, TradeData, OrderIntent, Side, AggTradeData, 
    OrderStateSnapshot, Event, EVENT_STRATEGY_UPDATE, StrategyData
)
from .detector import TrendDetector
from .predictor import MLTrendPredictor
from .selector import HybridModeSelector

from alpha.factors import GLFTCalibrator
from alpha.signal import MultiHorizonPredictor
from alpha.engine import FeatureEngine
from data.ref_data import ref_data_manager
from data.cache import data_cache

class HybridGLFTStrategy(StrategyTemplate):
    def __init__(self, engine, oms):
        super().__init__(engine, oms, "HybridGLFT_Pro")
        self.feature_engine = FeatureEngine()
        
        # 组件字典
        self.calibrators = {}
        self.ml_predictors = {}
        self.trend_detectors = {}
        
        self.mode_selector = HybridModeSelector(threshold=0.3) # 调低门槛以便观察
        self.last_run_times = defaultdict(float)
        self.quote_state = defaultdict(lambda: {
            "bid_oid": None, "ask_oid": None, 
            "bid_price": None, "ask_price": None, 
            "last_update": 0
        })
        
        self.cycle_interval = 0.5 
        self.base_gamma = 0.1
        self.min_spread_bps = 5.0

    def _ensure_symbol(self, symbol):
        if symbol not in self.calibrators:
            self.calibrators[symbol] = GLFTCalibrator(window=1000)
            self.trend_detectors[symbol] = TrendDetector()
            self.ml_predictors[symbol] = MLTrendPredictor(enabled=True)
            self.ml_predictors[symbol].trained = True # 强制开启，即使权重是0，也会输出基础信号
            self.feature_engine.on_orderbook(OrderBook(symbol, "INIT", datetime.now()))

    def on_orderbook(self, ob: OrderBook):
        symbol = ob.symbol
        self._ensure_symbol(symbol)

        # 1. 更新数据
        self.calibrators[symbol].on_orderbook(ob)
        self.feature_engine.on_orderbook(ob)
        self.trend_detectors[symbol].on_orderbook(ob)

        now = time.time()
        if now - self.last_run_times[symbol] < self.cycle_interval: return
        self.last_run_times[symbol] = now

        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 == 0: return
        mid = (bid_1 + ask_1) / 2.0

        # 2. 计算信号
        rule_sig = self.trend_detectors[symbol].compute_trend_signal()
        feats = self.trend_detectors[symbol].get_features()
        ml_pred = self.ml_predictors[symbol].predict(feats, now)
        
        # 3. 模式选择
        mode_obj = self.mode_selector.select_mode(rule_sig, ml_pred)

        # 4. 根据不同模式执行不同的 GLFT 变体
        if mode_obj.mode == "MARKET_MAKING":
            self._execute_mm(symbol, ob, mid, 0.0) # 0.0 代表无 alpha 偏移
        elif mode_obj.mode == "MOMENTUM_BUY":
            # 动能看涨：alpha 向上偏移 5bps，且降低 gamma 变得激进
            self._execute_mm(symbol, ob, mid, alpha_bps=10.0, gamma_mult=0.5)
        elif mode_obj.mode == "MOMENTUM_SELL":
            # 动能看跌：alpha 向下偏移 5bps
            self._execute_mm(symbol, ob, mid, alpha_bps=-10.0, gamma_mult=0.5)

    def _execute_mm(self, symbol, ob, mid, alpha_bps=0.0, gamma_mult=1.0):
        """
        核心 GLFT 报价逻辑（由原 PredictiveGLFT 移植并优化）
        """
        calibrator = self.calibrators[symbol]
        
        # A. 动态参数获取
        sigma = max(1.0, calibrator.sigma_bps)
        A = max(0.1, calibrator.A)
        k = max(0.1, calibrator.k)
        gamma = self.base_gamma * gamma_mult
        
        # B. 价格修正
        fair_mid = mid * (1.0 + alpha_bps / 10000.0)
        
        # C. 库存风险计算
        order_vol = self._calculate_safe_vol(symbol, mid)
        current_pos = self.pos # 从基类获取
        q_norm = current_pos / order_vol if order_vol > 0 else 0
        
        # GLFT 公式
        base_half_spread_bps = (1.0 / gamma) * math.log(1.0 + gamma / k)
        inventory_skew_bps = q_norm * (gamma * sigma ** 2) / (2 * A * k)
        
        spread_price = mid * (base_half_spread_bps / 10000.0)
        skew_price = mid * (inventory_skew_bps / 1000.0) # 这里注意量纲
        
        target_bid = fair_mid - spread_price - skew_price
        target_ask = fair_mid + spread_price - skew_price
        
        # D. 规整与发送
        target_bid = ref_data_manager.round_price(symbol, target_bid)
        target_ask = ref_data_manager.round_price(symbol, target_ask)
        
        self._update_quotes(symbol, target_bid, target_ask, order_vol)

    def _update_quotes(self, symbol, bid, ask, volume):
        """增量改单逻辑：只有价格变化才撤单重挂"""
        state = self.quote_state[symbol]
        info = ref_data_manager.get_info(symbol)
        tick = info.tick_size
        now = time.time()
        
        if (now - state["last_update"]) * 1000 < 200: return # 200ms 冷却

        # 买单逻辑
        if state["bid_price"] is None or abs(bid - state["bid_price"]) >= tick:
            if state["bid_oid"]: self.cancel_order(state["bid_oid"])
            oid = self.buy(symbol, bid, volume)
            if oid:
                state["bid_oid"], state["bid_price"] = oid, bid
        
        # 卖单逻辑
        if state["ask_price"] is None or abs(ask - state["ask_price"]) >= tick:
            if state["ask_oid"]: self.cancel_order(state["ask_oid"])
            oid = self.sell(symbol, ask, volume)
            if oid:
                state["ask_oid"], state["ask_price"] = oid, ask
                
        state["last_update"] = now

    def _calculate_safe_vol(self, symbol, price):
        info = ref_data_manager.get_info(symbol)
        if not info: return 0.0
        min_vol = max(info.min_qty, (info.min_notional * 1.2) / price)
        return ref_data_manager.round_qty(symbol, min_vol)

    def on_market_trade(self, trade: AggTradeData):
        self._ensure_symbol(trade.symbol)
        self.feature_engine.on_trade(trade)
        if trade.symbol in self.calibrators:
            mid = data_cache.get_mark_price(trade.symbol)
            self.calibrators[trade.symbol].on_market_trade(trade, mid)