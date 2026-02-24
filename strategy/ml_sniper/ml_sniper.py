# file: strategy/ml_sniper/ml_sniper.py
#
# 修复记录（相对于原版）：
#
# [FIX-A] 信号权重翻转
#   原版：1s:0.6, 10s:0.3, 30s:0.1 → 1s 主导，信号极度不稳定
#   修复：1s:0.1, 10s:0.5, 30s:0.4 → 10s/30s 主导方向，1s 只做时机微调
#
# [FIX-B] 费率覆盖：止盈目标与入场阈值必须覆盖往返成本
#   原版：taker_threshold=4.0bps, profit_target=3.0bps
#         IOC 入场往返成本约 10bps（单边 5bps），止盈 3bps 必亏
#   修复：taker_threshold=8.0bps, profit_target=8.0bps
#         确保 Taker 路径的期望收益 > 往返成本
#         GTX（Maker）路径零费率，profit_target 保持同值保持一致性
#
# [FIX-C] 强制平仓 slippage 从固定 50bps 改为 ATR 自适应
#   原版：bid_1 * 0.995（固定 -50bps），在低波动品种极度激进
#   修复：bid_1 × (1 - atr_bps × 1.5 / 10000)，与当前市场波动匹配
#
# [FIX-D] feature_engine.reset_interval 移到正确位置
#   原版：每 0.1s tick 都调用 reset_interval，成交流特征几乎不累积
#   修复：仅在 cycle_interval（1.0s）周期末调用，让特征有足够的累积窗口
#         tick 级别的特征更新继续保持 0.1s 频率
#
# [FIX-E] 冷启动保护从时间预热改为卡尔曼训练次数预热
#   原版：固定等待 warmup_duration_sec（默认 300s）
#   修复：等待 predictor.is_warmed_up（三个 horizon 各至少训练一次）
#         + 最短 min_warmup_sec 秒（保证数据量），两者同时满足才开始交易
#         避免刚启动时随机权重驱动真实下单
#
# [FIX-F] ENTERING 状态：入场单被拒后正确回退到 FLAT
#   原版：GTX 被拒时 state 停在 ENTERING，永远无法开仓
#   修复：on_order 里 REJECTED 时若 state==ENTERING 显式回退到 FLAT
#
# [FIX-G] 止盈单价格逻辑修正
#   原版 多仓：max(target, ask_1) → 止盈价至少等于 ask，可能挂在 ask 以上（永远不成交）
#   修复 多仓：max(target, bid_1 + tick_size) → 止盈价至少比 bid 高一个 tick，
#              是合理的 Maker 止盈位置
#   原版 空仓：min(target, bid_1) → 止盈价至多等于 bid，但对空仓来说需要买回
#   修复 空仓：min(target, ask_1 - tick_size) → 止盈价至多比 ask 低一个 tick

import time
from collections import defaultdict
from datetime import datetime

from event.type import (
    OrderBook, TradeData, OrderIntent, Side, AggTradeData,
    OrderStateSnapshot, OrderStatus,
    Event, EVENT_STRATEGY_UPDATE, StrategyData,
)
from ..base import StrategyTemplate
from alpha.engine import FeatureEngine
from alpha.factors import GLFTCalibrator      # 用于 ATR（强制平仓 slippage 自适应）
from data.ref_data import ref_data_manager

from .predictor import TimeHorizonPredictor
from .config_loader import load_sniper_config

# 9 维特征标签（与 FeatureEngine.get_features() 顺序严格对应）
FEATURE_LABELS = ["Imb", "Dep", "Mic", "Trd", "Arr", "Vwp", "dIm", "dSp", "Mom"]


