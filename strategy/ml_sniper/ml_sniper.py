import time
from collections import defaultdict, deque

from event.type import (
    AggTradeData,
    Event,
    EVENT_STRATEGY_UPDATE,
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

from .config_loader import load_sniper_config
from .predictor import TimeHorizonPredictor


FEATURE_LABELS = ["Imb", "Dep", "Mic", "Trd", "Arr", "Vwp", "dIm", "dSp", "Mom"]


class MLSniperStrategy(StrategyTemplate):
    """
    Multi-horizon ML sniper strategy tuned for USDC perpetuals.

    State flow:
      FLAT -> ENTERING -> ENTERING_PARTIAL -> HOLDING -> EXITING -> FLAT
    """

    def __init__(self, engine, oms):
        super().__init__(engine, oms, "ML_Sniper_USDC")

        self.strat_conf = load_sniper_config()

        raw_weights = self.strat_conf.get(
            "weights", {"1s": 0.1, "10s": 0.5, "30s": 0.4}
        )
        self.weights = (
            raw_weights
            if isinstance(raw_weights, dict)
            else {"1s": 0.1, "10s": 0.5, "30s": 0.4}
        )

        self.lot_multiplier = self.strat_conf.get("lot_multiplier", 1.0)

        entry_cfg = self.strat_conf.get("entry", {})
        self.taker_entry_threshold = entry_cfg.get("taker_entry_threshold_bps", 20.0)
        self.maker_entry_threshold = entry_cfg.get("maker_entry_threshold_bps", 1.5)
        self.velocity_threshold = entry_cfg.get("velocity_threshold_bps", 3.0)
        self.cancel_threshold = entry_cfg.get("cancel_threshold_bps", 1.0)
        self.velocity_window = entry_cfg.get("velocity_window_frames", 3)
        self.max_entry_wait_sec = entry_cfg.get("max_entry_wait_sec", 2.0)

        exit_cfg = self.strat_conf.get("exit", {})
        self.profit_target = exit_cfg.get("profit_target_bps", 4.0)
        self.max_hold_sec = exit_cfg.get("max_holding_sec", 10.0)

        exe_cfg = self.strat_conf.get("execution", {})
        self.tick_interval = exe_cfg.get("tick_interval_sec", 0.1)
        self.cycle_interval = exe_cfg.get("cycle_interval_sec", 1.0)

        self.min_warmup_sec = self.strat_conf.get("min_warmup_sec", 60.0)
        self.start_time = time.time()
        self.is_warmed_up = False

        self.feature_engine = FeatureEngine()
        self.predictors = {}
        self.calibrators = {}

        self.state = defaultdict(lambda: "FLAT")
        self.pos_entry_ts = defaultdict(float)
        self.entry_price = defaultdict(float)
        self.entry_oid = defaultdict(lambda: None)
        self.exit_oid = defaultdict(lambda: None)
        self.entry_mode = defaultdict(lambda: None)
        self.entry_submit_ts = defaultdict(float)
        self.last_tick_ts = defaultdict(float)
        self.last_cycle_ts = defaultdict(float)

        self.signal_history = defaultdict(lambda: deque(maxlen=20))

    def _get_predictor(self, symbol: str) -> TimeHorizonPredictor:
        if symbol not in self.predictors:
            self.predictors[symbol] = TimeHorizonPredictor(num_features=9)
            self.calibrators[symbol] = GLFTCalibrator(window=500)
        return self.predictors[symbol]

    def _check_warmup(self, symbol: str) -> bool:
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

    def _calc_vol(self, symbol: str, price: float) -> float:
        info = ref_data_manager.get_info(symbol)
        if not info:
            return 0.0
        min_vol = max(info.min_qty, (info.min_notional * 1.1) / price)
        return ref_data_manager.round_qty(symbol, min_vol * self.lot_multiplier)

    def _tick_size(self, symbol: str, mid: float) -> float:
        info = ref_data_manager.get_info(symbol)
        if info and hasattr(info, "tick_size") and info.tick_size > 0:
            return info.tick_size
        return ref_data_manager.round_price(symbol, mid * 0.0001)

    def _force_exit_slippage(self, symbol: str) -> float:
        atr = getattr(self.calibrators.get(symbol), "sigma_bps", 20.0)
        slippage_bps = float(max(10.0, min(80.0, atr * 1.5)))
        return slippage_bps / 10000.0

    def _compute_signal_velocity(self, symbol: str, signal: float, now: float) -> float:
        hist = self.signal_history[symbol]
        hist.append((now, signal))

        if len(hist) < self.velocity_window + 1:
            return 0.0

        past_signal = hist[-(self.velocity_window + 1)][1]
        return signal - past_signal

    def _maker_entry_price(
        self,
        side: Side,
        bid_1: float,
        ask_1: float,
        tick: float,
    ) -> float:
        spread = max(0.0, ask_1 - bid_1)
        if spread >= 2 * tick:
            raw_price = bid_1 + tick if side == Side.BUY else ask_1 - tick
        else:
            raw_price = bid_1 if side == Side.BUY else ask_1
        return raw_price

    def on_orderbook(self, ob: OrderBook):
        now = time.time()
        sym = ob.symbol

        if now - self.last_tick_ts[sym] < self.tick_interval:
            return
        self.last_tick_ts[sym] = now

        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 == 0:
            return
        mid = (bid_1 + ask_1) / 2.0

        self.feature_engine.on_orderbook(ob)
        predictor = self._get_predictor(sym)
        self.calibrators[sym].on_orderbook(ob)

        feats = self.feature_engine.get_features(sym)
        preds = predictor.update_and_predict(feats, mid, now)

        if now - self.last_cycle_ts[sym] >= self.cycle_interval:
            self.last_cycle_ts[sym] = now
            self.feature_engine.reset_interval(sym)

        try:
            signal = (
                float(preds.get("1s", 0)) * self.weights.get("1s", 0.1)
                + float(preds.get("10s", 0)) * self.weights.get("10s", 0.5)
                + float(preds.get("30s", 0)) * self.weights.get("30s", 0.4)
            )
        except Exception:
            signal = 0.0

        velocity = self._compute_signal_velocity(sym, signal, now)

        if not self._check_warmup(sym):
            self._publish_warmup(sym, mid, signal, velocity, preds, predictor)
            return

        self._publish_state(sym, mid, signal, velocity, preds, predictor)
        self._run_fsm(sym, mid, bid_1, ask_1, signal, velocity, now)

    def on_market_trade(self, trade: AggTradeData):
        self.feature_engine.on_trade(trade)

    def _run_fsm(
        self,
        sym: str,
        mid: float,
        bid_1: float,
        ask_1: float,
        signal: float,
        velocity: float,
        now: float,
    ):
        curr_state = self.state[sym]
        net_pos = self.oms.exposure.net_positions.get(sym, 0.0)
        tick = self._tick_size(sym, mid)

        if curr_state == "FLAT":
            if abs(net_pos) > 1e-6:
                self.state[sym] = "HOLDING"
                return

            vol = self._calc_vol(sym, mid)
            if vol <= 0:
                return

            if signal > self.maker_entry_threshold:
                if (
                    signal > self.taker_entry_threshold
                    and velocity > self.velocity_threshold
                ):
                    slippage = self._force_exit_slippage(sym)
                    price = ref_data_manager.round_price(sym, ask_1 * (1 + slippage))
                    self._entry(sym, Side.BUY, price, vol, "IOC")
                else:
                    price = ref_data_manager.round_price(
                        sym, self._maker_entry_price(Side.BUY, bid_1, ask_1, tick)
                    )
                    self._entry(sym, Side.BUY, price, vol, "GTX")

            elif signal < -self.maker_entry_threshold:
                if (
                    signal < -self.taker_entry_threshold
                    and velocity < -self.velocity_threshold
                ):
                    slippage = self._force_exit_slippage(sym)
                    price = ref_data_manager.round_price(sym, bid_1 * (1 - slippage))
                    self._entry(sym, Side.SELL, price, vol, "IOC")
                else:
                    price = ref_data_manager.round_price(
                        sym, self._maker_entry_price(Side.SELL, bid_1, ask_1, tick)
                    )
                    self._entry(sym, Side.SELL, price, vol, "GTX")

        elif curr_state in {"ENTERING", "ENTERING_PARTIAL"}:
            oid = self.entry_oid[sym]
            has_working_entry = oid and oid in self.active_orders
            has_position = abs(net_pos) > 1e-6

            if has_working_entry:
                signal_faded = abs(signal) < self.cancel_threshold
                stale_maker = (
                    self.entry_mode[sym] == "GTX"
                    and (now - self.entry_submit_ts[sym]) > self.max_entry_wait_sec
                )
                partial_cleanup = curr_state == "ENTERING_PARTIAL"

                if signal_faded or stale_maker or partial_cleanup:
                    self.cancel_order(oid)
                return

            if has_position:
                self.state[sym] = "HOLDING"
            else:
                self.state[sym] = "FLAT"

        elif curr_state == "HOLDING":
            if abs(net_pos) < 1e-6:
                self.state[sym] = "FLAT"
                self._clear_oids(sym)
                return

            holding_time = now - self.pos_entry_ts[sym]

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
                    price = ref_data_manager.round_price(sym, bid_1 * (1 - slippage))
                    self.exit_long(sym, price, abs(net_pos))
                else:
                    price = ref_data_manager.round_price(sym, ask_1 * (1 + slippage))
                    self.exit_short(sym, price, abs(net_pos))
                return

            if not self.exit_oid[sym]:
                entry_px = self.entry_price[sym]
                if net_pos > 0:
                    target = entry_px * (1 + self.profit_target / 10000.0)
                    price = ref_data_manager.round_price(sym, max(target, bid_1 + tick))
                    self._place_exit(sym, Side.SELL, price, abs(net_pos))
                else:
                    target = entry_px * (1 - self.profit_target / 10000.0)
                    price = ref_data_manager.round_price(sym, min(target, ask_1 - tick))
                    self._place_exit(sym, Side.BUY, price, abs(net_pos))

        elif curr_state == "EXITING":
            if abs(net_pos) < 1e-6:
                self.state[sym] = "FLAT"
                self._clear_oids(sym)

    def _entry(self, sym: str, side: Side, price: float, vol: float, mode: str):
        if self.entry_oid[sym]:
            self.cancel_order(self.entry_oid[sym])

        is_ioc = mode == "IOC"
        intent = OrderIntent(
            self.name,
            sym,
            side,
            price,
            vol,
            time_in_force="IOC" if is_ioc else "GTC",
            is_post_only=not is_ioc,
        )
        oid = self.send_intent(intent)
        if oid:
            self.entry_oid[sym] = oid
            self.entry_mode[sym] = mode
            self.entry_submit_ts[sym] = time.time()
            self.state[sym] = "ENTERING"
            sym_clean = sym.replace("USDC", "").replace("USDT", "").lower()
            side_str = "long" if side == Side.BUY else "short"
            self.log(f"{sym_clean} enter {side_str} @ {price:.6g}  ({mode}, vol={vol})")

    def _place_exit(self, sym: str, side: Side, price: float, vol: float):
        intent = OrderIntent(self.name, sym, side, price, vol, is_post_only=True)
        oid = self.send_intent(intent)
        if oid:
            self.exit_oid[sym] = oid
            sym_clean = sym.replace("USDC", "").replace("USDT", "").lower()
            pos_str = "short" if side == Side.BUY else "long"
            self.log(f"{sym_clean} exit  {pos_str} @ {price:.6g}  (GTX TP, vol={vol})")

    def _clear_oids(self, sym: str):
        self.entry_oid[sym] = None
        self.exit_oid[sym] = None
        self.entry_mode[sym] = None
        self.entry_submit_ts[sym] = 0.0
        self.pos_entry_ts[sym] = 0.0

    def on_order(self, snapshot: OrderStateSnapshot):
        super().on_order(snapshot)

        sym = snapshot.symbol
        oid = snapshot.client_oid
        status = snapshot.status

        terminal = {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.REJECTED_LOCALLY,
            OrderStatus.EXPIRED,
        }

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
            self.exit_oid[sym] = None

    def on_trade(self, trade: TradeData):
        pass

    def _publish_state(
        self,
        sym: str,
        mid: float,
        signal: float,
        velocity: float,
        preds: dict,
        predictor: TimeHorizonPredictor,
    ):
        weights_1s = predictor.get_model_weights("1s")
        labeled_w = {
            FEATURE_LABELS[i]: round(w, 4)
            for i, w in enumerate(weights_1s)
            if i < len(FEATURE_LABELS)
        }
        warmup_prog = predictor.warmup_progress()

        if (
            abs(signal) > self.taker_entry_threshold
            and abs(velocity) > self.velocity_threshold
        ):
            entry_mode = "IOC(accel)"
        elif abs(signal) > self.maker_entry_threshold:
            entry_mode = "GTX"
        else:
            entry_mode = "-"

        params = {
            "State": self.state[sym],
            "Sig": f"{signal:+.2f}",
            "Vel": f"{velocity:+.2f}",
            "Mode": entry_mode,
            "1s": f"{preds.get('1s', 0):+.1f}",
            "10s": f"{preds.get('10s', 0):+.1f}",
            "30s": f"{preds.get('30s', 0):+.1f}",
            "Weights": labeled_w,
            "Train": warmup_prog,
        }
        self.engine.put(
            Event(
                EVENT_STRATEGY_UPDATE,
                StrategyData(symbol=sym, fair_value=mid, alpha_bps=signal, params=params),
            )
        )

    def _publish_warmup(
        self,
        sym: str,
        mid: float,
        signal: float,
        velocity: float,
        preds: dict,
        predictor: TimeHorizonPredictor,
    ):
        elapsed = time.time() - self.start_time
        warmup_prog = predictor.warmup_progress()
        progress_pct = min(100.0, elapsed / self.min_warmup_sec * 100)

        params = {
            "State": f"WARMUP {progress_pct:.0f}%",
            "Sig": f"{signal:+.2f}",
            "Vel": f"{velocity:+.2f}",
            "Train": warmup_prog,
            "Weights": {
                FEATURE_LABELS[i]: round(w, 4)
                for i, w in enumerate(predictor.get_model_weights("1s"))
                if i < len(FEATURE_LABELS)
            },
        }
        self.engine.put(
            Event(
                EVENT_STRATEGY_UPDATE,
                StrategyData(symbol=sym, fair_value=mid, alpha_bps=0, params=params),
            )
        )
