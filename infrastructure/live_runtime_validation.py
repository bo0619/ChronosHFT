"""Domain validators used by the live runtime configuration gate.

This module is imported lazily by ``live_config_guard``. The public gate
passes its already-initialized primitive validators explicitly, keeping the
dependency graph acyclic while preserving validation failure ordering.
"""

import math
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from types import ModuleType

from governance.contracts import market_data_environment
from infrastructure.rpi_policy import effective_rpi_route_enabled
from strategy.registry import strategy_id_for_model


def _validate_live_canary_strategy_config(
    config: dict,
    root_strategy: Mapping,
    strategy: Mapping,
    live_launch: Mapping,
    limits: Mapping,
    *,
    is_calibration_canary: bool,
    deployment_id: str,
    deployed_cap: float | None,
    deployment_loss_cap: float | None,
    violations: list[str],
    guard: ModuleType,
) -> None:
    if not guard._enabled(live_launch.get("rpi_only")):
        violations.append("live_launch.rpi_only must be true")
    if not guard._enabled(strategy.get("use_rpi")):
        violations.append(
            "live_launch.rpi_only requires strategy.use_rpi=true"
        )
    elif not effective_rpi_route_enabled(config):
        violations.append(
            "live_launch.rpi_only requires the primary strategy's "
            "effective RPI route to be enabled"
        )
    if guard._enabled(strategy.get("rpi_fallback_to_gtx")):
        violations.append(
            "live_launch.rpi_only requires "
            "strategy.rpi_fallback_to_gtx=false"
        )
    rpi_live_policy = guard._section(strategy, "rpi_live_policy")
    if not guard._enabled(rpi_live_policy.get("require_zero_commission")):
        violations.append(
            "strategy.rpi_live_policy.require_zero_commission must be true"
        )

    primary_model = str(
        root_strategy.get("primary_model", "") or ""
    ).strip().lower()
    if primary_model != "glft":
        violations.append(
            "live_launch canary requires strategy.primary_model='glft'"
        )
    if is_calibration_canary and root_strategy.get("registered_models") != [
        "glft"
    ]:
        violations.append(
            "rpi_calibration_canary requires "
            "strategy.registered_models=['glft']"
        )
    glft = guard._section(strategy, "glft")
    alpha_value = guard._model_value(strategy, glft, "alpha", {})
    alpha = alpha_value if isinstance(alpha_value, Mapping) else {}
    if guard._enabled(alpha.get("enabled")):
        violations.append(
            "live_launch canary requires strategy.glft.alpha.enabled=false"
        )
    portfolio_risk_value = guard._model_value(
        strategy,
        glft,
        "portfolio_risk",
        {},
    )
    portfolio_risk = (
        portfolio_risk_value
        if isinstance(portfolio_risk_value, Mapping)
        else {}
    )
    if guard._enabled(portfolio_risk.get("enabled")):
        violations.append(
            "live_launch canary requires "
            "strategy.glft.portfolio_risk.enabled=false until separately "
            "approved"
        )
    adaptive_value = guard._model_value(strategy, glft, "adaptive", {})
    adaptive = adaptive_value if isinstance(adaptive_value, Mapping) else {}
    if guard._enabled(adaptive.get("enabled")):
        violations.append(
            "live_launch canary requires "
            "strategy.glft.adaptive.enabled=false until separately approved"
        )
    target_inventory = guard._model_value(
        strategy,
        glft,
        "target_inventory_notional_usdt",
    )
    try:
        target_inventory = float(target_inventory)
    except (TypeError, ValueError):
        target_inventory = math.nan
    if not math.isfinite(target_inventory) or target_inventory != 0.0:
        violations.append(
            "live_launch canary requires "
            "strategy.glft.target_inventory_notional_usdt=0"
        )
    target_order_notional = guard._positive_finite_value(
        strategy.get("target_order_notional")
    )
    if target_order_notional is None:
        violations.append(
            "strategy.target_order_notional must be positive and finite"
        )
    elif is_calibration_canary and (
        target_order_notional > guard.MAX_CALIBRATION_ORDER_NOTIONAL_USDT
    ):
        violations.append(
            "rpi_calibration_canary strategy.target_order_notional must not "
            f"exceed {guard.MAX_CALIBRATION_ORDER_NOTIONAL_USDT:g} USDT"
        )
    elif (
        not is_calibration_canary
        and target_order_notional > guard.MAX_CANARY_ORDER_NOTIONAL_USDT
    ):
        violations.append(
            "strategy.target_order_notional must not exceed "
            f"{guard.MAX_CANARY_ORDER_NOTIONAL_USDT:g} USDT for a live canary"
        )
    elif (
        not is_calibration_canary
        and deployed_cap is not None
        and target_order_notional
        > deployed_cap * (guard.MAX_CANARY_ORDER_FRACTION / 1.5)
    ):
        violations.append(
            "strategy.target_order_notional must not exceed "
            "8% of deployed capital"
        )

    max_pos_usdt = guard._positive_finite_value(
        guard._model_value(strategy, glft, "max_pos_usdt")
    )
    risk_max_pos = guard._positive_finite_value(limits.get("max_pos_notional"))
    if max_pos_usdt is None:
        violations.append(
            "effective strategy.max_pos_usdt must be positive and finite"
        )
    elif risk_max_pos is not None and max_pos_usdt > risk_max_pos:
        violations.append(
            "effective strategy.max_pos_usdt must not exceed "
            "risk.limits.max_pos_notional"
        )

    gamma = guard._positive_finite_value(
        guard._model_value(strategy, glft, "gamma", 0.1)
    )
    if gamma is None or gamma > 1.0:
        violations.append(
            "effective GLFT gamma must be positive, finite, and no more than 1"
        )
    cycle_interval = guard._positive_finite_value(
        guard._model_value(strategy, glft, "cycle_interval", 1.0)
    )
    if cycle_interval is None or cycle_interval < 0.25:
        violations.append(
            "effective GLFT cycle_interval must be at least 0.25 seconds"
        )
    if is_calibration_canary:
        calibration_policy = guard._validated_rpi_calibration_policy(
            config,
            violations,
            deployment_id=deployment_id,
            symbol=guard._configured_symbol(config),
            deployment_loss_cap=deployment_loss_cap,
            risk_order_cap=guard._positive_finite_value(
                limits.get("max_order_notional")
            ),
            target_order_notional=target_order_notional,
        )
        permit_interval = guard._positive_finite_value(
            calibration_policy.get("min_order_interval_sec")
        )
        if (
            cycle_interval is not None
            and permit_interval is not None
            and cycle_interval < permit_interval
        ):
            violations.append(
                "rpi_calibration_canary effective GLFT cycle_interval must "
                "be at least the signed permit min_order_interval_sec"
            )

    execution_value = guard._model_value(strategy, glft, "execution", {})
    execution_config = (
        execution_value if isinstance(execution_value, Mapping) else {}
    )
    min_spread_bps = guard._positive_finite_value(
        execution_config.get("min_spread_bps", 5.0)
    )
    if min_spread_bps is None or min_spread_bps < 1.0:
        violations.append(
            "effective GLFT execution.min_spread_bps must be at least 1"
        )

    readiness = guard._section(strategy, "model_readiness")
    readiness_models = guard._section(readiness, "models")
    glft_readiness = guard._section(readiness_models, "glft")
    readiness_volatility_samples = guard._positive_finite_value(
        glft_readiness.get(
            "min_volatility_samples",
            readiness.get("min_volatility_samples"),
        )
    )
    readiness_model_samples = guard._positive_finite_value(
        glft_readiness.get(
            "min_model_samples",
            readiness.get("min_model_samples"),
        )
    )
    readiness_values = (
        readiness_volatility_samples,
        readiness_model_samples,
    )
    if any(
        value is None or not value.is_integer()
        for value in readiness_values
    ):
        violations.append(
            "effective GLFT model readiness sample requirements must be "
            "positive integers"
        )
        required_calibrator_samples = None
    else:
        required_calibrator_samples = max(readiness_values)

    calibrator_value = guard._model_value(strategy, glft, "calibrator", {})
    calibrator = (
        calibrator_value if isinstance(calibrator_value, Mapping) else {}
    )
    for field in ("window", "min_samples"):
        parsed = guard._positive_finite_value(calibrator.get(field))
        if (
            parsed is None
            or not parsed.is_integer()
            or (
                required_calibrator_samples is not None
                and parsed < required_calibrator_samples
            )
        ):
            required_text = (
                f" and at least {required_calibrator_samples:g}"
                if required_calibrator_samples is not None
                else ""
            )
            violations.append(
                f"effective GLFT calibrator.{field} must be a positive "
                f"integer{required_text}"
            )
    calibrator_bounds = {}
    for field in (
        "initial_sigma_bps",
        "sigma_max_bps",
        "max_tick_gap_sec",
    ):
        parsed = guard._positive_finite_value(calibrator.get(field))
        if parsed is None:
            violations.append(
                f"effective GLFT calibrator.{field} must be positive and finite"
            )
        calibrator_bounds[field] = parsed
    if (
        calibrator_bounds["initial_sigma_bps"] is not None
        and calibrator_bounds["sigma_max_bps"] is not None
        and calibrator_bounds["initial_sigma_bps"]
        > calibrator_bounds["sigma_max_bps"]
    ):
        violations.append(
            "effective GLFT calibrator.initial_sigma_bps must not exceed "
            "calibrator.sigma_max_bps"
        )

    inventory_lot_notional = guard._positive_finite_value(
        guard._model_value(
            strategy,
            glft,
            "inventory_lot_notional_usdt",
        )
    )
    if inventory_lot_notional is None:
        violations.append(
            "strategy.glft.inventory_lot_notional_usdt must be positive "
            "and finite"
        )
    elif (
        target_order_notional is not None
        and not math.isclose(
            inventory_lot_notional,
            target_order_notional,
            rel_tol=0.0,
            abs_tol=1e-9,
        )
    ):
        violations.append(
            "strategy.glft.inventory_lot_notional_usdt must equal "
            "strategy.target_order_notional"
        )

    rpi_intensity_value = guard._model_value(strategy, glft, "rpi_intensity", {})
    rpi_intensity = (
        rpi_intensity_value
        if isinstance(rpi_intensity_value, Mapping)
        else {}
    )
    intensity_minimums = {
        "min_sample_count": guard.MIN_CANARY_RPI_INTENSITY_SAMPLES,
        "min_depth_level_count": guard.MIN_CANARY_RPI_DEPTH_LEVELS,
        "min_total_exposure_seconds": guard.MIN_CANARY_RPI_EXPOSURE_SEC,
        "min_fill_count": guard.MIN_CANARY_RPI_FILLS,
        "min_depth_span_bps": guard.MIN_CANARY_RPI_DEPTH_SPAN_BPS,
    }
    integer_fields = {
        "min_sample_count",
        "min_depth_level_count",
        "min_fill_count",
    }
    for field, minimum in intensity_minimums.items():
        parsed = guard._positive_finite_value(rpi_intensity.get(field))
        if (
            parsed is None
            or parsed < minimum
            or (field in integer_fields and not parsed.is_integer())
        ):
            violations.append(
                f"strategy.glft.rpi_intensity.{field} must be "
                f"at least {minimum:g}"
            )




