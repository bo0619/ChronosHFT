"""Durable command recording and fail-closed journal policy."""

from __future__ import annotations

from event.type import (
    CommandOutcome,
    Event,
    LifecycleState,
    OMSCapabilityMode,
    EVENT_SYSTEM_HEALTH,
)
from infrastructure.logger import logger

from .component import OMSComponent
from .journal import JournalError
from .order import Order


class OMSDurabilityManager(OMSComponent):
    """Own durable command outcomes and journal-failure containment."""

    def _record_command_result(
        self,
        command_id: str,
        command_type: str,
        order: Order,
        outcome: CommandOutcome,
        exchange_oid: str = "",
        error_code: str = "",
        error_message: str = "",
    ):
        return self.audit_logger.record_command_result(
            command_id,
            command_type,
            order,
            outcome.value,
            exchange_oid=exchange_oid,
            error_code=error_code,
            error_message=error_message,
        )

    def record_strategy_evidence(
        self,
        kind: str,
        payload: dict,
        *,
        symbol: str = "",
    ) -> int:
        """Durably commit strategy evidence or close the live send gate."""
        try:
            committed_seq = self.audit_logger.audit(kind, payload)
            if not committed_seq:
                raise JournalError(
                    f"OMS journal did not commit strategy evidence {kind}"
                )
        except Exception as exc:
            self._fail_closed_on_journal_error(
                exc,
                f"strategy_evidence:{kind}",
                symbol,
            )
            raise
        return committed_seq

    def _latch_journal_failure_locked(
        self,
        exc: Exception,
        context: str,
        symbol: str = "",
    ) -> str:
        """Atomically seal every order send before its lease is released."""
        reason = f"durable_journal_unavailable:{context}:{exc}"
        already_latched = "journal_failure" in self._outbound_gate_holds
        if already_latched and self._outbound_all_order_seal_reason:
            reason = self._outbound_all_order_seal_reason
        else:
            self._outbound_all_order_seal_reason = reason
        self._close_outbound_gate_locked(
            reason,
            hold="journal_failure",
        )
        self.state = LifecycleState.HALTED
        if not already_latched:
            self._lifecycle_generation += 1
        self.manual_rearm_required = True
        self.last_halt_reason = reason
        self.last_freeze_reason = ""
        self.capability_mode = OMSCapabilityMode.CANCEL_ONLY
        self.capability_reason = reason
        if symbol:
            target_symbol = symbol.upper()
            records = self._ensure_symbol_guard_records_locked(target_symbol)
            owner = self._symbol_guard_owner(reason)
            if owner not in records:
                epoch = (
                    max(
                        [
                            int(
                                self.symbol_guard_epoch_counters.get(
                                    target_symbol,
                                    0,
                                )
                                or 0
                            ),
                            *(
                                int(record.get("epoch", 0) or 0)
                                for record in records.values()
                            ),
                        ]
                    )
                    + 1
                )
                records[owner] = {"reason": reason, "epoch": epoch}
                self.symbol_guard_epoch_counters[target_symbol] = epoch
                self._refresh_symbol_guard_effective_locked(target_symbol)
        return reason

    def _latch_journal_failure(
        self,
        exc: Exception,
        context: str,
        symbol: str = "",
    ) -> str:
        with self.lock:
            return self._latch_journal_failure_locked(
                exc,
                context,
                symbol,
            )

    def _fail_closed_on_journal_error(
        self,
        exc: Exception,
        context: str,
        symbol: str = "",
    ):
        """Enter cancel-only mode without depending on the failed journal."""
        reason = self._latch_journal_failure(exc, context, symbol)
        logger.critical(f"[OMS] {reason}")

        try:
            self.event_engine.put(Event(EVENT_SYSTEM_HEALTH, f"HALT:{reason}"))
        except Exception as event_exc:
            logger.critical(
                "[OMS] Failed to publish journal-failure HALT event: "
                f"{type(event_exc).__name__}:{event_exc}"
            )
        sends_drained = self._wait_for_outbound_order_sends(
            f"journal_failure:{context}"
        )
        if not sends_drained:
            logger.critical(
                f"[OMS] Journal failure order-send drain did not complete for {context}"
            )
        for target_symbol in self._account_cancel_symbols():
            try:
                self._cancel_all_orders_unchecked(
                    target_symbol,
                    source="journal_failure",
                    audit=False,
                    bypass_message_budget=True,
                )
            except Exception as cancel_exc:
                logger.critical(
                    "[OMS] Failed to cancel orders after journal failure "
                    f"for {target_symbol}: {type(cancel_exc).__name__}:"
                    f"{cancel_exc}"
                )