class MLSniperStrategy(StrategyTemplate):
    """
    ML Sniper：三时间尺度卡尔曼滤波趋势跟踪策略。

    FSM 状态机：
      FLAT → ENTERING → HOLDING → EXITING → FLAT

    入场模式：
      信号 > taker_threshold → IOC 入场（Taker，快速确认方向）
      信号 > maker_threshold → GTX 入场（PostOnly Maker，零手续费）

    出场模式：
      止盈单（GTX Maker）：在目标价挂单，等待被动成交
      强制平仓（IOC）：持仓超时 or 信号反转，立即市价出场
    """

    def __init__(self, engine, oms):
        super().__init__(engine, oms, "ML_Sniper_KF")

        # ── 配置 ──────────────────────────────────────────────
        self.strat_conf = load_sniper_config()

        # [FIX-A] 默认权重翻转：10s/30s 主导
        raw_weights = self.strat_conf.get(
            "weights", {"1s": 0.1, "10s": 0.5, "30s": 0.4}
        )
        self.weights = (
            raw_weights if isinstance(raw_weights, dict)
            else {"1s": 0.1, "10s": 0.5, "30s": 0.4}
        )

        self.lot_multiplier = self.strat_conf.get("lot_multiplier", 1.0)

        entry_cfg = self.strat_conf.get("entry", {})
        # [FIX-B] 默认阈值提高，覆盖往返 Taker 成本
        self.taker_entry_threshold = entry_cfg.get("taker_entry_threshold_bps", 8.0)
        self.maker_entry_threshold = entry_cfg.get("maker_entry_threshold_bps", 3.0)

        exit_cfg = self.strat_conf.get("exit", {})
        # [FIX-B] 止盈默认值提高
        self.profit_target  = exit_cfg.get("profit_target_bps",  8.0)
        self.max_hold_sec   = exit_cfg.get("max_holding_sec",    10.0)

        exe_cfg = self.strat_conf.get("execution", {})
        self.tick_interval  = exe_cfg.get("tick_interval_sec",   0.1)
        # [FIX-D] cycle_interval 控制 reset_interval 的调用频率
        self.cycle_interval = exe_cfg.get("cycle_interval_sec",  1.0)

        # [FIX-E] 冷启动：时间 + 卡尔曼训练次数双重保护
        self.min_warmup_sec = self.strat_conf.get("min_warmup_sec", 60.0)
        self.start_time     = time.time()
        self.is_warmed_up   = False

        # ── 组件 ──────────────────────────────────────────────
        self.feature_engine = FeatureEngine()
        self.predictors:  dict = {}   # symbol → TimeHorizonPredictor
        self.calibrators: dict = {}   # symbol → GLFTCalibrator（提供 ATR）

        # ── 运行时状态 ────────────────────────────────────────
        self.state         = defaultdict(lambda: "FLAT")
        self.pos_entry_ts  = defaultdict(float)
        self.entry_price   = defaultdict(float)
        self.entry_oid     = defaultdict(lambda: None)
        self.exit_oid      = defaultdict(lambda: None)
        self.last_tick_ts  = defaultdict(float)
        self.last_cycle_ts = defaultdict(float)   # [FIX-D] 控制 reset_interval

    # ── 延迟初始化 ────────────────────────────────────────────
    def _get_predictor(self, symbol: str) -> TimeHorizonPredictor:
        if symbol not in self.predictors:
            self.predictors[symbol] = TimeHorizonPredictor(num_features=9)
            self.calibrators[symbol] = GLFTCalibrator(window=500)
        return self.predictors[symbol]

    # ── 冷启动检查 ────────────────────────────────────────────
    def _check_warmup(self, symbol: str) -> bool:
        """[FIX-E] 时间 AND 卡尔曼训练次数双重满足才解除冷启动"""
        if self.is_warmed_up:
            return True
        elapsed = time.time() - self.start_time
        if elapsed < self.min_warmup_sec:
            return False
        predictor = self._get_predictor(symbol)
        if not predictor.is_warmed_up:
            return False
        self.is_warmed_up = True
        return True

    # ── 安全下单量 ────────────────────────────────────────────
    def _calc_vol(self, symbol: str, price: float) -> float:
        info = ref_data_manager.get_info(symbol)
        if not info:
            return 0.0
        min_vol = max(info.min_qty, (info.min_notional * 1.1) / price)
        return ref_data_manager.round_qty(symbol, min_vol * self.lot_multiplier)

    # ── tick 大小 ─────────────────────────────────────────────
    def _tick_size(self, symbol: str, mid: float) -> float:
        info = ref_data_manager.get_info(symbol)
        if info and hasattr(info, "tick_size") and info.tick_size > 0:
            return info.tick_size
        return ref_data_manager.round_price(symbol, mid * 0.0001)

    # ── [FIX-C] ATR 自适应 slippage ──────────────────────────
    def _force_exit_slippage(self, symbol: str) -> float:
        """强制平仓的价格偏移比例。基于 ATR，最小 10bps，最大 80bps"""
        atr = getattr(self.calibrators.get(symbol), "sigma_bps", 20.0)
        slippage_bps = float(max(10.0, min(80.0, atr * 1.5)))
        return slippage_bps / 10000.0

    # ─────────────────────────────────────────────────────────
    # 行情事件
    # ─────────────────────────────────────────────────────────

    def on_orderbook(self, ob: OrderBook):
        now = time.time()
        sym = ob.symbol

        # tick 频率限制（特征更新 + 预测仍然保持 0.1s 精度）
        if now - self.last_tick_ts[sym] < self.tick_interval:
            return
        self.last_tick_ts[sym] = now

        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 == 0:
            return
        mid = (bid_1 + ask_1) / 2.0

        # ── 每 tick 更新特征 + 卡尔曼学习 ────────────────────
        self.feature_engine.on_orderbook(ob)
        predictor = self._get_predictor(sym)
        self.calibrators[sym].on_orderbook(ob)

        feats = self.feature_engine.get_features(sym)
        preds = predictor.update_and_predict(feats, mid, now)

        # ── [FIX-D] cycle_interval 周期末重置成交流累积器 ────
        if now - self.last_cycle_ts[sym] >= self.cycle_interval:
            self.last_cycle_ts[sym] = now
            self.feature_engine.reset_interval(sym)

        # ── 信号合成 ──────────────────────────────────────────
        try:
            signal = (
                float(preds.get("1s",  0)) * self.weights.get("1s",  0.1) +
                float(preds.get("10s", 0)) * self.weights.get("10s", 0.5) +
                float(preds.get("30s", 0)) * self.weights.get("30s", 0.4)
            )
        except Exception:
            signal = 0.0

        # ── 冷启动处理 ────────────────────────────────────────
        if not self._check_warmup(sym):
            self._publish_warmup(sym, mid, signal, preds, predictor)
            return

        # ── 正常运行 ──────────────────────────────────────────
        self._publish_state(sym, mid, signal, preds, predictor)
        self._run_fsm(sym, mid, bid_1, ask_1, signal, now)

    def on_market_trade(self, trade: AggTradeData):
        """驱动 FeatureEngine 的成交特征（trade_imbalance / arrival / vwap_drift）"""
        self.feature_engine.on_trade(trade)

    # ─────────────────────────────────────────────────────────
    # FSM 状态机
    # ─────────────────────────────────────────────────────────

    def _run_fsm(self, sym: str, mid: float,
                 bid_1: float, ask_1: float,
                 signal: float, now: float):
        curr_state = self.state[sym]
        net_pos    = self.oms.exposure.net_positions.get(sym, 0.0)

        # ── FLAT：评估入场 ────────────────────────────────────
        if curr_state == "FLAT":
            # 防御：OMS 实际有持仓但策略认为 FLAT → 同步状态
            if abs(net_pos) > 1e-6:
                self.state[sym] = "HOLDING"
                return

            vol = self._calc_vol(sym, mid)
            if vol <= 0:
                return

            if signal > self.maker_entry_threshold:
                if signal > self.taker_entry_threshold:
                    # IOC 入场：价格略高于 ask，确保吃单成交
                    slippage = self._force_exit_slippage(sym)
                    price = ref_data_manager.round_price(sym, ask_1 * (1 + slippage))
                    self._entry(sym, Side.BUY, price, vol, "IOC")
                else:
                    # GTX（PostOnly）入场：挂在 bid，等待被动成交
                    price = ref_data_manager.round_price(sym, bid_1)
                    self._entry(sym, Side.BUY, price, vol, "GTX")

            elif signal < -self.maker_entry_threshold:
                if signal < -self.taker_entry_threshold:
                    slippage = self._force_exit_slippage(sym)
                    price = ref_data_manager.round_price(sym, bid_1 * (1 - slippage))
                    self._entry(sym, Side.SELL, price, vol, "IOC")
                else:
                    price = ref_data_manager.round_price(sym, ask_1)
                    self._entry(sym, Side.SELL, price, vol, "GTX")

        # ── ENTERING：等待入场单成交，信号消失则撤单 ────────
        elif curr_state == "ENTERING":
            oid = self.entry_oid[sym]
            if oid and oid in self.active_orders:
                # 信号强度跌破阈值的一半 → 撤单，回到 FLAT
                if abs(signal) < self.maker_entry_threshold * 0.5:
                    self.cancel_order(oid)
                    # 注：状态回退在 on_order 里处理（CANCELLED → FLAT）

        # ── HOLDING：持仓管理 ─────────────────────────────────
        elif curr_state == "HOLDING":
            # OMS 已无持仓（可能被外部平掉）→ 回 FLAT
            if abs(net_pos) < 1e-6:
                self.state[sym] = "FLAT"
                self._clear_oids(sym)
                return

            holding_time = now - self.pos_entry_ts[sym]
            tick         = self._tick_size(sym, mid)

            # 强制平仓条件
            force_exit = False
            if holding_time > self.max_hold_sec:
                force_exit = True
            if net_pos > 0 and signal < -self.taker_entry_threshold:
                force_exit = True
            if net_pos < 0 and signal > self.taker_entry_threshold:
                force_exit = True

            if force_exit:
                self.cancel_all(sym)
                self.state[sym] = "EXITING"
                slippage = self._force_exit_slippage(sym)
                if net_pos > 0:
                    # [FIX-C] ATR 自适应 slippage
                    price = ref_data_manager.round_price(sym, bid_1 * (1 - slippage))
                    self.exit_long(sym, price, abs(net_pos))
                else:
                    price = ref_data_manager.round_price(sym, ask_1 * (1 + slippage))
                    self.exit_short(sym, price, abs(net_pos))
                return

            # 挂止盈 Maker 单（仅在没有在途出场单时）
            if not self.exit_oid[sym]:
                entry_px = self.entry_price[sym]
                if net_pos > 0:
                    target = entry_px * (1 + self.profit_target / 10000.0)
                    # [FIX-G] 止盈价至少比 bid 高一个 tick（才能做 Maker）
                    price = ref_data_manager.round_price(
                        sym, max(target, bid_1 + tick)
                    )
                    self._place_exit(sym, Side.SELL, price, abs(net_pos))
                else:
                    target = entry_px * (1 - self.profit_target / 10000.0)
                    # [FIX-G] 止盈价至多比 ask 低一个 tick
                    price = ref_data_manager.round_price(
                        sym, min(target, ask_1 - tick)
                    )
                    self._place_exit(sym, Side.BUY, price, abs(net_pos))

        # ── EXITING：等待出场单成交 ───────────────────────────
        elif curr_state == "EXITING":
            if abs(net_pos) < 1e-6:
                self.state[sym] = "FLAT"
                self._clear_oids(sym)

    # ─────────────────────────────────────────────────────────
    # 下单辅助
    # ─────────────────────────────────────────────────────────

    def _entry(self, sym: str, side: Side,
               price: float, vol: float, mode: str):
        """发入场单。mode = "IOC"（Taker）或 "GTX"（PostOnly Maker）"""
        if self.entry_oid[sym]:
            self.cancel_order(self.entry_oid[sym])

        is_ioc      = (mode == "IOC")
        intent = OrderIntent(
            self.name, sym, side, price, vol,
            time_in_force = "IOC" if is_ioc else "GTC",
            is_post_only  = not is_ioc,
        )
        oid = self.send_intent(intent)
        if oid:
            self.entry_oid[sym] = oid
            self.state[sym]     = "ENTERING"

    def _place_exit(self, sym: str, side: Side, price: float, vol: float):
        """挂止盈 Maker 单（PostOnly）"""
        intent = OrderIntent(
            self.name, sym, side, price, vol,
            is_post_only = True,
        )
        oid = self.send_intent(intent)
        if oid:
            self.exit_oid[sym] = oid

    def _clear_oids(self, sym: str):
        self.entry_oid[sym] = None
        self.exit_oid[sym]  = None

    # ─────────────────────────────────────────────────────────
    # 订单事件
    # ─────────────────────────────────────────────────────────

    def on_order(self, snapshot: OrderStateSnapshot):
        super().on_order(snapshot)   # 基类维护 active_orders

        sym    = snapshot.symbol
        oid    = snapshot.client_oid
        status = snapshot.status

        terminal = {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }

        # ── 入场单事件 ────────────────────────────────────────
        if oid == self.entry_oid[sym]:
            if status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                self.state[sym]        = "HOLDING"
                self.pos_entry_ts[sym] = time.time()
                self.entry_price[sym]  = snapshot.avg_price
                if status == OrderStatus.FILLED:
                    self.entry_oid[sym] = None

            elif status in terminal:
                self.entry_oid[sym] = None
                # [FIX-F] ENTERING 被拒 / 取消 → 显式回退 FLAT
                if self.state[sym] == "ENTERING":
                    self.state[sym] = "FLAT"

        # ── 出场单事件 ────────────────────────────────────────
        if oid == self.exit_oid[sym]:
            if status in terminal:
                self.exit_oid[sym] = None
                # 止盈单成交 → 等待 EXITING/HOLDING 里的 net_pos 检查回 FLAT

    def on_trade(self, trade: TradeData):
        pass

    # ─────────────────────────────────────────────────────────
    # Dashboard / UI
    # ─────────────────────────────────────────────────────────

    def _publish_state(self, sym: str, mid: float, signal: float,
                       preds: dict, predictor: TimeHorizonPredictor):
        weights_1s    = predictor.get_model_weights("1s")
        labeled_w     = {
            FEATURE_LABELS[i]: round(w, 4)
            for i, w in enumerate(weights_1s)
            if i < len(FEATURE_LABELS)
        }
        warmup_prog   = predictor.warmup_progress()

        params = {
            "State":   self.state[sym],
            "Sig":     f"{signal:+.2f}",
            "1s":      f"{preds.get('1s',  0):+.1f}",
            "10s":     f"{preds.get('10s', 0):+.1f}",
            "30s":     f"{preds.get('30s', 0):+.1f}",
            "Weights": labeled_w,
            "Train":   warmup_prog,
        }
        self.engine.put(Event(
            EVENT_STRATEGY_UPDATE,
            StrategyData(symbol=sym, fair_value=mid, alpha_bps=signal, params=params)
        ))

    def _publish_warmup(self, sym: str, mid: float, signal: float,
                        preds: dict, predictor: TimeHorizonPredictor):
        elapsed      = time.time() - self.start_time
        warmup_prog  = predictor.warmup_progress()
        progress_pct = min(100.0, elapsed / self.min_warmup_sec * 100)

        params = {
            "State":  f"WARMUP {progress_pct:.0f}%",
            "Sig":    f"{signal:+.2f}",
            "Train":  warmup_prog,
            "Weights": {
                FEATURE_LABELS[i]: round(w, 4)
                for i, w in enumerate(predictor.get_model_weights("1s"))
                if i < len(FEATURE_LABELS)
            },
        }
        self.engine.put(Event(
            EVENT_STRATEGY_UPDATE,
            StrategyData(symbol=sym, fair_value=mid, alpha_bps=0, params=params)
        ))