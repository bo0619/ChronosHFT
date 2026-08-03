import math
import multiprocessing
import os
import queue
import secrets
import signal
import time
from datetime import datetime, timezone

from risk.binance_sidecar_clock import BinanceSidecarClock
from risk.binance_sidecar_emergency import BinanceSidecarEmergencyActions
from risk.binance_sidecar_settings import BinanceSidecarExchangeConfiguration
from risk.binance_sidecar_truth import BinanceSidecarTruthReader

from risk.deployment_loss import (
    MAX_CANARY_DEPLOYED_EQUITY_FRACTION,
    deployed_capital_equity_ratio,
    deployed_capital_within_equity_limit,
    deployment_loss_action,
    update_deployment_loss,
)
from risk.funding_guard import (
    ALLOW as FUNDING_ALLOW,
    FundingGuardState,
    FundingObservation,
    evaluate_funding_guard,
)
from risk.sidecar_durable_state import SidecarDurableState
from risk.sidecar_core_status import RiskSidecarStatusProjection
from risk.sidecar_health import SidecarOmsHealth
from risk.sidecar_policy import RiskSidecarPolicy
from risk.sidecar_process import SidecarProcessBootstrap
from risk.sidecar_protocol import SidecarProtocol
from risk.sidecar_runtime import SidecarRuntime
from risk.sidecar_settings import SidecarSupervisorConfiguration
from risk.sidecar_snapshot_worker import RiskSnapshotWorker
from risk.sidecar_status import SidecarStatusProjection
from risk.sidecar_transport import SidecarTransport


SUPERVISOR_SOURCE = "independent_supervisor"
BINANCE_PREMIUM_INDEX_ENDPOINT = "/fapi/v1/premiumIndex"
_HARD_CLOCK_FAILURE_PREFIXES = (
    "clock_phase_error_kill:",
    "clock_initial_offset_exceeded:",
    "clock_anchor_non_finite",
    "clock_monotonic_regressed",
    "clock_phase_error_non_finite",
    "clock_phase_threshold_invalid",
)
STATE_REPLACE_MAX_ATTEMPTS = 5
STATE_REPLACE_RETRY_BASE_SEC = 0.01
TRANSIENT_WINDOWS_REPLACE_ERRORS = frozenset({5, 32, 33})


