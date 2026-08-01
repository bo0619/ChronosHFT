"""Avellaneda-Stoikov quoting with explicit log-bps and time units."""

from __future__ import annotations

import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass

import numpy as np

from alpha.glft_adaptive import (
    DynamicCovarianceEstimator,
    FillMarkoutEstimator,
    HawkesFlowIntensity,
    QueueLatencyEstimator,
    estimate_flow_adverse_costs,
    optimize_quote_size,
)
from data.ref_data import ref_data_manager
from event.type import (
    EVENT_STRATEGY_UPDATE,
    AggTradeData,
    Event,
    OrderBook,
    OrderIntent,
    OrderStateSnapshot,
    Side,
    StrategyData,
    TradeData,
)
from infrastructure.config_scaling import load_root_config
from infrastructure.paper_trade import is_paper_trade
from strategy.base import StrategyTemplate
from strategy.model_readiness import (
    evaluate_symbol_readiness,
    readiness_requirements,
)
from strategy.quote_math import (
    ADAPTIVE_AS_FORMULA_VERSION,
    ASQuoteScenario,
    AS_FORMULA_VERSION,
    PORTFOLIO_AS_FORMULA_VERSION,
    UNITS_VERSION,
    adaptive_portfolio_as_quote_offsets,
    as_quote_offsets,
    depths_bps_to_prices,
    robust_adaptive_portfolio_as_quote_offsets,
)


def _negative_infinity() -> float:
    return -math.inf


