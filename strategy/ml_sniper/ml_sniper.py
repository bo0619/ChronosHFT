# file: strategy/ml_sniper/ml_sniper.py
#
# ============================================================
# 修复记录（相对于原版）：
#
# [FIX-A] 信号权重翻转
#   原版：1s:0.6, 10s:0.3, 30s:0.1 → 1s 主导，信号极度不稳定
#   修复：1s:0.1, 10s:0.5, 30s:0.4 → 10s/30s 主导方向，1s 只做时机微调
#
# [FIX-B] 费率覆盖：止盈目标与入场阈值必须覆盖往返成本
#   原版：taker_threshold=4.0bps, profit_target=3.0bps，必亏
#   修复：见下方 [USDC-1]
#
# [FIX-C] 强制平仓 slippage 从固定 50bps 改为 ATR 自适应
#
# [FIX-D] feature_engine.reset_interval 移到正确位置
#
# [FIX-E] 冷启动保护从时间预热改为卡尔曼训练次数预热
#
# [FIX-F] ENTERING 状态：入场单被拒后正确回退到 FLAT
#
# [FIX-G] 止盈单价格逻辑修正
#
# ─────────────────────────────────────────────────────────
# [USDC 专项优化] 针对 Binance USDC 永续合约（Maker 费率 = 0）：
#
# [USDC-1] 入场阈值重构：GTX 为主路径，IOC 仅在信号加速时触发
#
#   USDC 合约费率结构：
#     Maker: 0 bps（甚至负）
#     Taker: ~5 bps 单边，往返 ~10 bps
#
#   原版设计：signal > 8bps → IOC，signal > 3bps → GTX
#   问题：对于 10s/30s 主导的中长期卡尔曼信号，主动吃单往往是
#         在中间价已经移动后才触发，此时支付 10bps 往返成本却
#         只获得已经部分衰减的信号，期望收益极低。
#
#   修复：
#     - GTX 为绝对主路径（maker_threshold 触发，零费率入场）
#     - IOC 仅在"信号 AND 信号速度均超过阈值"时触发
#       即：信号强 + 信号正在加速 → 动量明确，时间窗口极短，
#           GTX 挂单可能排不到队，此时才值得付 Taker 费
#     - taker_threshold 大幅提升至 20bps，远超往返成本，
#       确保 IOC 入场有足够的 Alpha 覆盖滑点和手续费
#
# [USDC-2] GTX 挂单价格优化
#
#   原版：BUY 挂 bid_1（排在最优买价队尾）
#   问题：加密货币 L2 深度往往 bid_1 挂单量极大，排队等到成交
#         时市场可能已反向移动（队列风险）。
#
#   修复：BUY 挂 bid_1 + 1 tick，优先排在现有最优买价前面，
#         在 PostOnly 约束下这仍然是 Maker 单（不穿越 ask），
#         但队列位置大幅靠前，成交速度更快，信号衰减更少。
#         SELL 对称处理：挂 ask_1 - 1 tick。
#
# [USDC-3] ENTERING 状态信号衰减撤单阈值调整
#
#   原版：abs(signal) < maker_threshold * 0.5 时撤单
#   问题：maker_threshold 现在更低（1.5bps），0.5 倍 = 0.75bps，
#         过于敏感，噪音就会触发撤单。
#   修复：撤单阈值改为固定 cancel_threshold_bps（默认 1.0bps），
#         与 maker_threshold 解耦，单独配置。
#
# [USDC-4] 止盈目标重构
#
#   USDC Maker 出场费率 = 0，止盈目标可以设得更小（不需要覆盖费率）。
#   止盈目标的下界应为"逆向选择成本 + 噪音 buffer"，约 3~5bps。
#   原版 profit_target=8bps 是为了覆盖 USDT Taker 出场费，
#   在 USDC Maker 出场下可以降低至 4bps，提高止盈命中率。
#
# [USDC-5] 信号速度（Signal Velocity）计算
#
#   使用固定长度滑动窗口（默认 3 帧）计算信号一阶差分的 EWMA。
#   velocity > velocity_threshold 时，表示信号正在加速，
#   结合 signal > taker_threshold，才触发 IOC 入场。
#
#   信号速度计算：
#     velocity = signal_now - signal_{n frames ago}
#   正值表示多头动量加速，负值表示空头动量加速。
#
# ============================================================

