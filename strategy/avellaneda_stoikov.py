"""Avellaneda-Stoikov quoting with explicit log-bps and time units."""

from __future__ import annotations

import math
import time
from collections import defaultdict, deque

import numpy as np

from data.ref_data import ref_data_manager
from event.type import (
    EVENT_STRATEGY_UPDATE,
    Event,
    OrderBook,
    OrderIntent,
    OrderStateSnapshot,
    Side,
    StrategyData,
    TradeData,
)
from infrastructure.config_scaling import load_root_config
from strategy.base import StrategyTemplate
from strategy.model_readiness import (
    evaluate_symbol_readiness,
    readiness_requirements,
)
from strategy.quote_math import (
    AS_FORMULA_VERSION,
    UNITS_VERSION,
    as_quote_offsets,
    depths_bps_to_prices,
)


def _negative_infinity() -> float:
    return -math.inf


class AvellanedaStoikovStrategy(StrategyTemplate):
    """Finite-horizon A-S strategy using fixed-notional inventory lots."""

    def __init__(self, engine, oms, strategy_config=None):
        super().__init__(engine, oms, "AvellanedaStoikov")

        self.config = (
            dict(strategy_config)
            if strategy_config is not None
            else self._load_strategy_config()
        )
        raw_as_config = self.config.get("as_parameters", {})
        self.as_conf = dict(raw_as_config) if isinstance(raw_as_config, dict) else {}

        self.use_rpi = bool(self.config.get("use_rpi", False)) and bool(
            self.as_conf.get(
                "use_rpi",
                self.config.get("use_rpi_for_avellaneda_stoikov", True),
            )
        )
        self.rpi_fallback_to_gtx = bool(
            self.as_conf.get(
                "rpi_fallback_to_gtx",
                self.config.get("rpi_fallback_to_gtx", True),
            )
        )

        self.gamma = float(self.as_conf.get("gamma", 0.05))
        self.k = float(self.as_conf.get("k", 1.5))
        self.horizon_s = self._positive_finite(
            self.as_conf.get("horizon_s", 1.0),
            1.0,
        )
        self.min_sigma_bps = self._positive_finite(
            self.as_conf.get("min_sigma_bps", 0.1),
            0.1,
        )
        self.max_tick_gap_sec = self._positive_finite(
            self.as_conf.get("max_tick_gap_sec", 2.0),
            2.0,
        )
        self.vol_window = max(2, int(self.as_conf.get("vol_window", 60) or 60))
        self.interval = self._nonnegative_finite(
            self.config.get(
                "cycle_interval",
                self.as_conf.get("cycle_interval", 1.0),
            ),
            1.0,
        )
        self.min_spread_ratio = self._positive_finite(
            self.as_conf.get("min_spread_ratio", 0.0002),
            0.0002,
        )
        self.readiness_requirements = readiness_requirements(
            self.config,
            "avellaneda_stoikov",
        )
        required_window = max(
            self.readiness_requirements.min_model_samples,
            self.readiness_requirements.min_volatility_samples,
        )
        if (
            self.readiness_requirements.enabled
            and self.vol_window < required_window
        ):
            raise ValueError(
                "avellaneda_stoikov.vol_window must retain at least "
                f"{required_window} normalized-return samples"
            )

        self.configure_quote_sizing(self.config)
        self.inventory_lot_notional_usdt = self._positive_finite(
            self.as_conf.get(
                "inventory_lot_notional_usdt",
                self.target_order_notional,
            )
        )

        self.normalized_returns = defaultdict(
            lambda: deque(maxlen=self.vol_window)
        )
        self.last_mid = defaultdict(float)
        self.last_tick_time = defaultdict(float)
        self.last_tick_source = defaultdict(str)
        self.last_tick_monotonic = defaultdict(float)
        self.last_recalc_time = defaultdict(_negative_infinity)
        self.current_sigma_bps = defaultdict(float)

        print(
            f"[{self.name}] A-S initialized: gamma={self.gamma}, "
            f"k={self.k}, cycle={self.interval}s"
        )

    def _load_strategy_config(self):
        full_config = load_root_config("config.json")
        return full_config.get("strategy", {}) if full_config else {}

    @staticmethod
    def _valid_clock_value(value) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and parsed > 0.0 else None

    def _clock_sample(
        self,
        ob: OrderBook,
        now_monotonic: float,
    ) -> tuple[str, float]:
        received_monotonic = self._valid_clock_value(ob.received_monotonic)
        if received_monotonic is not None:
            return "received_monotonic", received_monotonic
        exchange_timestamp = self._valid_clock_value(ob.exchange_timestamp)
        if exchange_timestamp is not None:
            return "exchange_timestamp", exchange_timestamp
        return "monotonic", now_monotonic

    def _set_volatility_reference(
        self,
        symbol: str,
        *,
        mid: float,
        clock_source: str,
        tick_time: float,
        now_monotonic: float,
    ) -> None:
        self.last_mid[symbol] = mid
        self.last_tick_source[symbol] = clock_source
        self.last_tick_time[symbol] = tick_time
        self.last_tick_monotonic[symbol] = now_monotonic

    def _update_volatility(
        self,
        symbol: str,
        mid: float,
        ob: OrderBook,
        now_monotonic: float,
    ) -> None:
        clock_source, tick_time = self._clock_sample(ob, now_monotonic)
        previous_mid = self.last_mid[symbol]
        if previous_mid > 0.0:
            if clock_source == self.last_tick_source[symbol]:
                dt = tick_time - self.last_tick_time[symbol]
            else:
                dt = now_monotonic - self.last_tick_monotonic[symbol]

            if not math.isfinite(dt) or dt <= 0.0:
                return
            if dt > self.max_tick_gap_sec:
                self._set_volatility_reference(
                    symbol,
                    mid=mid,
                    clock_source=clock_source,
                    tick_time=tick_time,
                    now_monotonic=now_monotonic,
                )
                return
            if dt > 1e-4:
                log_return_bps = math.log(mid / previous_mid) * 10_000.0
                normalized = log_return_bps / math.sqrt(dt)
                if math.isfinite(normalized):
                    self.normalized_returns[symbol].append(normalized)

        self._set_volatility_reference(
            symbol,
            mid=mid,
            clock_source=clock_source,
            tick_time=tick_time,
            now_monotonic=now_monotonic,
        )

    def _calculate_sigma_bps(self, symbol: str) -> float:
        samples = self.normalized_returns[symbol]
        if len(samples) < 2:
            return self.min_sigma_bps
        sigma = float(np.std(np.asarray(samples, dtype=float)))
        if not math.isfinite(sigma):
            return 0.0
        return max(self.min_sigma_bps, sigma)

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

    def on_orderbook(self, ob: OrderBook):
        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 <= 0.0 or ask_1 <= bid_1:
            return

        mid_price = (bid_1 + ask_1) / 2.0
        now = time.perf_counter()
        self._update_volatility(ob.symbol, mid_price, ob, now)
        if now - self.last_recalc_time[ob.symbol] < self.interval:
            return
        self.last_recalc_time[ob.symbol] = now

        self.current_sigma_bps[ob.symbol] = self._calculate_sigma_bps(ob.symbol)
        volatility_samples = len(self.normalized_returns[ob.symbol])
        readiness = evaluate_symbol_readiness(
            "avellaneda_stoikov",
            self.readiness_requirements,
            volatility_samples=volatility_samples,
            model_samples=volatility_samples,
        )
        if not readiness.ready:
            self._publish_warming_up(
                ob.symbol,
                mid_price,
                bid_1,
                ask_1,
                readiness,
            )
            return

        active_symbol_orders = [
            oid
            for oid, intent in self.active_orders.items()
            if intent.symbol == ob.symbol
        ]
        if active_symbol_orders:
            for oid in active_symbol_orders:
                self.cancel_order(oid)
            return

        strategy_positions = getattr(
            self.oms.exposure,
            "strategy_net_positions",
            {},
        )
        strategy_position = strategy_positions.get((self.name, ob.symbol))
        if strategy_position is None:
            strategy_position = self.oms.exposure.net_positions.get(
                ob.symbol,
                self.pos,
            )
        risk_position = self.oms.exposure.net_positions.get(
            ob.symbol,
            strategy_position,
        )

        reference_order_volume = self._calculate_safe_vol(
            ob.symbol,
            mid_price,
            current_position=risk_position,
            reference_price=mid_price,
        )
        if reference_order_volume <= 0.0:
            return
        inventory_lot_notional = (
            self.inventory_lot_notional_usdt
            or reference_order_volume * mid_price
        )
        if inventory_lot_notional <= 0.0:
            return
        inventory_lots = risk_position * mid_price / inventory_lot_notional
        sigma_bps = self.current_sigma_bps[ob.symbol]

        try:
            formula_quote = as_quote_offsets(
                mid_price=mid_price,
                inventory_lots=inventory_lots,
                sigma_bps_sqrt_s=sigma_bps,
                gamma_per_bps=self.gamma,
                k_per_bps=self.k,
                horizon_s=self.horizon_s,
            )
        except ValueError as exc:
            self._publish_warming_up(
                ob.symbol,
                mid_price,
                bid_1,
                ask_1,
                readiness,
                formula_error=str(exc),
            )
            return

        passive_tif = self.resolve_passive_time_in_force(
            ob.symbol,
            use_rpi=self.use_rpi,
            fallback_to_gtx=self.rpi_fallback_to_gtx,
            route="avellaneda_stoikov_quote",
        )
        configured_min_spread_bps = self.min_spread_ratio * 10_000.0
        passive_fee_bps = self.passive_round_trip_fee_bps(
            ob.symbol,
            passive_tif,
        )
        effective_min_spread_bps = max(
            configured_min_spread_bps,
            passive_fee_bps,
        )
        effective_half_spread_bps = max(
            formula_quote.half_spread_bps,
            effective_min_spread_bps / 2.0,
        )
        target_bid, target_ask = depths_bps_to_prices(
            mid_price,
            effective_half_spread_bps - formula_quote.center_offset_bps,
            effective_half_spread_bps + formula_quote.center_offset_bps,
        )
        reservation_price = mid_price * math.exp(
            formula_quote.center_offset_bps / 10_000.0
        )

        info = ref_data_manager.get_info(ob.symbol)
        if info is None:
            return
        tick = float(info.tick_size or 0.0)
        if tick <= 0.0:
            return
        target_bid = ref_data_manager.round_price(
            ob.symbol,
            target_bid,
            direction="down",
        )
        target_ask = ref_data_manager.round_price(
            ob.symbol,
            target_ask,
            direction="up",
        )
        if target_bid >= ask_1:
            target_bid = ask_1 - tick
        if target_ask <= bid_1:
            target_ask = bid_1 + tick
        if target_bid <= 0.0 or target_bid >= target_ask:
            return

        bid_order_vol = self._calculate_safe_vol(
            ob.symbol,
            target_bid,
            side=Side.BUY,
            current_position=risk_position,
            reference_price=mid_price,
        )
        ask_order_vol = self._calculate_safe_vol(
            ob.symbol,
            target_ask,
            side=Side.SELL,
            current_position=risk_position,
            reference_price=mid_price,
        )
        if bid_order_vol <= 0.0 and ask_order_vol <= 0.0:
            return

        bid_oid = None
        if bid_order_vol > 0.0:
            bid_oid = self.send_intent(
                OrderIntent(
                    strategy_id=self.name,
                    symbol=ob.symbol,
                    side=Side.BUY,
                    price=target_bid,
                    volume=bid_order_vol,
                    time_in_force=passive_tif,
                    is_post_only=True,
                )
            )

        ask_oid = None
        if ask_order_vol > 0.0:
            ask_oid = self.send_intent(
                OrderIntent(
                    strategy_id=self.name,
                    symbol=ob.symbol,
                    side=Side.SELL,
                    price=target_ask,
                    volume=ask_order_vol,
                    time_in_force=passive_tif,
                    is_post_only=True,
                )
            )

        quoted_spread = max(0.0, target_ask - target_bid)
        quoted_spread_bps = quoted_spread / mid_price * 10_000.0
        inventory_risk_adjustment = mid_price - reservation_price
        params = {
            "schema": "market_making.v1",
            "strategy": self.name,
            "state": "QUOTING",
            "mode": passive_tif,
            "time_in_force": passive_tif,
            "use_rpi": self.use_rpi,
            "rpi_supported": ref_data_manager.supports_rpi(ob.symbol),
            "mid_price": mid_price,
            "best_bid": bid_1,
            "best_ask": ask_1,
            "market_spread_bps": (ask_1 - bid_1) / mid_price * 10_000.0,
            "fair_value": reservation_price,
            "alpha_bps": 0.0,
            "position_qty": float(strategy_position),
            "position_notional": float(strategy_position) * mid_price,
            "risk_position_qty": float(risk_position),
            "target_bid": target_bid,
            "target_ask": target_ask,
            "quote_spread_bps": quoted_spread_bps,
            "quote_qty": max(bid_order_vol, ask_order_vol),
            "bid_quote_qty": bid_order_vol,
            "ask_quote_qty": ask_order_vol,
            "target_order_notional": self.target_order_notional,
            "max_position_notional": self.max_pos_usdt,
            "bid_order_id": bid_oid or "",
            "ask_order_id": ask_oid or "",
            "gamma_per_bps": self.gamma,
            "k_per_bps": self.k,
            "horizon_s": self.horizon_s,
            "sigma_bps": sigma_bps,
            "sigma_sq": sigma_bps * sigma_bps,
            "sigma_units": "bps/sqrt(second)",
            "inventory_lots": inventory_lots,
            "inventory_lot_notional_usdt": inventory_lot_notional,
            "inventory_risk_adjustment": inventory_risk_adjustment,
            "inventory_center_offset_bps": formula_quote.center_offset_bps,
            "reservation_price": reservation_price,
            "formula_half_spread_bps": formula_quote.half_spread_bps,
            "formula_bid_depth_bps": formula_quote.bid_depth_bps,
            "formula_ask_depth_bps": formula_quote.ask_depth_bps,
            "configured_min_spread_bps": configured_min_spread_bps,
            "passive_fee_bps": passive_fee_bps,
            "effective_min_spread_bps": effective_min_spread_bps,
            "units_version": UNITS_VERSION,
            "formula_version": AS_FORMULA_VERSION,
            "readiness": readiness.as_params(),
            "final_spread": quoted_spread,
            "final_spread_bps": quoted_spread_bps,
            "State": "QUOTING",
            "Mode": passive_tif,
            "Spread": f"{quoted_spread_bps:.1f}",
            "Sigma": f"{sigma_bps:.1f}",
            "Size": f"{max(bid_order_vol, ask_order_vol):.8g}",
        }
        self.engine.put(
            Event(
                EVENT_STRATEGY_UPDATE,
                StrategyData(
                    symbol=ob.symbol,
                    fair_value=reservation_price,
                    alpha_bps=0.0,
                    params=params,
                ),
            )
        )

    def _publish_warming_up(
        self,
        symbol,
        mid_price,
        best_bid,
        best_ask,
        readiness,
        *,
        formula_error="",
    ):
        for client_oid, intent in tuple(self.active_orders.items()):
            if intent.symbol == symbol:
                self.cancel_order(client_oid)
        readiness_params = readiness.as_params()
        state = "WARMING_UP"
        if formula_error:
            state = "FORMULA_INVALID"
            readiness_params["ready"] = False
            readiness_params["state"] = state
            readiness_params["formula_error"] = formula_error
        sigma_bps = float(self.current_sigma_bps[symbol])
        params = {
            "schema": "market_making.v1",
            "strategy": self.name,
            "state": state,
            "mode": "OBSERVE_ONLY",
            "time_in_force": "",
            "use_rpi": self.use_rpi,
            "rpi_supported": ref_data_manager.supports_rpi(symbol),
            "mid_price": mid_price,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "fair_value": mid_price,
            "alpha_bps": 0.0,
            "sigma_sq": sigma_bps * sigma_bps,
            "sigma_bps": sigma_bps,
            "sigma_units": "bps/sqrt(second)",
            "units_version": UNITS_VERSION,
            "formula_version": AS_FORMULA_VERSION,
            "readiness": readiness_params,
        }
        self.engine.put(
            Event(
                EVENT_STRATEGY_UPDATE,
                StrategyData(
                    symbol=symbol,
                    fair_value=mid_price,
                    alpha_bps=0.0,
                    params=params,
                ),
            )
        )

    def on_trade(self, trade: TradeData):
        del trade

    def on_order(self, snapshot: OrderStateSnapshot):
        super().on_order(snapshot)
