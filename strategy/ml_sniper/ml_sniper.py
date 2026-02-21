# file: strategy/ml_sniper/strategy.py

import time
from collections import defaultdict
from event.type import (
    OrderBook, TradeData, OrderIntent, Side, AggTradeData, 
    OrderStateSnapshot, OrderStatus, Event, 
    EVENT_STRATEGY_UPDATE, StrategyData
)
from ..base import StrategyTemplate
from alpha.engine import FeatureEngine
from data.ref_data import ref_data_manager
from data.cache import data_cache

from .predictor import TimeHorizonPredictor
from .config_loader import load_sniper_config

FEATURE_LABELS = ["Imb", "Dep", "Mic", "Trd", "Arr", "Vwp", "dIm", "dSp", "Mom"]

class MLSniperStrategy(StrategyTemplate):
    def __init__(self, engine, oms):
        super().__init__(engine, oms, "ML_Sniper_KF")
        
        # 1. 配置加载
        self.strat_conf = load_sniper_config()
        # 确保 weights 是字典类型，防止出现 string indices 错误
        raw_weights = self.strat_conf.get("weights", {"1s": 0.6, "10s": 0.3, "30s": 0.1})
        self.weights = raw_weights if isinstance(raw_weights, dict) else {"1s": 0.6, "10s": 0.3, "30s": 0.1}
        
        self.lot_multiplier = self.strat_conf.get("lot_multiplier", 1.0)
        
        # 提取嵌套配置
        entry_cfg = self.strat_conf.get("entry", {})
        self.taker_entry_threshold = entry_cfg.get("taker_entry_threshold_bps", 4.0)
        self.maker_entry_threshold = entry_cfg.get("maker_entry_threshold_bps", 1.5)
        
        exit_cfg = self.strat_conf.get("exit", {})
        self.profit_target = exit_cfg.get("profit_target_bps", 3.0)
        self.max_hold_sec = exit_cfg.get("max_holding_sec", 5.0)

        # 2. 预热逻辑
        self.warmup_duration = self.strat_conf.get("warmup_duration_sec", 300.0)
        self.start_time = time.time()
        self.is_warmed_up = False

        # 3. 组件
        self.feature_engine = FeatureEngine()
        self.predictors = {} 

        # 4. 状态机
        self.state = defaultdict(lambda: "FLAT")
        self.pos_entry_ts = defaultdict(float)
        self.entry_price = defaultdict(float)
        self.entry_oid = defaultdict(lambda: None)
        self.exit_oid = defaultdict(lambda: None)
        self.last_tick_ts = defaultdict(float)
        self.tick_interval = 0.1

    def _get_predictor(self, symbol):
        if symbol not in self.predictors:
            self.predictors[symbol] = TimeHorizonPredictor(num_features=9)
        return self.predictors[symbol]

    def _calc_vol(self, symbol, price):
        info = ref_data_manager.get_info(symbol)
        if not info: return 0.0
        min_vol_val = (info.min_notional * 1.1) / price
        base_vol = max(info.min_qty, min_vol_val)
        return ref_data_manager.round_qty(symbol, base_vol * self.lot_multiplier)

    def on_orderbook(self, ob: OrderBook):
        now = time.time()
        sym = ob.symbol
        if now - self.last_tick_ts[sym] < self.tick_interval: return
        self.last_tick_ts[sym] = now
        
        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 == 0: return
        mid = (bid_1 + ask_1) / 2.0

        # 1. 预测
        self.feature_engine.on_orderbook(ob)
        feats = self.feature_engine.get_features(sym)
        preds = self._get_predictor(sym).update_and_predict(feats, mid, now)
        
        # 2. 信号合成 (带安全检查)
        try:
            signal = (float(preds.get("1s", 0)) * self.weights.get("1s", 0) + 
                      float(preds.get("10s", 0)) * self.weights.get("10s", 0) + 
                      float(preds.get("30s", 0)) * self.weights.get("30s", 0))
        except:
            signal = 0.0

        # 3. 预热处理
        elapsed = now - self.start_time
        if elapsed < self.warmup_duration:
            self._update_ui_warmup(sym, mid, signal, preds, elapsed)
            return
        elif not self.is_warmed_up:
            self.is_warmed_up = True

        self._update_ui(sym, mid, signal, preds)
        self.feature_engine.reset_interval(sym)
        self._run_fsm(sym, mid, bid_1, ask_1, signal, now)

    def _update_ui(self, sym, mid, signal, preds):
        predictor = self._get_predictor(sym)
        weights_1s = predictor.get_model_weights("1s")
        
        # [NEW] 将权重转换为带标签的字典
        # 只保留绝对值较大的权重，避免 UI 过于拥挤
        labeled_weights = {}
        for i, w in enumerate(weights_1s):
            if i < len(FEATURE_LABELS):
                label = FEATURE_LABELS[i]
                labeled_weights[label] = w

        params = {
            "State": self.state[sym],
            "Sig": f"{signal:+.1f}",
            "1s": f"{preds.get('1s', 0):+.1f}",
            # [修改] 传递字典类型的权重
            "Weights": labeled_weights 
        }
        
        self.engine.put(Event(EVENT_STRATEGY_UPDATE, StrategyData(
            symbol=sym, fair_value=mid, alpha_bps=signal, params=params
        )))

    def _update_ui_warmup(self, sym, mid, signal, preds, elapsed):
        predictor = self._get_predictor(sym)
        weights_1s = predictor.get_model_weights("1s")
        
        labeled_weights = {}
        for i, w in enumerate(weights_1s):
            if i < len(FEATURE_LABELS):
                labeled_weights[FEATURE_LABELS[i]] = w

        progress = (elapsed / self.warmup_duration) * 100
        params = {
            "State": f"WARM {progress:.0f}%",
            "Sig": f"{signal:+.1f}",
            "Weights": labeled_weights # 预热期也看权重收敛
        }
        self.engine.put(Event(EVENT_STRATEGY_UPDATE, StrategyData(
            symbol=sym, fair_value=mid, alpha_bps=0, params=params
        )))

    def _run_fsm(self, sym, mid, bid_1, ask_1, signal, now):
        curr_state = self.state[sym]
        net_pos = self.oms.exposure.net_positions.get(sym, 0.0)

        if curr_state == "FLAT":
            if abs(net_pos) > 1e-6:
                self.state[sym] = "HOLDING"
                return
            vol = self._calc_vol(sym, mid)
            if vol <= 0: return

            if signal > self.maker_entry_threshold:
                if signal > self.taker_entry_threshold:
                    price = ref_data_manager.round_price(sym, ask_1 * 1.002)
                    self._entry(sym, Side.BUY, price, vol, "IOC")
                else:
                    price = ref_data_manager.round_price(sym, bid_1)
                    self._entry(sym, Side.BUY, price, vol, "GTX")
            elif signal < -self.maker_entry_threshold:
                if signal < -self.taker_entry_threshold:
                    price = ref_data_manager.round_price(sym, bid_1 * 0.998)
                    self._entry(sym, Side.SELL, price, vol, "IOC")
                else:
                    price = ref_data_manager.round_price(sym, ask_1)
                    self._entry(sym, Side.SELL, price, vol, "GTX")

        elif curr_state == "ENTERING":
            oid = self.entry_oid[sym]
            if oid and oid in self.active_orders:
                if abs(signal) < self.maker_entry_threshold * 0.5:
                    self.cancel_order(oid)

        elif curr_state == "HOLDING":
            if abs(net_pos) < 1e-6:
                self.state[sym] = "FLAT"
                self._clear_oids(sym)
                return
            holding_time = now - self.pos_entry_ts[sym]
            force_exit = False
            if holding_time > self.max_hold_sec: force_exit = True
            if net_pos > 0 and signal < -self.taker_entry_threshold: force_exit = True
            if net_pos < 0 and signal > self.taker_entry_threshold: force_exit = True

            if force_exit:
                self.cancel_all(sym)
                self.state[sym] = "EXITING"
                if net_pos > 0:
                    price = ref_data_manager.round_price(sym, bid_1 * 0.995)
                    self.exit_long(sym, price, abs(net_pos))
                else:
                    price = ref_data_manager.round_price(sym, ask_1 * 1.005)
                    self.exit_short(sym, price, abs(net_pos))
                return

            if not self.exit_oid[sym]:
                entry_px = self.entry_price[sym]
                if net_pos > 0:
                    target = entry_px * (1 + self.profit_target / 10000.0)
                    price = ref_data_manager.round_price(sym, max(target, ask_1))
                    self._place_exit(sym, Side.SELL, price, abs(net_pos))
                else:
                    target = entry_px * (1 - self.profit_target / 10000.0)
                    price = ref_data_manager.round_price(sym, min(target, bid_1))
                    self._place_exit(sym, Side.BUY, price, abs(net_pos))

        elif curr_state == "EXITING":
            if abs(net_pos) < 1e-6:
                self.state[sym] = "FLAT"
                self._clear_oids(sym)

    def _entry(self, sym, side, price, vol, mode):
        if self.entry_oid[sym]: self.cancel_order(self.entry_oid[sym])
        intent = OrderIntent(self.name, sym, side, price, vol, 
                             time_in_force="IOC" if mode=="IOC" else "GTC",
                             is_post_only=(mode=="GTX"))
        oid = self.send_intent(intent)
        if oid:
            self.entry_oid[sym] = oid
            self.state[sym] = "ENTERING"

    def _place_exit(self, sym, side, price, vol):
        intent = OrderIntent(self.name, sym, side, price, vol, is_post_only=True)
        oid = self.send_intent(intent)
        if oid: self.exit_oid[sym] = oid

    def _clear_oids(self, sym):
        self.entry_oid[sym] = None
        self.exit_oid[sym] = None

    def on_market_trade(self, trade: AggTradeData):
        self.feature_engine.on_trade(trade)

    def on_order(self, snapshot: OrderStateSnapshot):
        super().on_order(snapshot)
        sym = snapshot.symbol
        status = snapshot.status
        oid = snapshot.client_oid
        
        terminal_statuses = [OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED]
        
        if oid == self.entry_oid[sym]:
            if status in [OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED]:
                self.state[sym] = "HOLDING"
                self.pos_entry_ts[sym] = time.time()
                self.entry_price[sym] = snapshot.avg_price
                if status == OrderStatus.FILLED: self.entry_oid[sym] = None
            elif status in terminal_statuses:
                self.entry_oid[sym] = None
                if self.state[sym] == "ENTERING": self.state[sym] = "FLAT"

        if oid == self.exit_oid[sym]:
            if status in terminal_statuses:
                self.exit_oid[sym] = None