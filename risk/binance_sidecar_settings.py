"""Configuration policy for the Binance risk-sidecar exchange facade."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True, slots=True)
class BinanceSidecarExchangeConfiguration:
    rate_limit_settings: dict
    symbols: tuple[str, ...]
    funding_guard_enabled: bool
    funding_max_source_age_ms: float
    daily_loss_enabled: bool
    cash_flow_income_types: frozenset[str]
    cash_flow_assets: frozenset[str]
    cash_flow_max_pages: int
    cash_flow_poll_interval_sec: float
    full_open_orders_audit_interval_sec: float
    clock_sync_enabled: bool
    clock_sync_interval_sec: float
    clock_sample_count: int
    clock_min_successful_samples: int
    clock_low_rtt_sample_count: int
    clock_sample_spacing_ms: float
    clock_max_rtt_ms: float
    clock_max_uncertainty_ms: float
    clock_max_offset_dispersion_ms: float
    clock_max_wall_step_ms: float
    clock_max_initial_offset_ms: float
    clock_reduce_only_phase_error_ms: float
    clock_kill_phase_error_ms: float

    @staticmethod
    def validated_rate_limit_settings(settings: dict) -> dict:
        rate_limit_settings = dict(
            settings.get("rest_rate_limit", {}) or {}
        )
        if rate_limit_settings.get("enabled", True) is not True:
            raise ValueError(
                "risk sidecar requires Binance REST rate-limit coordination"
            )
        return rate_limit_settings

    @classmethod
    def from_settings(
        cls,
        settings: dict,
        finite_float: Callable[[object, str], float],
        *,
        rate_limit_settings: dict = None,
    ) -> BinanceSidecarExchangeConfiguration:
        if rate_limit_settings is None:
            rate_limit_settings = cls.validated_rate_limit_settings(settings)
        symbols = tuple(
            sorted(
                {
                    str(symbol or "").strip().upper()
                    for symbol in settings.get("symbols", ())
                    if str(symbol or "").strip()
                }
            )
        )
        funding_guard = settings.get("funding_guard", {})
        if not isinstance(funding_guard, dict):
            raise ValueError("funding_guard settings must be an object")
        funding_guard_enabled = (
            funding_guard.get("enabled", False) is True
        )
        funding_max_source_age_ms = finite_float(
            funding_guard.get("max_snapshot_age_ms", 3_000.0),
            "funding_guard.max_snapshot_age_ms",
        )
        cash_flow_income_types = frozenset(
            str(value or "").upper()
            for value in settings.get(
                "cash_flow_income_types",
                ["TRANSFER"],
            )
            if str(value or "").strip()
        )
        cash_flow_assets = frozenset(
            str(value or "").upper()
            for value in settings.get(
                "cash_flow_assets",
                ["USDT", "USDC", "BUSD", "FDUSD"],
            )
            if str(value or "").strip()
        )
        cash_flow_max_pages = max(
            1,
            int(settings.get("cash_flow_max_pages", 5) or 5),
        )
        cash_flow_poll_interval_sec = max(
            1.0,
            finite_float(
                settings.get("cash_flow_poll_interval_sec", 30.0) or 30.0,
                "cash_flow_poll_interval_sec",
            ),
        )
        full_open_orders_audit_interval_sec = max(
            5.0,
            finite_float(
                settings.get(
                    "full_open_orders_audit_interval_sec",
                    60.0,
                )
                or 60.0,
                "full_open_orders_audit_interval_sec",
            ),
        )
        clock_sync_interval_sec = max(
            1.0,
            finite_float(
                settings.get("clock_sync_interval_sec", 30.0) or 30.0,
                "clock_sync_interval_sec",
            ),
        )
        clock_sample_count = max(
            1,
            int(settings.get("clock_sample_count", 5) or 5),
        )
        clock_min_successful_samples = max(
            1,
            min(
                clock_sample_count,
                int(settings.get("clock_min_successful_samples", 3) or 3),
            ),
        )
        clock_low_rtt_sample_count = max(
            1,
            min(
                clock_sample_count,
                int(settings.get("clock_low_rtt_sample_count", 3) or 3),
            ),
        )
        clock_sample_spacing_ms = max(
            0.0,
            finite_float(
                settings.get("clock_sample_spacing_ms", 10.0) or 0.0,
                "clock_sample_spacing_ms",
            ),
        )
        clock_max_rtt_ms = max(
            0.0,
            finite_float(
                settings.get("clock_max_rtt_ms", 200.0) or 0.0,
                "clock_max_rtt_ms",
            ),
        )
        clock_max_uncertainty_ms = max(
            0.0,
            finite_float(
                settings.get("clock_max_uncertainty_ms", 50.0) or 0.0,
                "clock_max_uncertainty_ms",
            ),
        )
        clock_max_offset_dispersion_ms = max(
            0.0,
            finite_float(
                settings.get("clock_max_offset_dispersion_ms", 10.0)
                or 0.0,
                "clock_max_offset_dispersion_ms",
            ),
        )
        clock_max_wall_step_ms = max(
            0.0,
            finite_float(
                settings.get("clock_max_wall_step_ms", 20.0) or 0.0,
                "clock_max_wall_step_ms",
            ),
        )
        clock_max_initial_offset_ms = max(
            0.0,
            finite_float(
                settings.get("clock_max_initial_offset_ms", 5000.0)
                or 0.0,
                "clock_max_initial_offset_ms",
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
        return cls(
            rate_limit_settings=dict(rate_limit_settings),
            symbols=symbols,
            funding_guard_enabled=funding_guard_enabled,
            funding_max_source_age_ms=funding_max_source_age_ms,
            daily_loss_enabled=bool(
                settings.get("daily_loss_enabled", False)
            ),
            cash_flow_income_types=cash_flow_income_types,
            cash_flow_assets=cash_flow_assets,
            cash_flow_max_pages=cash_flow_max_pages,
            cash_flow_poll_interval_sec=cash_flow_poll_interval_sec,
            full_open_orders_audit_interval_sec=(
                full_open_orders_audit_interval_sec
            ),
            clock_sync_enabled=bool(
                settings.get("clock_sync_enabled", True)
            ),
            clock_sync_interval_sec=clock_sync_interval_sec,
            clock_sample_count=clock_sample_count,
            clock_min_successful_samples=clock_min_successful_samples,
            clock_low_rtt_sample_count=clock_low_rtt_sample_count,
            clock_sample_spacing_ms=clock_sample_spacing_ms,
            clock_max_rtt_ms=clock_max_rtt_ms,
            clock_max_uncertainty_ms=clock_max_uncertainty_ms,
            clock_max_offset_dispersion_ms=(
                clock_max_offset_dispersion_ms
            ),
            clock_max_wall_step_ms=clock_max_wall_step_ms,
            clock_max_initial_offset_ms=clock_max_initial_offset_ms,
            clock_reduce_only_phase_error_ms=(
                clock_reduce_only_phase_error_ms
            ),
            clock_kill_phase_error_ms=clock_kill_phase_error_ms,
        )

    def initialize_owner(self, owner) -> None:
        owner.symbols = self.symbols
        owner.funding_guard_enabled = self.funding_guard_enabled
        owner.funding_max_source_age_ms = self.funding_max_source_age_ms
        owner.daily_loss_enabled = self.daily_loss_enabled
        owner.cash_flow_income_types = set(self.cash_flow_income_types)
        owner.cash_flow_assets = set(self.cash_flow_assets)
        owner.cash_flow_max_pages = self.cash_flow_max_pages
        owner.cash_flow_poll_interval_sec = self.cash_flow_poll_interval_sec
        owner._last_cash_flow_poll_monotonic = 0.0
        owner._cached_external_cash_flow_total = 0.0
        owner._cash_flow_cache_initialized = False
        owner.full_open_orders_audit_interval_sec = (
            self.full_open_orders_audit_interval_sec
        )
        owner._last_full_open_orders_audit_monotonic = 0.0
        owner._known_open_order_symbols = set()
        owner.clock_sync_enabled = self.clock_sync_enabled
        owner.clock_sync_interval_sec = self.clock_sync_interval_sec
        owner.clock_sample_count = self.clock_sample_count
        owner.clock_min_successful_samples = (
            self.clock_min_successful_samples
        )
        owner.clock_low_rtt_sample_count = self.clock_low_rtt_sample_count
        owner.clock_sample_spacing_ms = self.clock_sample_spacing_ms
        owner.clock_max_rtt_ms = self.clock_max_rtt_ms
        owner.clock_max_uncertainty_ms = self.clock_max_uncertainty_ms
        owner.clock_max_offset_dispersion_ms = (
            self.clock_max_offset_dispersion_ms
        )
        owner.clock_max_wall_step_ms = self.clock_max_wall_step_ms
        owner.clock_max_initial_offset_ms = self.clock_max_initial_offset_ms
        owner.clock_reduce_only_phase_error_ms = (
            self.clock_reduce_only_phase_error_ms
        )
        owner.clock_kill_phase_error_ms = self.clock_kill_phase_error_ms
        owner.last_clock_sync_monotonic = 0.0
        owner.clock_offset_ms = 0.0
        owner.clock_phase_error_ms = 0.0
        owner.clock_rtt_ms = 0.0
        owner.clock_uncertainty_ms = 0.0
        owner.clock_offset_dispersion_ms = 0.0
        owner._clock_anchor_epoch_ms = 0.0
        owner._clock_anchor_monotonic = 0.0
        owner.clock_reason = "clock_sync_missing"