@dataclass(frozen=True, slots=True)
class _ASPortfolioAssetState:
    mid_price: float
    sigma_bps_sqrt_s: float
    gamma_per_bps: float
    k_per_bps: float
    bid_k_per_bps: float
    ask_k_per_bps: float
    bid_adverse_cost_bps: float
    ask_adverse_cost_bps: float
    order_size_lots: float
    inventory_lot_notional_usdt: float
    updated_at_monotonic: float


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

        self.gamma = self._strict_positive(
            self.as_conf.get("gamma", 0.05),
            "avellaneda_stoikov.gamma",
        )
        self.k = self._strict_positive(
            self.as_conf.get("k", 1.5),
            "avellaneda_stoikov.k",
        )
        self.horizon_s = self._strict_positive(
            self.as_conf.get("horizon_s", 1.0),
            "avellaneda_stoikov.horizon_s",
        )
        self.min_sigma_bps = self._strict_positive(
            self.as_conf.get("min_sigma_bps", 0.1),
            "avellaneda_stoikov.min_sigma_bps",
        )
        self.max_tick_gap_sec = self._strict_positive(
            self.as_conf.get("max_tick_gap_sec", 2.0),
            "avellaneda_stoikov.max_tick_gap_sec",
        )
        self.vol_window = max(2, int(self.as_conf.get("vol_window", 60) or 60))
        self.interval = self._strict_nonnegative(
            self.config.get(
                "cycle_interval",
                self.as_conf.get("cycle_interval", 1.0),
            ),
            "avellaneda_stoikov.cycle_interval",
        )
        self.min_spread_ratio = self._strict_positive(
            self.as_conf.get("min_spread_ratio", 0.0002),
            "avellaneda_stoikov.min_spread_ratio",
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

        root_config = getattr(self.oms, "config", {})
        root_config = root_config if isinstance(root_config, dict) else {}
        self.live_mode = not is_paper_trade(root_config)
        raw_portfolio_config = self._config_mapping(
            self.as_conf.get("portfolio_risk", {}),
            "avellaneda_stoikov.portfolio_risk",
        )
        self.portfolio_risk_enabled = bool(
            raw_portfolio_config.get("enabled", False)
        )
        self.portfolio_state_max_age_sec = self._strict_positive(
            raw_portfolio_config.get("max_state_age_sec", 5.0),
            "avellaneda_stoikov.portfolio_risk.max_state_age_sec",
        )
        self.portfolio_require_full_universe = bool(
            raw_portfolio_config.get("require_full_universe", True)
        )
        raw_symbols = root_config.get("symbols", ())
        if not isinstance(raw_symbols, (list, tuple)):
            raw_symbols = ()
        self.portfolio_symbols = tuple(
            str(value or "").strip().upper()
            for value in raw_symbols
            if str(value or "").strip()
        )
        if len(set(self.portfolio_symbols)) != len(self.portfolio_symbols):
            raise ValueError("portfolio_risk requires unique symbols")
        self.portfolio_correlations = self._parse_portfolio_correlations(
            raw_portfolio_config.get("correlations", {})
        )
        self.portfolio_asset_states: dict[str, _ASPortfolioAssetState] = {}

        self.adaptive_config = self._config_mapping(
            self.as_conf.get("adaptive", {}),
            "avellaneda_stoikov.adaptive",
        )
        self.adaptive_enabled = bool(
            self.adaptive_config.get("enabled", False)
        )
        side_intensity = self._config_mapping(
            self.adaptive_config.get("side_intensity", {}),
            "avellaneda_stoikov.adaptive.side_intensity",
        )
        self.adaptive_base_A_per_s = self._strict_positive(
            side_intensity.get("base_A_per_s", 10.0),
            "avellaneda_stoikov.adaptive.side_intensity.base_A_per_s",
        )
        self.adaptive_bid_A_multiplier = self._strict_positive(
            side_intensity.get("bid_A_multiplier", 1.0),
            "avellaneda_stoikov.adaptive.side_intensity.bid_A_multiplier",
        )
        self.adaptive_ask_A_multiplier = self._strict_positive(
            side_intensity.get("ask_A_multiplier", 1.0),
            "avellaneda_stoikov.adaptive.side_intensity.ask_A_multiplier",
        )
        self.adaptive_bid_k_multiplier = self._strict_positive(
            side_intensity.get("bid_k_multiplier", 1.0),
            "avellaneda_stoikov.adaptive.side_intensity.bid_k_multiplier",
        )
        self.adaptive_ask_k_multiplier = self._strict_positive(
            side_intensity.get("ask_k_multiplier", 1.0),
            "avellaneda_stoikov.adaptive.side_intensity.ask_k_multiplier",
        )

        hawkes_config = self._config_mapping(
            self.adaptive_config.get("hawkes", {}),
            "avellaneda_stoikov.adaptive.hawkes",
        )
        self.adaptive_hawkes = HawkesFlowIntensity(
            decay_rate_per_s=hawkes_config.get("decay_rate_per_s", 2.0),
            self_excitation=hawkes_config.get("self_excitation", 0.12),
            cross_excitation=hawkes_config.get("cross_excitation", 0.03),
            max_multiplier=hawkes_config.get("max_multiplier", 3.0),
        )
        markout_config = self._config_mapping(
            self.adaptive_config.get("markout", {}),
            "avellaneda_stoikov.adaptive.markout",
        )
        self.adaptive_markout = FillMarkoutEstimator(
            horizons_ms=markout_config.get("horizons_ms", (100, 500, 1000)),
            min_samples=markout_config.get("min_samples", 20),
            confidence_z=markout_config.get("confidence_z", 1.645),
            max_pending=markout_config.get("max_pending", 5000),
            window_size=markout_config.get("window_size", 500),
        )
        flow_config = self._config_mapping(
            self.adaptive_config.get("flow_toxicity", {}),
            "avellaneda_stoikov.adaptive.flow_toxicity",
        )
        self.adaptive_flow_toxicity_enabled = bool(
            flow_config.get("enabled", False)
        )
        self.flow_half_life_s = self._strict_positive(
            flow_config.get("half_life_s", 0.75),
            "avellaneda_stoikov.adaptive.flow_toxicity.half_life_s",
        )
        self.flow_ewma_alpha = self._unit_interval(
            flow_config.get("ewma_alpha", 0.2),
            "avellaneda_stoikov.adaptive.flow_toxicity.ewma_alpha",
            open_zero=True,
        )
        self.flow_trade_cost_bps = self._strict_nonnegative(
            flow_config.get("trade_imbalance_cost_bps", 0.0),
            "avellaneda_stoikov.adaptive.flow_toxicity.trade_imbalance_cost_bps",
        )
        self.flow_microprice_weight = self._strict_nonnegative(
            flow_config.get("microprice_weight", 0.0),
            "avellaneda_stoikov.adaptive.flow_toxicity.microprice_weight",
        )
        self.flow_max_adverse_cost_bps = self._strict_nonnegative(
            flow_config.get("max_adverse_cost_bps", 0.0),
            "avellaneda_stoikov.adaptive.flow_toxicity.max_adverse_cost_bps",
        )
        stale_guard_config = self._config_mapping(
            self.adaptive_config.get("stale_quote_guard", {}),
            "avellaneda_stoikov.adaptive.stale_quote_guard",
        )
        self.stale_quote_guard_enabled = bool(
            stale_guard_config.get("enabled", False)
        ) and not self.live_mode
        self.stale_quote_min_depth_bps = self._strict_nonnegative(
            stale_guard_config.get("min_depth_bps", 0.0),
            "avellaneda_stoikov.adaptive.stale_quote_guard.min_depth_bps",
        )
        covariance_config = self._config_mapping(
            self.adaptive_config.get("dynamic_covariance", {}),
            "avellaneda_stoikov.adaptive.dynamic_covariance",
        )
        self.adaptive_covariance = DynamicCovarianceEstimator(
            self.portfolio_symbols,
            sample_interval_s=covariance_config.get("sample_interval_s", 1.0),
            max_state_age_s=covariance_config.get("max_state_age_s", 3.0),
            max_sync_skew_s=covariance_config.get("max_sync_skew_s", 0.25),
            ewma_alpha=covariance_config.get("ewma_alpha", 0.05),
            diagonal_shrinkage=covariance_config.get(
                "diagonal_shrinkage",
                0.15,
            ),
            min_samples=covariance_config.get("min_samples", 30),
        )
        queue_config = self._config_mapping(
            self.adaptive_config.get("queue_latency", {}),
            "avellaneda_stoikov.adaptive.queue_latency",
        )
        self.adaptive_queue = QueueLatencyEstimator(
            rate_ewma_alpha=queue_config.get("rate_ewma_alpha", 0.15),
            default_service_rate_qty_per_s=queue_config.get(
                "default_service_rate_qty_per_s",
                1.0,
            ),
            max_queue_delay_s=queue_config.get("max_queue_delay_s", 5.0),
            network_latency_ms=queue_config.get("network_latency_ms", 30.0),
            queue_risk_time_weight=queue_config.get(
                "queue_risk_time_weight",
                0.02,
            ),
            confidence_z=queue_config.get("confidence_z", 1.0),
        )
        robust_config = self._config_mapping(
            self.adaptive_config.get("robust", {}),
            "avellaneda_stoikov.adaptive.robust",
        )
        self.adaptive_k_uncertainty_ratio = self._ratio_at_least_one(
            robust_config.get("k_ratio", 1.25),
            "avellaneda_stoikov.adaptive.robust.k_ratio",
        )
        self.adaptive_volatility_uncertainty_ratio = self._ratio_at_least_one(
            robust_config.get("volatility_ratio", 1.2),
            "avellaneda_stoikov.adaptive.robust.volatility_ratio",
        )
        size_config = self._config_mapping(
            self.adaptive_config.get("size_optimization", {}),
            "avellaneda_stoikov.adaptive.size_optimization",
        )
        raw_candidates = size_config.get(
            "candidate_multipliers",
            (0.0, 0.25, 0.5, 1.0),
        )
        if not isinstance(raw_candidates, (list, tuple)):
            raise ValueError(
                "avellaneda_stoikov.adaptive.size_optimization."
                "candidate_multipliers must be an array"
            )
        self.adaptive_size_candidates = tuple(
            self._strict_nonnegative(
                value,
                "avellaneda_stoikov.adaptive.size_optimization."
                "candidate_multipliers",
            )
            for value in raw_candidates
        )
        if (
            not self.adaptive_size_candidates
            or max(self.adaptive_size_candidates) > 1.0
            or 0.0 not in self.adaptive_size_candidates
        ):
            raise ValueError(
                "adaptive size multipliers must be in [0, 1] and include zero"
            )
        self.adaptive_size_utility_horizon_s = self._strict_positive(
            size_config.get("utility_horizon_s", 1.0),
            "avellaneda_stoikov.adaptive.size_optimization.utility_horizon_s",
        )
        self.adaptive_size_penalty_bps = self._strict_nonnegative(
            size_config.get("size_penalty_bps", 0.05),
            "avellaneda_stoikov.adaptive.size_optimization.size_penalty_bps",
        )
        if self.live_mode and self.portfolio_risk_enabled:
            raise ValueError(
                "Live A-S requires portfolio_risk.enabled=false until the "
                "portfolio formula has separately approved evidence"
            )
        if self.live_mode and self.adaptive_enabled:
            raise ValueError(
                "Live A-S requires adaptive.enabled=false until the adaptive "
                "formula has separately approved evidence"
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
        self.imbalance_ewma = defaultdict(float)
        self.imbalance_updated_at = defaultdict(_negative_infinity)
        self.latest_stale_guard = defaultdict(dict)

        print(
            f"[{self.name}] A-S initialized: gamma={self.gamma}, "
            f"k={self.k}, cycle={self.interval}s, "
            f"adaptive={self.adaptive_enabled}, live={self.live_mode}"
        )

    def _load_strategy_config(self):
        full_config = load_root_config("config.json")
        return full_config.get("strategy", {}) if full_config else {}

    @staticmethod
    def _config_mapping(value, field: str) -> dict:
        if not isinstance(value, dict):
            raise ValueError(f"{field} must be an object")
        return dict(value)

    @staticmethod
    def _strict_finite(value, field: str) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be finite") from exc
        if not math.isfinite(parsed):
            raise ValueError(f"{field} must be finite")
        return parsed

    @classmethod
    def _strict_positive(cls, value, field: str) -> float:
        parsed = cls._strict_finite(value, field)
        if parsed <= 0.0:
            raise ValueError(f"{field} must be positive")
        return parsed

    @classmethod
    def _strict_nonnegative(cls, value, field: str) -> float:
        parsed = cls._strict_finite(value, field)
        if parsed < 0.0:
            raise ValueError(f"{field} must be nonnegative")
        return parsed

    @classmethod
    def _unit_interval(
        cls,
        value,
        field: str,
        *,
        open_zero: bool = False,
    ) -> float:
        parsed = cls._strict_finite(value, field)
        if parsed > 1.0 or parsed < 0.0 or (open_zero and parsed == 0.0):
            boundary = "(0, 1]" if open_zero else "[0, 1]"
            raise ValueError(f"{field} must be in {boundary}")
        return parsed

    @classmethod
    def _ratio_at_least_one(cls, value, field: str) -> float:
        parsed = cls._strict_finite(value, field)
        if parsed < 1.0:
            raise ValueError(f"{field} must be at least one")
        return parsed

    def _parse_portfolio_correlations(
        self,
        raw_correlations,
    ) -> dict[tuple[str, str], float]:
        if not isinstance(raw_correlations, dict):
            raise ValueError("portfolio_risk.correlations must be an object")
        configured_symbols = set(self.portfolio_symbols)
        correlations: dict[tuple[str, str], float] = {}
        for raw_pair, raw_value in raw_correlations.items():
            pair_parts = str(raw_pair or "").split("|")
            if len(pair_parts) != 2:
                raise ValueError(
                    "portfolio_risk correlation keys must use SYMBOL|SYMBOL"
                )
            left, right = (part.strip().upper() for part in pair_parts)
            if not left or not right or left == right:
                raise ValueError(
                    "portfolio_risk correlation keys require two symbols"
                )
            if configured_symbols and (
                left not in configured_symbols or right not in configured_symbols
            ):
                raise ValueError(
                    "portfolio_risk correlation references an unknown symbol"
                )
            correlation = self._strict_finite(
                raw_value,
                f"portfolio_risk.correlations.{raw_pair}",
            )
            if correlation < -1.0 or correlation > 1.0:
                raise ValueError(
                    "portfolio_risk correlations must be between -1 and 1"
                )
            normalized_pair = tuple(sorted((left, right)))
            if normalized_pair in correlations:
                raise ValueError(
                    "portfolio_risk correlation pair is configured twice"
                )
            correlations[normalized_pair] = correlation
        return correlations

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

    @staticmethod
    def _scale_safe_volume(
        symbol: str,
        safe_volume: float,
        multiplier: float,
        price: float,
    ) -> float:
        if safe_volume <= 0.0:
            return 0.0
        bounded_multiplier = min(1.0, max(0.0, float(multiplier)))
        if bounded_multiplier >= 1.0:
            return safe_volume
        info = ref_data_manager.get_info(symbol)
        if info is None:
            return 0.0
        scaled = ref_data_manager.round_qty(
            symbol,
            safe_volume * bounded_multiplier,
        )
        min_qty = max(0.0, float(info.min_qty or 0.0))
        min_notional = max(5.0, float(info.min_notional or 0.0))
        if (
            scaled <= 0.0
            or scaled > safe_volume + 1e-12
            or scaled < min_qty
            or scaled * price + 1e-9 < min_notional
        ):
            return 0.0
        return scaled

    def _decayed_orderflow_imbalance(
        self,
        symbol: str,
        now_monotonic: float,
    ) -> float:
        raw = max(-1.0, min(1.0, float(self.imbalance_ewma[symbol])))
        updated_at = float(self.imbalance_updated_at[symbol])
        if not math.isfinite(updated_at) or now_monotonic <= updated_at:
            return raw
        age_s = now_monotonic - updated_at
        decay = math.exp(-math.log(2.0) * age_s / self.flow_half_life_s)
        return raw * decay

    def _record_resolved_paper_markouts(self) -> None:
        resolved = self.adaptive_markout.drain_resolved()
        if not resolved or self.live_mode:
            return
        recorder = getattr(self.oms, "record_paper_markout", None)
        if not callable(recorder):
            return
        for observation in resolved:
            recorder(
                {
                    "client_oid": observation.client_oid,
                    "trade_id": observation.trade_id,
                    "symbol": observation.symbol,
                    "side": observation.side.value,
                    "fill_price": observation.fill_price,
                    "horizon_ms": observation.horizon_ms,
                    "mid_price": observation.mid_price,
                    "signed_markout_bps": observation.signed_markout_bps,
                    "fill_observed_monotonic": (
                        observation.fill_observed_monotonic
                    ),
                    "mid_observed_monotonic": (
                        observation.mid_observed_monotonic
                    ),
                    "observation_lag_ms": observation.observation_lag_ms,
                }
            )

    def _calculate_formula_quote(
        self,
        *,
        symbol: str,
        mid_price: float,
        inventory_lots: float,
        sigma_bps_sqrt_s: float,
        order_size_lots: float,
        inventory_lot_notional_usdt: float,
        now_monotonic: float,
        adaptive_context: dict | None,
    ):
        if not self.portfolio_risk_enabled and not self.adaptive_enabled:
            return (
                as_quote_offsets(
                    mid_price=mid_price,
                    inventory_lots=inventory_lots,
                    sigma_bps_sqrt_s=sigma_bps_sqrt_s,
                    gamma_per_bps=self.gamma,
                    k_per_bps=self.k,
                    horizon_s=self.horizon_s,
                ),
                {
                    "enabled": False,
                    "adaptive_enabled": False,
                    "formula_version": AS_FORMULA_VERSION,
                },
            )

        context = adaptive_context if isinstance(adaptive_context, dict) else {}
        self.portfolio_asset_states[symbol] = _ASPortfolioAssetState(
            mid_price=mid_price,
            sigma_bps_sqrt_s=sigma_bps_sqrt_s,
            gamma_per_bps=self.gamma,
            k_per_bps=self.k,
            bid_k_per_bps=float(context.get("bid_k_per_bps", self.k)),
            ask_k_per_bps=float(context.get("ask_k_per_bps", self.k)),
            bid_adverse_cost_bps=float(
                context.get("bid_adverse_cost_bps", 0.0)
            ),
            ask_adverse_cost_bps=float(
                context.get("ask_adverse_cost_bps", 0.0)
            ),
            order_size_lots=order_size_lots,
            inventory_lot_notional_usdt=inventory_lot_notional_usdt,
            updated_at_monotonic=now_monotonic,
        )
        universe = (
            self.portfolio_symbols
            if self.portfolio_risk_enabled
            else (symbol,)
        ) or tuple(sorted(self.portfolio_asset_states))
        if symbol not in universe:
            raise ValueError(
                f"portfolio A-S symbol {symbol} is outside configured universe"
            )

        active_symbols = []
        unavailable_symbols = []
        for portfolio_symbol in universe:
            state = self.portfolio_asset_states.get(portfolio_symbol)
            if state is None or (
                now_monotonic - state.updated_at_monotonic
                > self.portfolio_state_max_age_sec
            ):
                unavailable_symbols.append(portfolio_symbol)
            else:
                active_symbols.append(portfolio_symbol)
        if (
            unavailable_symbols
            and self.portfolio_risk_enabled
            and self.portfolio_require_full_universe
        ):
            raise ValueError(
                "portfolio A-S state unavailable for "
                + ",".join(unavailable_symbols)
            )
        if unavailable_symbols:
            open_unavailable = [
                unavailable_symbol
                for unavailable_symbol in unavailable_symbols
                if abs(
                    float(
                        self.oms.exposure.net_positions.get(
                            unavailable_symbol,
                            0.0,
                        )
                        or 0.0
                    )
                )
                > 1e-12
            ]
            if open_unavailable:
                raise ValueError(
                    "portfolio A-S cannot omit open inventory for "
                    + ",".join(open_unavailable)
                )
        if symbol not in active_symbols:
            raise ValueError(
                f"portfolio A-S current state is unavailable for {symbol}"
            )

        states = [
            self.portfolio_asset_states[item] for item in active_symbols
        ]
        reference_lot_notional = states[0].inventory_lot_notional_usdt
        if any(
            not math.isclose(
                state.inventory_lot_notional_usdt,
                reference_lot_notional,
                rel_tol=1e-10,
                abs_tol=1e-10,
            )
            for state in states[1:]
        ):
            raise ValueError(
                "portfolio A-S requires one common inventory lot notional"
            )
        portfolio_gamma = max(state.gamma_per_bps for state in states)
        portfolio_inventory = [
            float(
                self.oms.exposure.net_positions.get(portfolio_symbol, 0.0)
                or 0.0
            )
            * state.mid_price
            / state.inventory_lot_notional_usdt
            for portfolio_symbol, state in zip(
                active_symbols,
                states,
                strict=True,
            )
        ]
        covariance = []
        for row_symbol, row_state in zip(
            active_symbols,
            states,
            strict=True,
        ):
            row = []
            for column_symbol, column_state in zip(
                active_symbols,
                states,
                strict=True,
            ):
                correlation = (
                    1.0
                    if row_symbol == column_symbol
                    else self.portfolio_correlations.get(
                        tuple(sorted((row_symbol, column_symbol))),
                        0.0,
                    )
                )
                row.append(
                    correlation
                    * row_state.sigma_bps_sqrt_s
                    * column_state.sigma_bps_sqrt_s
                )
            covariance.append(row)

        covariance_source = "STATIC_CONFIGURED_CORRELATION"
        if self.adaptive_enabled:
            covariance, covariance_source = self.adaptive_covariance.covariance(
                active_symbols,
                covariance,
            )
            high_covariance = [
                [
                    value
                    * self.adaptive_volatility_uncertainty_ratio
                    * self.adaptive_volatility_uncertainty_ratio
                    for value in row
                ]
                for row in covariance
            ]
            scenarios = []
            k_bounds = (
                ("LOW_K", 1.0 / self.adaptive_k_uncertainty_ratio),
                ("BASE_K", 1.0),
                ("HIGH_K", self.adaptive_k_uncertainty_ratio),
            )
            volatility_bounds = (
                ("BASE_VOL", covariance),
                ("HIGH_VOL", high_covariance),
            )
            for k_name, k_multiplier in k_bounds:
                for volatility_name, scenario_covariance in volatility_bounds:
                    scenario_name = f"{k_name}_{volatility_name}"
                    if scenario_name == "BASE_K_BASE_VOL":
                        scenario_name = "BASELINE"
                    scenarios.append(
                        ASQuoteScenario(
                            name=scenario_name,
                            bid_k_per_bps=[
                                state.bid_k_per_bps * k_multiplier
                                for state in states
                            ],
                            ask_k_per_bps=[
                                state.ask_k_per_bps * k_multiplier
                                for state in states
                            ],
                            covariance_bps2_per_s=scenario_covariance,
                            bid_adverse_cost_bps=[
                                state.bid_adverse_cost_bps for state in states
                            ],
                            ask_adverse_cost_bps=[
                                state.ask_adverse_cost_bps for state in states
                            ],
                        )
                    )
            scenarios.sort(key=lambda item: item.name != "BASELINE")
            robust_solution = robust_adaptive_portfolio_as_quote_offsets(
                mid_prices=[state.mid_price for state in states],
                inventory_lots=portfolio_inventory,
                gamma_per_bps=portfolio_gamma,
                order_size_lots=[state.order_size_lots for state in states],
                horizon_s=self.horizon_s,
                scenarios=scenarios,
            )
            current_index = active_symbols.index(symbol)
            baseline_solution = robust_solution.scenario_solutions[0]
            return (
                robust_solution.quotes[current_index],
                {
                    "enabled": self.portfolio_risk_enabled,
                    "adaptive_enabled": True,
                    "formula_version": ADAPTIVE_AS_FORMULA_VERSION,
                    "symbols": list(active_symbols),
                    "finite_horizon_s": self.horizon_s,
                    "covariance_source": covariance_source,
                    "covariance": [list(row) for row in covariance],
                    "dynamic_covariance": self.adaptive_covariance.summary(),
                    "scenario_count": len(scenarios),
                    "selected_bid_scenario": (
                        robust_solution.selected_bid_scenario[current_index]
                    ),
                    "selected_ask_scenario": (
                        robust_solution.selected_ask_scenario[current_index]
                    ),
                    "common_gamma_per_bps": portfolio_gamma,
                    "common_inventory_lot_notional_usdt": (
                        reference_lot_notional
                    ),
                    "inventory_lots": dict(
                        zip(active_symbols, portfolio_inventory, strict=True)
                    ),
                    "marginal_inventory_risk_bps": dict(
                        zip(
                            active_symbols,
                            baseline_solution.marginal_inventory_risk_bps,
                            strict=True,
                        )
                    ),
                    "current_risk_curvature_bps": dict(
                        zip(
                            active_symbols,
                            baseline_solution.risk_curvature_bps[current_index],
                            strict=True,
                        )
                    ),
                    "inventory_penalty_bps": (
                        baseline_solution.inventory_penalty_bps
                    ),
                },
            )

        solution = adaptive_portfolio_as_quote_offsets(
            mid_prices=[state.mid_price for state in states],
            inventory_lots=portfolio_inventory,
            covariance_bps2_per_s=covariance,
            gamma_per_bps=portfolio_gamma,
            bid_k_per_bps=[state.k_per_bps for state in states],
            ask_k_per_bps=[state.k_per_bps for state in states],
            order_size_lots=[state.order_size_lots for state in states],
            bid_adverse_cost_bps=[0.0 for _state in states],
            ask_adverse_cost_bps=[0.0 for _state in states],
            horizon_s=self.horizon_s,
        )
        current_index = active_symbols.index(symbol)
        return (
            solution.quotes[current_index],
            {
                "enabled": True,
                "adaptive_enabled": False,
                "formula_version": PORTFOLIO_AS_FORMULA_VERSION,
                "symbols": list(active_symbols),
                "finite_horizon_s": self.horizon_s,
                "covariance_source": covariance_source,
                "covariance": [list(row) for row in covariance],
                "common_gamma_per_bps": portfolio_gamma,
                "common_inventory_lot_notional_usdt": (
                    reference_lot_notional
                ),
                "inventory_lots": dict(
                    zip(active_symbols, portfolio_inventory, strict=True)
                ),
                "marginal_inventory_risk_bps": dict(
                    zip(
                        active_symbols,
                        solution.marginal_inventory_risk_bps,
                        strict=True,
                    )
                ),
                "current_risk_curvature_bps": dict(
                    zip(
                        active_symbols,
                        solution.risk_curvature_bps[current_index],
                        strict=True,
                    )
                ),
                "inventory_penalty_bps": solution.inventory_penalty_bps,
            },
        )

    def on_orderbook(self, ob: OrderBook):
        bid_1, bid_1_volume = ob.get_best_bid()
        ask_1, ask_1_volume = ob.get_best_ask()
        if bid_1 <= 0.0 or ask_1 <= bid_1:
            return

        mid_price = (bid_1 + ask_1) / 2.0
        now = time.perf_counter()
        self._update_volatility(ob.symbol, mid_price, ob, now)
        if self.adaptive_enabled:
            self.adaptive_markout.observe_mid(
                symbol=ob.symbol,
                mid_price=mid_price,
                observed_at_monotonic=now,
            )
            self._record_resolved_paper_markouts()
            self.adaptive_covariance.observe_mid(
                symbol=ob.symbol,
                mid_price=mid_price,
                observed_at_monotonic=now,
            )
        self.latest_stale_guard[ob.symbol] = self._guard_stale_quotes(
            symbol=ob.symbol,
            mid_price=mid_price,
        )
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
        reference_order_notional = reference_order_volume * mid_price
        inventory_lot_notional = (
            self.inventory_lot_notional_usdt or reference_order_notional
        )
        if inventory_lot_notional <= 0.0:
            return
        inventory_lots = risk_position * mid_price / inventory_lot_notional
        order_size_lots = reference_order_notional / inventory_lot_notional
        sigma_bps = self.current_sigma_bps[ob.symbol]

        adaptive_context = None
        adaptive_runtime = {"enabled": False}
        bid_queue_estimate = None
        ask_queue_estimate = None
        bid_markout = None
        ask_markout = None
        if self.adaptive_enabled:
            bid_hawkes, ask_hawkes = self.adaptive_hawkes.multipliers(
                ob.symbol,
                now,
            )
            bid_markout = self.adaptive_markout.estimate(
                ob.symbol,
                Side.BUY,
            )
            ask_markout = self.adaptive_markout.estimate(
                ob.symbol,
                Side.SELL,
            )
            bid_queue_estimate = self.adaptive_queue.estimate(
                symbol=ob.symbol,
                quote_side=Side.BUY,
                queue_ahead_qty=max(0.0, float(bid_1_volume or 0.0)),
                sigma_bps_sqrt_s=sigma_bps,
            )
            ask_queue_estimate = self.adaptive_queue.estimate(
                symbol=ob.symbol,
                quote_side=Side.SELL,
                queue_ahead_qty=max(0.0, float(ask_1_volume or 0.0)),
                sigma_bps_sqrt_s=sigma_bps,
            )
            signed_imbalance = self._decayed_orderflow_imbalance(
                ob.symbol,
                now,
            )
            flow_adverse = None
            if self.adaptive_flow_toxicity_enabled:
                flow_adverse = estimate_flow_adverse_costs(
                    mid_price=mid_price,
                    best_bid=bid_1,
                    best_ask=ask_1,
                    bid_quantity=max(0.0, float(bid_1_volume or 0.0)),
                    ask_quantity=max(0.0, float(ask_1_volume or 0.0)),
                    signed_trade_imbalance=signed_imbalance,
                    trade_imbalance_cost_bps=self.flow_trade_cost_bps,
                    microprice_weight=self.flow_microprice_weight,
                    max_adverse_cost_bps=self.flow_max_adverse_cost_bps,
                )
            bid_flow_cost = (
                flow_adverse.bid_adverse_cost_bps
                if flow_adverse is not None
                else 0.0
            )
            ask_flow_cost = (
                flow_adverse.ask_adverse_cost_bps
                if flow_adverse is not None
                else 0.0
            )
            adaptive_context = {
                "bid_A_per_s": (
                    self.adaptive_base_A_per_s
                    * self.adaptive_bid_A_multiplier
                    * bid_hawkes
                ),
                "ask_A_per_s": (
                    self.adaptive_base_A_per_s
                    * self.adaptive_ask_A_multiplier
                    * ask_hawkes
                ),
                "bid_k_per_bps": self.k * self.adaptive_bid_k_multiplier,
                "ask_k_per_bps": self.k * self.adaptive_ask_k_multiplier,
                "bid_adverse_cost_bps": (
                    bid_markout.adverse_cost_bps
                    + bid_queue_estimate.latency_cost_bps
                    + bid_flow_cost
                ),
                "ask_adverse_cost_bps": (
                    ask_markout.adverse_cost_bps
                    + ask_queue_estimate.latency_cost_bps
                    + ask_flow_cost
                ),
            }
            adaptive_runtime = {
                "enabled": True,
                "intensity_source": "CONFIGURED_PAPER_PROXY",
                "hawkes": self.adaptive_hawkes.summary(ob.symbol, now),
                "markout": self.adaptive_markout.summary(ob.symbol),
                "bid_queue": {
                    "queue_ahead_qty": bid_queue_estimate.queue_ahead_qty,
                    "service_rate_qty_per_s": (
                        bid_queue_estimate.service_rate_qty_per_s
                    ),
                    "expected_delay_s": bid_queue_estimate.expected_delay_s,
                    "latency_cost_bps": bid_queue_estimate.latency_cost_bps,
                },
                "ask_queue": {
                    "queue_ahead_qty": ask_queue_estimate.queue_ahead_qty,
                    "service_rate_qty_per_s": (
                        ask_queue_estimate.service_rate_qty_per_s
                    ),
                    "expected_delay_s": ask_queue_estimate.expected_delay_s,
                    "latency_cost_bps": ask_queue_estimate.latency_cost_bps,
                },
                "flow_toxicity": {
                    "enabled": flow_adverse is not None,
                    "signed_trade_imbalance": signed_imbalance,
                    "microprice_offset_bps": (
                        flow_adverse.microprice_offset_bps
                        if flow_adverse is not None
                        else 0.0
                    ),
                    "bid_adverse_cost_bps": bid_flow_cost,
                    "ask_adverse_cost_bps": ask_flow_cost,
                },
                "parameter_bounds": {
                    "k_ratio": self.adaptive_k_uncertainty_ratio,
                    "volatility_ratio": (
                        self.adaptive_volatility_uncertainty_ratio
                    ),
                },
            }

        try:
            formula_quote, portfolio_risk = self._calculate_formula_quote(
                symbol=ob.symbol,
                mid_price=mid_price,
                inventory_lots=inventory_lots,
                sigma_bps_sqrt_s=sigma_bps,
                order_size_lots=order_size_lots,
                inventory_lot_notional_usdt=inventory_lot_notional,
                now_monotonic=now,
                adaptive_context=adaptive_context,
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
        quote_center_price = mid_price * math.exp(
            formula_quote.center_offset_bps / 10_000.0
        )
        marginal_risk_map = portfolio_risk.get(
            "marginal_inventory_risk_bps",
            {},
        )
        if isinstance(marginal_risk_map, dict) and ob.symbol in marginal_risk_map:
            inventory_center_offset_bps = -float(
                marginal_risk_map[ob.symbol]
            )
        else:
            inventory_center_offset_bps = formula_quote.center_offset_bps
        reservation_price = mid_price * math.exp(
            inventory_center_offset_bps / 10_000.0
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
        size_optimization = {"enabled": False}
        if (
            self.adaptive_enabled
            and adaptive_context is not None
            and bid_queue_estimate is not None
            and ask_queue_estimate is not None
        ):
            marginal_risk_map = portfolio_risk.get(
                "marginal_inventory_risk_bps",
                {},
            )
            curvature_row = portfolio_risk.get(
                "current_risk_curvature_bps",
                {},
            )
            marginal_risk_bps = (
                marginal_risk_map.get(ob.symbol)
                if isinstance(marginal_risk_map, dict)
                else None
            )
            self_curvature_bps = (
                curvature_row.get(ob.symbol)
                if isinstance(curvature_row, dict)
                else None
            )
            bid_size = optimize_quote_size(
                side=Side.BUY,
                candidate_multipliers=self.adaptive_size_candidates,
                base_order_size_lots=order_size_lots,
                inventory_lots=inventory_lots,
                depth_bps=formula_quote.bid_depth_bps,
                A_per_s=adaptive_context["bid_A_per_s"],
                k_per_bps=adaptive_context["bid_k_per_bps"],
                sigma_bps_sqrt_s=sigma_bps,
                gamma_per_bps=self.gamma,
                fee_bps=passive_fee_bps,
                adverse_cost_bps=adaptive_context[
                    "bid_adverse_cost_bps"
                ],
                expected_queue_delay_s=bid_queue_estimate.expected_delay_s,
                utility_horizon_s=self.adaptive_size_utility_horizon_s,
                size_penalty_bps=self.adaptive_size_penalty_bps,
                marginal_inventory_risk_bps=marginal_risk_bps,
                self_risk_curvature_bps=self_curvature_bps,
            )
            ask_size = optimize_quote_size(
                side=Side.SELL,
                candidate_multipliers=self.adaptive_size_candidates,
                base_order_size_lots=order_size_lots,
                inventory_lots=inventory_lots,
                depth_bps=formula_quote.ask_depth_bps,
                A_per_s=adaptive_context["ask_A_per_s"],
                k_per_bps=adaptive_context["ask_k_per_bps"],
                sigma_bps_sqrt_s=sigma_bps,
                gamma_per_bps=self.gamma,
                fee_bps=passive_fee_bps,
                adverse_cost_bps=adaptive_context[
                    "ask_adverse_cost_bps"
                ],
                expected_queue_delay_s=ask_queue_estimate.expected_delay_s,
                utility_horizon_s=self.adaptive_size_utility_horizon_s,
                size_penalty_bps=self.adaptive_size_penalty_bps,
                marginal_inventory_risk_bps=marginal_risk_bps,
                self_risk_curvature_bps=self_curvature_bps,
            )
            bid_order_vol = self._scale_safe_volume(
                ob.symbol,
                bid_order_vol,
                bid_size.multiplier,
                target_bid,
            )
            ask_order_vol = self._scale_safe_volume(
                ob.symbol,
                ask_order_vol,
                ask_size.multiplier,
                target_ask,
            )
            size_optimization = {
                "enabled": True,
                "candidate_multipliers": list(
                    bid_size.evaluated_multipliers
                ),
                "bid_multiplier": bid_size.multiplier,
                "ask_multiplier": ask_size.multiplier,
                "bid_expected_utility_bps_lots_per_s": (
                    bid_size.expected_utility_bps_lots_per_s
                ),
                "ask_expected_utility_bps_lots_per_s": (
                    ask_size.expected_utility_bps_lots_per_s
                ),
                "bid_effective_fill_intensity_per_s": (
                    bid_size.effective_fill_intensity_per_s
                ),
                "ask_effective_fill_intensity_per_s": (
                    ask_size.effective_fill_intensity_per_s
                ),
            }
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
                    tag="as_quote",
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
                    tag="as_quote",
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
            "A_per_s": (
                0.5
                * (
                    adaptive_context["bid_A_per_s"]
                    + adaptive_context["ask_A_per_s"]
                )
                if adaptive_context is not None
                else 0.0
            ),
            "intensity_source": (
                "CONFIGURED_PAPER_PROXY"
                if adaptive_context is not None
                else "NOT_USED_BY_AS_PRICE"
            ),
            "bid_k_per_bps": (
                adaptive_context["bid_k_per_bps"]
                if adaptive_context is not None
                else self.k
            ),
            "ask_k_per_bps": (
                adaptive_context["ask_k_per_bps"]
                if adaptive_context is not None
                else self.k
            ),
            "horizon_s": self.horizon_s,
            "sigma_bps": sigma_bps,
            "sigma_sq": sigma_bps * sigma_bps,
            "sigma_units": "bps/sqrt(second)",
            "inventory_lots": inventory_lots,
            "order_size_lots": order_size_lots,
            "inventory_lot_notional_usdt": inventory_lot_notional,
            "inventory_risk_adjustment": inventory_risk_adjustment,
            "inventory_center_offset_bps": inventory_center_offset_bps,
            "reservation_price": reservation_price,
            "quote_center_offset_bps": formula_quote.center_offset_bps,
            "quote_center_price": quote_center_price,
            "formula_half_spread_bps": formula_quote.half_spread_bps,
            "formula_bid_depth_bps": formula_quote.bid_depth_bps,
            "formula_ask_depth_bps": formula_quote.ask_depth_bps,
            "configured_min_spread_bps": configured_min_spread_bps,
            "passive_fee_bps": passive_fee_bps,
            "effective_min_spread_bps": effective_min_spread_bps,
            "units_version": UNITS_VERSION,
            "formula_version": portfolio_risk["formula_version"],
            "portfolio_risk": portfolio_risk,
            "adaptive": adaptive_runtime,
            "size_optimization": size_optimization,
            "stale_quote_guard": dict(
                self.latest_stale_guard[ob.symbol]
            ),
            "market_data_timing": {
                "exchange_timestamp": ob.exchange_timestamp,
                "received_timestamp": ob.received_timestamp,
                "corrected_received_timestamp": (
                    ob.corrected_received_timestamp
                ),
                "received_monotonic": ob.received_monotonic,
                "dispatch_timestamp": ob.dispatch_timestamp,
                "dispatch_monotonic": ob.dispatch_monotonic,
                "callback_monotonic": now,
                "clock_offset_ms": ob.clock_offset_ms,
                "best_bid_qty": bid_1_volume,
                "best_ask_qty": ask_1_volume,
            },
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

    def _guard_stale_quotes(
        self,
        *,
        symbol: str,
        mid_price: float,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "enabled": self.stale_quote_guard_enabled,
            "min_depth_bps": self.stale_quote_min_depth_bps,
            "bid_depth_bps": None,
            "ask_depth_bps": None,
            "bid_at_risk": False,
            "ask_at_risk": False,
            "bid_cancel_requested": False,
            "ask_cancel_requested": False,
        }
        if not self.stale_quote_guard_enabled or mid_price <= 0.0:
            return result

        side_depths: dict[Side, list[tuple[str, float]]] = {
            Side.BUY: [],
            Side.SELL: [],
        }
        for client_oid, intent in tuple(self.active_orders.items()):
            if intent.symbol != symbol or intent.side not in side_depths:
                continue
            price = float(intent.price)
            if not math.isfinite(price) or price <= 0.0:
                continue
            if intent.side == Side.BUY:
                depth = math.log(mid_price / price) * 10_000.0
            else:
                depth = math.log(price / mid_price) * 10_000.0
            side_depths[intent.side].append((client_oid, depth))

        for side, key in ((Side.BUY, "bid"), (Side.SELL, "ask")):
            quotes = side_depths[side]
            if not quotes:
                continue
            result[f"{key}_depth_bps"] = min(depth for _oid, depth in quotes)
            at_risk = [
                client_oid
                for client_oid, depth in quotes
                if depth < self.stale_quote_min_depth_bps
            ]
            result[f"{key}_at_risk"] = bool(at_risk)
            for client_oid in at_risk:
                accepted = self.cancel_order(client_oid)
                if accepted or client_oid in self.orders_cancelling:
                    result[f"{key}_cancel_requested"] = True
        return result

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
        formula_version = AS_FORMULA_VERSION
        if self.adaptive_enabled:
            formula_version = ADAPTIVE_AS_FORMULA_VERSION
        elif self.portfolio_risk_enabled:
            formula_version = PORTFOLIO_AS_FORMULA_VERSION
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
            "formula_version": formula_version,
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

    def on_market_trade(self, trade: AggTradeData):
        if not self.adaptive_enabled:
            return
        try:
            event_monotonic = float(trade.received_monotonic or 0.0)
        except (TypeError, ValueError):
            event_monotonic = 0.0
        if not math.isfinite(event_monotonic) or event_monotonic <= 0.0:
            event_monotonic = time.perf_counter()
        sign = -1.0 if trade.maker_is_buyer else 1.0
        previous = self._decayed_orderflow_imbalance(
            trade.symbol,
            event_monotonic,
        )
        alpha = self.flow_ewma_alpha
        self.imbalance_ewma[trade.symbol] = (
            (1.0 - alpha) * previous + alpha * sign
        )
        self.imbalance_updated_at[trade.symbol] = event_monotonic
        aggressor_side = Side.SELL if trade.maker_is_buyer else Side.BUY
        self.adaptive_hawkes.record_trade(
            symbol=trade.symbol,
            aggressor_side=aggressor_side,
            observed_at_monotonic=event_monotonic,
        )
        self.adaptive_queue.record_trade(
            symbol=trade.symbol,
            aggressor_side=aggressor_side,
            quantity=trade.quantity,
            observed_at_monotonic=event_monotonic,
        )

    def on_trade(self, trade: TradeData):
        if not self.adaptive_enabled:
            return
        self.adaptive_markout.record_fill(
            symbol=trade.symbol,
            side=trade.side,
            fill_price=trade.price,
            observed_at_monotonic=time.perf_counter(),
            client_oid=trade.order_id,
            trade_id=trade.trade_id,
        )

    def on_order(self, snapshot: OrderStateSnapshot):
        super().on_order(snapshot)
