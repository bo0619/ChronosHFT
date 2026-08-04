"""Parent-side configuration assembly for the independent risk sidecar."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable

from risk.limit_contract import (
    DEFAULT_MAX_ACCOUNT_GROSS_NOTIONAL,
    DEFAULT_MAX_DAILY_LOSS,
    DEFAULT_MAX_DRAWDOWN_PCT,
)


def _bounded_risk_cap(
    *,
    field: str,
    limits: dict,
    supervisor: dict,
    enabled: bool,
    root_default: float,
    finite_float: Callable[[object, str], float],
) -> float:
    root_label = f"risk.limits.{field}"
    supervisor_label = f"risk.independent_supervisor.{field}"
    root_value = finite_float(limits.get(field, root_default), root_label)
    value = finite_float(supervisor.get(field, root_value), supervisor_label)
    if not enabled:
        return value
    if root_value < 0.0:
        raise ValueError(f"{root_label} must be nonnegative")
    if value < 0.0 or (value == 0.0 and root_value > 0.0):
        raise ValueError(f"{supervisor_label} must be positive")
    if root_value > 0.0 and value > root_value:
        raise ValueError(
            f"{supervisor_label} must not exceed {root_label}"
        )
    return value


@dataclass(frozen=True, slots=True)
class SidecarSupervisorConfiguration:
    supervisor_config: dict
    enabled: bool
    heartbeat_interval_sec: float
    status_max_age_sec: float
    stop_timeout_sec: float
    control_enqueue_timeout_sec: float
    recovery_checks: int
    recovery_snapshot_max_age_sec: float
    rearm_command_timeout_sec: float
    child_settings: dict

    @classmethod
    def from_root(
        cls,
        config: dict,
        risk_manager,
        finite_float: Callable[[object, str], float],
        required_heartbeat_source: str,
    ) -> SidecarSupervisorConfiguration:
        risk_config = config.get("risk", {}) or {}
        supervisor_config = dict(
            risk_config.get("independent_supervisor", {}) or {}
        )
        enabled = bool(supervisor_config.get("enabled", False))
        heartbeat_config = dict(
            risk_config.get("risk_control_heartbeat", {}) or {}
        )
        if enabled and not bool(heartbeat_config.get("enabled", False)):
            raise ValueError(
                "independent_supervisor requires "
                "risk_control_heartbeat.enabled"
            )
        configured_source = str(
            heartbeat_config.get("required_source", "") or ""
        ).strip()
        if enabled and configured_source != required_heartbeat_source:
            raise ValueError(
                "independent_supervisor requires risk_control_heartbeat."
                f"required_source={required_heartbeat_source!r}"
            )
        if enabled and (
            not str(supervisor_config.get("api_key", "") or "")
            or not str(supervisor_config.get("api_secret", "") or "")
        ):
            raise ValueError(
                "independent_supervisor requires dedicated API credentials"
            )

        heartbeat_interval_sec = max(
            0.05,
            finite_float(
                supervisor_config.get("heartbeat_interval_sec", 0.25)
                or 0.25,
                "heartbeat_interval_sec",
            ),
        )
        status_max_age_sec = max(
            heartbeat_interval_sec,
            finite_float(
                supervisor_config.get("status_max_age_sec", 2.0) or 2.0,
                "status_max_age_sec",
            ),
        )
        stop_timeout_sec = max(
            0.5,
            finite_float(
                supervisor_config.get("stop_timeout_sec", 10.0) or 10.0,
                "stop_timeout_sec",
            ),
        )
        control_enqueue_timeout_sec = max(
            0.01,
            finite_float(
                supervisor_config.get("control_enqueue_timeout_sec", 0.5)
                or 0.5,
                "control_enqueue_timeout_sec",
            ),
        )
        recovery_checks = max(
            1,
            int(supervisor_config.get("recovery_checks", 2) or 2),
        )
        recovery_snapshot_max_age_sec = max(
            heartbeat_interval_sec,
            finite_float(
                supervisor_config.get("exchange_max_age_sec", 10.0)
                or 10.0,
                "exchange_max_age_sec",
            ),
        )
        rearm_command_timeout_sec = max(
            0.5,
            finite_float(
                supervisor_config.get("rearm_command_timeout_sec", 5.0)
                or 5.0,
                "rearm_command_timeout_sec",
            ),
        )

        limits_config = dict(risk_config.get("limits", {}) or {})
        margin_config = dict(risk_config.get("margin_health", {}) or {})
        cash_flow_config = dict(
            risk_config.get("cash_flow_truth", {}) or {}
        )
        rest_rate_limit_config = dict(
            config.get("system", {}).get(
                "binance_rest_rate_limit",
                {},
            )
            or {}
        )
        funding_guard_config = dict(
            risk_config.get("funding_guard", {}) or {}
        )
        live_launch_config = dict(config.get("live_launch", {}) or {})
        oms_config = dict(config.get("oms", {}) or {})
        max_account_gross_notional = _bounded_risk_cap(
            field="max_account_gross_notional",
            limits=limits_config,
            supervisor=supervisor_config,
            enabled=enabled,
            root_default=getattr(
                risk_manager,
                "max_account_gross_notional",
                DEFAULT_MAX_ACCOUNT_GROSS_NOTIONAL,
            ),
            finite_float=finite_float,
        )
        max_daily_loss = _bounded_risk_cap(
            field="max_daily_loss",
            limits=limits_config,
            supervisor=supervisor_config,
            enabled=enabled,
            root_default=getattr(
                risk_manager,
                "max_daily_loss",
                DEFAULT_MAX_DAILY_LOSS,
            ),
            finite_float=finite_float,
        )
        max_drawdown_pct = _bounded_risk_cap(
            field="max_drawdown_pct",
            limits=limits_config,
            supervisor=supervisor_config,
            enabled=enabled,
            root_default=getattr(
                risk_manager,
                "max_drawdown_pct",
                DEFAULT_MAX_DRAWDOWN_PCT,
            ),
            finite_float=finite_float,
        )
        child_settings = {
            **supervisor_config,
            "api_key": str(supervisor_config.get("api_key", "") or ""),
            "api_secret": str(
                supervisor_config.get("api_secret", "") or ""
            ),
            "testnet": bool(config.get("testnet", False)),
            "symbols": list(config.get("symbols", [])),
            "rest_rate_limit": rest_rate_limit_config,
            "full_open_orders_audit_interval_sec": float(
                rest_rate_limit_config.get(
                    "full_open_orders_audit_interval_sec",
                    60.0,
                )
                or 60.0
            ),
            "funding_guard": funding_guard_config,
            "emergency_countdown_time_ms": int(
                supervisor_config.get(
                    "emergency_countdown_time_ms",
                    1000,
                )
                or 1000
            ),
            "max_account_gross_notional": max_account_gross_notional,
            "margin_reduce_only_ratio": float(
                supervisor_config.get(
                    "margin_reduce_only_ratio",
                    margin_config.get("reduce_only_ratio", 0.70),
                )
                or 0.0
            ),
            "margin_kill_ratio": float(
                supervisor_config.get(
                    "margin_kill_ratio",
                    margin_config.get("kill_ratio", 0.90),
                )
                or 0.0
            ),
            "max_open_orders": int(
                supervisor_config.get(
                    "max_open_orders",
                    oms_config.get("max_total_active_orders", 100),
                )
                or 0
            ),
            "max_daily_loss": max_daily_loss,
            "max_drawdown_pct": max_drawdown_pct,
            "deployment_id": str(
                live_launch_config.get("deployment_id", "") or ""
            ),
            "declared_account_equity_usdt": float(
                live_launch_config.get(
                    "declared_account_equity_usdt",
                    0.0,
                )
                or 0.0
            ),
            "max_deployed_capital_usdt": float(
                live_launch_config.get(
                    "max_deployed_capital_usdt",
                    0.0,
                )
                or 0.0
            ),
            "max_deployment_loss_usdt": float(
                live_launch_config.get(
                    "max_deployment_loss_usdt",
                    0.0,
                )
                or 0.0
            ),
            "deployment_loss_reduce_only_fraction": float(
                live_launch_config.get(
                    "deployment_loss_reduce_only_fraction",
                    0.80,
                )
                or 0.0
            ),
            "cash_flow_income_types": list(
                supervisor_config.get(
                    "cash_flow_income_types",
                    cash_flow_config.get(
                        "external_income_types",
                        ["TRANSFER"],
                    ),
                )
                or []
            ),
            "cash_flow_poll_interval_sec": float(
                cash_flow_config.get("poll_interval_sec", 30.0) or 30.0
            ),
            "seed_risk_day": str(
                getattr(risk_manager, "risk_day", "") or ""
            ),
            "seed_day_start_equity": float(
                getattr(risk_manager, "initial_equity", 0.0) or 0.0
            ),
            "seed_external_cash_flow_total": float(
                getattr(
                    risk_manager,
                    "initial_external_cash_flow_total",
                    0.0,
                )
                or 0.0
            ),
            "seed_peak_adjusted_equity": float(
                getattr(risk_manager, "peak_equity", 0.0) or 0.0
            ),
            "seed_last_equity": float(
                getattr(risk_manager, "last_equity", 0.0) or 0.0
            ),
            "seed_deployment_start_equity": float(
                getattr(
                    risk_manager,
                    "deployment_start_equity",
                    0.0,
                )
                or 0.0
            ),
            "seed_deployment_external_cash_flow_total": float(
                getattr(
                    risk_manager,
                    "deployment_start_external_cash_flow_total",
                    0.0,
                )
                or 0.0
            ),
            "seed_deployment_adjusted_equity": float(
                getattr(
                    risk_manager,
                    "deployment_adjusted_equity",
                    0.0,
                )
                or 0.0
            ),
            "seed_deployment_loss": float(
                getattr(risk_manager, "deployment_loss", 0.0) or 0.0
            ),
        }
        api_key = str(child_settings.get("api_key", "") or "")
        child_settings["account_key_fingerprint"] = (
            hashlib.sha256(api_key.encode("utf-8")).hexdigest()
            if api_key
            else ""
        )
        return cls(
            supervisor_config=supervisor_config,
            enabled=enabled,
            heartbeat_interval_sec=heartbeat_interval_sec,
            status_max_age_sec=status_max_age_sec,
            stop_timeout_sec=stop_timeout_sec,
            control_enqueue_timeout_sec=control_enqueue_timeout_sec,
            recovery_checks=recovery_checks,
            recovery_snapshot_max_age_sec=recovery_snapshot_max_age_sec,
            rearm_command_timeout_sec=rearm_command_timeout_sec,
            child_settings=child_settings,
        )
