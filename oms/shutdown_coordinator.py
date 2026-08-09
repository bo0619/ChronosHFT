"""Ordered, durable OMS shutdown coordination."""

from __future__ import annotations

from infrastructure.logger import logger

from .component import OMSComponent


class OMSShutdownCoordinator(OMSComponent):
    """Run shutdown through the lifecycle component's declared capabilities."""

    OWNER_READS = frozenset(
        {
            "_audit",
            "_background_tasks",
            "_close_outbound_gate_locked",
            "_deferred_cancel_all_symbols",
            "_deferred_cancel_oids",
            "_order_truth_resolution_inflight",
            "_refresh_outbound_gate_locked",
            "_rpi_calibration_snapshot_locked",
            "_shutdown_cancel_verified",
            "_shutdown_reason",
            "_shutdown_requested",
            "_submit_cancel_requested_oids",
            "_submit_settlement_inflight_oids",
            "_wait_for_outbound_order_sends",
            "account",
            "capability_mode",
            "execution_ids",
            "exposure",
            "external_cash_flow_ids",
            "external_cash_flow_scan_end_ms",
            "guard_store",
            "journal",
            "last_freeze_reason",
            "last_halt_reason",
            "lock",
            "manual_rearm_required",
            "mode_constraint_generation",
            "mode_constraints",
            "mode_override",
            "mode_override_reason",
            "order_monitor",
            "order_store",
            "outbound_gate_drain_timeout_sec",
            "paper_trade_database",
            "single_writer_fence",
            "state",
            "symbol_guard_records",
            "symbol_guards",
            "terminated_oids",
            "trade_cursors",
            "trade_scan_end_ms",
            "trade_tail_verification_inflight",
            "venue_guard_records",
            "venue_guards",
        }
    )
    OWNER_WRITES = frozenset(
        {
            "_outbound_all_order_seal_reason",
            "_stopped",
            "reconcile_retry_scheduled",
        }
    )

    def checkpoint_summary(self) -> dict:
        owner = self
        with owner.lock:
            strategy_exposure = list(
                owner.exposure.strategy_checkpoint_rows()
            )
            calibration_snapshot = owner._rpi_calibration_snapshot_locked()
            strategy_guard_payload = (
                owner.guard_store.checkpoint_payload()
            )
            return {
                "state_version": 1,
                "state": owner.state.value,
                "capability_mode": owner.capability_mode.value,
                "manual_rearm_required": bool(
                    owner.manual_rearm_required
                ),
                "last_freeze_reason": str(owner.last_freeze_reason or ""),
                "last_halt_reason": str(owner.last_halt_reason or ""),
                "active_orders": [
                    order.to_record()
                    for _client_oid, order in sorted(
                        owner.order_store.active_view().items()
                    )
                ],
                "terminated_oids": sorted(
                    str(oid) for oid in owner.terminated_oids
                ),
                "execution_ids": sorted(
                    str(value) for value in owner.execution_ids
                ),
                "strategy_exposure": strategy_exposure,
                "symbol_guards": dict(owner.symbol_guards),
                "symbol_guard_records": dict(owner.symbol_guard_records),
                "venue_guards": dict(owner.venue_guards),
                "venue_guard_records": dict(owner.venue_guard_records),
                "strategy_guards": strategy_guard_payload[
                    "strategy_guards"
                ],
                "strategy_symbol_guards": strategy_guard_payload[
                    "strategy_symbol_guards"
                ],
                "mode_override": str(owner.mode_override or ""),
                "mode_override_reason": str(
                    owner.mode_override_reason or ""
                ),
                "mode_constraint_generation": int(
                    owner.mode_constraint_generation or 0
                ),
                "mode_constraints": dict(owner.mode_constraints),
                "trade_cursors": dict(owner.trade_cursors),
                "trade_scan_end_ms": dict(owner.trade_scan_end_ms),
                "external_cash_flow_total": float(
                    owner.account.external_cash_flow_total or 0.0
                ),
                "external_cash_flow_ids": sorted(
                    str(value) for value in owner.external_cash_flow_ids
                ),
                "external_cash_flow_scan_end_ms": int(
                    owner.external_cash_flow_scan_end_ms or 0
                ),
                "rpi_calibration": calibration_snapshot,
            }

    def stop(self, clean_shutdown: bool = False, reason: str = ""):
        owner = self
        with owner.lock:
            owner._stopped = True
            owner._outbound_all_order_seal_reason = reason or "oms_stop"
            owner._close_outbound_gate_locked("oms_stop", hold="stopped")
        shutdown_started_persisted = True
        try:
            owner._audit(
                "shutdown_started",
                state=owner.state.value,
                reason=reason or owner._shutdown_reason or "oms_stop",
                clean_requested=bool(clean_shutdown),
                cancel_verified=bool(owner._shutdown_cancel_verified),
            )
        except Exception as exc:
            shutdown_started_persisted = False
            logger.critical(
                "[OMS] Shutdown start could not be persisted: "
                f"{type(exc).__name__}:{exc}"
            )
        drained = owner._wait_for_outbound_order_sends("oms_stop")
        background_tasks_stopped = owner._background_tasks.shutdown(
            timeout=owner.outbound_gate_drain_timeout_sec
        )
        if not background_tasks_stopped:
            logger.critical(
                "[OMS] Bounded background executor did not stop cleanly"
            )
        with owner.lock:
            owner.reconcile_retry_scheduled = False
            owner._submit_settlement_inflight_oids.clear()
            owner._submit_cancel_requested_oids.clear()
            owner._deferred_cancel_oids.clear()
            owner._deferred_cancel_all_symbols.clear()
            owner.trade_tail_verification_inflight.clear()
            owner._order_truth_resolution_inflight.clear()
        clean_shutdown = bool(
            clean_shutdown
            and shutdown_started_persisted
            and drained
            and background_tasks_stopped
            and owner._shutdown_requested
            and owner._shutdown_cancel_verified
        )
        paper_database = getattr(owner, "paper_trade_database", None)
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
            monitor_result = owner.order_monitor.stop()
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
                        reason=(
                            reason or owner._shutdown_reason or "oms_stop"
                        ),
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
            "cancel_verified": bool(owner._shutdown_cancel_verified),
        }
        checkpoint_committed = False
        if clean_shutdown and all(components.values()):
            try:
                checkpoint = owner.journal.commit_checkpoint(
                    self.checkpoint_summary()
                )
                checkpoint_committed = bool(
                    checkpoint.get("checkpoint_sha256")
                )
            except Exception as exc:
                logger.critical(
                    "[OMS] Final recovery checkpoint could not be committed: "
                    f"{type(exc).__name__}:{exc}"
                )
        components["checkpoint_committed"] = checkpoint_committed
        resources_stopped = all(components.values())
        strategy_guard_snapshot = owner.guard_store.snapshot()
        audit_ok = True
        try:
            if clean_shutdown and resources_stopped:
                owner._audit(
                    "oms_stopped",
                    shutdown_protocol_version=3,
                    state=owner.state.value,
                    reason=reason or owner._shutdown_reason,
                    cancel_verified=True,
                    components=components,
                    manual_rearm_required=owner.manual_rearm_required,
                    symbol_guard_count=len(owner.symbol_guards),
                    venue_guard_count=len(owner.venue_guards),
                    strategy_guard_count=(
                        strategy_guard_snapshot.strategy_guard_count
                    ),
                    strategy_symbol_guard_count=(
                        strategy_guard_snapshot.strategy_symbol_guard_count
                    ),
                    **paper_audit_fields,
                )
            else:
                owner._audit(
                    "shutdown_incomplete",
                    shutdown_protocol_version=3,
                    state=owner.state.value,
                    reason=(
                        reason
                        or owner._shutdown_reason
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

        with owner.lock:
            owner._refresh_outbound_gate_locked("oms_stopped")

        fence_released = True
        if (
            owner.single_writer_fence is not None
            and getattr(owner.single_writer_fence, "handle", None) is not None
        ):
            try:
                release_result = owner.single_writer_fence.release()
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