def _validate_live_runtime_operational_config(
    config: dict,
    execution: Mapping,
    market_data: Mapping,
    tech_health: Mapping,
    web_dashboard: Mapping,
    admin_control: Mapping,
    *,
    config_base_dir: Path | None,
    external_alert_environ: Mapping[str, str] | None,
    violations: list[str],
    guard: ModuleType,
) -> None:
    execution_mode = str(execution.get("mode", "") or "").strip().lower()
    if execution_mode != "live":
        violations.append("execution.mode must be explicitly set to 'live'")
    market_environment = market_data_environment(config)
    if market_environment != "production":
        violations.append(
            "system.market_data.environment must be 'production'"
        )
    risk_latency_ms = guard._positive_finite_value(
        tech_health.get("max_latency_ms")
    )
    ingress_age_ms = guard._positive_finite_value(
        market_data.get(
            "max_market_event_ingress_age_ms",
            risk_latency_ms or 1000.0,
        )
    )
    if ingress_age_ms is None or ingress_age_ms < 100.0:
        violations.append(
            "system.market_data.max_market_event_ingress_age_ms must be "
            "finite and at least 100ms"
        )
    elif risk_latency_ms is not None and ingress_age_ms > risk_latency_ms:
        violations.append(
            "system.market_data.max_market_event_ingress_age_ms must be no "
            "greater than risk.tech_health.max_latency_ms so stale batches "
            "are rejected before symbol circuit breakers"
        )
    if web_dashboard.get("enabled") is not True:
        violations.append("system.web_dashboard.enabled must be JSON true")
    dashboard_host = str(
        web_dashboard.get("host", "") or ""
    ).strip().lower()
    if dashboard_host not in {"127.0.0.1", "::1", "localhost"}:
        violations.append(
            "system.web_dashboard.host must be an explicit loopback address"
        )
    dashboard_port = web_dashboard.get("port")
    if (
        isinstance(dashboard_port, bool)
        or not isinstance(dashboard_port, int)
        or not 1 <= dashboard_port <= 65535
    ):
        violations.append(
            "system.web_dashboard.port must be an integer from 1 to 65535"
        )
    dashboard_request_threads = web_dashboard.get("max_request_threads", 8)
    if (
        isinstance(dashboard_request_threads, bool)
        or not isinstance(dashboard_request_threads, int)
        or not 1 <= dashboard_request_threads <= 32
    ):
        violations.append(
            "system.web_dashboard.max_request_threads must be an integer "
            "from 1 to 32"
        )
    dashboard_request_timeout = guard._positive_finite_value(
        web_dashboard.get("request_timeout_sec", 5.0)
    )
    if (
        dashboard_request_timeout is None
        or not 0.1 <= dashboard_request_timeout <= 30.0
    ):
        violations.append(
            "system.web_dashboard.request_timeout_sec must be finite and "
            "from 0.1 to 30 seconds"
        )
    admin_command_ttl = guard._positive_finite_value(
        admin_control.get("command_ttl_sec")
    )
    if admin_command_ttl is None or admin_command_ttl > 30.0:
        violations.append(
            "system.admin_control.command_ttl_sec must be positive and no "
            "more than 30 seconds"
        )
    admin_session_max_age = guard._positive_finite_value(
        admin_control.get("session_max_age_sec")
    )
    if admin_session_max_age is None or admin_session_max_age > 5.0:
        violations.append(
            "system.admin_control.session_max_age_sec must be positive and "
            "no more than 5 seconds"
        )
    elif (
        admin_command_ttl is not None
        and admin_session_max_age >= admin_command_ttl
    ):
        violations.append(
            "system.admin_control.session_max_age_sec must be less than "
            "command_ttl_sec"
        )

    try:
        guard.validate_live_external_alert_config(
            config,
            environ=external_alert_environ,
            base_dir=config_base_dir,
        )
    except (TypeError, ValueError) as exc:
        violations.append(str(exc))
    try:
        guard.validate_live_evidence_recorder_config(
            config,
            base_dir=config_base_dir,
        )
    except (TypeError, ValueError) as exc:
        violations.append(str(exc))




