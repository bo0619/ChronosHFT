# file: strategy/glft_strategy.py

import time
import math
from collections import defaultdict, deque

from .base import StrategyTemplate
from event.type import (
    OrderBook, TradeData, OrderIntent, Side,
    AggTradeData, OrderStateSnapshot, OrderStatus,
    Event, EVENT_STRATEGY_UPDATE, StrategyData
)

from alpha.factors import GLFTCalibrator
from alpha.signal import MultiHorizonPredictor
from alpha.gate import AlphaGate
from alpha.engine import FeatureEngine

from data.ref_data import ref_data_manager
from data.cache import data_cache
from infrastructure.config_scaling import load_root_config


def _negative_infinity():
    return -math.inf


class GLFTStrategy(StrategyTemplate):
    def __init__(self, engine, oms, strategy_config=None):
        super().__init__(engine, oms, "GLFT_MultiScale")

        if strategy_config is None:
            full_config = self._load_full_config()
            self.strat_conf = full_config.get("strategy", {})
        else:
            self.strat_conf = dict(strategy_config)
        glft_conf = self.strat_conf.get("glft", {})
        self.use_rpi = bool(self.strat_conf.get("use_rpi", False)) and bool(
            glft_conf.get(
                "use_rpi",
                self.strat_conf.get("use_rpi_for_glft", True),
            )
        )
        self.rpi_fallback_to_gtx = bool(
            glft_conf.get(
                "rpi_fallback_to_gtx",
                self.strat_conf.get("rpi_fallback_to_gtx", True),
            )
        )

        # 基础参数
        self.gamma_base = self.strat_conf.get("gamma", 0.1)
        self.configure_quote_sizing(self.strat_conf)
        self.cycle_interval = self.strat_conf.get("cycle_interval", 1.0)
        self.min_spread_bps = self.strat_conf.get("execution", {}).get("min_spread_bps", 5.0)

        # ========= OrderFlow 参数 =========
        self.of_lambda = 0.2
        self.imbalance_ewma = defaultdict(float)

        # ========= 成交强度与防御 =========
        self.trade_timestamps = defaultdict(deque)
        
        # Durations use a monotonic clock.  Negative infinity keeps the
        # post-fill defense inactive until a real fill has been observed.
        self.last_fill_time = defaultdict(_negative_infinity)

        # ========= 执行状态 =========
        self.quote_state = defaultdict(lambda: {
            "bid_oid": None, "ask_oid": None,
            "bid_price": None, "ask_price": None,
            "bid_volume": None, "ask_volume": None,
            "last_update": float("-inf")
        })
        self.cooldown_ms = 200

        # ========= 核心组件 =========
        self.feature_engine = FeatureEngine()
        self.calibrators = {}
        self.models = {}
        self.gates = {}
        self.last_run_times = defaultdict(_negative_infinity)
        
        # 信号权重配置
        self.alpha_weights = {
            "short_fv_weight": 1.0,  
            "mid_spr_weight": 0.2,   
            "long_pos_weight": 500.0 
        }
        
        print(f"[{self.name}] 策略已启动. BaseGamma={self.gamma_base}")

    def _load_full_config(self):
        return load_root_config("config.json")

    def _get_components(self, symbol):
        if symbol not in self.calibrators:
            self.calibrators[symbol] = GLFTCalibrator(window=1000)
            self.models[symbol] = MultiHorizonPredictor(num_features=9)
            self.gates[symbol] = AlphaGate(max_bps=10.0, decay_factor=0.9, inventory_dampening=0.05)
        return self.calibrators[symbol], self.models[symbol], self.gates[symbol]

    def _calculate_safe_vol(
        self,
        symbol,
        price,
        *,
        side=None,
        current_position=0.0,
        reference_price=None,
    ):
        return self.calculate_quote_volume(
            symbol,
            price,
            side=side,
            current_position=current_position,
            reference_price=reference_price,
        )

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
        now = time.perf_counter()
        if now - self.last_run_times[symbol] < self.cycle_interval:
            return
        self.last_run_times[symbol] = now

        # 3. 市场切片
        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 == 0:
            return
        mid = (bid_1 + ask_1) / 2.0

        # 4. [Multi-Scale ML] 预测
        features = self.feature_engine.get_features(symbol)
        alphas = model.update_and_predict(features, mid, now)
        
        # 5. [Signal Mapping] 
        short_signal = gate.process(alphas["short"], 0) 
        fair_mid = mid * (1 + short_signal * self.alpha_weights["short_fv_weight"] / 10000.0)
        
        mid_signal_strength = abs(alphas["mid"])
        
        target_pos_usdt = alphas["long"] * self.alpha_weights["long_pos_weight"]
        target_position_limit = (
            self.max_pos_usdt if self.max_pos_usdt > 0.0 else 2000.0
        )
        target_pos_usdt = max(
            -target_position_limit,
            min(target_position_limit, target_pos_usdt),
        )

        # 6. GLFT 参数准备
        acc = self.oms.account
        gamma = self.gamma_base
        
        # A. 资金占用防御
        margin_usage = 0.0
        if acc.equity > 0:
            margin_usage = acc.used_margin / acc.equity
            gamma *= (1 + max(0, (margin_usage - 0.5) * 4))
            
        # B. 订单流失衡防御
        of_imb = abs(self.imbalance_ewma[symbol])
        gamma *= (1.0 + 3.0 * of_imb)
        
        # C. [修复] 刚刚成交后的防御 (Post-Trade Defense)
        # 如果最近 2 秒内有成交，暂时加大 gamma 防止连续被穿
        recent_fill_defense = now - self.last_fill_time[symbol] < 2.0
        if recent_fill_defense:
            gamma *= 1.5

        sigma = max(1.0, calibrator.sigma_bps)
        A = max(0.1, calibrator.A)
        k_base = max(0.1, calibrator.k)
        
        k = k_base / (1.0 + mid_signal_strength * self.alpha_weights["mid_spr_weight"])

        # 7. 计算库存偏移
        current_pos = self.oms.exposure.net_positions.get(symbol, 0.0)
        order_vol = self._calculate_safe_vol(symbol, mid)
        if order_vol <= 0:
            return
        
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
        
        passive_tif = self.resolve_passive_time_in_force(
            symbol,
            use_rpi=self.use_rpi,
            fallback_to_gtx=self.rpi_fallback_to_gtx,
            route="glft_quote",
        )
        effective_min_spread_bps = max(
            self.min_spread_bps,
            self.passive_round_trip_fee_bps(symbol, passive_tif),
        )
        min_half = mid * (effective_min_spread_bps / 20000.0)
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

        bid_order_vol = self._calculate_safe_vol(
            symbol,
            target_bid,
            side=Side.BUY,
            current_position=current_pos,
            reference_price=mid,
        )
        ask_order_vol = self._calculate_safe_vol(
            symbol,
            target_ask,
            side=Side.SELL,
            current_position=current_pos,
            reference_price=mid,
        )

        # 10. 执行更新 (增量改单)
        self._update_quotes(
            symbol,
            target_bid,
            target_ask,
            order_vol,
            time_in_force=passive_tif,
            bid_volume=bid_order_vol,
            ask_volume=ask_order_vol,
        )

        # 11. 状态广播
        quote_state = self.quote_state[symbol]
        quote_spread_bps = (
            (target_ask - target_bid) / mid * 10000.0
            if mid > 0.0
            else 0.0
        )
        params = {
            "schema": "market_making.v1",
            "strategy": self.name,
            "state": "QUOTING",
            "mode": passive_tif,
            "time_in_force": passive_tif,
            "use_rpi": bool(self.use_rpi),
            "rpi_supported": ref_data_manager.supports_rpi(symbol),
            "mid_price": mid,
            "best_bid": bid_1,
            "best_ask": ask_1,
            "market_spread_bps": (
                (ask_1 - bid_1) / mid * 10000.0
                if mid > 0.0
                else 0.0
            ),
            "fair_value": fair_mid,
            "alpha_bps": short_signal,
            "position_qty": current_pos,
            "position_notional": current_pos_usdt,
            "target_bid": target_bid,
            "target_ask": target_ask,
            "quote_spread_bps": quote_spread_bps,
            "quote_qty": max(bid_order_vol, ask_order_vol),
            "bid_quote_qty": bid_order_vol,
            "ask_quote_qty": ask_order_vol,
            "target_order_notional": self.target_order_notional,
            "max_position_notional": self.max_pos_usdt,
            "bid_order_id": quote_state["bid_oid"] or "",
            "ask_order_id": quote_state["ask_oid"] or "",
            "gamma_base": float(self.gamma_base),
            "gamma": gamma,
            "k_base": k_base,
            "k": k,
            "A": A,
            "sigma_bps": sigma,
            "margin_usage": margin_usage,
            "orderflow_imbalance": self.imbalance_ewma[symbol],
            "recent_fill_defense": recent_fill_defense,
            "signals": {
                horizon: float(value)
                for horizon, value in alphas.items()
            },
            "short_signal_bps": short_signal,
            "mid_signal_strength": mid_signal_strength,
            "target_position_notional": target_pos_usdt,
            "effective_position_notional": effective_pos_usdt,
            "q_norm": q_norm_effective,
            "base_half_spread_bps": base_half_spread_bps,
            "inventory_skew_bps": inventory_skew_bps,
            "base_spread_price": base_spread_price,
            "skew_price": skew_price,
            "configured_min_spread_bps": self.min_spread_bps,
            "passive_fee_bps": self.passive_round_trip_fee_bps(
                symbol,
                passive_tif,
            ),
            "effective_min_spread_bps": effective_min_spread_bps,
            # Compatibility fields consumed by the existing TUI.
            "State": "QUOTING",
            "Mode": passive_tif,
            "Spread": f"{quote_spread_bps:.1f}",
            "Sigma": f"{sigma:.1f}",
            "Size": f"{max(bid_order_vol, ask_order_vol):.8g}",
        }
        strat_data = StrategyData(
            symbol=symbol,
            fair_value=fair_mid,
            alpha_bps=short_signal,
            params=params,
        )
        self.engine.put(Event(EVENT_STRATEGY_UPDATE, strat_data))
        self.feature_engine.reset_interval(symbol)

    def _update_quotes(
        self,
        symbol,
        bid,
        ask,
        volume,
        time_in_force=None,
        bid_volume=None,
        ask_volume=None,
    ):
        state = self.quote_state[symbol]
        info = ref_data_manager.get_info(symbol)
        tick = info.tick_size
        now = time.perf_counter()
        time_in_force = time_in_force or self.resolve_passive_time_in_force(
            symbol,
            use_rpi=self.use_rpi,
            fallback_to_gtx=self.rpi_fallback_to_gtx,
            route="glft_quote",
        )
        bid_volume = max(
            0.0,
            float(volume if bid_volume is None else bid_volume),
        )
        ask_volume = max(
            0.0,
            float(volume if ask_volume is None else ask_volume),
        )
        qty_step = max(float(info.step_size or 0.0), 1e-12)
        
        if (now - state["last_update"]) * 1000 < self.cooldown_ms:
            return

        # Buy Side
        bid_price_changed = (
            state["bid_price"] is None
            or abs(bid - state["bid_price"]) >= tick
        )
        bid_volume_changed = (
            state.get("bid_volume") is None
            or abs(bid_volume - state["bid_volume"]) >= qty_step
        )
        if bid_volume <= 0.0:
            if state["bid_oid"]:
                self.cancel_order(state["bid_oid"])
            else:
                state["bid_price"] = None
                state["bid_volume"] = None
        elif bid_price_changed or bid_volume_changed:
            if state["bid_oid"]:
                self.cancel_order(state["bid_oid"])
            else:
                oid = self.send_intent(
                    OrderIntent(
                        self.name,
                        symbol,
                        Side.BUY,
                        bid,
                        bid_volume,
                        time_in_force=time_in_force,
                        is_post_only=True,
                    )
                )
                if oid:
                    state["bid_oid"] = oid
                    state["bid_price"] = bid
                    state["bid_volume"] = bid_volume
        
        # Sell Side
        ask_price_changed = (
            state["ask_price"] is None
            or abs(ask - state["ask_price"]) >= tick
        )
        ask_volume_changed = (
            state.get("ask_volume") is None
            or abs(ask_volume - state["ask_volume"]) >= qty_step
        )
        if ask_volume <= 0.0:
            if state["ask_oid"]:
                self.cancel_order(state["ask_oid"])
            else:
                state["ask_price"] = None
                state["ask_volume"] = None
        elif ask_price_changed or ask_volume_changed:
            if state["ask_oid"]:
                self.cancel_order(state["ask_oid"])
            else:
                oid = self.send_intent(
                    OrderIntent(
                        self.name,
                        symbol,
                        Side.SELL,
                        ask,
                        ask_volume,
                        time_in_force=time_in_force,
                        is_post_only=True,
                    )
                )
                if oid:
                    state["ask_oid"] = oid
                    state["ask_price"] = ask
                    state["ask_volume"] = ask_volume
                
        state["last_update"] = now

    def on_market_trade(self, trade: AggTradeData):
        self.feature_engine.on_trade(trade)
        now = time.perf_counter()
        self.trade_timestamps[trade.symbol].append(now)
        
        sign = -1 if trade.maker_is_buyer else 1
        prev = self.imbalance_ewma[trade.symbol]
        self.imbalance_ewma[trade.symbol] = (1 - self.of_lambda) * prev + self.of_lambda * sign

        if trade.symbol in self.calibrators:
            mid = data_cache.get_mark_price(trade.symbol)
            self.calibrators[trade.symbol].on_market_trade(trade, mid)

    def on_order(self, snapshot: OrderStateSnapshot):
        super().on_order(snapshot)
        # 订单终结时清理 Quote State；下一次行情回调才允许重挂。
        terminal_statuses = {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.REJECTED_LOCALLY,
            OrderStatus.EXPIRED,
        }
        if snapshot.status in terminal_statuses:
            symbol = snapshot.symbol
            state = self.quote_state[symbol]
            if state["bid_oid"] == snapshot.client_oid:
                state["bid_oid"] = None
                state["bid_price"] = None
                state["bid_volume"] = None
            if state["ask_oid"] == snapshot.client_oid:
                state["ask_oid"] = None
                state["ask_price"] = None
                state["ask_volume"] = None

    def on_trade(self, trade: TradeData):
        # [修复] 记录成交时间
        self.last_fill_time[trade.symbol] = time.perf_counter()
