"""Control, quiesce, stop, and rearm state for the risk sidecar."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import secrets
import time


PersistTransition = Callable[[str, bool], tuple[bool, str]]


@dataclass(slots=True)
class SidecarControlState:
    kill_latched: bool = False
    kill_reason: str = ""
    stage: str = "ARMED"
    unsafe_since: float = 0.0
    flat_verification_count: int = 0
    last_verified_snapshot_sequence: int = 0
    quiesced: bool = False
    quiesce_reason: str = ""
    quiesced_at: float = 0.0
    quiesce_snapshot_sequence: int = 0
    stop_requested: bool = False
    stop_request_id: str = ""
    cancel_on_stop: bool = True
    last_quiesce_request_id: str = ""
    last_quiesce_accepted: bool | None = None
    last_quiesce_reason: str = ""
    last_quiesce_persisted: bool = False
    last_shutdown_resume_request_id: str = ""
    last_shutdown_resume_accepted: bool | None = None
    last_shutdown_resume_reason: str = ""
    last_shutdown_resume_persisted: bool = False
    last_stop_request_id: str = ""
    last_stop_accepted: bool | None = None
    last_stop_reason: str = ""
    last_stop_quiesced: bool = False
    last_stop_cancel_requested: bool = False
    last_stop_cancel_attempted: bool = False
    last_stop_cancel_ok: bool | None = None
    prepared_rearm: dict | None = None
    last_rearm_request_id: str = ""
    last_rearm_phase: str = ""
    last_rearm_accepted: bool | None = None
    last_rearm_reason: str = ""
    last_rearm_token: str = ""


@dataclass(frozen=True, slots=True)
class ControlEffects:
    reset_cancel_retry: bool = False
    reset_flatten_retry: bool = False


@dataclass(frozen=True, slots=True)
class StopPlan:
    action: str
    reason: str = ""
    transition_reason: str = ""


@dataclass(frozen=True, slots=True)
class RiskStageActions:
    cancel: bool = False
    flatten: bool = False


@dataclass(frozen=True, slots=True)
class RearmCheckpoint:
    kill_latched: bool
    kill_reason: str
    stage: str
    flat_verification_count: int
    last_verified_snapshot_sequence: int


class SidecarControlController:
    """Own sidecar control state without retaining the runtime core."""

    __slots__ = (
        "_token_factory",
        "_wall_time",
        "cancel_retry_sec",
        "flat_verification_checks",
        "flatten_enabled",
        "flatten_retry_sec",
        "generation",
        "rearm_prepare_ttl_sec",
        "state",
    )

    def __init__(
        self,
        *,
        cancel_retry_sec: float,
        flatten_enabled: bool,
        flatten_retry_sec: float,
        flat_verification_checks: int,
        rearm_prepare_ttl_sec: float,
        token_factory: Callable[[int], str] = secrets.token_hex,
        wall_time: Callable[[], float] = time.time,
    ):
        self.cancel_retry_sec = float(cancel_retry_sec)
        self.flatten_enabled = bool(flatten_enabled)
        self.flatten_retry_sec = float(flatten_retry_sec)
        self.flat_verification_checks = int(flat_verification_checks)
        self.rearm_prepare_ttl_sec = float(rearm_prepare_ttl_sec)
        self._token_factory = token_factory
        self._wall_time = wall_time
        self.generation = 0
        self.state = SidecarControlState()

    def _bump_if_changed(self, before: tuple) -> None:
        state = self.state
        after = (
            state.kill_latched,
            state.kill_reason,
            state.stage,
            bool(
                state.stage == "FLAT_VERIFIED"
                and state.flat_verification_count
                >= self.flat_verification_checks
            ),
            state.quiesced,
            state.quiesce_reason,
            state.quiesced_at,
            state.quiesce_snapshot_sequence,
        )
        if after != before:
            self.generation += 1

    def _transition_snapshot(self) -> tuple:
        state = self.state
        return (
            state.kill_latched,
            state.kill_reason,
            state.stage,
            bool(
                state.stage == "FLAT_VERIFIED"
                and state.flat_verification_count
                >= self.flat_verification_checks
            ),
            state.quiesced,
            state.quiesce_reason,
            state.quiesced_at,
            state.quiesce_snapshot_sequence,
        )

    def reset_flat_verification(self) -> None:
        before = self._transition_snapshot()
        self.state.flat_verification_count = 0
        self.state.last_verified_snapshot_sequence = 0
        self._bump_if_changed(before)

    def latch_kill(self, reason: str) -> None:
        state = self.state
        if state.kill_latched:
            return
        before = self._transition_snapshot()
        state.kill_latched = True
        state.kill_reason = str(reason or "independent_hard_risk_breach")
        self._bump_if_changed(before)

    def _record_quiesce(
        self,
        request_id: str,
        accepted: bool,
        reason: str,
        persisted: bool,
    ) -> None:
        state = self.state
        state.last_quiesce_request_id = str(request_id or "")
        state.last_quiesce_accepted = bool(accepted)
        state.last_quiesce_reason = str(reason or "")
        state.last_quiesce_persisted = bool(persisted)

    def _begin_quiesced(
        self,
        reason: str,
        snapshot_sequence: int,
    ) -> None:
        state = self.state
        before = self._transition_snapshot()
        state.quiesced = True
        state.quiesce_reason = str(reason or "operator_quiesce")
        if state.quiesced_at <= 0.0:
            state.quiesced_at = float(self._wall_time())
        state.quiesce_snapshot_sequence = int(snapshot_sequence)
        state.stage = "QUIESCED"
        state.prepared_rearm = None
        self._bump_if_changed(before)

    def enter_quiesced(
        self,
        reason: str,
        snapshot_sequence: int,
        event: str,
        persist: PersistTransition,
    ) -> tuple[bool, str, ControlEffects]:
        self._begin_quiesced(reason, snapshot_sequence)
        persisted, persist_error = persist(event, True)
        if persisted:
            return True, "", ControlEffects()

        state = self.state
        before = self._transition_snapshot()
        state.quiesced = False
        state.quiesce_reason = ""
        state.quiesced_at = 0.0
        state.quiesce_snapshot_sequence = 0
        state.kill_latched = True
        state.kill_reason = str(
            persist_error or "quiesce_state_persist_failed"
        )
        state.stage = "FAILED"
        state.prepared_rearm = None
        state.flat_verification_count = 0
        state.last_verified_snapshot_sequence = 0
        self._bump_if_changed(before)
        return (
            False,
            state.kill_reason,
            ControlEffects(True, True),
        )

    def request_quiesce(
        self,
        request_id: str,
        reason: str,
        snapshot_sequence: int,
        persist: PersistTransition,
    ) -> tuple[bool, str, ControlEffects]:
        request_id = str(request_id or "")
        if not request_id:
            self._record_quiesce(
                request_id,
                False,
                "request_id_missing",
                False,
            )
            return False, "request_id_missing", ControlEffects()

        persisted, persist_error, effects = self.enter_quiesced(
            str(reason or "operator_quiesce"),
            snapshot_sequence,
            "supervisor_quiesced",
            persist,
        )
        accepted = bool(persisted)
        result_reason = (
            "supervisor_quiesced"
            if accepted
            else persist_error or "quiesce_state_persist_failed"
        )
        self._record_quiesce(
            request_id,
            accepted,
            result_reason,
            persisted,
        )
        return accepted, result_reason, effects

    def takeover_from_quiesce(
        self,
        reason: str,
        event: str,
        persist: PersistTransition,
    ) -> tuple[bool, str, ControlEffects]:
        state = self.state
        before = self._transition_snapshot()
        state.quiesced = False
        state.quiesce_reason = ""
        state.quiesced_at = 0.0
        state.quiesce_snapshot_sequence = 0
        state.kill_latched = True
        state.kill_reason = str(reason or "quiesce_safety_takeover")
        state.stage = "FLATTENING"
        state.prepared_rearm = None
        state.flat_verification_count = 0
        state.last_verified_snapshot_sequence = 0
        self._bump_if_changed(before)
        persisted, persist_error = persist(event, False)
        return (
            bool(persisted),
            str(persist_error or ""),
            ControlEffects(True, True),
        )

    def _record_shutdown_resume(
        self,
        request_id: str,
        accepted: bool,
        reason: str,
        persisted: bool,
    ) -> None:
        state = self.state
        state.last_shutdown_resume_request_id = str(request_id or "")
        state.last_shutdown_resume_accepted = bool(accepted)
        state.last_shutdown_resume_reason = str(reason or "")
        state.last_shutdown_resume_persisted = bool(persisted)

    def request_shutdown_resume(
        self,
        request_id: str,
        reason: str,
        persist: PersistTransition,
    ) -> tuple[bool, str, ControlEffects]:
        request_id = str(request_id or "")
        if not request_id:
            self._record_shutdown_resume(
                request_id,
                False,
                "request_id_missing",
                False,
            )
            return False, "request_id_missing", ControlEffects()

        persisted, persist_error, effects = self.takeover_from_quiesce(
            str(reason or "shutdown account truth drift"),
            "supervisor_shutdown_guard_resumed",
            persist,
        )
        accepted = bool(persisted)
        result_reason = (
            "supervisor_shutdown_guard_resumed"
            if accepted
            else persist_error or "shutdown_resume_state_persist_failed"
        )
        self._record_shutdown_resume(
            request_id,
            accepted,
            result_reason,
            persisted,
        )
        return accepted, result_reason, effects

    def request_stop(self, request_id: str, cancel_orders: bool) -> None:
        state = self.state
        state.stop_requested = True
        state.stop_request_id = str(request_id or "")
        state.cancel_on_stop = bool(cancel_orders)

    def stop_plan(
        self,
        *,
        exchange_valid: bool,
        exchange_reason: str,
        open_order_count: int,
        nonzero_position_count: int,
        risk_snapshot_sequence: int,
    ) -> StopPlan:
        state = self.state
        if state.quiesced:
            if not exchange_valid:
                reason = str(
                    exchange_reason or "stop_exchange_truth_stale"
                )
                return StopPlan(
                    "TAKEOVER",
                    reason,
                    f"stop_guard_exchange_truth_invalid:{reason}",
                )
            if open_order_count or nonzero_position_count:
                return StopPlan(
                    "TAKEOVER",
                    "stop_guard_account_not_flat:"
                    f"open_orders={open_order_count}:"
                    f"positions={nonzero_position_count}",
                    "stop_guard_account_not_flat:"
                    f"open_orders={open_order_count}:"
                    f"positions={nonzero_position_count}",
                )
            if risk_snapshot_sequence <= state.quiesce_snapshot_sequence:
                return StopPlan("WAIT")
            return StopPlan(
                "QUIESCE",
                state.quiesce_reason or "stop_after_flat_snapshot",
            )
        if state.cancel_on_stop:
            return StopPlan("CANCEL")
        return StopPlan("REJECT", "stop_without_cancel_requires_quiesced")

    def finish_stop(
        self,
        *,
        accepted: bool,
        reason: str,
        cancel_requested: bool,
        cancel_attempted: bool,
        cancel_ok: bool | None,
    ) -> None:
        state = self.state
        state.last_stop_request_id = str(state.stop_request_id or "")
        state.last_stop_accepted = bool(accepted)
        state.last_stop_reason = str(reason or "")
        state.last_stop_quiesced = bool(state.quiesced)
        state.last_stop_cancel_requested = bool(cancel_requested)
        state.last_stop_cancel_attempted = bool(cancel_attempted)
        state.last_stop_cancel_ok = (
            None if cancel_ok is None else bool(cancel_ok)
        )
        state.stop_requested = False

    def fail_pending_stop_after_takeover(self, reason: str) -> None:
        state = self.state
        if not state.stop_requested:
            return
        self.finish_stop(
            accepted=False,
            reason=f"stop_guard_takeover:{reason}",
            cancel_requested=bool(state.cancel_on_stop),
            cancel_attempted=False,
            cancel_ok=None,
        )

    def quiesce_takeover_reason(
        self,
        *,
        parent_healthy: bool,
        parent_error: str,
        exchange_valid: bool,
        exchange_reason: str,
        open_order_count: int,
        nonzero_position_count: int,
    ) -> str:
        if not parent_healthy:
            return str(parent_error or "quiesce_parent_heartbeat_stale")
        if not exchange_valid:
            return str(exchange_reason or "quiesce_exchange_truth_stale")
        if open_order_count or nonzero_position_count:
            return (
                "quiesce_account_truth_drift:"
                f"open_orders={open_order_count}:"
                f"positions={nonzero_position_count}"
            )
        return ""

    def _record_rearm(
        self,
        request_id: str,
        phase: str,
        accepted: bool,
        reason: str,
        token: str = "",
    ) -> None:
        state = self.state
        state.last_rearm_request_id = str(request_id or "")
        state.last_rearm_phase = str(phase or "")
        state.last_rearm_accepted = bool(accepted)
        state.last_rearm_reason = str(reason or "")
        state.last_rearm_token = str(token or "")

    def rearm_control_gate(
        self,
        *,
        parent_heartbeat_error: str,
        parent_age_sec: float,
        parent_timeout_sec: float,
    ) -> tuple[bool, str]:
        state = self.state
        if state.quiesced:
            return False, "supervisor_quiesced"
        if not state.kill_latched:
            return False, "kill_latch_not_set"
        if parent_heartbeat_error:
            return False, str(parent_heartbeat_error)
        if max(0.0, parent_age_sec) > parent_timeout_sec:
            return False, "parent_heartbeat_stale"
        return True, ""

    def rearm_truth_gate(
        self,
        *,
        exchange_healthy: bool,
        exchange_reason: str,
        snapshot_fresh: bool,
        snapshot_pending_reason: str,
        risk_action: str,
        risk_reason: str,
        funding_action: str,
        funding_reason: str,
        open_order_count: int,
        nonzero_position_count: int,
    ) -> tuple[bool, str]:
        state = self.state
        if not exchange_healthy:
            return False, str(exchange_reason or "exchange_snapshot_failed")
        if not snapshot_fresh:
            return False, str(snapshot_pending_reason)
        if risk_action != "NONE":
            return False, str(risk_reason or "risk_breach_remains")
        if funding_action != "NONE":
            return False, str(
                funding_reason or "funding_guard_not_healthy"
            )
        if int(open_order_count) != 0:
            return False, "open_orders_remain"
        if int(nonzero_position_count) != 0:
            return False, "positions_remain"
        if state.kill_latched and state.stage != "FLAT_VERIFIED":
            return False, f"flat_not_verified:{state.stage}"
        return True, ""

    def prepare_rearm(
        self,
        request_id: str,
        reason: str,
        now: float,
        safety: tuple[bool, str],
        proof_binding: tuple | None = None,
    ) -> tuple[bool, str, str]:
        state = self.state
        request_id = str(request_id or "")
        if not request_id:
            self._record_rearm(
                request_id,
                "PREPARE",
                False,
                "request_id_missing",
            )
            return False, "", "request_id_missing"
        safe, refusal_reason = safety
        if not safe:
            state.prepared_rearm = None
            self._record_rearm(
                request_id,
                "PREPARE",
                False,
                refusal_reason,
            )
            return False, "", refusal_reason
        token = self._token_factory(24)
        state.prepared_rearm = {
            "token": token,
            "reason": str(reason or "operator_rearm"),
            "expires_at": now + self.rearm_prepare_ttl_sec,
            "generation": self.generation,
            "proof_binding": proof_binding,
        }
        self._record_rearm(
            request_id,
            "PREPARE",
            True,
            "rearm_prepared",
            token,
        )
        return True, token, "rearm_prepared"

    def validate_rearm_commit(
        self,
        request_id: str,
        token: str,
        now: float,
        proof_binding: tuple | None = None,
    ) -> tuple[bool, str]:
        state = self.state
        request_id = str(request_id or "")
        token = str(token or "")
        prepared = state.prepared_rearm or {}
        if (
            not token
            or not secrets.compare_digest(
                token,
                str(prepared.get("token", "") or ""),
            )
        ):
            reason = "rearm_token_invalid"
            self._record_rearm(
                request_id,
                "COMMIT",
                False,
                reason,
            )
            return False, reason
        if now > float(prepared.get("expires_at", 0.0) or 0.0):
            state.prepared_rearm = None
            reason = "rearm_prepare_expired"
            self._record_rearm(
                request_id,
                "COMMIT",
                False,
                reason,
            )
            return False, reason
        if int(prepared.get("generation", -1)) != self.generation:
            state.prepared_rearm = None
            reason = "rearm_generation_changed"
            self._record_rearm(
                request_id,
                "COMMIT",
                False,
                reason,
            )
            return False, reason
        if prepared.get("proof_binding") != proof_binding:
            state.prepared_rearm = None
            reason = "rearm_proof_binding_changed"
            self._record_rearm(
                request_id,
                "COMMIT",
                False,
                reason,
            )
            return False, reason
        return True, ""

    def reject_rearm_commit(
        self,
        request_id: str,
        reason: str,
    ) -> tuple[bool, str]:
        self.state.prepared_rearm = None
        self._record_rearm(
            request_id,
            "COMMIT",
            False,
            reason,
        )
        return False, reason

    def begin_rearm_commit(self) -> RearmCheckpoint:
        state = self.state
        checkpoint = RearmCheckpoint(
            kill_latched=state.kill_latched,
            kill_reason=state.kill_reason,
            stage=state.stage,
            flat_verification_count=state.flat_verification_count,
            last_verified_snapshot_sequence=(
                state.last_verified_snapshot_sequence
            ),
        )
        before = self._transition_snapshot()
        state.kill_latched = False
        state.kill_reason = ""
        state.stage = "ARMED"
        state.flat_verification_count = 0
        state.last_verified_snapshot_sequence = 0
        state.prepared_rearm = None
        self._bump_if_changed(before)
        return checkpoint

    def finish_rearm_commit(
        self,
        request_id: str,
        persisted: bool,
        reason: str,
        checkpoint: RearmCheckpoint,
    ) -> tuple[bool, str]:
        state = self.state
        if not persisted:
            before = self._transition_snapshot()
            state.kill_latched = checkpoint.kill_latched
            state.kill_reason = checkpoint.kill_reason
            state.stage = checkpoint.stage
            state.flat_verification_count = (
                checkpoint.flat_verification_count
            )
            state.last_verified_snapshot_sequence = (
                checkpoint.last_verified_snapshot_sequence
            )
            self._bump_if_changed(before)
            refusal_reason = str(reason or "state_persist_failed")
            self._record_rearm(
                request_id,
                "COMMIT",
                False,
                refusal_reason,
            )
            return False, refusal_reason
        self._record_rearm(
            request_id,
            "COMMIT",
            True,
            "rearm_committed",
        )
        return True, "rearm_committed"

    def abort_rearm(self, token: str) -> bool:
        state = self.state
        prepared = state.prepared_rearm or {}
        if not token or not secrets.compare_digest(
            str(token),
            str(prepared.get("token", "") or ""),
        ):
            return False
        state.prepared_rearm = None
        return True

    def advance_risk_stage(
        self,
        *,
        healthy: bool,
        action: str,
        now: float,
        parent_healthy: bool,
        exchange_valid: bool,
        open_order_count: int,
        nonzero_position_count: int,
        risk_snapshot_sequence: int,
        parent_stale_snapshot_sequence: int,
        last_cancel_attempt_at: float,
        last_flatten_attempt_at: float,
    ) -> RiskStageActions:
        state = self.state
        before = self._transition_snapshot()
        cancel = False
        flatten = False
        if healthy:
            state.unsafe_since = 0.0
            state.stage = "ARMED"
            state.flat_verification_count = 0
            state.last_verified_snapshot_sequence = 0
        else:
            if state.unsafe_since <= 0.0:
                state.unsafe_since = now
            if (
                state.stage == "FLAT_VERIFIED"
                and (
                    not exchange_valid
                    or open_order_count
                    or nonzero_position_count
                )
            ):
                state.stage = "FLATTENING"
                state.flat_verification_count = 0
                state.last_verified_snapshot_sequence = 0
            if state.stage != "FLAT_VERIFIED":
                if (
                    last_cancel_attempt_at <= 0.0
                    or now - last_cancel_attempt_at >= self.cancel_retry_sec
                ):
                    state.stage = "CANCEL_PENDING"
                    cancel = True
                if action == "KILL" and self.flatten_enabled:
                    state.stage = "FLATTENING"
                    exposure_remains = bool(
                        open_order_count or nonzero_position_count
                    )
                    exposure_unknown = not exchange_valid
                    if (exposure_remains or exposure_unknown) and (
                        last_flatten_attempt_at <= 0.0
                        or now - last_flatten_attempt_at
                        >= self.flatten_retry_sec
                    ):
                        flatten = True

            if exchange_valid:
                if action == "KILL" and self.flatten_enabled:
                    if (
                        open_order_count == 0
                        and nonzero_position_count == 0
                        and (
                            parent_healthy
                            or risk_snapshot_sequence
                            > parent_stale_snapshot_sequence
                        )
                        and risk_snapshot_sequence
                        > state.last_verified_snapshot_sequence
                    ):
                        state.last_verified_snapshot_sequence = (
                            risk_snapshot_sequence
                        )
                        state.flat_verification_count += 1
                    elif open_order_count or nonzero_position_count:
                        state.stage = "FLATTENING"
                        state.flat_verification_count = 0
                        state.last_verified_snapshot_sequence = (
                            risk_snapshot_sequence
                        )
                    if (
                        state.flat_verification_count
                        >= self.flat_verification_checks
                    ):
                        state.stage = "FLAT_VERIFIED"
                elif open_order_count == 0:
                    state.stage = "CANCEL_VERIFIED"
        self._bump_if_changed(before)
        return RiskStageActions(cancel=cancel, flatten=flatten)

    def should_keep_running(
        self,
        *,
        parent_healthy: bool,
        parent_stale_since: float,
        now: float,
        orphan_exit_sec: float,
        exchange_valid: bool,
        open_order_count: int,
        nonzero_position_count: int,
        risk_snapshot_sequence: int,
        parent_stale_snapshot_sequence: int,
    ) -> bool:
        state = self.state
        return not (
            not parent_healthy
            and parent_stale_since > 0.0
            and now - parent_stale_since >= orphan_exit_sec
            and state.stage == "FLAT_VERIFIED"
            and exchange_valid
            and open_order_count == 0
            and nonzero_position_count == 0
            and state.flat_verification_count
            >= self.flat_verification_checks
            and state.last_verified_snapshot_sequence
            == risk_snapshot_sequence
            and state.last_verified_snapshot_sequence
            > parent_stale_snapshot_sequence
        )
