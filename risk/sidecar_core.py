"""Deterministic child-side risk supervision state machine."""

import math
import secrets

from risk.exchange_port import StateVersion
from risk.sidecar_account_risk import SidecarAccountRiskController
from risk.sidecar_command_runtime import (
    check_rearm_safety,
    commit_rearm as run_commit_rearm,
    complete_stop_request,
    prepare_rearm as run_prepare_rearm,
    rearm_proof_binding,
)
from risk.sidecar_control_state import ControlEffects, SidecarControlController
from risk.sidecar_core_status import RiskSidecarStatusProjection
from risk.sidecar_funding_risk import SidecarFundingRiskController
from risk.sidecar_flat_proof import FlatProofEngine, FlatProofError
from risk.sidecar_observation import SidecarObservationController
from risk.sidecar_policy import RiskSidecarPolicy
from risk.runtime_clock import system_runtime_clock
from risk.sidecar_state_projection import (
    apply_state_store_payload,
    build_state_store_payload,
    close_state_store,
    open_state_store,
    persist_state,
)
from risk.sidecar_values import finite_float as _finite_float


_HARD_CLOCK_FAILURE_PREFIXES = (
    "clock_phase_error_kill:",
    "clock_initial_offset_exceeded:",
    "clock_anchor_non_finite",
    "clock_monotonic_regressed",
    "clock_phase_error_non_finite",
    "clock_phase_threshold_invalid",
)
def _clock_failure_requires_kill(reason: str) -> bool:
    normalized = str(reason or "").strip().lower()
    return normalized.startswith(_HARD_CLOCK_FAILURE_PREFIXES)


