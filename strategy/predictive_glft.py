# file: strategy/predictive_glft.py
# ============================================================
# BUG FIXES:
#
# [FIX-5] pred_conf["fee_threshold_bps"]: 2.0 → 7.0
#
#   原问题：2.0 bps 仅等于 USDT 合约单边 Maker 费，完全未覆盖往返成本(4 bps)。
#           在 USDC Maker-0 合约中，往返 Maker 成本为 0，
#           但 fee_threshold 的语义应为「信号收益 > 逆向选择风险 + buffer」，
#           推荐值 = 逆向选择 buffer(3~4 bps) + 安全余量(3 bps) = 6~7 bps。
#           用 7 bps（保守）。
#
# [FIX-6] gamma_mult 下限: 0.2 → 0.4
#
#   原问题：当 confidence=1.0, beta=2.0 时：
#           gamma_mult = max(0.2, 1 - 2.0*1.0) = max(0.2, -1.0) = 0.2
#           gamma 缩减到 20%，在高波动时价差极窄，远低于任何逆向选择成本。
#           Maker-0 合约下，价差被压到 ~1 bps 时，单次逆向选择即可吞噬
#           数十次正常成交的利润。
#   修复：将下限提升到 0.4，确保最小价差不低于逆向选择 buffer。
# ============================================================

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
        self.cycle_interval = 0.5

        # --- 预测控制参数 ---
        self.pred_conf = {
            "prediction_horizon_sec": 5.0,

            # [FIX-5] fee_threshold_bps: 2.0 → 7.0
            #   旧值 2.0 = USDT 合约单边 Maker 费，未覆盖往返成本或逆向选择。
            #   新值 7.0 = 逆向选择 buffer(4 bps) + 安全余量(3 bps)。
            #   对 USDC Maker-0 合约同样适用：0 费率不代表 0 逆向选择风险。
            "fee_threshold_bps": 7.0,

            # [FIX-6] gamma_mult 下限在 _execute_mm 中控制，此处保留 beta
            "gamma_beta": 2.0,

            "max_pos_usdt": 3000.0
        }

        # --- 组件 ---
        self.feature_engine = FeatureEngine()
        self.calibrators = {}
        self.models = {}
        self.last_run_times = defaultdict(float)
        self.last_prediction = defaultdict(lambda: {"val": 0.0, "time": 0})
        self.quote_state = defaultdict(lambda: {
            "bid_oid": None, "ask_oid": None,
            "bid_price": None, "ask_price": None,
            "last_update": 0
        })
        self.cooldown_ms = 200

        print(f"[{self.name}] 预测型 GLFT 已启动. "
              f"fee_threshold={self.pred_conf['fee_threshold_bps']} bps")

    # ----------------------------------------------------------

    def _load_strategy_config(self):
        try:
            import json
            with open("config.json", "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _get_components(self, symbol):
        if symbol not in self.calibrators:
            self.calibrators[symbol] = GLFTCalibrator(window=1000)
            self.models[symbol] = OnlineRidgePredictor(
                num_features=9, lambda_reg=0.5
            )
        return self.calibrators[symbol], self.models[symbol]

    def _calculate_safe_vol(self, symbol, price):
        info = ref_data_manager.get_info(symbol)
        if not info:
            return 0.0
        min_vol = max(info.min_qty, (info.min_notional * 1.1) / price)
        return ref_data_manager.round_qty(symbol, min_vol * self.lot_multiplier)

    # ----------------------------------------------------------
    # 核心 Tick 逻辑
    # ----------------------------------------------------------
    def on_orderbook(self, ob: OrderBook):
        symbol = ob.symbol
        calibrator, model = self._get_components(symbol)

        calibrator.on_orderbook(ob)
        self.feature_engine.on_orderbook(ob)

        now = time.time()
        if now - self.last_run_times[symbol] < self.cycle_interval:
            return
        self.last_run_times[symbol] = now

        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 == 0:
            return
        mid = (bid_1 + ask_1) / 2.0

        sigma_bps = max(1.0, calibrator.sigma_bps)

        # --------------------------------------------------------
        # Layer 1: 原始预测
        # --------------------------------------------------------
        features = self.feature_engine.get_features(symbol)
        raw_pred_bps = model.update_and_predict(features, mid)
        self.last_prediction[symbol] = {"val": raw_pred_bps, "time": now}
        pred_bps = raw_pred_bps

        # --------------------------------------------------------
        # Layer 5: 费率门槛过滤 [FIX-5]
        #   只有预测幅度超过 fee_threshold_bps，才视为有效 alpha 信号。
        #   低于门槛 → 置零，策略退化为纯中性做市，避免带噪音偏移报价。
        # --------------------------------------------------------
        if abs(pred_bps) < self.pred_conf["fee_threshold_bps"]:
            pred_bps = 0.0

        # --------------------------------------------------------
        # Layer 3: 库存耦合
        # --------------------------------------------------------
        current_pos = self.oms.exposure.net_positions.get(symbol, 0.0)
        order_vol = self._calculate_safe_vol(symbol, mid)
        if order_vol <= 0:
            return

        max_pos_val = self.pred_conf["max_pos_usdt"]
        current_pos_val = current_pos * mid
        inventory_ratio = max(-1.0, min(1.0, current_pos_val / max_pos_val))
        adjusted_pred_bps = pred_bps * (1.0 - abs(inventory_ratio))

        # --------------------------------------------------------
        # Layer 2: 置信度计算
        # --------------------------------------------------------
        confidence = min(1.0, abs(adjusted_pred_bps) / sigma_bps)
        fair_mid = mid * (1.0 + (confidence * adjusted_pred_bps) / 10000.0)

        # --------------------------------------------------------
        # Layer 6: 预测控制 Gamma [FIX-6]
        # --------------------------------------------------------
        beta = self.pred_conf["gamma_beta"]

        # [FIX-6] gamma_mult 下限从 0.2 → 0.4
        #   confidence=1.0, beta=2.0 时：1 - 2*1 = -1 → 原来 max(0.2,...) 取 0.2
        #   新下限 0.4 确保 gamma 最多缩减到 40%，维持最小价差覆盖逆向选择。
        gamma_mult = max(0.4, 1.0 - beta * confidence)
        gamma_final = self.base_gamma * gamma_mult

        # 资金占用硬风控
        acc = self.oms.account
        if acc.equity > 0:
            usage = acc.used_margin / acc.equity
            if usage > 0.6:
                gamma_final *= 2.0

        # --------------------------------------------------------
        # GLFT 核心计算
        # --------------------------------------------------------
        A = max(0.1, calibrator.A)
        k = max(0.1, calibrator.k)
        q_norm = current_pos / order_vol

        half_spread_bps = (1.0 / gamma_final) * math.log(1.0 + gamma_final / k)
        skew_bps = q_norm * (gamma_final * (sigma_bps ** 2)) / (2 * A * k)

        spread_price = mid * (half_spread_bps / 10000.0)
        skew_price = mid * (skew_bps / 10000.0)  # 量纲与 predictive 保持一致

        target_bid = fair_mid - spread_price - skew_price
        target_ask = fair_mid + spread_price - skew_price

        # --------------------------------------------------------
        # 安全钳与规整
        # --------------------------------------------------------
        info = ref_data_manager.get_info(symbol)
        tick = info.tick_size

        min_half = mid * (5.0 / 20000.0)
        if (target_ask - target_bid) < min_half * 2:
            center = (target_bid + target_ask) / 2
            target_bid = center - min_half
            target_ask = center + min_half

        target_bid = ref_data_manager.round_price(symbol, target_bid)
        target_ask = ref_data_manager.round_price(symbol, target_ask)

        if target_bid >= ask_1:
            target_bid = ask_1 - tick
        if target_ask <= bid_1:
            target_ask = bid_1 + tick
        if target_bid >= target_ask:
            target_bid = mid - tick
            target_ask = mid + tick

        # --------------------------------------------------------
        # 执行与反馈
        # --------------------------------------------------------
        self._update_quotes(symbol, target_bid, target_ask, order_vol)

        strat_data = StrategyData(
            symbol=symbol,
            fair_value=fair_mid,
            alpha_bps=adjusted_pred_bps,
            gamma=gamma_final,
            k=k, A=A, sigma=sigma_bps
        )
        self.engine.put(Event(EVENT_STRATEGY_UPDATE, strat_data))
        self.feature_engine.reset_interval(symbol)

    def _update_quotes(self, symbol, bid, ask, volume):
        state = self.quote_state[symbol]
        info = ref_data_manager.get_info(symbol)
        tick = info.tick_size
        now = time.time()

        if (now - state["last_update"]) * 1000 < self.cooldown_ms:
            return

        if state["bid_price"] is None or abs(bid - state["bid_price"]) >= tick:
            if state["bid_oid"]:
                self.oms.cancel_order(state["bid_oid"])
            oid = self.send_intent(
                OrderIntent(self.name, symbol, Side.BUY, bid, volume,
                            is_post_only=True)
            )
            if oid:
                state["bid_oid"] = oid
                state["bid_price"] = bid

        if state["ask_price"] is None or abs(ask - state["ask_price"]) >= tick:
            if state["ask_oid"]:
                self.oms.cancel_order(state["ask_oid"])
            oid = self.send_intent(
                OrderIntent(self.name, symbol, Side.SELL, ask, volume,
                            is_post_only=True)
            )
            if oid:
                state["ask_oid"] = oid
                state["ask_price"] = ask

        state["last_update"] = now

    def on_market_trade(self, trade: AggTradeData):
        _, model = self._get_components(trade.symbol)
        self.feature_engine.on_trade(trade)
        mid = data_cache.get_mark_price(trade.symbol)
        if trade.symbol in self.calibrators:
            self.calibrators[trade.symbol].on_market_trade(trade, mid)