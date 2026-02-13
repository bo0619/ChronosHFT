# file: strategy/glft_strategy.py

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
from alpha.signal import MultiHorizonPredictor
from alpha.gate import AlphaGate
from alpha.engine import FeatureEngine

from data.ref_data import ref_data_manager
from data.cache import data_cache

class GLFTStrategy(StrategyTemplate):
    def __init__(self, engine, oms):
        super().__init__(engine, oms, "GLFT_MultiScale")

        full_config = self._load_full_config()
        self.strat_conf = full_config.get("strategy", {})

        # 基础参数
        self.gamma_base = self.strat_conf.get("gamma", 0.1)
        self.lot_multiplier = self.strat_conf.get("lot_multiplier", 1.0)
        self.cycle_interval = self.strat_conf.get("cycle_interval", 1.0)
        self.min_spread_bps = self.strat_conf.get("execution", {}).get("min_spread_bps", 5.0)

        # ========= OrderFlow 参数 =========
        self.of_lambda = 0.2
        self.imbalance_ewma = defaultdict(float)

        # ========= 成交强度与防御 =========
        self.trade_timestamps = defaultdict(deque)
        
        # [修复] 初始化 last_fill_time
        self.last_fill_time = defaultdict(float) 

        # ========= 执行状态 =========
        self.quote_state = defaultdict(lambda: {
            "bid_oid": None, "ask_oid": None,
            "bid_price": None, "ask_price": None,
            "last_update": 0.0
        })
        self.cooldown_ms = 200

        # ========= 核心组件 =========
        self.feature_engine = FeatureEngine()
        self.calibrators = {}
        self.models = {}
        self.gates = {}
        self.last_run_times = defaultdict(float)
        
        # 信号权重配置
        self.alpha_weights = {
            "short_fv_weight": 1.0,  
            "mid_spr_weight": 0.2,   
            "long_pos_weight": 500.0 
        }
        
        print(f"[{self.name}] 策略已启动. BaseGamma={self.gamma_base}")

    def _load_full_config(self):
        try:
            import json
            with open("config.json", "r") as f: return json.load(f)
        except: return {}

    def _get_components(self, symbol):
        if symbol not in self.calibrators:
            self.calibrators[symbol] = GLFTCalibrator(window=1000)
            self.models[symbol] = MultiHorizonPredictor(num_features=9)
            self.gates[symbol] = AlphaGate(max_bps=10.0, decay_factor=0.9, inventory_dampening=0.05)
        return self.calibrators[symbol], self.models[symbol], self.gates[symbol]

    def _calculate_safe_vol(self, symbol, price):
        info = ref_data_manager.get_info(symbol)
        if not info: return 0.0
        min_vol_by_val = (info.min_notional * 1.1) / price
        base_vol = max(info.min_qty, min_vol_by_val)
        target_vol = base_vol * self.lot_multiplier
        return ref_data_manager.round_qty(symbol, target_vol)

    # ============================================================
    # 核心 Tick 逻辑
    # ============================================================

    def on_orderbook(self, ob: OrderBook):
        symbol = ob.symbol
        calibrator, model, gate = self._get_components(symbol)

        # 1. 更新数据
        calibrator.on_orderbook(ob)
        self.feature_engine.on_orderbook(ob)

        # 2. 频率控制
        now = time.time()
        if now - self.last_run_times[symbol] < self.cycle_interval: return
        self.last_run_times[symbol] = now

        # 3. 市场切片
        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 == 0: return
        mid = (bid_1 + ask_1) / 2.0

        # 4. [Multi-Scale ML] 预测
        features = self.feature_engine.get_features(symbol)
        alphas = model.update_and_predict(features, mid, now)
        
        # 5. [Signal Mapping] 
        short_signal = gate.process(alphas["short"], 0) 
        fair_mid = mid * (1 + short_signal * self.alpha_weights["short_fv_weight"] / 10000.0)
        
        mid_signal_strength = abs(alphas["mid"])
        
        target_pos_usdt = alphas["long"] * self.alpha_weights["long_pos_weight"]
        target_pos_usdt = max(-2000, min(2000, target_pos_usdt))

        # 6. GLFT 参数准备
        acc = self.oms.account
        gamma = self.gamma_base
        
        # A. 资金占用防御
        if acc.equity > 0:
            usage = acc.used_margin / acc.equity
            gamma *= (1 + max(0, (usage - 0.5) * 4))
            
        # B. 订单流失衡防御
        of_imb = abs(self.imbalance_ewma[symbol])
        gamma *= (1.0 + 3.0 * of_imb)
        
        # C. [修复] 刚刚成交后的防御 (Post-Trade Defense)
        # 如果最近 2 秒内有成交，暂时加大 gamma 防止连续被穿
        if now - self.last_fill_time[symbol] < 2.0:
            gamma *= 1.5

        sigma = max(1.0, calibrator.sigma_bps)
        A = max(0.1, calibrator.A)
        k_base = max(0.1, calibrator.k)
        
        k = k_base / (1.0 + mid_signal_strength * self.alpha_weights["mid_spr_weight"])

        # 7. 计算库存偏移
        current_pos = self.oms.exposure.net_positions.get(symbol, 0.0)
        order_vol = self._calculate_safe_vol(symbol, mid)
        if order_vol <= 0: return
        
        current_pos_usdt = current_pos * mid
        effective_pos_usdt = current_pos_usdt - target_pos_usdt
        
        q_norm_effective = effective_pos_usdt / (order_vol * mid)

        # 8. GLFT 公式
        base_half_spread_bps = (1.0 / gamma) * math.log(1.0 + gamma / k)
        inventory_skew_bps = q_norm_effective * (gamma * sigma ** 2) / (2 * A * k)

        base_spread_price = mid * (base_half_spread_bps / 10000.0)
        skew_price = mid * (inventory_skew_bps / 10000.0)

        target_bid = fair_mid - base_spread_price - skew_price
        target_ask = fair_mid + base_spread_price - skew_price

        # 9. 安全钳与规整
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

        # 10. 执行更新 (增量改单)
        self._update_quotes(symbol, target_bid, target_ask, order_vol)

        # 11. 状态广播
        strat_data = StrategyData(
            symbol=symbol,
            fair_value=fair_mid,
            alpha_bps=short_signal, 
            gamma=gamma, k=k, A=A, sigma=sigma
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
        now = time.time()
        self.trade_timestamps[trade.symbol].append(now)
        
        sign = -1 if trade.maker_is_buyer else 1
        prev = self.imbalance_ewma[trade.symbol]
        self.imbalance_ewma[trade.symbol] = (1 - self.of_lambda) * prev + self.of_lambda * sign

        if trade.symbol in self.calibrators:
            mid = data_cache.get_mark_price(trade.symbol)
            self.calibrators[trade.symbol].on_market_trade(trade, mid)

    def on_order(self, snapshot: OrderStateSnapshot):
        super().on_order(snapshot)
        # 订单终结时清理 Quote State
        if snapshot.status in ["FILLED", "CANCELLED", "REJECTED", "EXPIRED"]:
            symbol = snapshot.symbol
            state = self.quote_state[symbol]
            if state["bid_oid"] == snapshot.client_oid:
                state["bid_oid"] = None
                state["bid_price"] = None
            if state["ask_oid"] == snapshot.client_oid:
                state["ask_oid"] = None
                state["ask_price"] = None

    def on_trade(self, trade: TradeData):
        # [修复] 记录成交时间
        self.last_fill_time[trade.symbol] = time.time()