"""Immutable configuration policy for the independent risk sidecar."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from risk.deployment_loss import deployment_policy_fingerprint
from risk.funding_guard import FundingGuardPolicy


@dataclass(frozen=True, slots=True)
class RiskSidecarPolicy:
    symbols: tuple[str, ...]
    funding_guard_policy: FundingGuardPolicy
    parent_heartbeat_timeout_sec: float
    exchange_poll_interval_sec: float
    exchange_max_age_sec: float
    snapshot_worker_timeout_sec: float
    rearm_snapshot_max_age_sec: float
    cancel_retry_sec: float
    orphan_exit_sec: float
    emergency_countdown_time_ms: int
    max_account_gross_notional: float
    gross_kill_multiplier: float
    margin_reduce_only_ratio: float
    margin_kill_ratio: float
    max_open_orders: int
    daily_loss_enabled: bool
    max_daily_loss: float
    max_drawdown_pct: float
    daily_loss_reduce_only_fraction: float
    deployment_id: str
    declared_account_equity: float
    max_deployed_capital: float
    max_deployment_loss: float
    deployment_loss_reduce_only_fraction: float
    deployment_policy_fingerprint: str
    account_key_fingerprint: str
    clock_sync_enabled: bool
    clock_reduce_only_phase_error_ms: float
    clock_kill_phase_error_ms: float
    clock_max_rtt_ms: float
    clock_max_uncertainty_ms: float
    clock_max_offset_dispersion_ms: float
    liquidation_proximity_enabled: bool
    require_liquidation_price: bool
    liquidation_reduce_only_distance_pct: float
    liquidation_kill_distance_pct: float
    flatten_enabled: bool
    parent_loss_flatten_delay_sec: float
    flatten_retry_sec: float
    flat_verification_checks: int

    @property
    def clock_reduce_only_offset_ms(self) -> float:
        return self.clock_reduce_only_phase_error_ms

    @property
    def clock_kill_offset_ms(self) -> float:
        return self.clock_kill_phase_error_ms

    @classmethod
    def from_settings(
        cls,
        settings: dict,
        finite_float: Callable[[object, str], float],
    ) -> RiskSidecarPolicy:
        symbols = tuple(
            sorted(
                {
                    str(symbol or "").upper()
                    for symbol in settings.get("symbols", [])
                    if str(symbol or "").strip()
                }
            )
        )
        funding_guard = settings.get("funding_guard", {})
        if not isinstance(funding_guard, dict):
            raise ValueError("funding_guard settings must be an object")
        funding_guard_policy = FundingGuardPolicy(
            enabled=funding_guard.get("enabled", False),
            require_snapshot=funding_guard.get(
                "require_snapshot",
                funding_guard.get("enabled", False),
            ),
            max_snapshot_age_ms=funding_guard.get(
                "max_snapshot_age_ms",
                3_000.0,
            ),
            pre_funding_reduce_only_sec=funding_guard.get(
                "pre_funding_reduce_only_sec",
                600.0,
            ),
            post_funding_hold_sec=funding_guard.get(
                "post_funding_hold_sec",
                120.0,
            ),
            max_abs_funding_rate=funding_guard.get(
                "max_abs_funding_rate",
                0.0005,
            ),
            max_next_funding_horizon_sec=funding_guard.get(
                "max_next_funding_horizon_sec",
                32_400.0,
            ),
            recovery_updates=funding_guard.get("recovery_updates", 5),
        )
        parent_heartbeat_timeout_sec = max(
            0.1,
            finite_float(
                settings.get("parent_heartbeat_timeout_sec", 1.5) or 1.5,
                "parent_heartbeat_timeout_sec",
            ),
        )
        configured_exchange_poll_interval_sec = max(
            0.1,
            finite_float(
                settings.get("exchange_poll_interval_sec", 5.0) or 5.0,
                "exchange_poll_interval_sec",
            ),
        )
        funding_poll_ceiling_sec = max(
            0.1,
            funding_guard_policy.max_snapshot_age_ms / 2_000.0,
        )
        exchange_poll_interval_sec = (
            min(
                configured_exchange_poll_interval_sec,
                funding_poll_ceiling_sec,
            )
            if funding_guard_policy.enabled
            else configured_exchange_poll_interval_sec
        )
        exchange_max_age_sec = max(
            exchange_poll_interval_sec,
            finite_float(
                settings.get("exchange_max_age_sec", 10.0) or 10.0,
                "exchange_max_age_sec",
            ),
        )
        snapshot_worker_timeout_sec = min(
            exchange_max_age_sec,
            max(
                0.1,
                finite_float(
                    settings.get(
                        "snapshot_worker_timeout_sec",
                        exchange_max_age_sec,
                    )
                    or exchange_max_age_sec,
                    "snapshot_worker_timeout_sec",
                ),
            ),
        )
        rearm_snapshot_max_age_sec = min(
            exchange_max_age_sec,
            max(
                0.1,
                finite_float(
                    settings.get(
                        "rearm_snapshot_max_age_sec",
                        min(1.0, exchange_poll_interval_sec),
                    )
                    or min(1.0, exchange_poll_interval_sec),
                    "rearm_snapshot_max_age_sec",
                ),
            ),
        )
        cancel_retry_sec = max(
            0.1,
            finite_float(
                settings.get("cancel_retry_sec", 2.0) or 2.0,
                "cancel_retry_sec",
            ),
        )
        orphan_exit_sec = max(
            parent_heartbeat_timeout_sec,
            finite_float(
                settings.get("orphan_exit_sec", 30.0) or 30.0,
                "orphan_exit_sec",
            ),
        )
        max_account_gross_notional = max(
            0.0,
            finite_float(
                settings.get("max_account_gross_notional", 0.0) or 0.0,
                "max_account_gross_notional",
            ),
        )
        gross_kill_multiplier = max(
            1.0,
            finite_float(
                settings.get("gross_kill_multiplier", 1.25) or 1.25,
                "gross_kill_multiplier",
            ),
        )
        margin_reduce_only_ratio = max(
            0.0,
            finite_float(
                settings.get("margin_reduce_only_ratio", 0.70),
                "margin_reduce_only_ratio",
            ),
        )
        margin_kill_ratio = max(
            margin_reduce_only_ratio,
            finite_float(
                settings.get("margin_kill_ratio", 0.90),
                "margin_kill_ratio",
            ),
        )
        daily_loss_reduce_only_fraction = min(
            1.0,
            max(
                0.0,
                finite_float(
                    settings.get("daily_loss_reduce_only_fraction", 0.80),
                    "daily_loss_reduce_only_fraction",
                ),
            ),
        )
        deployment_id = str(
            settings.get("deployment_id", "") or ""
        ).strip()
        declared_account_equity = max(
            0.0,
            finite_float(
                settings.get("declared_account_equity_usdt", 0.0) or 0.0,
                "declared_account_equity_usdt",
            ),
        )
        max_deployed_capital = max(
            0.0,
            finite_float(
                settings.get("max_deployed_capital_usdt", 0.0) or 0.0,
                "max_deployed_capital_usdt",
            ),
        )
        max_deployment_loss = max(
            0.0,
            finite_float(
                settings.get("max_deployment_loss_usdt", 0.0) or 0.0,
                "max_deployment_loss_usdt",
            ),
        )
        deployment_loss_reduce_only_fraction = min(
            1.0,
            max(
                0.0,
                finite_float(
                    settings.get(
                        "deployment_loss_reduce_only_fraction",
                        0.80,
                    )
                    or 0.0,
                    "deployment_loss_reduce_only_fraction",
                ),
            ),
        )
        reduce_only_phase_setting = settings.get(
            "clock_reduce_only_phase_error_ms"
        )
        reduce_only_phase_key = "clock_reduce_only_phase_error_ms"
        if reduce_only_phase_setting is None:
            reduce_only_phase_setting = settings.get(
                "clock_reduce_only_offset_ms",
                25.0,
            )
            reduce_only_phase_key = "clock_reduce_only_offset_ms"
        clock_reduce_only_phase_error_ms = max(
            0.0,
            finite_float(
                reduce_only_phase_setting or 0.0,
                reduce_only_phase_key,
            ),
        )
        kill_phase_setting = settings.get("clock_kill_phase_error_ms")
        kill_phase_key = "clock_kill_phase_error_ms"
        if kill_phase_setting is None:
            kill_phase_setting = settings.get(
                "clock_kill_offset_ms",
                100.0,
            )
            kill_phase_key = "clock_kill_offset_ms"
        clock_kill_phase_error_ms = max(
            clock_reduce_only_phase_error_ms,
            finite_float(
                kill_phase_setting or 0.0,
                kill_phase_key,
            ),
        )
        liquidation_reduce_only_distance_pct = max(
            0.0,
            finite_float(
                settings.get(
                    "liquidation_reduce_only_distance_pct",
                    0.05,
                )
                or 0.0,
                "liquidation_reduce_only_distance_pct",
            ),
        )
        liquidation_kill_distance_pct = min(
            liquidation_reduce_only_distance_pct,
            max(
                0.0,
                finite_float(
                    settings.get(
                        "liquidation_kill_distance_pct",
                        0.02,
                    )
                    or 0.0,
                    "liquidation_kill_distance_pct",
                ),
            ),
        )
        policy_fingerprint = deployment_policy_fingerprint(
            deployment_id=deployment_id,
            symbols=settings.get("symbols", []),
            declared_account_equity=declared_account_equity,
            max_deployed_capital=max_deployed_capital,
            maximum_loss=max_deployment_loss,
            reduce_only_fraction=deployment_loss_reduce_only_fraction,
        )
        return cls(
            symbols=symbols,
            funding_guard_policy=funding_guard_policy,
            parent_heartbeat_timeout_sec=parent_heartbeat_timeout_sec,
            exchange_poll_interval_sec=exchange_poll_interval_sec,
            exchange_max_age_sec=exchange_max_age_sec,
            snapshot_worker_timeout_sec=snapshot_worker_timeout_sec,
            rearm_snapshot_max_age_sec=rearm_snapshot_max_age_sec,
            cancel_retry_sec=cancel_retry_sec,
            orphan_exit_sec=orphan_exit_sec,
            emergency_countdown_time_ms=max(
                1,
                int(
                    settings.get("emergency_countdown_time_ms", 1000)
                    or 1000
                ),
            ),
            max_account_gross_notional=max_account_gross_notional,
            gross_kill_multiplier=gross_kill_multiplier,
            margin_reduce_only_ratio=margin_reduce_only_ratio,
            margin_kill_ratio=margin_kill_ratio,
            max_open_orders=max(
                0,
                int(settings.get("max_open_orders", 0) or 0),
            ),
            daily_loss_enabled=bool(
                settings.get("daily_loss_enabled", False)
            ),
            max_daily_loss=max(
                0.0,
                finite_float(
                    settings.get("max_daily_loss", 0.0) or 0.0,
                    "max_daily_loss",
                ),
            ),
            max_drawdown_pct=max(
                0.0,
                finite_float(
                    settings.get("max_drawdown_pct", 0.0) or 0.0,
                    "max_drawdown_pct",
                ),
            ),
            daily_loss_reduce_only_fraction=(
                daily_loss_reduce_only_fraction
            ),
            deployment_id=deployment_id,
            declared_account_equity=declared_account_equity,
            max_deployed_capital=max_deployed_capital,
            max_deployment_loss=max_deployment_loss,
            deployment_loss_reduce_only_fraction=(
                deployment_loss_reduce_only_fraction
            ),
            deployment_policy_fingerprint=policy_fingerprint,
            account_key_fingerprint=str(
                settings.get("account_key_fingerprint", "") or ""
            ).strip(),
            clock_sync_enabled=bool(
                settings.get("clock_sync_enabled", False)
            ),
            clock_reduce_only_phase_error_ms=(
                clock_reduce_only_phase_error_ms
            ),
            clock_kill_phase_error_ms=clock_kill_phase_error_ms,
            clock_max_rtt_ms=max(
                0.0,
                finite_float(
                    settings.get("clock_max_rtt_ms", 200.0) or 0.0,
                    "clock_max_rtt_ms",
                ),
            ),
            clock_max_uncertainty_ms=max(
                0.0,
                finite_float(
                    settings.get("clock_max_uncertainty_ms", 50.0) or 0.0,
                    "clock_max_uncertainty_ms",
                ),
            ),
            clock_max_offset_dispersion_ms=max(
                0.0,
                finite_float(
                    settings.get(
                        "clock_max_offset_dispersion_ms",
                        10.0,
                    )
                    or 0.0,
                    "clock_max_offset_dispersion_ms",
                ),
            ),
            liquidation_proximity_enabled=bool(
                settings.get("liquidation_proximity_enabled", False)
            ),
            require_liquidation_price=bool(
                settings.get("require_liquidation_price", True)
            ),
            liquidation_reduce_only_distance_pct=(
                liquidation_reduce_only_distance_pct
            ),
            liquidation_kill_distance_pct=liquidation_kill_distance_pct,
            flatten_enabled=bool(settings.get("flatten_enabled", True)),
            parent_loss_flatten_delay_sec=max(
                0.0,
                finite_float(
                    settings.get("parent_loss_flatten_delay_sec", 3.0)
                    or 0.0,
                    "parent_loss_flatten_delay_sec",
                ),
            ),
            flatten_retry_sec=max(
                0.1,
                finite_float(
                    settings.get("flatten_retry_sec", 2.0) or 2.0,
                    "flatten_retry_sec",
                ),
            ),
            flat_verification_checks=max(
                1,
                int(settings.get("flat_verification_checks", 2) or 2),
            ),
        )
