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
    GLFT_FORMULA_VERSION,
    UNITS_VERSION,
    depths_bps_to_prices,
    glft_quote_offsets,
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
        self.cycle_interval = self._positive_finite(
            self.strat_conf.get(
                "cycle_interval",
                self.glft_conf.get("cycle_interval", 1.0),
            ),
            1.0,
        )
        raw_execution = self.strat_conf.get(
            "execution",
            self.glft_conf.get("execution", {}),
        )
        model_execution = (
            raw_execution if isinstance(raw_execution, dict) else {}
        )
        self.min_spread_bps = self._positive_finite(
            model_execution.get("min_spread_bps", 5.0),
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

    def on_orderbook(self, ob: OrderBook):
        symbol = ob.symbol
        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if (
            not math.isfinite(bid_1)
            or not math.isfinite(ask_1)
            or bid_1 <= 0.0
            or ask_1 <= bid_1
        ):
            return

        now = time.perf_counter()
        mid = (bid_1 + ask_1) / 2.0
        self._observe_rpi_orderbook(
            symbol=symbol,
            mid=mid,
            book_monotonic=self._orderbook_monotonic(ob),
            callback_monotonic=now,
        )
        self.latest_mid[symbol] = mid

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
            self.inventory_lot_notional_usdt
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

        orderflow_imbalance = abs(self.imbalance_ewma[symbol])
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

        try:
            formula_quote = glft_quote_offsets(
                mid_price=fair_mid,
                inventory_lots=inventory_lots,
                sigma_bps_sqrt_s=sigma,
                gamma_per_bps=gamma,
                A_per_s=A,
                k_per_bps=k,
                order_size_lots=order_size_lots,
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
        target_bid = ref_data_manager.round_price(symbol, target_bid)
        target_ask = ref_data_manager.round_price(symbol, target_ask)
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
            "orderflow_imbalance": self.imbalance_ewma[symbol],
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
            "units_version": UNITS_VERSION,
            "formula_version": GLFT_FORMULA_VERSION,
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
            "formula_version": GLFT_FORMULA_VERSION,
            "readiness": readiness_params,
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
        previous = self.imbalance_ewma[trade.symbol]
        self.imbalance_ewma[trade.symbol] = (
            (1.0 - self.of_lambda) * previous + self.of_lambda * sign
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
        self.last_fill_time[trade.symbol] = time.perf_counter()
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
