"""OMS startup, outbound-gate and shutdown lifecycle control."""

from __future__ import annotations

import time

from event.type import (
    EVENT_SYSTEM_HEALTH,
    Event,
    LifecycleState,
)
from infrastructure.logger import logger

from .component import OMSComponent
from .shutdown_coordinator import OMSShutdownCoordinator


class OMSLifecycleController(OMSComponent):
    """Own startup and operator-visible lifecycle transitions."""

    OWNER_READS = frozenset(
        {
            "_account_cancel_symbols",
            "_audit",
            "_cancel_all_orders_unchecked",
            "_close_outbound_gate_locked",
            "_ensure_venue_dead_man_switch_armed",
            "_fail_closed_on_journal_error",
            "_perform_full_reset",
            "_sync_capability_mode",
            "_wait_for_outbound_risk_sends",
            "account",
            "can_query_exchange",
            "event_engine",
            "gateway",
            "guard_store",
            "last_freeze_reason",
            "last_halt_reason",
            "lifecycle_store",
            "lock",
            "manual_rearm_required",
            "rebuild_summary",
            "reconciler",
            "state",
            "symbol_guards",
            "trigger_reconcile",
            "venue_guards",
        }
    )
    OWNER_WRITES = frozenset(
        {
            "recovered_guard_cleanup_pending",
        }
    )

    def bootstrap(self):
        logger.info("OMS: Bootstrapping state...")
        self._audit("bootstrap_requested", recovered=self.rebuild_summary)
        if self.manual_rearm_required or self.state == LifecycleState.HALTED:
            if not self._ensure_venue_dead_man_switch_armed(
                "bootstrap_read_only"
            ):
                return False
            self._sync_capability_mode("manual_rearm_required")
            self._refresh_read_only_account_snapshot()
            logger.error("[OMS] Bootstrap blocked: manual rearm required after recovered HALT")
            self._audit(
                "bootstrap_blocked",
                reason="manual_rearm_required",
                recovered=self.rebuild_summary,
            )
            return False

        if self.state == LifecycleState.FROZEN or self._has_active_guards():
            if not self._ensure_venue_dead_man_switch_armed(
                "bootstrap_guarded"
            ):
                return False
            logger.warning("[OMS] Bootstrapping into guarded reconcile mode")
            freeze_reason = (
                self.last_freeze_reason or "Recovered guarded state"
            )
            self.lifecycle_store.transition(
                LifecycleState.FROZEN,
                last_freeze_reason=freeze_reason,
            )
            self._sync_capability_mode("bootstrap_guarded")
            self.recovered_guard_cleanup_pending = True
            self._audit(
                "bootstrap_guarded",
                reason=freeze_reason,
                recovered=self.rebuild_summary,
            )
            self.trigger_reconcile("Recovered guarded state")
            return True

        self._perform_full_reset()
        return self.state == LifecycleState.LIVE

    def _refresh_read_only_account_snapshot(self):
        if not self.can_query_exchange():
            return False

        try:
            account = self.gateway.get_account_info()
        except Exception as exc:
            logger.warning(f"[OMS] Read-only account sync failed: {exc}")
            return False

        if not isinstance(account, dict) or not account:
            return False

        try:
            account = self.reconciler._normalize_remote_account(
                account,
                require_initial_margin=True,
            )
            balances = self.reconciler._normalize_remote_account_balances(
                account
            )

            available_balance = account.get("availableBalance")
            self.account.force_sync(
                account["totalWalletBalance"],
                account["totalInitialMargin"],
                available_balance,
                balances=balances,
                maintenance_margin=account.get("totalMaintMargin"),
                margin_balance=account.get("totalMarginBalance"),
                margin_snapshot_time=time.time(),
                margin_snapshot_monotonic=time.perf_counter(),
            )
        except (TypeError, ValueError) as exc:
            logger.error(
                "[OMS] Invalid read-only account snapshot: "
                f"{type(exc).__name__}:{exc}"
            )
            self._audit(
                "read_only_account_sync_rejected",
                reason=f"{type(exc).__name__}:{exc}",
            )
            return False
        self._audit(
            "read_only_account_sync",
            balance=self.account.balance,
            available=self.account.available,
            budget_available=self.account.budget_available,
            assets=sorted(balances.keys()),
        )
        return True


    def _has_active_guards(self):
        return bool(
            self.symbol_guards
            or self.venue_guards
            or self.guard_store.has_active()
        )


    def freeze_system(self, reason: str, cancel_active_orders: bool = False):
        audit_error = None
        with self.lock:
            if self.state == LifecycleState.HALTED:
                self._close_outbound_gate_locked(reason)
                return

            previous = self.lifecycle_store.transition(
                LifecycleState.FROZEN,
                increment_generation=True,
                last_freeze_reason=reason,
            )
            previous_state = previous.state
            try:
                self._sync_capability_mode(reason)
            except Exception as exc:
                audit_error = exc

        if audit_error is not None:
            self._fail_closed_on_journal_error(
                audit_error,
                "freeze_capability_transition",
            )
            return
        if previous_state != LifecycleState.FROZEN:
            logger.error(f"OMS FROZEN: {reason}")
            try:
                self._audit(
                    "lifecycle",
                    state=self.state.value,
                    reason=reason,
                    previous_state=previous_state.value,
                )
            except Exception as exc:
                self._fail_closed_on_journal_error(
                    exc,
                    "freeze_system",
                )
                return
        else:
            logger.error(f"OMS still FROZEN: {reason}")
            try:
                self._audit("freeze_reasserted", reason=reason)
            except Exception as exc:
                self._fail_closed_on_journal_error(
                    exc,
                    "freeze_system_reasserted",
                )
                return

        self._wait_for_outbound_risk_sends(f"system_freeze:{reason}")
        if not cancel_active_orders:
            return

        self._audit(
            "freeze_cancel_all_requested",
            reason=reason,
            symbols=self._account_cancel_symbols(),
        )
        try:
            for symbol in self._account_cancel_symbols():
                self._cancel_all_orders_unchecked(
                    symbol,
                    source="system_freeze",
                )
        except Exception:
            pass

    def halt_system(self, reason: str):
        emit_halt_event = False
        audit_error = None
        with self.lock:
            if self.state == LifecycleState.HALTED:
                self.lifecycle_store.transition(
                    LifecycleState.HALTED,
                    increment_generation=True,
                    manual_rearm_required=True,
                    last_halt_reason=reason,
                )
                try:
                    self._sync_capability_mode(reason)
                    self._audit("halt_reasserted", reason=reason)
                except Exception as exc:
                    audit_error = exc
            else:
                self.lifecycle_store.transition(
                    LifecycleState.HALTED,
                    increment_generation=True,
                    manual_rearm_required=True,
                    last_halt_reason=reason,
                    last_freeze_reason="",
                )
                logger.critical(f"OMS HALTED: {reason}")
                try:
                    self._sync_capability_mode(reason)
                    self._audit(
                        "lifecycle",
                        state=self.state.value,
                        reason=reason,
                        manual_rearm_required=True,
                    )
                except Exception as exc:
                    audit_error = exc
                emit_halt_event = True
        if audit_error is not None:
            self._fail_closed_on_journal_error(
                audit_error,
                "halt_system",
            )
            return
        if emit_halt_event:
            try:
                self.event_engine.put(
                    Event(EVENT_SYSTEM_HEALTH, f"HALT:{reason}")
                )
            except Exception as event_exc:
                logger.critical(
                    "[OMS] Failed to publish HALT event: "
                    f"{type(event_exc).__name__}:{event_exc}"
                )
        self._wait_for_outbound_risk_sends(f"system_halt:{reason}")
        try:
            for symbol in self._account_cancel_symbols():
                self._cancel_all_orders_unchecked(
                    symbol,
                    source="system_halt",
                )
        except Exception:
            pass

    def rearm_system(self, reason: str = "manual"):
        audit_error = None
        ignored = False
        with self.lock:
            if self.state != LifecycleState.HALTED or not self.manual_rearm_required:
                ignored = True
                try:
                    self._audit("rearm_ignored", reason=reason)
                except Exception as exc:
                    audit_error = exc
            else:
                logger.warning(f"OMS manual rearm requested: {reason}")
                try:
                    self._audit(
                        "rearm_requested",
                        reason=reason,
                        halted_reason=self.last_halt_reason,
                    )
                except Exception as exc:
                    audit_error = exc
                if audit_error is None:
                    self.lifecycle_store.transition(
                        LifecycleState.RECONCILING,
                        increment_generation=True,
                    )
                    try:
                        self._sync_capability_mode(
                            f"manual_rearm:{reason}"
                        )
                        self._audit(
                            "lifecycle",
                            state=self.state.value,
                            reason=f"manual_rearm:{reason}",
                        )
                    except Exception as exc:
                        audit_error = exc
        if audit_error is not None:
            self._fail_closed_on_journal_error(
                audit_error,
                "rearm_ignored" if ignored else "rearm_transition",
            )
            return False
        if ignored:
            return False
        self._perform_full_reset()
        with self.lock:
            if self.state == LifecycleState.LIVE:
                self.lifecycle_store.transition(
                    LifecycleState.LIVE,
                    manual_rearm_required=False,
                    last_halt_reason="",
                )
                try:
                    self._audit(
                        "rearm_completed",
                        state=self.state.value,
                        reason=reason,
                    )
                except Exception as exc:
                    audit_error = exc
                if audit_error is None:
                    return True

            self.lifecycle_store.transition(
                self.state,
                manual_rearm_required=True,
            )
        if audit_error is not None:
            self._fail_closed_on_journal_error(
                audit_error,
                "rearm_completed",
            )
        return False

    def stop(self, clean_shutdown: bool = False, reason: str = ""):
        return self._spawn_component(OMSShutdownCoordinator).stop(
            clean_shutdown,
            reason,
        )
