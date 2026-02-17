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
from alpha.engine import FeatureEngine
from data.ref_data import ref_data_manager
from data.cache import data_cache


class HybridGLFTStrategy(StrategyTemplate):
    def __init__(self, engine, oms):
        super().__init__(engine, oms, "HybridGLFT_Pro")
        self.feature_engine = FeatureEngine()
        self.calibrators = {}
        self.ml_predictors = {}
        self.trend_detectors = {}
        self.mode_selector = HybridModeSelector(threshold=0.55)
        self.last_run_times = defaultdict(float)
        self.quote_state = defaultdict(lambda: {
            "bid_oid": None, "ask_oid": None,
            "bid_price": None, "ask_price": None,
            "last_update": 0
        })
        # [FIX-10] per-symbol 状态，供广播时读取
        self.last_mode = defaultdict(lambda: "MARKET_MAKING")
        self.last_ml_pred = defaultdict(lambda: None)

        self.cycle_interval = 0.5
        self.base_gamma = 0.1
        self.min_spread_bps = 5.0
        self.COLDSTART_GAMMA_MULT = 3.0

    def _ensure_symbol(self, symbol):
        if symbol not in self.calibrators:
            self.calibrators[symbol] = GLFTCalibrator(window=1000)
            self.trend_detectors[symbol] = TrendDetector()
            self.ml_predictors[symbol] = MLTrendPredictor(enabled=True)
            # [FIX-3] 不强制 trained=True
            self.feature_engine.on_orderbook(OrderBook(symbol, "INIT", datetime.now()))

    def on_orderbook(self, ob: OrderBook):
        symbol = ob.symbol
        self._ensure_symbol(symbol)

        # ── 阶段一：每 tick，无门控 ──────────────────────────────
        self.calibrators[symbol].on_orderbook(ob)
        self.feature_engine.on_orderbook(ob)
        self.trend_detectors[symbol].on_orderbook(ob)

        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 == 0:
            return
        mid = (bid_1 + ask_1) / 2.0
        feats = self.trend_detectors[symbol].get_features()
        now = time.time()

        # [FIX-9] 每 tick 训练，最大化样本密度
        self.ml_predictors[symbol].add_tick(feats, mid, now)

        # ── 阶段二：频率门控 0.5s ────────────────────────────────
        if now - self.last_run_times[symbol] < self.cycle_interval:
            return
        self.last_run_times[symbol] = now

        # [FIX-2] 冷启动保护
        if not self.calibrators[symbol].is_warmed_up:
            self.last_mode[symbol] = "COLDSTART"
            self._execute_mm(symbol, ob, mid, alpha_bps=0.0,
                             gamma_mult=self.COLDSTART_GAMMA_MULT)
            return

        rule_sig = self.trend_detectors[symbol].compute_trend_signal()
        ml_pred = self.ml_predictors[symbol].predict(feats, now)

        # [FIX-10] 保存最新预测和模式
        self.last_ml_pred[symbol] = ml_pred
        mode_obj = self.mode_selector.select_mode(rule_sig, ml_pred)
        self.last_mode[symbol] = mode_obj.mode

        if mode_obj.mode == "MARKET_MAKING":
            self._execute_mm(symbol, ob, mid, alpha_bps=0.0, gamma_mult=1.0)
        elif mode_obj.mode == "MOMENTUM_BUY":
            self._execute_mm(symbol, ob, mid, alpha_bps=10.0, gamma_mult=0.5)
        elif mode_obj.mode == "MOMENTUM_SELL":
            self._execute_mm(symbol, ob, mid, alpha_bps=-10.0, gamma_mult=0.5)

    def _execute_mm(self, symbol, ob, mid, alpha_bps=0.0, gamma_mult=1.0):
        calibrator = self.calibrators[symbol]
        sigma = max(1.0, calibrator.sigma_bps)
        A = max(0.1, calibrator.A)
        k = max(0.1, calibrator.k)
        gamma = self.base_gamma * gamma_mult

        fair_mid = mid * (1.0 + alpha_bps / 10000.0)
        order_vol = self._calculate_safe_vol(symbol, mid)
        if order_vol <= 0:
            return

        current_pos = self.pos
        q_norm = current_pos / order_vol if order_vol > 0 else 0
        base_half_spread_bps = (1.0 / gamma) * math.log(1.0 + gamma / k)
        inventory_skew_bps = q_norm * (gamma * sigma ** 2) / (2 * A * k)

        spread_price = mid * (base_half_spread_bps / 10000.0)
        skew_price   = mid * (inventory_skew_bps   / 10000.0)  # [FIX-1]

        target_bid = fair_mid - spread_price - skew_price
        target_ask = fair_mid + spread_price - skew_price

        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        info = ref_data_manager.get_info(symbol)
        tick = info.tick_size

        min_half = mid * (self.min_spread_bps / 20000.0)
        if (target_ask - target_bid) < min_half * 2:
            center = (target_bid + target_ask) / 2
            target_bid = center - min_half
            target_ask = center + min_half

        target_bid = ref_data_manager.round_price(symbol, target_bid)
        target_ask = ref_data_manager.round_price(symbol, target_ask)
        if target_bid >= ask_1: target_bid = ask_1 - tick
        if target_ask <= bid_1: target_ask = bid_1 + tick
        if target_bid >= target_ask:
            target_bid = mid - tick
            target_ask = mid + tick

        # [FIX-10] 读取 ML 状态，打包完整 StrategyData
        predictor  = self.ml_predictors[symbol]
        ml_stats   = predictor.get_stats()
        ml_pred    = self.last_ml_pred[symbol]

        strat_data = StrategyData(
            symbol=symbol,
            fair_value=fair_mid,
            alpha_bps=alpha_bps,
            gamma=gamma, k=k, A=A, sigma=sigma,
            # ML 字段
            ml_mode=self.last_mode[symbol],
            ml_p_trend=ml_pred.p_trend if ml_pred else 0.5,
            ml_trained=ml_stats["trained"],
            ml_n_samples=ml_stats["n_samples"],
            ml_buffer_size=ml_stats["buffer_size"],
            ml_clf_weights=ml_stats["clf_weights"],
            ml_reg_weights=ml_stats["reg_weights"],
        )
        self.engine.put(Event(EVENT_STRATEGY_UPDATE, strat_data))
        self._update_quotes(symbol, target_bid, target_ask, order_vol)

    def _update_quotes(self, symbol, bid, ask, volume):
        state = self.quote_state[symbol]
        info  = ref_data_manager.get_info(symbol)
        tick  = info.tick_size
        now   = time.time()
        if (now - state["last_update"]) * 1000 < 200:
            return
        if state["bid_price"] is None or abs(bid - state["bid_price"]) >= tick:
            if state["bid_oid"]: self.cancel_order(state["bid_oid"])
            oid = self.buy(symbol, bid, volume)
            if oid: state["bid_oid"], state["bid_price"] = oid, bid
        if state["ask_price"] is None or abs(ask - state["ask_price"]) >= tick:
            if state["ask_oid"]: self.cancel_order(state["ask_oid"])
            oid = self.sell(symbol, ask, volume)
            if oid: state["ask_oid"], state["ask_price"] = oid, ask
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

    def on_order(self, snapshot: OrderStateSnapshot):
        super().on_order(snapshot)
        if snapshot.status in ["FILLED", "CANCELLED", "REJECTED", "EXPIRED"]:
            state = self.quote_state[snapshot.symbol]
            if state["bid_oid"] == snapshot.client_oid:
                state["bid_oid"] = None; state["bid_price"] = None
            if state["ask_oid"] == snapshot.client_oid:
                state["ask_oid"] = None; state["ask_price"] = None

    def on_trade(self, trade: TradeData):
        self.log(f"FILL: {trade.symbol} {trade.side} {trade.volume} @ {trade.price}")