def _validate_live_runtime_safety_planes(
    risk: Mapping,
    time_sync: Mapping,
    market_freshness: Mapping,
    margin_health: Mapping,
    funding_guard: Mapping,
    supervisor: Mapping,
    limits: Mapping,
    *,
    violations: list[str],
    guard: ModuleType,
) -> None:
    if not guard._enabled(risk.get("active")):
        violations.append("risk.active must be true")
    if not guard._enabled(time_sync.get("startup_required")):
        violations.append("system.time_sync.startup_required must be true")
    if not guard._enabled(time_sync.get("require_healthy_for_trading")):
        violations.append(
            "system.time_sync.require_healthy_for_trading must be true"
        )
    if not guard._enabled(market_freshness.get("enabled")):
        violations.append("risk.market_data_freshness.enabled must be true")
    if not guard._enabled(market_freshness.get("require_mark_price")):
        violations.append(
            "risk.market_data_freshness.require_mark_price must be true"
        )
    if not guard._enabled(market_freshness.get("require_book")):
        violations.append(
            "risk.market_data_freshness.require_book must be true"
        )
    for field in ("max_mark_age_ms", "max_book_age_ms"):
        if not guard._positive_finite(market_freshness.get(field)):
            violations.append(
                f"risk.market_data_freshness.{field} must be positive"
            )
    if not guard._enabled(margin_health.get("enabled")):
        violations.append("risk.margin_health.enabled must be true")
    if not guard._enabled(margin_health.get("require_snapshot")):
        violations.append("risk.margin_health.require_snapshot must be true")
    if not guard._positive_finite(margin_health.get("max_snapshot_age_sec")):
        violations.append(
            "risk.margin_health.max_snapshot_age_sec must be positive"
        )

    if not guard._enabled(funding_guard.get("enabled")):
        violations.append("risk.funding_guard.enabled must be true")
    if not guard._enabled(funding_guard.get("require_snapshot")):
        violations.append(
            "risk.funding_guard.require_snapshot must be true"
        )
    funding_snapshot_age_ms = guard._positive_finite_value(
        funding_guard.get("max_snapshot_age_ms")
    )
    market_mark_age_ms = guard._positive_finite_value(
        market_freshness.get("max_mark_age_ms")
    )
    if (
        funding_snapshot_age_ms is None
        or funding_snapshot_age_ms > guard.MAX_CANARY_FUNDING_SNAPSHOT_AGE_MS
    ):
        violations.append(
            "risk.funding_guard.max_snapshot_age_ms must be positive and "
            f"no more than {guard.MAX_CANARY_FUNDING_SNAPSHOT_AGE_MS:g}"
        )
    elif (
        market_mark_age_ms is not None
        and funding_snapshot_age_ms > market_mark_age_ms
    ):
        violations.append(
            "risk.funding_guard.max_snapshot_age_ms must not exceed "
            "risk.market_data_freshness.max_mark_age_ms"
        )
    pre_funding_sec = guard._positive_finite_value(
        funding_guard.get("pre_funding_reduce_only_sec")
    )
    if (
        pre_funding_sec is None
        or pre_funding_sec < guard.MIN_CANARY_PRE_FUNDING_REDUCE_ONLY_SEC
    ):
        violations.append(
            "risk.funding_guard.pre_funding_reduce_only_sec must be at "
            f"least {guard.MIN_CANARY_PRE_FUNDING_REDUCE_ONLY_SEC:g}"
        )
    post_funding_sec = guard._positive_finite_value(
        funding_guard.get("post_funding_hold_sec")
    )
    if (
        post_funding_sec is None
        or post_funding_sec < guard.MIN_CANARY_POST_FUNDING_HOLD_SEC
    ):
        violations.append(
            "risk.funding_guard.post_funding_hold_sec must be at least "
            f"{guard.MIN_CANARY_POST_FUNDING_HOLD_SEC:g}"
        )
    max_abs_funding_rate = guard._positive_finite_value(
        funding_guard.get("max_abs_funding_rate")
    )
    if (
        max_abs_funding_rate is None
        or max_abs_funding_rate > guard.MAX_CANARY_ABS_FUNDING_RATE
    ):
        violations.append(
            "risk.funding_guard.max_abs_funding_rate must be positive and "
            f"no more than {guard.MAX_CANARY_ABS_FUNDING_RATE:g}"
        )
    max_funding_horizon_sec = guard._positive_finite_value(
        funding_guard.get("max_next_funding_horizon_sec")
    )
    if (
        max_funding_horizon_sec is None
        or max_funding_horizon_sec
        > guard.MAX_CANARY_NEXT_FUNDING_HORIZON_SEC
    ):
        violations.append(
            "risk.funding_guard.max_next_funding_horizon_sec must be "
            "positive and no more than "
            f"{guard.MAX_CANARY_NEXT_FUNDING_HORIZON_SEC:g}"
        )
    elif (
        pre_funding_sec is not None
        and max_funding_horizon_sec <= pre_funding_sec
    ):
        violations.append(
            "risk.funding_guard.max_next_funding_horizon_sec must exceed "
            "risk.funding_guard.pre_funding_reduce_only_sec"
        )
    funding_recovery_updates = funding_guard.get("recovery_updates")
    if (
        isinstance(funding_recovery_updates, bool)
        or not isinstance(funding_recovery_updates, int)
        or funding_recovery_updates < guard.MIN_CANARY_FUNDING_RECOVERY_UPDATES
    ):
        violations.append(
            "risk.funding_guard.recovery_updates must be an integer of at "
            f"least {guard.MIN_CANARY_FUNDING_RECOVERY_UPDATES}"
        )

    if not guard._enabled(supervisor.get("enabled")):
        violations.append("risk.independent_supervisor.enabled must be true")
    for field, allow_disabled_root in (
        ("max_account_gross_notional", False),
        ("max_daily_loss", False),
        ("max_drawdown_pct", True),
    ):
        parse_cap = (
            guard._nonnegative_finite_value
            if allow_disabled_root
            else guard._positive_finite_value
        )
        root_cap = parse_cap(
            limits.get(field, 0.0 if allow_disabled_root else None)
        )
        if root_cap is None:
            violations.append(
                f"risk.limits.{field} must be "
                + (
                    "nonnegative and finite"
                    if allow_disabled_root
                    else "positive and finite"
                )
            )
        supervisor_cap = parse_cap(supervisor.get(field, root_cap))
        if supervisor_cap is None:
            violations.append(
                f"risk.independent_supervisor.{field} must be "
                + (
                    "nonnegative and finite"
                    if allow_disabled_root
                    else "positive and finite"
                )
            )
        elif root_cap is not None and root_cap > 0.0 and supervisor_cap <= 0.0:
            violations.append(
                f"risk.independent_supervisor.{field} must be positive when "
                f"risk.limits.{field} is enabled"
            )
        elif (
            root_cap is not None
            and root_cap > 0.0
            and supervisor_cap > root_cap
        ):
            violations.append(
                f"risk.independent_supervisor.{field} must not exceed "
                f"risk.limits.{field}"
            )
    if not guard._enabled(supervisor.get("flatten_enabled")):
        violations.append(
            "risk.independent_supervisor.flatten_enabled must be true"
        )
    for field in (
        "daily_loss_enabled",
        "clock_sync_enabled",
        "liquidation_proximity_enabled",
        "require_liquidation_price",
    ):
        if not guard._enabled(supervisor.get(field)):
            violations.append(
                f"risk.independent_supervisor.{field} must be true"
            )
    legacy_state_fields = tuple(
        field
        for field in ("state_path", "state_required", "state_fsync")
        if field in supervisor
    )
    if legacy_state_fields:
        violations.append(
            "risk.independent_supervisor legacy state fields are unsupported: "
            + ",".join(legacy_state_fields)
        )
    supervisor_state_root = str(
        supervisor.get("state_store_root", "") or ""
    ).strip()
    if not supervisor_state_root:
        violations.append(
            "risk.independent_supervisor.state_store_root must be configured"
        )
    elif guard._uses_paper_state_path(supervisor_state_root):
        violations.append(
            "risk.independent_supervisor.state_store_root must not use Paper "
            "state"
        )
    if not str(supervisor.get("account_scope_id", "") or "").strip():
        violations.append(
            "risk.independent_supervisor.account_scope_id must be configured"
        )
    if not str(supervisor.get("state_genesis_id", "") or "").strip():
        violations.append(
            "risk.independent_supervisor.state_genesis_id must be configured"
        )
    cash_flow_deployment_start_ms = supervisor.get(
        "cash_flow_deployment_start_ms"
    )
    if (
        isinstance(cash_flow_deployment_start_ms, bool)
        or not isinstance(cash_flow_deployment_start_ms, int)
        or cash_flow_deployment_start_ms <= 0
    ):
        violations.append(
            "risk.independent_supervisor.cash_flow_deployment_start_ms must "
            "be a positive integer"
        )




