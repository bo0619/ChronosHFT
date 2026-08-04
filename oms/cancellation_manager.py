"""Single-order and account-wide cancellation workflows."""

from __future__ import annotations

import time
import uuid

from event.type import CancelRequest, CommandOutcome, OrderStatus
from infrastructure.logger import logger

from .component import OMSComponent
from .journal import JournalError


class OMSCancellationManager(OMSComponent):
    """Own cancel dispatch, retries and verified account-wide cancellation."""

    OWNER_READS = frozenset(
        {
            "OUTBOUND_CANCEL",
            "_apply_exchange_order_snapshot",
            "_audit",
            "_deferred_cancel_all_symbols",
            "_deferred_cancel_oids",
            "_emit_order_update",
            "_fail_closed_on_journal_error",
            "_get_capability_block_reason",
            "_known_account_order_symbols",
            "_latch_journal_failure",
            "_normalize_remote_open_orders",
            "_notify_order_state_safely",
            "_on_order_truth_check",
            "_outbound_budget",
            "_record_command_prepared",
            "_record_command_result",
            "_record_order_snapshot",
            "_shutdown_cancel_verified",
            "_shutdown_requested",
            "_stopped",
            "_submit_background_task",
            "_submit_cancel_requested_oids",
            "_submit_settlement_inflight_oids",
            "_wait_for_outbound_order_sends",
            "can_cancel_orders",
            "config",
            "freeze_symbol",
            "gateway",
            "get_outbound_gate_snapshot",
            "lock",
            "order_monitor",
            "orders",
            "outbound_message_window_sec",
            "shutdown_cancel_settle_interval_sec",
            "shutdown_cancel_timeout_sec",
            "shutdown_empty_snapshots_required",
            "symbol_guards",
            "trigger_reconcile",
        }
    )
    OWNER_WRITES = frozenset({"_shutdown_cancel_verified"})

    def _schedule_cancel_order_retry(self, client_oid: str) -> bool:
        with self.lock:
            if self._stopped or client_oid in self._deferred_cancel_oids:
                return False
            self._deferred_cancel_oids.add(client_oid)

        def retry():
            with self.lock:
                self._deferred_cancel_oids.discard(client_oid)
                stopped = self._stopped
            if not stopped:
                self.cancel_order(client_oid)

        handle = self._submit_background_task(
            f"cancel-retry:{client_oid}",
            retry,
            name=f"CancelRetry-{client_oid}",
            safety=True,
            delay_sec=self.outbound_message_window_sec + 0.01,
        )
        if handle is None:
            with self.lock:
                self._deferred_cancel_oids.discard(client_oid)
            return False
        return True

    def _schedule_cancel_all_retry(
        self,
        symbol: str,
        source: str,
        *,
        audit: bool = True,
        bypass_message_budget: bool = False,
    ) -> bool:
        symbol = str(symbol or "").upper()
        with self.lock:
            if self._stopped or symbol in self._deferred_cancel_all_symbols:
                return False
            self._deferred_cancel_all_symbols.add(symbol)

        def retry():
            with self.lock:
                self._deferred_cancel_all_symbols.discard(symbol)
                stopped = self._stopped
            if stopped:
                return
            retry_source = (
                source
                if source.startswith("deferred:")
                else f"deferred:{source}"
            )
            self._cancel_all_orders_unchecked(
                symbol,
                source=retry_source,
                audit=audit,
                bypass_message_budget=bypass_message_budget,
            )

        handle = self._submit_background_task(
            f"cancel-all-retry:{symbol}",
            retry,
            name=f"CancelAllRetry-{symbol}",
            safety=True,
            delay_sec=self.outbound_message_window_sec + 0.01,
        )
        if handle is None:
            with self.lock:
                self._deferred_cancel_all_symbols.discard(symbol)
            return False
        return True

    def cancel_order(self, client_oid: str):
        if not self.can_cancel_orders():
            self._audit(
                "cancel_rejected",
                client_oid=client_oid,
                reason=self._get_capability_block_reason("cancel"),
            )
            return False

        command_id = f"CANCEL:{client_oid}:{uuid.uuid4().hex}"
        message_reservation = None
        try:
            with self.lock:
                order = self.orders.get(client_oid)
                if not order or not order.is_active():
                    return False
                if (
                    client_oid in self._submit_settlement_inflight_oids
                    and (
                        order.status != OrderStatus.PENDING_ACK
                        or not order.exchange_oid
                    )
                ):
                    if client_oid in self._submit_cancel_requested_oids:
                        return True
                    self._submit_cancel_requested_oids.add(client_oid)
                    self._audit(
                        "cancel_queued_until_submit_settled",
                        client_oid=client_oid,
                        symbol=order.intent.symbol,
                    )
                    return True
                if client_oid in self._submit_cancel_requested_oids:
                    return True
                if order.status == OrderStatus.CANCELLING:
                    return True
                (
                    message_reservation,
                    budget_rejection,
                ) = self._outbound_budget.reserve_token(
                    self.OUTBOUND_CANCEL,
                )
                if budget_rejection:
                    scheduled = self._schedule_cancel_order_retry(client_oid)
                    deferred = scheduled or client_oid in self._deferred_cancel_oids
                    self._audit(
                        "cancel_message_budget_deferred",
                        client_oid=client_oid,
                        symbol=order.intent.symbol,
                        reason=budget_rejection,
                        scheduled=scheduled,
                    )
                    return deferred
                target_id = order.exchange_oid if order.exchange_oid else client_oid
                try:
                    order.mark_cancelling()
                except ValueError:
                    self._outbound_budget.rollback(message_reservation)
                    return order.status == OrderStatus.CANCELLING
                self._record_order_snapshot(order, "cancel_requested")
                self._emit_order_update(order)
                request = CancelRequest(order.intent.symbol, target_id)
            self._record_command_prepared(command_id, "CANCEL", order, request)
        except JournalError as exc:
            self._outbound_budget.rollback(message_reservation)
            self._fail_closed_on_journal_error(
                exc,
                "prepare_cancel",
                order.intent.symbol if order else "",
            )
            return False

        try:
            response = self.gateway.cancel_order(request)
        except Exception as exc:
            logger.error(f"[OMS] Cancel command raised for {client_oid}: {exc}")
            response = None
        error_code = ""
        error_message = ""
        if response is not None and getattr(response, "status_code", 0) != 200:
            try:
                payload = response.json()
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                raw_code = payload.get("code")
                error_code = "" if raw_code is None else str(raw_code)
                error_message = str(payload.get("msg", "") or "")

        command_outcome = (
            CommandOutcome.ACKNOWLEDGED
            if response is not None and getattr(response, "status_code", 0) == 200
            else CommandOutcome.UNKNOWN
        )
        try:
            self._record_command_result(
                command_id,
                "CANCEL",
                order,
                command_outcome,
                exchange_oid=order.exchange_oid,
                error_code=error_code,
                error_message=error_message,
            )
        except JournalError as exc:
            self._latch_journal_failure(
                exc,
                "result_cancel",
                request.symbol,
            )
            with self.lock:
                current = self.orders.get(client_oid)
                if current and current.is_active():
                    try:
                        current.mark_cancel_unknown("result_not_durable")
                    except ValueError:
                        pass
            if current is not None:
                self._notify_order_state_safely(
                    current,
                    "cancel_result_journal_failure",
                )
            try:
                self._fail_closed_on_journal_error(
                    exc,
                    "result_cancel",
                    request.symbol,
                )
            except BaseException as fail_closed_exc:
                logger.critical(
                    "[OMS] Cancel result failure could not complete "
                    f"fail-closed: {type(fail_closed_exc).__name__}:"
                    f"{fail_closed_exc}"
                )
            try:
                self._on_order_truth_check(
                    "Cancel result could not be persisted",
                    suspicious_oid=client_oid,
                )
            except BaseException as truth_exc:
                logger.critical(
                    "[OMS] Cancel result failure could not start truth "
                    f"resolution: {type(truth_exc).__name__}:"
                    f"{truth_exc}"
                )
            return True

        if response is not None and getattr(response, "status_code", 0) == 200:
            try:
                payload = response.json()
            except Exception:
                payload = {}
            if isinstance(payload, dict) and payload.get("status"):
                try:
                    snapshot_applied = self._apply_exchange_order_snapshot(
                        payload,
                        source="cancel_rest_ack",
                    )
                except JournalError as exc:
                    self._fail_closed_on_journal_error(
                        exc,
                        "snapshot_cancel_ack",
                        request.symbol,
                    )
                    return True
                if not snapshot_applied:
                    self.trigger_reconcile(
                        "Cancel acknowledgement is ahead of exact trade history",
                        suspicious_oid=client_oid,
                    )
            self._audit(
                "cancel_acknowledged",
                client_oid=client_oid,
                target_id=target_id,
                symbol=request.symbol,
            )
            return True

        terminal_status = ""
        try:
            with self.lock:
                order = self.orders.get(client_oid)
                if order and order.is_active():
                    try:
                        order.mark_cancel_unknown(error_message or error_code or "cancel_outcome_unknown")
                    except ValueError:
                        order.note_exchange_update(
                            exchange_status="CANCEL_UNKNOWN",
                            update_time=time.time(),
                        )
                    self._record_order_snapshot(
                        order,
                        "cancel_unknown",
                        exchange_error_code=error_code,
                        exchange_error_message=error_message,
                    )
                    self._emit_order_update(order)
                    self.order_monitor.on_order_update(order.client_oid, order.status)
                elif order and order.is_terminal():
                    terminal_status = order.status.value
        except JournalError as exc:
            self._fail_closed_on_journal_error(exc, "snapshot_cancel_unknown", request.symbol)
            self._on_order_truth_check(
                "Cancel result could not be persisted",
                suspicious_oid=client_oid,
            )
            return True

        if terminal_status:
            self._audit(
                "cancel_response_superseded_by_terminal_update",
                client_oid=client_oid,
                target_id=target_id,
                symbol=request.symbol,
                terminal_status=terminal_status,
                error_code=error_code,
                error_message=error_message,
            )
            return True

        self.freeze_symbol(
            request.symbol,
            f"order_truth:cancel_unknown:{client_oid}",
            cancel_active_orders=False,
        )
        self._audit(
            "cancel_outcome_unknown",
            client_oid=client_oid,
            target_id=target_id,
            symbol=request.symbol,
            error_code=error_code,
            error_message=error_message,
        )
        self._on_order_truth_check("Order cancel outcome unknown", suspicious_oid=client_oid)
        return True

    def _cancel_all_orders_unchecked(
        self,
        symbol: str,
        source: str,
        audit: bool = True,
        bypass_message_budget: bool = False,
    ) -> bool:
        budget_rejection = ""
        message_reservation = None
        if not bypass_message_budget:
            (
                message_reservation,
                budget_rejection,
            ) = self._outbound_budget.reserve_token(
                self.OUTBOUND_CANCEL,
            )
        if budget_rejection:
            logger.error(
                f"[OMS] Mass cancel blocked for {symbol}: {budget_rejection}"
            )
            scheduled = (
                self._schedule_cancel_all_retry(symbol, source)
                if audit and not bypass_message_budget
                else self._schedule_cancel_all_retry(
                    symbol,
                    source,
                    audit=audit,
                    bypass_message_budget=bypass_message_budget,
                )
            )
            with self.lock:
                deferred = (
                    scheduled or symbol.upper() in self._deferred_cancel_all_symbols
                )
            if audit:
                self._audit(
                    "cancel_all_message_budget_deferred",
                    symbol=symbol,
                    source=source,
                    reason=budget_rejection,
                    scheduled=scheduled,
                )
            return deferred

        if audit:
            try:
                self._audit(
                    "cancel_all_submitted",
                    symbol=symbol,
                    source=source,
                )
            except Exception:
                self._outbound_budget.rollback(message_reservation)
                raise
        try:
            response = self.gateway.cancel_all_orders(symbol)
        except Exception as exc:
            logger.error(f"[OMS] Mass cancel failed for {symbol}: {exc}")
            if audit:
                self._audit(
                    "cancel_all_outcome_unknown",
                    symbol=symbol,
                    source=source,
                    error=f"{type(exc).__name__}:{exc}",
                )
                self.freeze_symbol(
                    symbol,
                    f"order_truth:cancel_all_unknown:{source}",
                    cancel_active_orders=False,
                )
            if audit and not bypass_message_budget:
                self._schedule_cancel_all_retry(symbol, source)
            else:
                self._schedule_cancel_all_retry(
                    symbol,
                    source,
                    audit=audit,
                    bypass_message_budget=bypass_message_budget,
                )
            if audit:
                self._on_order_truth_check("Mass cancel outcome unknown")
            return False

        status_code = getattr(response, "status_code", None)
        if response is True or status_code == 200:
            if audit:
                self._audit(
                    "cancel_all_acknowledged",
                    symbol=symbol,
                    source=source,
                )
            return True

        error_code = ""
        error_message = ""
        try:
            payload = response.json() if response is not None else {}
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            error_code = str(payload.get("code", "") or "")
            error_message = str(payload.get("msg", "") or "")
        logger.error(
            f"[OMS] Mass cancel outcome unknown for {symbol}: "
            f"status={status_code} code={error_code} msg={error_message}"
        )
        if audit:
            self._audit(
                "cancel_all_outcome_unknown",
                symbol=symbol,
                source=source,
                status_code=status_code,
                error_code=error_code,
                error_message=error_message,
            )
            self.freeze_symbol(
                symbol,
                f"order_truth:cancel_all_unknown:{source}",
                cancel_active_orders=False,
            )
        if audit and not bypass_message_budget:
            self._schedule_cancel_all_retry(symbol, source)
        else:
            self._schedule_cancel_all_retry(
                symbol,
                source,
                audit=audit,
                bypass_message_budget=bypass_message_budget,
            )
        if audit:
            self._on_order_truth_check("Mass cancel outcome unknown")
        return False

    def _account_cancel_symbols(self, remote_orders=None) -> list[str]:
        normalized_remote = (
            self._normalize_remote_open_orders(remote_orders)
            if remote_orders is not None
            else []
        )
        with self.lock:
            symbols = set(self._known_account_order_symbols)
            symbols.update(
                str(symbol or "").upper()
                for symbol in self.config.get("symbols", [])
                if str(symbol or "").strip()
            )
            symbols.update(
                str(order.intent.symbol or "").upper()
                for order in self.orders.values()
                if order.is_active() and str(order.intent.symbol or "").strip()
            )
            symbols.update(
                str(symbol or "").upper()
                for symbol in self.symbol_guards
                if str(symbol or "").strip()
            )
            symbols.update(
                item["symbol"] for item in normalized_remote if item.get("symbol")
            )
            self._known_account_order_symbols.update(symbols)
        return sorted(symbols)

    def cancel_all_account_orders_verified(
        self,
        snapshot_provider=None,
        *,
        source: str = "shutdown",
        timeout_sec: float | None = None,
        required_empty_snapshots: int | None = None,
        settle_interval_sec: float | None = None,
    ) -> bool:
        """Cancel every discovered symbol and prove two consecutive empty snapshots."""
        provider = snapshot_provider or self.gateway
        query = getattr(provider, "get_open_orders", None)
        if not callable(query):
            self._audit(
                "account_cancel_verification_failed",
                source=source,
                reason="open_orders_query_unavailable",
            )
            return False

        timeout_sec = max(
            1.0,
            float(
                self.shutdown_cancel_timeout_sec
                if timeout_sec is None
                else timeout_sec
            ),
        )
        required_empty_snapshots = max(
            2,
            int(
                self.shutdown_empty_snapshots_required
                if required_empty_snapshots is None
                else required_empty_snapshots
            ),
        )
        settle_interval_sec = max(
            0.01,
            float(
                self.shutdown_cancel_settle_interval_sec
                if settle_interval_sec is None
                else settle_interval_sec
            ),
        )
        deadline = time.perf_counter() + timeout_sec
        empty_snapshots = 0
        cancel_sweep_completed = False
        last_error = ""

        if not self._wait_for_outbound_order_sends(f"{source}:pre_cancel"):
            last_error = "outbound_order_sends_not_drained"
        else:
            while time.perf_counter() < deadline:
                try:
                    remote_orders = (
                        query(emergency=True)
                        if bool(
                            getattr(
                                provider,
                                "supports_emergency_query_priority",
                                False,
                            )
                        )
                        else query()
                    )
                    normalized_remote = self._normalize_remote_open_orders(
                        remote_orders
                    )
                except Exception as exc:
                    remote_orders = None
                    normalized_remote = []
                    last_error = f"open_orders_query:{type(exc).__name__}:{exc}"
                    empty_snapshots = 0

                if remote_orders is not None:
                    cancel_targets = self._account_cancel_symbols(remote_orders)
                    if normalized_remote or not cancel_sweep_completed:
                        for symbol in cancel_targets:
                            acknowledged = self._cancel_all_orders_unchecked(
                                symbol,
                                source=f"{source}:account_sweep",
                                bypass_message_budget=True,
                            )
                            if not acknowledged:
                                last_error = f"cancel_unverified:{symbol}"
                        cancel_sweep_completed = True
                        empty_snapshots = 0
                    elif self.get_outbound_gate_snapshot()[
                        "order_sends_inflight"
                    ] == 0:
                        empty_snapshots += 1
                        if empty_snapshots >= required_empty_snapshots:
                            with self.lock:
                                if self._shutdown_requested:
                                    self._shutdown_cancel_verified = True
                            self._audit(
                                "account_cancel_verified",
                                source=source,
                                empty_snapshots=empty_snapshots,
                                symbols=cancel_targets,
                            )
                            return True
                    else:
                        last_error = "outbound_order_send_reappeared"
                        empty_snapshots = 0

                time.sleep(settle_interval_sec)

        self._audit(
            "account_cancel_verification_failed",
            source=source,
            reason=last_error or "verification_timeout",
            empty_snapshots=empty_snapshots,
            timeout_sec=timeout_sec,
        )
        return False

    def cancel_all_orders(self, symbol: str):
        if not self.can_cancel_orders():
            self._audit(
                "cancel_all_rejected",
                symbol=symbol,
                reason=self._get_capability_block_reason("cancel"),
            )
            return False
        return self._cancel_all_orders_unchecked(
            symbol,
            source="public_cancel_all",
        )
