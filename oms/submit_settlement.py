"""Durable settlement for order submission transport outcomes."""

from __future__ import annotations

import time

from event.type import (
    EVENT_ORDER_SUBMITTED,
    CommandOutcome,
    Event,
    GatewayCommandResult,
    LifecycleState,
    OMSCapabilityMode,
    OrderIntent,
    OrderRequest,
    OrderStatus,
    OrderSubmitted,
)
from infrastructure.logger import logger
from infrastructure.time_service import time_service

from .component import OMSComponent
from .journal import JournalError
from .order import Order


class OMSSubmitSettlement(OMSComponent):
    """Own final send fencing, ambiguity handling and durable settlement."""

    OWNER_READS = frozenset(
        {
            "OUTBOUND_NEW_ORDER",
            "OUTBOUND_REDUCE_ORDER",
            "_audit",
            "_close_outbound_gate_locked",
            "_emit_order_update",
            "_ensure_symbol_guard_records_locked",
            "_fail_closed_on_journal_error",
            "_get_clock_health_rejection_locked",
            "_get_margin_health_rejection_locked",
            "_get_order_block_reason",
            "_get_risk_control_heartbeat_rejection_locked",
            "_get_self_trade_prevention_rejection_locked",
            "_get_venue_dead_man_switch_rejection_locked",
            "_latch_journal_failure",
            "_lifecycle_generation",
            "_observe_rpi_calibration_loss_locked",
            "_on_order_truth_check",
            "_outbound_all_order_seal_reason",
            "_outbound_budget",
            "_outbound_gate_condition",
            "_outbound_gate_epoch",
            "_outbound_gate_holds",
            "_outbound_gate_open",
            "_outbound_gate_reason",
            "_record_command_result",
            "_record_order_snapshot",
            "_refresh_outbound_gate_locked",
            "_refresh_symbol_guard_effective_locked",
            "_rpi_calibration",
            "_rpi_calibration_effective_loss_cap_microu",
            "_rpi_calibration_expired",
            "_rpi_calibration_expiry_reason",
            "_rpi_calibration_peak_observed_loss_microu",
            "_rpi_calibration_permit_activated",
            "_rpi_calibration_reservation_ids",
            "_rpi_calibration_restart_rearm_blocked",
            "_shutdown_reason",
            "_shutdown_requested",
            "_stopped",
            "_submit_background_task",
            "_submit_cancel_requested_oids",
            "_submit_settlement_inflight_oids",
            "_symbol_guard_owner",
            "_sync_capability_mode",
            "_write_tombstone",
            "account",
            "cancel_order",
            "capability_mode",
            "degraded_aggressive_to_passive",
            "event_engine",
            "exchange_id_map",
            "exposure",
            "freeze_symbol",
            "gateway",
            "last_freeze_reason",
            "lock",
            "order_monitor",
            "orders",
            "state",
            "symbol_guard_epoch_counters",
            "symbol_guards",
            "trigger_reconcile",
        }
    )
    OWNER_WRITES = frozenset(
        {"_lifecycle_generation", "last_freeze_reason", "state"}
    )

    def _normalize_submit_command(self, raw_result) -> GatewayCommandResult:
        if isinstance(raw_result, GatewayCommandResult):
            return raw_result
        if isinstance(raw_result, str) and raw_result:
            return GatewayCommandResult(
                CommandOutcome.ACKNOWLEDGED,
                exchange_oid=raw_result,
            )
        # Compatibility for simple/custom gateways which predate the explicit
        # command outcome contract. The Binance gateway never returns bare None.
        return GatewayCommandResult(
            CommandOutcome.REJECTED,
            error_message="gateway_send_failed",
        )

    def _get_final_outbound_send_rejection_locked(
        self,
        *,
        permit_epoch: int | None,
        intent: OrderIntent,
        client_oid: str,
        risk_increasing: bool,
        allow_shutdown_emergency: bool,
    ) -> tuple[str, str]:
        """Revalidate a prepared order immediately before transport dispatch."""
        with self._outbound_gate_condition:
            if permit_epoch is None:
                return "outbound_send_permit_missing", ""
            if self._stopped:
                reason = self._shutdown_reason or "oms_stopping"
                return f"shutdown_requested:{reason}", ""
            if self._outbound_all_order_seal_reason:
                return (
                    "outbound_order_gate_closed:"
                    f"{self._outbound_all_order_seal_reason}",
                    "",
                )
            if self._shutdown_requested and not allow_shutdown_emergency:
                reason = self._shutdown_reason or "oms_stopping"
                return f"shutdown_requested:{reason}", ""
            if risk_increasing and permit_epoch != self._outbound_gate_epoch:
                return (
                    "outbound_gate_epoch_changed:"
                    f"{permit_epoch}!={self._outbound_gate_epoch}",
                    "",
                )
            if risk_increasing and not self._outbound_gate_open:
                reason = self._outbound_gate_reason or "closed"
                return f"outbound_gate_closed:{reason}", ""

        if client_oid in self._submit_cancel_requested_oids:
            return "cancel_requested_before_transport", ""

        if not risk_increasing:
            return "", ""

        block_reason = self._get_order_block_reason(
            intent.strategy_id,
            intent.symbol,
            reduce_only=intent.reduce_only,
        )
        if block_reason:
            return block_reason, ""
        if (
            self.capability_mode == OMSCapabilityMode.PASSIVE_ONLY
            and not intent.is_post_only
        ):
            return "oms_mode_passive_only_changed_before_dispatch", ""
        if (
            self.capability_mode == OMSCapabilityMode.DEGRADED
            and self.degraded_aggressive_to_passive
            and not intent.is_post_only
        ):
            return "oms_mode_degraded_changed_before_dispatch", ""

        for check in (
            self._get_clock_health_rejection_locked,
            self._get_venue_dead_man_switch_rejection_locked,
            self._get_risk_control_heartbeat_rejection_locked,
            self._get_margin_health_rejection_locked,
            self._get_self_trade_prevention_rejection_locked,
        ):
            rejection = check(intent)
            if rejection:
                return rejection, ""

        if not self._rpi_calibration["enabled"]:
            return "", ""
        if self._rpi_calibration_expired:
            return (
                "rpi_calibration_permit_expired_at_dispatch",
                self._rpi_calibration_expiry_reason or "permit_expired",
            )
        if self._rpi_calibration_restart_rearm_blocked:
            return (
                "rpi_calibration_restart_blocked_at_dispatch",
                "unclean_restart_requires_new_permit",
            )
        if client_oid not in self._rpi_calibration_reservation_ids:
            return (
                "rpi_calibration_reservation_missing_at_dispatch",
                "reservation_missing_at_dispatch",
            )
        if not self._rpi_calibration_permit_activated:
            return (
                "rpi_calibration_permit_inactive_at_dispatch",
                "permit_inactive_at_dispatch",
            )

        now_ns = time_service.now_ns()
        if now_ns < self._rpi_calibration["not_before_ns"]:
            return (
                "rpi_calibration_permit_not_yet_valid_at_dispatch",
                "permit_time_regressed_before_not_before",
            )
        if now_ns >= self._rpi_calibration["expires_at_ns"]:
            return (
                "rpi_calibration_permit_expired_at_dispatch",
                "permit_expired",
            )

        loss_truth_reason, _ = self._observe_rpi_calibration_loss_locked(
            initialize_baseline=False,
        )
        if loss_truth_reason:
            return (
                "rpi_calibration_loss_truth_unavailable_at_dispatch",
                f"calibration_loss_truth_unavailable:{loss_truth_reason}",
            )
        if (
            self._rpi_calibration_peak_observed_loss_microu
            >= self._rpi_calibration_effective_loss_cap_microu
        ):
            return (
                "rpi_calibration_loss_cap_exhausted_at_dispatch",
                "max_calibration_loss_exhausted",
            )
        return "", ""

    def _dispatch_gateway_order_with_final_fence(
        self,
        request: OrderRequest,
        client_oid: str,
        intent: OrderIntent,
        *,
        permit_epoch: int | None,
        risk_increasing: bool,
        allow_shutdown_emergency: bool = False,
    ) -> tuple[GatewayCommandResult, str]:
        terminal_reason = ""
        outbound_reservation = None
        message_kind = (
            self.OUTBOUND_REDUCE_ORDER
            if request.reduce_only
            else self.OUTBOUND_NEW_ORDER
        )

        def reserve_transport_message() -> str:
            nonlocal outbound_reservation
            if outbound_reservation is not None:
                return ""
            reservation, budget_rejection = (
                self._outbound_budget.reserve_token(message_kind)
            )
            if not budget_rejection:
                outbound_reservation = reservation
            return budget_rejection

        try:
            with self.lock:
                rejection, terminal_reason = (
                    self._get_final_outbound_send_rejection_locked(
                        permit_epoch=permit_epoch,
                        intent=intent,
                        client_oid=client_oid,
                        risk_increasing=risk_increasing,
                        allow_shutdown_emergency=allow_shutdown_emergency,
                    )
                )
                if terminal_reason:
                    self._close_outbound_gate_locked(
                        f"rpi_calibration:{terminal_reason}",
                        hold="rpi_calibration_dispatch_revoked",
                    )
        except Exception as exc:
            rejection = (
                "outbound_send_fence_unavailable:"
                f"{type(exc).__name__}:{exc}"
            )
            if self._rpi_calibration["enabled"] and risk_increasing:
                terminal_reason = "dispatch_fence_unavailable"
            with self.lock:
                self._close_outbound_gate_locked(
                    rejection,
                    hold="outbound_dispatch_fence_failure",
                )

        if rejection:
            try:
                self._audit(
                    "outbound_send_fence_rejected",
                    client_oid=client_oid,
                    symbol=intent.symbol,
                    strategy_id=intent.strategy_id,
                    permit_epoch=permit_epoch,
                    current_gate_epoch=self._outbound_gate_epoch,
                    risk_increasing=risk_increasing,
                    allow_shutdown_emergency=allow_shutdown_emergency,
                    reason=rejection,
                    calibration_terminal_reason=terminal_reason,
                )
            except Exception as exc:
                logger.critical(
                    "[OMS] Could not audit outbound send-fence rejection: "
                    f"{type(exc).__name__}:{exc}"
                )
            return (
                GatewayCommandResult(
                    CommandOutcome.REJECTED,
                    error_code="OUTBOUND_SEND_FENCE_REVOKED",
                    error_message=rejection,
                ),
                terminal_reason,
            )

        def transport_pre_send_guard():
            nonlocal terminal_reason
            try:
                with self.lock:
                    transport_rejection, transport_terminal_reason = (
                        self._get_final_outbound_send_rejection_locked(
                            permit_epoch=permit_epoch,
                            intent=intent,
                            client_oid=client_oid,
                            risk_increasing=risk_increasing,
                            allow_shutdown_emergency=(
                                allow_shutdown_emergency
                            ),
                        )
                    )
                    if transport_terminal_reason:
                        terminal_reason = transport_terminal_reason
                        self._close_outbound_gate_locked(
                            f"rpi_calibration:{terminal_reason}",
                            hold="rpi_calibration_dispatch_revoked",
                        )
            except Exception as exc:
                transport_rejection = (
                    "outbound_transport_guard_unavailable:"
                    f"{type(exc).__name__}:{exc}"
                )
                if self._rpi_calibration["enabled"] and risk_increasing:
                    terminal_reason = "transport_guard_unavailable"
                with self.lock:
                    self._close_outbound_gate_locked(
                        transport_rejection,
                        hold="outbound_transport_guard_failure",
                    )
            if transport_rejection:
                return (
                    False,
                    "OUTBOUND_SEND_FENCE_REVOKED",
                    transport_rejection,
                )
            budget_rejection = reserve_transport_message()
            if budget_rejection:
                return (
                    False,
                    "OUTBOUND_MESSAGE_BUDGET",
                    budget_rejection,
                )
            return True, "", ""

        supports_transport_guard = bool(
            getattr(
                self.gateway,
                "supports_outbound_send_guard",
                False,
            )
        )
        if (
            not supports_transport_guard
            and self._rpi_calibration["enabled"]
            and risk_increasing
        ):
            terminal_reason = "transport_guard_unavailable"
            with self.lock:
                self._close_outbound_gate_locked(
                    "rpi_calibration:transport_guard_unavailable",
                    hold="outbound_transport_guard_failure",
                )
            return (
                GatewayCommandResult(
                    CommandOutcome.REJECTED,
                    error_code="OUTBOUND_SEND_FENCE_REVOKED",
                    error_message="outbound_transport_guard_unavailable",
                ),
                terminal_reason,
            )

        try:
            if supports_transport_guard:
                raw_result = self.gateway.send_order(
                    request,
                    client_oid,
                    pre_send_guard=transport_pre_send_guard,
                )
            else:
                budget_rejection = reserve_transport_message()
                if budget_rejection:
                    return (
                        GatewayCommandResult(
                            CommandOutcome.REJECTED,
                            error_code="OUTBOUND_MESSAGE_BUDGET",
                            error_message=budget_rejection,
                        ),
                        terminal_reason,
                    )
                raw_result = self.gateway.send_order(request, client_oid)
            command = self._normalize_submit_command(raw_result)
        except Exception as exc:
            command = GatewayCommandResult(
                CommandOutcome.UNKNOWN,
                error_message=f"gateway_send_exception:{exc}",
            )
        return command, terminal_reason

    def _bind_submit_exchange_oid_locked(
        self,
        order: Order,
        exchange_oid: str,
        *,
        source: str,
    ) -> str:
        """Bind a transport ACK without overwriting stronger exchange truth."""
        exchange_oid = str(exchange_oid or "")
        if not exchange_oid:
            return ""
        if order.exchange_oid and order.exchange_oid != exchange_oid:
            reason = (
                f"submit_exchange_oid_mismatch:{order.client_oid}:"
                f"{order.exchange_oid}!={exchange_oid}"
            )
            self._audit(
                "submit_exchange_oid_mismatch",
                client_oid=order.client_oid,
                local_exchange_oid=order.exchange_oid,
                transport_exchange_oid=exchange_oid,
                source=source,
            )
            return reason
        mapped_order = self.exchange_id_map.get(exchange_oid)
        if mapped_order is not None and mapped_order is not order:
            reason = (
                f"submit_exchange_oid_collision:{order.client_oid}:"
                f"{exchange_oid}"
            )
            self._audit(
                "submit_exchange_oid_collision",
                client_oid=order.client_oid,
                exchange_oid=exchange_oid,
                mapped_client_oid=mapped_order.client_oid,
                source=source,
            )
            return reason
        order.exchange_oid = exchange_oid
        self.exchange_id_map[exchange_oid] = order
        return ""

    def _handle_submit_transport_conflict(
        self,
        order: Order,
        reason: str,
    ) -> None:
        if not reason:
            return
        context = f"submit_transport_conflict:{order.client_oid}"
        try:
            self.freeze_symbol(
                order.intent.symbol,
                f"order_truth:{reason}",
                cancel_active_orders=False,
            )
            self.trigger_reconcile(
                reason,
                suspicious_oid=order.client_oid,
            )
        except JournalError as exc:
            try:
                self._fail_closed_on_journal_error(
                    exc,
                    context,
                    order.intent.symbol,
                )
            except BaseException as fail_closed_exc:
                logger.critical(
                    "[OMS] Submit transport conflict could not complete "
                    f"fail-closed client_oid={order.client_oid}: "
                    f"{type(fail_closed_exc).__name__}:{fail_closed_exc}"
                )
        except BaseException as exc:
            self._close_gate_after_submit_settlement_failure(
                order,
                context,
                exc,
            )
        finally:
            try:
                self._on_order_truth_check(
                    f"Submit transport conflict: {reason}",
                    suspicious_oid=order.client_oid,
                )
            except BaseException as truth_exc:
                logger.critical(
                    "[OMS] Submit transport conflict could not start "
                    f"truth resolution client_oid={order.client_oid}: "
                    f"{type(truth_exc).__name__}:{truth_exc}"
                )

    def _commit_gateway_submission(self, client_oid: str) -> None:
        """Release gateways which stage exchange events until the OMS ACK is durable.

        Live gateways do not implement this hook.  A local paper venue does: its
        ``send_order`` call returns an acknowledgement without publishing NEW or
        fill events, then this hook releases those events only after the OMS has
        persisted the ACK and installed the exchange-order mapping.
        """
        commit = getattr(self.gateway, "commit_order_submission", None)
        if not callable(commit):
            return
        committed = commit(client_oid)
        if committed is False:
            raise RuntimeError("gateway rejected the submit commit barrier")

    def _notify_order_state_safely(self, order: Order, context: str) -> None:
        """Notify local observers without changing the transport outcome."""
        try:
            self.order_monitor.on_order_update(order.client_oid, order.status)
        except BaseException as exc:
            logger.critical(
                "[OMS] Order monitor notification failed after submit "
                f"context={context} client_oid={order.client_oid}: "
                f"{type(exc).__name__}:{exc}"
            )
        try:
            self._emit_order_update(order)
        except BaseException as exc:
            logger.critical(
                "[OMS] Order update publication failed after submit "
                f"context={context} client_oid={order.client_oid}: "
                f"{type(exc).__name__}:{exc}"
            )
        if context not in {"submit_ack", "internal_submit_ack"}:
            self._finish_submit_settlement(order, context)

    def _finish_submit_settlement(
        self,
        order: Order,
        context: str,
    ) -> None:
        """Release submit coordination and honor one queued cancel."""
        client_oid = order.client_oid
        with self.lock:
            if client_oid not in self._submit_settlement_inflight_oids:
                return
            self._submit_settlement_inflight_oids.discard(client_oid)
            with self._outbound_gate_condition:
                self._outbound_gate_condition.notify_all()
            cancel_requested = (
                client_oid in self._submit_cancel_requested_oids
            )
            current = self.orders.get(client_oid)
            journal_failed = "journal_failure" in self._outbound_gate_holds
            should_cancel = bool(
                cancel_requested
                and current is not None
                and current.is_active()
                and not self._stopped
                and not journal_failed
            )
            if not should_cancel:
                self._submit_cancel_requested_oids.discard(client_oid)

        if not should_cancel:
            return

        def cancel_after_settlement():
            with self.lock:
                self._submit_cancel_requested_oids.discard(client_oid)
                current_order = self.orders.get(client_oid)
                stopped = self._stopped
            if stopped or current_order is None or not current_order.is_active():
                return
            try:
                cancel_admitted = bool(self.cancel_order(client_oid))
            except BaseException as exc:
                cancel_admitted = False
                logger.critical(
                    "[OMS] Queued post-submit cancel raised "
                    f"client_oid={client_oid}: "
                    f"{type(exc).__name__}:{exc}"
                )
            if cancel_admitted:
                return
            with self.lock:
                unresolved = self.orders.get(client_oid)
                still_active = bool(
                    unresolved is not None and unresolved.is_active()
                )
            if still_active:
                self._handle_submit_transport_conflict(
                    unresolved,
                    f"queued_cancel_not_admitted:{context}",
                )

        task_key = f"post-submit-cancel:{client_oid}"
        try:
            handle = self._submit_background_task(
                task_key,
                cancel_after_settlement,
                name=f"PostSubmitCancel-{client_oid}",
                safety=True,
            )
        except BaseException as exc:
            handle = None
            logger.critical(
                "[OMS] Could not enqueue queued post-submit cancel "
                f"client_oid={client_oid}: "
                f"{type(exc).__name__}:{exc}"
            )
        if handle is None:
            with self.lock:
                self._submit_cancel_requested_oids.discard(client_oid)
                unresolved = self.orders.get(client_oid)
                still_active = bool(
                    unresolved is not None and unresolved.is_active()
                )
            cancel_admitted = False
            if still_active:
                try:
                    cancel_admitted = bool(self.cancel_order(client_oid))
                except BaseException as exc:
                    logger.critical(
                        "[OMS] Synchronous queued-cancel fallback failed "
                        f"client_oid={client_oid}: "
                        f"{type(exc).__name__}:{exc}"
                    )
            if cancel_admitted:
                return
            if still_active:
                self._handle_submit_transport_conflict(
                    unresolved,
                    f"queued_cancel_enqueue_failed:{context}",
                )

    def _publish_order_submitted_safely(
        self,
        request: OrderRequest,
        order: Order,
        submitted_status: OrderStatus,
        context: str,
    ) -> None:
        """Publish a derived event without turning a durable ACK into failure."""
        try:
            self.event_engine.put(
                Event(
                    EVENT_ORDER_SUBMITTED,
                    OrderSubmitted(
                        request,
                        order.client_oid,
                        time.time(),
                        submitted_status,
                        monotonic_timestamp=time.perf_counter(),
                    ),
                )
            )
        except BaseException as exc:
            logger.critical(
                "[OMS] Order-submitted publication failed "
                f"context={context} client_oid={order.client_oid}: "
                f"{type(exc).__name__}:{exc}"
            )

    def _audit_post_submit_safely(
        self,
        kind: str,
        order: Order,
        **payload,
    ) -> None:
        """Fail closed on durable-audit loss while preserving submit truth."""
        try:
            self._audit(kind, **payload)
        except JournalError as exc:
            try:
                self._fail_closed_on_journal_error(
                    exc,
                    f"post_submit_audit:{kind}",
                    order.intent.symbol,
                )
            except BaseException as fail_closed_exc:
                logger.critical(
                    "[OMS] Post-submit audit failure could not complete "
                    f"fail-closed context={kind} "
                    f"client_oid={order.client_oid}: "
                    f"{type(fail_closed_exc).__name__}:{fail_closed_exc}"
                )
        except BaseException as exc:
            logger.critical(
                "[OMS] Post-submit audit raised unexpectedly "
                f"context={kind} client_oid={order.client_oid}: "
                f"{type(exc).__name__}:{exc}"
            )

    def _latch_submit_ambiguity_locked(
        self,
        order: Order,
        context: str,
    ) -> str:
        """Durably freeze one symbol without waiting on the current send lease."""
        if not order.is_active():
            return ""
        symbol = str(order.intent.symbol or "").upper()
        reason = f"order_truth:submit_exception:{order.client_oid}"
        owner = self._symbol_guard_owner(reason)
        records = self._ensure_symbol_guard_records_locked(symbol)
        previous_reason = self.symbol_guards.get(symbol, "")
        previous_owner_reason = str(
            (records.get(owner) or {}).get("reason", "") or ""
        )
        epoch = max(
            [
                int(self.symbol_guard_epoch_counters.get(symbol, 0) or 0),
                *(
                    int(record.get("epoch", 0) or 0)
                    for record in records.values()
                ),
            ]
        ) + 1
        self._audit(
            (
                "symbol_frozen"
                if previous_owner_reason != reason
                else "symbol_freeze_reasserted"
            ),
            symbol=symbol,
            reason=reason,
            previous_reason=previous_reason,
            previous_owner_reason=previous_owner_reason,
            owner=owner,
            epoch=epoch,
            source=context,
        )
        records[owner] = {"reason": reason, "epoch": epoch}
        self.symbol_guard_epoch_counters[symbol] = epoch
        self._refresh_symbol_guard_effective_locked(symbol)
        self._refresh_outbound_gate_locked(
            f"submit_exception:{symbol}:{order.client_oid}"
        )
        return reason

    def _close_gate_after_submit_settlement_failure(
        self,
        order: Order,
        context: str,
        exc: BaseException,
    ) -> None:
        """Last-resort in-memory fence when even ambiguity settlement fails."""
        logger.critical(
            "[OMS] Submit exception settlement failed "
            f"context={context} client_oid={order.client_oid}: "
            f"{type(exc).__name__}:{exc}"
        )
        try:
            with self.lock:
                if order.status == OrderStatus.SUBMITTING:
                    order.mark_submit_unknown(
                        f"submit_settlement_failed:{type(exc).__name__}"
                    )
                self._close_outbound_gate_locked(
                    f"submit_settlement_failed:{context}",
                    hold="submit_exception_settlement_failure",
                )
                if self.state not in {
                    LifecycleState.HALTED,
                    LifecycleState.RECONCILING,
                }:
                    self.state = LifecycleState.FROZEN
                    self._lifecycle_generation += 1
                    self.last_freeze_reason = (
                        f"submit_settlement_failed:{context}"
                    )
                    self._sync_capability_mode(self.last_freeze_reason)
        except BaseException as fence_exc:
            logger.critical(
                "[OMS] Could not install submit settlement fallback fence "
                f"client_oid={order.client_oid}: "
                f"{type(fence_exc).__name__}:{fence_exc}"
            )

    def _cleanup_pre_dispatch_submit_exception(
        self,
        order: Order | None,
        exc: BaseException,
        context: str,
        snapshot_source: str,
        **snapshot_extra,
    ) -> JournalError | None:
        """Durably terminate a prepared order which never reached transport."""
        if order is None:
            return None
        journal_failure = None
        try:
            with self.lock:
                if order.status in {
                    OrderStatus.CREATED,
                    OrderStatus.SUBMITTING,
                }:
                    order.mark_rejected_locally(
                        f"pre_dispatch_exception:{type(exc).__name__}"
                    )
                self._record_order_snapshot(
                    order,
                    f"{snapshot_source}_pre_dispatch_exception",
                    exception_type=type(exc).__name__,
                    **snapshot_extra,
                )
                if order.is_terminal():
                    self._write_tombstone(order)
                    self.orders.pop(order.client_oid, None)
                    self.exposure.update_open_orders(self.orders)
                    self.account.calculate()
        except JournalError as journal_exc:
            self._latch_journal_failure(
                journal_exc,
                f"{context}_cleanup",
                order.intent.symbol,
            )
            journal_failure = journal_exc
            try:
                with self.lock:
                    if order.status in {
                        OrderStatus.CREATED,
                        OrderStatus.SUBMITTING,
                    }:
                        order.mark_rejected_locally(
                            "durable_journal_unavailable"
                        )
                    if order.is_terminal():
                        self.orders.pop(order.client_oid, None)
                        self.exposure.update_open_orders(self.orders)
                        self.account.calculate()
            except BaseException as cleanup_exc:
                self._close_gate_after_submit_settlement_failure(
                    order,
                    f"{context}_journal_cleanup",
                    cleanup_exc,
                )
        except BaseException as cleanup_exc:
            self._close_gate_after_submit_settlement_failure(
                order,
                context,
                cleanup_exc,
            )
        self._notify_order_state_safely(order, context)
        return journal_failure

    def _settle_post_dispatch_submit_exception(
        self,
        order: Order,
        command_id: str,
        exc: BaseException,
        context: str,
        snapshot_source: str,
        exchange_oid: str = "",
        **snapshot_extra,
    ) -> JournalError | None:
        """Persist ambiguity and quarantine the symbol before lease release."""
        journal_failure = None
        error_message = (
            f"post_dispatch_exception:{type(exc).__name__}:"
            f"{str(exc)[:512]}"
        )
        try:
            self._record_command_result(
                command_id,
                "SUBMIT",
                order,
                CommandOutcome.UNKNOWN,
                exchange_oid=exchange_oid or order.exchange_oid,
                error_message=error_message,
            )
            with self.lock:
                if order.status == OrderStatus.SUBMITTING:
                    order.mark_submit_unknown(error_message)
                self._record_order_snapshot(
                    order,
                    f"{snapshot_source}_post_dispatch_exception",
                    exception_type=type(exc).__name__,
                    exception_message=str(exc)[:512],
                    **snapshot_extra,
                )
                self._latch_submit_ambiguity_locked(order, context)
        except JournalError as journal_exc:
            self._latch_journal_failure(
                journal_exc,
                f"{context}_settlement",
                order.intent.symbol,
            )
            journal_failure = journal_exc
            try:
                with self.lock:
                    if order.status == OrderStatus.SUBMITTING:
                        order.mark_submit_unknown("result_not_durable")
            except BaseException as cleanup_exc:
                self._close_gate_after_submit_settlement_failure(
                    order,
                    f"{context}_journal_cleanup",
                    cleanup_exc,
                )
        except BaseException as settlement_exc:
            self._close_gate_after_submit_settlement_failure(
                order,
                context,
                settlement_exc,
            )

        self._notify_order_state_safely(order, context)
        try:
            self._on_order_truth_check(
                f"Post-dispatch submit exception: {type(exc).__name__}",
                suspicious_oid=order.client_oid,
            )
        except BaseException as truth_exc:
            logger.critical(
                "[OMS] Could not start client-ID truth resolution after "
                f"submit exception client_oid={order.client_oid}: "
                f"{type(truth_exc).__name__}:{truth_exc}"
            )
        return journal_failure
