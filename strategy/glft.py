"""GLFT market making with validated units and RPI-only intensity evidence."""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR

from alpha.engine import FeatureEngine
from alpha.factors import GLFTCalibrator
from alpha.gate import AlphaGate
from alpha.glft_adaptive import (
    DynamicCovarianceEstimator,
    FillMarkoutEstimator,
    HawkesFlowIntensity,
    QueueLatencyEstimator,
    estimate_flow_adverse_costs,
    optimize_quote_size,
)
from alpha.rpi_intensity import (
    RPIExposureBin,
    RPIIntensityAccumulator,
    RPIIntensityRequirements,
    RPIOrderExposure,
    estimate_rpi_intensity,
)
from alpha.signal import MultiHorizonPredictor
from data.ref_data import ref_data_manager
from event.type import (
    EVENT_STRATEGY_UPDATE,
    AggTradeData,
    Event,
    OrderBook,
    OrderIntent,
    OrderStateSnapshot,
    OrderStatus,
    Side,
    StrategyData,
    TIF_RPI,
    TradeData,
)
from infrastructure.config_scaling import load_root_config
from infrastructure.paper_trade import is_paper_trade
from infrastructure.time_service import time_service
from strategy.base import StrategyTemplate
from strategy.model_readiness import (
    evaluate_symbol_readiness,
    readiness_requirements,
)
from strategy.quote_math import (
    ADAPTIVE_GLFT_FORMULA_VERSION,
    GLFT_FORMULA_VERSION,
    PORTFOLIO_GLFT_FORMULA_VERSION,
    GLFTQuoteScenario,
    UNITS_VERSION,
    depths_bps_to_prices,
    glft_quote_offsets,
    portfolio_glft_quote_offsets,
    robust_adaptive_portfolio_glft_quote_offsets,
)


def _negative_infinity() -> float:
    return -math.inf


@dataclass(slots=True)
class _AcknowledgedRPIQuote:
    symbol: str
    client_oid: str
    exchange_oid: str
    side: Side
    price: float
    quantity: float
    acknowledged_at_time: float
    acknowledged_at_monotonic: float
    exposure: RPIOrderExposure | None
    censor_reason: str = ""
    seen_trade_ids: set[str] = field(default_factory=set)

    def mark_censored(self, reason: str) -> None:
        if not self.censor_reason:
            self.censor_reason = str(reason or "unspecified_censor").strip()
        if self.exposure is not None:
            self.exposure.mark_censored(self.censor_reason)


@dataclass(frozen=True, slots=True)
class _PortfolioAssetState:
    mid_price: float
    fair_mid: float
    sigma_bps_sqrt_s: float
    gamma_per_bps: float
    A_per_s: float
    k_per_bps: float
    bid_A_per_s: float
    ask_A_per_s: float
    bid_k_per_bps: float
    ask_k_per_bps: float
    bid_adverse_cost_bps: float
    ask_adverse_cost_bps: float
    order_size_lots: float
    inventory_lot_notional_usdt: float
    target_position_notional_usdt: float
    updated_at_monotonic: float