def _validate_live_runtime_integrity_controls(
    config: dict,
    supervisor: Mapping,
    dead_man_switch: Mapping,
    oms: Mapping,
    writer_fence: Mapping,
    cash_flow: Mapping,
    heartbeat: Mapping,
    strategy_budget: Mapping,
    root_strategy: Mapping,
    limits: Mapping,
    *,
    config_base_dir: Path | None,
    violations: list[str],
    guard: ModuleType,
) -> None:
    primary_api_key = str(config.get("api_key", "") or "").strip()
    primary_api_secret = str(config.get("api_secret", "") or "").strip()
    supervisor_api_key = str(supervisor.get("api_key", "") or "").strip()
    supervisor_api_secret = str(
        supervisor.get("api_secret", "") or ""
    ).strip()
    if not primary_api_key or not primary_api_secret:
        violations.append("primary Binance API credentials must be configured")
    if not supervisor_api_key or not supervisor_api_secret:
        violations.append(
            "risk.independent_supervisor must use dedicated API credentials"
        )
    if (
        primary_api_key
        and supervisor_api_key
        and primary_api_key == supervisor_api_key
    ):
        violations.append(
            "risk.independent_supervisor.api_key must differ from the "
            "primary API key"
        )
    if (
        primary_api_secret
        and supervisor_api_secret
        and primary_api_secret == supervisor_api_secret
    ):
        violations.append(
            "risk.independent_supervisor.api_secret must differ from the "
            "primary API secret"
        )

    primary_key_env = str(config.get("api_key_env", "") or "").strip()
    primary_secret_env = str(config.get("api_secret_env", "") or "").strip()
    supervisor_key_env = str(
        supervisor.get("api_key_env", "") or ""
    ).strip()
    supervisor_secret_env = str(
        supervisor.get("api_secret_env", "") or ""
    ).strip()
    if not primary_key_env or not primary_secret_env:
        violations.append(
            "primary API credential environment variable names must be configured"
        )
    if not supervisor_key_env or not supervisor_secret_env:
        violations.append(
            "independent supervisor credential environment variable names "
            "must be configured"
        )
    if (
        primary_key_env
        and supervisor_key_env
        and primary_key_env == supervisor_key_env
    ):
        violations.append(
            "independent supervisor API key environment variable must differ "
            "from the primary"
        )
    if (
        primary_secret_env
        and supervisor_secret_env
        and primary_secret_env == supervisor_secret_env
    ):
        violations.append(
            "independent supervisor API secret environment variable must "
            "differ from the primary"
        )
    if not guard._enabled(dead_man_switch.get("enabled")):
        violations.append("oms.venue_dead_man_switch.enabled must be true")

    if not guard._enabled(oms.get("journal_enabled")):
        violations.append("oms.journal_enabled must be true")
    if not guard._enabled(oms.get("replay_journal_on_startup")):
        violations.append("oms.replay_journal_on_startup must be true")
    if not guard._enabled(oms.get("journal_fsync")):
        violations.append("oms.journal_fsync must be true")
    if not guard._enabled(oms.get("journal_integrity_check")):
        violations.append("oms.journal_integrity_check must be true")
    if not guard._enabled(writer_fence.get("enabled")):
        violations.append("oms.single_writer_fence.enabled must be true")

    journal_path = str(oms.get("journal_path", "") or "").strip()
    if not journal_path:
        violations.append("oms.journal_path must be configured")
    elif guard._uses_paper_state_path(journal_path):
        violations.append("oms.journal_path must not use Paper state")

    fence_path = writer_fence.get("path", "")
    if fence_path and guard._uses_paper_state_path(fence_path):
        violations.append(
            "oms.single_writer_fence.path must not use Paper state"
        )
    try:
        guard.validate_live_state_path_bindings(
            config,
            base_dir=config_base_dir,
        )
    except (TypeError, ValueError) as exc:
        violations.append(str(exc))

    if not guard._enabled(cash_flow.get("enabled")):
        violations.append("risk.cash_flow_truth.enabled must be true")
    if not guard._enabled(cash_flow.get("require_snapshot")):
        violations.append(
            "risk.cash_flow_truth.require_snapshot must be true"
        )
    if not guard._positive_finite(cash_flow.get("max_snapshot_age_sec")):
        violations.append(
            "risk.cash_flow_truth.max_snapshot_age_sec must be positive"
        )

    if not guard._enabled(heartbeat.get("enabled")):
        violations.append("risk.risk_control_heartbeat.enabled must be true")
    heartbeat_source = str(
        heartbeat.get("required_source", "") or ""
    ).strip()
    if heartbeat_source != guard.INDEPENDENT_SUPERVISOR_SOURCE:
        violations.append(
            "risk.risk_control_heartbeat.required_source must be "
            f"{guard.INDEPENDENT_SUPERVISOR_SOURCE!r}"
        )
    if not guard._positive_finite(heartbeat.get("max_age_sec")):
        violations.append(
            "risk.risk_control_heartbeat.max_age_sec must be positive"
        )

    if not guard._enabled(strategy_budget.get("enabled")):
        violations.append("risk.strategy_risk_budgets.enabled must be true")
    if not guard._enabled(strategy_budget.get("require_explicit_strategy")):
        violations.append(
            "risk.strategy_risk_budgets.require_explicit_strategy must be true"
        )
    primary_strategy_id = strategy_id_for_model(
        root_strategy.get("primary_model", root_strategy.get("name"))
    )
    configured_budgets = strategy_budget.get("budgets")
    if not isinstance(configured_budgets, Mapping):
        violations.append("risk.strategy_risk_budgets.budgets must be an object")
    else:
        configured_budget_ids = {
            str(strategy_id or "").strip()
            for strategy_id in configured_budgets
            if str(strategy_id or "").strip()
        }
        if configured_budget_ids != {primary_strategy_id}:
            violations.append(
                "risk.strategy_risk_budgets.budgets must contain exactly the "
                f"primary strategy ID {primary_strategy_id!r}"
            )
        primary_budget = configured_budgets.get(primary_strategy_id)
        if not isinstance(primary_budget, Mapping):
            violations.append(
                "risk.strategy_risk_budgets.budgets must configure the "
                f"primary strategy ID {primary_strategy_id!r}"
            )
        else:
            budget_symbol_cap = guard._positive_finite_value(
                primary_budget.get("max_symbol_notional")
            )
            budget_gross_cap = guard._positive_finite_value(
                primary_budget.get("max_gross_notional")
            )
            risk_symbol_cap = guard._positive_finite_value(
                limits.get("max_pos_notional")
            )
            risk_gross_cap = guard._positive_finite_value(
                limits.get("max_account_gross_notional")
            )
            if budget_symbol_cap is None or budget_gross_cap is None:
                violations.append(
                    "primary strategy risk budget caps must be positive and finite"
                )
            elif budget_symbol_cap > budget_gross_cap:
                violations.append(
                    "primary strategy max_symbol_notional must not exceed "
                    "max_gross_notional"
                )
            if (
                budget_symbol_cap is not None
                and risk_symbol_cap is not None
                and budget_symbol_cap > risk_symbol_cap
            ):
                violations.append(
                    "primary strategy max_symbol_notional must not exceed "
                    "risk.limits.max_pos_notional"
                )
            if (
                budget_gross_cap is not None
                and risk_gross_cap is not None
                and budget_gross_cap > risk_gross_cap
            ):
                violations.append(
                    "primary strategy max_gross_notional must not exceed "
                    "risk.limits.max_account_gross_notional"
                )




