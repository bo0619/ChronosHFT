# file: strategy/predictive_glft.py

import time
import math
import numpy as np
from collections import defaultdict, deque

from .base import StrategyTemplate
from event.type import (
    OrderBook, TradeData, OrderIntent, Side,
    AggTradeData, OrderStateSnapshot,
    Event, EVENT_STRATEGY_UPDATE, StrategyData
)

from alpha.factors import GLFTCalibrator
from alpha.signal import OnlineRidgePredictor
from alpha.engine import FeatureEngine

from data.ref_data import ref_data_manager
from data.cache import data_cache

class PredictiveGLFTStrategy(StrategyTemplate):
    """
    [Predictive GLFT] 预测驱动型做市策略
    
    核心逻辑分层：
    L1: 预测未来价格 E[St+Δ]
    L2: 置信度衰减 (Signal / Volatility)
    L3: 库存耦合 (Inventory Dampening)
    L4: 信号有效期管理 (Time Decay)
    L5: 费率门槛过滤 (Fee Threshold)
    L6: 预测控制 Gamma (Behavior Intensity)
    """
    def __init__(self, engine, oms):
        super().__init__(engine, oms, "Pred_GLFT")
        
        self.config = self._load_strategy_config()
        self.strat_conf = self.config.get("strategy", {})
        
        # --- 基础参数 ---
        self.base_gamma = self.strat_conf.get("gamma", 0.1)
        self.lot_multiplier = self.strat_conf.get("lot_multiplier", 1.0)
        self.cycle_interval = 0.5 # 预测型策略建议加快频率
        
        # --- 预测控制参数 (六层逻辑参数) ---
        self.pred_conf = {
            "prediction_horizon_sec": 5.0, # L4: 预测有效时间窗
            "fee_threshold_bps": 2.0,      # L5: 只有预测收益 > 2bp 才生效
            "gamma_beta": 2.0,             # L6: Gamma 敏感度系数
            "max_pos_usdt": 3000.0         # L3: 最大持仓限制
        }

        # --- 组件 ---
        self.feature_engine = FeatureEngine()
        
        # Per-Symbol State
        self.calibrators = {}
        self.models = {}
        self.last_run_times = defaultdict(float)
        
        # 信号状态缓存 (用于 L4 时间衰减)
        # {symbol: {"val": bps, "time": timestamp}}
        self.last_prediction = defaultdict(lambda: {"val": 0.0, "time": 0})
        
        # 执行状态
        self.quote_state = defaultdict(lambda: {
            "bid_oid": None, "ask_oid": None, 
            "bid_price": None, "ask_price": None,
            "last_update": 0
        })
        self.cooldown_ms = 200

        print(f"[{self.name}] 预测型 GLFT 已启动. BaseGamma={self.base_gamma}")

    def _load_strategy_config(self):
        try:
            import json
            with open("config.json", "r") as f: return json.load(f)
        except: return {}

    def _get_components(self, symbol):
        if symbol not in self.calibrators:
            # GLFT 参数校准器
            self.calibrators[symbol] = GLFTCalibrator(window=1000)
            # ML 预测器 (9维特征)
            self.models[symbol] = OnlineRidgePredictor(num_features=9, lambda_reg=0.5)
        return self.calibrators[symbol], self.models[symbol]

    def _calculate_safe_vol(self, symbol, price):
        info = ref_data_manager.get_info(symbol)
        if not info: return 0.0
        min_vol = max(info.min_qty, (info.min_notional * 1.1) / price)
        return ref_data_manager.round_qty(symbol, min_vol * self.lot_multiplier)

    # ============================================================
    # 核心 Tick 逻辑
    # ============================================================
    def on_orderbook(self, ob: OrderBook):
        symbol = ob.symbol
        calibrator, model = self._get_components(symbol)

        # 1. 数据更新
        calibrator.on_orderbook(ob)
        self.feature_engine.on_orderbook(ob)
        
        now = time.time()
        if now - self.last_run_times[symbol] < self.cycle_interval: return
        self.last_run_times[symbol] = now

        # 2. 基础市场数据
        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 == 0: return
        mid = (bid_1 + ask_1) / 2.0
        
        # 获取当前波动率 (用于 L2 置信度计算)
        sigma_bps = max(1.0, calibrator.sigma_bps)

        # --------------------------------------------------------
        # Layer 1: 获取原始预测 (未来价格偏移量 bps)
        # --------------------------------------------------------
        features = self.feature_engine.get_features(symbol)
        raw_pred_bps = model.update_and_predict(features, mid)
        
        # 缓存预测值
        self.last_prediction[symbol] = {"val": raw_pred_bps, "time": now}

        # --------------------------------------------------------
        # Layer 4: 预测有效期管理 (Time Decay)
        # --------------------------------------------------------
        # 即使模型这一刻没输出(假设)，我们也应该沿用旧预测并衰减
        # 这里模型是每帧输出的，但在信号稀疏的模型中这很有用
        # 我们对当前输出应用一个 "瞬时置信度"
        
        # 这里简化：假设 raw_pred_bps 就是当前的有效预测
        pred_bps = raw_pred_bps

        # --------------------------------------------------------
        # Layer 5: 费率门槛过滤 (Fee Threshold)
        # --------------------------------------------------------
        if abs(pred_bps) < self.pred_conf["fee_threshold_bps"]:
            pred_bps = 0.0
            
        # --------------------------------------------------------
        # Layer 3: 库存耦合 (Inventory Coupling)
        # --------------------------------------------------------
        current_pos = self.oms.exposure.net_positions.get(symbol, 0.0)
        order_vol = self._calculate_safe_vol(symbol, mid)
        if order_vol <= 0: return
        
        # 计算当前持仓占最大持仓的比例 (-1.0 ~ 1.0)
        max_pos_val = self.pred_conf["max_pos_usdt"]
        current_pos_val = current_pos * mid
        inventory_ratio = current_pos_val / max_pos_val
        inventory_ratio = max(-1.0, min(1.0, inventory_ratio))
        
        # 耦合公式: 如果预测方向与持仓方向一致，且仓位已重，则抑制预测
        # 你的公式: adjusted = pred * (1 - abs(bias))
        adjusted_pred_bps = pred_bps * (1.0 - abs(inventory_ratio))

        # --------------------------------------------------------
        # Layer 2: 置信度计算 (Confidence)
        # --------------------------------------------------------
        # Confidence = |Pred| / Sigma
        # 预测幅度相对于当前波动率越大，置信度越高
        confidence = abs(adjusted_pred_bps) / sigma_bps
        confidence = min(1.0, confidence) # 归一化到 [0, 1]

        # 计算新的 Fair Value
        # Fair = Mid + Confidence * (Pred_Mid - Mid)
        # 也就是: Fair = Mid * (1 + Confidence * Pred_Bps / 10000)
        fair_mid = mid * (1.0 + (confidence * adjusted_pred_bps) / 10000.0)

        # --------------------------------------------------------
        # Layer 6: 预测控制 Gamma (Behavior Intensity)
        # --------------------------------------------------------
        # 趋势强 (Pred强) -> Confidence高 -> Gamma低 (Aggressive)
        # 震荡/无信号 -> Confidence低 -> Gamma高 (Defensive)
        
        beta = self.pred_conf["gamma_beta"]
        # Gamma = Base * (1 - Beta * Confidence)
        # 限制 gamma_mult 最小为 0.2，防止变成负数
        gamma_mult = max(0.2, 1.0 - beta * confidence)
        gamma_final = self.base_gamma * gamma_mult
        
        # 叠加资金占用保护 (这是硬风控，不能省)
        acc = self.oms.account
        if acc.equity > 0:
            usage = acc.used_margin / acc.equity
            if usage > 0.6: gamma_final *= 2.0 # 资金紧张时强制防守

        # --------------------------------------------------------
        # GLFT 核心计算 (Standard Routine)
        # --------------------------------------------------------
        A = max(0.1, calibrator.A)
        k = max(0.1, calibrator.k)
        
        # 归一化持仓 (手)
        q_norm = current_pos / order_vol
        
        # 1. 基础半价差 (Spread)
        half_spread_bps = (1.0 / gamma_final) * math.log(1.0 + gamma_final / k)
        
        # 2. 库存偏离 (Skew)
        skew_bps = q_norm * (gamma_final * (sigma_bps ** 2)) / (2 * A * k)
        
        # 3. 计算目标价 (基于 FairMid)
        # Bid = Fair - HalfSpread - Skew
        # Ask = Fair + HalfSpread - Skew
        
        # 转为绝对价格
        spread_price = mid * (half_spread_bps / 10000.0)
        skew_price = mid * (skew_bps / 10000.0)
        
        target_bid = fair_mid - spread_price - skew_price
        target_ask = fair_mid + spread_price - skew_price

        # --------------------------------------------------------
        # 安全与规整
        # --------------------------------------------------------
        info = ref_data_manager.get_info(symbol)
        tick = info.tick_size
        
        # 最小价差保护
        min_half = mid * (5.0 / 20000.0) # 5bps min spread
        if (target_ask - target_bid) < min_half * 2:
            center = (target_bid + target_ask) / 2
            target_bid = center - min_half
            target_ask = center + min_half

        target_bid = ref_data_manager.round_price(symbol, target_bid)
        target_ask = ref_data_manager.round_price(symbol, target_ask)
        
        # 防穿仓
        if target_bid >= ask_1: target_bid = ask_1 - tick
        if target_ask <= bid_1: target_ask = bid_1 + tick
        if target_bid >= target_ask: # 倒挂处理
            target_bid = mid - tick
            target_ask = mid + tick

        # --------------------------------------------------------
        # 执行与反馈
        # --------------------------------------------------------
        self._update_quotes(symbol, target_bid, target_ask, order_vol)
        
        # 广播状态
        strat_data = StrategyData(
            symbol=symbol,
            fair_value=fair_mid,
            alpha_bps=adjusted_pred_bps, # 显示调整后的 Alpha
            gamma=gamma_final, # 显示调整后的 Gamma
            k=k, A=A, sigma=sigma_bps
        )
        self.engine.put(Event(EVENT_STRATEGY_UPDATE, strat_data))
        
        self.feature_engine.reset_interval(symbol)

    def _update_quotes(self, symbol, bid, ask, volume):
        state = self.quote_state[symbol]
        info = ref_data_manager.get_info(symbol)
        tick = info.tick_size
        now = time.time()
        
        if (now - state["last_update"]) * 1000 < self.cooldown_ms: return

        # Buy Side
        if state["bid_price"] is None or abs(bid - state["bid_price"]) >= tick:
            if state["bid_oid"]: self.oms.cancel_order(state["bid_oid"])
            oid = self.send_intent(OrderIntent(self.name, symbol, Side.BUY, bid, volume, is_post_only=True))
            if oid:
                state["bid_oid"] = oid
                state["bid_price"] = bid
        
        # Sell Side
        if state["ask_price"] is None or abs(ask - state["ask_price"]) >= tick:
            if state["ask_oid"]: self.oms.cancel_order(state["ask_oid"])
            oid = self.send_intent(OrderIntent(self.name, symbol, Side.SELL, ask, volume, is_post_only=True))
            if oid:
                state["ask_oid"] = oid
                state["ask_price"] = ask
                
        state["last_update"] = now

    def on_market_trade(self, trade: AggTradeData):
        self.feature_engine.on_trade(trade)
        if trade.symbol in self.calibrators:
            mid = data_cache.get_mark_price(trade.symbol)
            self.calibrators[trade.symbol].on_market_trade(trade, mid)

    def on_trade(self, trade: TradeData):
        self.log(f"FILL: {trade.symbol} {trade.side} {trade.volume} @ {trade.price}")

    def on_order(self, snapshot: OrderStateSnapshot):
        super().on_order(snapshot)
        if snapshot.status in ["FILLED", "CANCELLED", "REJECTED", "EXPIRED"]:
            symbol = snapshot.symbol
            state = self.quote_state[symbol]
            if state["bid_oid"] == snapshot.client_oid:
                state["bid_oid"] = None
                state["bid_price"] = None
            if state["ask_oid"] == snapshot.client_oid:
                state["ask_oid"] = None
                state["ask_price"] = None