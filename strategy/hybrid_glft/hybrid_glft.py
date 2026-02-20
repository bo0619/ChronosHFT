# file: strategy/hybrid_glft/hybrid_glft.py
# [FIX-A] on_order: Enum 比较（非字符串）
# [FIX-B] _execute_mm: per-symbol 持仓，从 OMS 直接读取
# [FIX-C] _execute_mm: q_norm 用 max_pos_vol 归一化，有界 [-1,1]
# [FIX-D] on_orderbook: 动量模式仓位硬上限
# [FIX-E] _update_quotes: 买卖侧独立冷却时间戳
# [FIX-F] _update_quotes: is_post_only=True
# [FIX-G] _update_quotes: cancelling 守卫消灭撤单竞态

import json
import math
import time
from collections import defaultdict
from datetime import datetime

from ..base import StrategyTemplate
from event.type import (
    OrderBook, TradeData, OrderIntent, Side, AggTradeData,
    OrderStateSnapshot, OrderStatus, Event, EVENT_STRATEGY_UPDATE, StrategyData,
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

        # ── 读取 config ──────────────────────────────────────
        try:
            with open("config.json") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}

        sc   = cfg.get("strategy", {})
        glft = sc.get("glft", {})
        asym = sc.get("asymmetric_sizing", {})
        fund = sc.get("funding", {})
        mom  = sc.get("momentum", {})
        exe  = sc.get("execution", {})

        # ── 组件 ─────────────────────────────────────────────
        self.feature_engine  = FeatureEngine()
        self.calibrators     = {}
        self.ml_predictors   = {}
        self.trend_detectors = {}
        self.mode_selector   = HybridModeSelector(
            threshold=mom.get("threshold", 0.55)
        )
        self.last_run_times  = defaultdict(float)

        # ── quote_state（每个 symbol 独立）───────────────────
        self.quote_state = defaultdict(lambda: {
            "bid_oid":         None,
            "ask_oid":         None,
            "bid_price":       None,
            "ask_price":       None,
            "bid_last_update": 0.0,
            "ask_last_update": 0.0,
            "bid_cancelling":  False,   # [FIX-G]
            "ask_cancelling":  False,   # [FIX-G]
        })

        # [FIX-6] 部分成交剩余量，供下一张单继承
        self.partial_remaining: dict = defaultdict(
            lambda: {"bid": 0.0, "ask": 0.0}
        )

        self.last_mode    = defaultdict(lambda: "MARKET_MAKING")
        self.last_ml_pred = defaultdict(lambda: None)

        # ── GLFT 基础参数（全从 config）──────────────────────
        self.cycle_interval       = sc.get("cycle_interval",  0.5)
        self.base_gamma           = glft.get("base_gamma",    0.1)
        self.min_spread_bps       = glft.get("min_spread_bps", 5.0)
        self.COLDSTART_GAMMA_MULT = glft.get("coldstart_gamma_mult", 3.0)
        self.max_pos_usdt         = glft.get("max_pos_usdt",  3000.0)

        # ── [FIX-1] 非对称挂单量参数 ─────────────────────────
        self.asym_enabled   = asym.get("enabled",        True)
        self.asym_min_ratio = asym.get("min_vol_ratio",   0.1)
        self.asym_max_ratio = asym.get("max_vol_ratio",   3.0)

        # ── [FIX-2] 资金费率参数 ──────────────────────────────
        self.fund_enabled        = fund.get("enabled",            True)
        self.fund_max_adj_bps    = fund.get("max_adjustment_bps", 20.0)
        self.fund_urgency_window = fund.get("urgency_window_sec", 1800.0)

        # ── 动量参数 ──────────────────────────────────────────
        self.mom_alpha_bps        = mom.get("alpha_bps",            10.0)
        self.mom_gamma_mult       = mom.get("gamma_mult",            0.5)
        self.mom_pos_cap_ratio    = mom.get("pos_cap_ratio",         0.8)
        self.mom_overweight_gmult = mom.get("overweight_gamma_mult", 2.0)

        # ── 执行参数 ──────────────────────────────────────────
        self.cooldown_ms          = exe.get("cooldown_ms",              200)
        self.cancel_guard_timeout = exe.get("cancel_guard_timeout_sec", 3.0)

    # ----------------------------------------------------------
    # 内部工具
    # ----------------------------------------------------------

    def _ensure_symbol(self, symbol: str):
        if symbol not in self.calibrators:
            self.calibrators[symbol]     = GLFTCalibrator(window=1000)
            self.trend_detectors[symbol] = TrendDetector()
            self.ml_predictors[symbol]   = MLTrendPredictor(enabled=True)
            self.feature_engine.on_orderbook(
                OrderBook(symbol, "INIT", datetime.now())
            )

    def _calculate_safe_vol(self, symbol: str, price: float) -> float:
        info = ref_data_manager.get_info(symbol)
        if not info:
            return 0.0
        min_vol = max(info.min_qty, (info.min_notional * 1.2) / price)
        return ref_data_manager.round_qty(symbol, min_vol)

    # ----------------------------------------------------------
    # [FIX-2] 资金费率调整
    # ----------------------------------------------------------

    def _calc_funding_adj_bps(self, symbol: str, q_norm: float) -> float:
        """
        将 funding_rate 折算为对 fair_mid 的 bps 调整量。

        多头(q_norm>0) + 正 funding → 调整为负 → fair_mid 下移
          → ask 变便宜 → 更多人愿意买入 → 帮做市商减多头
        urgency：距离下次结算越近，调整越强。
        """
        if not self.fund_enabled:
            return 0.0

        mp = data_cache.mark_prices.get(symbol)
        if mp is None:
            return 0.0

        funding_rate    = mp.funding_rate
        time_to_funding = max(0.0, mp.next_funding_time.timestamp() - time.time())
        urgency = max(0.0, (self.fund_urgency_window - time_to_funding)
                     / self.fund_urgency_window)

        raw_adj = -funding_rate * 10000.0 * urgency * q_norm
        return max(-self.fund_max_adj_bps, min(self.fund_max_adj_bps, raw_adj))

    # ----------------------------------------------------------
    # [FIX-1] 非对称挂单量
    # ----------------------------------------------------------

    def _calc_asymmetric_vols(self, symbol: str, price: float,
                               q_norm: float) -> tuple:
        """
        q_norm > 0（多头偏重）：bid 量缩，ask 量扩。
        q_norm < 0（空头偏重）：ask 量缩，bid 量扩。
        返回 (bid_vol, ask_vol)，均满足最小名义价值。
        """
        base_vol = self._calculate_safe_vol(symbol, price)
        if base_vol <= 0:
            return 0.0, 0.0

        if not self.asym_enabled:
            return base_vol, base_vol

        lo, hi = self.asym_min_ratio, self.asym_max_ratio

        bid_ratio = max(lo, min(hi, 1.0 - q_norm))
        ask_ratio = max(lo, min(hi, 1.0 + q_norm))

        bid_vol = max(ref_data_manager.round_qty(symbol, base_vol * bid_ratio),
                      base_vol)
        ask_vol = max(ref_data_manager.round_qty(symbol, base_vol * ask_ratio),
                      base_vol)
        return bid_vol, ask_vol

    # ----------------------------------------------------------
    # 行情主循环
    # ----------------------------------------------------------

    def on_orderbook(self, ob: OrderBook):
        symbol = ob.symbol
        self._ensure_symbol(symbol)

        self.calibrators[symbol].on_orderbook(ob)
        self.feature_engine.on_orderbook(ob)
        self.trend_detectors[symbol].on_orderbook(ob)

        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 == 0:
            return
        mid = (bid_1 + ask_1) / 2.0

        feats = self.trend_detectors[symbol].get_features()
        now   = time.time()

        self.ml_predictors[symbol].add_tick(feats, mid, now)

        if now - self.last_run_times[symbol] < self.cycle_interval:
            return
        self.last_run_times[symbol] = now

        # 冷启动保护
        if not self.calibrators[symbol].is_warmed_up:
            self.last_mode[symbol] = "COLDSTART"
            self._execute_mm(symbol, ob, mid,
                             alpha_bps=0.0,
                             gamma_mult=self.COLDSTART_GAMMA_MULT)
            return

        # 模式选择
        rule_sig = self.trend_detectors[symbol].compute_trend_signal()
        ml_pred  = self.ml_predictors[symbol].predict(feats, now)
        self.last_ml_pred[symbol] = ml_pred
        mode_obj = self.mode_selector.select_mode(rule_sig, ml_pred)
        self.last_mode[symbol] = mode_obj.mode

        # 模式分发
        if mode_obj.mode == "MARKET_MAKING":
            self._execute_mm(symbol, ob, mid, alpha_bps=0.0, gamma_mult=1.0)

        elif mode_obj.mode == "MOMENTUM_BUY":
            pos_usdt = self.oms.exposure.net_positions.get(symbol, 0.0) * mid
            if pos_usdt >= self.max_pos_usdt * self.mom_pos_cap_ratio:
                self._execute_mm(symbol, ob, mid,
                                 alpha_bps=0.0,
                                 gamma_mult=self.mom_overweight_gmult)
            else:
                self._execute_mm(symbol, ob, mid,
                                 alpha_bps=self.mom_alpha_bps,
                                 gamma_mult=self.mom_gamma_mult)

        elif mode_obj.mode == "MOMENTUM_SELL":
            pos_usdt = self.oms.exposure.net_positions.get(symbol, 0.0) * mid
            if pos_usdt <= -(self.max_pos_usdt * self.mom_pos_cap_ratio):
                self._execute_mm(symbol, ob, mid,
                                 alpha_bps=0.0,
                                 gamma_mult=self.mom_overweight_gmult)
            else:
                self._execute_mm(symbol, ob, mid,
                                 alpha_bps=-self.mom_alpha_bps,
                                 gamma_mult=self.mom_gamma_mult)

    # ----------------------------------------------------------
    # GLFT 核心定价
    # ----------------------------------------------------------

    def _execute_mm(self, symbol: str, ob: OrderBook, mid: float,
                    alpha_bps: float = 0.0, gamma_mult: float = 1.0):

        calibrator = self.calibrators[symbol]
        sigma = max(1.0, calibrator.sigma_bps)
        A     = max(0.1, calibrator.A)
        k     = max(0.1, calibrator.k)
        gamma = self.base_gamma * gamma_mult

        # [FIX-B] per-symbol 净持仓
        current_pos = self.oms.exposure.net_positions.get(symbol, 0.0)

        # [FIX-C] 有界 q_norm
        max_pos_vol = self.max_pos_usdt / mid if mid > 0 else 1.0
        q_norm      = max(-1.0, min(1.0, current_pos / max_pos_vol))

        # [FIX-2] funding 调整叠加到 alpha
        total_alpha_bps = alpha_bps + self._calc_funding_adj_bps(symbol, q_norm)
        fair_mid = mid * (1.0 + total_alpha_bps / 10000.0)

        # GLFT 公式
        base_half_spread_bps = (1.0 / gamma) * math.log(1.0 + gamma / k)
        inventory_skew_bps   = q_norm * (gamma * sigma ** 2) / (2.0 * A * k)

        spread_price = mid * (base_half_spread_bps / 10000.0)
        skew_price   = mid * (inventory_skew_bps   / 10000.0)

        target_bid = fair_mid - spread_price - skew_price
        target_ask = fair_mid + spread_price - skew_price

        # 安全钳
        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        info = ref_data_manager.get_info(symbol)
        tick = info.tick_size

        min_half = mid * (self.min_spread_bps / 20000.0)
        if (target_ask - target_bid) < min_half * 2:
            center     = (target_bid + target_ask) / 2.0
            target_bid = center - min_half
            target_ask = center + min_half

        target_bid = ref_data_manager.round_price(symbol, target_bid)
        target_ask = ref_data_manager.round_price(symbol, target_ask)

        if target_bid >= ask_1: target_bid = ask_1 - tick
        if target_ask <= bid_1: target_ask = bid_1 + tick
        if target_bid >= target_ask:
            target_bid = mid - tick
            target_ask = mid + tick

        # [FIX-1] 非对称量
        bid_vol, ask_vol = self._calc_asymmetric_vols(symbol, mid, q_norm)
        if bid_vol <= 0 or ask_vol <= 0:
            return

        # 状态广播
        predictor = self.ml_predictors[symbol]
        ml_stats  = predictor.get_stats()
        ml_pred   = self.last_ml_pred[symbol]

        dynamic_params = {
            "γ": f"{gamma:.2f}",
            "k": f"{k:.2f}",
            "A": f"{A:.2f}",
            "σ": f"{sigma:.1f}",
            "Mode": self.last_mode[symbol],
            "P_Trd": f"{ml_pred.p_trend:.2f}" if ml_pred else "0.5",
            "N": ml_stats["n_samples"],
            "Clf_Weights": ml_stats["clf_weights"], # 这是一个 list，Dashboard 会自动美化
            "Reg_Weights": ml_stats["reg_weights"]  # 这是一个 list
        }

        # 发送通用数据包
        self.engine.put(Event(EVENT_STRATEGY_UPDATE, StrategyData(
            symbol=symbol,
            fair_value=fair_mid,
            alpha_bps=total_alpha_bps,
            params=dynamic_params  # 所有的特有参数都塞进这里
        )))
        self._update_quotes(symbol, target_bid, target_ask, bid_vol, ask_vol)

    # ----------------------------------------------------------
    # 报价管理
    # ----------------------------------------------------------

    def _update_quotes(self, symbol: str,
                       bid: float, ask: float,
                       bid_vol: float, ask_vol: float):
        """
        [FIX-E] 买卖侧独立冷却
        [FIX-F] is_post_only=True
        [FIX-G] cancelling 守卫
        [FIX-1] bid_vol/ask_vol 由调用方非对称传入
        [FIX-6] 挂新单时继承部分成交剩余量
        """
        state = self.quote_state[symbol]
        info  = ref_data_manager.get_info(symbol)
        tick  = info.tick_size
        now   = time.time()
        cd    = self.cooldown_ms

        # ── Bid 侧 ────────────────────────────────────────────
        if (now - state["bid_last_update"]) * 1000 >= cd:

            if state["bid_cancelling"]:
                if now - state["bid_last_update"] > self.cancel_guard_timeout:
                    self.log(f"[WARN] {symbol} bid cancel guard timeout, force reset")
                    state["bid_cancelling"] = False
                    state["bid_oid"]        = None
                    state["bid_price"]      = None

            if not state["bid_cancelling"]:
                if state["bid_price"] is None or abs(bid - state["bid_price"]) >= tick:
                    if state["bid_oid"]:
                        self.cancel_order(state["bid_oid"])
                        state["bid_cancelling"]  = True
                        state["bid_last_update"] = now
                    else:
                        # [FIX-6] 继承部分成交剩余量
                        rem       = self.partial_remaining[symbol]["bid"]
                        final_vol = ref_data_manager.round_qty(
                            symbol, max(bid_vol, rem) if rem > 0 else bid_vol
                        )
                        oid = self.send_intent(OrderIntent(
                            strategy_id=self.name, symbol=symbol,
                            side=Side.BUY, price=bid, volume=final_vol,
                            is_post_only=True,
                        ))
                        if oid:
                            state["bid_oid"]         = oid
                            state["bid_price"]       = bid
                            state["bid_last_update"] = now
                            self.partial_remaining[symbol]["bid"] = 0.0

        # ── Ask 侧 ────────────────────────────────────────────
        if (now - state["ask_last_update"]) * 1000 >= cd:

            if state["ask_cancelling"]:
                if now - state["ask_last_update"] > self.cancel_guard_timeout:
                    self.log(f"[WARN] {symbol} ask cancel guard timeout, force reset")
                    state["ask_cancelling"] = False
                    state["ask_oid"]        = None
                    state["ask_price"]      = None

            if not state["ask_cancelling"]:
                if state["ask_price"] is None or abs(ask - state["ask_price"]) >= tick:
                    if state["ask_oid"]:
                        self.cancel_order(state["ask_oid"])
                        state["ask_cancelling"]  = True
                        state["ask_last_update"] = now
                    else:
                        rem       = self.partial_remaining[symbol]["ask"]
                        final_vol = ref_data_manager.round_qty(
                            symbol, max(ask_vol, rem) if rem > 0 else ask_vol
                        )
                        oid = self.send_intent(OrderIntent(
                            strategy_id=self.name, symbol=symbol,
                            side=Side.SELL, price=ask, volume=final_vol,
                            is_post_only=True,
                        ))
                        if oid:
                            state["ask_oid"]         = oid
                            state["ask_price"]       = ask
                            state["ask_last_update"] = now
                            self.partial_remaining[symbol]["ask"] = 0.0

    # ----------------------------------------------------------
    # 事件回调
    # ----------------------------------------------------------

    def on_market_trade(self, trade: AggTradeData):
        self._ensure_symbol(trade.symbol)
        self.feature_engine.on_trade(trade)
        if trade.symbol in self.calibrators:
            mid = data_cache.get_mark_price(trade.symbol)
            self.calibrators[trade.symbol].on_market_trade(trade, mid)

    def on_order(self, snapshot: OrderStateSnapshot):
        """
        [FIX-A] Enum 比较。
        [FIX-G] 终结状态清 cancelling 标志。
        [FIX-6] PARTIALLY_FILLED 记录剩余量。
        """
        super().on_order(snapshot)

        symbol = snapshot.symbol
        state  = self.quote_state[symbol]

        # [FIX-6] 部分成交：记录剩余，单子仍活跃，不清 oid
        if snapshot.status == OrderStatus.PARTIALLY_FILLED:
            remaining = max(0.0, snapshot.volume - snapshot.filled_volume)
            if state["bid_oid"] == snapshot.client_oid:
                self.partial_remaining[symbol]["bid"] = remaining
            elif state["ask_oid"] == snapshot.client_oid:
                self.partial_remaining[symbol]["ask"] = remaining
            return

        # [FIX-A] 终结状态用 Enum 集合比较
        terminal = {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }

        if snapshot.status in terminal:
            if state["bid_oid"] == snapshot.client_oid:
                state["bid_oid"]        = None
                state["bid_price"]      = None
                state["bid_cancelling"] = False          # [FIX-G]
                self.partial_remaining[symbol]["bid"] = 0.0

            if state["ask_oid"] == snapshot.client_oid:
                state["ask_oid"]        = None
                state["ask_price"]      = None
                state["ask_cancelling"] = False          # [FIX-G]
                self.partial_remaining[symbol]["ask"] = 0.0

    def on_trade(self, trade: TradeData):
        self.log(
            f"FILL: {trade.symbol} {trade.side} "
            f"{trade.volume} @ {trade.price}"
        )