def _validate_live_canary_launch_config(
    config: dict,
    symbols: object,
    live_launch: Mapping,
    oms: Mapping,
    supervisor: Mapping,
    truth_monitor: Mapping,
    account: Mapping,
    limits: Mapping,
    root_strategy: Mapping,
    strategy: Mapping,
    *,
    config_path: str | Path | None,
    target_config_normalizer: Callable[[dict], Mapping] | None,
    now_utc: datetime | None,
    violations: list[str],
    guard: ModuleType,
) -> None:
    stage = guard.live_launch_stage(config)
    is_calibration_canary = stage == guard.RPI_CALIBRATION_CANARY_STAGE
    if stage not in guard.LIVE_CANARY_STAGES:
        violations.append(
            "live_launch.stage must be 'canary' or "
            f"{guard.RPI_CALIBRATION_CANARY_STAGE!r}"
        )
    deployment_id = str(
        live_launch.get("deployment_id", "") or ""
    ).strip()
    if not deployment_id:
        violations.append("live_launch.deployment_id must be configured")
    elif not guard._DEPLOYMENT_ID_RE.fullmatch(deployment_id):
        violations.append(
            "live_launch.deployment_id must be 6-128 path-safe characters"
        )
    elif any(
        token in deployment_id.upper()
        for token in guard._DEPLOYMENT_PLACEHOLDER_TOKENS
    ):
        violations.append(
            "live_launch.deployment_id must replace the EDIT-ME placeholder"
        )
    if is_calibration_canary:
        if config_path is None:
            violations.append(
                "rpi_calibration_canary runtime validation requires the "
                "exact config_path for independent permit revalidation"
            )
        elif not callable(target_config_normalizer):
            violations.append(
                "rpi_calibration_canary runtime validation requires an "
                "injected target config normalizer"
            )
        else:
            try:
                from infrastructure.rpi_calibration_permit import (
                    load_and_validate_rpi_calibration_permit,
                )

                revalidated_permit = (
                    load_and_validate_rpi_calibration_permit(
                        config,
                        config_path=config_path,
                        target_config_normalizer=target_config_normalizer,
                        now_utc=now_utc,
                    )
                )
            except Exception as exc:
                violations.append(
                    "rpi_calibration_canary independent permit "
                    f"revalidation failed: {exc}"
                )
            else:
                if (
                    config.get("_validated_rpi_calibration_permit")
                    != revalidated_permit
                ):
                    violations.append(
                        "rpi_calibration_canary caller permit wrapper does "
                        "not match the independently revalidated permit"
                    )
        for field in (
            "calibration_permit_path",
            "target_deployment_config_path",
        ):
            if not str(live_launch.get(field, "") or "").strip():
                violations.append(
                    f"rpi_calibration_canary live_launch.{field} must be "
                    "configured"
                )
        trusted_signers = live_launch.get(
            "calibration_permit_trusted_signers"
        )
        if not isinstance(trusted_signers, Mapping) or not trusted_signers:
            violations.append(
                "rpi_calibration_canary requires dedicated "
                "live_launch.calibration_permit_trusted_signers"
            )
        for field in guard.CALIBRATION_ACTIVE_ORDER_CAP_FIELDS:
            value = oms.get(field)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value != 1
            ):
                violations.append(
                    f"rpi_calibration_canary oms.{field} must be exactly 1"
                )
        supervisor_open_orders = supervisor.get("max_open_orders")
        if (
            isinstance(supervisor_open_orders, bool)
            or not isinstance(supervisor_open_orders, int)
            or supervisor_open_orders != 1
        ):
            violations.append(
                "rpi_calibration_canary "
                "risk.independent_supervisor.max_open_orders must be exactly 1"
            )
    else:
        if "_validated_rpi_calibration_permit" in config:
            violations.append(
                "_validated_rpi_calibration_permit is forbidden outside "
                "rpi_calibration_canary"
            )
        for field in guard.CALIBRATION_ACTIVE_ORDER_CAP_FIELDS:
            value = oms.get(field)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value != guard.MAX_CANARY_ACTIVE_ORDERS
            ):
                violations.append(
                    f"live canary oms.{field} must be exactly "
                    f"{guard.MAX_CANARY_ACTIVE_ORDERS}"
                )
        supervisor_open_orders = supervisor.get("max_open_orders")
        if (
            isinstance(supervisor_open_orders, bool)
            or not isinstance(supervisor_open_orders, int)
            or supervisor_open_orders != guard.MAX_CANARY_ACTIVE_ORDERS
        ):
            violations.append(
                "live canary risk.independent_supervisor.max_open_orders "
                f"must be exactly {guard.MAX_CANARY_ACTIVE_ORDERS}"
            )

    commission_poll_interval = guard._positive_finite_value(
        truth_monitor.get("rpi_commission_poll_interval_sec")
    )
    if (
        commission_poll_interval is None
        or commission_poll_interval
        < guard.MIN_RPI_COMMISSION_POLL_INTERVAL_SEC
        or commission_poll_interval
        > guard.MAX_RPI_COMMISSION_POLL_INTERVAL_SEC
    ):
        violations.append(
            "oms.truth_monitor.rpi_commission_poll_interval_sec must be "
            f"between {guard.MIN_RPI_COMMISSION_POLL_INTERVAL_SEC:g} and "
            f"{guard.MAX_RPI_COMMISSION_POLL_INTERVAL_SEC:g}"
        )
    commission_halt_threshold = truth_monitor.get(
        "rpi_commission_halt_threshold"
    )
    if (
        isinstance(commission_halt_threshold, bool)
        or not isinstance(commission_halt_threshold, int)
        or not 1
        <= commission_halt_threshold
        <= guard.MAX_RPI_COMMISSION_HALT_THRESHOLD
    ):
        violations.append(
            "oms.truth_monitor.rpi_commission_halt_threshold must be "
            f"an integer between 1 and {guard.MAX_RPI_COMMISSION_HALT_THRESHOLD}"
        )
    commission_clean_polls = truth_monitor.get(
        "rpi_commission_clean_polls_to_clear"
    )
    if (
        isinstance(commission_clean_polls, bool)
        or not isinstance(commission_clean_polls, int)
        or commission_clean_polls != guard.RPI_COMMISSION_CLEAN_POLLS_TO_CLEAR
    ):
        violations.append(
            "oms.truth_monitor.rpi_commission_clean_polls_to_clear must "
            f"be exactly {guard.RPI_COMMISSION_CLEAN_POLLS_TO_CLEAR}"
        )
    reduce_only_fraction = guard._positive_finite_value(
        live_launch.get("deployment_loss_reduce_only_fraction")
    )
    if reduce_only_fraction is None or reduce_only_fraction >= 1.0:
        violations.append(
            "live_launch.deployment_loss_reduce_only_fraction must be "
            "greater than 0 and less than 1"
        )

    if (
        not isinstance(symbols, (list, tuple))
        or len(symbols) != 1
        or not str(symbols[0] or "").strip()
    ):
        violations.append(
            "live_launch canary requires exactly one configured symbol"
        )

    margin_type = str(account.get("margin_type", "") or "").strip().upper()
    if margin_type != "ISOLATED":
        violations.append(
            "live_launch canary requires account.margin_type='ISOLATED'"
        )
    leverage = guard._positive_finite_value(account.get("leverage"))
    if leverage != 1.0:
        violations.append(
            "live_launch canary requires account.leverage=1"
        )
    configuration_mode = str(
        account.get("configuration_mode", "") or ""
    ).strip().upper()
    if configuration_mode != "VERIFY_ONLY":
        violations.append(
            "live_launch canary requires "
            "account.configuration_mode='VERIFY_ONLY'"
        )

    declared_equity = guard._positive_finite_value(
        live_launch.get("declared_account_equity_usdt")
    )
    deployed_cap = guard._positive_finite_value(
        live_launch.get("max_deployed_capital_usdt")
    )
    deployment_loss_cap = guard._positive_finite_value(
        live_launch.get("max_deployment_loss_usdt")
    )
    max_deployed_equity_fraction = guard._max_deployed_equity_fraction(config)
    if declared_equity is None:
        violations.append(
            "live_launch.declared_account_equity_usdt must be positive and finite"
        )
    if deployed_cap is None:
        violations.append(
            "live_launch.max_deployed_capital_usdt must be positive and finite"
        )
    elif declared_equity is not None and deployed_cap > declared_equity:
        violations.append(
            "live_launch.max_deployed_capital_usdt must not exceed "
            "live_launch.declared_account_equity_usdt"
        )
    elif (
        declared_equity is not None
        and deployed_cap
        > declared_equity * max_deployed_equity_fraction
    ):
        violations.append(
            "live_launch.max_deployed_capital_usdt must not exceed "
            f"{guard._deployed_equity_fraction_label(config)} of declared account "
            "equity"
        )
    elif (
        not is_calibration_canary
        and deployed_cap > guard.MAX_CANARY_DEPLOYED_CAPITAL_USDT
    ):
        violations.append(
            "live_launch.max_deployed_capital_usdt must not exceed "
            f"{guard.MAX_CANARY_DEPLOYED_CAPITAL_USDT:g} USDT for a canary"
        )
    if deployment_loss_cap is None:
        violations.append(
            "live_launch.max_deployment_loss_usdt must be positive and finite"
        )
    elif deployed_cap is not None and deployment_loss_cap > deployed_cap:
        violations.append(
            "live_launch.max_deployment_loss_usdt must not exceed "
            "live_launch.max_deployed_capital_usdt"
        )
    elif (
        deployed_cap is not None
        and deployment_loss_cap
        > deployed_cap * guard.MAX_CANARY_DEPLOYMENT_LOSS_FRACTION
    ):
        violations.append(
            "live_launch.max_deployment_loss_usdt must not exceed "
            f"{guard.MAX_CANARY_DEPLOYMENT_LOSS_FRACTION:.0%} of deployed capital"
        )
    elif (
        not is_calibration_canary
        and deployment_loss_cap > guard.MAX_CANARY_DEPLOYMENT_LOSS_USDT
    ):
        violations.append(
            "live_launch.max_deployment_loss_usdt must not exceed "
            f"{guard.MAX_CANARY_DEPLOYMENT_LOSS_USDT:g} USDT for a canary"
        )
    if (
        is_calibration_canary
        and deployment_loss_cap is not None
        and deployment_loss_cap > guard.MAX_CALIBRATION_DEPLOYMENT_LOSS_USDT
    ):
        violations.append(
            "rpi_calibration_canary "
            "live_launch.max_deployment_loss_usdt must not exceed "
            f"{guard.MAX_CALIBRATION_DEPLOYMENT_LOSS_USDT:g}"
        )

    guard._append_cap_violation(
        violations,
        field="account.trading_budget_total",
        value=account.get("trading_budget_total"),
        cap_field="live_launch.max_deployed_capital_usdt",
        cap_value=deployed_cap,
    )
    budget_by_asset = account.get("trading_budget_by_asset")
    if not isinstance(budget_by_asset, Mapping) or not budget_by_asset:
        violations.append(
            "account.trading_budget_by_asset must declare the canary budget"
        )
    else:
        parsed_asset_budgets = [
            guard._positive_finite_value(value)
            for value in budget_by_asset.values()
        ]
        if any(value is None for value in parsed_asset_budgets):
            violations.append(
                "account.trading_budget_by_asset values must be positive and finite"
            )
        else:
            asset_budget_total = sum(parsed_asset_budgets)
            if deployed_cap is not None and asset_budget_total > deployed_cap:
                violations.append(
                    "account.trading_budget_by_asset total exceeds "
                    "live_launch.max_deployed_capital_usdt"
                )

    canary_limit_fractions = {
        "max_order_notional": guard.MAX_CANARY_ORDER_FRACTION,
        "max_pos_notional": guard.MAX_CANARY_POSITION_FRACTION,
        "max_account_gross_notional": guard.MAX_CANARY_GROSS_FRACTION,
    }
    absolute_canary_caps = {
        "max_order_notional": (
            guard.MAX_CALIBRATION_ORDER_NOTIONAL_USDT
            if is_calibration_canary
            else guard.MAX_CANARY_ORDER_NOTIONAL_USDT
        ),
        "max_pos_notional": (
            guard.MAX_CALIBRATION_POSITION_NOTIONAL_USDT
            if is_calibration_canary
            else guard.MAX_CANARY_POSITION_NOTIONAL_USDT
        ),
        "max_account_gross_notional": (
            guard.MAX_CALIBRATION_GROSS_NOTIONAL_USDT
            if is_calibration_canary
            else guard.MAX_CANARY_GROSS_NOTIONAL_USDT
        ),
    }
    for field, canary_fraction in canary_limit_fractions.items():
        guard._append_cap_violation(
            violations,
            field=f"risk.limits.{field}",
            value=limits.get(field),
            cap_field="live_launch.max_deployed_capital_usdt",
            cap_value=deployed_cap,
        )
        parsed_limit = guard._positive_finite_value(limits.get(field))
        if (
            parsed_limit is not None
            and parsed_limit > absolute_canary_caps[field]
        ):
            stage_label = (
                "rpi_calibration_canary"
                if is_calibration_canary
                else "live canary"
            )
            violations.append(
                f"{stage_label} risk.limits.{field} must not exceed "
                f"{absolute_canary_caps[field]:g} USDT"
            )
        elif (
            not is_calibration_canary
            and parsed_limit is not None
            and deployed_cap is not None
            and parsed_limit > deployed_cap * canary_fraction
        ):
            violations.append(
                f"risk.limits.{field} must not exceed "
                f"{canary_fraction:.0%} of deployed capital"
            )
    guard._append_cap_violation(
        violations,
        field="risk.limits.max_daily_loss",
        value=limits.get("max_daily_loss"),
        cap_field="live_launch.max_deployment_loss_usdt",
        cap_value=deployment_loss_cap,
    )
    max_daily_loss = guard._positive_finite_value(limits.get("max_daily_loss"))
    if (
        max_daily_loss is not None
        and deployment_loss_cap is not None
        and max_daily_loss
        > deployment_loss_cap * guard.MAX_CANARY_DAILY_LOSS_FRACTION
    ):
        violations.append(
            "risk.limits.max_daily_loss must not exceed "
            f"{guard.MAX_CANARY_DAILY_LOSS_FRACTION:.0%} of the deployment loss cap"
        )
    if (
        is_calibration_canary
        and max_daily_loss is not None
        and max_daily_loss > guard.MAX_CALIBRATION_DAILY_LOSS_USDT
    ):
        violations.append(
            "rpi_calibration_canary risk.limits.max_daily_loss must not "
            f"exceed {guard.MAX_CALIBRATION_DAILY_LOSS_USDT:g} USDT"
        )
    if (
        not is_calibration_canary
        and max_daily_loss is not None
        and max_daily_loss > guard.MAX_CANARY_DAILY_LOSS_USDT
    ):
        violations.append(
            "live canary risk.limits.max_daily_loss must not exceed "
            f"{guard.MAX_CANARY_DAILY_LOSS_USDT:g} USDT"
        )

    _validate_live_canary_strategy_config(
        config,
        root_strategy,
        strategy,
        live_launch,
        limits,
        is_calibration_canary=is_calibration_canary,
        deployment_id=deployment_id,
        deployed_cap=deployed_cap,
        deployment_loss_cap=deployment_loss_cap,
        violations=violations,
        guard=guard,
    )



# Domain validators are appended below.