class GLFTStrategy(StrategyTemplate):
    """GLFT Model A strategy with one fixed-notional inventory unit."""

    def __init__(self, engine, oms, strategy_config=None):
        super().__init__(engine, oms, "GLFT_MultiScale")

        if strategy_config is None:
            full_config = self._load_full_config()
            self.strat_conf = full_config.get("strategy", {})
        else:
            self.strat_conf = dict(strategy_config)

        raw_glft_config = self.strat_conf.get("glft", {})
        self.glft_conf = (
            dict(raw_glft_config)
            if isinstance(raw_glft_config, dict)
            else {}
        )
        self.use_rpi = bool(self.strat_conf.get("use_rpi", False)) and bool(
            self.glft_conf.get(
                "use_rpi",
                self.strat_conf.get("use_rpi_for_glft", True),
            )
        )
        self.rpi_fallback_to_gtx = bool(
            self.glft_conf.get(
                "rpi_fallback_to_gtx",
                self.strat_conf.get("rpi_fallback_to_gtx", True),
            )
        )

        root_config = getattr(self.oms, "config", {})
        root_config = root_config if isinstance(root_config, dict) else {}
        self.live_mode = not is_paper_trade(root_config)
        live_launch = root_config.get("live_launch", {})
        live_launch = live_launch if isinstance(live_launch, dict) else {}
        self.rpi_calibration_mode = (
            self.live_mode
            and str(live_launch.get("stage", "") or "").strip().lower()
            == "rpi_calibration_canary"
        )
        validated_permit = root_config.get(
            "_validated_rpi_calibration_permit",
            {},
        )
        self.rpi_calibration_permit = (
            dict(validated_permit.get("permit", {}))
            if isinstance(validated_permit, dict)
            and isinstance(validated_permit.get("permit"), dict)
            else {}
        )
        self.rpi_calibration_permit_sha256 = (
            self._sha256_identity(validated_permit.get("permit_sha256"))
            if isinstance(validated_permit, dict)
            else ""
        )
        calibration_policy = self.rpi_calibration_permit.get("policy", {})
        self.rpi_calibration_policy = (
            dict(calibration_policy)
            if isinstance(calibration_policy, dict)
            else {}
        )
        self.rpi_calibration_permit_id = str(
            self.rpi_calibration_permit.get("permit_id", "") or ""
        ).strip()
        self.rpi_calibration_symbol = str(
            self.rpi_calibration_permit.get("symbol", "") or ""
        ).strip().upper()
        self.rpi_calibration_fixed_depths_bps = tuple(
            float(value)
            for value in self.rpi_calibration_policy.get(
                "fixed_depths_bps",
                (),
            )
        )
        self.rpi_calibration_order_ttl_sec = self._positive_finite(
            self.rpi_calibration_policy.get("order_ttl_sec", 0.0)
        )
        self.rpi_calibration_min_order_interval_sec = self._positive_finite(
            self.rpi_calibration_policy.get("min_order_interval_sec", 0.0)
        )
        self.rpi_calibration_min_order_notional = self._positive_finite(
            self.rpi_calibration_policy.get(
                "min_order_notional_usdt",
                0.0,
            )
        )
        self.rpi_calibration_max_order_notional = self._positive_finite(
            self.rpi_calibration_policy.get(
                "max_order_notional_usdt",
                0.0,
            )
        )
        self.rpi_calibration_expires_at = self._parse_utc_epoch(
            self.rpi_calibration_permit.get("expires_at_utc")
        )
        self.rpi_calibration_expiry_handled = False
        if self.rpi_calibration_mode and (
            not self.rpi_calibration_permit_id
            or not self.rpi_calibration_permit_sha256
            or not self.rpi_calibration_symbol
            or len(self.rpi_calibration_fixed_depths_bps) < 3
            or self.rpi_calibration_order_ttl_sec <= 0.0
            or self.rpi_calibration_min_order_interval_sec <= 0.0
            or self.rpi_calibration_min_order_notional <= 0.0
            or self.rpi_calibration_max_order_notional
            < self.rpi_calibration_min_order_notional
            or self.rpi_calibration_expires_at <= 0.0
        ):
            raise ValueError(
                "RPI calibration canary requires a complete validated permit"
            )

        self.gamma_base = self._positive_finite(
            self.strat_conf.get(
                "gamma",
                self.glft_conf.get("gamma", 0.1),
            ),
            0.1,
        )
        self.configure_quote_sizing(self.strat_conf)
        configured_cycle_interval = self.strat_conf.get(
            "cycle_interval",
            self.glft_conf.get("cycle_interval", 1.0),
        )
        if not self.live_mode:
            configured_cycle_interval = self.strat_conf.get(
                "paper_cycle_interval",
                self.glft_conf.get(
                    "paper_cycle_interval",
                    configured_cycle_interval,
                ),
            )
        self.cycle_interval = self._nonnegative_finite(
            configured_cycle_interval,
            1.0,
        )
        raw_execution = self.strat_conf.get(
            "execution",
            self.glft_conf.get("execution", {}),
        )
        model_execution = (
            raw_execution if isinstance(raw_execution, dict) else {}
        )
        configured_min_spread_bps = model_execution.get("min_spread_bps", 5.0)
        if not self.live_mode:
            configured_min_spread_bps = model_execution.get(
                "paper_min_spread_bps",
                configured_min_spread_bps,
            )
        self.min_spread_bps = self._positive_finite(
            configured_min_spread_bps,
            5.0,
        )
        self.readiness_requirements = readiness_requirements(
            self.strat_conf,
            "glft",
        )

        raw_calibrator_config = self.strat_conf.get(
            "calibrator",
            self.glft_conf.get("calibrator", {}),
        )
        self.calibrator_config = (
            dict(raw_calibrator_config)
            if isinstance(raw_calibrator_config, dict)
            else {}
        )
        self.calibrator_config.setdefault(
            "min_samples",
            self.readiness_requirements.min_volatility_samples,
        )
        self.calibrator_window = max(
            self.readiness_requirements.min_volatility_samples,
            int(self.calibrator_config.get("window", 1000) or 1000),
        )

        raw_alpha_config = self.strat_conf.get(
            "alpha",
            self.glft_conf.get("alpha", {}),
        )
        self.alpha_config = (
            dict(raw_alpha_config)
            if isinstance(raw_alpha_config, dict)
            else {}
        )
        self.alpha_enabled = bool(self.alpha_config.get("enabled", False))
        self.target_inventory_notional_usdt = self._finite_float(
            self.strat_conf.get(
                "target_inventory_notional_usdt",
                self.glft_conf.get("target_inventory_notional_usdt", 0.0),
            ),
            "target_inventory_notional_usdt",
        )
        self.alpha_weights = {
            "short_fv_weight": self._finite_float(
                self.alpha_config.get("short_fv_weight", 1.0),
                "alpha.short_fv_weight",
            ),
            "long_pos_weight": self._finite_float(
                self.alpha_config.get("long_pos_weight", 500.0),
                "alpha.long_pos_weight",
            ),
        }
        raw_portfolio_config = self.glft_conf.get("portfolio_risk", {})
        self.portfolio_risk_config = (
            dict(raw_portfolio_config)
            if isinstance(raw_portfolio_config, dict)
            else {}
        )
        self.portfolio_risk_enabled = bool(
            self.portfolio_risk_config.get("enabled", False)
        )
        self.portfolio_state_max_age_sec = self._positive_finite(
            self.portfolio_risk_config.get("max_state_age_sec", 5.0),
            5.0,
        )
        self.portfolio_require_full_universe = bool(
            self.portfolio_risk_config.get("require_full_universe", True)
        )
        raw_portfolio_symbols = root_config.get("symbols", ())
        if not isinstance(raw_portfolio_symbols, (list, tuple)):
            raw_portfolio_symbols = ()
        normalized_portfolio_symbols = tuple(
            str(value or "").strip().upper()
            for value in raw_portfolio_symbols
            if str(value or "").strip()
        )
        if len(set(normalized_portfolio_symbols)) != len(
            normalized_portfolio_symbols
        ):
            raise ValueError("portfolio_risk requires unique symbols")
        self.portfolio_symbols = normalized_portfolio_symbols
        self.portfolio_correlations = self._parse_portfolio_correlations(
            self.portfolio_risk_config.get("correlations", {})
        )
        self.portfolio_asset_states: dict[str, _PortfolioAssetState] = {}

        self.adaptive_config = self._config_mapping(
            self.glft_conf.get("adaptive", {}),
            "glft.adaptive",
        )
        self.adaptive_enabled = bool(self.adaptive_config.get("enabled", False))
        self.adaptive_horizon_s = self._positive_finite(
            self.adaptive_config.get("finite_horizon_s", 5.0),
            5.0,
        )
        side_intensity_config = self._config_mapping(
            self.adaptive_config.get("side_intensity", {}),
            "glft.adaptive.side_intensity",
        )
        self.adaptive_bid_A_multiplier = self._positive_finite(
            side_intensity_config.get("bid_A_multiplier", 1.0),
            1.0,
        )
        self.adaptive_ask_A_multiplier = self._positive_finite(
            side_intensity_config.get("ask_A_multiplier", 1.0),
            1.0,
        )
        self.adaptive_bid_k_multiplier = self._positive_finite(
            side_intensity_config.get("bid_k_multiplier", 1.0),
            1.0,
        )
        self.adaptive_ask_k_multiplier = self._positive_finite(
            side_intensity_config.get("ask_k_multiplier", 1.0),
            1.0,
        )

        hawkes_config = self._config_mapping(
            self.adaptive_config.get("hawkes", {}),
            "glft.adaptive.hawkes",
        )
        self.adaptive_hawkes = HawkesFlowIntensity(
            decay_rate_per_s=hawkes_config.get("decay_rate_per_s", 2.0),
            self_excitation=hawkes_config.get("self_excitation", 0.12),
            cross_excitation=hawkes_config.get("cross_excitation", 0.03),
            max_multiplier=hawkes_config.get("max_multiplier", 3.0),
        )
        markout_config = self._config_mapping(
            self.adaptive_config.get("markout", {}),
            "glft.adaptive.markout",
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
            "glft.adaptive.flow_toxicity",
        )
        self.adaptive_flow_toxicity_enabled = bool(
            flow_config.get("enabled", False)
        )
        self.flow_half_life_s = self._positive_finite(
            flow_config.get("half_life_s", 0.75),
            0.75,
        )
        self.flow_trade_cost_bps = self._nonnegative_finite(
            flow_config.get("trade_imbalance_cost_bps", 0.0),
            0.0,
        )
        self.flow_microprice_weight = self._nonnegative_finite(
            flow_config.get("microprice_weight", 0.0),
            0.0,
        )
        self.flow_max_adverse_cost_bps = self._nonnegative_finite(
            flow_config.get("max_adverse_cost_bps", 0.0),
            0.0,
        )
        stale_guard_config = self._config_mapping(
            self.adaptive_config.get("stale_quote_guard", {}),
            "glft.adaptive.stale_quote_guard",
        )
        self.stale_quote_guard_enabled = bool(
            stale_guard_config.get("enabled", False)
        ) and not self.live_mode
        self.stale_quote_min_depth_bps = self._nonnegative_finite(
            stale_guard_config.get("min_depth_bps", 0.0),
            0.0,
        )
        covariance_config = self._config_mapping(
            self.adaptive_config.get("dynamic_covariance", {}),
            "glft.adaptive.dynamic_covariance",
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
            "glft.adaptive.queue_latency",
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
            "glft.adaptive.robust",
        )
        self.adaptive_intensity_uncertainty_ratio = self._ratio_at_least_one(
            robust_config.get("intensity_ratio", 1.5),
            "glft.adaptive.robust.intensity_ratio",
        )
        self.adaptive_k_uncertainty_ratio = self._ratio_at_least_one(
            robust_config.get("k_ratio", 1.25),
            "glft.adaptive.robust.k_ratio",
        )
        self.adaptive_volatility_uncertainty_ratio = self._ratio_at_least_one(
            robust_config.get("volatility_ratio", 1.2),
            "glft.adaptive.robust.volatility_ratio",
        )
        size_config = self._config_mapping(
            self.adaptive_config.get("size_optimization", {}),
            "glft.adaptive.size_optimization",
        )
        raw_size_candidates = size_config.get(
            "candidate_multipliers",
            (0.0, 0.25, 0.5, 1.0),
        )
        if not isinstance(raw_size_candidates, (list, tuple)):
            raise ValueError(
                "glft.adaptive.size_optimization.candidate_multipliers "
                "must be an array"
            )
        self.adaptive_size_candidates = tuple(
            self._finite_float(
                value,
                "glft.adaptive.size_optimization.candidate_multipliers",
            )
            for value in raw_size_candidates
        )
        if (
            not self.adaptive_size_candidates
            or min(self.adaptive_size_candidates) < 0.0
            or max(self.adaptive_size_candidates) > 1.0
            or 0.0 not in self.adaptive_size_candidates
        ):
            raise ValueError(
                "adaptive size multipliers must be in [0, 1] and include zero"
            )
        self.adaptive_size_penalty_bps = self._nonnegative_finite(
            size_config.get("size_penalty_bps", 0.05),
            0.05,
        )
        self.adaptive_size_utility_horizon_s = self._positive_finite(
            size_config.get("utility_horizon_s", 1.0),
            1.0,
        )
        self.formula_version = (
            ADAPTIVE_GLFT_FORMULA_VERSION
            if self.adaptive_enabled
            else (
                PORTFOLIO_GLFT_FORMULA_VERSION
                if self.portfolio_risk_enabled
                else GLFT_FORMULA_VERSION
            )
        )
        if self.live_mode:
            if not callable(
                getattr(self.oms, "record_strategy_evidence", None)
            ):
                raise ValueError(
                    "Live GLFT requires durable OMS strategy evidence"
                )
            if self.alpha_enabled:
                raise ValueError("Live GLFT requires alpha.enabled=false")
            if self.target_inventory_notional_usdt != 0.0:
                raise ValueError(
                    "Live GLFT requires target_inventory_notional_usdt=0"
                )
            if self.portfolio_risk_enabled:
                raise ValueError(
                    "Live GLFT requires portfolio_risk.enabled=false until "
                    "the portfolio formula has its own approved evidence"
                )
            if self.adaptive_enabled:
                raise ValueError(
                    "Live GLFT requires adaptive.enabled=false until the "
                    "adaptive formula has separately approved evidence"
                )
            if not self.use_rpi or self.rpi_fallback_to_gtx:
                raise ValueError(
                    "Live GLFT requires RPI-only routing with no GTX fallback"
                )

        self.inventory_lot_notional_usdt = self._positive_finite(
            self.strat_conf.get(
                "inventory_lot_notional_usdt",
                self.glft_conf.get(
                    "inventory_lot_notional_usdt",
                    self.target_order_notional,
                ),
            )
        )

        raw_intensity_config = self.strat_conf.get(
            "rpi_intensity",
            self.glft_conf.get("rpi_intensity", {}),
        )
        self.rpi_intensity_config = (
            dict(raw_intensity_config)
            if isinstance(raw_intensity_config, dict)
            else {}
        )
        self.rpi_depth_bin_width_bps = self._positive_finite(
            self.rpi_intensity_config.get("depth_bin_width_bps", 0.25),
            0.25,
        )
        self.rpi_max_initial_book_age_seconds = self._positive_finite(
            self.rpi_intensity_config.get(
                "max_initial_book_age_seconds",
                1.5,
            ),
            1.5,
        )
        self.rpi_max_runtime_parameter_ratio = self._positive_finite(
            self.rpi_intensity_config.get(
                "max_runtime_parameter_ratio",
                3.0,
            ),
            3.0,
        )
        if self.rpi_max_runtime_parameter_ratio <= 1.0:
            raise ValueError(
                "rpi_intensity.max_runtime_parameter_ratio must exceed 1"
            )
        default_intensity_requirements = RPIIntensityRequirements()
        self.rpi_intensity_requirements = RPIIntensityRequirements(
            min_sample_count=self.rpi_intensity_config.get(
                "min_sample_count",
                max(
                    default_intensity_requirements.min_sample_count,
                    self.readiness_requirements.min_model_samples,
                ),
            ),
            min_depth_level_count=self.rpi_intensity_config.get(
                "min_depth_level_count",
                default_intensity_requirements.min_depth_level_count,
            ),
            min_total_exposure_seconds=self.rpi_intensity_config.get(
                "min_total_exposure_seconds",
                default_intensity_requirements.min_total_exposure_seconds,
            ),
            min_fill_count=self.rpi_intensity_config.get(
                "min_fill_count",
                default_intensity_requirements.min_fill_count,
            ),
            min_depth_span_bps=self.rpi_intensity_config.get(
                "min_depth_span_bps",
                default_intensity_requirements.min_depth_span_bps,
            ),
            min_k_per_bps=self.rpi_intensity_config.get(
                "min_k_per_bps",
                default_intensity_requirements.min_k_per_bps,
            ),
            max_k_per_bps=self.rpi_intensity_config.get(
                "max_k_per_bps",
                default_intensity_requirements.max_k_per_bps,
            ),
        )
        self.validated_prior_bins = self._load_validated_prior_bins()
        self.rpi_intensity_accumulators = defaultdict(RPIIntensityAccumulator)
        self.rpi_acknowledged_quotes: dict[str, _AcknowledgedRPIQuote] = {}
        raw_sampling_identity = self.strat_conf.get(
            "_rpi_sampling_identity",
            {},
        )
        self.rpi_sampling_identity = (
            dict(raw_sampling_identity)
            if isinstance(raw_sampling_identity, dict)
            else {}
        )
        self.rpi_deployment_id = str(
            self.rpi_sampling_identity.get("deployment_id", "") or ""
        ).strip()
        self.rpi_strategy_policy_sha256 = self._sha256_identity(
            self.rpi_sampling_identity.get("strategy_policy_sha256")
        )
        self.rpi_implementation_sha256 = self._sha256_identity(
            self.rpi_sampling_identity.get("implementation_sha256")
        )
        if self.live_mode and (
            not self.rpi_deployment_id
            or not self.rpi_strategy_policy_sha256
            or not self.rpi_implementation_sha256
        ):
            raise ValueError(
                "Live GLFT requires root-bound RPI sampling identity"
            )

        self.of_lambda = 0.2
        self.imbalance_ewma = defaultdict(float)
        self.imbalance_updated_at = defaultdict(_negative_infinity)
        self.last_fill_time = defaultdict(_negative_infinity)
        self.latest_mid = defaultdict(float)
        self.latest_rpi_mid = defaultdict(float)
        self.latest_rpi_book_monotonic = defaultdict(_negative_infinity)

        self.quote_state = defaultdict(
            lambda: {
                "bid_oid": None,
                "ask_oid": None,
                "bid_price": None,
                "ask_price": None,
                "bid_volume": None,
                "ask_volume": None,
                "last_update": float("-inf"),
            }
        )
        self.cooldown_ms = 200

        self.feature_engine = FeatureEngine()
        self.calibrators = {}
        self.models = {}
        self.gates = {}
        self.last_run_times = defaultdict(_negative_infinity)
        self.last_calibration_status_at = defaultdict(_negative_infinity)
        self.last_calibration_status_signature = {}
        self.latest_stale_guard = defaultdict(dict)
        self.latest_market_timing = defaultdict(dict)

        print(
            f"[{self.name}] GLFT initialized: gamma={self.gamma_base}, "
            f"alpha_enabled={self.alpha_enabled}, live={self.live_mode}"
        )

    @staticmethod
    def _finite_float(value, field: str) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be finite") from exc
        if not math.isfinite(parsed):
            raise ValueError(f"{field} must be finite")
        return parsed

    @staticmethod
    def _config_mapping(value, field: str) -> dict:
        if not isinstance(value, dict):
            raise ValueError(f"{field} must be an object")
        return dict(value)

    @classmethod
    def _ratio_at_least_one(cls, value, field: str) -> float:
        parsed = cls._finite_float(value, field)
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
            left, right = (
                part.strip().upper() for part in pair_parts
            )
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
            correlation = self._finite_float(
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
    def _sha256_identity(value) -> str:
        normalized = str(value or "").strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef"
            for character in normalized
        ):
            return ""
        return normalized

    @staticmethod
    def _parse_utc_epoch(value) -> float:
        normalized = str(value or "").strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return 0.0
        if parsed.tzinfo is None:
            return 0.0
        return parsed.astimezone(timezone.utc).timestamp()

    def _load_full_config(self):
        return load_root_config("config.json")

    def _load_validated_prior_bins(
        self,
    ) -> dict[str, tuple[RPIExposureBin, ...]]:
        validated = self.strat_conf.get("_validated_calibration", {})
        if self.rpi_calibration_mode:
            if validated:
                raise ValueError(
                    "RPI calibration canary cannot consume an approved "
                    "trading calibration"
                )
            return {}
        if not isinstance(validated, dict):
            if self.live_mode:
                raise ValueError("Live GLFT calibration payload is invalid")
            return {}
        raw_symbols = validated.get("symbols", {})
        if not isinstance(raw_symbols, dict):
            if self.live_mode:
                raise ValueError(
                    "Live GLFT calibration symbols payload is invalid"
                )
            return {}

        result = {}
        for raw_symbol, raw_payload in raw_symbols.items():
            symbol = str(raw_symbol or "").strip().upper()
            if not symbol or not isinstance(raw_payload, dict):
                if self.live_mode:
                    raise ValueError(
                        "Live GLFT calibration contains an invalid symbol entry"
                    )
                continue
            raw_bins = raw_payload.get("rpi_exposure_bins", ())
            if not isinstance(raw_bins, (list, tuple)):
                if self.live_mode:
                    raise ValueError(
                        f"Live GLFT calibration bins are invalid for {symbol}"
                    )
                continue
            bins = []
            for raw_bin in raw_bins:
                if not isinstance(raw_bin, dict):
                    raise ValueError(
                        f"Live GLFT calibration bin is invalid for {symbol}"
                    )
                bins.append(
                    RPIExposureBin(
                        depth_bps=raw_bin.get("depth_bps"),
                        exposure_seconds=raw_bin.get("exposure_seconds"),
                        fill_count=raw_bin.get("fill_count"),
                        sample_count=raw_bin.get("sample_count", 1),
                    )
                )
            result[symbol] = tuple(bins)
        return result

    def _get_components(self, symbol):
        if symbol not in self.calibrators:
            self.calibrators[symbol] = GLFTCalibrator(
                window=self.calibrator_window,
                config=self.calibrator_config,
            )
            self.models[symbol] = MultiHorizonPredictor(num_features=9)
            self.gates[symbol] = AlphaGate(
                max_bps=10.0,
                decay_factor=0.9,
                inventory_dampening=0.05,
            )
        return (
            self.calibrators[symbol],
            self.models[symbol],
            self.gates[symbol],
        )

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
        """Scale a pre-validated volume down without expanding its risk bound."""

        if safe_volume <= 0.0:
            return 0.0
        bounded_multiplier = min(1.0, max(0.0, float(multiplier)))
        if bounded_multiplier >= 1.0:
            return safe_volume
        info = ref_data_manager.get_info(symbol)
        if info is None:
            return 0.0
        scaled = ref_data_manager.round_qty(symbol, safe_volume * bounded_multiplier)
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

    def _approved_rpi_intensity(self, symbol):
        return estimate_rpi_intensity(
            self.validated_prior_bins.get(symbol, ()),
            requirements=self.rpi_intensity_requirements,
        )

    def _runtime_rpi_intensity(self, symbol):
        return self.rpi_intensity_accumulators[symbol].estimate(
            self.rpi_intensity_requirements
        )

    @staticmethod
    def _intensity_summary(estimate) -> dict:
        payload = estimate.as_dict()
        payload.pop("bins", None)
        return payload

    def _runtime_intensity_error(
        self,
        approved_intensity,
        runtime_intensity,
    ) -> str:
        if runtime_intensity.state == "WARMING_UP":
            return ""
        if not runtime_intensity.ready:
            reasons = ",".join(runtime_intensity.reasons)
            return (
                f"runtime_rpi_intensity:{runtime_intensity.state}"
                f"[{reasons}]"
            )

        approved_A = float(approved_intensity.A_per_s or 0.0)
        approved_k = float(approved_intensity.k_per_bps or 0.0)
        runtime_A = float(runtime_intensity.A_per_s or 0.0)
        runtime_k = float(runtime_intensity.k_per_bps or 0.0)
        if min(approved_A, approved_k, runtime_A, runtime_k) <= 0.0:
            return "runtime_rpi_intensity:non_positive_parameter"
        max_ratio = self.rpi_max_runtime_parameter_ratio
        A_ratio = max(runtime_A / approved_A, approved_A / runtime_A)
        k_ratio = max(runtime_k / approved_k, approved_k / runtime_k)
        if A_ratio > max_ratio or k_ratio > max_ratio:
            return (
                "runtime_rpi_intensity:approved_drift"
                f"[A_ratio={A_ratio:.6g},k_ratio={k_ratio:.6g},"
                f"limit={max_ratio:.6g}]"
            )
        return ""

    @staticmethod
    def _orderbook_monotonic(ob: OrderBook) -> float | None:
        for field_name in ("received_monotonic", "dispatch_monotonic"):
            try:
                value = float(getattr(ob, field_name, 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value > 0.0:
                return value
        return None

    @staticmethod
    def _market_timing_snapshot(
        ob: OrderBook,
        callback_monotonic: float,
    ) -> dict:
        def optional_finite(field_name: str) -> float | None:
            try:
                value = float(getattr(ob, field_name, 0.0) or 0.0)
            except (TypeError, ValueError):
                return None
            return value if math.isfinite(value) and value > 0.0 else None

        exchange_time = optional_finite("exchange_timestamp")
        received_time = optional_finite("received_timestamp")
        corrected_received_time = optional_finite(
            "corrected_received_timestamp"
        )
        received_monotonic = optional_finite("received_monotonic")
        dispatch_time = optional_finite("dispatch_timestamp")
        dispatch_monotonic = optional_finite("dispatch_monotonic")
        try:
            clock_offset_ms = float(ob.clock_offset_ms)
        except (TypeError, ValueError):
            clock_offset_ms = None
        if clock_offset_ms is not None and not math.isfinite(clock_offset_ms):
            clock_offset_ms = None

        transport_latency_ms = None
        if exchange_time is not None and corrected_received_time is not None:
            transport_latency_ms = (
                corrected_received_time - exchange_time
            ) * 1000.0
        gateway_processing_latency_ms = None
        if (
            received_monotonic is not None
            and dispatch_monotonic is not None
            and dispatch_monotonic >= received_monotonic
        ):
            gateway_processing_latency_ms = (
                dispatch_monotonic - received_monotonic
            ) * 1000.0
        strategy_queue_latency_ms = None
        if (
            dispatch_monotonic is not None
            and callback_monotonic >= dispatch_monotonic
        ):
            strategy_queue_latency_ms = (
                callback_monotonic - dispatch_monotonic
            ) * 1000.0
        callback_age_ms = None
        if (
            received_monotonic is not None
            and callback_monotonic >= received_monotonic
        ):
            callback_age_ms = (
                callback_monotonic - received_monotonic
            ) * 1000.0

        return {
            "exchange_timestamp": exchange_time,
            "received_timestamp": received_time,
            "corrected_received_timestamp": corrected_received_time,
            "dispatch_timestamp": dispatch_time,
            "received_monotonic": received_monotonic,
            "dispatch_monotonic": dispatch_monotonic,
            "callback_monotonic": callback_monotonic,
            "clock_offset_ms": clock_offset_ms,
            "transport_latency_ms": transport_latency_ms,
            "gateway_processing_latency_ms": gateway_processing_latency_ms,
            "strategy_queue_latency_ms": strategy_queue_latency_ms,
            "callback_age_ms": callback_age_ms,
            "best_bid_qty": float(ob.get_best_bid()[1] or 0.0),
            "best_ask_qty": float(ob.get_best_ask()[1] or 0.0),
        }

    def _published_market_timing(self, symbol: str) -> dict:
        if self.live_mode:
            return {}
        timing = dict(self.latest_market_timing[symbol])
        callback_monotonic = timing.get("callback_monotonic")
        if callback_monotonic is not None:
            timing["strategy_compute_latency_ms"] = max(
                0.0,
                (
                    time.perf_counter_ns() / 1_000_000_000.0
                    - float(callback_monotonic)
                )
                * 1000.0,
            )
        return timing

    def _observe_rpi_orderbook(
        self,
        *,
        symbol: str,
        mid: float,
        book_monotonic: float | None,
        callback_monotonic: float,
    ) -> None:
        active_quotes = tuple(
            quote
            for quote in self.rpi_acknowledged_quotes.values()
            if quote.symbol == symbol
        )
        if (
            book_monotonic is None
            or not math.isfinite(book_monotonic)
            or book_monotonic > callback_monotonic + 1e-6
        ):
            for quote in active_quotes:
                quote.mark_censored("unreliable_orderbook_monotonic")
            return

        previous_book_time = self.latest_rpi_book_monotonic[symbol]
        if (
            math.isfinite(previous_book_time)
            and book_monotonic < previous_book_time
        ):
            for quote in active_quotes:
                quote.mark_censored("non_monotonic_orderbook_stream")
            return

        for quote in active_quotes:
            if quote.exposure is None:
                continue
            quote.exposure.observe_depth(
                observed_at_monotonic=book_monotonic,
                depth_bps=self._rpi_depth_bps(
                    quote.side,
                    quote.price,
                    mid,
                ),
            )
        self.latest_rpi_mid[symbol] = mid
        self.latest_rpi_book_monotonic[symbol] = book_monotonic

    def _calculate_formula_quote(
        self,
        *,
        symbol: str,
        mid_price: float,
        fair_mid: float,
        inventory_lots: float,
        sigma_bps_sqrt_s: float,
        gamma_per_bps: float,
        A_per_s: float,
        k_per_bps: float,
        order_size_lots: float,
        inventory_lot_notional_usdt: float,
        target_position_notional_usdt: float,
        now_monotonic: float,
        adaptive_context: dict | None = None,
    ):
        if not self.portfolio_risk_enabled and not self.adaptive_enabled:
            return (
                glft_quote_offsets(
                    mid_price=fair_mid,
                    inventory_lots=inventory_lots,
                    sigma_bps_sqrt_s=sigma_bps_sqrt_s,
                    gamma_per_bps=gamma_per_bps,
                    A_per_s=A_per_s,
                    k_per_bps=k_per_bps,
                    order_size_lots=order_size_lots,
                ),
                {
                    "enabled": False,
                    "formula_version": GLFT_FORMULA_VERSION,
                },
            )

        adaptive_context = (
            adaptive_context if isinstance(adaptive_context, dict) else {}
        )

        self.portfolio_asset_states[symbol] = _PortfolioAssetState(
            mid_price=mid_price,
            fair_mid=fair_mid,
            sigma_bps_sqrt_s=sigma_bps_sqrt_s,
            gamma_per_bps=gamma_per_bps,
            A_per_s=A_per_s,
            k_per_bps=k_per_bps,
            bid_A_per_s=float(adaptive_context.get("bid_A_per_s", A_per_s)),
            ask_A_per_s=float(adaptive_context.get("ask_A_per_s", A_per_s)),
            bid_k_per_bps=float(adaptive_context.get("bid_k_per_bps", k_per_bps)),
            ask_k_per_bps=float(adaptive_context.get("ask_k_per_bps", k_per_bps)),
            bid_adverse_cost_bps=float(
                adaptive_context.get("bid_adverse_cost_bps", 0.0)
            ),
            ask_adverse_cost_bps=float(
                adaptive_context.get("ask_adverse_cost_bps", 0.0)
            ),
            order_size_lots=order_size_lots,
            inventory_lot_notional_usdt=inventory_lot_notional_usdt,
            target_position_notional_usdt=target_position_notional_usdt,
            updated_at_monotonic=now_monotonic,
        )
        universe = (
            self.portfolio_symbols
            if self.portfolio_risk_enabled
            else (symbol,)
        ) or tuple(sorted(self.portfolio_asset_states))
        if symbol not in universe:
            raise ValueError(
                f"portfolio GLFT symbol {symbol} is outside configured universe"
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
                continue
            active_symbols.append(portfolio_symbol)

        if (
            unavailable_symbols
            and self.portfolio_risk_enabled
            and self.portfolio_require_full_universe
        ):
            raise ValueError(
                "portfolio GLFT state unavailable for "
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
                    "portfolio GLFT cannot omit open inventory for "
                    + ",".join(open_unavailable)
                )
        if symbol not in active_symbols:
            raise ValueError(f"portfolio GLFT current state is unavailable for {symbol}")

        states = [
            self.portfolio_asset_states[portfolio_symbol]
            for portfolio_symbol in active_symbols
        ]
        lot_notionals = [
            state.inventory_lot_notional_usdt for state in states
        ]
        reference_lot_notional = lot_notionals[0]
        if any(
            not math.isclose(
                lot_notional,
                reference_lot_notional,
                rel_tol=1e-10,
                abs_tol=1e-10,
            )
            for lot_notional in lot_notionals[1:]
        ):
            raise ValueError(
                "portfolio GLFT requires one common inventory lot notional"
            )
        portfolio_gamma = max(state.gamma_per_bps for state in states)
        portfolio_inventory = []
        for portfolio_symbol, state in zip(
            active_symbols,
            states,
            strict=True,
        ):
            position_qty = float(
                self.oms.exposure.net_positions.get(portfolio_symbol, 0.0)
                or 0.0
            )
            effective_position_notional = (
                position_qty * state.mid_price
                - state.target_position_notional_usdt
            )
            portfolio_inventory.append(
                effective_position_notional
                / state.inventory_lot_notional_usdt
            )

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
                if row_symbol == column_symbol:
                    correlation = 1.0
                else:
                    correlation = self.portfolio_correlations.get(
                        tuple(sorted((row_symbol, column_symbol))),
                        0.0,
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

            bid_A = [state.bid_A_per_s for state in states]
            ask_A = [state.ask_A_per_s for state in states]
            bid_k = [state.bid_k_per_bps for state in states]
            ask_k = [state.ask_k_per_bps for state in states]
            bid_adverse = [state.bid_adverse_cost_bps for state in states]
            ask_adverse = [state.ask_adverse_cost_bps for state in states]
            intensity_ratio = self.adaptive_intensity_uncertainty_ratio
            k_ratio = self.adaptive_k_uncertainty_ratio
            volatility_ratio = self.adaptive_volatility_uncertainty_ratio
            high_covariance = [
                [value * volatility_ratio * volatility_ratio for value in row]
                for row in covariance
            ]
            scenarios = []
            intensity_bounds = (
                ("BASE_A", 1.0),
                ("LOW_A", 1.0 / intensity_ratio),
            )
            k_bounds = (
                ("LOW_K", 1.0 / k_ratio),
                ("BASE_K", 1.0),
                ("HIGH_K", k_ratio),
            )
            volatility_bounds = (
                ("BASE_VOL", covariance),
                ("HIGH_VOL", high_covariance),
            )
            for intensity_name, intensity_multiplier in intensity_bounds:
                for k_name, k_multiplier in k_bounds:
                    for volatility_name, scenario_covariance in volatility_bounds:
                        scenario_name = "_".join(
                            (intensity_name, k_name, volatility_name)
                        )
                        if scenario_name == "BASE_A_BASE_K_BASE_VOL":
                            scenario_name = "BASELINE"
                        scenarios.append(
                            GLFTQuoteScenario(
                                name=scenario_name,
                                bid_A_per_s=[
                                    value * intensity_multiplier for value in bid_A
                                ],
                                ask_A_per_s=[
                                    value * intensity_multiplier for value in ask_A
                                ],
                                bid_k_per_bps=[
                                    value * k_multiplier for value in bid_k
                                ],
                                ask_k_per_bps=[
                                    value * k_multiplier for value in ask_k
                                ],
                                covariance_bps2_per_s=scenario_covariance,
                                bid_adverse_cost_bps=bid_adverse,
                                ask_adverse_cost_bps=ask_adverse,
                            )
                        )
            scenarios.sort(key=lambda item: item.name != "BASELINE")
            robust_solution = robust_adaptive_portfolio_glft_quote_offsets(
                mid_prices=[state.fair_mid for state in states],
                inventory_lots=portfolio_inventory,
                gamma_per_bps=portfolio_gamma,
                order_size_lots=[state.order_size_lots for state in states],
                horizon_s=self.adaptive_horizon_s,
                scenarios=scenarios,
            )
            current_index = active_symbols.index(symbol)
            baseline_solution = robust_solution.scenario_solutions[0]
            return (
                robust_solution.quotes[current_index],
                {
                    "enabled": self.portfolio_risk_enabled,
                    "adaptive_enabled": True,
                    "formula_version": ADAPTIVE_GLFT_FORMULA_VERSION,
                    "symbols": list(active_symbols),
                    "finite_horizon_s": self.adaptive_horizon_s,
                    "covariance_source": covariance_source,
                    "covariance": [list(row) for row in covariance],
                    "dynamic_covariance": self.adaptive_covariance.summary(),
                    "scenario_count": len(scenarios),
                    "selected_bid_scenario": robust_solution.selected_bid_scenario[
                        current_index
                    ],
                    "selected_ask_scenario": robust_solution.selected_ask_scenario[
                        current_index
                    ],
                    "common_gamma_per_bps": portfolio_gamma,
                    "common_inventory_lot_notional_usdt": reference_lot_notional,
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
                    "inventory_penalty_bps": baseline_solution.inventory_penalty_bps,
                },
            )

        solution = portfolio_glft_quote_offsets(
            mid_prices=[state.fair_mid for state in states],
            inventory_lots=portfolio_inventory,
            covariance_bps2_per_s=covariance,
            gamma_per_bps=portfolio_gamma,
            A_per_s=[state.A_per_s for state in states],
            k_per_bps=[state.k_per_bps for state in states],
            order_size_lots=[state.order_size_lots for state in states],
        )
        current_index = active_symbols.index(symbol)
        return (
            solution.quotes[current_index],
            {
                "enabled": True,
                "formula_version": PORTFOLIO_GLFT_FORMULA_VERSION,
                "symbols": list(active_symbols),
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
        symbol = ob.symbol
        bid_1, bid_1_volume = ob.get_best_bid()
        ask_1, ask_1_volume = ob.get_best_ask()
        if (
            not math.isfinite(bid_1)
            or not math.isfinite(ask_1)
            or bid_1 <= 0.0
            or ask_1 <= bid_1
        ):
            return

        now = time.perf_counter()
        if not self.live_mode:
            self.latest_market_timing[symbol] = (
                self._market_timing_snapshot(
                    ob,
                    now,
                )
            )
        mid = (bid_1 + ask_1) / 2.0
        self._observe_rpi_orderbook(
            symbol=symbol,
            mid=mid,
            book_monotonic=self._orderbook_monotonic(ob),
            callback_monotonic=now,
        )
        self.latest_mid[symbol] = mid
        if self.adaptive_enabled:
            self.adaptive_markout.observe_mid(
                symbol=symbol,
                mid_price=mid,
                observed_at_monotonic=now,
            )
            self._record_resolved_paper_markouts()
            self.adaptive_covariance.observe_mid(
                symbol=symbol,
                mid_price=mid,
                observed_at_monotonic=now,
            )
        self.latest_stale_guard[symbol] = self._guard_stale_quotes(
            symbol=symbol,
            mid_price=mid,
        )

        if self.rpi_calibration_mode:
            self._run_rpi_calibration_cycle(
                symbol=symbol,
                mid=mid,
                best_bid=bid_1,
                best_ask=ask_1,
                now_monotonic=now,
            )
            return

        calibrator, model, gate = self._get_components(symbol)
        calibrator.on_orderbook(ob)
        self.feature_engine.on_orderbook(ob)

        if now - self.last_run_times[symbol] < self.cycle_interval:
            return
        self.last_run_times[symbol] = now

        features = self.feature_engine.get_features(symbol)
        alphas = model.update_and_predict(features, mid, now)
        predictor_samples = int(max(0, getattr(model, "sample_count", 0)))
        approved_intensity = self._approved_rpi_intensity(symbol)
        runtime_intensity = self._runtime_rpi_intensity(symbol)
        runtime_intensity_error = self._runtime_intensity_error(
            approved_intensity,
            runtime_intensity,
        )
        intensity_samples = approved_intensity.sample_count
        readiness_model_samples = (
            intensity_samples if self.live_mode else predictor_samples
        )
        readiness = evaluate_symbol_readiness(
            "glft",
            self.readiness_requirements,
            volatility_samples=getattr(
                calibrator,
                "volatility_sample_count",
                0,
            ),
            model_samples=readiness_model_samples,
        )
        if not readiness.ready or (
            self.live_mode
            and (
                not approved_intensity.ready
                or bool(runtime_intensity_error)
            )
        ):
            self._publish_warming_up(
                symbol,
                mid,
                bid_1,
                ask_1,
                readiness,
                predictor_samples=predictor_samples,
                approved_intensity=approved_intensity,
                runtime_intensity=runtime_intensity,
                runtime_intensity_error=runtime_intensity_error,
            )
            self.feature_engine.reset_interval(symbol)
            return

        current_pos = self.oms.exposure.net_positions.get(symbol, 0.0)
        reference_order_volume = self._calculate_safe_vol(
            symbol,
            mid,
            current_position=current_pos,
            reference_price=mid,
        )
        if reference_order_volume <= 0.0:
            return
        inventory_lot_notional = (
            reference_order_volume * mid
            if self.fixed_order_quantity > 0.0
            else self.inventory_lot_notional_usdt
            or reference_order_volume * mid
        )
        if inventory_lot_notional <= 0.0:
            return
        order_size_lots = (
            reference_order_volume * mid / inventory_lot_notional
        )

        short_signal = 0.0
        target_pos_usdt = self.target_inventory_notional_usdt
        current_pos_usdt = current_pos * mid
        preliminary_inventory_lots = (
            current_pos_usdt - target_pos_usdt
        ) / inventory_lot_notional
        if self.alpha_enabled:
            short_signal = gate.process(
                alphas["short"],
                preliminary_inventory_lots,
            )
            target_pos_usdt += (
                alphas["long"] * self.alpha_weights["long_pos_weight"]
            )
            target_position_limit = (
                self.max_pos_usdt if self.max_pos_usdt > 0.0 else 2_000.0
            )
            target_pos_usdt = max(
                -target_position_limit,
                min(target_position_limit, target_pos_usdt),
            )

        alpha_offset_bps = (
            short_signal * self.alpha_weights["short_fv_weight"]
            if self.alpha_enabled
            else 0.0
        )
        try:
            fair_mid = mid * math.exp(alpha_offset_bps / 10_000.0)
        except OverflowError:
            self._publish_formula_invalid(
                symbol,
                mid,
                bid_1,
                ask_1,
                readiness,
                predictor_samples,
                approved_intensity,
                runtime_intensity,
                "alpha fair value overflow",
            )
            return

        effective_pos_usdt = current_pos_usdt - target_pos_usdt
        inventory_lots = effective_pos_usdt / inventory_lot_notional

        account = self.oms.account
        gamma = self.gamma_base
        margin_usage = 0.0
        if account.equity > 0.0:
            margin_usage = account.used_margin / account.equity
            gamma *= 1.0 + max(0.0, (margin_usage - 0.5) * 4.0)

        signed_orderflow_imbalance = self._decayed_orderflow_imbalance(
            symbol,
            now,
        )
        orderflow_imbalance = abs(signed_orderflow_imbalance)
        gamma *= 1.0 + 3.0 * orderflow_imbalance
        recent_fill_defense = now - self.last_fill_time[symbol] < 2.0
        if recent_fill_defense:
            gamma *= 1.5

        sigma = max(0.1, float(calibrator.sigma_bps))
        if self.live_mode:
            A = approved_intensity.A_per_s
            k = approved_intensity.k_per_bps
            intensity_source = "APPROVED_RPI_ARTIFACT_FROZEN"
        else:
            A = float(calibrator.A)
            k = float(calibrator.k)
            intensity_source = "CONFIGURED_PAPER_PROXY"

        adaptive_context = None
        adaptive_runtime = {"enabled": False}
        bid_queue_estimate = None
        ask_queue_estimate = None
        bid_markout = None
        ask_markout = None
        flow_adverse = None
        if self.adaptive_enabled:
            bid_hawkes, ask_hawkes = self.adaptive_hawkes.multipliers(
                symbol,
                now,
            )
            bid_markout = self.adaptive_markout.estimate(symbol, Side.BUY)
            ask_markout = self.adaptive_markout.estimate(symbol, Side.SELL)
            bid_queue_estimate = self.adaptive_queue.estimate(
                symbol=symbol,
                quote_side=Side.BUY,
                queue_ahead_qty=max(0.0, float(bid_1_volume or 0.0)),
                sigma_bps_sqrt_s=sigma,
            )
            ask_queue_estimate = self.adaptive_queue.estimate(
                symbol=symbol,
                quote_side=Side.SELL,
                queue_ahead_qty=max(0.0, float(ask_1_volume or 0.0)),
                sigma_bps_sqrt_s=sigma,
            )
            if self.adaptive_flow_toxicity_enabled:
                flow_adverse = estimate_flow_adverse_costs(
                    mid_price=mid,
                    best_bid=bid_1,
                    best_ask=ask_1,
                    bid_quantity=max(0.0, float(bid_1_volume or 0.0)),
                    ask_quantity=max(0.0, float(ask_1_volume or 0.0)),
                    signed_trade_imbalance=signed_orderflow_imbalance,
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
                "bid_A_per_s": A
                * self.adaptive_bid_A_multiplier
                * bid_hawkes,
                "ask_A_per_s": A
                * self.adaptive_ask_A_multiplier
                * ask_hawkes,
                "bid_k_per_bps": k * self.adaptive_bid_k_multiplier,
                "ask_k_per_bps": k * self.adaptive_ask_k_multiplier,
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
                "hawkes": self.adaptive_hawkes.summary(symbol, now),
                "markout": self.adaptive_markout.summary(symbol),
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
                    "signed_trade_imbalance": signed_orderflow_imbalance,
                    "microprice_offset_bps": (
                        flow_adverse.microprice_offset_bps
                        if flow_adverse is not None
                        else 0.0
                    ),
                    "bid_adverse_cost_bps": bid_flow_cost,
                    "ask_adverse_cost_bps": ask_flow_cost,
                },
                "parameter_bounds": {
                    "intensity_ratio": self.adaptive_intensity_uncertainty_ratio,
                    "k_ratio": self.adaptive_k_uncertainty_ratio,
                    "volatility_ratio": (
                        self.adaptive_volatility_uncertainty_ratio
                    ),
                },
            }

        try:
            formula_quote, portfolio_risk = self._calculate_formula_quote(
                symbol=symbol,
                mid_price=mid,
                fair_mid=fair_mid,
                inventory_lots=inventory_lots,
                sigma_bps_sqrt_s=sigma,
                gamma_per_bps=gamma,
                A_per_s=A,
                k_per_bps=k,
                order_size_lots=order_size_lots,
                inventory_lot_notional_usdt=inventory_lot_notional,
                target_position_notional_usdt=target_pos_usdt,
                now_monotonic=now,
                adaptive_context=adaptive_context,
            )
        except ValueError as exc:
            self._publish_formula_invalid(
                symbol,
                mid,
                bid_1,
                ask_1,
                readiness,
                predictor_samples,
                approved_intensity,
                runtime_intensity,
                str(exc),
            )
            self.feature_engine.reset_interval(symbol)
            return

        passive_tif = self.resolve_passive_time_in_force(
            symbol,
            use_rpi=self.use_rpi,
            fallback_to_gtx=self.rpi_fallback_to_gtx,
            route="glft_quote",
        )
        passive_fee_bps = self.passive_round_trip_fee_bps(
            symbol,
            passive_tif,
        )
        effective_min_spread_bps = max(
            self.min_spread_bps,
            passive_fee_bps,
        )
        effective_half_spread_bps = max(
            formula_quote.half_spread_bps,
            effective_min_spread_bps / 2.0,
        )
        target_bid, target_ask = depths_bps_to_prices(
            fair_mid,
            effective_half_spread_bps - formula_quote.center_offset_bps,
            effective_half_spread_bps + formula_quote.center_offset_bps,
        )

        info = ref_data_manager.get_info(symbol)
        if info is None:
            return
        tick = float(info.tick_size or 0.0)
        if tick <= 0.0:
            return
        target_bid = ref_data_manager.round_price(
            symbol,
            target_bid,
            direction="down",
        )
        target_ask = ref_data_manager.round_price(
            symbol,
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
        size_optimization = {"enabled": False}
        if (
            self.adaptive_enabled
            and adaptive_context is not None
            and bid_queue_estimate is not None
            and ask_queue_estimate is not None
        ):
            bid_size = optimize_quote_size(
                side=Side.BUY,
                candidate_multipliers=self.adaptive_size_candidates,
                base_order_size_lots=order_size_lots,
                inventory_lots=inventory_lots,
                depth_bps=formula_quote.bid_depth_bps,
                A_per_s=adaptive_context["bid_A_per_s"],
                k_per_bps=adaptive_context["bid_k_per_bps"],
                sigma_bps_sqrt_s=sigma,
                gamma_per_bps=gamma,
                fee_bps=passive_fee_bps,
                adverse_cost_bps=adaptive_context[
                    "bid_adverse_cost_bps"
                ],
                expected_queue_delay_s=bid_queue_estimate.expected_delay_s,
                utility_horizon_s=self.adaptive_size_utility_horizon_s,
                size_penalty_bps=self.adaptive_size_penalty_bps,
            )
            ask_size = optimize_quote_size(
                side=Side.SELL,
                candidate_multipliers=self.adaptive_size_candidates,
                base_order_size_lots=order_size_lots,
                inventory_lots=inventory_lots,
                depth_bps=formula_quote.ask_depth_bps,
                A_per_s=adaptive_context["ask_A_per_s"],
                k_per_bps=adaptive_context["ask_k_per_bps"],
                sigma_bps_sqrt_s=sigma,
                gamma_per_bps=gamma,
                fee_bps=passive_fee_bps,
                adverse_cost_bps=adaptive_context[
                    "ask_adverse_cost_bps"
                ],
                expected_queue_delay_s=ask_queue_estimate.expected_delay_s,
                utility_horizon_s=self.adaptive_size_utility_horizon_s,
                size_penalty_bps=self.adaptive_size_penalty_bps,
            )
            bid_order_vol = self._scale_safe_volume(
                symbol,
                bid_order_vol,
                bid_size.multiplier,
                target_bid,
            )
            ask_order_vol = self._scale_safe_volume(
                symbol,
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
        self._update_quotes(
            symbol,
            target_bid,
            target_ask,
            reference_order_volume,
            time_in_force=passive_tif,
            bid_volume=bid_order_vol,
            ask_volume=ask_order_vol,
        )

        quote_state = self.quote_state[symbol]
        quote_spread_bps = (target_ask - target_bid) / mid * 10_000.0
        params = {
            "schema": "market_making.v1",
            "strategy": self.name,
            "state": "QUOTING",
            "mode": passive_tif,
            "time_in_force": passive_tif,
            "use_rpi": self.use_rpi,
            "rpi_supported": ref_data_manager.supports_rpi(symbol),
            "mid_price": mid,
            "best_bid": bid_1,
            "best_ask": ask_1,
            "market_spread_bps": (ask_1 - bid_1) / mid * 10_000.0,
            "fair_value": fair_mid,
            "alpha_enabled": self.alpha_enabled,
            "alpha_bps": alpha_offset_bps,
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
            "gamma_base_per_bps": self.gamma_base,
            "gamma_per_bps": gamma,
            "k_per_bps": k,
            "A_per_s": A,
            "sigma_bps": sigma,
            "sigma_units": "bps/sqrt(second)",
            "margin_usage": margin_usage,
            "orderflow_imbalance": signed_orderflow_imbalance,
            "recent_fill_defense": recent_fill_defense,
            "readiness": readiness.as_params(),
            "signals": {
                horizon: float(value) for horizon, value in alphas.items()
            },
            "short_signal_bps": short_signal,
            "target_position_notional": target_pos_usdt,
            "effective_position_notional": effective_pos_usdt,
            "inventory_lots": inventory_lots,
            "order_size_lots": order_size_lots,
            "inventory_lot_notional_usdt": inventory_lot_notional,
            "formula_half_spread_bps": formula_quote.half_spread_bps,
            "inventory_center_offset_bps": (
                formula_quote.center_offset_bps
            ),
            "formula_bid_depth_bps": formula_quote.bid_depth_bps,
            "formula_ask_depth_bps": formula_quote.ask_depth_bps,
            "configured_min_spread_bps": self.min_spread_bps,
            "passive_fee_bps": passive_fee_bps,
            "effective_min_spread_bps": effective_min_spread_bps,
            "intensity_source": intensity_source,
            "approved_rpi_intensity": self._intensity_summary(
                approved_intensity
            ),
            "runtime_rpi_intensity": self._intensity_summary(
                runtime_intensity
            ),
            "adaptive": adaptive_runtime,
            "size_optimization": size_optimization,
            "stale_quote_guard": dict(self.latest_stale_guard[symbol]),
            "market_data_timing": self._published_market_timing(symbol),
            "portfolio_risk": portfolio_risk,
            "units_version": UNITS_VERSION,
            "formula_version": self.formula_version,
            "State": "QUOTING",
            "Mode": passive_tif,
            "Spread": f"{quote_spread_bps:.1f}",
            "Sigma": f"{sigma:.1f}",
            "Size": f"{max(bid_order_vol, ask_order_vol):.8g}",
        }
        self.engine.put(
            Event(
                EVENT_STRATEGY_UPDATE,
                StrategyData(
                    symbol=symbol,
                    fair_value=fair_mid,
                    alpha_bps=alpha_offset_bps,
                    params=params,
                ),
            )
        )
        self.feature_engine.reset_interval(symbol)

    def _run_rpi_calibration_cycle(
        self,
        *,
        symbol: str,
        mid: float,
        best_bid: float,
        best_ask: float,
        now_monotonic: float,
    ) -> None:
        if symbol != self.rpi_calibration_symbol:
            self._publish_rpi_calibration_status(
                symbol,
                mid,
                best_bid,
                best_ask,
                "CALIBRATION_SYMBOL_MISMATCH",
                {},
                now_monotonic,
            )
            return

        enforce_limits = getattr(
            self.oms,
            "enforce_rpi_calibration_runtime_limits",
            None,
        )
        if not callable(enforce_limits):
            self._publish_rpi_calibration_status(
                symbol,
                mid,
                best_bid,
                best_ask,
                "CALIBRATION_OMS_GUARD_MISSING",
                {},
                now_monotonic,
            )
            return
        try:
            enforced = enforce_limits()
        except Exception as exc:
            self.log(f"RPI calibration runtime guard failed: {exc}")
            return
        if enforced is False:
            return

        try:
            exchange_now = float(time_service.now_seconds())
        except Exception as exc:
            self.log(f"RPI calibration exchange time unavailable: {exc}")
            return
        if (
            not math.isfinite(exchange_now)
            or exchange_now >= self.rpi_calibration_expires_at
        ):
            self._expire_rpi_calibration(
                "calibration_permit_expired",
                symbol,
            )
            self._publish_rpi_calibration_status(
                symbol,
                mid,
                best_bid,
                best_ask,
                "CALIBRATION_PERMIT_EXPIRED",
                {},
                now_monotonic,
            )
            return

        gate_snapshot = self.oms.get_outbound_gate_snapshot()
        calibration = gate_snapshot.get("rpi_calibration", {})
        if not isinstance(calibration, dict):
            calibration = {}
        if (
            calibration.get("enabled") is not True
            or str(calibration.get("permit_id", "") or "")
            != self.rpi_calibration_permit_id
            or str(calibration.get("permit_sha256", "") or "").lower()
            != self.rpi_calibration_permit_sha256
        ):
            self._expire_rpi_calibration(
                "calibration_oms_permit_binding_mismatch",
                symbol,
            )
            self._publish_rpi_calibration_status(
                symbol,
                mid,
                best_bid,
                best_ask,
                "CALIBRATION_PERMIT_BINDING_INVALID",
                calibration,
                now_monotonic,
            )
            return

        strategy_active = [
            client_oid
            for client_oid, intent in self.active_orders.items()
            if intent.symbol == symbol
        ]
        active_count = max(
            len(strategy_active),
            int(calibration.get("active_order_count", 0) or 0),
        )
        if active_count > 1:
            self._expire_rpi_calibration(
                "calibration_multiple_active_orders",
                symbol,
            )
            return
        if active_count:
            self._cancel_expired_calibration_quote(
                strategy_active,
                now_monotonic,
            )
            self._publish_rpi_calibration_status(
                symbol,
                mid,
                best_bid,
                best_ask,
                "CALIBRATION_ORDER_ACTIVE",
                calibration,
                now_monotonic,
            )
            return

        if (
            calibration.get("expired") is True
            or calibration.get("budget_exhausted") is True
        ):
            self._expire_rpi_calibration(
                "calibration_budget_or_time_exhausted",
                symbol,
            )
            self._publish_rpi_calibration_status(
                symbol,
                mid,
                best_bid,
                best_ask,
                "CALIBRATION_BUDGET_EXHAUSTED",
                calibration,
                now_monotonic,
            )
            return

        reserved_count = int(
            calibration.get("reserved_order_count", 0) or 0
        )
        last_reserved_at = float(
            calibration.get("last_reserved_exchange_time", 0.0) or 0.0
        )
        if (
            last_reserved_at > 0.0
            and exchange_now - last_reserved_at
            < self.rpi_calibration_min_order_interval_sec
        ):
            self._publish_rpi_calibration_status(
                symbol,
                mid,
                best_bid,
                best_ask,
                "CALIBRATION_INTERVAL_WAIT",
                calibration,
                now_monotonic,
            )
            return
        if not self.can_submit_orders(symbol):
            self._publish_rpi_calibration_status(
                symbol,
                mid,
                best_bid,
                best_ask,
                "CALIBRATION_EXECUTION_GATED",
                calibration,
                now_monotonic,
            )
            return

        current_position = float(
            self.oms.exposure.net_positions.get(symbol, 0.0) or 0.0
        )
        depth_index = (reserved_count // 2) % len(
            self.rpi_calibration_fixed_depths_bps
        )
        depth_bps = self.rpi_calibration_fixed_depths_bps[depth_index]
        if current_position > 0.0:
            side = Side.SELL
            reduce_only = True
        elif current_position < 0.0:
            side = Side.BUY
            reduce_only = True
        else:
            side = Side.BUY if reserved_count % 2 == 0 else Side.SELL
            reduce_only = False

        price = self._calibration_price(
            symbol,
            side,
            mid,
            depth_bps,
        )
        if (
            price <= 0.0
            or (side == Side.BUY and price >= best_ask)
            or (side == Side.SELL and price <= best_bid)
        ):
            if reduce_only and abs(current_position) > 1e-12:
                self._expire_rpi_calibration(
                    "calibration_residual_price_not_closeable",
                    symbol,
                )
            self._publish_rpi_calibration_status(
                symbol,
                mid,
                best_bid,
                best_ask,
                (
                    "CALIBRATION_RESIDUAL_FLATTEN_REQUIRED"
                    if reduce_only
                    else "CALIBRATION_PRICE_INVALID"
                ),
                calibration,
                now_monotonic,
                side=side,
                depth_bps=depth_bps,
            )
            return

        volume = self._calculate_safe_vol(
            symbol,
            price,
            side=side,
            current_position=current_position,
            reference_price=mid,
        )
        if reduce_only:
            volume = ref_data_manager.round_qty(
                symbol,
                min(volume, abs(current_position)),
            )
        notional = price * volume
        if (
            volume <= 0.0
            or notional + 1e-9
            < self.rpi_calibration_min_order_notional
            or notional
            > self.rpi_calibration_max_order_notional + 1e-9
        ):
            if reduce_only and abs(current_position) > 1e-12:
                self._expire_rpi_calibration(
                    "calibration_residual_size_not_closeable",
                    symbol,
                )
            self._publish_rpi_calibration_status(
                symbol,
                mid,
                best_bid,
                best_ask,
                (
                    "CALIBRATION_RESIDUAL_FLATTEN_REQUIRED"
                    if reduce_only
                    else "CALIBRATION_SIZE_INVALID"
                ),
                calibration,
                now_monotonic,
                side=side,
                depth_bps=depth_bps,
            )
            return

        intent = OrderIntent(
            strategy_id=self.name,
            symbol=symbol,
            side=side,
            price=price,
            volume=volume,
            order_type="LIMIT",
            time_in_force=TIF_RPI,
            is_post_only=True,
            reduce_only=reduce_only,
            tag="rpi_calibration_canary",
            calibration_permit_id=self.rpi_calibration_permit_id,
            calibration_depth_bps=depth_bps,
            calibration_reference_mid=mid,
        )
        client_oid = self.send_intent(intent)
        state = (
            "CALIBRATION_ORDER_SUBMITTED"
            if client_oid
            else "CALIBRATION_ORDER_REJECTED"
        )
        if client_oid:
            quote_state = self.quote_state[symbol]
            key = "bid" if side == Side.BUY else "ask"
            quote_state[f"{key}_oid"] = client_oid
            quote_state[f"{key}_price"] = price
            quote_state[f"{key}_volume"] = volume
        self._publish_rpi_calibration_status(
            symbol,
            mid,
            best_bid,
            best_ask,
            state,
            calibration,
            now_monotonic,
            side=side,
            depth_bps=depth_bps,
        )

    def _cancel_expired_calibration_quote(
        self,
        client_oids,
        now_monotonic: float,
    ) -> None:
        for client_oid in client_oids:
            quote = self.rpi_acknowledged_quotes.get(client_oid)
            if quote is None:
                continue
            if (
                now_monotonic - quote.acknowledged_at_monotonic
                >= self.rpi_calibration_order_ttl_sec
            ):
                self.cancel_order(client_oid)

    def _expire_rpi_calibration(self, reason: str, symbol: str) -> None:
        if self.rpi_calibration_expiry_handled:
            self._cancel_symbol_quotes(symbol)
            return
        self.rpi_calibration_expiry_handled = True
        expire = getattr(
            self.oms,
            "expire_rpi_calibration_permit",
            None,
        )
        if callable(expire):
            try:
                expire(reason)
                return
            except Exception as exc:
                self.log(f"RPI calibration fail-closed transition failed: {exc}")
        self._cancel_symbol_quotes(symbol)

    @staticmethod
    def _calibration_tick_round(
        raw_price: float,
        tick_size: float,
        side: Side,
    ) -> float:
        try:
            raw = Decimal(str(raw_price))
            tick = Decimal(str(tick_size))
            if not raw.is_finite() or not tick.is_finite() or tick <= 0:
                return 0.0
            rounding = ROUND_FLOOR if side == Side.BUY else ROUND_CEILING
            units = (raw / tick).to_integral_value(rounding=rounding)
            return float(units * tick)
        except (InvalidOperation, TypeError, ValueError, OverflowError):
            return 0.0

    def _calibration_price(
        self,
        symbol: str,
        side: Side,
        mid: float,
        depth_bps: float,
    ) -> float:
        info = ref_data_manager.get_info(symbol)
        if info is None:
            return 0.0
        direction = -1.0 if side == Side.BUY else 1.0
        try:
            raw_price = mid * math.exp(direction * depth_bps / 10_000.0)
        except OverflowError:
            return 0.0
        return self._calibration_tick_round(
            raw_price,
            float(info.tick_size or 0.0),
            side,
        )

    def _publish_rpi_calibration_status(
        self,
        symbol: str,
        mid: float,
        best_bid: float,
        best_ask: float,
        state: str,
        calibration: dict,
        now_monotonic: float,
        *,
        side: Side | None = None,
        depth_bps: float | None = None,
    ) -> None:
        signature = (
            state,
            int(calibration.get("reserved_order_count", 0) or 0),
            int(calibration.get("active_order_count", 0) or 0),
            side.value if side else "",
            depth_bps,
        )
        if (
            self.last_calibration_status_signature.get(symbol) == signature
            and now_monotonic - self.last_calibration_status_at[symbol] < 0.25
        ):
            return
        self.last_calibration_status_signature[symbol] = signature
        self.last_calibration_status_at[symbol] = now_monotonic
        params = {
            "schema": "market_making.v1",
            "strategy": self.name,
            "state": state,
            "mode": "RPI_CALIBRATION_CANARY",
            "time_in_force": TIF_RPI,
            "use_rpi": True,
            "rpi_supported": ref_data_manager.supports_rpi(symbol),
            "mid_price": mid,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "fair_value": mid,
            "alpha_enabled": False,
            "alpha_bps": 0.0,
            "permit_id": self.rpi_calibration_permit_id,
            "permit_sha256": self.rpi_calibration_permit_sha256,
            "fixed_depths_bps": list(
                self.rpi_calibration_fixed_depths_bps
            ),
            "selected_depth_bps": depth_bps,
            "selected_side": side.value if side else "",
            "order_ttl_sec": self.rpi_calibration_order_ttl_sec,
            "min_order_interval_sec": (
                self.rpi_calibration_min_order_interval_sec
            ),
            "reserved_order_count": int(
                calibration.get("reserved_order_count", 0) or 0
            ),
            "active_order_count": int(
                calibration.get("active_order_count", 0) or 0
            ),
            "cumulative_submitted_notional_usdt": float(
                calibration.get(
                    "cumulative_submitted_notional_usdt",
                    0.0,
                )
                or 0.0
            ),
            "data_source": "LIVE_BINANCE_RPI_ACK",
            "market_data_timing": self._published_market_timing(symbol),
            "units_version": UNITS_VERSION,
            "formula_version": GLFT_FORMULA_VERSION,
            "State": state,
            "Mode": "RPI_CALIBRATION_CANARY",
        }
        self.engine.put(
            Event(
                EVENT_STRATEGY_UPDATE,
                StrategyData(
                    symbol=symbol,
                    fair_value=mid,
                    alpha_bps=0.0,
                    params=params,
                ),
            )
        )

    def _publish_formula_invalid(
        self,
        symbol,
        mid,
        best_bid,
        best_ask,
        readiness,
        predictor_samples,
        approved_intensity,
        runtime_intensity,
        formula_error,
    ):
        self._publish_warming_up(
            symbol,
            mid,
            best_bid,
            best_ask,
            readiness,
            predictor_samples=predictor_samples,
            approved_intensity=approved_intensity,
            runtime_intensity=runtime_intensity,
            formula_error=formula_error,
        )

    def _publish_warming_up(
        self,
        symbol,
        mid,
        best_bid,
        best_ask,
        readiness,
        *,
        predictor_samples,
        approved_intensity,
        runtime_intensity,
        runtime_intensity_error="",
        formula_error="",
    ):
        self._cancel_symbol_quotes(symbol)
        readiness_params = readiness.as_params()
        readiness_params["predictor_samples"] = int(
            max(0, predictor_samples)
        )
        readiness_params["intensity_samples"] = int(
            max(0, approved_intensity.sample_count)
        )
        readiness_params["approved_rpi_intensity"] = self._intensity_summary(
            approved_intensity
        )
        readiness_params["runtime_rpi_intensity"] = self._intensity_summary(
            runtime_intensity
        )
        state = "WARMING_UP"
        if self.live_mode and not approved_intensity.ready:
            state = approved_intensity.state
            readiness_params["ready"] = False
            readiness_params["state"] = state
            readiness_params["reasons"] = [
                *readiness_params.get("reasons", []),
                *approved_intensity.reasons,
            ]
        if self.live_mode and runtime_intensity_error:
            state = "RPI_EXPOSURE_INVALID"
            readiness_params["ready"] = False
            readiness_params["state"] = state
            readiness_params["reasons"] = [
                *readiness_params.get("reasons", []),
                runtime_intensity_error,
                *runtime_intensity.reasons,
            ]
        if formula_error:
            state = "FORMULA_INVALID"
            readiness_params["ready"] = False
            readiness_params["state"] = state
            readiness_params["formula_error"] = formula_error
        params = {
            "schema": "market_making.v1",
            "strategy": self.name,
            "state": state,
            "mode": "OBSERVE_ONLY",
            "time_in_force": "",
            "use_rpi": self.use_rpi,
            "rpi_supported": ref_data_manager.supports_rpi(symbol),
            "mid_price": mid,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "fair_value": mid,
            "alpha_enabled": self.alpha_enabled,
            "alpha_bps": 0.0,
            "intensity_source": (
                "APPROVED_RPI_ARTIFACT_FROZEN"
                if self.live_mode
                else "CONFIGURED_PAPER_PROXY"
            ),
            "units_version": UNITS_VERSION,
            "formula_version": self.formula_version,
            "readiness": readiness_params,
            "market_data_timing": self._published_market_timing(symbol),
        }
        self.engine.put(
            Event(
                EVENT_STRATEGY_UPDATE,
                StrategyData(
                    symbol=symbol,
                    fair_value=mid,
                    alpha_bps=0.0,
                    params=params,
                ),
            )
        )

    def _decayed_orderflow_imbalance(
        self,
        symbol: str,
        now_monotonic: float,
    ) -> float:
        raw = max(-1.0, min(1.0, float(self.imbalance_ewma[symbol])))
        if self.live_mode:
            return raw
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
                    "mid_observed_monotonic": observation.mid_observed_monotonic,
                    "observation_lag_ms": observation.observation_lag_ms,
                }
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

        state = self.quote_state[symbol]
        for key, depth in (
            (
                "bid",
                (
                    math.log(mid_price / float(state["bid_price"])) * 10_000.0
                    if state["bid_price"] is not None
                    and float(state["bid_price"]) > 0.0
                    else None
                ),
            ),
            (
                "ask",
                (
                    math.log(float(state["ask_price"]) / mid_price) * 10_000.0
                    if state["ask_price"] is not None
                    and float(state["ask_price"]) > 0.0
                    else None
                ),
            ),
        ):
            result[f"{key}_depth_bps"] = depth
            client_oid = state[f"{key}_oid"]
            at_risk = (
                client_oid is not None
                and depth is not None
                and depth < self.stale_quote_min_depth_bps
            )
            result[f"{key}_at_risk"] = at_risk
            if not at_risk:
                continue
            accepted = self.cancel_order(client_oid)
            result[f"{key}_cancel_requested"] = bool(
                accepted or client_oid in self.orders_cancelling
            )
        return result

    def _cancel_symbol_quotes(self, symbol: str) -> None:
        state = self.quote_state[symbol]
        client_oids = {
            state.get("bid_oid"),
            state.get("ask_oid"),
            *(
                oid
                for oid, intent in self.active_orders.items()
                if intent.symbol == symbol
            ),
        }
        for client_oid in client_oids:
            if client_oid:
                self.cancel_order(client_oid)

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
        if not self.can_submit_orders(symbol):
            return
        info = ref_data_manager.get_info(symbol)
        if info is None:
            return
        tick = float(info.tick_size or 0.0)
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
        if (now - state["last_update"]) * 1_000.0 < self.cooldown_ms:
            return

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
        sign = -1.0 if trade.maker_is_buyer else 1.0
        event_monotonic = self._positive_snapshot_time(
            trade.received_monotonic
        ) or time.perf_counter()
        previous = self._decayed_orderflow_imbalance(
            trade.symbol,
            event_monotonic,
        )
        self.imbalance_ewma[trade.symbol] = (
            (1.0 - self.of_lambda) * previous + self.of_lambda * sign
        )
        self.imbalance_updated_at[trade.symbol] = event_monotonic
        if self.adaptive_enabled:
            aggressor_side = (
                Side.SELL if trade.maker_is_buyer else Side.BUY
            )
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

    def _rpi_depth_bps(
        self,
        side: Side,
        price: float,
        mid: float,
    ) -> float:
        if (
            not math.isfinite(mid)
            or not math.isfinite(price)
            or mid <= 0.0
            or price <= 0.0
        ):
            return math.nan
        if side == Side.BUY:
            return math.log(mid / price) * 10_000.0
        if side == Side.SELL:
            return math.log(price / mid) * 10_000.0
        return math.nan

    @staticmethod
    def _snapshot_side(
        snapshot: OrderStateSnapshot,
        intent,
    ) -> Side | None:
        value = snapshot.side
        if isinstance(value, Side):
            return value
        if value is not None:
            try:
                return Side(str(value))
            except ValueError:
                pass
        intent_side = getattr(intent, "side", None)
        return intent_side if isinstance(intent_side, Side) else None

    @staticmethod
    def _positive_snapshot_time(value) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return 0.0
        return parsed if math.isfinite(parsed) and parsed > 0.0 else 0.0

    def _start_rpi_exposure(
        self,
        snapshot: OrderStateSnapshot,
        *,
        initial_censor_reason: str = "",
    ) -> _AcknowledgedRPIQuote | None:
        existing = self.rpi_acknowledged_quotes.get(snapshot.client_oid)
        if existing is not None:
            return existing
        intent = self.active_orders.get(snapshot.client_oid)
        if not snapshot.is_rpi and not bool(
            getattr(intent, "is_rpi", False)
        ):
            return None

        side = self._snapshot_side(snapshot, intent)
        if side is None:
            return None
        price = self._positive_snapshot_time(snapshot.price)
        quantity = self._positive_snapshot_time(snapshot.volume)
        ack_monotonic = self._positive_snapshot_time(
            snapshot.updated_monotonic
        )
        ack_time = self._positive_snapshot_time(snapshot.update_time)
        exchange_oid = str(snapshot.exchange_oid or "").strip()
        censor_reason = str(initial_censor_reason or "").strip()
        if snapshot.recovered_from_journal:
            censor_reason = censor_reason or "recovered_order_clock_domain"
        if not exchange_oid:
            censor_reason = censor_reason or "missing_exchange_oid_at_rest_ack"
        if not price or not quantity:
            censor_reason = censor_reason or "invalid_order_price_or_quantity"
        if not ack_monotonic:
            censor_reason = censor_reason or "missing_oms_ack_monotonic"

        latest_book_time = self.latest_rpi_book_monotonic[snapshot.symbol]
        latest_mid = self.latest_rpi_mid[snapshot.symbol]
        if (
            not math.isfinite(latest_book_time)
            or latest_book_time <= 0.0
            or not math.isfinite(latest_mid)
            or latest_mid <= 0.0
        ):
            censor_reason = censor_reason or "missing_pre_ack_orderbook"
        elif ack_monotonic and latest_book_time > ack_monotonic:
            censor_reason = censor_reason or "first_segment_not_observable"
        elif (
            ack_monotonic
            and ack_monotonic - latest_book_time
            > self.rpi_max_initial_book_age_seconds
        ):
            censor_reason = censor_reason or "stale_pre_ack_orderbook"

        exposure = None
        if ack_monotonic:
            exposure = RPIOrderExposure(
                acknowledged_at_monotonic=ack_monotonic,
                initial_depth_bps=self._rpi_depth_bps(
                    side,
                    price,
                    latest_mid,
                ),
                depth_bin_width_bps=self.rpi_depth_bin_width_bps,
                censor_reason=censor_reason,
            )
            censor_reason = exposure.censor_reason

        quote = _AcknowledgedRPIQuote(
            symbol=snapshot.symbol,
            client_oid=snapshot.client_oid,
            exchange_oid=exchange_oid,
            side=side,
            price=price,
            quantity=quantity,
            acknowledged_at_time=ack_time,
            acknowledged_at_monotonic=ack_monotonic,
            exposure=exposure,
            censor_reason=censor_reason,
        )
        self.rpi_acknowledged_quotes[snapshot.client_oid] = quote
        return quote

    def _terminal_rpi_exposure(
        self,
        snapshot: OrderStateSnapshot,
    ) -> None:
        quote = self.rpi_acknowledged_quotes.pop(
            snapshot.client_oid,
            None,
        )
        if quote is None:
            quote = self._start_rpi_exposure(
                snapshot,
                initial_censor_reason="missing_pending_ack_snapshot",
            )
            if quote is None:
                return
            self.rpi_acknowledged_quotes.pop(snapshot.client_oid, None)

        if snapshot.recovered_from_journal:
            quote.mark_censored("recovered_order_clock_domain")
        terminal_exchange_oid = str(snapshot.exchange_oid or "").strip()
        if (
            not terminal_exchange_oid
            or terminal_exchange_oid != quote.exchange_oid
        ):
            quote.mark_censored("terminal_exchange_oid_mismatch")

        terminal_monotonic = self._positive_snapshot_time(
            snapshot.updated_monotonic
        )
        terminal_time = self._positive_snapshot_time(snapshot.update_time)
        if not terminal_time:
            quote.mark_censored("missing_terminal_time")
        if snapshot.filled_volume > 0.0 and not quote.seen_trade_ids:
            quote.mark_censored("fill_event_missing_before_terminal")
        if snapshot.filled_volume <= 0.0 and quote.seen_trade_ids:
            quote.mark_censored("fill_event_without_terminal_volume")

        if quote.exposure is None:
            exposure_bins = ()
            quote.mark_censored("missing_exposure_clock")
        else:
            if quote.censor_reason:
                quote.exposure.mark_censored(quote.censor_reason)
            result = quote.exposure.finish(
                terminal_at_monotonic=terminal_monotonic,
            )
            exposure_bins = result.exposure_bins
            if result.censored:
                quote.mark_censored(result.censor_reason)
            if (
                not quote.censor_reason
                and result.fill_count != len(quote.seen_trade_ids)
            ):
                quote.mark_censored("fill_count_not_attributed_to_depth")
                exposure_bins = ()

        payload = {
            "schema": "chronoshft.rpi_exposure_sample.v2",
            "strategy": self.name,
            "symbol": snapshot.symbol,
            "client_oid": snapshot.client_oid,
            "exchange_oid": quote.exchange_oid,
            "terminal_status": snapshot.status.value,
            "side": quote.side.value,
            "price": quote.price,
            "quantity": quote.quantity,
            "ack_time": quote.acknowledged_at_time,
            "ack_monotonic": quote.acknowledged_at_monotonic,
            "terminal_time": terminal_time,
            "terminal_monotonic": terminal_monotonic,
            "deployment_id": self.rpi_deployment_id,
            "strategy_policy_sha256": self.rpi_strategy_policy_sha256,
            "implementation_sha256": self.rpi_implementation_sha256,
            "exposure_bins": [
                {
                    "depth_bps": item.depth_bps,
                    "exposure_seconds": item.exposure_seconds,
                    "fill_count": item.fill_count,
                    "sample_count": item.sample_count,
                }
                for item in exposure_bins
            ],
            "fill_count": len(quote.seen_trade_ids),
            "censored": bool(quote.censor_reason),
            "censor_reason": quote.censor_reason,
            "units_version": UNITS_VERSION,
            "formula_version": GLFT_FORMULA_VERSION,
            "data_source": "LIVE_BINANCE_RPI_ACK",
        }
        try:
            committed_seq = self.oms.record_strategy_evidence(
                "rpi_exposure_sample",
                payload,
                symbol=snapshot.symbol,
            )
            if not committed_seq:
                raise RuntimeError("OMS journal did not commit the sample")
        except Exception as exc:
            self.log(f"RPI exposure persistence failed: {exc}")
            return

        if quote.censor_reason:
            return
        accumulator = self.rpi_intensity_accumulators[snapshot.symbol]
        for exposure_bin in exposure_bins:
            if not accumulator.add_acked_bin(exposure_bin):
                self.log(
                    "Validated RPI exposure bin was rejected by accumulator"
                )
                return

    def _record_rpi_order_snapshot(
        self,
        snapshot: OrderStateSnapshot,
    ) -> None:
        if not self.live_mode:
            return
        if snapshot.status == OrderStatus.PENDING_ACK:
            self._start_rpi_exposure(snapshot)
            return
        if (
            snapshot.status
            in {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}
            and snapshot.client_oid not in self.rpi_acknowledged_quotes
        ):
            self._start_rpi_exposure(
                snapshot,
                initial_censor_reason="missing_pending_ack_snapshot",
            )

        terminal_statuses = {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }
        if snapshot.status in terminal_statuses:
            self._terminal_rpi_exposure(snapshot)
        elif snapshot.status == OrderStatus.REJECTED_LOCALLY:
            self.rpi_acknowledged_quotes.pop(snapshot.client_oid, None)

    def on_order(self, snapshot: OrderStateSnapshot):
        self._record_rpi_order_snapshot(snapshot)
        super().on_order(snapshot)
        terminal_statuses = {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.REJECTED_LOCALLY,
            OrderStatus.EXPIRED,
        }
        if snapshot.status in terminal_statuses:
            state = self.quote_state[snapshot.symbol]
            if state["bid_oid"] == snapshot.client_oid:
                state["bid_oid"] = None
                state["bid_price"] = None
                state["bid_volume"] = None
            if state["ask_oid"] == snapshot.client_oid:
                state["ask_oid"] = None
                state["ask_price"] = None
                state["ask_volume"] = None

    def on_trade(self, trade: TradeData):
        now_monotonic = time.perf_counter()
        self.last_fill_time[trade.symbol] = now_monotonic
        if self.adaptive_enabled:
            self.adaptive_markout.record_fill(
                symbol=trade.symbol,
                side=trade.side,
                fill_price=trade.price,
                observed_at_monotonic=now_monotonic,
                client_oid=trade.order_id,
                trade_id=trade.trade_id,
            )
        if not self.live_mode:
            return
        quote = self.rpi_acknowledged_quotes.get(trade.order_id)
        if quote is None:
            return
        trade_id = str(trade.trade_id or "").strip()
        if not trade_id:
            quote.mark_censored("missing_trade_id")
            return
        if trade_id in quote.seen_trade_ids:
            return
        quote.seen_trade_ids.add(trade_id)
        if quote.exposure is not None:
            quote.exposure.record_fill(trade_id)