import time
from collections import defaultdict, deque
from datetime import datetime

from event.type import (
    OrderBook, TradeData, OrderIntent, Side, AggTradeData,
    OrderStateSnapshot, OrderStatus,
    Event, EVENT_STRATEGY_UPDATE, StrategyData,
)
from ..base import StrategyTemplate
from alpha.engine import FeatureEngine
from alpha.factors import GLFTCalibrator
from data.ref_data import ref_data_manager

from .predictor import TimeHorizonPredictor
from .config_loader import load_sniper_config

# 9 维特征标签（与 FeatureEngine.get_features() 顺序严格对应）
FEATURE_LABELS = ["Imb", "Dep", "Mic", "Trd", "Arr", "Vwp", "dIm", "dSp", "Mom"]


class MLSniperStrategy(StrategyTemplate):
    """
    ML Sniper（USDC 优化版）：三时间尺度卡尔曼滤波趋势跟踪策略。

    FSM 状态机：
      FLAT → ENTERING → HOLDING → EXITING → FLAT

    入场模式（USDC 费率结构优化）：
      [主路径] GTX Maker：signal > maker_threshold → 挂 bid+1tick / ask-1tick
               零手续费，队列靠前，覆盖中长期卡尔曼信号
      [辅路径] IOC Taker：signal > taker_threshold AND velocity > velocity_threshold
               仅在信号强且正在加速时触发，确保 Alpha 覆盖 ~10bps 往返成本

    出场模式：
      止盈单（GTX Maker）：在目标价挂单，零手续费等待被动成交
      强制平仓（IOC）：持仓超时 or 信号反转，立即出场
    """

    def __init__(self, engine, oms):
        super().__init__(engine, oms, "ML_Sniper_USDC")

        # ── 配置 ──────────────────────────────────────────────
        self.strat_conf = load_sniper_config()

        # [FIX-A] 默认权重：10s/30s 主导
        raw_weights = self.strat_conf.get(
            "weights", {"1s": 0.1, "10s": 0.5, "30s": 0.4}
        )
        self.weights = (
            raw_weights if isinstance(raw_weights, dict)
            else {"1s": 0.1, "10s": 0.5, "30s": 0.4}
        )

        self.lot_multiplier = self.strat_conf.get("lot_multiplier", 1.0)

        entry_cfg = self.strat_conf.get("entry", {})
        # [USDC-1] Taker 阈值大幅提升，GTX 阈值保持低敏感
        self.taker_entry_threshold  = entry_cfg.get("taker_entry_threshold_bps", 20.0)
        self.maker_entry_threshold  = entry_cfg.get("maker_entry_threshold_bps", 1.5)
        # [USDC-5] 信号速度阈值：触发 IOC 的额外条件
        self.velocity_threshold     = entry_cfg.get("velocity_threshold_bps",    3.0)
        # [USDC-3] 撤单阈值与 maker_threshold 解耦
        self.cancel_threshold       = entry_cfg.get("cancel_threshold_bps",      1.0)
        # [USDC-5] 计算 velocity 使用的历史帧数
        self.velocity_window        = entry_cfg.get("velocity_window_frames",    3)

        exit_cfg = self.strat_conf.get("exit", {})
        # [USDC-4] USDC Maker 出场零费率，止盈目标降低
        self.profit_target  = exit_cfg.get("profit_target_bps",  4.0)
        self.max_hold_sec   = exit_cfg.get("max_holding_sec",    10.0)

        exe_cfg = self.strat_conf.get("execution", {})
        self.tick_interval  = exe_cfg.get("tick_interval_sec",   0.1)
        self.cycle_interval = exe_cfg.get("cycle_interval_sec",  1.0)

        # [FIX-E] 冷启动：时间 + 卡尔曼训练次数双重保护
        self.min_warmup_sec = self.strat_conf.get("min_warmup_sec", 60.0)
        self.start_time     = time.time()
        self.is_warmed_up   = False

        # ── 组件 ──────────────────────────────────────────────
        self.feature_engine = FeatureEngine()
        self.predictors:  dict = {}
        self.calibrators: dict = {}

        # ── 运行时状态 ────────────────────────────────────────
        self.state         = defaultdict(lambda: "FLAT")
        self.pos_entry_ts  = defaultdict(float)
        self.entry_price   = defaultdict(float)
        self.entry_oid     = defaultdict(lambda: None)
        self.exit_oid      = defaultdict(lambda: None)
        self.last_tick_ts  = defaultdict(float)
        self.last_cycle_ts = defaultdict(float)

        # [USDC-5] 信号历史队列（每个 symbol 独立），用于计算 velocity
        # deque 存储 (timestamp, signal) 元组
        self.signal_history: dict = defaultdict(lambda: deque(maxlen=20))

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
        """强制平仓的价格偏移比例，基于 ATR，最小 10bps，最大 80bps"""
        atr = getattr(self.calibrators.get(symbol), "sigma_bps", 20.0)
        slippage_bps = float(max(10.0, min(80.0, atr * 1.5)))
        return slippage_bps / 10000.0

    # ── [USDC-5] 信号速度计算 ─────────────────────────────────
    def _compute_signal_velocity(self, symbol: str, signal: float, now: float) -> float:
        """
        计算信号速度（一阶差分）。

        将当前 (ts, signal) 推入历史队列，然后取
        velocity_window 帧前的信号做差，得到信号变化速率。

        返回值：正数表示多头动量加速，负数表示空头动量加速。
        当历史帧数不足时返回 0.0（保守处理，不触发 IOC）。
        """
        hist = self.signal_history[symbol]
        hist.append((now, signal))

        if len(hist) < self.velocity_window + 1:
            # 帧数不足，不能计算速度，保守返回 0
            return 0.0

        # 取 velocity_window 帧前的信号
        past_signal = hist[-(self.velocity_window + 1)][1]
        return signal - past_signal

    # ─────────────────────────────────────────────────────────
    # 行情事件
    # ─────────────────────────────────────────────────────────

    def on_orderbook(self, ob: OrderBook):
        now = time.time()
        sym = ob.symbol

        # tick 频率限制
        if now - self.last_tick_ts[sym] < self.tick_interval:
            return
        self.last_tick_ts[sym] = now

        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 == 0:
            return
        mid = (bid_1 + ask_1) / 2.0

        # ── 每 tick：特征更新 + 卡尔曼学习 ──────────────────
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

        # ── [USDC-5] 计算信号速度 ─────────────────────────────
        velocity = self._compute_signal_velocity(sym, signal, now)

        # ── 冷启动处理 ────────────────────────────────────────
        if not self._check_warmup(sym):
            self._publish_warmup(sym, mid, signal, velocity, preds, predictor)
            return

        # ── 正常运行 ──────────────────────────────────────────
        self._publish_state(sym, mid, signal, velocity, preds, predictor)
        self._run_fsm(sym, mid, bid_1, ask_1, signal, velocity, now)

    def on_market_trade(self, trade: AggTradeData):
        """驱动 FeatureEngine 的成交特征"""
        self.feature_engine.on_trade(trade)

    # ─────────────────────────────────────────────────────────
    # FSM 状态机
    # ─────────────────────────────────────────────────────────

    def _run_fsm(self, sym: str, mid: float,
                 bid_1: float, ask_1: float,
                 signal: float, velocity: float, now: float):
        curr_state = self.state[sym]
        net_pos    = self.oms.exposure.net_positions.get(sym, 0.0)
        tick       = self._tick_size(sym, mid)

        # ── FLAT：评估入场 ────────────────────────────────────
        if curr_state == "FLAT":
            # 防御：OMS 实际有持仓但策略认为 FLAT → 同步状态
            if abs(net_pos) > 1e-6:
                self.state[sym] = "HOLDING"
                return

            vol = self._calc_vol(sym, mid)
            if vol <= 0:
                return

            # ── 多头入场判断 ──────────────────────────────────
            if signal > self.maker_entry_threshold:

                # [USDC-1] IOC 辅路径：信号强 AND 速度加速
                # 语义：行情在快速移动，GTX 排队可能错过，此时才值得付 Taker 费
                if (signal > self.taker_entry_threshold
                        and velocity > self.velocity_threshold):
                    slippage = self._force_exit_slippage(sym)
                    price = ref_data_manager.round_price(
                        sym, ask_1 * (1 + slippage)
                    )
                    self._entry(sym, Side.BUY, price, vol, "IOC")

                else:
                    # [USDC-2] GTX 主路径：挂 bid + 1 tick，抢队列靠前位置
                    # bid+1tick 仍低于 ask，满足 PostOnly 约束（不穿越盘口）
                    price = ref_data_manager.round_price(sym, bid_1 + tick)
                    self._entry(sym, Side.BUY, price, vol, "GTX")

            # ── 空头入场判断 ──────────────────────────────────
            elif signal < -self.maker_entry_threshold:

                # [USDC-1] IOC 辅路径：信号强 AND 速度加速（空头方向）
                if (signal < -self.taker_entry_threshold
                        and velocity < -self.velocity_threshold):
                    slippage = self._force_exit_slippage(sym)
                    price = ref_data_manager.round_price(
                        sym, bid_1 * (1 - slippage)
                    )
                    self._entry(sym, Side.SELL, price, vol, "IOC")

                else:
                    # [USDC-2] GTX 主路径：挂 ask - 1 tick，抢队列靠前位置
                    price = ref_data_manager.round_price(sym, ask_1 - tick)
                    self._entry(sym, Side.SELL, price, vol, "GTX")

        # ── ENTERING：等待入场单成交 ──────────────────────────
        elif curr_state == "ENTERING":
            oid = self.entry_oid[sym]
            if oid and oid in self.active_orders:
                # [USDC-3] 撤单阈值与 maker_threshold 解耦，避免噪音触发
                if abs(signal) < self.cancel_threshold:
                    self.cancel_order(oid)

        # ── HOLDING：持仓管理 ─────────────────────────────────
        elif curr_state == "HOLDING":
            if abs(net_pos) < 1e-6:
                self.state[sym] = "FLAT"
                self._clear_oids(sym)
                return

            holding_time = now - self.pos_entry_ts[sym]

            # 强制平仓条件
            force_exit = False
            if holding_time > self.max_hold_sec:
                force_exit = True
            # 信号强反转（使用 taker_threshold，确保是真正的趋势反转）
            if net_pos > 0 and signal < -self.taker_entry_threshold:
                force_exit = True
            if net_pos < 0 and signal > self.taker_entry_threshold:
                force_exit = True

            if force_exit:
                self.cancel_all(sym)
                self.state[sym] = "EXITING"
                slippage = self._force_exit_slippage(sym)
                if net_pos > 0:
                    price = ref_data_manager.round_price(
                        sym, bid_1 * (1 - slippage)
                    )
                    self.exit_long(sym, price, abs(net_pos))
                else:
                    price = ref_data_manager.round_price(
                        sym, ask_1 * (1 + slippage)
                    )
                    self.exit_short(sym, price, abs(net_pos))
                return

            # [USDC-4] 挂止盈 Maker 单（零费率，目标更小）
            if not self.exit_oid[sym]:
                entry_px = self.entry_price[sym]
                if net_pos > 0:
                    target = entry_px * (1 + self.profit_target / 10000.0)
                    # [FIX-G] 止盈价至少比 bid 高一个 tick
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
        """发入场单。mode = 'IOC'（Taker）或 'GTX'（PostOnly Maker）"""
        if self.entry_oid[sym]:
            self.cancel_order(self.entry_oid[sym])

        is_ioc = (mode == "IOC")
        intent = OrderIntent(
            self.name, sym, side, price, vol,
            time_in_force = "IOC" if is_ioc else "GTC",
            is_post_only  = not is_ioc,
        )
        oid = self.send_intent(intent)
        if oid:
            self.entry_oid[sym] = oid
            self.state[sym]     = "ENTERING"
            # ── 策略层入场意图日志 ────────────────────────────
            sym_clean = sym.replace("USDC", "").replace("USDT", "").lower()
            side_str  = "long" if side == Side.BUY else "short"
            self.log(f"{sym_clean} enter {side_str} @ {price:.6g}  ({mode}, vol={vol})")

    def _place_exit(self, sym: str, side: Side, price: float, vol: float):
        """挂止盈 Maker 单（PostOnly）"""
        intent = OrderIntent(
            self.name, sym, side, price, vol,
            is_post_only = True,
        )
        oid = self.send_intent(intent)
        if oid:
            self.exit_oid[sym] = oid
            # ── 策略层出场意图日志 ────────────────────────────
            sym_clean = sym.replace("USDC", "").replace("USDT", "").lower()
            # side 是平仓方向（BUY=平空，SELL=平多），日志反映持仓方向
            pos_str = "short" if side == Side.BUY else "long"
            self.log(f"{sym_clean} exit  {pos_str} @ {price:.6g}  (GTX TP, vol={vol})")

    def _clear_oids(self, sym: str):
        self.entry_oid[sym] = None
        self.exit_oid[sym]  = None

    # ─────────────────────────────────────────────────────────
    # 订单事件
    # ─────────────────────────────────────────────────────────

    def on_order(self, snapshot: OrderStateSnapshot):
        super().on_order(snapshot)

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

    def on_trade(self, trade: TradeData):
        pass

    # ─────────────────────────────────────────────────────────
    # Dashboard / UI
    # ─────────────────────────────────────────────────────────

    def _publish_state(self, sym: str, mid: float, signal: float,
                       velocity: float, preds: dict,
                       predictor: TimeHorizonPredictor):
        weights_1s  = predictor.get_model_weights("1s")
        labeled_w   = {
            FEATURE_LABELS[i]: round(w, 4)
            for i, w in enumerate(weights_1s)
            if i < len(FEATURE_LABELS)
        }
        warmup_prog = predictor.warmup_progress()

        # 判断入场模式用于 UI 提示
        if (abs(signal) > self.taker_entry_threshold
                and abs(velocity) > self.velocity_threshold):
            entry_mode = "IOC(accel)"
        elif abs(signal) > self.maker_entry_threshold:
            entry_mode = "GTX"
        else:
            entry_mode = "-"

        params = {
            "State":  self.state[sym],
            "Sig":    f"{signal:+.2f}",
            "Vel":    f"{velocity:+.2f}",    # 新增：信号速度展示
            "Mode":   entry_mode,            # 新增：当前会触发哪种入场模式
            "1s":     f"{preds.get('1s',  0):+.1f}",
            "10s":    f"{preds.get('10s', 0):+.1f}",
            "30s":    f"{preds.get('30s', 0):+.1f}",
            "Weights": labeled_w,
            "Train":  warmup_prog,
        }
        self.engine.put(Event(
            EVENT_STRATEGY_UPDATE,
            StrategyData(symbol=sym, fair_value=mid, alpha_bps=signal, params=params)
        ))

    def _publish_warmup(self, sym: str, mid: float, signal: float,
                        velocity: float, preds: dict,
                        predictor: TimeHorizonPredictor):
        elapsed      = time.time() - self.start_time
        warmup_prog  = predictor.warmup_progress()
        progress_pct = min(100.0, elapsed / self.min_warmup_sec * 100)

        params = {
            "State":  f"WARMUP {progress_pct:.0f}%",
            "Sig":    f"{signal:+.2f}",
            "Vel":    f"{velocity:+.2f}",
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