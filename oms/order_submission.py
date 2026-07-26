"""Order submission pipelines for strategy and OMS-internal orders."""

from __future__ import annotations

import uuid

from infrastructure.logger import logger

from event.type import (
    CommandOutcome,
    OrderIntent,
    OrderRequest,
    OrderStatus,
    OrderSubmitResult,
    TIF_IOC,
)

from .component import OMSComponent
from .journal import JournalError
from .order import Order


class OMSOrderSubmission(OMSComponent):
    """Own prepare, fence, dispatch and durable submit settlement."""

    def _reject_intent_locally(
        self,
        intent: OrderIntent,
        client_oid: str,
        reason: str,
        **extra,
    ):
        order = Order(client_oid, intent)
        order.mark_rejected_locally(reason)
        with self.lock:
            self._record_order_snapshot(order, "intent_rejected", **extra)
            self._emit_order_update(order)
        self._write_tombstone(order)
        audit_payload = {
            "reason": reason,
            "intent": self._serialize_intent(intent),
            "client_oid": client_oid,
        }
        audit_payload.update(extra)
        self._audit("intent_rejected", **audit_payload)
        return OrderSubmitResult(
            accepted=False,
            client_oid=client_oid,
            reason=reason,
            state=self.state.value,
        )

    def _submit_internal_order(
        self,
        intent: OrderIntent,
        request: OrderRequest,
        client_oid: str,
        snapshot_source: str,
        audit_kind: str,
        **audit_extra,
    ) -> bool:
        order = Order(client_oid, intent)
        command_id = f"SUBMIT:{client_oid}"
        prepared_records = None
        order_send_risk_increasing = not request.reduce_only
        order_send_permit = False
        allow_shutdown_emergency = bool(
            intent.strategy_id == "system_emergency"
            and intent.reduce_only
            and request.reduce_only
            and intent.order_type == "MARKET"
            and request.order_type == "MARKET"
            and intent.time_in_force == TIF_IOC
            and request.time_in_force == TIF_IOC
            and not intent.is_post_only
            and not request.post_only
            and str(intent.tag or "").startswith("reduce_only_flatten:")
        )

        with self.lock:
            permit_epoch, permit_rejection = (
                self._acquire_outbound_order_send_permit_locked(
                    risk_increasing=order_send_risk_increasing,
                    symbol=request.symbol,
                    allow_shutdown_emergency=allow_shutdown_emergency,
                )
            )
            order_send_permit = permit_epoch is not None
            budget_rejection = permit_rejection
        if budget_rejection:
            if order_send_permit:
                self._release_outbound_order_send_permit(
                    risk_increasing=order_send_risk_increasing,
                    symbol=request.symbol,
                )
                order_send_permit = False
            logger.error(
                f"[OMS] Internal order blocked by message budget: {budget_rejection}"
            )
            self._audit(
                "internal_order_message_budget_rejected",
                client_oid=client_oid,
                symbol=intent.symbol,
                reduce_only=request.reduce_only,
                reason=budget_rejection,
            )
            return False

        try:
            with self.lock:
                self._audit_rpi_calibration_emergency_bypass_locked(
                    intent,
                    request,
                    client_oid,
                )
                if self._shutdown_requested:
                    if not allow_shutdown_emergency:
                        raise JournalError(
                            "Shutdown order bypass failed strict emergency validation"
                        )
                    self._audit(
                        "shutdown_emergency_reduce_bypass",
                        client_oid=client_oid,
                        symbol=request.symbol,
                        side=request.side,
                        quantity=request.volume,
                        reason=intent.tag,
                    )
                self.orders[client_oid] = order
                order.mark_submitting()
                self.exposure.update_open_orders(self.orders)
                self.account.calculate()
                self._schedule_rpi_calibration_runtime_enforcement(
                    terminal_truth_changed=True,
                )
                self._submit_settlement_inflight_oids.add(client_oid)
                prepared_records = self._build_submit_prepared_records(
                    command_id,
                    order,
                    request,
                    snapshot_source,
                    **audit_extra,
                )
            self._record_submit_prepared_batch(
                prepared_records,
            )
            self.order_monitor.track_prepared_order(order)
        except JournalError as exc:
            self._latch_journal_failure(
                exc,
                "prepare_internal_submit",
                intent.symbol,
            )
            try:
                with self.lock:
                    if order.status in {
                        OrderStatus.CREATED,
                        OrderStatus.SUBMITTING,
                    }:
                        order.mark_rejected_locally(
                            "durable_journal_unavailable"
                        )
                    self.orders.pop(client_oid, None)
                    self.exposure.update_open_orders(self.orders)
                    self.account.calculate()
            except BaseException as cleanup_exc:
                self._close_gate_after_submit_settlement_failure(
                    order,
                    "prepare_internal_submit_journal_cleanup",
                    cleanup_exc,
                )
            self._notify_order_state_safely(
                order,
                "prepare_internal_submit_journal_failure",
            )
            if order_send_permit:
                self._release_outbound_order_send_permit(
                    risk_increasing=order_send_risk_increasing,
                    symbol=request.symbol,
                )
                order_send_permit = False
            try:
                self._fail_closed_on_journal_error(
                    exc,
                    "prepare_internal_submit",
                    intent.symbol,
                )
            except BaseException as fail_closed_exc:
                logger.critical(
                    "[OMS] Internal submit prepare failure could not "
                    "complete fail-closed: "
                    f"{type(fail_closed_exc).__name__}:{fail_closed_exc}"
                )
            return False
        except BaseException as exc:
            cleanup_journal_failure = None
            try:
                cleanup_journal_failure = (
                    self._cleanup_pre_dispatch_submit_exception(
                        order,
                        exc,
                        "prepare_internal_submit_exception",
                        snapshot_source,
                        **audit_extra,
                    )
                )
            except BaseException as cleanup_exc:
                self._close_gate_after_submit_settlement_failure(
                    order,
                    "prepare_internal_submit_exception",
                    cleanup_exc,
                )
            finally:
                if order_send_permit:
                    self._release_outbound_order_send_permit(
                        risk_increasing=order_send_risk_increasing,
                        symbol=request.symbol,
                    )
                    order_send_permit = False
            if cleanup_journal_failure is not None:
                try:
                    self._fail_closed_on_journal_error(
                        cleanup_journal_failure,
                        "prepare_internal_submit_exception_cleanup",
                        intent.symbol,
                    )
                except BaseException as fail_closed_exc:
                    logger.critical(
                        "[OMS] Internal pre-dispatch cleanup could not "
                        "complete fail-closed: "
                        f"{type(fail_closed_exc).__name__}:"
                        f"{fail_closed_exc}"
                    )
            raise

        try:
            command, _ = self._dispatch_gateway_order_with_final_fence(
                request,
                client_oid,
                intent,
                permit_epoch=permit_epoch,
                risk_increasing=order_send_risk_increasing,
                allow_shutdown_emergency=allow_shutdown_emergency,
            )
        except BaseException as exc:
            settlement_journal_failure = None
            try:
                settlement_journal_failure = (
                    self._settle_post_dispatch_submit_exception(
                        order,
                        command_id,
                        exc,
                        "dispatch_internal_submit_exception",
                        snapshot_source,
                        **audit_extra,
                    )
                )
            except BaseException as settlement_exc:
                self._close_gate_after_submit_settlement_failure(
                    order,
                    "dispatch_internal_submit_exception",
                    settlement_exc,
                )
            finally:
                if order_send_permit:
                    self._release_outbound_order_send_permit(
                        risk_increasing=order_send_risk_increasing,
                        symbol=request.symbol,
                    )
                    order_send_permit = False
            if settlement_journal_failure is not None:
                try:
                    self._fail_closed_on_journal_error(
                        settlement_journal_failure,
                        "dispatch_internal_submit_exception",
                        intent.symbol,
                    )
                except BaseException as fail_closed_exc:
                    logger.critical(
                        "[OMS] Internal post-dispatch failure could not "
                        "complete fail-closed: "
                        f"{type(fail_closed_exc).__name__}:"
                        f"{fail_closed_exc}"
                    )
            raise

        try:
            self._record_command_result(
                command_id,
                "SUBMIT",
                order,
                command.outcome,
                exchange_oid=command.exchange_oid,
                error_code=command.error_code,
                error_message=command.error_message,
            )
        except JournalError as exc:
            self._latch_journal_failure(
                exc,
                "result_internal_submit",
                intent.symbol,
            )
            try:
                with self.lock:
                    if order.status == OrderStatus.SUBMITTING:
                        order.mark_submit_unknown("result_not_durable")
            except BaseException as cleanup_exc:
                self._close_gate_after_submit_settlement_failure(
                    order,
                    "result_internal_submit_journal_cleanup",
                    cleanup_exc,
                )
            self._notify_order_state_safely(
                order,
                "result_internal_submit_journal_failure",
            )
            if order_send_permit:
                self._release_outbound_order_send_permit(
                    risk_increasing=order_send_risk_increasing,
                    symbol=request.symbol,
                )
                order_send_permit = False
            try:
                self._fail_closed_on_journal_error(
                    exc,
                    "result_internal_submit",
                    intent.symbol,
                )
            except BaseException as fail_closed_exc:
                logger.critical(
                    "[OMS] Internal submit result failure could not "
                    "complete fail-closed: "
                    f"{type(fail_closed_exc).__name__}:{fail_closed_exc}"
                )
            try:
                self._on_order_truth_check(
                    "Submit result could not be persisted",
                    suspicious_oid=client_oid,
                )
            except BaseException as truth_exc:
                logger.critical(
                    "[OMS] Internal submit result failure could not start "
                    f"truth resolution: {type(truth_exc).__name__}:"
                    f"{truth_exc}"
                )
            return True
        except BaseException as exc:
            settlement_journal_failure = None
            try:
                settlement_journal_failure = (
                    self._settle_post_dispatch_submit_exception(
                        order,
                        command_id,
                        exc,
                        "result_internal_submit_exception",
                        snapshot_source,
                        exchange_oid=command.exchange_oid,
                        **audit_extra,
                    )
                )
            except BaseException as settlement_exc:
                self._close_gate_after_submit_settlement_failure(
                    order,
                    "result_internal_submit_exception",
                    settlement_exc,
                )
            finally:
                if order_send_permit:
                    self._release_outbound_order_send_permit(
                        risk_increasing=order_send_risk_increasing,
                        symbol=request.symbol,
                    )
                    order_send_permit = False
            if settlement_journal_failure is not None:
                try:
                    self._fail_closed_on_journal_error(
                        settlement_journal_failure,
                        "result_internal_submit_exception",
                        intent.symbol,
                    )
                except BaseException as fail_closed_exc:
                    logger.critical(
                        "[OMS] Internal result exception could not "
                        "complete fail-closed: "
                        f"{type(fail_closed_exc).__name__}:"
                        f"{fail_closed_exc}"
                    )
            raise

        if command.outcome == CommandOutcome.ACKNOWLEDGED:
            exchange_oid = command.exchange_oid
            transport_conflict = ""
            try:
                with self.lock:
                    status_before_transport = order.status
                    transport_conflict = self._bind_submit_exchange_oid_locked(
                        order,
                        exchange_oid,
                        source=f"{snapshot_source}_ack",
                    )
                    if order.status in {
                        OrderStatus.SUBMITTING,
                        OrderStatus.SUBMIT_UNKNOWN,
                    }:
                        order.mark_pending_ack(order.exchange_oid)
                    submitted_status = order.status
                    snapshot_suffix = (
                        "ack"
                        if status_before_transport
                        in {
                            OrderStatus.SUBMITTING,
                            OrderStatus.SUBMIT_UNKNOWN,
                        }
                        else "ack_after_exchange_truth"
                    )
                    self._record_order_snapshot(
                        order,
                        f"{snapshot_source}_{snapshot_suffix}",
                        **audit_extra,
                    )
            except JournalError as exc:
                self._latch_journal_failure(
                    exc,
                    "snapshot_internal_submit_ack",
                    intent.symbol,
                )
                try:
                    with self.lock:
                        if order.status == OrderStatus.SUBMITTING:
                            order.mark_submit_unknown(
                                "ack_snapshot_not_durable"
                            )
                except BaseException as cleanup_exc:
                    self._close_gate_after_submit_settlement_failure(
                        order,
                        "snapshot_internal_submit_ack_journal_cleanup",
                        cleanup_exc,
                    )
                if order_send_permit:
                    self._release_outbound_order_send_permit(
                        risk_increasing=order_send_risk_increasing,
                        symbol=request.symbol,
                    )
                    order_send_permit = False
                self._notify_order_state_safely(
                    order,
                    "snapshot_internal_submit_ack_failure",
                )
                try:
                    self._on_order_truth_check(
                        "Submit ACK snapshot could not be persisted",
                        suspicious_oid=client_oid,
                    )
                except BaseException as truth_exc:
                    logger.critical(
                        "[OMS] Internal ACK snapshot failure could not "
                        "start truth resolution: "
                        f"{type(truth_exc).__name__}:{truth_exc}"
                    )
                try:
                    self._fail_closed_on_journal_error(
                        exc,
                        "snapshot_internal_submit_ack",
                        intent.symbol,
                    )
                except BaseException as fail_closed_exc:
                    logger.critical(
                        "[OMS] Internal ACK snapshot failure could not "
                        "complete fail-closed: "
                        f"{type(fail_closed_exc).__name__}:"
                        f"{fail_closed_exc}"
                    )
                return True
            except BaseException as exc:
                settlement_journal_failure = None
                try:
                    settlement_journal_failure = (
                        self._settle_post_dispatch_submit_exception(
                            order,
                            command_id,
                            exc,
                            "snapshot_internal_submit_ack_exception",
                            snapshot_source,
                            exchange_oid=exchange_oid,
                            **audit_extra,
                        )
                    )
                except BaseException as settlement_exc:
                    self._close_gate_after_submit_settlement_failure(
                        order,
                        "snapshot_internal_submit_ack_exception",
                        settlement_exc,
                    )
                finally:
                    if order_send_permit:
                        self._release_outbound_order_send_permit(
                            risk_increasing=order_send_risk_increasing,
                            symbol=request.symbol,
                        )
                        order_send_permit = False
                if settlement_journal_failure is not None:
                    try:
                        self._fail_closed_on_journal_error(
                            settlement_journal_failure,
                            "snapshot_internal_submit_ack_exception",
                            intent.symbol,
                        )
                    except BaseException as fail_closed_exc:
                        logger.critical(
                            "[OMS] Internal ACK settlement exception could "
                            "not complete fail-closed: "
                            f"{type(fail_closed_exc).__name__}:"
                            f"{fail_closed_exc}"
                        )
                raise

            if order_send_permit:
                self._release_outbound_order_send_permit(
                    risk_increasing=order_send_risk_increasing,
                    symbol=request.symbol,
                )
                order_send_permit = False

            self._notify_order_state_safely(
                order,
                "internal_submit_ack",
            )
            self._publish_order_submitted_safely(
                request,
                order,
                submitted_status,
                "internal_submit_ack",
            )
            self._handle_submit_transport_conflict(order, transport_conflict)
            try:
                self._commit_gateway_submission(client_oid)
            except BaseException as exc:
                logger.error(
                    f"[OMS] Gateway submit commit failed for {client_oid}: {exc}"
                )
                self._handle_submit_transport_conflict(
                    order,
                    f"submit_commit_failed:{client_oid}",
                )
            self._finish_submit_settlement(
                order,
                "internal_submit_ack_committed",
            )
            payload = {
                "client_oid": client_oid,
                "exchange_oid": exchange_oid,
                "symbol": intent.symbol,
                "side": intent.side.value,
                "price": intent.price,
                "volume": intent.volume,
            }
            payload.update(audit_extra)
            self._audit_post_submit_safely(audit_kind, order, **payload)
            return True

        if command.outcome == CommandOutcome.UNKNOWN:
            try:
                with self.lock:
                    if order.status == OrderStatus.SUBMITTING:
                        order.mark_submit_unknown(
                            command.error_message
                            or "submit_outcome_unknown"
                        )
                    transport_still_unknown = (
                        order.status == OrderStatus.SUBMIT_UNKNOWN
                    )
                    submitted_status = order.status
                    self._record_order_snapshot(
                        order,
                        (
                            f"{snapshot_source}_unknown"
                            if transport_still_unknown
                            else (
                                f"{snapshot_source}_unknown_after_"
                                "exchange_truth"
                            )
                        ),
                        error_code=command.error_code,
                        **audit_extra,
                    )
            except JournalError as exc:
                self._latch_journal_failure(
                    exc,
                    "snapshot_internal_submit_unknown",
                    intent.symbol,
                )
                if order_send_permit:
                    self._release_outbound_order_send_permit(
                        risk_increasing=order_send_risk_increasing,
                        symbol=request.symbol,
                    )
                    order_send_permit = False
                self._notify_order_state_safely(
                    order,
                    "snapshot_internal_submit_unknown_failure",
                )
                try:
                    self._fail_closed_on_journal_error(
                        exc,
                        "snapshot_internal_submit_unknown",
                        intent.symbol,
                    )
                except BaseException as fail_closed_exc:
                    logger.critical(
                        "[OMS] Internal UNKNOWN snapshot failure could not "
                        "complete fail-closed: "
                        f"{type(fail_closed_exc).__name__}:"
                        f"{fail_closed_exc}"
                    )
                try:
                    self._on_order_truth_check(
                        "Submit result could not be persisted",
                        suspicious_oid=client_oid,
                    )
                except BaseException as truth_exc:
                    logger.critical(
                        "[OMS] Internal UNKNOWN snapshot failure could not "
                        "start truth resolution: "
                        f"{type(truth_exc).__name__}:{truth_exc}"
                    )
                return True
            except BaseException as exc:
                settlement_journal_failure = None
                try:
                    settlement_journal_failure = (
                        self._settle_post_dispatch_submit_exception(
                            order,
                            command_id,
                            exc,
                            "snapshot_internal_submit_unknown_exception",
                            snapshot_source,
                            exchange_oid=command.exchange_oid,
                            **audit_extra,
                        )
                    )
                except BaseException as settlement_exc:
                    self._close_gate_after_submit_settlement_failure(
                        order,
                        "snapshot_internal_submit_unknown_exception",
                        settlement_exc,
                    )
                finally:
                    if order_send_permit:
                        self._release_outbound_order_send_permit(
                            risk_increasing=order_send_risk_increasing,
                            symbol=request.symbol,
                        )
                        order_send_permit = False
                if settlement_journal_failure is not None:
                    try:
                        self._fail_closed_on_journal_error(
                            settlement_journal_failure,
                            "snapshot_internal_submit_unknown_exception",
                            intent.symbol,
                        )
                    except BaseException as fail_closed_exc:
                        logger.critical(
                            "[OMS] Internal UNKNOWN settlement exception "
                            "could not complete fail-closed: "
                            f"{type(fail_closed_exc).__name__}:"
                            f"{fail_closed_exc}"
                        )
                raise
            if order_send_permit:
                self._release_outbound_order_send_permit(
                    risk_increasing=order_send_risk_increasing,
                    symbol=request.symbol,
                )
                order_send_permit = False
            self._notify_order_state_safely(
                order,
                "internal_submit_unknown",
            )
            self._publish_order_submitted_safely(
                request,
                order,
                submitted_status,
                "internal_submit_unknown",
            )
            if not transport_still_unknown:
                self._audit_post_submit_safely(
                    f"{audit_kind}_transport_unknown_resolved",
                    order,
                    client_oid=client_oid,
                    symbol=intent.symbol,
                    exchange_status=submitted_status.value,
                    exchange_oid=order.exchange_oid,
                    error_code=command.error_code,
                    error_message=command.error_message,
                    **audit_extra,
                )
                return True
            self._handle_submit_transport_conflict(
                order,
                f"submit_unknown:{client_oid}",
            )
            self._audit_post_submit_safely(
                f"{audit_kind}_unknown",
                order,
                client_oid=client_oid,
                symbol=intent.symbol,
                error_code=command.error_code,
                error_message=command.error_message,
                **audit_extra,
            )
            return True

        transport_rejection_superseded = False
        try:
            with self.lock:
                reject_reason = command.error_message or command.error_code or "gateway_send_rejected"
                if order.status in {
                    OrderStatus.SUBMITTING,
                    OrderStatus.SUBMIT_UNKNOWN,
                }:
                    order.mark_rejected_locally(reject_reason)
                else:
                    transport_rejection_superseded = True
                submitted_status = order.status
                self._record_order_snapshot(
                    order,
                    (
                        f"{snapshot_source}_failed"
                        if not transport_rejection_superseded
                        else (
                            f"{snapshot_source}_rejection_after_"
                            "exchange_truth"
                        )
                    ),
                    **audit_extra,
                )
                if not transport_rejection_superseded:
                    self.orders.pop(client_oid, None)
                    self.exposure.update_open_orders(self.orders)
                    self.account.calculate()
        except JournalError as exc:
            self._latch_journal_failure(
                exc,
                "snapshot_internal_submit_rejected",
                intent.symbol,
            )
            if order_send_permit:
                self._release_outbound_order_send_permit(
                    risk_increasing=order_send_risk_increasing,
                    symbol=request.symbol,
                )
                order_send_permit = False
            self._notify_order_state_safely(
                order,
                "snapshot_internal_submit_rejected_failure",
            )
            try:
                self._fail_closed_on_journal_error(
                    exc,
                    "snapshot_internal_submit_rejected",
                    intent.symbol,
                )
            except BaseException as fail_closed_exc:
                logger.critical(
                    "[OMS] Internal rejection snapshot failure could not "
                    "complete fail-closed: "
                    f"{type(fail_closed_exc).__name__}:{fail_closed_exc}"
                )
            return transport_rejection_superseded
        except BaseException as exc:
            settlement_journal_failure = None
            try:
                settlement_journal_failure = (
                    self._settle_post_dispatch_submit_exception(
                        order,
                        command_id,
                        exc,
                        "snapshot_internal_submit_rejected_exception",
                        snapshot_source,
                        exchange_oid=command.exchange_oid,
                        **audit_extra,
                    )
                )
            except BaseException as settlement_exc:
                self._close_gate_after_submit_settlement_failure(
                    order,
                    "snapshot_internal_submit_rejected_exception",
                    settlement_exc,
                )
            finally:
                if order_send_permit:
                    self._release_outbound_order_send_permit(
                        risk_increasing=order_send_risk_increasing,
                        symbol=request.symbol,
                    )
                    order_send_permit = False
            if settlement_journal_failure is not None:
                try:
                    self._fail_closed_on_journal_error(
                        settlement_journal_failure,
                        "snapshot_internal_submit_rejected_exception",
                        intent.symbol,
                    )
                except BaseException as fail_closed_exc:
                    logger.critical(
                        "[OMS] Internal rejection settlement exception "
                        "could not complete fail-closed: "
                        f"{type(fail_closed_exc).__name__}:"
                        f"{fail_closed_exc}"
                    )
            raise
        if order_send_permit:
            self._release_outbound_order_send_permit(
                risk_increasing=order_send_risk_increasing,
                symbol=request.symbol,
            )
            order_send_permit = False
        self._notify_order_state_safely(
            order,
            "internal_submit_rejected",
        )
        if transport_rejection_superseded:
            self._publish_order_submitted_safely(
                request,
                order,
                submitted_status,
                "internal_submit_rejection_superseded",
            )
            self._audit_post_submit_safely(
                f"{audit_kind}_transport_rejection_superseded",
                order,
                client_oid=client_oid,
                symbol=intent.symbol,
                exchange_status=submitted_status.value,
                exchange_oid=order.exchange_oid,
                error_code=command.error_code,
                error_message=command.error_message,
                **audit_extra,
            )
            if order.is_active():
                self._handle_submit_transport_conflict(
                    order,
                    f"transport_rejection_superseded:{client_oid}",
                )
            return True

        self._write_tombstone(order)
        payload = {
            "client_oid": client_oid,
            "symbol": intent.symbol,
            "reason": reject_reason,
        }
        payload.update(audit_extra)
        self._audit_post_submit_safely(
            f"{audit_kind}_failed",
            order,
            **payload,
        )
        return False
    def submit_order(self, intent: OrderIntent) -> OrderSubmitResult:
        client_oid = str(uuid.uuid4())
        original_intent = intent
        order = None
        request = None
        rejection_reason = ""
        rejection_extra = {}
        rejection_intent = intent
        command_id = f"SUBMIT:{client_oid}"
        order_send_permit = False
        order_send_risk_increasing = not intent.reduce_only
        calibration_terminal_reason = ""
        final_calibration_terminal_reason = ""
        prepared_records = None

        # Risk evaluation and exposure reservation are one critical section.
        # Every concurrent submit sees earlier accepted-but-not-yet-ACKed orders.
        try:
            with self.lock:
                rejection_reason = self._get_order_block_reason(
                    intent.strategy_id,
                    intent.symbol,
                    reduce_only=intent.reduce_only,
                )
                if not rejection_reason:
                    intent, rejection_reason = self.adapt_intent_for_trading_mode(intent)
                    rejection_intent = original_intent if rejection_reason else intent

                if not rejection_reason:
                    valid, rejection_reason = self.validator.validate_params(intent)
                    rejection_intent = intent
                    if valid:
                        rejection_reason = ""

                if not rejection_reason:
                    rejection_reason = self._get_submission_safety_reason_locked(intent)

                notional = intent.price * intent.volume if not rejection_reason else 0.0
                if not rejection_reason and intent.reduce_only:
                    ok, risk_reason = self.exposure.check_reduce_only(
                        intent.symbol,
                        intent.side,
                        intent.volume,
                    )
                    if not ok:
                        rejection_reason = risk_reason
                elif not rejection_reason:
                    if not self.account.check_margin(notional):
                        rejection_reason = "insufficient_margin"
                        rejection_extra = {
                            "notional": notional,
                            "available": self.account.available,
                        }
                    else:
                        ok, risk_reason = self.exposure.check_risk(
                            intent.symbol,
                            intent.side,
                            intent.volume,
                            self.max_pos_notional,
                            self.max_account_gross_notional,
                            intent.price,
                            self.max_concurrent_symbols,
                        )
                        if not ok:
                            logger.warning(f"[OMS] Risk rejected: {risk_reason}")
                            rejection_reason = f"exposure_limit:{risk_reason}"

                if not rejection_reason:
                    rejection_reason = (
                        self._get_strategy_budget_rejection_locked(intent)
                    )

                if not rejection_reason:
                    order_send_risk_increasing = not intent.reduce_only
                    permit_epoch, rejection_reason = (
                        self._acquire_outbound_order_send_permit_locked(
                            risk_increasing=order_send_risk_increasing,
                            symbol=intent.symbol,
                        )
                    )
                    order_send_permit = permit_epoch is not None

                if not rejection_reason:
                    request = OrderRequest(
                        symbol=intent.symbol,
                        price=intent.price,
                        volume=intent.volume,
                        side=intent.side.value,
                        order_type=intent.order_type,
                        time_in_force=intent.time_in_force,
                        post_only=intent.is_post_only,
                        reduce_only=intent.reduce_only,
                        self_trade_prevention_mode=(
                            self.exchange_self_trade_prevention_mode
                        ),
                    )
                    (
                        rejection_reason,
                        calibration_terminal_reason,
                    ) = self._reserve_rpi_calibration_sample_locked(
                        intent,
                        request,
                        client_oid,
                    )

                if not rejection_reason:
                    order = Order(client_oid, intent)
                    self.orders[client_oid] = order
                    order.mark_submitting()
                    self.exposure.update_open_orders(self.orders)
                    self.account.calculate()
                    self._submit_settlement_inflight_oids.add(client_oid)
                    prepared_records = self._build_submit_prepared_records(
                        command_id,
                        order,
                        request,
                        "accepted",
                    )

            if rejection_reason:
                if order_send_permit:
                    self._release_outbound_order_send_permit(
                        risk_increasing=order_send_risk_increasing,
                        symbol=intent.symbol,
                    )
                    order_send_permit = False
                if calibration_terminal_reason:
                    self.expire_rpi_calibration_permit(
                        calibration_terminal_reason
                    )
                return self._reject_intent_locally(
                    rejection_intent,
                    client_oid,
                    rejection_reason,
                    **rejection_extra,
                )

            # The durable command intent is committed before the first byte is
            # sent to the venue. Recovery queries by client_oid and never
            # blindly resends an ambiguous command.
            self._record_submit_prepared_batch(prepared_records)
            self.order_monitor.track_prepared_order(order)
        except JournalError as exc:
            self._latch_journal_failure(
                exc,
                "prepare_submit",
                intent.symbol,
            )
            try:
                with self.lock:
                    if (
                        order is not None
                        and order.status == OrderStatus.SUBMITTING
                    ):
                        order.mark_rejected_locally(
                            "durable_journal_unavailable"
                        )
                    self.orders.pop(client_oid, None)
                    self.exposure.update_open_orders(self.orders)
                    self.account.calculate()
            except BaseException as cleanup_exc:
                if order is not None:
                    self._close_gate_after_submit_settlement_failure(
                        order,
                        "prepare_submit_journal_cleanup",
                        cleanup_exc,
                    )
                else:
                    logger.critical(
                        "[OMS] Submit prepare journal cleanup failed before "
                        f"order creation: {type(cleanup_exc).__name__}:"
                        f"{cleanup_exc}"
                    )
            if order is not None:
                self._notify_order_state_safely(
                    order,
                    "prepare_submit_journal_failure",
                )
            if order_send_permit:
                self._release_outbound_order_send_permit(
                    risk_increasing=order_send_risk_increasing,
                    symbol=intent.symbol,
                )
                order_send_permit = False
            try:
                self._fail_closed_on_journal_error(
                    exc,
                    "prepare_submit",
                    intent.symbol,
                )
            except BaseException as fail_closed_exc:
                logger.critical(
                    "[OMS] Submit prepare failure could not complete "
                    f"fail-closed: {type(fail_closed_exc).__name__}:"
                    f"{fail_closed_exc}"
                )
            return OrderSubmitResult(
                accepted=False,
                client_oid=client_oid,
                reason="durable_journal_unavailable",
                state=self.state.value,
            )
        except BaseException as exc:
            cleanup_journal_failure = None
            try:
                cleanup_journal_failure = (
                    self._cleanup_pre_dispatch_submit_exception(
                        order,
                        exc,
                        "prepare_submit_exception",
                        "accepted",
                    )
                )
            except BaseException as cleanup_exc:
                if order is not None:
                    self._close_gate_after_submit_settlement_failure(
                        order,
                        "prepare_submit_exception",
                        cleanup_exc,
                    )
                else:
                    logger.critical(
                        "[OMS] Pre-dispatch submit cleanup failed before "
                        f"order creation: {type(cleanup_exc).__name__}:"
                        f"{cleanup_exc}"
                    )
            finally:
                if order_send_permit:
                    self._release_outbound_order_send_permit(
                        risk_increasing=order_send_risk_increasing,
                        symbol=intent.symbol,
                    )
                    order_send_permit = False
            if cleanup_journal_failure is not None:
                try:
                    self._fail_closed_on_journal_error(
                        cleanup_journal_failure,
                        "prepare_submit_exception_cleanup",
                        intent.symbol,
                    )
                except BaseException as fail_closed_exc:
                    logger.critical(
                        "[OMS] Pre-dispatch submit cleanup could not "
                        "complete fail-closed: "
                        f"{type(fail_closed_exc).__name__}:"
                        f"{fail_closed_exc}"
                    )
            raise

        try:
            (
                command,
                final_calibration_terminal_reason,
            ) = self._dispatch_gateway_order_with_final_fence(
                request,
                client_oid,
                intent,
                permit_epoch=permit_epoch,
                risk_increasing=order_send_risk_increasing,
            )
        except BaseException as exc:
            settlement_journal_failure = None
            try:
                settlement_journal_failure = (
                    self._settle_post_dispatch_submit_exception(
                        order,
                        command_id,
                        exc,
                        "dispatch_submit_exception",
                        "accepted",
                    )
                )
            except BaseException as settlement_exc:
                self._close_gate_after_submit_settlement_failure(
                    order,
                    "dispatch_submit_exception",
                    settlement_exc,
                )
            finally:
                if order_send_permit:
                    self._release_outbound_order_send_permit(
                        risk_increasing=order_send_risk_increasing,
                        symbol=intent.symbol,
                    )
                    order_send_permit = False
            if settlement_journal_failure is not None:
                try:
                    self._fail_closed_on_journal_error(
                        settlement_journal_failure,
                        "dispatch_submit_exception",
                        intent.symbol,
                    )
                except BaseException as fail_closed_exc:
                    logger.critical(
                        "[OMS] Post-dispatch submit failure could not "
                        "complete fail-closed: "
                        f"{type(fail_closed_exc).__name__}:"
                        f"{fail_closed_exc}"
                    )
            raise

        try:
            self._record_command_result(
                command_id,
                "SUBMIT",
                order,
                command.outcome,
                exchange_oid=command.exchange_oid,
                error_code=command.error_code,
                error_message=command.error_message,
            )
        except JournalError as exc:
            self._latch_journal_failure(
                exc,
                "result_submit",
                intent.symbol,
            )
            try:
                with self.lock:
                    if order.status == OrderStatus.SUBMITTING:
                        order.mark_submit_unknown("result_not_durable")
            except BaseException as cleanup_exc:
                self._close_gate_after_submit_settlement_failure(
                    order,
                    "result_submit_journal_cleanup",
                    cleanup_exc,
                )
            self._notify_order_state_safely(
                order,
                "result_submit_journal_failure",
            )
            if order_send_permit:
                self._release_outbound_order_send_permit(
                    risk_increasing=order_send_risk_increasing,
                    symbol=intent.symbol,
                )
                order_send_permit = False
            try:
                self._fail_closed_on_journal_error(
                    exc,
                    "result_submit",
                    intent.symbol,
                )
            except BaseException as fail_closed_exc:
                logger.critical(
                    "[OMS] Submit result failure could not complete "
                    f"fail-closed: {type(fail_closed_exc).__name__}:"
                    f"{fail_closed_exc}"
                )
            try:
                self._on_order_truth_check(
                    "Submit result could not be persisted",
                    suspicious_oid=client_oid,
                )
            except BaseException as truth_exc:
                logger.critical(
                    "[OMS] Submit result failure could not start truth "
                    f"resolution: {type(truth_exc).__name__}:{truth_exc}"
                )
            return OrderSubmitResult(
                accepted=True,
                client_oid=client_oid,
                reason="submit_outcome_unknown",
                state=self.state.value,
            )
        except BaseException as exc:
            settlement_journal_failure = None
            try:
                settlement_journal_failure = (
                    self._settle_post_dispatch_submit_exception(
                        order,
                        command_id,
                        exc,
                        "result_submit_exception",
                        "accepted",
                        exchange_oid=command.exchange_oid,
                    )
                )
            except BaseException as settlement_exc:
                self._close_gate_after_submit_settlement_failure(
                    order,
                    "result_submit_exception",
                    settlement_exc,
                )
            finally:
                if order_send_permit:
                    self._release_outbound_order_send_permit(
                        risk_increasing=order_send_risk_increasing,
                        symbol=intent.symbol,
                    )
                    order_send_permit = False
            if settlement_journal_failure is not None:
                try:
                    self._fail_closed_on_journal_error(
                        settlement_journal_failure,
                        "result_submit_exception",
                        intent.symbol,
                    )
                except BaseException as fail_closed_exc:
                    logger.critical(
                        "[OMS] Submit result exception could not complete "
                        f"fail-closed: {type(fail_closed_exc).__name__}:"
                        f"{fail_closed_exc}"
                    )
            raise

        if command.outcome == CommandOutcome.ACKNOWLEDGED:
            exchange_oid = command.exchange_oid
            transport_conflict = ""
            try:
                with self.lock:
                    status_before_transport = order.status
                    transport_conflict = self._bind_submit_exchange_oid_locked(
                        order,
                        exchange_oid,
                        source="rest_ack",
                    )
                    if order.status in {
                        OrderStatus.SUBMITTING,
                        OrderStatus.SUBMIT_UNKNOWN,
                    }:
                        order.mark_pending_ack(order.exchange_oid)
                    submitted_status = order.status
                    self._record_order_snapshot(
                        order,
                        (
                            "rest_ack"
                            if status_before_transport
                            in {
                                OrderStatus.SUBMITTING,
                                OrderStatus.SUBMIT_UNKNOWN,
                            }
                            else "rest_ack_after_exchange_truth"
                        ),
                    )
            except JournalError as exc:
                self._latch_journal_failure(
                    exc,
                    "snapshot_submit_ack",
                    intent.symbol,
                )
                try:
                    with self.lock:
                        if order.status == OrderStatus.SUBMITTING:
                            order.mark_submit_unknown(
                                "ack_snapshot_not_durable"
                            )
                except BaseException as cleanup_exc:
                    self._close_gate_after_submit_settlement_failure(
                        order,
                        "snapshot_submit_ack_journal_cleanup",
                        cleanup_exc,
                    )
                if order_send_permit:
                    self._release_outbound_order_send_permit(
                        risk_increasing=order_send_risk_increasing,
                        symbol=intent.symbol,
                    )
                    order_send_permit = False
                self._notify_order_state_safely(
                    order,
                    "snapshot_submit_ack_failure",
                )
                try:
                    self._on_order_truth_check(
                        "Submit ACK snapshot could not be persisted",
                        suspicious_oid=client_oid,
                    )
                except BaseException as truth_exc:
                    logger.critical(
                        "[OMS] ACK snapshot failure could not start truth "
                        f"resolution: {type(truth_exc).__name__}:"
                        f"{truth_exc}"
                    )
                try:
                    self._fail_closed_on_journal_error(
                        exc,
                        "snapshot_submit_ack",
                        intent.symbol,
                    )
                except BaseException as fail_closed_exc:
                    logger.critical(
                        "[OMS] ACK snapshot failure could not complete "
                        f"fail-closed: {type(fail_closed_exc).__name__}:"
                        f"{fail_closed_exc}"
                    )
                return OrderSubmitResult(
                    accepted=True,
                    client_oid=client_oid,
                    reason="accepted_but_local_snapshot_failed",
                    state=self.state.value,
                )
            except BaseException as exc:
                settlement_journal_failure = None
                try:
                    settlement_journal_failure = (
                        self._settle_post_dispatch_submit_exception(
                            order,
                            command_id,
                            exc,
                            "snapshot_submit_ack_exception",
                            "rest_ack",
                            exchange_oid=exchange_oid,
                        )
                    )
                except BaseException as settlement_exc:
                    self._close_gate_after_submit_settlement_failure(
                        order,
                        "snapshot_submit_ack_exception",
                        settlement_exc,
                    )
                finally:
                    if order_send_permit:
                        self._release_outbound_order_send_permit(
                            risk_increasing=order_send_risk_increasing,
                            symbol=intent.symbol,
                        )
                        order_send_permit = False
                if settlement_journal_failure is not None:
                    try:
                        self._fail_closed_on_journal_error(
                            settlement_journal_failure,
                            "snapshot_submit_ack_exception",
                            intent.symbol,
                        )
                    except BaseException as fail_closed_exc:
                        logger.critical(
                            "[OMS] Submit ACK settlement exception could "
                            "not complete fail-closed: "
                            f"{type(fail_closed_exc).__name__}:"
                            f"{fail_closed_exc}"
                        )
                raise

            if order_send_permit:
                self._release_outbound_order_send_permit(
                    risk_increasing=order_send_risk_increasing,
                    symbol=intent.symbol,
                )
                order_send_permit = False

            self._notify_order_state_safely(
                order,
                "submit_ack",
            )
            self._publish_order_submitted_safely(
                request,
                order,
                submitted_status,
                "submit_ack",
            )
            self._handle_submit_transport_conflict(order, transport_conflict)
            try:
                self._commit_gateway_submission(client_oid)
            except BaseException as exc:
                logger.error(
                    f"[OMS] Gateway submit commit failed for {client_oid}: {exc}"
                )
                self._handle_submit_transport_conflict(
                    order,
                    f"submit_commit_failed:{client_oid}",
                )
            self._finish_submit_settlement(
                order,
                "submit_ack_committed",
            )
            self._audit_post_submit_safely(
                "order_submitted",
                order,
                client_oid=client_oid,
                exchange_oid=exchange_oid,
                symbol=intent.symbol,
                side=intent.side.value,
                price=intent.price,
                volume=intent.volume,
            )
            return OrderSubmitResult(
                accepted=True,
                client_oid=client_oid,
                state=self.state.value,
            )

        if command.outcome == CommandOutcome.UNKNOWN:
            try:
                with self.lock:
                    if order.status == OrderStatus.SUBMITTING:
                        order.mark_submit_unknown(
                            command.error_message
                            or "submit_outcome_unknown"
                        )
                    transport_still_unknown = (
                        order.status == OrderStatus.SUBMIT_UNKNOWN
                    )
                    submitted_status = order.status
                    self._record_order_snapshot(
                        order,
                        (
                            "send_unknown"
                            if transport_still_unknown
                            else "send_unknown_after_exchange_truth"
                        ),
                        error_code=command.error_code,
                        error_message=command.error_message,
                    )
            except JournalError as exc:
                self._latch_journal_failure(
                    exc,
                    "snapshot_submit_unknown",
                    intent.symbol,
                )
                if order_send_permit:
                    self._release_outbound_order_send_permit(
                        risk_increasing=order_send_risk_increasing,
                        symbol=intent.symbol,
                    )
                    order_send_permit = False
                self._notify_order_state_safely(
                    order,
                    "snapshot_submit_unknown_failure",
                )
                try:
                    self._fail_closed_on_journal_error(
                        exc,
                        "snapshot_submit_unknown",
                        intent.symbol,
                    )
                except BaseException as fail_closed_exc:
                    logger.critical(
                        "[OMS] UNKNOWN snapshot failure could not complete "
                        f"fail-closed: {type(fail_closed_exc).__name__}:"
                        f"{fail_closed_exc}"
                    )
                try:
                    self._on_order_truth_check(
                        "Submit result could not be persisted",
                        suspicious_oid=client_oid,
                    )
                except BaseException as truth_exc:
                    logger.critical(
                        "[OMS] UNKNOWN snapshot failure could not start "
                        f"truth resolution: {type(truth_exc).__name__}:"
                        f"{truth_exc}"
                    )
                return OrderSubmitResult(
                    accepted=True,
                    client_oid=client_oid,
                    reason="submit_outcome_unknown",
                    state=self.state.value,
                )
            except BaseException as exc:
                settlement_journal_failure = None
                try:
                    settlement_journal_failure = (
                        self._settle_post_dispatch_submit_exception(
                            order,
                            command_id,
                            exc,
                            "snapshot_submit_unknown_exception",
                            "send_unknown",
                            exchange_oid=command.exchange_oid,
                        )
                    )
                except BaseException as settlement_exc:
                    self._close_gate_after_submit_settlement_failure(
                        order,
                        "snapshot_submit_unknown_exception",
                        settlement_exc,
                    )
                finally:
                    if order_send_permit:
                        self._release_outbound_order_send_permit(
                            risk_increasing=order_send_risk_increasing,
                            symbol=intent.symbol,
                        )
                        order_send_permit = False
                if settlement_journal_failure is not None:
                    try:
                        self._fail_closed_on_journal_error(
                            settlement_journal_failure,
                            "snapshot_submit_unknown_exception",
                            intent.symbol,
                        )
                    except BaseException as fail_closed_exc:
                        logger.critical(
                            "[OMS] UNKNOWN settlement exception could not "
                            "complete fail-closed: "
                            f"{type(fail_closed_exc).__name__}:"
                            f"{fail_closed_exc}"
                        )
                raise
            if order_send_permit:
                self._release_outbound_order_send_permit(
                    risk_increasing=order_send_risk_increasing,
                    symbol=intent.symbol,
                )
                order_send_permit = False
            self._notify_order_state_safely(
                order,
                "submit_unknown",
            )
            self._publish_order_submitted_safely(
                request,
                order,
                submitted_status,
                "submit_unknown",
            )
            if not transport_still_unknown:
                self._audit_post_submit_safely(
                    "order_submit_transport_unknown_resolved",
                    order,
                    client_oid=client_oid,
                    symbol=intent.symbol,
                    exchange_status=submitted_status.value,
                    exchange_oid=order.exchange_oid,
                    error_code=command.error_code,
                    error_message=command.error_message,
                )
                return OrderSubmitResult(
                    accepted=True,
                    client_oid=client_oid,
                    reason="transport_unknown_resolved_by_exchange_truth",
                    state=self.state.value,
                )
            self._handle_submit_transport_conflict(
                order,
                f"submit_unknown:{client_oid}",
            )
            self._audit_post_submit_safely(
                "order_submit_unknown",
                order,
                client_oid=client_oid,
                symbol=intent.symbol,
                error_code=command.error_code,
                error_message=command.error_message,
            )
            self._on_order_truth_check("Order submit outcome unknown", suspicious_oid=client_oid)
            return OrderSubmitResult(
                accepted=True,
                client_oid=client_oid,
                reason="submit_outcome_unknown",
                state=self.state.value,
            )

        transport_rejection_superseded = False
        try:
            with self.lock:
                reject_reason = command.error_message or command.error_code or "gateway_send_rejected"
                if order.status in {
                    OrderStatus.SUBMITTING,
                    OrderStatus.SUBMIT_UNKNOWN,
                }:
                    order.mark_rejected_locally(reject_reason)
                else:
                    transport_rejection_superseded = True
                submitted_status = order.status
                self._record_order_snapshot(
                    order,
                    (
                        "send_failed"
                        if not transport_rejection_superseded
                        else "send_rejection_after_exchange_truth"
                    ),
                )
                if not transport_rejection_superseded:
                    self.orders.pop(client_oid, None)
                    self.exposure.update_open_orders(self.orders)
                    self.account.calculate()
        except JournalError as exc:
            self._latch_journal_failure(
                exc,
                "snapshot_submit_rejected",
                intent.symbol,
            )
            if order_send_permit:
                self._release_outbound_order_send_permit(
                    risk_increasing=order_send_risk_increasing,
                    symbol=intent.symbol,
                )
                order_send_permit = False
            self._notify_order_state_safely(
                order,
                "snapshot_submit_rejected_failure",
            )
            try:
                self._fail_closed_on_journal_error(
                    exc,
                    "snapshot_submit_rejected",
                    intent.symbol,
                )
            except BaseException as fail_closed_exc:
                logger.critical(
                    "[OMS] Rejection snapshot failure could not complete "
                    f"fail-closed: {type(fail_closed_exc).__name__}:"
                    f"{fail_closed_exc}"
                )
            return OrderSubmitResult(
                accepted=transport_rejection_superseded,
                client_oid=client_oid,
                reason=(
                    "transport_rejection_superseded_but_journal_unavailable"
                    if transport_rejection_superseded
                    else "durable_journal_unavailable"
                ),
                state=self.state.value,
            )
        except BaseException as exc:
            settlement_journal_failure = None
            try:
                settlement_journal_failure = (
                    self._settle_post_dispatch_submit_exception(
                        order,
                        command_id,
                        exc,
                        "snapshot_submit_rejected_exception",
                        "send_rejected",
                        exchange_oid=command.exchange_oid,
                    )
                )
            except BaseException as settlement_exc:
                self._close_gate_after_submit_settlement_failure(
                    order,
                    "snapshot_submit_rejected_exception",
                    settlement_exc,
                )
            finally:
                if order_send_permit:
                    self._release_outbound_order_send_permit(
                        risk_increasing=order_send_risk_increasing,
                        symbol=intent.symbol,
                    )
                    order_send_permit = False
            if settlement_journal_failure is not None:
                try:
                    self._fail_closed_on_journal_error(
                        settlement_journal_failure,
                        "snapshot_submit_rejected_exception",
                        intent.symbol,
                    )
                except BaseException as fail_closed_exc:
                    logger.critical(
                        "[OMS] Rejection settlement exception could not "
                        "complete fail-closed: "
                        f"{type(fail_closed_exc).__name__}:"
                        f"{fail_closed_exc}"
                    )
            raise
        if order_send_permit:
            self._release_outbound_order_send_permit(
                risk_increasing=order_send_risk_increasing,
                symbol=intent.symbol,
            )
            order_send_permit = False
        self._notify_order_state_safely(
            order,
            "submit_rejected",
        )
        if transport_rejection_superseded:
            self._publish_order_submitted_safely(
                request,
                order,
                submitted_status,
                "submit_rejection_superseded",
            )
            self._audit_post_submit_safely(
                "order_submit_transport_rejection_superseded",
                order,
                client_oid=client_oid,
                symbol=intent.symbol,
                exchange_status=submitted_status.value,
                exchange_oid=order.exchange_oid,
                error_code=command.error_code,
                error_message=command.error_message,
            )
            if order.is_active():
                self._handle_submit_transport_conflict(
                    order,
                    f"transport_rejection_superseded:{client_oid}",
                )
            if final_calibration_terminal_reason:
                self.expire_rpi_calibration_permit(
                    final_calibration_terminal_reason
                )
            return OrderSubmitResult(
                accepted=True,
                client_oid=client_oid,
                reason="transport_rejection_superseded_by_exchange_truth",
                state=self.state.value,
            )

        self._write_tombstone(order)
        self._audit_post_submit_safely(
            "order_rejected_locally",
            order,
            client_oid=client_oid,
            symbol=intent.symbol,
            reason=reject_reason,
        )
        if final_calibration_terminal_reason:
            self.expire_rpi_calibration_permit(
                final_calibration_terminal_reason
            )
        return OrderSubmitResult(
            accepted=False,
            client_oid=client_oid,
            reason=reject_reason,
            state=self.state.value,
        )
