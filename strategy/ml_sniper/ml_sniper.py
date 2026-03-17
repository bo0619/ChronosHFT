import time
from collections import defaultdict, deque

from event.type import (
    AggTradeData,
    Event,
    EVENT_STRATEGY_UPDATE,
    LifecycleState,
    OrderBook,
    OrderIntent,
    OrderStateSnapshot,
    OrderStatus,
    Side,
    StrategyData,
    TradeData,
)
from ..base import StrategyTemplate
from alpha.engine import FeatureEngine
from alpha.factors import GLFTCalibrator
from data.ref_data import ref_data_manager

from .alpha_process import MLSniperAlphaProcess
from .config_loader import load_sniper_config
from .predictor import TimeHorizonPredictor


FEATURE_LABELS = ["Imb", "Dep", "Mic", "Trd", "Arr", "Vwp", "dIm", "dSp", "Mom"]
CORE_HORIZONS = ("10s", "30s")


class MLSniperStrategy(StrategyTemplate):
    """
    Multi-horizon ML sniper strategy tuned for USDC perpetuals.

    State flow:
      FLAT -> ENTERING -> ENTERING_PARTIAL -> HOLDING -> EXITING -> FLAT
    """

    def __init__(self, engine, oms, alpha_process_config=None):
        super().__init__(engine, oms, "ML_Sniper_USDC")

        self.strat_conf = load_sniper_config()
        default_weights = {"1s": 0.1, "10s": 0.5, "30s": 0.4}
        raw_weights = self.strat_conf.get("weights", default_weights)
        if isinstance(raw_weights, dict):
            self.weights = {}
            for horizon, fallback in default_weights.items():
                try:
                    self.weights[horizon] = float(raw_weights.get(horizon, fallback))
                except (TypeError, ValueError):
                    self.weights[horizon] = fallback
        else:
            self.weights = dict(default_weights)
        self.lot_multiplier = float(self.strat_conf.get("lot_multiplier", 1.0) or 1.0)

        entry_cfg = self.strat_conf.get("entry", {})
        self.base_taker_entry_threshold = entry_cfg.get("taker_entry_threshold_bps", 20.0)
        self.base_maker_entry_threshold = entry_cfg.get("maker_entry_threshold_bps", 1.5)
        self.base_velocity_threshold = entry_cfg.get("velocity_threshold_bps", 3.0)
        self.cancel_threshold = entry_cfg.get("cancel_threshold_bps", 1.0)
        self.velocity_window = entry_cfg.get("velocity_window_frames", 3)
        self.max_entry_wait_sec = entry_cfg.get("max_entry_wait_sec", 2.0)
        self.require_horizon_consensus = entry_cfg.get("require_horizon_consensus", True)
        self.consensus_min_abs_bps = entry_cfg.get("consensus_min_abs_bps", 0.5)
        self.ioc_requires_short_horizon_confirmation = entry_cfg.get("ioc_requires_short_horizon_confirmation", True)
        self.net_edge_buffer_bps = entry_cfg.get("net_edge_buffer_bps", 1.0)
        self.maker_spread_weight = entry_cfg.get("maker_spread_weight", 0.25)
        self.taker_spread_weight = entry_cfg.get("taker_spread_weight", 0.5)

        exit_cfg = self.strat_conf.get("exit", {})
        self.profit_target = exit_cfg.get("profit_target_bps", 4.0)
        self.max_hold_sec = exit_cfg.get("max_holding_sec", 10.0)
        self.min_profit_target_bps = exit_cfg.get("min_profit_target_bps", 1.5)
        self.max_profit_target_bps = exit_cfg.get("max_profit_target_bps", 9.0)
        self.alpha_decay_exit_threshold_bps = exit_cfg.get("alpha_decay_exit_threshold_bps", 0.35)
        self.alpha_flip_exit_threshold_bps = exit_cfg.get("alpha_flip_exit_threshold_bps", 6.0)
        self.min_decay_hold_sec = exit_cfg.get("min_decay_hold_sec", 1.0)
        self.exit_requote_sec = exit_cfg.get("exit_requote_sec", 1.5)
        self.exit_signal_profit_weight = exit_cfg.get("signal_profit_target_weight", 0.12)
        self.exit_sigma_profit_weight = exit_cfg.get("sigma_profit_target_weight", 0.08)
        self.exit_confidence_profit_weight = exit_cfg.get("confidence_profit_target_weight", 1.25)

        exe_cfg = self.strat_conf.get("execution", {})
        self.tick_interval = exe_cfg.get("tick_interval_sec", 0.1)
        self.cycle_interval = exe_cfg.get("cycle_interval_sec", 1.0)

        feedback_cfg = self.strat_conf.get("feedback", {})
        self.feedback_alpha = feedback_cfg.get("ema_alpha", 0.2)
        self.edge_penalty_weight = feedback_cfg.get("edge_penalty_weight", 0.5)
        self.pnl_penalty_weight = feedback_cfg.get("pnl_penalty_weight", 0.15)
        self.win_rate_target = feedback_cfg.get("win_rate_target", 0.55)
        self.win_rate_penalty_scale = feedback_cfg.get("win_rate_penalty_scale", 40.0)
        self.max_threshold_adjust_bps = feedback_cfg.get("max_threshold_adjust_bps", 8.0)
        self.min_closed_trades_for_adaptation = int(feedback_cfg.get("min_closed_trades_for_adaptation", 3))

        regime_cfg = self.strat_conf.get("regime", {})
        self.max_entry_spread_bps = regime_cfg.get("max_entry_spread_bps", 25.0)
        self.max_entry_sigma_bps = regime_cfg.get("max_entry_sigma_bps", 45.0)
        self.max_spread_sigma_ratio = regime_cfg.get("max_spread_sigma_ratio", 3.0)
        self.confidence_floor = regime_cfg.get("confidence_floor", 0.18)
        self.high_edge_confidence_relaxation = regime_cfg.get("high_edge_confidence_relaxation", 1.6)

        sizing_cfg = self.strat_conf.get("sizing", {})
        self.min_size_scale = sizing_cfg.get("min_scale", 1.0)
        self.max_size_scale = sizing_cfg.get("max_scale", 3.0)
        self.edge_size_exponent = sizing_cfg.get("edge_exponent", 1.2)
        self.confidence_size_weight = sizing_cfg.get("confidence_weight", 0.75)
        self.spread_size_penalty = sizing_cfg.get("spread_penalty", 0.04)
        self.sigma_size_penalty = sizing_cfg.get("sigma_penalty", 0.01)

        self.labeling_cfg = self.strat_conf.get("labeling", {})
        self.predict_net_edge = self.labeling_cfg.get("predict_net_edge", True)
        self.live_cost_weight = float(self.labeling_cfg.get("live_cost_weight", 0.35))

        self.min_warmup_sec = self.strat_conf.get("min_warmup_sec", 60.0)
        self.start_time = time.time()

        oms_config = getattr(self.oms, "config", {})
        account_cfg = oms_config.get("account", {})
        risk_limits = oms_config.get("risk", {}).get("limits", {})
        self.account_leverage = max(1.0, float(account_cfg.get("leverage", 1.0) or 1.0))
        self.max_order_notional = float(risk_limits.get("max_order_notional", 0.0) or 0.0)

        backtest_cfg = oms_config.get("backtest", {})
        self.maker_fee_bps = float(backtest_cfg.get("maker_fee", 0.0)) * 10000.0
        self.taker_fee_bps = float(backtest_cfg.get("taker_fee", 0.0005)) * 10000.0

        self.feature_engine = FeatureEngine()
        self.predictors = {}
        self.calibrators = {}
        self.remote_predictor_ready = defaultdict(bool)
        self.remote_model_meta = defaultdict(dict)

        self.state = defaultdict(lambda: "FLAT")
        self.pos_entry_ts = defaultdict(float)
        self.entry_price = defaultdict(float)
        self.entry_oid = defaultdict(lambda: None)
        self.exit_oid = defaultdict(lambda: None)
        self.entry_mode = defaultdict(lambda: None)
        self.entry_submit_ts = defaultdict(float)
        self.last_tick_ts = defaultdict(float)
        self.last_cycle_ts = defaultdict(float)
        self.symbol_start_ts = defaultdict(float)
        self.symbol_warmup_ready = defaultdict(bool)
        self.alpha_rewarming_symbols = set()

        self.signal_history = defaultdict(lambda: deque(maxlen=20))
        self.latest_mid = defaultdict(float)
        self.latest_signal = defaultdict(float)
        self.latest_velocity = defaultdict(float)
        self.latest_preds = defaultdict(dict)
        self.latest_confidence = defaultdict(float)
        self.latest_size_scale = defaultdict(lambda: 1.0)
        self.latest_regime = defaultdict(lambda: "OK")
        self.latest_spread_bps = defaultdict(float)
        self.latest_sigma_bps = defaultdict(float)
        self.order_context = {}
        self.execution_feedback = defaultdict(self._new_execution_feedback)
        alpha_process_config = dict(alpha_process_config or {})
        alpha_process_config.setdefault("tick_interval_sec", self.tick_interval)
        alpha_process_config.setdefault("cycle_interval_sec", self.cycle_interval)
        label_cfg = dict(self.labeling_cfg)
        label_cfg.setdefault("maker_fee_bps", self.maker_fee_bps)
        label_cfg.setdefault("taker_fee_bps", self.taker_fee_bps)
        alpha_process_config.setdefault("labeling", label_cfg)
        self.alpha_process = MLSniperAlphaProcess(alpha_process_config)
        if self.alpha_process.enabled:
            self.alpha_process.start()

    def _new_execution_feedback(self):
        return {
            "maker_edge_ewma": 0.0,
            "taker_edge_ewma": 0.0,
            "exit_pnl_ewma": 0.0,
            "win_rate_ewma": 0.5,
            "closed_trades": 0,
            "maker_fills": 0,
            "taker_fills": 0,
        }

    def _oms_health_detail(self) -> str:
        oms_state = getattr(self.oms, "state", None)
        capability_reason = str(getattr(self.oms, "capability_reason", "") or "")
        if oms_state == LifecycleState.HALTED:
            halt_reason = str(getattr(self.oms, "last_halt_reason", "") or capability_reason)
            if getattr(self.oms, "manual_rearm_required", False):
                return f"manual_rearm_required:{halt_reason}" if halt_reason else "manual_rearm_required"
            return halt_reason or "halted"
        if oms_state == LifecycleState.FROZEN:
            freeze_reason = str(getattr(self.oms, "last_freeze_reason", "") or capability_reason)
            return freeze_reason or "frozen"
        return capability_reason or str(self.last_system_health or "")

    def _ema(self, previous: float, value: float) -> float:
        alpha = self.feedback_alpha
        return (1.0 - alpha) * previous + alpha * value

    def _clamp(self, value: float, low: float, high: float) -> float:
        return max(low, min(high, value))

    def _get_predictor(self, symbol: str) -> TimeHorizonPredictor:
        if symbol not in self.predictors:
            label_cfg = dict(self.labeling_cfg)
            label_cfg.setdefault("maker_fee_bps", self.maker_fee_bps)
            label_cfg.setdefault("taker_fee_bps", self.taker_fee_bps)
            self.predictors[symbol] = TimeHorizonPredictor(num_features=9, label_config=label_cfg)
            self.calibrators[symbol] = GLFTCalibrator(window=500)
        return self.predictors[symbol]

    def _check_warmup(self, symbol: str, predictor_ready: bool = None) -> bool:
        if self.symbol_warmup_ready[symbol]:
            return True

        started_at = self.symbol_start_ts[symbol] or self.start_time
        elapsed = time.time() - started_at
        if elapsed < self.min_warmup_sec:
            return False

        if predictor_ready is None:
            predictor = self._get_predictor(symbol)
            predictor_ready = predictor.is_warmed_up
        if not predictor_ready:
            return False

        self.symbol_warmup_ready[symbol] = True
        return True

    def _begin_alpha_symbol_recovery(self, symbol: str, reset_model_state: bool = False):
        symbol = (symbol or "").upper()
        if not symbol:
            return

        if reset_model_state:
            self.signal_history[symbol].clear()
            self.latest_signal[symbol] = 0.0
            self.latest_velocity[symbol] = 0.0
            self.latest_preds[symbol] = {horizon: 0.0 for horizon in self.weights}
            self.latest_confidence[symbol] = 0.0
            self.latest_size_scale[symbol] = 1.0
            self.latest_regime[symbol] = "RECOVERING"
            self.latest_spread_bps[symbol] = 0.0
            self.latest_sigma_bps[symbol] = 0.0
            self.symbol_warmup_ready[symbol] = False
            self.remote_predictor_ready[symbol] = False
            self.remote_model_meta[symbol] = {}
            self.symbol_start_ts[symbol] = time.time()
            self.last_tick_ts[symbol] = 0.0
            self.last_cycle_ts[symbol] = 0.0

        self.alpha_rewarming_symbols.add(symbol)

    def _freeze_alpha_symbol(self, symbol: str, reason: str):
        if not hasattr(self.oms, "freeze_strategy"):
            return

        current_reason = ""
        if hasattr(self.oms, "get_strategy_freeze_reason"):
            current_reason = self.oms.get_strategy_freeze_reason(self.name, symbol=symbol)
        if current_reason and not current_reason.startswith("alpha_process_"):
            return
        if current_reason == reason:
            return

        self.oms.freeze_strategy(
            self.name,
            reason,
            symbol=symbol,
            cancel_active_orders=True,
        )

    def _complete_alpha_symbol_recovery(self, symbol: str):
        symbol = (symbol or "").upper()
        if not symbol:
            return

        self.alpha_rewarming_symbols.discard(symbol)
        if hasattr(self.alpha_process, "mark_symbol_recovered"):
            self.alpha_process.mark_symbol_recovered(symbol)

        if not hasattr(self.oms, "clear_strategy_freeze"):
            return

        freeze_reason = ""
        if hasattr(self.oms, "get_strategy_freeze_reason"):
            freeze_reason = self.oms.get_strategy_freeze_reason(self.name, symbol=symbol)
            if freeze_reason and not freeze_reason.startswith("alpha_process_"):
                return

        if freeze_reason or not hasattr(self.oms, "get_strategy_freeze_reason"):
            self.oms.clear_strategy_freeze(
                self.name,
                symbol=symbol,
                reason="alpha_process_recovered",
            )

    def _calc_vol(self, symbol: str, price: float) -> float:
        info = ref_data_manager.get_info(symbol)
        if not info or price <= 0:
            return 0.0

        min_notional = max(info.min_notional * 1.1, info.min_qty * price)
        target_notional = min_notional * max(self.lot_multiplier, 0.0) * self.account_leverage
        if self.max_order_notional > 0:
            target_notional = min(target_notional, self.max_order_notional * 0.95)

        target_qty = max(info.min_qty, target_notional / price)
        return ref_data_manager.round_qty(symbol, target_qty)

    def _tick_size(self, symbol: str, mid: float) -> float:
        info = ref_data_manager.get_info(symbol)
        if info and hasattr(info, "tick_size") and info.tick_size > 0:
            return info.tick_size
        return ref_data_manager.round_price(symbol, mid * 0.0001)

    def _current_sigma_bps(self, symbol: str, default: float = 10.0) -> float:
        latest = float(self.latest_sigma_bps.get(symbol, 0.0) or 0.0)
        if latest > 0.0:
            return latest
        return float(max(0.0, getattr(self.calibrators.get(symbol), "sigma_bps", default)))

    def _force_exit_slippage(self, symbol: str) -> float:
        atr = self._current_sigma_bps(symbol, default=20.0)
        slippage_bps = float(max(10.0, min(80.0, atr * 1.5)))
        return slippage_bps / 10000.0

    def _compute_signal_velocity(self, symbol: str, signal: float, now: float) -> float:
        history = self.signal_history[symbol]
        history.append((now, signal))
        if len(history) < self.velocity_window + 1:
            return 0.0
        past_signal = history[-(self.velocity_window + 1)][1]
        return signal - past_signal

    def _maker_entry_price(self, side: Side, bid_1: float, ask_1: float, tick: float) -> float:
        spread = max(0.0, ask_1 - bid_1)
        if spread >= 2 * tick:
            return bid_1 + tick if side == Side.BUY else ask_1 - tick
        return bid_1 if side == Side.BUY else ask_1

    def _consensus_direction(self, preds: dict) -> int:
        votes = []
        for horizon in CORE_HORIZONS:
            value = float(preds.get(horizon, 0.0))
            if value >= self.consensus_min_abs_bps:
                votes.append(1)
            elif value <= -self.consensus_min_abs_bps:
                votes.append(-1)
            else:
                votes.append(0)

        if votes and all(vote > 0 for vote in votes):
            return 1
        if votes and all(vote < 0 for vote in votes):
            return -1
        return 0

    def _has_consensus(self, preds: dict, side: Side, mode: str) -> bool:
        if not self.require_horizon_consensus:
            return True

        direction = 1 if side == Side.BUY else -1
        if self._consensus_direction(preds) != direction:
            return False

        if mode == "IOC" and self.ioc_requires_short_horizon_confirmation:
            return direction * float(preds.get("1s", 0.0)) > 0.0

        return True

    def _adaptive_threshold_adjustment(self, symbol: str, mode: str) -> float:
        feedback = self.execution_feedback[symbol]
        mode_key = "maker" if mode == "GTX" else "taker"
        adjustment = max(0.0, -feedback[f"{mode_key}_edge_ewma"]) * self.edge_penalty_weight

        if feedback["closed_trades"] >= self.min_closed_trades_for_adaptation:
            adjustment += max(0.0, -feedback["exit_pnl_ewma"]) * self.pnl_penalty_weight
            if feedback["win_rate_ewma"] < self.win_rate_target:
                adjustment += (self.win_rate_target - feedback["win_rate_ewma"]) * self.win_rate_penalty_scale

        return min(self.max_threshold_adjust_bps, adjustment)

    def _adaptive_entry_threshold(self, symbol: str, mode: str) -> float:
        base = self.base_taker_entry_threshold if mode == "IOC" else self.base_maker_entry_threshold
        return base + self._adaptive_threshold_adjustment(symbol, mode)

    def _estimate_entry_cost_bps(self, symbol: str, mid: float, bid_1: float, ask_1: float, mode: str) -> float:
        if mid <= 0:
            return float("inf")

        spread_bps = max(0.0, (ask_1 - bid_1) / mid * 10000.0)
        sigma_bps = self._current_sigma_bps(symbol, default=10.0)
        feedback = self.execution_feedback[symbol]

        if mode == "IOC":
            slippage_bps = max(0.5, min(20.0, sigma_bps * 0.35))
            exit_fee_bps = max(self.maker_fee_bps, self.taker_fee_bps * 0.5)
            quality_penalty = max(0.0, -feedback["taker_edge_ewma"])
            return self.taker_spread_weight * spread_bps + self.taker_fee_bps + exit_fee_bps + slippage_bps + quality_penalty

        adverse_bps = max(0.25, min(6.0, sigma_bps * 0.10))
        exit_fee_bps = max(self.maker_fee_bps, self.taker_fee_bps * 0.35)
        quality_penalty = max(0.0, -feedback["maker_edge_ewma"])
        return self.maker_spread_weight * spread_bps + self.maker_fee_bps + exit_fee_bps + adverse_bps + quality_penalty

    def _required_signal_bps(self, symbol: str, mid: float, bid_1: float, ask_1: float, mode: str) -> tuple[float, float]:
        cost_bps = self._estimate_entry_cost_bps(symbol, mid, bid_1, ask_1, mode)
        threshold_bps = self._adaptive_entry_threshold(symbol, mode)
        effective_cost_bps = cost_bps * (self.live_cost_weight if self.predict_net_edge else 1.0)
        return max(threshold_bps, effective_cost_bps + self.net_edge_buffer_bps), cost_bps

    def _prediction_confidence(self, predictor: TimeHorizonPredictor, preds: dict) -> float:
        return self._prediction_confidence_from_diagnostics(preds, predictor.get_last_diagnostics())

    def _prediction_confidence_from_diagnostics(self, preds: dict, diagnostics: dict) -> float:
        weighted = 0.0
        total_weight = 0.0
        for horizon, weight in self.weights.items():
            weighted += float(diagnostics.get(horizon, {}).get("confidence", 0.0)) * weight
            total_weight += weight
        if total_weight <= 0:
            return 0.0

        values = [float(preds.get(h, 0.0)) for h in self.weights]
        dispersion = (max(values) - min(values)) if values else 0.0
        consensus_bonus = 0.1 if self._consensus_direction(preds) != 0 else 0.0
        dispersion_penalty = min(0.35, dispersion / 40.0)
        return self._clamp(weighted / total_weight - dispersion_penalty + consensus_bonus, 0.0, 1.0)

    def _regime_status(self, symbol: str, mid: float, bid_1: float, ask_1: float, signal: float, required_signal: float, confidence: float) -> tuple[bool, str, float, float]:
        if mid <= 0:
            return False, "bad_mid", 0.0, 0.0

        spread_bps = max(0.0, (ask_1 - bid_1) / mid * 10000.0)
        sigma_bps = self._current_sigma_bps(symbol, default=10.0)
        strong_edge = abs(signal) >= max(required_signal, 1.0) * self.high_edge_confidence_relaxation

        if spread_bps > self.max_entry_spread_bps:
            return False, "spread", spread_bps, sigma_bps
        if sigma_bps > self.max_entry_sigma_bps:
            return False, "sigma", spread_bps, sigma_bps
        if sigma_bps > 0 and spread_bps / max(sigma_bps, 1e-6) > self.max_spread_sigma_ratio:
            return False, "spread_sigma", spread_bps, sigma_bps
        if confidence < self.confidence_floor and not strong_edge:
            return False, "low_conf", spread_bps, sigma_bps
        return True, "OK", spread_bps, sigma_bps

    def _size_scale(self, signal: float, required_signal: float, confidence: float, spread_bps: float, sigma_bps: float) -> float:
        edge_ratio = abs(signal) / max(required_signal, 1e-6)
        scale = max(1.0, edge_ratio) ** self.edge_size_exponent
        scale *= 1.0 + confidence * self.confidence_size_weight
        scale -= spread_bps * self.spread_size_penalty
        scale -= sigma_bps * self.sigma_size_penalty
        return self._clamp(scale, self.min_size_scale, self.max_size_scale)

    def _sized_volume(self, symbol: str, mid: float, signal: float, required_signal: float, confidence: float, spread_bps: float, sigma_bps: float) -> tuple[float, float]:
        base_vol = self._calc_vol(symbol, mid)
        if base_vol <= 0:
            return 0.0, 1.0

        scale = self._size_scale(signal, required_signal, confidence, spread_bps, sigma_bps)
        vol = ref_data_manager.round_qty(symbol, base_vol * scale)
        return vol, scale

    def _directional_signal(self, side: Side, signal: float) -> float:
        direction = 1.0 if side == Side.BUY else -1.0
        return direction * signal

    def _dynamic_profit_target_bps(self, symbol: str, side: Side, signal: float, confidence: float, holding_time: float) -> float:
        aligned_signal = max(0.0, self._directional_signal(side, signal))
        sigma_bps = self._current_sigma_bps(symbol, default=10.0)
        decay = self._clamp(holding_time / max(self.max_hold_sec, 1.0), 0.0, 1.0)

        target = self.profit_target + aligned_signal * self.exit_signal_profit_weight + sigma_bps * self.exit_sigma_profit_weight + confidence * self.exit_confidence_profit_weight
        target *= 1.0 - 0.45 * decay
        return self._clamp(target, self.min_profit_target_bps, self.max_profit_target_bps)

    def _desired_exit_price(self, symbol: str, net_pos: float, entry_px: float, bid_1: float, ask_1: float, tick: float, signal: float, confidence: float, holding_time: float) -> tuple[Side, float, float]:
        if net_pos > 0:
            side = Side.SELL
            target_bps = self._dynamic_profit_target_bps(symbol, Side.BUY, signal, confidence, holding_time)
            target_px = entry_px * (1 + target_bps / 10000.0)
            price = ref_data_manager.round_price(symbol, max(target_px, bid_1 + tick))
            return side, price, target_bps

        side = Side.BUY
        target_bps = self._dynamic_profit_target_bps(symbol, Side.SELL, signal, confidence, holding_time)
        target_px = entry_px * (1 - target_bps / 10000.0)
        price = ref_data_manager.round_price(symbol, min(target_px, ask_1 - tick))
        return side, price, target_bps

    def _track_order_context(self, oid: str, sym: str, side: Side, mode: str, role: str, limit_price: float):
        self.order_context[oid] = {
            "symbol": sym,
            "side": side,
            "mode": mode,
            "role": role,
            "limit_price": limit_price,
            "mid": self.latest_mid[sym],
            "signal": self.latest_signal[sym],
            "velocity": self.latest_velocity[sym],
            "confidence": self.latest_confidence[sym],
            "entry_price": self.entry_price[sym],
            "submit_ts": time.time(),
            "exit_pnl_sum": 0.0,
            "exit_qty": 0.0,
        }

    def _execution_edge_bps(self, side: Side, fill_price: float, ref_mid: float) -> float:
        if ref_mid <= 0:
            return 0.0
        if side == Side.BUY:
            return (ref_mid - fill_price) / ref_mid * 10000.0
        return (fill_price - ref_mid) / ref_mid * 10000.0

    def _exit_pnl_bps(self, exit_side: Side, fill_price: float, entry_price: float) -> float:
        if entry_price <= 0 or fill_price <= 0:
            return 0.0
        if exit_side == Side.SELL:
            return (fill_price / entry_price - 1.0) * 10000.0
        return (entry_price / fill_price - 1.0) * 10000.0

    def on_orderbook(self, ob: OrderBook):
        if self.alpha_process.enabled:
            self.alpha_process.submit_orderbook(ob)
            self.poll_async_workers()
            return

        self._process_orderbook_inline(ob)

    def _process_orderbook_inline(self, ob: OrderBook):
        now = time.time()
        sym = ob.symbol
        if self.symbol_start_ts[sym] == 0.0:
            self.symbol_start_ts[sym] = now

        if now - self.last_tick_ts[sym] < self.tick_interval:
            return
        self.last_tick_ts[sym] = now

        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 == 0 or ask_1 == 0:
            return
        mid = (bid_1 + ask_1) / 2.0
        self.latest_mid[sym] = mid

        self.feature_engine.on_orderbook(ob)
        predictor = self._get_predictor(sym)
        self.calibrators[sym].on_orderbook(ob)

        feats = self.feature_engine.get_features(sym)
        spread_bps = max(0.0, (ask_1 - bid_1) / mid * 10000.0)
        sigma_bps = float(max(0.0, getattr(self.calibrators.get(sym), "sigma_bps", 10.0)))
        preds = predictor.update_and_predict(feats, mid, now, spread_bps=spread_bps, sigma_bps=sigma_bps)

        if now - self.last_cycle_ts[sym] >= self.cycle_interval:
            self.last_cycle_ts[sym] = now
            self.feature_engine.reset_interval(sym)

        try:
            signal = float(preds.get("1s", 0.0)) * self.weights.get("1s", 0.1) + float(preds.get("10s", 0.0)) * self.weights.get("10s", 0.5) + float(preds.get("30s", 0.0)) * self.weights.get("30s", 0.4)
        except Exception:
            signal = 0.0

        velocity = self._compute_signal_velocity(sym, signal, now)
        confidence = self._prediction_confidence(predictor, preds)

        self.latest_signal[sym] = signal
        self.latest_velocity[sym] = velocity
        self.latest_preds[sym] = {horizon: float(preds.get(horizon, 0.0)) for horizon in self.weights}
        self.latest_confidence[sym] = confidence
        self.latest_spread_bps[sym] = spread_bps
        self.latest_sigma_bps[sym] = sigma_bps

        if not self._check_warmup(sym):
            self._publish_warmup(sym, mid, signal, velocity, preds, predictor, confidence)
            return

        self._publish_state(sym, mid, bid_1, ask_1, signal, velocity, preds, predictor, confidence)
        self._run_fsm(sym, mid, bid_1, ask_1, signal, velocity, confidence, now)

    def on_market_trade(self, trade: AggTradeData):
        if self.alpha_process.enabled:
            self.alpha_process.submit_trade(trade)
            self.poll_async_workers()
            return

        self.feature_engine.on_trade(trade)
        current_mid = self.latest_mid.get(trade.symbol, 0.0)
        calibrator = self.calibrators.get(trade.symbol)
        if calibrator and current_mid > 0:
            calibrator.on_market_trade(trade, current_mid)

    def poll_async_workers(self):
        if not self.alpha_process.enabled:
            return

        snapshots = list(self.alpha_process.poll())
        quarantined_symbols = set()
        if hasattr(self.alpha_process, "drain_quarantine_events"):
            quarantined_symbols = set(self.alpha_process.drain_quarantine_events())
        for symbol in quarantined_symbols:
            self._begin_alpha_symbol_recovery(symbol, reset_model_state=True)
            self._freeze_alpha_symbol(symbol, "alpha_process_quarantined")

        current_quarantined_symbols = set()
        if hasattr(self.alpha_process, "get_quarantined_symbols"):
            current_quarantined_symbols = set(self.alpha_process.get_quarantined_symbols())
        for symbol in current_quarantined_symbols:
            self._begin_alpha_symbol_recovery(symbol, reset_model_state=False)
            self._freeze_alpha_symbol(symbol, "alpha_process_quarantined")

        restarted_symbols = set()
        if hasattr(self.alpha_process, "drain_restart_events"):
            restarted_symbols = set(self.alpha_process.drain_restart_events())
        for symbol in restarted_symbols:
            self._begin_alpha_symbol_recovery(symbol, reset_model_state=True)
            self._freeze_alpha_symbol(symbol, "alpha_process_recovering")

        recovering_symbols = set()
        if hasattr(self.alpha_process, "get_recovering_symbols"):
            recovering_symbols = set(self.alpha_process.get_recovering_symbols())
        for symbol in recovering_symbols:
            self._begin_alpha_symbol_recovery(symbol, reset_model_state=False)
            self._freeze_alpha_symbol(symbol, "alpha_process_recovering")

        for snapshot in snapshots:
            if snapshot.get("kind") == "alpha_snapshot":
                self._consume_alpha_snapshot(snapshot)

        if not self.alpha_process.is_healthy() and hasattr(self.oms, "freeze_strategy"):
            unhealthy_symbols = set()
            if hasattr(self.alpha_process, "get_unhealthy_symbols"):
                unhealthy_symbols = set(self.alpha_process.get_unhealthy_symbols())
            unhealthy_symbols.difference_update(current_quarantined_symbols)
            if unhealthy_symbols:
                for symbol in unhealthy_symbols:
                    self._begin_alpha_symbol_recovery(symbol, reset_model_state=False)
                    self._freeze_alpha_symbol(symbol, "alpha_process_unhealthy")
            elif not current_quarantined_symbols:
                self.oms.freeze_strategy(self.name, "alpha_process_unhealthy", cancel_active_orders=True)

    def stop_async_workers(self):
        if self.alpha_process.enabled:
            self.alpha_process.stop()

    def get_async_worker_metrics(self):
        if self.alpha_process.enabled:
            return self.alpha_process.get_metrics_snapshot()
        return {}

    def _consume_alpha_snapshot(self, snapshot: dict):
        sym = snapshot["symbol"]
        now = float(snapshot.get("now", time.time()) or time.time())
        if self.symbol_start_ts[sym] == 0.0:
            self.symbol_start_ts[sym] = now

        bid_1 = float(snapshot.get("bid_1", 0.0) or 0.0)
        ask_1 = float(snapshot.get("ask_1", 0.0) or 0.0)
        mid = float(snapshot.get("mid", 0.0) or 0.0)
        if bid_1 <= 0.0 or ask_1 <= 0.0 or mid <= 0.0:
            return

        preds = {horizon: float(snapshot.get("preds", {}).get(horizon, 0.0)) for horizon in self.weights}
        confidence = self._prediction_confidence_from_diagnostics(preds, snapshot.get("diagnostics", {}))
        try:
            signal = (
                float(preds.get("1s", 0.0)) * self.weights.get("1s", 0.1)
                + float(preds.get("10s", 0.0)) * self.weights.get("10s", 0.5)
                + float(preds.get("30s", 0.0)) * self.weights.get("30s", 0.4)
            )
        except Exception:
            signal = 0.0

        velocity = self._compute_signal_velocity(sym, signal, now)
        self.latest_mid[sym] = mid
        self.latest_signal[sym] = signal
        self.latest_velocity[sym] = velocity
        self.latest_preds[sym] = preds
        self.latest_confidence[sym] = confidence
        self.latest_spread_bps[sym] = float(snapshot.get("spread_bps", 0.0) or 0.0)
        self.latest_sigma_bps[sym] = float(snapshot.get("sigma_bps", 0.0) or 0.0)
        self.remote_predictor_ready[sym] = bool(snapshot.get("predictor_warmed_up", False))
        self.remote_model_meta[sym] = {
            "weights_1s": list(snapshot.get("weights_1s", ()) or ()),
            "warmup_progress": dict(snapshot.get("warmup_progress", {}) or {}),
        }

        if not self._check_warmup(sym, predictor_ready=self.remote_predictor_ready[sym]):
            self._publish_warmup(
                sym,
                mid,
                signal,
                velocity,
                preds,
                predictor=None,
                confidence=confidence,
                model_meta=self.remote_model_meta[sym],
            )
            return

        if sym in self.alpha_rewarming_symbols:
            self._complete_alpha_symbol_recovery(sym)

        self._publish_state(
            sym,
            mid,
            bid_1,
            ask_1,
            signal,
            velocity,
            preds,
            predictor=None,
            confidence=confidence,
            model_meta=self.remote_model_meta[sym],
        )
        self._run_fsm(sym, mid, bid_1, ask_1, signal, velocity, confidence, now)

    def _run_fsm(self, sym: str, mid: float, bid_1: float, ask_1: float, signal: float, velocity: float, confidence: float = 0.0, now: float = 0.0):
        curr_state = self.state[sym]
        preds = self.latest_preds[sym]
        net_pos = self.oms.exposure.net_positions.get(sym, 0.0)
        tick = self._tick_size(sym, mid)
        can_submit = self.can_submit_orders(sym)
        if confidence <= 0.0:
            confidence = self.latest_confidence[sym] if self.latest_confidence[sym] > 0.0 else 1.0


        if curr_state == "FLAT":
            if not can_submit:
                return
            if abs(net_pos) > 1e-6:
                self.state[sym] = "HOLDING"
                return

            maker_required, _ = self._required_signal_bps(sym, mid, bid_1, ask_1, "GTX")
            taker_required, _ = self._required_signal_bps(sym, mid, bid_1, ask_1, "IOC")
            regime_ok, regime_reason, spread_bps, sigma_bps = self._regime_status(sym, mid, bid_1, ask_1, signal, min(maker_required, taker_required), confidence)
            self.latest_regime[sym] = regime_reason
            if not regime_ok:
                self.latest_size_scale[sym] = 1.0
                return

            if signal > taker_required and velocity > self.base_velocity_threshold and self._has_consensus(preds, Side.BUY, "IOC"):
                vol, scale = self._sized_volume(sym, mid, signal, taker_required, confidence, spread_bps, sigma_bps)
                if vol <= 0:
                    return
                self.latest_size_scale[sym] = scale
                slippage = self._force_exit_slippage(sym)
                price = ref_data_manager.round_price(sym, ask_1 * (1 + slippage))
                self._entry(sym, Side.BUY, price, vol, "IOC")
            elif signal > maker_required and self._has_consensus(preds, Side.BUY, "GTX"):
                vol, scale = self._sized_volume(sym, mid, signal, maker_required, confidence, spread_bps, sigma_bps)
                if vol <= 0:
                    return
                self.latest_size_scale[sym] = scale
                price = ref_data_manager.round_price(sym, self._maker_entry_price(Side.BUY, bid_1, ask_1, tick))
                self._entry(sym, Side.BUY, price, vol, "GTX")
            elif signal < -taker_required and velocity < -self.base_velocity_threshold and self._has_consensus(preds, Side.SELL, "IOC"):
                vol, scale = self._sized_volume(sym, mid, signal, taker_required, confidence, spread_bps, sigma_bps)
                if vol <= 0:
                    return
                self.latest_size_scale[sym] = scale
                slippage = self._force_exit_slippage(sym)
                price = ref_data_manager.round_price(sym, bid_1 * (1 - slippage))
                self._entry(sym, Side.SELL, price, vol, "IOC")
            elif signal < -maker_required and self._has_consensus(preds, Side.SELL, "GTX"):
                vol, scale = self._sized_volume(sym, mid, signal, maker_required, confidence, spread_bps, sigma_bps)
                if vol <= 0:
                    return
                self.latest_size_scale[sym] = scale
                price = ref_data_manager.round_price(sym, self._maker_entry_price(Side.SELL, bid_1, ask_1, tick))
                self._entry(sym, Side.SELL, price, vol, "GTX")

        elif curr_state in {"ENTERING", "ENTERING_PARTIAL"}:
            oid = self.entry_oid[sym]
            has_working_entry = oid and oid in self.active_orders
            has_position = abs(net_pos) > 1e-6

            if has_working_entry:
                signal_faded = abs(signal) < self.cancel_threshold
                stale_maker = self.entry_mode[sym] == "GTX" and (now - self.entry_submit_ts[sym]) > self.max_entry_wait_sec
                partial_cleanup = curr_state == "ENTERING_PARTIAL"

                if signal_faded or stale_maker or partial_cleanup:
                    self.cancel_order(oid)
                return

            self.state[sym] = "HOLDING" if has_position else "FLAT"

        elif curr_state == "HOLDING":
            if abs(net_pos) < 1e-6:
                self.state[sym] = "FLAT"
                self._clear_oids(sym)
                return

            holding_time = now - self.pos_entry_ts[sym]
            aligned_signal = self._directional_signal(Side.BUY if net_pos > 0 else Side.SELL, signal)
            force_exit = holding_time > self.max_hold_sec or aligned_signal < -self.alpha_flip_exit_threshold_bps
            if holding_time > self.min_decay_hold_sec and aligned_signal < self.alpha_decay_exit_threshold_bps and confidence < self.confidence_floor:
                force_exit = True

            if force_exit:
                if not can_submit:
                    return
                self.cancel_all(sym)
                self.state[sym] = "EXITING"
                slippage = self._force_exit_slippage(sym)
                if net_pos > 0:
                    price = ref_data_manager.round_price(sym, bid_1 * (1 - slippage))
                    oid = self.exit_long(sym, price, abs(net_pos))
                    exit_side = Side.SELL
                else:
                    price = ref_data_manager.round_price(sym, ask_1 * (1 + slippage))
                    oid = self.exit_short(sym, price, abs(net_pos))
                    exit_side = Side.BUY
                if oid:
                    self.exit_oid[sym] = oid
                    self._track_order_context(oid, sym, exit_side, "IOC", "exit", price)
                return

            if not can_submit:
                return

            entry_px = self.entry_price[sym]
            exit_side, desired_price, _ = self._desired_exit_price(sym, net_pos, entry_px, bid_1, ask_1, tick, signal, confidence, holding_time)
            if self.exit_oid[sym]:
                meta = self.order_context.get(self.exit_oid[sym], {})
                current_price = float(meta.get("limit_price", desired_price))
                stale = (now - float(meta.get("submit_ts", now))) > self.exit_requote_sec
                price_gap = abs(current_price - desired_price) >= tick
                if stale and price_gap and self.exit_oid[sym] in self.active_orders:
                    self.cancel_order(self.exit_oid[sym])
                return

            self._place_exit(sym, exit_side, desired_price, abs(net_pos))

        elif curr_state == "EXITING":
            if abs(net_pos) < 1e-6:
                self.state[sym] = "FLAT"
                self._clear_oids(sym)
            elif not self.exit_oid[sym]:
                self.state[sym] = "HOLDING"

    def _entry(self, sym: str, side: Side, price: float, vol: float, mode: str):
        if self.entry_oid[sym]:
            self.cancel_order(self.entry_oid[sym])

        is_ioc = mode == "IOC"
        intent = OrderIntent(self.name, sym, side, price, vol, time_in_force="IOC" if is_ioc else "GTC", is_post_only=not is_ioc)
        oid = self.send_intent(intent)
        if oid:
            self.entry_oid[sym] = oid
            self.entry_mode[sym] = mode
            self.entry_submit_ts[sym] = time.time()
            self.state[sym] = "ENTERING"
            self._track_order_context(oid, sym, side, mode, "entry", price)
            sym_clean = sym.replace("USDC", "").replace("USDT", "").lower()
            side_str = "long" if side == Side.BUY else "short"
            self.log(f"{sym_clean} enter {side_str} @ {price:.6g} ({mode}, vol={vol})")

    def _place_exit(self, sym: str, side: Side, price: float, vol: float):
        intent = OrderIntent(self.name, sym, side, price, vol, is_post_only=True)
        oid = self.send_intent(intent)
        if oid:
            self.exit_oid[sym] = oid
            self._track_order_context(oid, sym, side, "GTX", "exit", price)
            sym_clean = sym.replace("USDC", "").replace("USDT", "").lower()
            pos_str = "short" if side == Side.BUY else "long"
            self.log(f"{sym_clean} exit {pos_str} @ {price:.6g} (GTX TP, vol={vol})")

    def _clear_oids(self, sym: str):
        if self.entry_oid[sym]:
            self.order_context.pop(self.entry_oid[sym], None)
        if self.exit_oid[sym]:
            self.order_context.pop(self.exit_oid[sym], None)
        self.entry_oid[sym] = None
        self.exit_oid[sym] = None
        self.entry_mode[sym] = None
        self.entry_submit_ts[sym] = 0.0
        self.pos_entry_ts[sym] = 0.0
        self.entry_price[sym] = 0.0

    def on_order(self, snapshot: OrderStateSnapshot):
        super().on_order(snapshot)

        sym = snapshot.symbol
        oid = snapshot.client_oid
        status = snapshot.status
        terminal = {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.REJECTED_LOCALLY, OrderStatus.EXPIRED}

        if oid == self.entry_oid[sym]:
            if status == OrderStatus.PARTIALLY_FILLED:
                self.state[sym] = "ENTERING_PARTIAL"
                if self.pos_entry_ts[sym] == 0.0:
                    self.pos_entry_ts[sym] = time.time()
                self.entry_price[sym] = snapshot.avg_price
                if oid in self.active_orders:
                    self.cancel_order(oid)
            elif status == OrderStatus.FILLED:
                self.state[sym] = "HOLDING"
                if self.pos_entry_ts[sym] == 0.0:
                    self.pos_entry_ts[sym] = time.time()
                self.entry_price[sym] = snapshot.avg_price
                self.entry_oid[sym] = None
            elif status in terminal:
                self.entry_oid[sym] = None
                self.entry_mode[sym] = None
                self.entry_submit_ts[sym] = 0.0
                if abs(self.oms.exposure.net_positions.get(sym, 0.0)) > 1e-6:
                    self.state[sym] = "HOLDING"
                elif self.state[sym] in {"ENTERING", "ENTERING_PARTIAL"}:
                    self.state[sym] = "FLAT"

        if oid == self.exit_oid[sym] and status in terminal:
            if abs(self.oms.exposure.net_positions.get(sym, 0.0)) > 1e-6:
                self.state[sym] = "HOLDING"
            self.exit_oid[sym] = None

        if status in terminal:
            self._finalize_exit_feedback(sym, oid)
            self.order_context.pop(oid, None)

    def on_trade(self, trade: TradeData):
        meta = self.order_context.get(trade.order_id)
        if not meta:
            return

        sym = trade.symbol
        feedback = self.execution_feedback[sym]
        side = meta.get("side")
        ref_mid = meta.get("mid", 0.0)
        mode = meta.get("mode", "IOC")

        if side in {Side.BUY, Side.SELL} and ref_mid > 0:
            edge_bps = self._execution_edge_bps(side, trade.price, ref_mid)
            if mode == "GTX":
                feedback["maker_edge_ewma"] = self._ema(feedback["maker_edge_ewma"], edge_bps)
                feedback["maker_fills"] += 1
            else:
                feedback["taker_edge_ewma"] = self._ema(feedback["taker_edge_ewma"], edge_bps)
                feedback["taker_fills"] += 1

        if meta.get("role") == "exit":
            entry_px = meta.get("entry_price", 0.0)
            if entry_px > 0:
                pnl_bps = self._exit_pnl_bps(side, trade.price, entry_px)
                meta["exit_pnl_sum"] = meta.get("exit_pnl_sum", 0.0) + pnl_bps * trade.volume
                meta["exit_qty"] = meta.get("exit_qty", 0.0) + trade.volume

    def _finalize_exit_feedback(self, sym: str, oid: str):
        meta = self.order_context.get(oid)
        if not meta or meta.get("role") != "exit":
            return

        exit_qty = meta.get("exit_qty", 0.0)
        if exit_qty <= 0:
            return

        avg_exit_pnl_bps = meta.get("exit_pnl_sum", 0.0) / exit_qty
        feedback = self.execution_feedback[sym]
        feedback["exit_pnl_ewma"] = self._ema(feedback["exit_pnl_ewma"], avg_exit_pnl_bps)
        feedback["win_rate_ewma"] = self._ema(feedback["win_rate_ewma"], 1.0 if avg_exit_pnl_bps > 0 else 0.0)
        feedback["closed_trades"] += 1

    def _publish_state(
        self,
        sym: str,
        mid: float,
        bid_1: float,
        ask_1: float,
        signal: float,
        velocity: float,
        preds: dict,
        predictor: TimeHorizonPredictor,
        confidence: float = 0.0,
        model_meta: dict = None,
    ):
        model_meta = model_meta or {}
        weights_1s = model_meta.get("weights_1s")
        if weights_1s is None and predictor is not None:
            weights_1s = predictor.get_model_weights("1s")
        weights_1s = weights_1s or []
        labeled_w = {FEATURE_LABELS[i]: round(weight, 4) for i, weight in enumerate(weights_1s) if i < len(FEATURE_LABELS)}
        warmup_prog = model_meta.get("warmup_progress")
        if warmup_prog is None and predictor is not None:
            warmup_prog = predictor.warmup_progress()
        warmup_prog = warmup_prog or {}
        maker_required, maker_cost = self._required_signal_bps(sym, mid, bid_1, ask_1, "GTX")
        taker_required, taker_cost = self._required_signal_bps(sym, mid, bid_1, ask_1, "IOC")
        consensus = self._consensus_direction(preds)
        feedback = self.execution_feedback[sym]
        health = self.last_system_health or getattr(self.oms.state, "value", str(self.oms.state))
        account_available = f"{self.latest_account.available:.1f}" if self.latest_account is not None else "-"
        reject_reason = self.last_submit_reject_by_symbol.get(sym, "-")
        block_reason = (
            self.oms.get_order_block_reason(self.name, sym)
            if hasattr(self.oms, "get_order_block_reason")
            else ""
        )
        regime_ok, regime_reason, spread_bps, sigma_bps = self._regime_status(sym, mid, bid_1, ask_1, signal, min(maker_required, taker_required), confidence)
        self.latest_regime[sym] = regime_reason
        self.latest_spread_bps[sym] = spread_bps
        self.latest_sigma_bps[sym] = sigma_bps

        if not self.can_submit_orders(sym):
            entry_mode = "PAUSED"
        elif not regime_ok:
            entry_mode = f"BLOCKED:{regime_reason}"
        elif signal > taker_required and velocity > self.base_velocity_threshold and self._has_consensus(preds, Side.BUY, "IOC"):
            entry_mode = "IOC(accel)"
        elif signal < -taker_required and velocity < -self.base_velocity_threshold and self._has_consensus(preds, Side.SELL, "IOC"):
            entry_mode = "IOC(accel)"
        elif signal > maker_required and self._has_consensus(preds, Side.BUY, "GTX"):
            entry_mode = "GTX"
        elif signal < -maker_required and self._has_consensus(preds, Side.SELL, "GTX"):
            entry_mode = "GTX"
        else:
            entry_mode = "-"

        params = {
            "State": self.state[sym],
            "Sig": f"{signal:+.2f}",
            "Vel": f"{velocity:+.2f}",
            "Conf": f"{confidence:.2f}",
            "Mode": entry_mode,
            "1s": f"{preds.get('1s', 0):+.1f}",
            "10s": f"{preds.get('10s', 0):+.1f}",
            "30s": f"{preds.get('30s', 0):+.1f}",
            "Consensus": "UP" if consensus > 0 else "DOWN" if consensus < 0 else "MIXED",
            "Regime": regime_reason,
            "Spread": f"{spread_bps:.1f}",
            "Sigma": f"{sigma_bps:.1f}",
            "Size": f"{self.latest_size_scale[sym]:.2f}x",
            "MakerReq": f"{maker_required:.1f}",
            "TakerReq": f"{taker_required:.1f}",
            "MakerCost": f"{maker_cost:.1f}",
            "TakerCost": f"{taker_cost:.1f}",
            "MEdge": f"{feedback['maker_edge_ewma']:+.2f}",
            "TEdge": f"{feedback['taker_edge_ewma']:+.2f}",
            "ExitEWMA": f"{feedback['exit_pnl_ewma']:+.2f}",
            "WinEWMA": f"{feedback['win_rate_ewma'] * 100:.0f}%",
            "Closed": feedback["closed_trades"],
            "Avail": account_available,
            "Health": health,
            "HealthDetail": self._oms_health_detail()[:72] or "-",
            "Rearm": "Y" if getattr(self.oms, "manual_rearm_required", False) else "N",
            "OMSMode": getattr(self.oms, "capability_mode", "-").value
            if hasattr(getattr(self.oms, "capability_mode", None), "value")
            else str(getattr(self.oms, "capability_mode", "-")),
            "Block": block_reason[:72] if block_reason else "-",
            "Reject": reject_reason[:32],
            "Blend": {horizon: round(self.weights.get(horizon, 0.0), 2) for horizon in ("1s", "10s", "30s")},
            "Weights": labeled_w,
            "Train": warmup_prog,
        }
        self.engine.put(Event(EVENT_STRATEGY_UPDATE, StrategyData(symbol=sym, fair_value=mid, alpha_bps=signal, params=params)))

    def _publish_warmup(
        self,
        sym: str,
        mid: float,
        signal: float,
        velocity: float,
        preds: dict,
        predictor: TimeHorizonPredictor,
        confidence: float = 0.0,
        model_meta: dict = None,
    ):
        started_at = self.symbol_start_ts[sym] or self.start_time
        elapsed = time.time() - started_at
        model_meta = model_meta or {}
        warmup_prog = model_meta.get("warmup_progress")
        if warmup_prog is None and predictor is not None:
            warmup_prog = predictor.warmup_progress()
        warmup_prog = warmup_prog or {}
        progress_pct = min(100.0, elapsed / self.min_warmup_sec * 100.0)

        params = {
            "State": f"WARMUP {progress_pct:.0f}%",
            "Sig": f"{signal:+.2f}",
            "Vel": f"{velocity:+.2f}",
            "Conf": f"{confidence:.2f}",
            "Consensus": "UP" if self._consensus_direction(preds) > 0 else "DOWN" if self._consensus_direction(preds) < 0 else "MIXED",
            "Avail": f"{self.latest_account.available:.1f}" if self.latest_account is not None else "-",
            "Health": self.last_system_health or getattr(self.oms.state, "value", str(self.oms.state)),
            "HealthDetail": self._oms_health_detail()[:72] or "-",
            "Rearm": "Y" if getattr(self.oms, "manual_rearm_required", False) else "N",
            "OMSMode": getattr(self.oms, "capability_mode", "-").value
            if hasattr(getattr(self.oms, "capability_mode", None), "value")
            else str(getattr(self.oms, "capability_mode", "-")),
            "Block": (
                self.oms.get_order_block_reason(self.name, sym)[:72]
                if hasattr(self.oms, "get_order_block_reason") and self.oms.get_order_block_reason(self.name, sym)
                else "-"
            ),
            "Reject": self.last_submit_reject_by_symbol.get(sym, "-")[:32],
            "Blend": {horizon: round(self.weights.get(horizon, 0.0), 2) for horizon in ("1s", "10s", "30s")},
            "Train": warmup_prog,
            "Weights": {
                FEATURE_LABELS[i]: round(weight, 4)
                for i, weight in enumerate(
                    (model_meta.get("weights_1s") or [])
                    or (predictor.get_model_weights("1s") if predictor is not None else [])
                )
                if i < len(FEATURE_LABELS)
            },
        }
        self.engine.put(Event(EVENT_STRATEGY_UPDATE, StrategyData(symbol=sym, fair_value=mid, alpha_bps=0, params=params)))
