# file: strategy/hybrid_glft/hybrid_glft.py
#
# ============================================================
# BUG FIX CHANGELOG
#
# [FIX-A] on_order(): 字符串 vs Enum 类型比较错误
#   原代码: snapshot.status in ["FILLED", "CANCELLED", ...]
#   问题:   snapshot.status 是 OrderStatus Enum，Python中 Enum != str，
#           导致 quote_state 的 bid_oid/ask_oid 永远不会被清空。
#           后果：每个 cycle 都对着已成交/已撤销的 oid 重复发撤单请求，
#           日志中产生大量垃圾撤单，且可能双边重复下单。
#   修复:   改为 OrderStatus.FILLED / OrderStatus.CANCELLED 等 Enum 比较。
#
# [FIX-B] _execute_mm(): self.pos 多标的污染
#   原代码: current_pos = self.pos
#   问题:   self.pos 是基类 StrategyTemplate 中的一个标量 float，
#           所有 symbol 的持仓推送都会无差别覆盖它（最后一个 symbol 获胜）。
#           多标的场景下（如同时交易 BTC/ETH），库存偏斜方向完全不可靠。
#   修复:   直接从 OMS ExposureManager 读取 per-symbol 净持仓，
#           与 GLFTStrategy 的正确写法保持一致。
#
# [FIX-C] _execute_mm(): q_norm 归一化分母错误
#   原代码: q_norm = current_pos / order_vol
#   问题:   order_vol 是最小下单量（如 0.001 BTC），用它做分母，
#           持仓稍多时 q_norm 就爆炸（如 0.05 / 0.001 = 50），
#           导致 inventory_skew_bps 极大，报价严重偏离盘口，双边报价实际失效。
#   修复:   引入 max_pos_usdt 参数作为最大仓位容量，计算 max_pos_vol，
#           q_norm = current_pos / max_pos_vol，确保 q_norm ∈ [-1, 1]。
#
# [FIX-D] on_orderbook(): 动量模式缺少仓位硬上限
#   原代码: MOMENTUM_BUY → gamma_mult=0.5（直接削弱库存回归力）
#   问题:   动量模式同时做两件相反的事：
#           1. alpha 偏移使报价向有利方向移动（鼓励继续加仓）
#           2. gamma_mult=0.5 使库存偏斜减半（削弱减仓驱动力）
#           当 ML 连续判断同一方向时，仓位可无界单边积累。
#   修复:   进入动量模式前，先检查当前仓位名义价值是否超过阈值（max_pos_usdt * 0.8）。
#           超限则强制切换为保守做市（gamma_mult=2.0），不执行动量策略。
#
# [FIX-E] _update_quotes(): 买卖侧共享同一个 last_update 时间戳
#   原代码: 任意一侧更新后 state["last_update"] = now，200ms内整个函数 return
#   问题:   bid侧刚更新，ask侧急需调整时，被整体 return 拦住，
#           ask 报价在错误位置停留最多 200ms。
#   修复:   为买卖侧各自维护独立的 last_update 时间戳。
# ============================================================

import time
import math
from collections import defaultdict
from datetime import datetime

