"""Deterministic child-side risk supervision state machine."""

import math
import os
import secrets
import time
from dataclasses import asdict

from risk.exchange_port import FlatProof, StateVersion
from risk.sidecar_account_risk import SidecarAccountRiskController
from risk.sidecar_control_state import ControlEffects, SidecarControlController
from risk.sidecar_core_fields import RiskSidecarCompatibilityFields
from risk.sidecar_core_status import RiskSidecarStatusProjection
from risk.sidecar_durable_state import SidecarDurableState
from risk.sidecar_funding_risk import SidecarFundingRiskController
from risk.sidecar_flat_proof import FlatProofEngine, FlatProofError
from risk.sidecar_observation import SidecarObservationController
from risk.sidecar_policy import RiskSidecarPolicy
from risk.sidecar_state_store import (
    SidecarStateStore,
    SidecarStateStoreError,
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
STATE_REPLACE_MAX_ATTEMPTS = 5
STATE_REPLACE_RETRY_BASE_SEC = 0.01
TRANSIENT_WINDOWS_REPLACE_ERRORS = frozenset({5, 32, 33})


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


def _clock_failure_requires_kill(reason: str) -> bool:
    normalized = str(reason or "").strip().lower()
    return normalized.startswith(_HARD_CLOCK_FAILURE_PREFIXES)


class RiskSidecarCore(RiskSidecarCompatibilityFields):
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
            wall_time=time.time,
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
            wall_time=time.time,
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
        self.state_path = str(settings.get("state_path", "") or "").strip()
        self.state_required = bool(settings.get("state_required", False))
        self.state_fsync = bool(settings.get("state_fsync", True))
        self.state_generation = 0
        self.state_recovered = False
        self.state_load_error = ""
        self.state_persist_error = ""
        self._last_persisted_fingerprint = None
        self.state_store = None
        state_store_root = str(
            settings.get("state_store_root", "") or ""
        ).strip()
        if state_store_root:
            if self.state_path:
                raise ValueError(
                    "state_store_root and legacy state_path are mutually exclusive"
                )
            self._open_state_store(state_store_root, settings)
        else:
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

    def _open_state_store(self, root: str, settings: dict) -> None:
        account_scope_id = str(
            settings.get("account_scope_id", "") or ""
        ).strip()
        genesis_id = str(
            settings.get("state_genesis_id", "") or ""
        ).strip()
        if not account_scope_id or not genesis_id or not self.deployment_id:
            raise ValueError("state_store_identity_missing")
        store = SidecarStateStore(
            root,
            account_scope_id=account_scope_id,
            deployment_id=self.deployment_id,
            genesis_id=genesis_id,
            writer_id=str(
                settings.get("session_id", "") or f"pid-{os.getpid()}"
            ),
        )
        payload, version = store.open_recover()
        try:
            self._apply_state_store_payload(payload)
        except Exception:
            store.close()
            raise
        self.state_store = store
        self.state_version = version
        self.state_generation = version.generation
        self.state_recovered = True
        self.state_load_error = ""

    def _apply_state_store_payload(self, payload: dict) -> None:
        if int(payload.get("schema_version", 0) or 0) != 2:
            raise SidecarStateStoreError("state_payload_schema_unsupported")
        kill_latched = payload.get("kill_latched")
        quiesced = payload.get("quiesced", False)
        if not isinstance(kill_latched, bool) or not isinstance(quiesced, bool):
            raise SidecarStateStoreError("state_payload_boolean_invalid")
        self.kill_latched = kill_latched
        self.kill_reason = str(payload.get("kill_reason", "") or "")
        self.quiesced = quiesced
        self.quiesce_reason = str(
            payload.get("quiesce_reason", "") or ""
        )
        self.quiesced_at = _finite_float(
            payload.get("quiesced_at", 0.0) or 0.0,
            "state.quiesced_at",
        )
        self.stage = (
            "QUIESCED"
            if quiesced
            else str(
                payload.get(
                    "stage",
                    "FLATTENING" if kill_latched else "ARMED",
                )
                or "FAILED"
            )
        )
        for field in (
            "day_start_equity",
            "day_start_external_cash_flow_total",
            "peak_adjusted_equity",
            "last_equity",
            "deployment_start_equity",
            "deployment_start_external_cash_flow_total",
            "deployment_adjusted_equity",
            "deployment_loss",
        ):
            if field in payload:
                setattr(
                    self,
                    field,
                    _finite_float(payload[field], f"state.{field}"),
                )
        if "risk_day" in payload:
            self.risk_day = str(payload["risk_day"] or "")
        proof_payload = payload.get("last_flat_proof")
        if isinstance(proof_payload, dict):
            self.last_flat_proof = FlatProof(**proof_payload)

    def _state_store_payload(self) -> dict:
        store = self.state_store
        return {
            "schema_version": 2,
            "account_scope_id": (
                store.account_scope_id if store is not None else ""
            ),
            "deployment_id": self.deployment_id,
            "kill_latched": bool(self.kill_latched),
            "kill_reason": str(self.kill_reason or ""),
            "stage": str(self.stage or ""),
            "quiesced": bool(self.quiesced),
            "quiesce_reason": str(self.quiesce_reason or ""),
            "quiesced_at": float(self.quiesced_at),
            "risk_day": str(self.risk_day or ""),
            "day_start_equity": float(self.day_start_equity),
            "day_start_external_cash_flow_total": float(
                self.day_start_external_cash_flow_total
            ),
            "peak_adjusted_equity": float(self.peak_adjusted_equity),
            "last_equity": float(self.last_equity),
            "deployment_start_equity": float(
                self.deployment_start_equity
            ),
            "deployment_start_external_cash_flow_total": float(
                self.deployment_start_external_cash_flow_total
            ),
            "deployment_adjusted_equity": float(
                self.deployment_adjusted_equity
            ),
            "deployment_loss": float(self.deployment_loss),
            "last_flat_proof": (
                asdict(self.last_flat_proof)
                if self.last_flat_proof is not None
                else None
            ),
        }

    def _persist_durable_state(self, event: str, force: bool = False) -> bool:
        if self.state_store is not None:
            operation_class = (
                "RISK_INCREASING"
                if "rearm" in str(event or "").lower()
                else "SAFETY_INCREASING"
                if any(
                    marker in str(event or "").lower()
                    for marker in ("kill", "flat", "cancel", "failed")
                )
                else "NEUTRAL"
            )
            try:
                version = self.state_store.compare_and_swap(
                    self.state_version,
                    self._state_store_payload(),
                    event=event,
                    operation_class=operation_class,
                    safety_epoch=max(
                        self.state_version.safety_epoch,
                        self._pending_safety_epoch,
                    ),
                )
            except SidecarStateStoreError as exc:
                self.state_persist_error = (
                    f"state_store_persist_failed:{type(exc).__name__}:{exc}"
                )
                self._fail_closed_on_state_error(self.state_persist_error)
                return False
            self.state_version = version
            self._pending_safety_epoch = version.safety_epoch
            self.state_generation = version.generation
            self.state_persist_error = ""
            return True
        return SidecarDurableState(
            self,
            _finite_float,
            _replace_state_file,
        ).persist(event, force)

    def close(self) -> None:
        if self.state_store is not None:
            self.state_store.close()
            self.state_store = None

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
        if require_path and not self.state_path:
            self.state_persist_error = "quiesce_state_path_missing"
            self._fail_closed_on_state_error(self.state_persist_error)
            return False, self.state_persist_error
        persisted = self._persist_durable_state(event, force=True)
        return bool(persisted), str(self.state_persist_error or "")

    def _enter_quiesced(self, reason: str, event: str) -> bool:
        persisted, _, effects = self.control.enter_quiesced(
            reason,
            self.risk_snapshot_sequence,
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
            self.risk_snapshot_sequence,
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
            proof_sequence = self.risk_snapshot_sequence
            if self.flat_proof_engine is not None:
                exchange_valid = self._capture_account_flat_proof(
                    "STOP",
                    now,
                )
                open_order_count, nonzero_position_count = 0, 0
                if exchange_valid:
                    proof_sequence = max(
                        self.risk_snapshot_sequence,
                        self.quiesce_snapshot_sequence + 1,
                    )
                else:
                    self.exchange_reason = (
                        self.last_flat_proof_error
                        or "account_wide_flat_proof_failed"
                    )
            else:
                open_order_count, nonzero_position_count = (
                    self._account_truth_counts()
                )
        else:
            exchange_valid = False
            open_order_count = 0
            nonzero_position_count = 0
            proof_sequence = self.risk_snapshot_sequence
        plan = self.control.stop_plan(
            exchange_valid=exchange_valid,
            exchange_reason=self.exchange_reason,
            open_order_count=open_order_count,
            nonzero_position_count=nonzero_position_count,
            risk_snapshot_sequence=proof_sequence,
        )

        if plan.action == "WAIT":
            return None
        if plan.action == "TAKEOVER":
            self._takeover_from_quiesce(
                plan.transition_reason or plan.reason,
                "supervisor_stop_guard_takeover",
            )
            reason = plan.reason
        elif plan.action == "QUIESCE":
            persisted = self._enter_quiesced(
                plan.reason,
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
        elif plan.action == "CANCEL":
            cancel_attempted = True
            cancel_ok = self._emergency_cancel(now)
            reason = (
                "stop_after_cancel_requires_fresh_quiesce"
                if cancel_ok
                else self.last_cancel_reason or "stop_cancel_failed"
            )
        else:
            reason = plan.reason

        self.control.finish_stop(
            accepted=accepted,
            reason=reason,
            cancel_requested=cancel_requested,
            cancel_attempted=cancel_attempted,
            cancel_ok=cancel_ok,
        )
        return accepted

    def _check_rearm_safety(self, now: float):
        gate = self.control.rearm_control_gate(
            parent_heartbeat_error=self.parent_heartbeat_error,
            parent_age_sec=max(0.0, now - self.last_parent_heartbeat_at),
            parent_timeout_sec=self.parent_heartbeat_timeout_sec,
        )
        if not gate[0]:
            return gate
        self._service_exchange_risk(now, force=True)
        if self.snapshot_worker is None:
            snapshot_fresh = self.last_exchange_success_at == now
            snapshot_pending_reason = "exchange_snapshot_failed"
        else:
            snapshot_fresh = bool(
                self.last_exchange_success_at > 0.0
                and now - self.last_exchange_success_at
                <= self.rearm_snapshot_max_age_sec
            )
            snapshot_pending_reason = "exchange_snapshot_refresh_pending"
        funding_action, funding_reason = self._evaluate_funding_guard(now)
        if not self._capture_account_flat_proof("REARM", now):
            return (
                False,
                self.last_flat_proof_error
                or "account_wide_flat_proof_failed",
            )
        if self.flat_proof_engine is not None:
            open_order_count, nonzero_position_count = 0, 0
        else:
            open_order_count, nonzero_position_count = (
                self._account_truth_counts()
            )
        return self.control.rearm_truth_gate(
            exchange_healthy=self.exchange_healthy,
            exchange_reason=self.exchange_reason,
            snapshot_fresh=snapshot_fresh,
            snapshot_pending_reason=snapshot_pending_reason,
            risk_action=self.risk_action,
            risk_reason=self.risk_reason,
            funding_action=funding_action,
            funding_reason=funding_reason,
            open_order_count=open_order_count,
            nonzero_position_count=nonzero_position_count,
        )

    def prepare_rearm(self, request_id: str, reason: str, now: float = None):
        now = time.perf_counter() if now is None else float(now)
        request_id = str(request_id or "")
        safety = (
            self._check_rearm_safety(now)
            if request_id
            else (False, "request_id_missing")
        )
        return self.control.prepare_rearm(
            request_id,
            reason,
            now,
            safety,
            self._rearm_proof_binding(),
        )

    def _rearm_proof_binding(self) -> tuple | None:
        proof = self.last_flat_proof
        if proof is None:
            return None
        version = self._effective_state_version()
        return (
            version.writer_epoch,
            version.owner_epoch,
            version.safety_epoch,
            version.generation,
            version.state_sha256,
            proof.proof_id,
            proof.last_truth_sequence,
            proof.snapshot_digest,
        )

    def commit_rearm(
        self,
        request_id: str,
        token: str,
        now: float = None,
    ):
        now = time.perf_counter() if now is None else float(now)
        request_id = str(request_id or "")
        valid, refusal_reason = self.control.validate_rearm_commit(
            request_id,
            token,
            now,
            self._rearm_proof_binding(),
        )
        if not valid:
            return False, refusal_reason
        safe, refusal_reason = self._check_rearm_safety(now)
        if not safe:
            return self.control.reject_rearm_commit(
                request_id,
                refusal_reason,
            )

        checkpoint = self.control.begin_rearm_commit()
        previous_risk_action = self.risk_action
        previous_risk_reason = self.risk_reason
        self.risk_action = "NONE"
        self.risk_reason = ""
        persisted = self._persist_durable_state(
            "operator_rearm_committed",
            force=True,
        )
        if not persisted:
            self.risk_action = previous_risk_action
            self.risk_reason = previous_risk_reason
            return self.control.finish_rearm_commit(
                request_id,
                False,
                self.state_persist_error or "state_persist_failed",
                checkpoint,
            )
        self.state_load_error = ""
        return self.control.finish_rearm_commit(
            request_id,
            True,
            "rearm_committed",
            checkpoint,
        )

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
                deployment_id=self.deployment_id,
                version=self._effective_state_version(),
                barrier_monotonic=barrier_monotonic,
            )
        except FlatProofError as exc:
            self.last_flat_proof = None
            self.last_flat_proof_error = str(exc)
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
                self.risk_snapshot_sequence
            )
            self.control.reset_flat_verification()

    def _emergency_cancel(self, now: float):
        if self.quiesced:
            self.last_cancel_reason = "supervisor_quiesced"
            return False
        self._bump_safety_epoch()
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
        self._bump_safety_epoch()
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

        takeover_reason = self.control.quiesce_takeover_reason(
            parent_healthy=parent_healthy,
            parent_error=self.parent_heartbeat_error,
            exchange_valid=exchange_valid,
            exchange_reason=self.exchange_reason,
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
            self.control.latch_kill(
                reason
                or self.risk_reason
                or "independent_hard_risk_breach"
            )
        if self.kill_latched:
            action = "KILL"
            reason = self.kill_reason or "independent_hard_risk_breach"

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
            risk_snapshot_sequence=self.risk_snapshot_sequence,
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
            risk_snapshot_sequence=self.risk_snapshot_sequence,
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
                self.stage = "FLATTENING"
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
