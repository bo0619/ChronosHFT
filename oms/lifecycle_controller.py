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


class OMSLifecycleController(OMSComponent):
    """Own startup and operator-visible lifecycle transitions."""

    OWNER_READS = frozenset(
        {
            "_account_cancel_symbols",
            "_audit",
            "_background_tasks",
            "_cancel_all_orders_unchecked",
            "_close_outbound_gate_locked",
            "_deferred_cancel_all_symbols",
            "_deferred_cancel_oids",
            "_ensure_venue_dead_man_switch_armed",
            "_fail_closed_on_journal_error",
            "_order_truth_resolution_inflight",
            "_perform_full_reset",
            "_refresh_outbound_gate_locked",
            "_rpi_calibration_snapshot_locked",
            "_shutdown_cancel_verified",
            "_shutdown_reason",
            "_shutdown_requested",
            "_submit_cancel_requested_oids",
            "_submit_settlement_inflight_oids",
            "_sync_capability_mode",
            "_wait_for_outbound_order_sends",
            "_wait_for_outbound_risk_sends",
            "account",
            "capability_mode",
            "can_query_exchange",
            "event_engine",
            "execution_ids",
            "exposure",
            "gateway",
            "lock",
            "mode_constraint_generation",
            "mode_constraints",
            "mode_override",
            "mode_override_reason",
            "order_monitor",
            "orders",
            "outbound_gate_drain_timeout_sec",
            "paper_trade_database",
            "rebuild_summary",
            "reconciler",
            "single_writer_fence",
            "strategy_guards",
            "strategy_symbol_guards",
            "symbol_guard_records",
            "symbol_guards",
            "terminated_oids",
            "trade_cursors",
            "trade_scan_end_ms",
            "trade_tail_verification_inflight",
            "trigger_reconcile",
            "venue_guards",
            "venue_guard_records",
            "external_cash_flow_ids",
            "external_cash_flow_scan_end_ms",
            "journal",
        }
    )
    OWNER_WRITES = frozenset(
        {
            "_lifecycle_generation",
            "_outbound_all_order_seal_reason",
            "_stopped",
            "last_freeze_reason",
            "last_halt_reason",
            "manual_rearm_required",
            "reconcile_retry_scheduled",
            "recovered_guard_cleanup_pending",
            "state",
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
            self.state = LifecycleState.FROZEN
            self._sync_capability_mode("bootstrap_guarded")
            self.recovered_guard_cleanup_pending = True
            if not self.last_freeze_reason:
                self.last_freeze_reason = "Recovered guarded state"
            self._audit(
                "bootstrap_guarded",
                reason=self.last_freeze_reason,
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
            or self.strategy_guards
            or self.strategy_symbol_guards
        )


    def freeze_system(self, reason: str, cancel_active_orders: bool = False):
        audit_error = None
        with self.lock:
            if self.state == LifecycleState.HALTED:
                self._close_outbound_gate_locked(reason)
                return

            previous_state = self.state
            self._lifecycle_generation += 1
            self.state = LifecycleState.FROZEN
            try:
                self._sync_capability_mode(reason)
            except Exception as exc:
                audit_error = exc
            self.last_freeze_reason = reason

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
            self._lifecycle_generation += 1
            if self.state == LifecycleState.HALTED:
                self.last_halt_reason = reason
                self.manual_rearm_required = True
                try:
                    self._sync_capability_mode(reason)
                    self._audit("halt_reasserted", reason=reason)
                except Exception as exc:
                    audit_error = exc
            else:
                self.state = LifecycleState.HALTED
                self.manual_rearm_required = True
                self.last_halt_reason = reason
                self.last_freeze_reason = ""
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
                    self.state = LifecycleState.RECONCILING
                    self._lifecycle_generation += 1
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
                self.manual_rearm_required = False
                self.last_halt_reason = ""
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

            self.manual_rearm_required = True
        if audit_error is not None:
            self._fail_closed_on_journal_error(
                audit_error,
                "rearm_completed",
            )
        return False

    def _shutdown_checkpoint_summary(self) -> dict:
        """Capture the complete in-memory recovery surface after drain."""
        with self.lock:
            strategy_exposure = []
            average_prices = self.exposure.strategy_avg_prices
            for key, quantity in sorted(
                self.exposure.strategy_net_positions.items(),
                key=lambda item: tuple(str(part) for part in item[0]),
            ):
                strategy_id, symbol = key
                strategy_exposure.append(
                    {
                        "strategy_id": str(strategy_id),
                        "symbol": str(symbol),
                        "quantity": float(quantity),
                        "average_price": float(average_prices.get(key, 0.0)),
                    }
                )
            calibration_snapshot = self._rpi_calibration_snapshot_locked()
            return {
                "state_version": 1,
                "state": self.state.value,
                "capability_mode": self.capability_mode.value,
                "manual_rearm_required": bool(self.manual_rearm_required),
                "last_freeze_reason": str(self.last_freeze_reason or ""),
                "last_halt_reason": str(self.last_halt_reason or ""),
                "active_orders": [
                    order.to_record()
                    for _client_oid, order in sorted(self.orders.items())
                ],
                "terminated_oids": sorted(str(oid) for oid in self.terminated_oids),
                "execution_ids": sorted(str(value) for value in self.execution_ids),
                "strategy_exposure": strategy_exposure,
                "symbol_guards": dict(self.symbol_guards),
                "symbol_guard_records": dict(self.symbol_guard_records),
                "venue_guards": dict(self.venue_guards),
                "venue_guard_records": dict(self.venue_guard_records),
                "strategy_guards": dict(self.strategy_guards),
                "strategy_symbol_guards": dict(self.strategy_symbol_guards),
                "mode_override": str(self.mode_override or ""),
                "mode_override_reason": str(self.mode_override_reason or ""),
                "mode_constraint_generation": int(
                    self.mode_constraint_generation or 0
                ),
                "mode_constraints": dict(self.mode_constraints),
                "trade_cursors": dict(self.trade_cursors),
                "trade_scan_end_ms": dict(self.trade_scan_end_ms),
                "external_cash_flow_total": float(
                    self.account.external_cash_flow_total or 0.0
                ),
                "external_cash_flow_ids": sorted(
                    str(value) for value in self.external_cash_flow_ids
                ),
                "external_cash_flow_scan_end_ms": int(
                    self.external_cash_flow_scan_end_ms or 0
                ),
                "rpi_calibration": calibration_snapshot,
            }

    def stop(self, clean_shutdown: bool = False, reason: str = ""):
        with self.lock:
            self._stopped = True
            self._outbound_all_order_seal_reason = reason or "oms_stop"
            self._close_outbound_gate_locked("oms_stop", hold="stopped")
        shutdown_started_persisted = True
        try:
            self._audit(
                "shutdown_started",
                state=self.state.value,
                reason=reason or self._shutdown_reason or "oms_stop",
                clean_requested=bool(clean_shutdown),
                cancel_verified=bool(self._shutdown_cancel_verified),
            )
        except Exception as exc:
            shutdown_started_persisted = False
            logger.critical(
                "[OMS] Shutdown start could not be persisted: "
                f"{type(exc).__name__}:{exc}"
            )
        drained = self._wait_for_outbound_order_sends("oms_stop")
        background_tasks_stopped = self._background_tasks.shutdown(
            timeout=self.outbound_gate_drain_timeout_sec
        )
        if not background_tasks_stopped:
            logger.critical(
                "[OMS] Bounded background executor did not stop cleanly"
            )
        with self.lock:
            self.reconcile_retry_scheduled = False
            self._submit_settlement_inflight_oids.clear()
            self._submit_cancel_requested_oids.clear()
            self._deferred_cancel_oids.clear()
            self._deferred_cancel_all_symbols.clear()
            self.trade_tail_verification_inflight.clear()
            self._order_truth_resolution_inflight.clear()
        clean_shutdown = bool(
            clean_shutdown
            and shutdown_started_persisted
            and drained
            and background_tasks_stopped
            and self._shutdown_requested
            and self._shutdown_cancel_verified
        )
        paper_database = getattr(self, "paper_trade_database", None)
        paper_run_id = (
            str(getattr(paper_database, "run_id", "") or "")
            if paper_database is not None
            else ""
        )
        paper_audit_fields = (
            {"paper_run_id": paper_run_id} if paper_run_id else {}
        )
        order_monitor_stopped = True
        try:
            monitor_result = self.order_monitor.stop()
            order_monitor_stopped = monitor_result is not False
        except Exception as exc:
            order_monitor_stopped = False
            logger.critical(
                "[OMS] Order monitor did not stop cleanly: "
                f"{type(exc).__name__}:{exc}"
            )

        paper_database_stopped = True
        if paper_database is not None:
            try:
                paper_database_stopped = bool(
                    paper_database.close(
                        clean_shutdown=bool(
                            clean_shutdown and order_monitor_stopped
                        ),
                        reason=reason or self._shutdown_reason or "oms_stop",
                    )
                )
            except Exception as exc:
                paper_database_stopped = False
                logger.critical(
                    "[OMS] Paper trade database did not stop cleanly: "
                    f"{type(exc).__name__}:{exc}"
                )

        components = {
            "shutdown_started_persisted": bool(
                shutdown_started_persisted
            ),
            "outbound_sends_drained": bool(drained),
            "background_tasks_stopped": bool(background_tasks_stopped),
            "order_monitor_stopped": bool(order_monitor_stopped),
            "paper_trade_database_stopped": bool(
                paper_database_stopped
            ),
            "cancel_verified": bool(self._shutdown_cancel_verified),
        }
        checkpoint_committed = False
        if clean_shutdown and all(components.values()):
            try:
                checkpoint = self.journal.commit_checkpoint(
                    self._shutdown_checkpoint_summary()
                )
                checkpoint_committed = bool(checkpoint.get("checkpoint_sha256"))
            except Exception as exc:
                logger.critical(
                    "[OMS] Final recovery checkpoint could not be committed: "
                    f"{type(exc).__name__}:{exc}"
                )
        components["checkpoint_committed"] = checkpoint_committed
        resources_stopped = all(components.values())
        audit_ok = True
        try:
            if clean_shutdown and resources_stopped:
                self._audit(
                    "oms_stopped",
                    shutdown_protocol_version=3,
                    state=self.state.value,
                    reason=reason or self._shutdown_reason,
                    cancel_verified=True,
                    components=components,
                    manual_rearm_required=self.manual_rearm_required,
                    symbol_guard_count=len(self.symbol_guards),
                    venue_guard_count=len(self.venue_guards),
                    strategy_guard_count=len(self.strategy_guards),
                    strategy_symbol_guard_count=len(
                        self.strategy_symbol_guards
                    ),
                    **paper_audit_fields,
                )
            else:
                self._audit(
                    "shutdown_incomplete",
                    shutdown_protocol_version=3,
                    state=self.state.value,
                    reason=(
                        reason
                        or self._shutdown_reason
                        or "oms_stop_without_verification"
                    ),
                    components=components,
                    **paper_audit_fields,
                )
        except Exception as exc:
            audit_ok = False
            logger.critical(
                "[OMS] Shutdown completion could not be persisted: "
                f"{type(exc).__name__}:{exc}"
            )

        with self.lock:
            self._refresh_outbound_gate_locked("oms_stopped")

        fence_released = True
        if (
            self.single_writer_fence is not None
            and getattr(self.single_writer_fence, "handle", None) is not None
        ):
            try:
                release_result = self.single_writer_fence.release()
                fence_released = release_result is not False
            except Exception as exc:
                fence_released = False
                logger.critical(
                    "[OMS] Single-writer fence release failed: "
                    f"{type(exc).__name__}:{exc}"
                )

        stopped = bool(
            background_tasks_stopped
            and order_monitor_stopped
            and paper_database_stopped
            and fence_released
        )
        return {
            "stopped": stopped,
            "drained": bool(drained),
            "background_tasks_stopped": bool(background_tasks_stopped),
            "paper_trade_database_stopped": bool(paper_database_stopped),
            "clean": bool(
                clean_shutdown
                and resources_stopped
                and audit_ok
                and fence_released
            ),
        }