class RiskSidecarCore:
    """Deterministic sidecar state machine, separated for fault-injection tests."""

    def __init__(
        self,
        exchange,
        settings: dict,
        now: float = None,
        snapshot_worker=None,
        *,
        clock=None,
    ):
        self.clock = clock or system_runtime_clock()
        now = _finite_float(
            self.clock.monotonic() if now is None else now,
            "now",
        )
        if now < 0.0:
            raise ValueError("now must be non-negative")
        self.exchange = exchange
        self.policy = RiskSidecarPolicy.from_settings(
            settings,
            _finite_float,
        )
        account_risk = SidecarAccountRiskController.from_settings(
            self.policy,
            settings,
            _finite_float,
        )
        funding_risk = SidecarFundingRiskController(
            self.policy.funding_guard_policy,
            self.policy.symbols,
            self.policy.exchange_poll_interval_sec,
            now=now,
        )
        self.observation = SidecarObservationController(
            exchange=self.exchange,
            snapshot_worker=snapshot_worker,
            account_risk=account_risk,
            funding_risk=funding_risk,
            exchange_poll_interval_sec=(
                self.policy.exchange_poll_interval_sec
            ),
            exchange_max_age_sec=self.policy.exchange_max_age_sec,
            snapshot_worker_timeout_sec=(
                self.policy.snapshot_worker_timeout_sec
            ),
            clock_failure_requires_kill=_clock_failure_requires_kill,
            wall_time=self.clock.wall_time,
        )
        self.symbols = self.policy.symbols
        self.funding_guard_policy = self.policy.funding_guard_policy
        self.parent_heartbeat_timeout_sec = self.policy.parent_heartbeat_timeout_sec
        self.exchange_poll_interval_sec = self.policy.exchange_poll_interval_sec
        self.exchange_max_age_sec = self.policy.exchange_max_age_sec
        self.snapshot_worker_timeout_sec = self.policy.snapshot_worker_timeout_sec
        self.rearm_snapshot_max_age_sec = self.policy.rearm_snapshot_max_age_sec
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
        self.parent_loss_flatten_delay_sec = self.policy.parent_loss_flatten_delay_sec
        rearm_prepare_ttl_sec = max(
            1.0,
            _finite_float(
                settings.get("rearm_prepare_ttl_sec", 10.0) or 10.0,
                "rearm_prepare_ttl_sec",
            ),
        )
        self.control = SidecarControlController(
            cancel_retry_sec=self.policy.cancel_retry_sec,
            flatten_enabled=self.policy.flatten_enabled,
            flatten_retry_sec=self.policy.flatten_retry_sec,
            flat_verification_checks=self.policy.flat_verification_checks,
            rearm_prepare_ttl_sec=rearm_prepare_ttl_sec,
            token_factory=secrets.token_hex,
            wall_time=self.clock.wall_time,
        )
        self.state_version = StateVersion(0, 0, 0, 0, "")
        self._pending_safety_epoch = 0
        self.last_flat_proof = None
        self.last_flat_proof_error = ""
        self.flat_proof_engine = (
            FlatProofEngine(
                exchange,
                required_samples=self.policy.flat_verification_checks,
                settle_interval_sec=float(
                    settings.get("flat_proof_settle_interval_sec", 0.0)
                    or 0.0
                ),
                proof_ttl_sec=float(
                    settings.get("flat_proof_ttl_sec", 2.0) or 2.0
                ),
                expected_account_scope_id=str(
                    settings.get("account_scope_id", "") or ""
                ),
                allowed_symbols=self.symbols,
                monotonic=self.clock.monotonic,
                sleep=self.clock.sleep,
            )
            if callable(getattr(exchange, "read_account_truth", None))
            else None
        )
        self.started_at = now
        self.last_parent_heartbeat_at = now
        self.last_parent_heartbeat_sent_monotonic = 0.0
        self.last_parent_heartbeat_received_at = 0.0
        self.parent_heartbeat_error = ""
        self.last_parent_sequence = 0
        self.last_cancel_attempt_at = 0.0
        self.last_cancel_ok = None
        self.last_cancel_reason = ""
        self.last_flatten_attempt_at = 0.0
        self.last_flatten_ok = None
        self.last_flatten_count = 0
        self.last_flatten_reason = ""
        self.parent_stale_since = 0.0
        self.parent_stale_snapshot_sequence = 0
        legacy_state_fields = tuple(
            field
            for field in ("state_path", "state_required", "state_fsync")
            if field in settings
        )
        if legacy_state_fields:
            raise ValueError(
                "legacy sidecar runtime state is unsupported: "
                + ",".join(legacy_state_fields)
            )
        self.state_path = ""
        self.state_generation = 0
        self.state_recovered = False
        self.state_load_error = ""
        self.state_persist_error = ""
        self.state_store = None
        state_store_root = str(
            settings.get("state_store_root", "") or ""
        ).strip()
        if state_store_root:
            self._open_state_store(state_store_root, settings)

    def _fail_closed_on_state_error(self, reason: str):
        state = self.control.state
        state.kill_latched = True
        state.kill_reason = str(reason or "sidecar_state_error")
        state.stage = "FAILED"

    def _open_state_store(self, root: str, settings: dict) -> None:
        open_state_store(self, root, settings, _finite_float)

    def _apply_state_store_payload(self, payload: dict) -> None:
        apply_state_store_payload(self, payload, _finite_float)

    def _state_store_payload(self) -> dict:
        return build_state_store_payload(self)

    def _persist_durable_state(self, event: str, force: bool = False) -> bool:
        return persist_state(
            self,
            event,
            force,
            _finite_float,
        )

    def close(self) -> None:
        close_state_store(self)

    def receive_parent_heartbeat(
        self,
        sequence: int,
        sent_monotonic: float = None,
        now: float = None,
    ):
        now = self.clock.monotonic() if now is None else float(now)
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

    def _apply_control_effects(self, effects: ControlEffects) -> None:
        if effects.reset_cancel_retry:
            self.last_cancel_attempt_at = 0.0
        if effects.reset_flatten_retry:
            self.last_flatten_attempt_at = 0.0

    def _persist_control_transition(
        self,
        event: str,
        require_path: bool,
    ) -> tuple[bool, str]:
        if require_path and self.state_store is None:
            self.state_persist_error = "quiesce_state_path_missing"
            self._fail_closed_on_state_error(self.state_persist_error)
            return False, self.state_persist_error
        persisted = self._persist_durable_state(event, force=True)
        return bool(persisted), str(self.state_persist_error or "")

    def _enter_quiesced(self, reason: str, event: str) -> bool:
        persisted, _, effects = self.control.enter_quiesced(
            reason,
            self.observation.risk_snapshot_sequence,
            event,
            self._persist_control_transition,
        )
        self._apply_control_effects(effects)
        return persisted

    def request_quiesce(
        self,
        request_id: str,
        reason: str,
    ):
        accepted, result_reason, effects = self.control.request_quiesce(
            request_id,
            reason,
            self.observation.risk_snapshot_sequence,
            self._persist_control_transition,
        )
        self._apply_control_effects(effects)
        return accepted, result_reason

    def _takeover_from_quiesce(self, reason: str, event: str) -> bool:
        persisted, _, effects = self.control.takeover_from_quiesce(
            reason,
            event,
            self._persist_control_transition,
        )
        self._apply_control_effects(effects)
        return persisted

    def request_shutdown_resume(
        self,
        request_id: str,
        reason: str,
    ):
        accepted, result_reason, effects = (
            self.control.request_shutdown_resume(
                request_id,
                reason,
                self._persist_control_transition,
            )
        )
        self._apply_control_effects(effects)
        return accepted, result_reason

    def request_stop(
        self,
        request_id: str,
        cancel_orders: bool = True,
    ):
        self.control.request_stop(request_id, cancel_orders)

    def _complete_stop_request(self, now: float) -> bool | None:
        return complete_stop_request(self, now)

    def _check_rearm_safety(self, now: float):
        return check_rearm_safety(self, now)

    def prepare_rearm(self, request_id: str, reason: str, now: float = None):
        return run_prepare_rearm(self, request_id, reason, now)

    def _rearm_proof_binding(self, now: float | None = None) -> tuple | None:
        return rearm_proof_binding(self, now)

    def commit_rearm(
        self,
        request_id: str,
        token: str,
        now: float = None,
    ):
        return run_commit_rearm(self, request_id, token, now)

    def abort_rearm(self, token: str):
        return self.control.abort_rearm(token)

    def _evaluate_funding_guard(self, now: float):
        return self.observation.evaluate_funding_guard(now)

    def _mark_exchange_snapshot_unhealthy(self, reason: str):
        self.observation.mark_unhealthy(reason)

    def _snapshot_wall_time(self, snapshot: dict, fallback: float) -> float:
        return self.observation.snapshot_wall_time(snapshot, fallback)

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
        self.observation.apply_result(
            healthy=healthy,
            snapshot=snapshot,
            reason=reason,
            completed_monotonic=completed_monotonic,
            completed_at=completed_at,
            full_snapshot=full_snapshot,
        )

    def _poll_exchange_risk(self, now: float):
        self.observation.poll(now)

    def _service_snapshot_worker(self, now: float, force: bool = False):
        self.observation.service_worker(now, force=force)

    def _service_exchange_risk(self, now: float, force: bool = False):
        self.observation.service(now, force=force)

    def _exchange_snapshot_valid(self, now: float) -> bool:
        return self.observation.snapshot_valid(now)

    def _account_truth_counts(self):
        return self.observation.account_truth_counts()

    def _bump_safety_epoch(self) -> None:
        version = self.state_version
        self._pending_safety_epoch = max(
            self._pending_safety_epoch,
            version.safety_epoch + 1,
        )
        self.last_flat_proof = None

    def _effective_state_version(self) -> StateVersion:
        version = self.state_version
        if self._pending_safety_epoch <= version.safety_epoch:
            return version
        return StateVersion(
            writer_epoch=version.writer_epoch,
            owner_epoch=version.owner_epoch,
            safety_epoch=self._pending_safety_epoch,
            generation=version.generation,
            state_sha256=version.state_sha256,
        )

    def _begin_exchange_action(self, event: str) -> bool:
        self._bump_safety_epoch()
        return self._persist_durable_state(event, force=True)

    def _capture_account_flat_proof(
        self,
        purpose: str,
        barrier_monotonic: float,
    ) -> bool:
        engine = self.flat_proof_engine
        if engine is None:
            return True
        try:
            proof = engine.capture(
                purpose=purpose,
                deployment_id=(
                    self.observation.account_risk.state.deployment_id
                ),
                version=self._effective_state_version(),
                barrier_monotonic=barrier_monotonic,
            )
        except FlatProofError as exc:
            self.last_flat_proof = None
            self.last_flat_proof_error = str(exc)
            return False
        version = self._effective_state_version()
        store = self.state_store
        expected_scope = (
            store.account_scope_id if store is not None else ""
        )
        if (
            proof.deployment_id
            != self.observation.account_risk.state.deployment_id
            or (expected_scope and proof.account_scope_id != expected_scope)
            or not proof.is_valid(proof.verified_monotonic, version)
        ):
            self.last_flat_proof = None
            self.last_flat_proof_error = "flat_proof_binding_invalid"
            return False
        self.last_flat_proof = proof
        self.last_flat_proof_error = ""
        return True

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
                self.observation.risk_snapshot_sequence
            )
            self.control.reset_flat_verification()

    def _emergency_cancel(self, now: float):
        if self.control.state.quiesced:
            self.last_cancel_reason = "supervisor_quiesced"
            return False
        self.last_cancel_attempt_at = now
        if not self._begin_exchange_action("emergency_cancel_started"):
            self.last_cancel_ok = False
            self.last_cancel_reason = (
                self.state_persist_error
                or "cancel_started_state_persist_failed"
            )
            return False
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
        if not self._persist_durable_state(
            "emergency_cancel_completed",
            force=True,
        ):
            self.last_cancel_ok = False
            self.last_cancel_reason = (
                self.state_persist_error
                or "cancel_completed_state_persist_failed"
            )
        return self.last_cancel_ok

    def _emergency_flatten(self, now: float):
        if self.control.state.quiesced:
            self.last_flatten_reason = "supervisor_quiesced"
            return False
        self.last_flatten_attempt_at = now
        if not self._begin_exchange_action("emergency_flatten_started"):
            self.last_flatten_ok = False
            self.last_flatten_count = 0
            self.last_flatten_reason = (
                self.state_persist_error
                or "flatten_started_state_persist_failed"
            )
            return False
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
        if not self._persist_durable_state(
            "emergency_flatten_completed",
            force=True,
        ):
            self.last_flatten_ok = False
            self.last_flatten_reason = (
                self.state_persist_error
                or "flatten_completed_state_persist_failed"
            )
        return self.last_flatten_ok

    def _step_quiesced(self, now: float):
        control_state = self.control.state
        truth = self.observation
        self._service_exchange_risk(
            now,
            force=(
                truth.risk_snapshot_sequence
                <= control_state.quiesce_snapshot_sequence
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

        takeover_reason = self.control.quiesce_takeover_reason(
            parent_healthy=parent_healthy,
            parent_error=self.parent_heartbeat_error,
            exchange_valid=exchange_valid,
            exchange_reason=truth.exchange_reason,
            open_order_count=open_order_count,
            nonzero_position_count=nonzero_position_count,
        )

        if takeover_reason:
            self._takeover_from_quiesce(
                takeover_reason,
                "supervisor_quiesce_safety_takeover",
            )
            self.control.fail_pending_stop_after_takeover(takeover_reason)
            return self.step(now, exchange_serviced=True)

        reason = "supervisor_quiesced"
        action = (
            "KILL" if control_state.kill_latched else "REDUCE_ONLY"
        )
        return self._status(False, reason, action, now), True

    def step(
        self,
        now: float = None,
        *,
        exchange_serviced: bool = False,
    ):
        now = self.clock.monotonic() if now is None else float(now)
        control_state = self.control.state
        truth = self.observation
        if control_state.stop_requested:
            stop_accepted = self._complete_stop_request(now)
            if stop_accepted is not None:
                return self._status(
                    False,
                    control_state.last_stop_reason
                    or "supervisor_stop_failed",
                    (
                        "KILL"
                        if control_state.kill_latched
                        else "REDUCE_ONLY"
                    ),
                    now,
                ), not stop_accepted
        if control_state.quiesced:
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
            truth.risk_action
            if exchange_valid or truth.risk_action == "KILL"
            else "REDUCE_ONLY"
        )
        if action == "NONE" and funding_action != "NONE":
            action = funding_action
        if not parent_healthy:
            parent_stale_age = max(0.0, now - self.parent_stale_since)
            if action == "KILL":
                reason = (
                    truth.risk_reason or "independent_hard_risk_breach"
                )
            elif (
                self.control.flatten_enabled
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
                truth.risk_reason
                if action == "KILL"
                else truth.exchange_reason
            ) or "exchange_health_stale"
        elif action != "NONE":
            reason = (
                truth.risk_reason
                if truth.risk_action != "NONE"
                else funding_reason
            ) or "independent_risk_breach"
        else:
            reason = ""

        if action == "KILL" and not control_state.kill_latched:
            self.control.latch_kill(
                reason
                or truth.risk_reason
                or "independent_hard_risk_breach"
            )
        if control_state.kill_latched:
            action = "KILL"
            reason = (
                control_state.kill_reason or "independent_hard_risk_breach"
            )

        healthy = parent_healthy and exchange_valid and action == "NONE"
        open_order_count, nonzero_position_count = (
            self._account_truth_counts()
        )
        stage_actions = self.control.advance_risk_stage(
            healthy=healthy,
            action=action,
            now=now,
            parent_healthy=parent_healthy,
            exchange_valid=exchange_valid,
            open_order_count=open_order_count,
            nonzero_position_count=nonzero_position_count,
            risk_snapshot_sequence=truth.risk_snapshot_sequence,
            parent_stale_snapshot_sequence=(
                self.parent_stale_snapshot_sequence
            ),
            last_cancel_attempt_at=self.last_cancel_attempt_at,
            last_flatten_attempt_at=self.last_flatten_attempt_at,
        )
        if stage_actions.cancel:
            self._emergency_cancel(now)
        if stage_actions.flatten:
            self._emergency_flatten(now)

        if not self._persist_durable_state("risk_state_transition"):
            healthy = False
            action = "KILL"
            reason = self.state_persist_error or "state_persist_failed"

        keep_running = self.control.should_keep_running(
            parent_healthy=parent_healthy,
            parent_stale_since=self.parent_stale_since,
            now=now,
            orphan_exit_sec=self.orphan_exit_sec,
            exchange_valid=exchange_valid,
            open_order_count=open_order_count,
            nonzero_position_count=nonzero_position_count,
            risk_snapshot_sequence=truth.risk_snapshot_sequence,
            parent_stale_snapshot_sequence=(
                self.parent_stale_snapshot_sequence
            ),
        )
        if not keep_running and self.flat_proof_engine is not None:
            barrier = max(
                self.parent_stale_since,
                self.last_cancel_attempt_at,
                self.last_flatten_attempt_at,
            )
            if not self._capture_account_flat_proof(
                "ORPHAN_EXIT",
                barrier,
            ):
                self.control.reset_flat_verification()
                control_state.stage = "FLATTENING"
                keep_running = True
                reason = (
                    self.last_flat_proof_error
                    or "account_wide_flat_proof_failed"
                )
            elif not self._persist_durable_state(
                "account_wide_flat_proof",
                force=True,
            ):
                keep_running = True
                healthy = False
                action = "KILL"
                reason = self.state_persist_error or "state_persist_failed"
        return self._status(healthy, reason, action, now), keep_running

    def _status(self, healthy: bool, reason: str, action: str, now: float):
        return RiskSidecarStatusProjection.build(
            self,
            healthy,
            reason,
            action,
            now,
        )