def _finite_float(value, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _replace_state_file(source: str, destination: str) -> None:
    """Replace one durable state file, tolerating brief Windows file locks."""
    for attempt in range(STATE_REPLACE_MAX_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            retryable = isinstance(exc, PermissionError) or getattr(
                exc,
                "winerror",
                None,
            ) in TRANSIENT_WINDOWS_REPLACE_ERRORS
            if not retryable or attempt + 1 >= STATE_REPLACE_MAX_ATTEMPTS:
                raise
            time.sleep(STATE_REPLACE_RETRY_BASE_SEC * (2**attempt))


def _is_truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _clock_failure_requires_kill(reason: str) -> bool:
    normalized = str(reason or "").strip().lower()
    return normalized.startswith(_HARD_CLOCK_FAILURE_PREFIXES)


def _isolate_sidecar_console_interrupts() -> None:
    if os.name == "nt":
        # The parent owns coordinated shutdown for the shared Windows console.
        signal.signal(signal.SIGINT, signal.SIG_IGN)


class BinanceRiskSidecarExchange:
    """Minimal authenticated exchange channel owned by the risk sidecar."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool,
        settings: dict = None,
    ):
        import requests

        from gateway.binance.rate_limit_budget import BinanceRateLimitBudget
        from gateway.binance.rest_api import BinanceRestApi

        settings = settings or {}
        rate_limit_settings = (
            BinanceSidecarExchangeConfiguration.validated_rate_limit_settings(
                settings
            )
        )
        self.rate_limit_budget = BinanceRateLimitBudget.from_config(
            rate_limit_settings
        )
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.rest = BinanceRestApi(
            api_key,
            api_secret,
            self.session,
            testnet=testnet,
            rate_limit_budget=self.rate_limit_budget,
        )
        self.rest.clock_resync_callback = self.sync_exchange_clock
        configuration = BinanceSidecarExchangeConfiguration.from_settings(
            settings,
            _finite_float,
            rate_limit_settings=rate_limit_settings,
        )
        configuration.initialize_owner(self)

    def _collect_clock_samples(self, *, emergency: bool = False):
        return BinanceSidecarClock(self, _finite_float).collect_samples(
            emergency=emergency
        )

    def sync_exchange_clock(self, *, emergency: bool = False):
        return BinanceSidecarClock(self, _finite_float).sync(
            emergency=emergency
        )

    def _ensure_exchange_clock(self, force: bool = False):
        return BinanceSidecarClock(self, _finite_float).ensure(force)

    def check_account_channel(self):
        return BinanceSidecarTruthReader(self).check_account_channel()

    @staticmethod
    def _response_payload(response, expected_type, label: str):
        return BinanceSidecarTruthReader.response_payload(
            response,
            expected_type,
            label,
        )

    @staticmethod
    def _position_risk_fingerprint(positions) -> tuple:
        return BinanceSidecarTruthReader.position_risk_fingerprint(
            positions
        )

    def _corrected_epoch_at(self, observed_monotonic: float):
        return BinanceSidecarClock(self, _finite_float).corrected_epoch_at(
            observed_monotonic
        )

    def _get_funding_observations(self):
        return BinanceSidecarTruthReader(self).get_funding_observations()

    def get_risk_snapshot(self):
        return BinanceSidecarTruthReader(self).get_risk_snapshot()

    @staticmethod
    def _income_identity(row: dict) -> str:
        return BinanceSidecarTruthReader.income_identity(row)

    def _get_daily_external_cash_flow(self):
        return BinanceSidecarTruthReader(
            self
        ).get_daily_external_cash_flow()

    def _get_cached_daily_external_cash_flow(self):
        return BinanceSidecarTruthReader(
            self
        ).get_cached_daily_external_cash_flow()

    def _get_open_orders_snapshot(self):
        return BinanceSidecarTruthReader(self).get_open_orders_snapshot()

    def _remember_open_order_symbols(self, rows):
        return BinanceSidecarTruthReader(self).remember_open_order_symbols(
            rows
        )

    def emergency_cancel(self, symbols, countdown_time_ms: int):
        return BinanceSidecarEmergencyActions(self).cancel(
            symbols,
            countdown_time_ms,
        )

    def emergency_flatten(self):
        return BinanceSidecarEmergencyActions(self).flatten()

    def close(self):
        self.session.close()


class _RiskSnapshotWorker(RiskSnapshotWorker):
    """Compatibility facade for the extracted snapshot worker."""

    def __init__(self, exchange):
        super().__init__(
            exchange,
            put_latest=_put_latest,
            perf_counter=time.perf_counter,
            wall_time=time.time,
        )


class RiskSidecarCore:
    """Deterministic sidecar state machine, separated for fault-injection tests."""

    def __init__(
        self,
        exchange,
        settings: dict,
        now: float = None,
        snapshot_worker=None,
    ):
        now = _finite_float(
            time.perf_counter() if now is None else now,
            "now",
        )
        if now < 0.0:
            raise ValueError("now must be non-negative")
        self.exchange = exchange
        self.snapshot_worker = snapshot_worker
        self.policy = RiskSidecarPolicy.from_settings(
            settings,
            _finite_float,
        )
        self.symbols = self.policy.symbols
        self.funding_guard_policy = self.policy.funding_guard_policy
        self.parent_heartbeat_timeout_sec = self.policy.parent_heartbeat_timeout_sec
        self.exchange_poll_interval_sec = self.policy.exchange_poll_interval_sec
        self.exchange_max_age_sec = self.policy.exchange_max_age_sec
        self.snapshot_worker_timeout_sec = self.policy.snapshot_worker_timeout_sec
        self.rearm_snapshot_max_age_sec = self.policy.rearm_snapshot_max_age_sec
        self.cancel_retry_sec = self.policy.cancel_retry_sec
        self.orphan_exit_sec = self.policy.orphan_exit_sec
        self.emergency_countdown_time_ms = self.policy.emergency_countdown_time_ms
        self.max_account_gross_notional = self.policy.max_account_gross_notional
        self.gross_kill_multiplier = self.policy.gross_kill_multiplier
        self.margin_reduce_only_ratio = self.policy.margin_reduce_only_ratio
        self.margin_kill_ratio = self.policy.margin_kill_ratio
        self.max_open_orders = self.policy.max_open_orders
        self.daily_loss_enabled = self.policy.daily_loss_enabled
        self.max_daily_loss = self.policy.max_daily_loss
        self.max_drawdown_pct = self.policy.max_drawdown_pct
        self.daily_loss_reduce_only_fraction = self.policy.daily_loss_reduce_only_fraction
        self.deployment_id = self.policy.deployment_id
        self.declared_account_equity = self.policy.declared_account_equity
        self.max_deployed_capital = self.policy.max_deployed_capital
        self.max_deployment_loss = self.policy.max_deployment_loss
        self.deployment_loss_reduce_only_fraction = self.policy.deployment_loss_reduce_only_fraction
        self.deployment_policy_fingerprint = self.policy.deployment_policy_fingerprint
        self.account_key_fingerprint = self.policy.account_key_fingerprint
        self.clock_sync_enabled = self.policy.clock_sync_enabled
        self.clock_reduce_only_phase_error_ms = self.policy.clock_reduce_only_phase_error_ms
        self.clock_kill_phase_error_ms = self.policy.clock_kill_phase_error_ms
        self.clock_reduce_only_offset_ms = self.policy.clock_reduce_only_offset_ms
        self.clock_kill_offset_ms = self.policy.clock_kill_offset_ms
        self.clock_max_rtt_ms = self.policy.clock_max_rtt_ms
        self.clock_max_uncertainty_ms = self.policy.clock_max_uncertainty_ms
        self.clock_max_offset_dispersion_ms = self.policy.clock_max_offset_dispersion_ms
        self.liquidation_proximity_enabled = self.policy.liquidation_proximity_enabled
        self.require_liquidation_price = self.policy.require_liquidation_price
        self.liquidation_reduce_only_distance_pct = self.policy.liquidation_reduce_only_distance_pct
        self.liquidation_kill_distance_pct = self.policy.liquidation_kill_distance_pct
        self.flatten_enabled = self.policy.flatten_enabled
        self.parent_loss_flatten_delay_sec = self.policy.parent_loss_flatten_delay_sec
        self.flatten_retry_sec = self.policy.flatten_retry_sec
        self.flat_verification_checks = self.policy.flat_verification_checks
        self.started_at = now
        self.last_parent_heartbeat_at = now
        self.last_parent_heartbeat_sent_monotonic = 0.0
        self.last_parent_heartbeat_received_at = 0.0
        self.parent_heartbeat_error = ""
        self.last_parent_sequence = 0
        self.last_exchange_poll_at = 0.0
        self.last_exchange_success_at = 0.0
        self.last_snapshot_result_sequence = 0
        self.snapshot_request_sequence = 0
        self.snapshot_request_inflight_sequence = 0
        self.snapshot_request_inflight_since = 0.0
        self.risk_snapshot_captured_at = 0.0
        self.risk_snapshot_captured_monotonic = 0.0
        self.exchange_healthy = False
        self.exchange_reason = "exchange_check_missing"
        self.last_cancel_attempt_at = 0.0
        self.last_cancel_ok = None
        self.last_cancel_reason = ""
        self.last_flatten_attempt_at = 0.0
        self.last_flatten_ok = None
        self.last_flatten_count = 0
        self.last_flatten_reason = ""
        self.risk_action = "NONE"
        self.risk_reason = ""
        self.funding_observations: dict[
            str, FundingObservation
        ] = {}
        self.funding_guard_states = {
            symbol: FundingGuardState(
                reason="funding_guard:startup_hold",
                post_hold_until_monotonic=(
                    now
                    + self.funding_guard_policy.post_funding_hold_sec
                ),
            )
            for symbol in self.symbols
            if self.funding_guard_policy.enabled
        }
        self.funding_guard_decisions = {}
        self.funding_action = (
            "REDUCE_ONLY"
            if self.funding_guard_policy.enabled
            else "NONE"
        )
        self.funding_reason = (
            "funding_guard:snapshot_unavailable"
            if self.funding_guard_policy.enabled
            else ""
        )
        self.kill_latched = False
        self.kill_reason = ""
        self.risk_metrics = {
            "maintenance_margin_ratio": 0.0,
            "position_gross_notional": 0.0,
            "opening_order_notional": 0.0,
            "projected_gross_notional": 0.0,
            "open_order_count": 0,
            "nonzero_position_count": 0,
            "risk_day": "",
            "equity": 0.0,
            "cash_flow_adjusted_equity": 0.0,
            "cash_flow_adjusted_daily_loss": 0.0,
            "deployment_id": self.deployment_id,
            "deployment_start_equity": 0.0,
            "deployment_adjusted_equity": 0.0,
            "deployment_loss": 0.0,
            "max_deployment_loss": self.max_deployment_loss,
            "declared_account_equity": self.declared_account_equity,
            "max_deployed_capital": self.max_deployed_capital,
            "deployment_policy_fingerprint": (
                self.deployment_policy_fingerprint
            ),
            "peak_adjusted_equity": 0.0,
            "peak_drawdown_pct": 0.0,
            "external_cash_flow_total": 0.0,
            "clock_offset_ms": 0.0,
            "clock_phase_error_ms": 0.0,
            "clock_rtt_ms": 0.0,
            "clock_uncertainty_ms": 0.0,
            "clock_offset_dispersion_ms": 0.0,
            "minimum_liquidation_distance_pct": None,
            "minimum_liquidation_distance_symbol": "",
            "funding_guard": {
                "enabled": self.funding_guard_policy.enabled,
                "healthy": not self.funding_guard_policy.enabled,
                "action": self.funding_action,
                "reason": self.funding_reason,
                "symbols": {},
            },
        }
        self.risk_snapshot_sequence = 0
        self.last_verified_snapshot_sequence = 0
        self.flat_verification_count = 0
        self.stage = "ARMED"
        self.unsafe_since = 0.0
        self.parent_stale_since = 0.0
        self.parent_stale_snapshot_sequence = 0
        self.quiesced = False
        self.quiesce_reason = ""
        self.quiesced_at = 0.0
        self.quiesce_snapshot_sequence = 0
        self.stop_requested = False
        self.stop_request_id = ""
        self.cancel_on_stop = True
        self.last_quiesce_request_id = ""
        self.last_quiesce_accepted = None
        self.last_quiesce_reason = ""
        self.last_quiesce_persisted = False
        self.last_shutdown_resume_request_id = ""
        self.last_shutdown_resume_accepted = None
        self.last_shutdown_resume_reason = ""
        self.last_shutdown_resume_persisted = False
        self.last_stop_request_id = ""
        self.last_stop_accepted = None
        self.last_stop_reason = ""
        self.last_stop_quiesced = False
        self.last_stop_cancel_requested = False
        self.last_stop_cancel_attempted = False
        self.last_stop_cancel_ok = None
        self.risk_day = str(settings.get("seed_risk_day", "") or "")
        self.day_start_equity = _finite_float(
            settings.get("seed_day_start_equity", 0.0) or 0.0,
            "seed_day_start_equity",
        )
        self.day_start_external_cash_flow_total = _finite_float(
            settings.get("seed_external_cash_flow_total", 0.0) or 0.0,
            "seed_external_cash_flow_total",
        )
        self.peak_adjusted_equity = _finite_float(
            settings.get("seed_peak_adjusted_equity", 0.0) or 0.0,
            "seed_peak_adjusted_equity",
        )
        self.last_equity = _finite_float(
            settings.get("seed_last_equity", 0.0) or 0.0,
            "seed_last_equity",
        )
        self.deployment_start_equity = _finite_float(
            settings.get("seed_deployment_start_equity", 0.0) or 0.0,
            "seed_deployment_start_equity",
        )
        self.deployment_start_external_cash_flow_total = _finite_float(
            settings.get(
                "seed_deployment_external_cash_flow_total",
                0.0,
            )
            or 0.0,
            "seed_deployment_external_cash_flow_total",
        )
        self.deployment_adjusted_equity = _finite_float(
            settings.get("seed_deployment_adjusted_equity", 0.0) or 0.0,
            "seed_deployment_adjusted_equity",
        )
        self.deployment_loss = max(
            0.0,
            _finite_float(
                settings.get("seed_deployment_loss", 0.0) or 0.0,
                "seed_deployment_loss",
            ),
        )
        self.state_path = str(settings.get("state_path", "") or "").strip()
        self.state_required = bool(settings.get("state_required", False))
        self.state_fsync = bool(settings.get("state_fsync", True))
        self.state_generation = 0
        self.state_recovered = False
        self.state_load_error = ""
        self.state_persist_error = ""
        self._last_persisted_fingerprint = None
        self.prepared_rearm = None
        self.rearm_prepare_ttl_sec = max(
            1.0,
            _finite_float(
                settings.get("rearm_prepare_ttl_sec", 10.0) or 10.0,
                "rearm_prepare_ttl_sec",
            ),
        )
        self.last_rearm_request_id = ""
        self.last_rearm_phase = ""
        self.last_rearm_accepted = None
        self.last_rearm_reason = ""
        self.last_rearm_token = ""
        self._load_durable_state()

    @staticmethod
    def _state_checksum(payload: dict) -> str:
        return SidecarDurableState.checksum(payload)

    def _durable_fingerprint(self):
        return SidecarDurableState(
            self,
            _finite_float,
            _replace_state_file,
        ).fingerprint()

    def _fail_closed_on_state_error(self, reason: str):
        return SidecarDurableState(
            self,
            _finite_float,
            _replace_state_file,
        ).fail_closed(reason)

    def _quarantine_corrupt_state(self):
        return SidecarDurableState(
            self,
            _finite_float,
            _replace_state_file,
        ).quarantine_corrupt()

    def _load_durable_state(self):
        return SidecarDurableState(
            self,
            _finite_float,
            _replace_state_file,
        ).load()

    def _persist_durable_state(self, event: str, force: bool = False) -> bool:
        return SidecarDurableState(
            self,
            _finite_float,
            _replace_state_file,
        ).persist(event, force)
    def receive_parent_heartbeat(
        self,
        sequence: int,
        sent_monotonic: float = None,
        now: float = None,
    ):
        now = time.perf_counter() if now is None else float(now)
        try:
            sequence = int(sequence or 0)
        except (TypeError, ValueError):
            self.parent_heartbeat_error = (
                "parent_heartbeat_sequence_invalid"
            )
            return False
        if sequence <= self.last_parent_sequence:
            return False
        self.last_parent_sequence = sequence
        try:
            sent_monotonic = float(sent_monotonic)
        except (TypeError, ValueError):
            self.parent_heartbeat_error = (
                "parent_heartbeat_timestamp_invalid"
            )
            return False
        if not math.isfinite(sent_monotonic) or sent_monotonic <= 0.0:
            self.parent_heartbeat_error = (
                "parent_heartbeat_timestamp_invalid"
            )
            return False
        if sent_monotonic > now:
            self.parent_heartbeat_error = (
                "parent_heartbeat_timestamp_future"
            )
            return False
        heartbeat_age = now - min(now, sent_monotonic)
        if heartbeat_age > self.parent_heartbeat_timeout_sec:
            self.parent_heartbeat_error = (
                "parent_heartbeat_timestamp_stale"
            )
            return False

        self.last_parent_heartbeat_at = min(now, sent_monotonic)
        self.last_parent_heartbeat_sent_monotonic = sent_monotonic
        self.last_parent_heartbeat_received_at = now
        self.parent_heartbeat_error = ""
        return True

    def _set_quiesce_result(
        self,
        request_id: str,
        accepted: bool,
        reason: str,
        persisted: bool,
    ):
        self.last_quiesce_request_id = str(request_id or "")
        self.last_quiesce_accepted = bool(accepted)
        self.last_quiesce_reason = str(reason or "")
        self.last_quiesce_persisted = bool(persisted)

    def _enter_quiesced(self, reason: str, event: str) -> bool:
        self.quiesced = True
        self.quiesce_reason = str(reason or "operator_quiesce")
        if self.quiesced_at <= 0.0:
            self.quiesced_at = time.time()
        self.quiesce_snapshot_sequence = self.risk_snapshot_sequence
        self.stage = "QUIESCED"
        self.prepared_rearm = None
        if not self.state_path:
            self.state_persist_error = "quiesce_state_path_missing"
            self._fail_closed_on_state_error(self.state_persist_error)
            persisted = False
        else:
            persisted = self._persist_durable_state(event, force=True)
        if persisted:
            return True

        # A quiesce is only real after it is durable. Keep the sidecar actively
        # supervising if the transition cannot be persisted.
        self.quiesced = False
        self.quiesce_reason = ""
        self.quiesced_at = 0.0
        self.quiesce_snapshot_sequence = 0
        self.kill_latched = True
        self.kill_reason = (
            self.state_persist_error or "quiesce_state_persist_failed"
        )
        self.stage = "FAILED"
        self.prepared_rearm = None
        self.flat_verification_count = 0
        self.last_verified_snapshot_sequence = 0
        self.last_cancel_attempt_at = 0.0
        self.last_flatten_attempt_at = 0.0
        return False

    def request_quiesce(
        self,
        request_id: str,
        reason: str,
    ):
        request_id = str(request_id or "")
        if not request_id:
            self._set_quiesce_result(
                request_id,
                False,
                "request_id_missing",
                False,
            )
            return False, "request_id_missing"

        persisted = self._enter_quiesced(
            str(reason or "operator_quiesce"),
            "supervisor_quiesced",
        )
        accepted = bool(persisted)
        result_reason = (
            "supervisor_quiesced"
            if accepted
            else self.state_persist_error or "quiesce_state_persist_failed"
        )
        self._set_quiesce_result(
            request_id,
            accepted,
            result_reason,
            persisted,
        )
        return accepted, result_reason

    def _takeover_from_quiesce(self, reason: str, event: str) -> bool:
        """Resume active kill supervision before attempting persistence."""
        self.quiesced = False
        self.quiesce_reason = ""
        self.quiesced_at = 0.0
        self.quiesce_snapshot_sequence = 0
        self.kill_latched = True
        self.kill_reason = str(reason or "quiesce_safety_takeover")
        self.stage = "FLATTENING"
        self.prepared_rearm = None
        self.flat_verification_count = 0
        self.last_verified_snapshot_sequence = 0
        self.last_cancel_attempt_at = 0.0
        self.last_flatten_attempt_at = 0.0
        return self._persist_durable_state(event, force=True)

    def _set_shutdown_resume_result(
        self,
        request_id: str,
        accepted: bool,
        reason: str,
        persisted: bool,
    ):
        self.last_shutdown_resume_request_id = str(request_id or "")
        self.last_shutdown_resume_accepted = bool(accepted)
        self.last_shutdown_resume_reason = str(reason or "")
        self.last_shutdown_resume_persisted = bool(persisted)

    def request_shutdown_resume(
        self,
        request_id: str,
        reason: str,
    ):
        """Leave QUIESCED in a durable kill state after truth drift."""
        request_id = str(request_id or "")
        if not request_id:
            self._set_shutdown_resume_result(
                request_id,
                False,
                "request_id_missing",
                False,
            )
            return False, "request_id_missing"

        persisted = self._takeover_from_quiesce(
            str(reason or "shutdown account truth drift"),
            "supervisor_shutdown_guard_resumed",
        )
        accepted = bool(persisted)
        result_reason = (
            "supervisor_shutdown_guard_resumed"
            if accepted
            else self.state_persist_error
            or "shutdown_resume_state_persist_failed"
        )
        self._set_shutdown_resume_result(
            request_id,
            accepted,
            result_reason,
            persisted,
        )
        return accepted, result_reason

    def request_stop(
        self,
        request_id: str,
        cancel_orders: bool = True,
    ):
        self.stop_requested = True
        self.stop_request_id = str(request_id or "")
        self.cancel_on_stop = bool(cancel_orders)

    def _set_stop_result(
        self,
        *,
        accepted: bool,
        reason: str,
        cancel_requested: bool,
        cancel_attempted: bool,
        cancel_ok,
    ):
        self.last_stop_request_id = str(self.stop_request_id or "")
        self.last_stop_accepted = bool(accepted)
        self.last_stop_reason = str(reason or "")
        self.last_stop_quiesced = bool(self.quiesced)
        self.last_stop_cancel_requested = bool(cancel_requested)
        self.last_stop_cancel_attempted = bool(cancel_attempted)
        self.last_stop_cancel_ok = (
            None if cancel_ok is None else bool(cancel_ok)
        )

    def _complete_stop_request(self, now: float) -> bool | None:
        cancel_requested = bool(self.cancel_on_stop)
        cancel_attempted = False
        cancel_ok = None
        accepted = False

        if self.quiesced:
            self._service_exchange_risk(
                now,
                force=(
                    self.risk_snapshot_sequence
                    <= self.quiesce_snapshot_sequence
                ),
            )
            exchange_valid = self._exchange_snapshot_valid(now)
            open_order_count, nonzero_position_count = (
                self._account_truth_counts()
            )
            if not exchange_valid:
                reason = self.exchange_reason or "stop_exchange_truth_stale"
                self._takeover_from_quiesce(
                    f"stop_guard_exchange_truth_invalid:{reason}",
                    "supervisor_stop_guard_takeover",
                )
            elif open_order_count or nonzero_position_count:
                reason = (
                    "stop_guard_account_not_flat:"
                    f"open_orders={open_order_count}:"
                    f"positions={nonzero_position_count}"
                )
                self._takeover_from_quiesce(
                    reason,
                    "supervisor_stop_guard_takeover",
                )
            elif (
                self.risk_snapshot_sequence
                <= self.quiesce_snapshot_sequence
            ):
                return None
            else:
                persisted = self._enter_quiesced(
                    self.quiesce_reason or "stop_after_flat_snapshot",
                    "supervisor_stop_quiesced",
                )
                accepted = bool(persisted)
                cancel_ok = True if cancel_requested else None
                reason = (
                    "supervisor_stop_ack"
                    if accepted
                    else self.state_persist_error
                    or "stop_quiesce_state_persist_failed"
                )
        elif cancel_requested:
            cancel_attempted = True
            cancel_ok = self._emergency_cancel(now)
            if not cancel_ok:
                reason = self.last_cancel_reason or "stop_cancel_failed"
            else:
                reason = (
                    "stop_after_cancel_requires_fresh_quiesce"
                )
        else:
            reason = "stop_without_cancel_requires_quiesced"

        self._set_stop_result(
            accepted=accepted,
            reason=reason,
            cancel_requested=cancel_requested,
            cancel_attempted=cancel_attempted,
            cancel_ok=cancel_ok,
        )
        self.stop_requested = False
        return accepted

    def _set_rearm_result(
        self,
        request_id: str,
        phase: str,
        accepted: bool,
        reason: str,
        token: str = "",
    ):
        self.last_rearm_request_id = str(request_id or "")
        self.last_rearm_phase = str(phase or "")
        self.last_rearm_accepted = bool(accepted)
        self.last_rearm_reason = str(reason or "")
        self.last_rearm_token = str(token or "")

    def _check_rearm_safety(self, now: float):
        if self.quiesced:
            return False, "supervisor_quiesced"
        if not self.kill_latched:
            return False, "kill_latch_not_set"
        if self.parent_heartbeat_error:
            return False, self.parent_heartbeat_error
        if (
            max(0.0, now - self.last_parent_heartbeat_at)
            > self.parent_heartbeat_timeout_sec
        ):
            return False, "parent_heartbeat_stale"
        self._service_exchange_risk(now, force=True)
        if not self.exchange_healthy:
            return False, self.exchange_reason or "exchange_snapshot_failed"
        if self.snapshot_worker is None:
            if self.last_exchange_success_at != now:
                return False, "exchange_snapshot_failed"
        elif (
            self.last_exchange_success_at <= 0.0
            or now - self.last_exchange_success_at
            > self.rearm_snapshot_max_age_sec
        ):
            return False, "exchange_snapshot_refresh_pending"
        if self.risk_action != "NONE":
            return False, self.risk_reason or "risk_breach_remains"
        funding_action, funding_reason = self._evaluate_funding_guard(now)
        if funding_action != "NONE":
            return False, funding_reason or "funding_guard_not_healthy"
        if int(self.risk_metrics.get("open_order_count", 0) or 0) != 0:
            return False, "open_orders_remain"
        if int(
            self.risk_metrics.get("nonzero_position_count", 0) or 0
        ) != 0:
            return False, "positions_remain"
        if self.kill_latched and self.stage != "FLAT_VERIFIED":
            return False, f"flat_not_verified:{self.stage}"
        return True, ""

    def prepare_rearm(self, request_id: str, reason: str, now: float = None):
        now = time.perf_counter() if now is None else float(now)
        request_id = str(request_id or "")
        if not request_id:
            self._set_rearm_result(
                request_id,
                "PREPARE",
                False,
                "request_id_missing",
            )
            return False, "", "request_id_missing"
        safe, refusal_reason = self._check_rearm_safety(now)
        if not safe:
            self.prepared_rearm = None
            self._set_rearm_result(
                request_id,
                "PREPARE",
                False,
                refusal_reason,
            )
            return False, "", refusal_reason
        token = secrets.token_hex(24)
        self.prepared_rearm = {
            "token": token,
            "reason": str(reason or "operator_rearm"),
            "expires_at": now + self.rearm_prepare_ttl_sec,
        }
        self._set_rearm_result(
            request_id,
            "PREPARE",
            True,
            "rearm_prepared",
            token,
        )
        return True, token, "rearm_prepared"

    def commit_rearm(
        self,
        request_id: str,
        token: str,
        now: float = None,
    ):
        now = time.perf_counter() if now is None else float(now)
        request_id = str(request_id or "")
        token = str(token or "")
        prepared = self.prepared_rearm or {}
        if (
            not token
            or not secrets.compare_digest(
                token,
                str(prepared.get("token", "") or ""),
            )
        ):
            reason = "rearm_token_invalid"
            self._set_rearm_result(
                request_id,
                "COMMIT",
                False,
                reason,
            )
            return False, reason
        if now > float(prepared.get("expires_at", 0.0) or 0.0):
            self.prepared_rearm = None
            reason = "rearm_prepare_expired"
            self._set_rearm_result(
                request_id,
                "COMMIT",
                False,
                reason,
            )
            return False, reason
        safe, refusal_reason = self._check_rearm_safety(now)
        if not safe:
            self.prepared_rearm = None
            self._set_rearm_result(
                request_id,
                "COMMIT",
                False,
                refusal_reason,
            )
            return False, refusal_reason

        previous_risk_state = {
            "kill_latched": self.kill_latched,
            "kill_reason": self.kill_reason,
            "stage": self.stage,
            "flat_verification_count": self.flat_verification_count,
            "last_verified_snapshot_sequence": (
                self.last_verified_snapshot_sequence
            ),
            "risk_action": self.risk_action,
            "risk_reason": self.risk_reason,
        }
        self.kill_latched = False
        self.kill_reason = ""
        self.stage = "ARMED"
        self.flat_verification_count = 0
        self.last_verified_snapshot_sequence = 0
        self.risk_action = "NONE"
        self.risk_reason = ""
        self.prepared_rearm = None
        if not self._persist_durable_state("operator_rearm_committed", force=True):
            refusal_reason = self.state_persist_error or "state_persist_failed"
            self.kill_latched = previous_risk_state["kill_latched"]
            self.kill_reason = previous_risk_state["kill_reason"]
            self.stage = previous_risk_state["stage"]
            self.flat_verification_count = previous_risk_state[
                "flat_verification_count"
            ]
            self.last_verified_snapshot_sequence = previous_risk_state[
                "last_verified_snapshot_sequence"
            ]
            self.risk_action = previous_risk_state["risk_action"]
            self.risk_reason = previous_risk_state["risk_reason"]
            self._set_rearm_result(
                request_id,
                "COMMIT",
                False,
                refusal_reason,
            )
            return False, refusal_reason
        self.state_load_error = ""
        self._set_rearm_result(
            request_id,
            "COMMIT",
            True,
            "rearm_committed",
        )
        return True, "rearm_committed"

    def abort_rearm(self, token: str):
        prepared = self.prepared_rearm or {}
        if not token or not secrets.compare_digest(
            str(token),
            str(prepared.get("token", "") or ""),
        ):
            return False
        self.prepared_rearm = None
        return True

    def _evaluate_daily_equity(self, snapshot: dict, metrics: dict):
        if (
            not self.daily_loss_enabled
            and self.max_deployment_loss <= 0.0
        ):
            return "NONE", ""
        account = snapshot.get("account", {}) or {}
        try:
            equity = float(account.get("totalMarginBalance", 0.0) or 0.0)
            external_cash_flow_total = float(
                snapshot["external_cash_flow_total"]
            )
            captured_at = float(snapshot.get("captured_at", time.time()))
        except (KeyError, TypeError, ValueError):
            return "REDUCE_ONLY", "daily_equity_snapshot_invalid"
        if (
            not math.isfinite(equity)
            or not math.isfinite(external_cash_flow_total)
            or not math.isfinite(captured_at)
            or captured_at <= 0.0
        ):
            return "REDUCE_ONLY", "daily_equity_snapshot_invalid"
        if self.max_deployed_capital > 0.0:
            try:
                deployed_equity_ratio = deployed_capital_equity_ratio(
                    equity=equity,
                    max_deployed_capital=self.max_deployed_capital,
                )
                capital_envelope_safe = (
                    deployed_capital_within_equity_limit(
                        equity=equity,
                        max_deployed_capital=self.max_deployed_capital,
                        maximum_fraction=(
                            MAX_CANARY_DEPLOYED_EQUITY_FRACTION
                        ),
                    )
                )
            except ValueError:
                return "KILL", "canary_account_equity_invalid"
            metrics["deployed_capital_equity_ratio"] = (
                deployed_equity_ratio
            )
            if not capital_envelope_safe:
                return (
                    "KILL",
                    "canary_deployed_capital_exceeds_current_equity_fraction",
                )

        current_risk_day = datetime.fromtimestamp(
            captured_at,
            tz=timezone.utc,
        ).date().isoformat()
        baseline_missing = bool(
            self.day_start_equity == 0.0
            and self.peak_adjusted_equity == 0.0
            and self.last_equity == 0.0
        )
        if self.risk_day != current_risk_day or baseline_missing:
            self.risk_day = current_risk_day
            self.day_start_equity = equity
            self.day_start_external_cash_flow_total = (
                external_cash_flow_total
            )
            self.peak_adjusted_equity = equity

        cash_flow_delta = (
            external_cash_flow_total
            - self.day_start_external_cash_flow_total
        )
        adjusted_equity = equity - cash_flow_delta
        if self.max_deployment_loss > 0.0:
            (
                self.deployment_start_equity,
                self.deployment_start_external_cash_flow_total,
                self.deployment_adjusted_equity,
                self.deployment_loss,
            ) = update_deployment_loss(
                equity=equity,
                external_cash_flow_total=external_cash_flow_total,
                start_equity=self.deployment_start_equity,
                start_external_cash_flow_total=(
                    self.deployment_start_external_cash_flow_total
                ),
            )
        if self.peak_adjusted_equity <= 0.0:
            self.peak_adjusted_equity = adjusted_equity
        elif adjusted_equity > self.peak_adjusted_equity:
            self.peak_adjusted_equity = adjusted_equity
        self.last_equity = equity
        daily_loss = max(0.0, self.day_start_equity - adjusted_equity)
        peak_drawdown_pct = (
            max(
                0.0,
                (self.peak_adjusted_equity - adjusted_equity)
                / self.peak_adjusted_equity,
            )
            if self.peak_adjusted_equity > 0.0
            else 0.0
        )
        metrics.update(
            {
                "risk_day": self.risk_day,
                "equity": equity,
                "cash_flow_adjusted_equity": adjusted_equity,
                "cash_flow_adjusted_daily_loss": daily_loss,
                "deployment_id": self.deployment_id,
                "deployment_start_equity": (
                    self.deployment_start_equity
                ),
                "deployment_adjusted_equity": (
                    self.deployment_adjusted_equity
                ),
                "deployment_loss": self.deployment_loss,
                "max_deployment_loss": self.max_deployment_loss,
                "peak_adjusted_equity": self.peak_adjusted_equity,
                "peak_drawdown_pct": peak_drawdown_pct,
                "external_cash_flow_total": external_cash_flow_total,
            }
        )

        deployment_action = deployment_loss_action(
            loss=self.deployment_loss,
            maximum_loss=self.max_deployment_loss,
            reduce_only_fraction=(
                self.deployment_loss_reduce_only_fraction
            ),
        )
        if deployment_action == "KILL":
            return (
                "KILL",
                f"deployment_loss_kill:{self.deployment_loss:.6f}",
            )
        if (
            self.daily_loss_enabled
            and self.max_daily_loss > 0.0
            and daily_loss >= self.max_daily_loss
        ):
            return "KILL", f"daily_loss_kill:{daily_loss:.6f}"
        if (
            self.daily_loss_enabled
            and self.max_drawdown_pct > 0.0
            and peak_drawdown_pct >= self.max_drawdown_pct
        ):
            return "KILL", f"peak_drawdown_kill:{peak_drawdown_pct:.6f}"
        if deployment_action == "REDUCE_ONLY":
            return (
                "REDUCE_ONLY",
                "deployment_loss_reduce_only:"
                f"{self.deployment_loss:.6f}",
            )
        reduce_fraction = self.daily_loss_reduce_only_fraction
        if (
            self.daily_loss_enabled
            and reduce_fraction > 0.0
            and self.max_daily_loss > 0.0
            and daily_loss >= self.max_daily_loss * reduce_fraction
        ):
            return "REDUCE_ONLY", f"daily_loss_reduce_only:{daily_loss:.6f}"
        if (
            self.daily_loss_enabled
            and reduce_fraction > 0.0
            and self.max_drawdown_pct > 0.0
            and peak_drawdown_pct
            >= self.max_drawdown_pct * reduce_fraction
        ):
            return (
                "REDUCE_ONLY",
                f"peak_drawdown_reduce_only:{peak_drawdown_pct:.6f}",
            )
        return "NONE", ""

    @staticmethod
    def _funding_observation_from_record(record):
        if not isinstance(record, dict):
            return None
        return FundingObservation(
            observation_id=str(record.get("observation_id", "") or ""),
            funding_rate=record.get("funding_rate"),
            next_funding_epoch=record.get("next_funding_epoch"),
            corrected_received_epoch=record.get(
                "corrected_received_epoch"
            ),
            received_monotonic=record.get("received_monotonic"),
            clock_healthy=record.get("clock_healthy") is True,
        )

    def _ingest_funding_observations(self, snapshot: dict) -> None:
        if not self.funding_guard_policy.enabled:
            return
        raw_observations = snapshot.get("funding_observations")
        if not isinstance(raw_observations, dict):
            self.funding_observations = {}
            return
        self.funding_observations = {
            symbol: observation
            for symbol in self.symbols
            if (
                observation := self._funding_observation_from_record(
                    raw_observations.get(symbol)
                )
            )
            is not None
        }

    def _evaluate_funding_guard(self, now: float):
        policy = self.funding_guard_policy
        if not policy.enabled:
            self.funding_action = "NONE"
            self.funding_reason = ""
            self.risk_metrics["funding_guard"] = {
                "enabled": False,
                "healthy": True,
                "action": "NONE",
                "reason": "",
                "symbols": {},
            }
            return self.funding_action, self.funding_reason
        if not self.symbols:
            self.funding_action = "REDUCE_ONLY"
            self.funding_reason = "funding_guard:symbols_missing"
            self.risk_metrics["funding_guard"] = {
                "enabled": True,
                "healthy": False,
                "action": self.funding_action,
                "reason": self.funding_reason,
                "symbols": {},
            }
            return self.funding_action, self.funding_reason

        symbol_metrics = {}
        blocked_reason = ""
        for symbol in self.symbols:
            previous = self.funding_guard_states.get(symbol)
            if previous is None:
                previous = FundingGuardState(
                    reason="funding_guard:startup_hold",
                    post_hold_until_monotonic=(
                        now + policy.post_funding_hold_sec
                    ),
                )
            decision, next_state = evaluate_funding_guard(
                policy,
                self.funding_observations.get(symbol),
                previous,
                now_monotonic=now,
            )
            self.funding_guard_states[symbol] = next_state
            self.funding_guard_decisions[symbol] = decision
            if decision.action != FUNDING_ALLOW and not blocked_reason:
                blocked_reason = f"{decision.reason}:{symbol}"
            symbol_metrics[symbol] = {
                "healthy": decision.healthy,
                "action": decision.action,
                "reason": decision.reason,
                "observation_valid": decision.observation_valid,
                "snapshot_age_ms": decision.snapshot_age_ms,
                "seconds_to_funding": decision.seconds_to_funding,
                "funding_rate": decision.funding_rate,
                "post_hold_remaining_sec": (
                    decision.post_hold_remaining_sec
                ),
                "consecutive_healthy_updates": (
                    decision.consecutive_healthy_updates
                ),
                "required_recovery_updates": (
                    decision.required_recovery_updates
                ),
            }

        self.funding_action = (
            "REDUCE_ONLY" if blocked_reason else "NONE"
        )
        self.funding_reason = blocked_reason
        self.risk_metrics["funding_guard"] = {
            "enabled": True,
            "healthy": not blocked_reason,
            "action": self.funding_action,
            "reason": self.funding_reason,
            "poll_interval_sec": self.exchange_poll_interval_sec,
            "max_snapshot_age_ms": policy.max_snapshot_age_ms,
            "symbols": symbol_metrics,
        }
        return self.funding_action, self.funding_reason

    def _evaluate_risk_snapshot(self, snapshot: dict):
        if not isinstance(snapshot, dict):
            return "REDUCE_ONLY", "risk_snapshot_invalid", {}
        account = snapshot.get("account")
        positions = snapshot.get("positions")
        open_orders = snapshot.get("open_orders")
        if (
            not isinstance(account, dict)
            or not isinstance(positions, list)
            or not isinstance(open_orders, list)
        ):
            return "REDUCE_ONLY", "risk_snapshot_invalid", {}
        if (
            "totalMaintMargin" not in account
            or "totalMarginBalance" not in account
        ):
            return "REDUCE_ONLY", "margin_snapshot_missing", {}
        try:
            maintenance_margin = float(account["totalMaintMargin"])
            margin_balance = float(account["totalMarginBalance"])
        except (TypeError, ValueError, OverflowError):
            return "REDUCE_ONLY", "margin_snapshot_invalid", {}
        if (
            not math.isfinite(maintenance_margin)
            or not math.isfinite(margin_balance)
            or maintenance_margin < 0.0
            or margin_balance < 0.0
        ):
            return "REDUCE_ONLY", "margin_snapshot_invalid", {}
        if margin_balance <= 0.0:
            return "KILL", "account_margin_balance_non_positive", {}
        maintenance_margin_ratio = (
            maintenance_margin / margin_balance
            if margin_balance > 0.0
            else (
                max(1.0, self.margin_kill_ratio)
                if maintenance_margin > 0.0
                else 0.0
            )
        )

        mark_prices = {}
        position_gross_notional = 0.0
        nonzero_position_count = 0
        minimum_liquidation_distance_pct = None
        minimum_liquidation_distance_symbol = ""
        for position in positions:
            symbol = str(position.get("symbol", "") or "").upper()
            try:
                amount = float(position.get("positionAmt", 0.0) or 0.0)
                mark_price = float(position.get("markPrice", 0.0) or 0.0)
                entry_price = float(position.get("entryPrice", 0.0) or 0.0)
                liquidation_price = float(
                    position.get("liquidationPrice", 0.0) or 0.0
                )
            except (TypeError, ValueError):
                return "REDUCE_ONLY", f"position_snapshot_invalid:{symbol}", {}
            if not math.isfinite(amount):
                return "REDUCE_ONLY", f"position_amount_invalid:{symbol}", {}
            price = mark_price if mark_price > 0.0 else entry_price
            if price > 0.0 and math.isfinite(price):
                mark_prices[symbol] = price
            if abs(amount) <= 1e-9:
                continue
            if price <= 0.0 or not math.isfinite(price):
                return "REDUCE_ONLY", f"position_price_unavailable:{symbol}", {}
            nonzero_position_count += 1
            position_notional = abs(amount) * price
            if not math.isfinite(position_notional):
                return "KILL", f"position_notional_overflow:{symbol}", {}
            position_gross_notional += position_notional
            if not math.isfinite(position_gross_notional):
                return "KILL", "account_position_notional_overflow", {}
            if self.liquidation_proximity_enabled:
                if mark_price <= 0.0 or not math.isfinite(mark_price):
                    return (
                        "REDUCE_ONLY",
                        f"liquidation_mark_price_unavailable:{symbol}",
                        {},
                    )
                if (
                    liquidation_price <= 0.0
                    or not math.isfinite(liquidation_price)
                ):
                    if self.require_liquidation_price:
                        return (
                            "REDUCE_ONLY",
                            f"liquidation_price_unavailable:{symbol}",
                            {},
                        )
                    continue
                liquidation_distance_pct = (
                    (mark_price - liquidation_price) / mark_price
                    if amount > 0.0
                    else (liquidation_price - mark_price) / mark_price
                )
                if (
                    minimum_liquidation_distance_pct is None
                    or liquidation_distance_pct
                    < minimum_liquidation_distance_pct
                ):
                    minimum_liquidation_distance_pct = (
                        liquidation_distance_pct
                    )
                    minimum_liquidation_distance_symbol = symbol

        opening_order_notional = 0.0
        for order in open_orders:
            if _is_truthy(order.get("reduceOnly", False)):
                continue
            symbol = str(order.get("symbol", "") or "").upper()
            try:
                original_qty = float(
                    order.get("origQty", order.get("quantity", 0.0)) or 0.0
                )
                executed_qty = float(order.get("executedQty", 0.0) or 0.0)
                price = float(order.get("price", 0.0) or 0.0)
                stop_price = float(order.get("stopPrice", 0.0) or 0.0)
            except (TypeError, ValueError):
                return "REDUCE_ONLY", f"open_order_invalid:{symbol}", {}
            if (
                not math.isfinite(original_qty)
                or not math.isfinite(executed_qty)
                or not math.isfinite(price)
                or not math.isfinite(stop_price)
                or original_qty < 0.0
                or executed_qty < 0.0
                or executed_qty > original_qty + 1e-9
            ):
                return "REDUCE_ONLY", f"open_order_invalid:{symbol}", {}
            remaining_qty = max(0.0, original_qty - executed_qty)
            if remaining_qty <= 1e-9:
                continue
            risk_price = price or stop_price or mark_prices.get(symbol, 0.0)
            if risk_price <= 0.0 or not math.isfinite(risk_price):
                return "REDUCE_ONLY", f"open_order_price_unavailable:{symbol}", {}
            order_notional = remaining_qty * risk_price
            if not math.isfinite(order_notional):
                return "REDUCE_ONLY", f"open_order_notional_overflow:{symbol}", {}
            opening_order_notional += order_notional
            if not math.isfinite(opening_order_notional):
                return "REDUCE_ONLY", "opening_order_notional_overflow", {}

        projected_gross_notional = (
            position_gross_notional + opening_order_notional
        )
        if not math.isfinite(projected_gross_notional):
            return "KILL", "projected_gross_notional_overflow", {}
        metrics = {
            "maintenance_margin_ratio": maintenance_margin_ratio,
            "position_gross_notional": position_gross_notional,
            "opening_order_notional": opening_order_notional,
            "projected_gross_notional": projected_gross_notional,
            "open_order_count": len(open_orders),
            "nonzero_position_count": nonzero_position_count,
            "minimum_liquidation_distance_pct": (
                minimum_liquidation_distance_pct
            ),
            "minimum_liquidation_distance_symbol": (
                minimum_liquidation_distance_symbol
            ),
        }
        daily_action, daily_reason = self._evaluate_daily_equity(
            snapshot,
            metrics,
        )
        clock_action = "NONE"
        clock_reason = ""
        if self.clock_sync_enabled:
            try:
                clock_offset_ms = float(snapshot["clock_offset_ms"])
                clock_phase_error_ms = float(
                    snapshot["clock_phase_error_ms"]
                )
                clock_rtt_ms = float(snapshot["clock_rtt_ms"])
                clock_uncertainty_ms = float(
                    snapshot["clock_uncertainty_ms"]
                )
                clock_offset_dispersion_ms = float(
                    snapshot["clock_offset_dispersion_ms"]
                )
            except (KeyError, TypeError, ValueError):
                return "REDUCE_ONLY", "clock_snapshot_invalid", metrics
            if (
                not math.isfinite(clock_offset_ms)
                or not math.isfinite(clock_phase_error_ms)
                or not math.isfinite(clock_rtt_ms)
                or not math.isfinite(clock_uncertainty_ms)
                or not math.isfinite(clock_offset_dispersion_ms)
                or clock_rtt_ms < 0.0
                or clock_uncertainty_ms < 0.0
                or clock_offset_dispersion_ms < 0.0
            ):
                return "REDUCE_ONLY", "clock_snapshot_invalid", metrics
            metrics["clock_offset_ms"] = clock_offset_ms
            metrics["clock_phase_error_ms"] = clock_phase_error_ms
            metrics["clock_rtt_ms"] = clock_rtt_ms
            metrics["clock_uncertainty_ms"] = clock_uncertainty_ms
            metrics["clock_offset_dispersion_ms"] = (
                clock_offset_dispersion_ms
            )
            if (
                self.clock_kill_phase_error_ms > 0.0
                and abs(clock_phase_error_ms)
                >= self.clock_kill_phase_error_ms
            ):
                clock_action = "KILL"
                clock_reason = (
                    f"clock_phase_error_kill:{clock_phase_error_ms:.3f}ms"
                )
            elif (
                self.clock_reduce_only_phase_error_ms > 0.0
                and abs(clock_phase_error_ms)
                >= self.clock_reduce_only_phase_error_ms
            ):
                clock_action = "REDUCE_ONLY"
                clock_reason = (
                    "clock_phase_error_reduce_only:"
                    f"{clock_phase_error_ms:.3f}ms"
                )
            elif (
                self.clock_max_rtt_ms > 0.0
                and clock_rtt_ms >= self.clock_max_rtt_ms
            ):
                clock_action = "REDUCE_ONLY"
                clock_reason = f"clock_rtt_reduce_only:{clock_rtt_ms:.3f}ms"
            elif (
                self.clock_max_uncertainty_ms > 0.0
                and clock_uncertainty_ms >= self.clock_max_uncertainty_ms
            ):
                clock_action = "REDUCE_ONLY"
                clock_reason = (
                    "clock_uncertainty_reduce_only:"
                    f"{clock_uncertainty_ms:.3f}ms"
                )
            elif (
                self.clock_max_offset_dispersion_ms > 0.0
                and clock_offset_dispersion_ms
                >= self.clock_max_offset_dispersion_ms
            ):
                clock_action = "REDUCE_ONLY"
                clock_reason = (
                    "clock_dispersion_reduce_only:"
                    f"{clock_offset_dispersion_ms:.3f}ms"
                )

        if maintenance_margin_ratio >= self.margin_kill_ratio:
            return (
                "KILL",
                f"maintenance_margin_kill:{maintenance_margin_ratio:.6f}",
                metrics,
            )
        if (
            minimum_liquidation_distance_pct is not None
            and minimum_liquidation_distance_pct
            <= self.liquidation_kill_distance_pct
        ):
            return (
                "KILL",
                "liquidation_distance_kill:"
                f"{minimum_liquidation_distance_symbol}:"
                f"{minimum_liquidation_distance_pct:.6f}",
                metrics,
            )
        if clock_action == "KILL":
            return clock_action, clock_reason, metrics
        if daily_action == "KILL":
            return daily_action, daily_reason, metrics
        if (
            self.max_account_gross_notional > 0.0
            and projected_gross_notional
            >= self.max_account_gross_notional * self.gross_kill_multiplier
        ):
            return (
                "KILL",
                f"gross_notional_kill:{projected_gross_notional:.6f}",
                metrics,
            )
        if maintenance_margin_ratio >= self.margin_reduce_only_ratio:
            return (
                "REDUCE_ONLY",
                f"maintenance_margin_reduce_only:{maintenance_margin_ratio:.6f}",
                metrics,
            )
        if (
            minimum_liquidation_distance_pct is not None
            and minimum_liquidation_distance_pct
            <= self.liquidation_reduce_only_distance_pct
        ):
            return (
                "REDUCE_ONLY",
                "liquidation_distance_reduce_only:"
                f"{minimum_liquidation_distance_symbol}:"
                f"{minimum_liquidation_distance_pct:.6f}",
                metrics,
            )
        if clock_action == "REDUCE_ONLY":
            return clock_action, clock_reason, metrics
        if daily_action == "REDUCE_ONLY":
            return daily_action, daily_reason, metrics
        if (
            self.max_account_gross_notional > 0.0
            and projected_gross_notional > self.max_account_gross_notional
        ):
            return (
                "REDUCE_ONLY",
                f"gross_notional_reduce_only:{projected_gross_notional:.6f}",
                metrics,
            )
        if self.max_open_orders > 0 and len(open_orders) > self.max_open_orders:
            return (
                "REDUCE_ONLY",
                f"open_order_count_limit:{len(open_orders)}>{self.max_open_orders}",
                metrics,
            )
        return "NONE", "", metrics

    def _fallback_risk_metrics(self, snapshot: dict):
        positions = snapshot.get("positions", []) or []
        open_orders = snapshot.get("open_orders", []) or []
        nonzero_position_count = 0
        for position in positions:
            try:
                amount = float(position.get("positionAmt", 0.0) or 0.0)
                nonzero_position_count += int(
                    not math.isfinite(amount) or abs(amount) > 1e-9
                )
            except (AttributeError, TypeError, ValueError):
                nonzero_position_count += 1
        return {
            "maintenance_margin_ratio": 0.0,
            "position_gross_notional": 0.0,
            "opening_order_notional": 0.0,
            "projected_gross_notional": 0.0,
            "open_order_count": len(open_orders),
            "nonzero_position_count": nonzero_position_count,
            "risk_day": self.risk_day,
            "equity": self.last_equity,
            "cash_flow_adjusted_equity": self.last_equity,
            "cash_flow_adjusted_daily_loss": 0.0,
            "deployment_id": self.deployment_id,
            "deployment_start_equity": self.deployment_start_equity,
            "deployment_adjusted_equity": (
                self.deployment_adjusted_equity
            ),
            "deployment_loss": self.deployment_loss,
            "max_deployment_loss": self.max_deployment_loss,
            "declared_account_equity": self.declared_account_equity,
            "max_deployed_capital": self.max_deployed_capital,
            "deployment_policy_fingerprint": (
                self.deployment_policy_fingerprint
            ),
            "peak_adjusted_equity": self.peak_adjusted_equity,
            "peak_drawdown_pct": 0.0,
            "external_cash_flow_total": 0.0,
            "clock_offset_ms": 0.0,
            "clock_phase_error_ms": 0.0,
            "clock_rtt_ms": 0.0,
            "clock_uncertainty_ms": 0.0,
            "clock_offset_dispersion_ms": 0.0,
            "minimum_liquidation_distance_pct": None,
            "minimum_liquidation_distance_symbol": "",
        }

    def _mark_exchange_snapshot_unhealthy(self, reason: str):
        reason = str(reason or "exchange_snapshot_failed")
        self.exchange_healthy = False
        self.exchange_reason = reason
        if self.risk_action != "KILL":
            self.risk_action = (
                "KILL"
                if _clock_failure_requires_kill(reason)
                else "REDUCE_ONLY"
            )
            self.risk_reason = reason

    @staticmethod
    def _snapshot_wall_time(snapshot: dict, fallback: float) -> float:
        try:
            fallback = float(fallback)
        except (TypeError, ValueError):
            fallback = 0.0
        if not math.isfinite(fallback) or fallback <= 0.0:
            fallback = time.time()
        try:
            captured_at = float(snapshot.get("captured_at", fallback))
        except (AttributeError, TypeError, ValueError):
            captured_at = fallback
        if not math.isfinite(captured_at) or captured_at <= 0.0:
            captured_at = fallback
        return captured_at

    def _apply_exchange_risk_result(
        self,
        *,
        healthy: bool,
        snapshot,
        reason: str,
        completed_monotonic: float,
        completed_at: float,
        full_snapshot: bool,
    ):
        if not healthy:
            self._mark_exchange_snapshot_unhealthy(
                reason or "exchange_snapshot_failed"
            )
            return

        if not full_snapshot:
            self.exchange_healthy = True
            self.exchange_reason = ""
            self.risk_action = "NONE"
            self.risk_reason = ""
            self.last_exchange_success_at = completed_monotonic
            return
        if not isinstance(snapshot, dict):
            self._mark_exchange_snapshot_unhealthy(
                "exchange_snapshot_payload_invalid"
            )
            return

        self._ingest_funding_observations(snapshot)
        try:
            action, risk_reason, metrics = self._evaluate_risk_snapshot(
                snapshot
            )
        except Exception as exc:
            self._mark_exchange_snapshot_unhealthy(
                "snapshot_evaluation_exception:"
                f"{type(exc).__name__}:{exc}"
            )
            return

        self.exchange_healthy = True
        self.exchange_reason = ""
        self.risk_action = action
        self.risk_reason = risk_reason
        self.risk_metrics = (
            metrics if metrics else self._fallback_risk_metrics(snapshot)
        )
        self._evaluate_funding_guard(completed_monotonic)
        self.last_exchange_success_at = completed_monotonic
        self.risk_snapshot_sequence += 1
        self.risk_snapshot_captured_monotonic = completed_monotonic
        self.risk_snapshot_captured_at = self._snapshot_wall_time(
            snapshot,
            completed_at,
        )

    def _poll_exchange_risk(self, now: float):
        snapshot_query = getattr(self.exchange, "get_risk_snapshot", None)
        if callable(snapshot_query):
            try:
                healthy, snapshot, reason = snapshot_query()
            except Exception as exc:
                healthy = False
                snapshot = {}
                reason = f"snapshot_exception:{type(exc).__name__}:{exc}"
            self._apply_exchange_risk_result(
                healthy=bool(healthy),
                snapshot=snapshot,
                reason=str(reason or ""),
                completed_monotonic=now,
                completed_at=time.time(),
                full_snapshot=True,
            )
            return

        try:
            healthy, reason = self.exchange.check_account_channel()
        except Exception as exc:
            healthy = False
            reason = f"exchange_exception:{type(exc).__name__}:{exc}"
        self._apply_exchange_risk_result(
            healthy=bool(healthy),
            snapshot={},
            reason=str(reason or ""),
            completed_monotonic=now,
            completed_at=time.time(),
            full_snapshot=False,
        )

    def _service_snapshot_worker(self, now: float, force: bool = False):
        result = self.snapshot_worker.take_latest()
        if result is not None:
            try:
                result_sequence = int(result.get("sequence", 0) or 0)
                requested_monotonic = float(
                    result.get("requested_monotonic", 0.0) or 0.0
                )
                completed_monotonic = float(
                    result.get("completed_monotonic", 0.0) or 0.0
                )
                completed_at = float(
                    result.get("completed_at", 0.0) or 0.0
                )
            except (AttributeError, TypeError, ValueError):
                result_sequence = 0
                requested_monotonic = 0.0
                completed_monotonic = 0.0
                completed_at = 0.0

            if result_sequence > self.last_snapshot_result_sequence:
                expected_sequence = (
                    self.snapshot_request_inflight_sequence
                )
                self.last_snapshot_result_sequence = result_sequence
                if result_sequence == expected_sequence:
                    self.snapshot_request_inflight_sequence = 0
                    self.snapshot_request_inflight_since = 0.0

                timing_valid = bool(
                    result_sequence > 0
                    and result_sequence == expected_sequence
                    and math.isfinite(requested_monotonic)
                    and requested_monotonic > 0.0
                    and math.isfinite(completed_monotonic)
                    and completed_monotonic >= requested_monotonic
                    and completed_monotonic <= now
                )
                if not timing_valid:
                    self._mark_exchange_snapshot_unhealthy(
                        "exchange_snapshot_worker_timestamp_invalid"
                    )
                elif (
                    completed_monotonic - requested_monotonic
                    > self.snapshot_worker_timeout_sec
                ):
                    self._mark_exchange_snapshot_unhealthy(
                        "exchange_snapshot_worker_deadline_exceeded"
                    )
                elif (
                    now - min(now, completed_monotonic)
                    > self.exchange_max_age_sec
                ):
                    self._mark_exchange_snapshot_unhealthy(
                        "exchange_snapshot_worker_result_stale"
                    )
                else:
                    self._apply_exchange_risk_result(
                        healthy=bool(result.get("healthy", False)),
                        snapshot=result.get("snapshot", {}),
                        reason=str(result.get("reason", "") or ""),
                        completed_monotonic=min(now, completed_monotonic),
                        completed_at=completed_at,
                        full_snapshot=bool(
                            result.get("full_snapshot", False)
                        ),
                    )

        inflight_age = (
            max(0.0, now - self.snapshot_request_inflight_since)
            if self.snapshot_request_inflight_since > 0.0
            else None
        )
        if (
            inflight_age is not None
            and inflight_age > self.snapshot_worker_timeout_sec
        ):
            self._mark_exchange_snapshot_unhealthy(
                "exchange_snapshot_worker_timeout"
            )

        worker_thread = getattr(self.snapshot_worker, "thread", None)
        if worker_thread is not None and not worker_thread.is_alive():
            self._mark_exchange_snapshot_unhealthy(
                "exchange_snapshot_worker_down"
            )

        due = bool(
            self.last_exchange_poll_at <= 0.0
            or now - self.last_exchange_poll_at
            >= self.exchange_poll_interval_sec
        )
        if (
            self.snapshot_request_inflight_sequence <= 0
            and (force or due)
        ):
            self.snapshot_request_sequence += 1
            submitted = self.snapshot_worker.submit(
                self.snapshot_request_sequence,
                now,
            )
            if submitted:
                self.last_exchange_poll_at = now
                self.snapshot_request_inflight_sequence = (
                    self.snapshot_request_sequence
                )
                self.snapshot_request_inflight_since = now
            else:
                self._mark_exchange_snapshot_unhealthy(
                    "exchange_snapshot_worker_queue_full"
                )

        if self.last_exchange_success_at <= 0.0:
            if self.exchange_reason in {
                "",
                "exchange_check_missing",
            }:
                self._mark_exchange_snapshot_unhealthy(
                    "exchange_snapshot_worker_pending"
                )
        elif now - self.last_exchange_success_at > self.exchange_max_age_sec:
            self._mark_exchange_snapshot_unhealthy(
                "exchange_snapshot_worker_output_stale"
            )

    def _service_exchange_risk(self, now: float, force: bool = False):
        if self.snapshot_worker is not None:
            self._service_snapshot_worker(now, force=force)
            return
        if (
            force
            or self.last_exchange_poll_at <= 0.0
            or now - self.last_exchange_poll_at
            >= self.exchange_poll_interval_sec
        ):
            self.last_exchange_poll_at = now
            self._poll_exchange_risk(now)

    def _exchange_snapshot_valid(self, now: float) -> bool:
        success_at = self.last_exchange_success_at
        captured_at = self.risk_snapshot_captured_monotonic
        return bool(
            self.exchange_healthy
            and self.risk_snapshot_sequence > 0
            and math.isfinite(success_at)
            and math.isfinite(captured_at)
            and 0.0 < success_at <= now
            and 0.0 < captured_at <= now
            and success_at == captured_at
            and now - success_at <= self.exchange_max_age_sec
            and now - captured_at <= self.exchange_max_age_sec
        )

    def _account_truth_counts(self):
        return (
            max(
                0,
                int(self.risk_metrics.get("open_order_count", 0) or 0),
            ),
            max(
                0,
                int(
                    self.risk_metrics.get(
                        "nonzero_position_count",
                        0,
                    )
                    or 0
                ),
            ),
        )

    def _update_parent_stale_state(
        self,
        parent_healthy: bool,
        now: float,
    ) -> None:
        if parent_healthy:
            self.parent_stale_since = 0.0
            self.parent_stale_snapshot_sequence = 0
            return
        if self.parent_stale_since <= 0.0:
            self.parent_stale_since = now
            self.parent_stale_snapshot_sequence = (
                self.risk_snapshot_sequence
            )
            self.flat_verification_count = 0
            self.last_verified_snapshot_sequence = 0

    def _emergency_cancel(self, now: float):
        if self.quiesced:
            self.last_cancel_reason = "supervisor_quiesced"
            return False
        self.last_cancel_attempt_at = now
        try:
            ok, reason = self.exchange.emergency_cancel(
                self.symbols,
                self.emergency_countdown_time_ms,
            )
        except Exception as exc:
            ok = False
            reason = f"cancel_exception:{type(exc).__name__}:{exc}"
        self.last_cancel_ok = bool(ok)
        self.last_cancel_reason = str(reason or "")
        return self.last_cancel_ok

    def _emergency_flatten(self, now: float):
        if self.quiesced:
            self.last_flatten_reason = "supervisor_quiesced"
            return False
        self.last_flatten_attempt_at = now
        flatten = getattr(self.exchange, "emergency_flatten", None)
        if not callable(flatten):
            self.last_flatten_ok = False
            self.last_flatten_count = 0
            self.last_flatten_reason = "flatten_method_unavailable"
            return False
        try:
            ok, submitted, reason = flatten()
        except Exception as exc:
            ok = False
            submitted = 0
            reason = f"flatten_exception:{type(exc).__name__}:{exc}"
        self.last_flatten_ok = bool(ok)
        self.last_flatten_count = int(submitted or 0)
        self.last_flatten_reason = str(reason or "")
        return self.last_flatten_ok

    def _step_quiesced(self, now: float):
        self._service_exchange_risk(
            now,
            force=(
                self.risk_snapshot_sequence
                <= self.quiesce_snapshot_sequence
            ),
        )
        parent_age = max(0.0, now - self.last_parent_heartbeat_at)
        parent_healthy = bool(
            not self.parent_heartbeat_error
            and parent_age <= self.parent_heartbeat_timeout_sec
        )
        self._update_parent_stale_state(parent_healthy, now)
        exchange_valid = self._exchange_snapshot_valid(now)
        open_order_count, nonzero_position_count = (
            self._account_truth_counts()
        )

        takeover_reason = ""
        if not parent_healthy:
            takeover_reason = (
                self.parent_heartbeat_error
                or "quiesce_parent_heartbeat_stale"
            )
        elif not exchange_valid:
            takeover_reason = (
                self.exchange_reason or "quiesce_exchange_truth_stale"
            )
        elif open_order_count or nonzero_position_count:
            takeover_reason = (
                "quiesce_account_truth_drift:"
                f"open_orders={open_order_count}:"
                f"positions={nonzero_position_count}"
            )

        if takeover_reason:
            self._takeover_from_quiesce(
                takeover_reason,
                "supervisor_quiesce_safety_takeover",
            )
            if self.stop_requested:
                self._set_stop_result(
                    accepted=False,
                    reason=f"stop_guard_takeover:{takeover_reason}",
                    cancel_requested=bool(self.cancel_on_stop),
                    cancel_attempted=False,
                    cancel_ok=None,
                )
                self.stop_requested = False
            return self.step(now, exchange_serviced=True)

        reason = "supervisor_quiesced"
        action = "KILL" if self.kill_latched else "REDUCE_ONLY"
        return self._status(False, reason, action, now), True

    def step(
        self,
        now: float = None,
        *,
        exchange_serviced: bool = False,
    ):
        now = time.perf_counter() if now is None else float(now)
        if self.stop_requested:
            stop_accepted = self._complete_stop_request(now)
            if stop_accepted is not None:
                return self._status(
                    False,
                    self.last_stop_reason or "supervisor_stop_failed",
                    "KILL" if self.kill_latched else "REDUCE_ONLY",
                    now,
                ), not stop_accepted
        if self.quiesced:
            return self._step_quiesced(now)

        if not exchange_serviced:
            self._service_exchange_risk(now)
        funding_action, funding_reason = self._evaluate_funding_guard(now)

        parent_age = max(0.0, now - self.last_parent_heartbeat_at)
        parent_healthy = bool(
            not self.parent_heartbeat_error
            and parent_age <= self.parent_heartbeat_timeout_sec
        )
        exchange_valid = self._exchange_snapshot_valid(now)
        self._update_parent_stale_state(parent_healthy, now)

        action = (
            self.risk_action
            if exchange_valid or self.risk_action == "KILL"
            else "REDUCE_ONLY"
        )
        if action == "NONE" and funding_action != "NONE":
            action = funding_action
        if not parent_healthy:
            parent_stale_age = max(0.0, now - self.parent_stale_since)
            if action == "KILL":
                reason = self.risk_reason or "independent_hard_risk_breach"
            elif (
                self.flatten_enabled
                and parent_stale_age >= self.parent_loss_flatten_delay_sec
            ):
                action = "KILL"
                reason = "parent_heartbeat_stale_flatten"
            else:
                action = "REDUCE_ONLY"
                reason = (
                    self.parent_heartbeat_error
                    or "parent_heartbeat_stale"
                )
        elif not exchange_valid:
            reason = (
                self.risk_reason
                if action == "KILL"
                else self.exchange_reason
            ) or "exchange_health_stale"
        elif action != "NONE":
            reason = (
                self.risk_reason
                if self.risk_action != "NONE"
                else funding_reason
            ) or "independent_risk_breach"
        else:
            reason = ""

        if action == "KILL" and not self.kill_latched:
            self.kill_latched = True
            self.kill_reason = str(
                reason or self.risk_reason or "independent_hard_risk_breach"
            )
        if self.kill_latched:
            action = "KILL"
            reason = self.kill_reason or "independent_hard_risk_breach"

        healthy = parent_healthy and exchange_valid and action == "NONE"
        open_order_count, nonzero_position_count = (
            self._account_truth_counts()
        )
        if healthy:
            self.unsafe_since = 0.0
            self.stage = "ARMED"
            self.flat_verification_count = 0
            self.last_verified_snapshot_sequence = 0
        else:
            if self.unsafe_since <= 0.0:
                self.unsafe_since = now
            if (
                self.stage == "FLAT_VERIFIED"
                and (
                    not exchange_valid
                    or open_order_count
                    or nonzero_position_count
                )
            ):
                self.stage = "FLATTENING"
                self.flat_verification_count = 0
                self.last_verified_snapshot_sequence = 0
            if self.stage != "FLAT_VERIFIED":
                if (
                    self.last_cancel_attempt_at <= 0.0
                    or now - self.last_cancel_attempt_at >= self.cancel_retry_sec
                ):
                    self.stage = "CANCEL_PENDING"
                    self._emergency_cancel(now)

                if action == "KILL" and self.flatten_enabled:
                    self.stage = "FLATTENING"
                    exposure_remains = bool(
                        int(self.risk_metrics.get("open_order_count", 0) or 0)
                        or int(
                            self.risk_metrics.get(
                                "nonzero_position_count",
                                0,
                            )
                            or 0
                        )
                    )
                    exposure_unknown = not exchange_valid
                    if (exposure_remains or exposure_unknown) and (
                        self.last_flatten_attempt_at <= 0.0
                        or now - self.last_flatten_attempt_at
                        >= self.flatten_retry_sec
                    ):
                        self._emergency_flatten(now)

            if exchange_valid:
                if action == "KILL" and self.flatten_enabled:
                    if (
                        open_order_count == 0
                        and nonzero_position_count == 0
                        and (
                            parent_healthy
                            or self.risk_snapshot_sequence
                            > self.parent_stale_snapshot_sequence
                        )
                        and self.risk_snapshot_sequence
                        > self.last_verified_snapshot_sequence
                    ):
                        self.last_verified_snapshot_sequence = (
                            self.risk_snapshot_sequence
                        )
                        self.flat_verification_count += 1
                    elif open_order_count or nonzero_position_count:
                        self.stage = "FLATTENING"
                        self.flat_verification_count = 0
                        self.last_verified_snapshot_sequence = (
                            self.risk_snapshot_sequence
                        )
                    if (
                        self.flat_verification_count
                        >= self.flat_verification_checks
                    ):
                        self.stage = "FLAT_VERIFIED"
                elif open_order_count == 0:
                    self.stage = "CANCEL_VERIFIED"

        if not self._persist_durable_state("risk_state_transition"):
            healthy = False
            action = "KILL"
            reason = self.state_persist_error or "state_persist_failed"

        keep_running = not (
            not parent_healthy
            and self.parent_stale_since > 0.0
            and now - self.parent_stale_since >= self.orphan_exit_sec
            and self.stage == "FLAT_VERIFIED"
            and exchange_valid
            and open_order_count == 0
            and nonzero_position_count == 0
            and self.flat_verification_count
            >= self.flat_verification_checks
            and self.last_verified_snapshot_sequence
            == self.risk_snapshot_sequence
            and self.last_verified_snapshot_sequence
            > self.parent_stale_snapshot_sequence
        )
        return self._status(healthy, reason, action, now), keep_running

    def _status(self, healthy: bool, reason: str, action: str, now: float):
        return RiskSidecarStatusProjection.build(
            self,
            healthy,
            reason,
            action,
            now,
        )


def _put_latest(target_queue, payload):
    return SidecarTransport.put_latest(target_queue, payload)


def _put_reliable(target_queue, payload, timeout_sec: float) -> bool:
    return SidecarTransport.put_reliable(
        target_queue,
        payload,
        timeout_sec,
    )


def run_sidecar_loop(
    command_queue,
    status_queue,
    settings: dict,
    exchange,
    snapshot_exchange=None,
    heartbeat_queue=None,
):
    SidecarRuntime.run(
        command_queue,
        status_queue,
        settings,
        exchange,
        snapshot_exchange=snapshot_exchange,
        heartbeat_queue=heartbeat_queue,
        snapshot_worker_factory=_RiskSnapshotWorker,
        core_factory=RiskSidecarCore,
        isolated_exchange_type=BinanceRiskSidecarExchange,
        put_latest=_put_latest,
        perf_counter=time.perf_counter,
        wall_time=time.time,
        getpid=os.getpid,
        sleep=time.sleep,
    )


def _risk_sidecar_process(
    command_queue,
    status_queue,
    settings: dict,
    heartbeat_queue=None,
):
    SidecarProcessBootstrap.run(
        command_queue,
        status_queue,
        settings,
        heartbeat_queue,
        isolate_console_interrupts=_isolate_sidecar_console_interrupts,
        exchange_factory=BinanceRiskSidecarExchange,
        run_loop=run_sidecar_loop,
        put_latest=_put_latest,
        getpid=os.getpid,
        wall_time=time.time,
    )


class IndependentRiskSupervisor:
    """Parent-side controller for the independent risk supervision process."""

    def __init__(self, oms, config: dict, risk_manager=None):
        self.oms = oms
        self.risk_manager = risk_manager
        configuration = SidecarSupervisorConfiguration.from_root(
            config,
            risk_manager,
            _finite_float,
            SUPERVISOR_SOURCE,
        )
        self.config = configuration.supervisor_config
        self.enabled = configuration.enabled
        self.heartbeat_interval_sec = configuration.heartbeat_interval_sec
        self.status_max_age_sec = configuration.status_max_age_sec
        self.stop_timeout_sec = configuration.stop_timeout_sec
        self.control_enqueue_timeout_sec = (
            configuration.control_enqueue_timeout_sec
        )
        self.recovery_checks = configuration.recovery_checks
        self.recovery_snapshot_max_age_sec = (
            configuration.recovery_snapshot_max_age_sec
        )
        self.rearm_command_timeout_sec = (
            configuration.rearm_command_timeout_sec
        )
        self.settings = configuration.child_settings
        self.session_id = secrets.token_hex(16)
        self.settings["session_id"] = self.session_id
        self.context = None
        self.command_queue = None
        self.heartbeat_queue = None
        self.status_queue = None
        self.process = None
        self.heartbeat_sequence = 0
        self.last_heartbeat_sent_at = 0.0
        self.parent_heartbeat_suspended_reason = ""
        self.last_status = {}
        self.last_status_received_at = 0.0
        self.last_status_protocol_error = ""
        self.started_at = 0.0
        self.recovery_count = 0
        self.last_recovery_snapshot_sequence = 0

    def start(self) -> bool:
        if not self.enabled:
            return True
        if self.process is not None and self.process.is_alive():
            return True
        self.last_status = {}
        self.last_status_received_at = 0.0
        self.last_status_protocol_error = ""
        self.last_recovery_snapshot_sequence = 0
        self.recovery_count = 0
        self.heartbeat_sequence = 0
        self.last_heartbeat_sent_at = 0.0
        SidecarTransport.start_process(
            self,
            multiprocessing,
            _risk_sidecar_process,
            time.perf_counter,
        )
        self._send_heartbeat(self.started_at)
        self._apply_oms_health(False, "supervisor_starting")
        return True

    def pulse_parent_heartbeat(self) -> bool:
        """Emit only the liveness pulse, without applying parent-side risk state."""
        if not self.enabled:
            return True
        if self.parent_heartbeat_suspended_reason:
            return False
        process = self.process
        if process is None or not process.is_alive():
            return False
        self._send_heartbeat(time.perf_counter())
        return True

    def _send_heartbeat(self, now: float):
        if self.parent_heartbeat_suspended_reason:
            return
        if now - self.last_heartbeat_sent_at < self.heartbeat_interval_sec:
            return
        self.heartbeat_sequence += 1
        payload = {
            "type": "HEARTBEAT",
            "session_id": self.session_id,
            "sequence": self.heartbeat_sequence,
            "sent_monotonic": now,
        }
        if self.heartbeat_queue is not None:
            delivered = _put_latest(self.heartbeat_queue, payload)
        else:
            try:
                self.command_queue.put_nowait(payload)
                delivered = True
            except (OSError, ValueError, queue.Full):
                delivered = False
        if delivered:
            self.last_heartbeat_sent_at = now

    @staticmethod
    def _validate_status_payload(status: dict) -> dict:
        return SidecarProtocol.validate_status(status, _finite_float)

    def _drain_status(self, now: float):
        SidecarTransport.drain_status(self, now)

    def _record_oms_heartbeat(self, healthy: bool, reason: str):
        return SidecarOmsHealth.record_heartbeat(
            self,
            healthy,
            reason,
            SUPERVISOR_SOURCE,
        )

    def _reset_recovery_progress(self):
        SidecarOmsHealth.reset_recovery_progress(self)

    def _apply_oms_health(self, healthy: bool, reason: str) -> bool:
        return SidecarOmsHealth.apply(
            self,
            healthy,
            reason,
            time.perf_counter,
            math.isfinite,
        )

    def tick(self) -> bool:
        return SidecarOmsHealth.tick(self, time.perf_counter)

    def wait_until_healthy(self, timeout_sec: float = 10.0) -> bool:
        return SidecarOmsHealth.wait_until_healthy(
            self,
            timeout_sec,
            time.perf_counter,
            time.sleep,
        )

    def _control_failure_result(
        self,
        command_type: str,
        failure_reason: str,
        request_id: str = "",
        **payload,
    ) -> dict:
        return SidecarProtocol.control_failure(
            command_type,
            self.last_status,
            failure_reason,
            request_id,
            **payload,
        )

    def _read_control_ack(
        self,
        command_type: str,
        request_id: str,
    ):
        return SidecarProtocol.read_control_ack(
            command_type,
            request_id,
            self.last_status,
        )

    def _request_sidecar_control(
        self,
        command_type: str,
        timeout_sec: float = None,
        **payload,
    ) -> dict:
        return SidecarTransport.request_control(
            self,
            command_type,
            timeout_sec,
            payload,
            _put_reliable,
            secrets.token_hex,
            time.perf_counter,
            time.sleep,
        )

    def prepare_rearm(self, reason: str, timeout_sec: float = None) -> dict:
        return self._request_sidecar_control(
            "PREPARE_REARM",
            timeout_sec=timeout_sec,
            reason=str(reason or "operator_rearm"),
        )

    def commit_rearm(self, token: str, timeout_sec: float = None) -> dict:
        return self._request_sidecar_control(
            "COMMIT_REARM",
            timeout_sec=timeout_sec,
            token=str(token or ""),
        )

    def quiesce(
        self,
        reason: str = "parent_quiesce",
        timeout_sec: float = None,
    ) -> dict:
        result = self._request_sidecar_control(
            "QUIESCE",
            timeout_sec=timeout_sec,
            reason=str(reason or "parent_quiesce"),
        )
        if self.enabled and bool(result.get("quiesced", False)):
            self._apply_oms_health(False, "supervisor_quiesced")
        return result

    def resume_shutdown_guard(
        self,
        reason: str = "shutdown account truth drift",
        timeout_sec: float = None,
    ) -> dict:
        result = self._request_sidecar_control(
            "RESUME_SHUTDOWN",
            timeout_sec=timeout_sec,
            reason=str(reason or "shutdown account truth drift"),
        )
        if self.enabled and bool(result.get("accepted", False)):
            self._apply_oms_health(
                False,
                "supervisor_shutdown_guard_active",
            )
        if self.enabled and not (
            result.get("accepted") is True
            and result.get("kill_latched") is True
            and result.get("persisted") is True
        ):
            self.suspend_parent_heartbeat(
                "shutdown_guard_handoff_unconfirmed:"
                f"{result.get('reason', 'unknown')}"
            )
        return result

    def suspend_parent_heartbeat(self, reason: str) -> bool:
        """Force the sidecar stale-parent kill path after control-plane loss."""
        if not self.enabled:
            return False
        self.parent_heartbeat_suspended_reason = str(
            reason or "parent_requested_sidecar_takeover"
        )
        return True

    def abort_rearm(self, token: str) -> bool:
        return SidecarTransport.enqueue_abort(self, token, _put_reliable)

    def get_status_snapshot(self) -> dict:
        return SidecarStatusProjection.build(
            enabled=self.enabled,
            process=self.process,
            parent_heartbeat_suspended_reason=(
                self.parent_heartbeat_suspended_reason
            ),
            last_status=self.last_status,
            last_status_received_at=self.last_status_received_at,
            last_status_protocol_error=self.last_status_protocol_error,
            status_max_age_sec=self.status_max_age_sec,
            settings=self.settings,
            now=time.perf_counter(),
        )

    def stop(self, cancel_orders: bool = True) -> dict:
        if not self.enabled:
            return {
                "accepted": not bool(cancel_orders),
                "reason": (
                    "supervisor_disabled"
                    if not cancel_orders
                    else "supervisor_disabled_cancel_not_attempted"
                ),
                "request_id": "",
                "quiesced": True,
                "cancel_requested": bool(cancel_orders),
                "cancel_attempted": False,
                "cancel_ok": None,
                "process_exited": True,
                "forced_terminated": False,
            }
        process = self.process
        if process is None:
            return {
                "accepted": False,
                "reason": "supervisor_process_missing",
                "request_id": "",
                "quiesced": bool(
                    self.last_status.get("quiesced", False)
                ),
                "cancel_requested": bool(cancel_orders),
                "cancel_attempted": False,
                "cancel_ok": None,
                "process_exited": True,
                "forced_terminated": False,
            }

        if process.is_alive():
            result = self._request_sidecar_control(
                "STOP",
                timeout_sec=self.stop_timeout_sec,
                cancel_orders=bool(cancel_orders),
            )
        else:
            result = self._control_failure_result(
                "STOP",
                "supervisor_process_down_before_ack",
                cancel_orders=bool(cancel_orders),
            )

        forced_terminated = False
        if bool(result.get("accepted", False)) and bool(
            result.get("quiesced", False)
        ):
            process.join(min(1.0, self.stop_timeout_sec))
            if process.is_alive():
                process.terminate()
                process.join(2.0)
                forced_terminated = True

        process_exited = not process.is_alive()
        result = {
            **result,
            "process_exited": process_exited,
            "forced_terminated": forced_terminated,
        }
        if process_exited:
            self._apply_oms_health(False, "supervisor_stopped")
            SidecarTransport.close_channels(self)
        else:
            self._apply_oms_health(
                False,
                "supervisor_stop_not_acknowledged:"
                f"{result.get('reason', 'unknown')}",
            )
        return result
