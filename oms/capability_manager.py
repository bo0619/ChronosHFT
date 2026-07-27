"""OMS capability modes, safety heartbeat and venue dead-man switch."""

from __future__ import annotations

import math
import threading
import time

from event.type import LifecycleState, OMSCapabilityMode
from infrastructure.logger import logger

from .component import OMSComponent
from .journal import JournalError


class OMSCapabilityManager(OMSComponent):
    """Own account capability constraints and venue-level liveness controls."""

    def get_known_account_order_symbols(self) -> list[str]:
        with self.lock:
            return sorted(
                str(symbol or "").upper()
                for symbol in self._known_account_order_symbols
                if str(symbol or "").strip()
            )

    def _sync_capability_mode(self, reason: str = ""):
        with self.lock:
            previous_mode = getattr(self, "capability_mode", None)
            previous_reason = getattr(self, "capability_reason", "")
            base_mode = self._capability_mode_for_state()
            override_mode = self.mode_override
            next_mode = base_mode
            next_reason = reason or self.state.value.lower()
            if override_mode and self._mode_rank(override_mode) > self._mode_rank(base_mode):
                next_mode = override_mode
                next_reason = self.mode_override_reason or next_reason

            changed = previous_mode != next_mode or previous_reason != next_reason
            self.capability_mode = next_mode
            self.capability_reason = next_reason
            self._refresh_outbound_gate_locked(next_reason)
            if changed:
                self._audit(
                    "capability_mode_changed",
                    mode=next_mode.value,
                    reason=next_reason,
                    previous_mode=previous_mode.value if previous_mode else "",
                    previous_reason=previous_reason,
                )

    def _mode_rank(self, mode: OMSCapabilityMode) -> int:
        ranks = {
            OMSCapabilityMode.LIVE: 0,
            OMSCapabilityMode.DEGRADED: 1,
            OMSCapabilityMode.PASSIVE_ONLY: 2,
            OMSCapabilityMode.REDUCE_ONLY: 3,
            OMSCapabilityMode.CANCEL_ONLY: 4,
            OMSCapabilityMode.READ_ONLY: 5,
            OMSCapabilityMode.LOCKDOWN: 6,
        }
        return ranks.get(mode, 99)

    @staticmethod
    def _mode_constraint_key(reason: str) -> str:
        reason = str(reason or "").strip()
        if not reason:
            return "unspecified"
        prefix, separator, _detail = reason.partition(":")
        return f"{prefix}:" if separator else reason

    def _refresh_selected_mode_constraint(self):
        if not self.mode_constraints:
            self.mode_override = None
            self.mode_override_reason = ""
            return
        _key, (mode, reason) = max(
            self.mode_constraints.items(),
            key=lambda item: (self._mode_rank(item[1][0]), item[0]),
        )
        self.mode_override = mode
        self.mode_override_reason = reason

    def _capability_mode_for_state(self) -> OMSCapabilityMode:
        if self.state == LifecycleState.LIVE:
            return OMSCapabilityMode.LIVE
        if self.state in {LifecycleState.BOOTSTRAP, LifecycleState.RECONCILING}:
            return OMSCapabilityMode.READ_ONLY
        if self.state in {LifecycleState.FROZEN, LifecycleState.HALTED}:
            return OMSCapabilityMode.CANCEL_ONLY
        return OMSCapabilityMode.LOCKDOWN

    def _ensure_capability_mode_consistent(self):
        expected_mode = self._capability_mode_for_state()
        if self.mode_override and self._mode_rank(self.mode_override) > self._mode_rank(expected_mode):
            expected_mode = self.mode_override
        if self.capability_mode != expected_mode:
            self._sync_capability_mode(f"state_sync:{self.state.value}")

    def set_trading_mode(self, mode, reason: str):
        if isinstance(mode, str):
            mode = OMSCapabilityMode(mode)
        if mode not in {
            OMSCapabilityMode.DEGRADED,
            OMSCapabilityMode.PASSIVE_ONLY,
            OMSCapabilityMode.REDUCE_ONLY,
        }:
            raise ValueError(f"Unsupported trading mode override: {mode}")
        constraint_key = self._mode_constraint_key(reason)
        force_generation = constraint_key == "venue_dead_man_switch:"
        journal_error = None
        selected = False
        reduce_only_selected = False
        with self.lock:
            previous_mode = (
                self.mode_override.value if self.mode_override else ""
            )
            previous_reason = self.mode_override_reason
            previous_constraint = self.mode_constraints.get(constraint_key)
            if (
                not force_generation
                and previous_constraint == (mode, reason)
            ):
                return (
                    self.mode_override == mode
                    and self.mode_override_reason == reason
                )

            self.mode_constraint_generation += 1
            constraint_generation = self.mode_constraint_generation
            self.mode_constraints[constraint_key] = (mode, reason)
            self.mode_constraint_generations[constraint_key] = (
                constraint_generation
            )
            self._refresh_selected_mode_constraint()
            try:
                self._sync_capability_mode(
                    self.mode_override_reason or reason
                )
                selected = (
                    self.mode_override == mode
                    and self.mode_override_reason == reason
                )
                reduce_only_selected = (
                    self.mode_override == OMSCapabilityMode.REDUCE_ONLY
                    and previous_constraint != (mode, reason)
                )
                self._audit(
                    "trading_mode_override_set",
                    mode=mode.value,
                    reason=reason,
                    constraint_key=constraint_key,
                    constraint_generation=constraint_generation,
                    selected=selected,
                    previous_mode=previous_mode,
                    previous_reason=previous_reason,
                )
            except JournalError as exc:
                journal_error = exc
        if journal_error is not None:
            self._fail_closed_on_journal_error(
                journal_error,
                "set_trading_mode",
            )
            return False
        if reduce_only_selected:
            self._wait_for_outbound_risk_sends(
                f"trading_mode_reduce_only:{reason}"
            )
            self._cancel_orders_matching(lambda order: not order.intent.reduce_only)
        return selected

    def clear_trading_mode(
        self,
        reason: str = "",
        prefixes=(),
        *,
        expected_generations=None,
    ):
        journal_error = None
        with self.lock:
            if not self.mode_constraints:
                return False

            expected = (
                {
                    str(key): int(generation)
                    for key, generation in expected_generations.items()
                }
                if expected_generations is not None
                else None
            )
            candidate_keys = [
                key
                for key, (
                    _mode,
                    constraint_reason,
                ) in self.mode_constraints.items()
                if not prefixes
                or any(
                    constraint_reason.startswith(prefix)
                    for prefix in prefixes
                )
            ]
            matching_keys = [
                key
                for key in candidate_keys
                if expected is None
                or (
                    key in expected
                    and self.mode_constraint_generations.get(key)
                    == expected[key]
                )
            ]
            if not matching_keys:
                return False

            previous_mode = (
                self.mode_override.value if self.mode_override else ""
            )
            previous_reason = self.mode_override_reason
            previous_capability_mode = self.capability_mode
            previous_capability_reason = self.capability_reason
            cleared_generations = {
                key: self.mode_constraint_generations.get(key, 0)
                for key in matching_keys
            }
            cleared_constraints = {
                key: self.mode_constraints[key]
                for key in matching_keys
            }
            for key in matching_keys:
                self.mode_constraints.pop(key, None)
                self.mode_constraint_generations.pop(key, None)
            self._refresh_selected_mode_constraint()
            try:
                self._sync_capability_mode(
                    self.mode_override_reason
                    or reason
                    or "trading_mode_cleared"
                )
                self._audit(
                    "trading_mode_override_cleared",
                    reason=reason or previous_reason,
                    cleared_constraint_keys=matching_keys,
                    cleared_constraint_generations=cleared_generations,
                    previous_mode=previous_mode,
                    previous_reason=previous_reason,
                )
            except JournalError as exc:
                self.mode_constraints.update(cleared_constraints)
                self.mode_constraint_generations.update(
                    cleared_generations
                )
                self._refresh_selected_mode_constraint()
                self.capability_mode = previous_capability_mode
                self.capability_reason = previous_capability_reason
                self._close_outbound_gate_locked(
                    "durable_journal_unavailable:"
                    "clear_trading_mode",
                    hold="journal_failure",
                )
                journal_error = exc

        if journal_error is not None:
            self._fail_closed_on_journal_error(
                journal_error,
                "clear_trading_mode",
            )
            return False
        return True

    def has_trading_mode_constraint(self, prefixes=()) -> bool:
        with self.lock:
            if not prefixes:
                return bool(self.mode_constraints)
            return any(
                any(
                    constraint_reason.startswith(prefix)
                    for prefix in prefixes
                )
                for _mode, constraint_reason in self.mode_constraints.values()
            )

    def can_query_exchange(self) -> bool:
        self._ensure_capability_mode_consistent()
        return self.capability_mode != OMSCapabilityMode.LOCKDOWN

    def can_cancel_orders(self) -> bool:
        self._ensure_capability_mode_consistent()
        return self.capability_mode in {
            OMSCapabilityMode.LIVE,
            OMSCapabilityMode.DEGRADED,
            OMSCapabilityMode.PASSIVE_ONLY,
            OMSCapabilityMode.REDUCE_ONLY,
            OMSCapabilityMode.CANCEL_ONLY,
        }

    def can_open_new_risk(self) -> bool:
        self._ensure_capability_mode_consistent()
        return self.capability_mode in {
            OMSCapabilityMode.LIVE,
            OMSCapabilityMode.DEGRADED,
            OMSCapabilityMode.PASSIVE_ONLY,
        }

    def record_risk_control_heartbeat(
        self,
        source: str = "risk_manager",
        healthy: bool = True,
        reason: str = "",
    ) -> bool:
        if not self.risk_control_heartbeat_enabled:
            return False

        status = "healthy" if healthy else "unhealthy"
        source = str(source or "risk_manager")
        reason = str(reason or "")
        if (
            self.risk_control_heartbeat_required_source
            and source != self.risk_control_heartbeat_required_source
        ):
            should_audit = False
            with self.lock:
                if source not in self.rejected_risk_control_heartbeat_sources:
                    self.rejected_risk_control_heartbeat_sources.add(source)
                    should_audit = True
            if should_audit:
                try:
                    self._audit(
                        "risk_control_heartbeat_source_rejected",
                        source=source,
                        required_source=(
                            self.risk_control_heartbeat_required_source
                        ),
                    )
                except JournalError as exc:
                    self._fail_closed_on_journal_error(
                        exc,
                        "risk_control_heartbeat_source_rejected",
                    )
            return False
        with self.lock:
            previous_status = self.risk_control_heartbeat_status
            previous_reason = self.risk_control_heartbeat_reason
            self.last_risk_control_heartbeat_monotonic = time.perf_counter()
            self.last_risk_control_heartbeat_time = time.time()
            self.risk_control_heartbeat_status = status
            self.risk_control_heartbeat_source = source
            self.risk_control_heartbeat_reason = reason

        if status != previous_status or reason != previous_reason:
            try:
                self._audit(
                    "risk_control_heartbeat_status",
                    status=status,
                    source=source,
                    reason=reason,
                )
            except JournalError as exc:
                self._fail_closed_on_journal_error(
                    exc,
                    "risk_control_heartbeat",
                )
                return False
        return healthy

    def get_risk_control_heartbeat_snapshot(self) -> dict:
        with self.lock:
            monotonic_timestamp = self.last_risk_control_heartbeat_monotonic
            status = self.risk_control_heartbeat_status
            source = self.risk_control_heartbeat_source
            reason = self.risk_control_heartbeat_reason
            wall_time = self.last_risk_control_heartbeat_time
        age_sec = (
            max(0.0, time.perf_counter() - monotonic_timestamp)
            if monotonic_timestamp > 0.0
            else None
        )
        return {
            "enabled": self.risk_control_heartbeat_enabled,
            "status": status,
            "source": source,
            "reason": reason,
            "last_heartbeat_time": wall_time,
            "age_sec": age_sec,
            "max_age_sec": self.risk_control_heartbeat_max_age_sec,
            "required_source": self.risk_control_heartbeat_required_source,
            "valid": bool(
                not self.risk_control_heartbeat_enabled
                or (
                    status == "healthy"
                    and age_sec is not None
                    and age_sec <= self.risk_control_heartbeat_max_age_sec
                )
            ),
        }

    def _venue_dead_man_switch_health_locked(self, now: float = None):
        if not self.venue_dead_man_switch_enabled:
            return True, ""
        missing_symbols = sorted(
            self.venue_dead_man_switch_symbols
            - self.venue_dead_man_armed_symbols
        )
        if missing_symbols:
            return False, f"unarmed_symbols:{','.join(missing_symbols)}"
        if self.last_venue_dead_man_success_monotonic <= 0.0:
            return False, "renewal_missing"
        if self.venue_dead_man_last_error:
            return False, self.venue_dead_man_last_error

        now = time.perf_counter() if now is None else float(now)
        age_sec = max(
            0.0,
            now - self.last_venue_dead_man_success_monotonic,
        )
        if age_sec > self.venue_dead_man_switch_max_renewal_age_sec:
            return (
                False,
                f"renewal_stale:{age_sec:.3f}s>"
                f"{self.venue_dead_man_switch_max_renewal_age_sec:.3f}s",
            )
        return True, ""

    def get_venue_dead_man_switch_snapshot(self) -> dict:
        with self.lock:
            now = time.perf_counter()
            healthy, reason = self._venue_dead_man_switch_health_locked(now)
            success_monotonic = self.last_venue_dead_man_success_monotonic
            return {
                "enabled": self.venue_dead_man_switch_enabled,
                "valid": healthy,
                "reason": reason,
                "countdown_time_ms": self.venue_dead_man_switch_countdown_time_ms,
                "renewal_interval_sec": (
                    self.venue_dead_man_switch_renewal_interval_sec
                ),
                "max_renewal_age_sec": (
                    self.venue_dead_man_switch_max_renewal_age_sec
                ),
                "last_success_time": self.last_venue_dead_man_success_time,
                "age_sec": (
                    max(0.0, now - success_monotonic)
                    if success_monotonic > 0.0
                    else None
                ),
                "armed_symbols": sorted(self.venue_dead_man_armed_symbols),
                "required_symbols": sorted(self.venue_dead_man_switch_symbols),
                "failure_count": self.venue_dead_man_failure_count,
                "recovery_count": self.venue_dead_man_recovery_count,
                "recovery_checks": self.venue_dead_man_switch_recovery_checks,
                "last_error": self.venue_dead_man_last_error,
                "renewal_inflight": self.venue_dead_man_renewal_inflight,
                "safety_cancel_inflight": bool(
                    self._venue_dead_man_safety_cancel_thread is not None
                    and self._venue_dead_man_safety_cancel_thread.is_alive()
                ),
            }

    def _venue_dead_man_switch_renewal_allowed_locked(
        self,
        *,
        force: bool = False,
    ) -> tuple[bool, str]:
        if not self.venue_dead_man_switch_enabled:
            return True, ""
        if self._stopped:
            return False, "oms_stopped"
        if self._shutdown_requested:
            return False, "shutdown_requested"
        if force:
            return True, ""
        if self.state != LifecycleState.LIVE:
            return False, f"lifecycle_not_live:{self.state.value}"
        if self._has_active_guards():
            return False, "oms_guard_active"
        if self.mode_constraints:
            return False, "trading_mode_constraint_active"
        if self.capability_mode != OMSCapabilityMode.LIVE:
            return False, f"capability_not_live:{self.capability_mode.value}"
        healthy, reason = self._venue_dead_man_switch_health_locked()
        if not healthy:
            return False, reason or "renewal_health_invalid"
        return True, ""

    def can_renew_venue_dead_man_switch(self) -> bool:
        """Return whether a routine renewal may extend venue order lifetimes."""
        with self.lock:
            allowed, _reason = (
                self._venue_dead_man_switch_renewal_allowed_locked()
            )
            return allowed

    @staticmethod
    def _venue_dead_man_constraint_reason(reason: str) -> str:
        category = str(reason or "unhealthy").partition(":")[0]
        return f"venue_dead_man_switch:{category or 'unhealthy'}"

    def _start_venue_dead_man_safety_cancel(self, reason: str) -> bool:
        now = time.perf_counter()
        with self.lock:
            thread = self._venue_dead_man_safety_cancel_thread
            if thread is not None and thread.is_alive():
                return False
            if (
                self._venue_dead_man_safety_cancel_last_attempt > 0.0
                and now - self._venue_dead_man_safety_cancel_last_attempt
                < self.venue_dead_man_safety_cancel_retry_sec
            ):
                return False

        def cancel_and_verify():
            published.wait()
            try:
                verified = self.cancel_all_account_orders_verified(
                    self.gateway,
                    source="venue_dead_man_switch",
                    timeout_sec=self.venue_dead_man_safety_cancel_timeout_sec,
                )
                self._audit(
                    "venue_dead_man_switch_safety_cancel_completed",
                    reason=reason,
                    verified=bool(verified),
                )
            finally:
                with self.lock:
                    handle = self._venue_dead_man_safety_cancel_thread
                    if handle is not None and handle.is_current():
                        self._venue_dead_man_safety_cancel_thread = None

        published = threading.Event()
        thread = self._submit_background_task(
            "dms:safety-cancel",
            cancel_and_verify,
            name="VenueDeadManSafetyCancel",
            safety=True,
        )
        with self.lock:
            if thread is None:
                published.set()
                return False
            self._venue_dead_man_safety_cancel_thread = thread
            self._venue_dead_man_safety_cancel_last_attempt = now
        published.set()
        return True

    def handle_venue_dead_man_switch_unhealthy(
        self,
        reason: str = "",
    ) -> bool:
        if not self.venue_dead_man_switch_enabled:
            return True
        with self.lock:
            healthy, detected_reason = self._venue_dead_man_switch_health_locked()
        if healthy:
            return True

        detail = str(reason or detected_reason or "unhealthy")
        constraint_reason = self._venue_dead_man_constraint_reason(detail)
        with self.lock:
            if not self.venue_dead_man_last_error:
                self.venue_dead_man_last_error = detail
                self.venue_dead_man_failure_count += 1
                self.venue_dead_man_recovery_count = 0
        self.set_trading_mode(
            OMSCapabilityMode.REDUCE_ONLY,
            constraint_reason,
        )
        self._start_venue_dead_man_safety_cancel(detail)
        self._audit(
            "venue_dead_man_switch_unhealthy_latched",
            reason=detail,
            constraint=constraint_reason,
        )
        return False

    def request_venue_dead_man_switch_renewal(self) -> bool:
        """Schedule a due DMS renewal without blocking the risk heartbeat."""
        if not self.venue_dead_man_switch_enabled:
            return True

        now = time.perf_counter()
        with self.lock:
            allowed, authorization_reason = (
                self._venue_dead_man_switch_renewal_allowed_locked()
            )
            healthy, health_reason = self._venue_dead_man_switch_health_locked(now)
            if not allowed:
                schedule_renewal = False
            else:
                since_attempt = now - self.last_venue_dead_man_attempt_monotonic
                schedule_renewal = (
                    not self.venue_dead_man_renewal_inflight
                    and not (
                        self.last_venue_dead_man_attempt_monotonic > 0.0
                        and since_attempt
                        < self.venue_dead_man_switch_renewal_interval_sec
                    )
                )
                if schedule_renewal:
                    self.venue_dead_man_renewal_inflight = True

        if not healthy:
            self.handle_venue_dead_man_switch_unhealthy(
                health_reason or authorization_reason
            )
        if not allowed:
            return False
        if not schedule_renewal:
            return healthy

        def renew_in_background():
            published.wait()
            try:
                self.renew_venue_dead_man_switch()
            finally:
                with self.lock:
                    self.venue_dead_man_renewal_inflight = False
                    handle = self._venue_dead_man_renewal_thread
                    if handle is not None and handle.is_current():
                        self._venue_dead_man_renewal_thread = None

        published = threading.Event()
        thread = self._submit_background_task(
            "dms:renewal",
            renew_in_background,
            name="VenueDeadManRenewal",
            safety=True,
        )
        with self.lock:
            if thread is None:
                self.venue_dead_man_renewal_inflight = False
                published.set()
                return False
            self._venue_dead_man_renewal_thread = thread
        published.set()
        return healthy

    def renew_venue_dead_man_switch(self, force: bool = False) -> bool:
        if not self.venue_dead_man_switch_enabled:
            return True

        now = time.perf_counter()
        with self.lock:
            allowed, authorization_reason = (
                self._venue_dead_man_switch_renewal_allowed_locked(
                    force=force,
                )
            )
            if not allowed:
                healthy, health_reason = (
                    self._venue_dead_man_switch_health_locked(now)
                )
            else:
                since_attempt = (
                    now - self.last_venue_dead_man_attempt_monotonic
                )
                if (
                    not force
                    and self.last_venue_dead_man_attempt_monotonic > 0.0
                    and since_attempt
                    < self.venue_dead_man_switch_renewal_interval_sec
                ):
                    healthy, _reason = (
                        self._venue_dead_man_switch_health_locked(now)
                    )
                    return healthy
                self.last_venue_dead_man_attempt_monotonic = now

        if not allowed:
            if not force and not healthy:
                self.handle_venue_dead_man_switch_unhealthy(
                    health_reason or authorization_reason
                )
            return False

        renew = getattr(self.gateway, "set_countdown_cancel_all", None)
        renewed_symbols = set()
        failures = []
        if not callable(renew):
            failures.append("gateway_method_unavailable")
        else:
            for symbol in sorted(self.venue_dead_man_switch_symbols):
                try:
                    response = renew(
                        symbol,
                        self.venue_dead_man_switch_countdown_time_ms,
                    )
                    status_code = getattr(response, "status_code", None)
                    if response is True or status_code == 200:
                        renewed_symbols.add(symbol)
                        continue
                    failures.append(
                        f"{symbol}:status={status_code if status_code is not None else 'unknown'}"
                    )
                except Exception as exc:
                    failures.append(f"{symbol}:{type(exc).__name__}:{exc}")

        if failures:
            failure_reason = "renewal_failed:" + ";".join(failures)
            with self.lock:
                self.venue_dead_man_armed_symbols = renewed_symbols
                self.venue_dead_man_failure_count += 1
                self.venue_dead_man_recovery_count = 0
                self.venue_dead_man_last_error = failure_reason
            self.handle_venue_dead_man_switch_unhealthy(failure_reason)
            self._audit(
                "venue_dead_man_switch_renewal_failed",
                reason=failure_reason,
                renewed_symbols=sorted(renewed_symbols),
                required_symbols=sorted(self.venue_dead_man_switch_symbols),
            )
            return False

        constraint_key = "venue_dead_man_switch:"
        with self.lock:
            observed_constraint = self.mode_constraints.get(constraint_key)
            had_constraint = bool(
                observed_constraint
                and observed_constraint[1].startswith(constraint_key)
            )
            observed_generation = (
                self.mode_constraint_generations.get(constraint_key, 0)
                if had_constraint
                else 0
            )
            first_success = self.last_venue_dead_man_success_monotonic <= 0.0
            recovered_from_error = bool(self.venue_dead_man_last_error)
            self.venue_dead_man_armed_symbols = renewed_symbols
            self.last_venue_dead_man_success_monotonic = now
            self.last_venue_dead_man_success_time = time.time()
            self.venue_dead_man_failure_count = 0
            self.venue_dead_man_last_error = ""
            if had_constraint:
                self.venue_dead_man_recovery_count += 1
            else:
                self.venue_dead_man_recovery_count = 0
            recovery_count = self.venue_dead_man_recovery_count

        cleared = False
        if (
            had_constraint
            and recovery_count >= self.venue_dead_man_switch_recovery_checks
        ):
            cleared = self.clear_trading_mode(
                reason="venue dead-man switch renewal recovered",
                prefixes=("venue_dead_man_switch:",),
                expected_generations={
                    constraint_key: observed_generation,
                },
            )
            with self.lock:
                self.venue_dead_man_recovery_count = 0

        if first_success or recovered_from_error or cleared:
            self._audit(
                "venue_dead_man_switch_renewed",
                armed_symbols=sorted(renewed_symbols),
                recovered=bool(cleared),
                recovery_count=recovery_count,
                observed_constraint_generation=observed_generation,
            )
        return True

    def _ensure_venue_dead_man_switch_armed(self, context: str) -> bool:
        with self.lock:
            renewal_thread = self._venue_dead_man_renewal_thread
        if (
            renewal_thread is not None
            and renewal_thread.is_alive()
            and not renewal_thread.is_current()
        ):
            renewal_thread.join(
                timeout=max(
                    1.0,
                    self.venue_dead_man_switch_max_renewal_age_sec,
                )
            )
            if renewal_thread.is_alive():
                reason = f"venue_dead_man_switch_renewal_stuck:{context}"
                logger.critical(f"[OMS] {reason}")
                self.halt_system(reason)
                return False
        if self.renew_venue_dead_man_switch(force=True):
            return True
        reason = f"venue_dead_man_switch_unavailable:{context}"
        logger.critical(f"[OMS] {reason}")
        self.halt_system(reason)
        return False

    def get_capability_snapshot(self) -> dict:
        with self.lock:
            self._ensure_capability_mode_consistent()
            capability_mode = self.capability_mode
            capability_reason = self.capability_reason
            mode_override = self.mode_override
            mode_override_reason = self.mode_override_reason
            mode_constraint_generation = (
                self.mode_constraint_generation
            )
            mode_constraints = {
                key: {
                    "mode": mode.value,
                    "reason": reason,
                    "generation": self.mode_constraint_generations.get(
                        key,
                        0,
                    ),
                }
                for key, (mode, reason) in self.mode_constraints.items()
            }
        return {
            "mode": capability_mode.value,
            "reason": capability_reason,
            "override_mode": (
                mode_override.value if mode_override else ""
            ),
            "override_reason": mode_override_reason,
            "mode_constraint_generation": (
                mode_constraint_generation
            ),
            "mode_constraints": mode_constraints,
            "can_query": capability_mode != OMSCapabilityMode.LOCKDOWN,
            "can_cancel": capability_mode
            in {
                OMSCapabilityMode.LIVE,
                OMSCapabilityMode.DEGRADED,
                OMSCapabilityMode.PASSIVE_ONLY,
                OMSCapabilityMode.REDUCE_ONLY,
                OMSCapabilityMode.CANCEL_ONLY,
            },
            "can_open_risk": capability_mode
            in {
                OMSCapabilityMode.LIVE,
                OMSCapabilityMode.DEGRADED,
                OMSCapabilityMode.PASSIVE_ONLY,
            },
            "risk_control_heartbeat": self.get_risk_control_heartbeat_snapshot(),
            "venue_dead_man_switch": self.get_venue_dead_man_switch_snapshot(),
            "outbound_message_budget": self.get_outbound_message_budget_snapshot(),
            "background_tasks": self.get_background_task_snapshot(),
            "outbound_gate": self.get_outbound_gate_snapshot(),
            "strategy_risk_budgets": self.get_strategy_risk_budget_snapshot(),
            "single_writer_fence": (
                self.single_writer_fence.health_snapshot()
                if self.single_writer_fence is not None
                else {"held": False, "enabled": False}
            ),
        }

    def _get_capability_block_reason(self, action: str) -> str:
        return (
            f"{action}_blocked:"
            f"{self.capability_mode.value}:{self.capability_reason or self.state.value}"
        )

    def query_account_info(self):
        if not self.can_query_exchange():
            self._audit("query_rejected", query="account", reason=self._get_capability_block_reason("query"))
            return None
        return self.gateway.get_account_info()

    def sync_account_margin_health(
        self,
        account: dict,
        snapshot_time: float = None,
        snapshot_monotonic: float = None,
    ) -> bool:
        if not isinstance(account, dict):
            return False
        maintenance_margin = account.get("totalMaintMargin")
        margin_balance = account.get("totalMarginBalance")
        if maintenance_margin is None or margin_balance is None:
            return False
        try:
            maintenance_margin = float(maintenance_margin)
            margin_balance = float(margin_balance)
        except (TypeError, ValueError):
            self._audit(
                "account_margin_health_invalid",
                maintenance_margin=maintenance_margin,
                margin_balance=margin_balance,
            )
            return False
        if not math.isfinite(maintenance_margin) or not math.isfinite(margin_balance):
            self._audit(
                "account_margin_health_invalid",
                maintenance_margin=maintenance_margin,
                margin_balance=margin_balance,
                reason="non_finite",
            )
            return False
        if maintenance_margin < 0.0:
            self._audit(
                "account_margin_health_invalid",
                maintenance_margin=maintenance_margin,
                margin_balance=margin_balance,
                reason="negative_maintenance_margin",
            )
            return False

        snapshot_time = float(snapshot_time or time.time())
        snapshot_monotonic = float(
            snapshot_monotonic or time.perf_counter()
        )
        with self.lock:
            synced = self.account.sync_margin_health(
                maintenance_margin,
                margin_balance,
                snapshot_time=snapshot_time,
                snapshot_monotonic=snapshot_monotonic,
            )
        if synced:
            self._audit(
                "account_margin_health_synced",
                maintenance_margin=maintenance_margin,
                margin_balance=margin_balance,
                ratio=self.account.maintenance_margin_ratio,
                snapshot_time=snapshot_time,
                snapshot_monotonic=snapshot_monotonic,
            )
        return synced

    def query_positions(self):
        if not self.can_query_exchange():
            self._audit("query_rejected", query="positions", reason=self._get_capability_block_reason("query"))
            return None
        return self.gateway.get_all_positions()

    def query_open_orders(self):
        if not self.can_query_exchange():
            self._audit("query_rejected", query="open_orders", reason=self._get_capability_block_reason("query"))
            return None
        return self.gateway.get_open_orders()

    def query_order(self, symbol: str, order_id: str):
        if not self.can_query_exchange():
            return None
        query = getattr(self.gateway, "get_order", None)
        if not callable(query):
            return None
        return query(symbol, order_id)

    def query_user_trades(self, symbol: str, **kwargs):
        if not self.can_query_exchange():
            return None
        query = getattr(self.gateway, "get_user_trades", None)
        if not callable(query):
            return None
        return query(symbol, **kwargs)

    def query_income_history(self, **kwargs):
        if not self.can_query_exchange():
            self._audit(
                "query_rejected",
                query="income_history",
                reason=self._get_capability_block_reason("query"),
            )
            return None
        query = getattr(self.gateway, "get_income_history", None)
        if not callable(query):
            return None
        return query(**kwargs)
