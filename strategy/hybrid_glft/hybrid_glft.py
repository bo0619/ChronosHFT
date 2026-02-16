# file: strategy/hybrid_glft/main.py
import time, math, json
from collections import defaultdict
from ..base import StrategyTemplate
from event.type import (
    OrderBook, TradeData, OrderIntent, Side, AggTradeData, 
    OrderStateSnapshot, Event, EVENT_STRATEGY_UPDATE, StrategyData
)
from .detector import TrendDetector
from .predictor import MLTrendPredictor
from .selector import HybridModeSelector

from alpha.factors import GLFTCalibrator
from alpha.signal import OnlineRidgePredictor
from alpha.engine import FeatureEngine
from data.ref_data import ref_data_manager
from data.cache import data_cache

class HybridGLFTStrategy(StrategyTemplate):
    def __init__(self, engine, oms):
        super().__init__(engine, oms, "HybridGLFT_V2")
        self.feature_engine = FeatureEngine()
        self.trend_detectors = {}
        self.ml_predictors = {}
        self.calibrators = {}
        self.models = {}
        self.mode_selector = HybridModeSelector(threshold=0.5)
        self.last_run_times = defaultdict(float)
        self.quote_state = defaultdict(lambda: {"bid_oid": None, "ask_oid": None, "bid_price": None, "ask_price": None, "last_update": 0})
        
        self.cycle_interval = 0.5
        self.base_gamma = 0.1

    def _ensure_symbol(self, symbol):
        """确保所有字典都已为该 symbol 初始化，防止 KeyError"""
        if symbol not in self.calibrators:
            self.calibrators[symbol] = GLFTCalibrator(window=1000)
            self.models[symbol] = OnlineRidgePredictor(num_features=9)
            self.trend_detectors[symbol] = TrendDetector()
            self.ml_predictors[symbol] = MLTrendPredictor()
            # 必须调用一次 feature_engine 注册 symbol
            self.feature_engine.on_orderbook(OrderBook(symbol, "INIT", time.time()))

    def on_orderbook(self, ob: OrderBook):
        symbol = ob.symbol
        self._ensure_symbol(symbol) # 关键：修复 KeyError

        # 更新引擎
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

        # 计算信号
        rule_sig = self.trend_detectors[symbol].compute_trend_signal()
        feats = self.trend_detectors[symbol].get_features()
        ml_pred = self.ml_predictors[symbol].predict(feats, now)
        
        mode = self.mode_selector.select_mode(rule_sig, ml_pred)

        # 执行逻辑 (根据模式切换)
        if "MOMENTUM" in mode.mode:
            self._execute_momentum(symbol, ob, mid, mode)
        else:
            self._execute_mm(symbol, ob, mid)

    def _execute_mm(self, symbol, ob, mid):
        # 复用之前的 GLFT 报价逻辑...
        pass

    def _execute_momentum(self, symbol, ob, mid, mode):
        # 执行追单逻辑...
        pass

    def on_market_trade(self, trade: AggTradeData):
        self._ensure_symbol(trade.symbol)
        self.feature_engine.on_trade(trade)
        if trade.symbol in self.calibrators:
            mid = data_cache.get_mark_price(trade.symbol)
            self.calibrators[trade.symbol].on_market_trade(trade, mid)