from ..base import StrategyTemplate
from event.type import (
    OrderBook, TradeData, OrderIntent, Side, AggTradeData,
    OrderStateSnapshot, OrderStatus, Event, EVENT_STRATEGY_UPDATE, StrategyData
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

        self.feature_engine  = FeatureEngine()
        self.calibrators     = {}
        self.ml_predictors   = {}
        self.trend_detectors = {}
        self.mode_selector   = HybridModeSelector(threshold=0.55)
        self.last_run_times  = defaultdict(float)

        # [FIX-E] 买卖侧各自独立的时间戳
        self.quote_state = defaultdict(lambda: {
            "bid_oid":         None,
            "ask_oid":         None,
            "bid_price":       None,
            "ask_price":       None,
            "bid_last_update": 0.0,   # [FIX-E] 原 last_update 拆分
            "ask_last_update": 0.0,   # [FIX-E]
        })

        # per-symbol 状态，供 UI 广播读取
        self.last_mode   = defaultdict(lambda: "MARKET_MAKING")
        self.last_ml_pred = defaultdict(lambda: None)

        # ── 策略参数 ──────────────────────────────────────────
        self.cycle_interval      = 0.5
        self.base_gamma          = 0.1
        self.min_spread_bps      = 5.0
        self.COLDSTART_GAMMA_MULT = 3.0

        # [FIX-C] 最大仓位名义价值（USDT），作为 q_norm 的归一化分母容量
        # 调整此值可控制库存偏斜的灵敏度：
        #   值越小 → 稍有仓位偏斜就很重 → 库存回归更激进
        #   值越大 → 需要更大仓位才触发明显偏斜 → 策略更平滑
        self.max_pos_usdt = 3000.0

        # [FIX-D] 动量模式触发的仓位上限（占 max_pos_usdt 的比例）
        # 超过此比例时，即使 ML 判断有方向，也不执行动量，转为保守做市
        self.momentum_pos_cap_ratio = 0.8

    # ----------------------------------------------------------
    # 内部工具
    # ----------------------------------------------------------

    def _ensure_symbol(self, symbol: str):
        """懒初始化：首次收到某 symbol 行情时创建相关组件"""
        if symbol not in self.calibrators:
            self.calibrators[symbol]     = GLFTCalibrator(window=1000)
            self.trend_detectors[symbol] = TrendDetector()
            self.ml_predictors[symbol]   = MLTrendPredictor(enabled=True)
            # 用空快照热身 FeatureEngine，避免第一次计算时出现 None
            self.feature_engine.on_orderbook(OrderBook(symbol, "INIT", datetime.now()))

    def _calculate_safe_vol(self, symbol: str, price: float) -> float:
        """计算符合交易所最小限制的挂单量"""
        info = ref_data_manager.get_info(symbol)
        if not info:
            return 0.0
        min_vol = max(info.min_qty, (info.min_notional * 1.2) / price)
        return ref_data_manager.round_qty(symbol, min_vol)

    # ----------------------------------------------------------
    # 行情驱动主循环
    # ----------------------------------------------------------

    def on_orderbook(self, ob: OrderBook):
        symbol = ob.symbol
        self._ensure_symbol(symbol)

        # ── 阶段一：每 tick，无门控 ──────────────────────────────
        # 实时更新 Calibrator / FeatureEngine / TrendDetector / ML训练
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

        # 每 tick 训练 ML，最大化样本密度（不受 cycle_interval 限制）
        self.ml_predictors[symbol].add_tick(feats, mid, now)

        # ── 阶段二：频率门控 0.5s ────────────────────────────────
        if now - self.last_run_times[symbol] < self.cycle_interval:
            return
        self.last_run_times[symbol] = now

        # 冷启动保护：Calibrator 还没有足够样本时，用宽松参数做市
        if not self.calibrators[symbol].is_warmed_up:
            self.last_mode[symbol] = "COLDSTART"
            self._execute_mm(symbol, ob, mid,
                             alpha_bps=0.0, gamma_mult=self.COLDSTART_GAMMA_MULT)
            return

        # ── 阶段三：模式选择 ────────────────────────────────────
        rule_sig = self.trend_detectors[symbol].compute_trend_signal()
        ml_pred  = self.ml_predictors[symbol].predict(feats, now)

        self.last_ml_pred[symbol] = ml_pred
        mode_obj = self.mode_selector.select_mode(rule_sig, ml_pred)
        self.last_mode[symbol] = mode_obj.mode

        # ── 阶段四：模式分发 ────────────────────────────────────
        if mode_obj.mode == "MARKET_MAKING":
            self._execute_mm(symbol, ob, mid, alpha_bps=0.0, gamma_mult=1.0)

        elif mode_obj.mode == "MOMENTUM_BUY":
            # [FIX-D] 仓位硬上限检查：多头过重时不追涨，改为保守做市
            current_pos  = self.oms.exposure.net_positions.get(symbol, 0.0)
            pos_usdt     = current_pos * mid  # 多头为正，空头为负
            cap          = self.max_pos_usdt * self.momentum_pos_cap_ratio
            if pos_usdt >= cap:
                # 已经持有足够多头，不再动量加仓，反而要用高 gamma 偏向卖出来减仓
                self._execute_mm(symbol, ob, mid, alpha_bps=0.0, gamma_mult=2.0)
            else:
                self._execute_mm(symbol, ob, mid, alpha_bps=10.0, gamma_mult=0.5)

        elif mode_obj.mode == "MOMENTUM_SELL":
            # [FIX-D] 空头过重时不追跌，改为保守做市
            current_pos  = self.oms.exposure.net_positions.get(symbol, 0.0)
            pos_usdt     = current_pos * mid  # 空头持仓为负
            cap          = self.max_pos_usdt * self.momentum_pos_cap_ratio
            if pos_usdt <= -cap:
                self._execute_mm(symbol, ob, mid, alpha_bps=0.0, gamma_mult=2.0)
            else:
                self._execute_mm(symbol, ob, mid, alpha_bps=-10.0, gamma_mult=0.5)

    # ----------------------------------------------------------
    # GLFT 核心定价与报价执行
    # ----------------------------------------------------------

    def _execute_mm(self, symbol: str, ob: OrderBook, mid: float,
                    alpha_bps: float = 0.0, gamma_mult: float = 1.0):
        """
        根据当前 Calibrator 参数、库存状态、alpha 信号，
        计算目标 bid/ask 价格并提交给 _update_quotes。
        """
        calibrator = self.calibrators[symbol]
        sigma = max(1.0, calibrator.sigma_bps)
        A     = max(0.1, calibrator.A)
        k     = max(0.1, calibrator.k)
        gamma = self.base_gamma * gamma_mult

        # 公允价 = mid 加上 alpha 方向偏移
        fair_mid  = mid * (1.0 + alpha_bps / 10000.0)

        order_vol = self._calculate_safe_vol(symbol, mid)
        if order_vol <= 0:
            return

        # ── [FIX-B] per-symbol 持仓：直接从 OMS ExposureManager 读取 ──
        # 修复前: current_pos = self.pos
        #   self.pos 是所有 symbol 共享的标量，多标的场景下值不可靠。
        # 修复后: 精确读取该 symbol 的净持仓（正=多头，负=空头）
        current_pos = self.oms.exposure.net_positions.get(symbol, 0.0)

        # ── [FIX-C] q_norm 归一化：用 max_pos_vol 而不是 order_vol ──
        # 修复前: q_norm = current_pos / order_vol
        #   order_vol ≈ 0.001 BTC（最小下单量），持仓 0.05 BTC 时 q_norm = 50，爆炸。
        # 修复后: max_pos_vol = max_pos_usdt / mid，使 q_norm 在 [-1, 1] 之间有意义。
        #   当持仓达到 max_pos_usdt 时，q_norm = 1.0，偏斜最大但有界。
        max_pos_vol = self.max_pos_usdt / mid if mid > 0 else 1.0
        q_norm      = current_pos / max_pos_vol  # 有界，通常 ∈ [-1, 1]

        # ── GLFT 公式 ──────────────────────────────────────────
        # base_half_spread_bps: 零库存时的理论半价差
        # inventory_skew_bps:   库存带来的报价偏移（正持仓→ask压低/bid抬高，即偏向卖出）
        base_half_spread_bps = (1.0 / gamma) * math.log(1.0 + gamma / k)
        inventory_skew_bps   = q_norm * (gamma * sigma ** 2) / (2 * A * k)

        spread_price = mid * (base_half_spread_bps / 10000.0)
        skew_price   = mid * (inventory_skew_bps   / 10000.0)

        # 多头持仓时 skew_price > 0:
        #   bid 下移（不鼓励继续买）, ask 下移（鼓励对手方买入来帮我们减仓）
        target_bid = fair_mid - spread_price - skew_price
        target_ask = fair_mid + spread_price - skew_price

        # ── 安全钳与价格规整 ───────────────────────────────────
        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        info = ref_data_manager.get_info(symbol)
        tick = info.tick_size

        # 保证最小价差不低于 min_spread_bps
        min_half = mid * (self.min_spread_bps / 20000.0)
        if (target_ask - target_bid) < min_half * 2:
            center     = (target_bid + target_ask) / 2
            target_bid = center - min_half
            target_ask = center + min_half

        # 四舍五入到交易所 tick
        target_bid = ref_data_manager.round_price(symbol, target_bid)
        target_ask = ref_data_manager.round_price(symbol, target_ask)

        # 防止穿越盘口（bid 不得 >= ask_1，ask 不得 <= bid_1）
        if target_bid >= ask_1:
            target_bid = ask_1 - tick
        if target_ask <= bid_1:
            target_ask = bid_1 + tick

        # 最终保险：bid 必须 < ask
        if target_bid >= target_ask:
            target_bid = mid - tick
            target_ask = mid + tick

        # ── 状态广播到 UI ──────────────────────────────────────
        predictor = self.ml_predictors[symbol]
        ml_stats  = predictor.get_stats()
        ml_pred   = self.last_ml_pred[symbol]

        strat_data = StrategyData(
            symbol=symbol,
            fair_value=fair_mid,
            alpha_bps=alpha_bps,
            gamma=gamma, k=k, A=A, sigma=sigma,
            ml_mode=self.last_mode[symbol],
            ml_p_trend=ml_pred.p_trend if ml_pred else 0.5,
            ml_trained=ml_stats["trained"],
            ml_n_samples=ml_stats["n_samples"],
            ml_buffer_size=ml_stats["buffer_size"],
            ml_clf_weights=ml_stats["clf_weights"],
            ml_reg_weights=ml_stats["reg_weights"],
        )
        self.engine.put(Event(EVENT_STRATEGY_UPDATE, strat_data))

        # ── 执行报价更新 ───────────────────────────────────────
        self._update_quotes(symbol, target_bid, target_ask, order_vol)

    # ----------------------------------------------------------
    # 报价管理（增量改单）
    # ----------------------------------------------------------

    def _update_quotes(self, symbol: str, bid: float, ask: float, volume: float):
        """
        对比新旧报价，只在价格变动超过 1 个 tick 时才撤旧单、挂新单。
        [FIX-E] 买卖侧使用独立的冷却时间，互不干扰。
        """
        state = self.quote_state[symbol]
        info  = ref_data_manager.get_info(symbol)
        tick  = info.tick_size
        now   = time.time()
        cooldown_ms = 200  # 同侧 200ms 内不重复操作

        # ── Bid 侧 ────────────────────────────────────────────
        # [FIX-E] 每侧独立判断冷却，不会因为 ask 侧刚更新而阻塞 bid 侧
        if (now - state["bid_last_update"]) * 1000 >= cooldown_ms:
            if state["bid_price"] is None or abs(bid - state["bid_price"]) >= tick:
                if state["bid_oid"]:
                    self.cancel_order(state["bid_oid"])
                oid = self.buy(symbol, bid, volume)
                if oid:
                    state["bid_oid"]         = oid
                    state["bid_price"]       = bid
                    state["bid_last_update"] = now  # [FIX-E]

        # ── Ask 侧 ────────────────────────────────────────────
        if (now - state["ask_last_update"]) * 1000 >= cooldown_ms:
            if state["ask_price"] is None or abs(ask - state["ask_price"]) >= tick:
                if state["ask_oid"]:
                    self.cancel_order(state["ask_oid"])
                oid = self.sell(symbol, ask, volume)
                if oid:
                    state["ask_oid"]         = oid
                    state["ask_price"]       = ask
                    state["ask_last_update"] = now  # [FIX-E]

    # ----------------------------------------------------------
    # 事件回调
    # ----------------------------------------------------------

    def on_market_trade(self, trade: AggTradeData):
        """公共成交流 → 喂给 FeatureEngine 和 Calibrator"""
        self._ensure_symbol(trade.symbol)
        self.feature_engine.on_trade(trade)
        if trade.symbol in self.calibrators:
            mid = data_cache.get_mark_price(trade.symbol)
            self.calibrators[trade.symbol].on_market_trade(trade, mid)

    def on_order(self, snapshot: OrderStateSnapshot):
        """
        订单状态回调：清理 quote_state，防止对已终结订单重复操作。

        [FIX-A] 原代码用字符串列表做 in 判断：
            snapshot.status in ["FILLED", "CANCELLED", ...]
        但 snapshot.status 是 OrderStatus Enum 类型，Python 中
            OrderStatus.FILLED == "FILLED"  → False
        导致 quote_state 的 bid_oid/ask_oid 永远不清空。
        修复：改用 OrderStatus Enum 成员做比较。
        """
        # 先调用基类（清理 active_orders）
        super().on_order(snapshot)

        # [FIX-A] 使用 Enum 而非字符串
        terminal_statuses = {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }

        if snapshot.status in terminal_statuses:
            state = self.quote_state[snapshot.symbol]

            if state["bid_oid"] == snapshot.client_oid:
                state["bid_oid"]   = None
                state["bid_price"] = None

            if state["ask_oid"] == snapshot.client_oid:
                state["ask_oid"]   = None
                state["ask_price"] = None

    def on_trade(self, trade: TradeData):
        """自身成交回报，记录日志"""
        self.log(
            f"FILL: {trade.symbol} {trade.side} "
            f"{trade.volume} @ {trade.price}"
        )