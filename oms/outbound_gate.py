"""Outbound command gate, send leases and shutdown drain control."""

from __future__ import annotations

import time

from event.type import LifecycleState, OMSCapabilityMode
from infrastructure.logger import logger

from .component import OMSComponent
from .journal import JournalError


class OMSOutboundGate(OMSComponent):
    """Own outbound send admission, leases and bounded shutdown drains."""

    OWNER_READS = frozenset(
        {
            "_audit",
            "_collect_local_active_orders_locked",
            "_fail_closed_on_journal_error",
            "_outbound_all_order_seal_reason",
            "_outbound_gate_condition",
            "_outbound_gate_holds",
            "_outbound_risk_sends_inflight_by_symbol",
            "_reconcile_thread",
            "_rpi_calibration_enforcement_thread",
            "_rpi_calibration_snapshot_locked",
            "_stopped",
            "_submit_cancel_requested_oids",
            "_submit_settlement_inflight_oids",
            "_sync_capability_mode",
            "audit_logger",
            "capability_mode",
            "lock",
            "orders",
            "outbound_gate_drain_timeout_sec",
            "symbol_guards",
            "venue_guards",
        }
    )
    OWNER_WRITES = frozenset(
        {
            "_lifecycle_generation",
            "_outbound_gate_epoch",
            "_outbound_gate_open",
            "_outbound_gate_reason",
            "_outbound_order_sends_inflight",
            "_outbound_risk_sends_inflight",
            "_shutdown_cancel_verified",
            "_shutdown_reason",
            "_shutdown_requested",
            "last_freeze_reason",
            "state",
        }
    )

    def _outbound_gate_should_open_locked(self) -> bool:
        return bool(
            not getattr(self, "_stopped", False)
            and not self._shutdown_requested
            and self.state == LifecycleState.LIVE
            and not self.venue_guards
            and not self._outbound_gate_holds
            and self.capability_mode
            in {
                OMSCapabilityMode.LIVE,
                OMSCapabilityMode.DEGRADED,
                OMSCapabilityMode.PASSIVE_ONLY,
            }
        )

    def _close_outbound_gate_locked(self, reason: str, hold: str = "") -> None:
        if hold:
            self._outbound_gate_holds.add(str(hold))
        with self._outbound_gate_condition:
            if self._outbound_gate_open:
                self._outbound_gate_epoch += 1
            self._outbound_gate_open = False
            self._outbound_gate_reason = str(reason or "risk_send_gate_closed")
            self._outbound_gate_condition.notify_all()

    def _refresh_outbound_gate_locked(self, reason: str = "") -> None:
        should_open = self._outbound_gate_should_open_locked()
        with self._outbound_gate_condition:
            if should_open != self._outbound_gate_open:
                self._outbound_gate_epoch += 1
            self._outbound_gate_open = should_open
            if should_open:
                self._outbound_gate_reason = ""
            elif reason:
                self._outbound_gate_reason = str(reason)
            self._outbound_gate_condition.notify_all()

    def _acquire_outbound_order_send_permit_locked(
        self,
        *,
        risk_increasing: bool,
        symbol: str = "",
        allow_shutdown_emergency: bool = False,
    ) -> tuple[int | None, str]:
        symbol = str(symbol or "").upper()
        with self._outbound_gate_condition:
            if getattr(self, "_stopped", False):
                reason = self._shutdown_reason or "oms_stopping"
                return None, f"shutdown_requested:{reason}"
            if self._outbound_all_order_seal_reason:
                return (
                    None,
                    "outbound_order_gate_closed:"
                    f"{self._outbound_all_order_seal_reason}",
                )
            if self._shutdown_requested and not allow_shutdown_emergency:
                reason = self._shutdown_reason or "oms_stopping"
                return None, f"shutdown_requested:{reason}"
            if risk_increasing and not self._outbound_gate_open:
                reason = self._outbound_gate_reason or "closed"
                return None, f"outbound_gate_closed:{reason}"
            if risk_increasing and symbol in self.symbol_guards:
                return (
                    None,
                    f"symbol_guarded:{symbol}:{self.symbol_guards[symbol]}",
                )
            self._outbound_order_sends_inflight += 1
            if risk_increasing:
                self._outbound_risk_sends_inflight += 1
                if symbol:
                    self._outbound_risk_sends_inflight_by_symbol[symbol] = (
                        self._outbound_risk_sends_inflight_by_symbol.get(
                            symbol,
                            0,
                        )
                        + 1
                    )
            return self._outbound_gate_epoch, ""

    def _acquire_outbound_risk_send_permit_locked(
        self,
        symbol: str = "",
    ) -> tuple[int | None, str]:
        return self._acquire_outbound_order_send_permit_locked(
            risk_increasing=True,
            symbol=symbol,
        )

    def _release_outbound_order_send_permit(
        self,
        *,
        risk_increasing: bool,
        symbol: str = "",
    ) -> None:
        symbol = str(symbol or "").upper()
        with self._outbound_gate_condition:
            if self._outbound_order_sends_inflight <= 0:
                logger.critical("[OMS] Outbound order-send permit underflow")
                return
            self._outbound_order_sends_inflight -= 1
            if risk_increasing:
                if self._outbound_risk_sends_inflight <= 0:
                    logger.critical("[OMS] Outbound risk-send permit underflow")
                else:
                    self._outbound_risk_sends_inflight -= 1
                if symbol:
                    symbol_inflight = (
                        self._outbound_risk_sends_inflight_by_symbol.get(
                            symbol,
                            0,
                        )
                    )
                    if symbol_inflight <= 0:
                        logger.critical(
                            "[OMS] Outbound symbol risk-send permit underflow "
                            f"for {symbol}"
                        )
                    elif symbol_inflight == 1:
                        self._outbound_risk_sends_inflight_by_symbol.pop(
                            symbol,
                            None,
                        )
                    else:
                        self._outbound_risk_sends_inflight_by_symbol[symbol] = (
                            symbol_inflight - 1
                        )
            self._outbound_gate_condition.notify_all()

    def _release_outbound_risk_send_permit(self, symbol: str = "") -> None:
        self._release_outbound_order_send_permit(
            risk_increasing=True,
            symbol=symbol,
        )

    def _submit_settlement_count_locked(
        self,
        *,
        risk_increasing_only: bool = False,
        symbol: str = "",
    ) -> int:
        symbol = str(symbol or "").upper()
        count = 0
        for client_oid in self._submit_settlement_inflight_oids:
            order = self.orders.get(client_oid)
            if order is None:
                if not risk_increasing_only and not symbol:
                    count += 1
                continue
            if risk_increasing_only and order.intent.reduce_only:
                continue
            if symbol and str(order.intent.symbol or "").upper() != symbol:
                continue
            count += 1
        return count

    def _wait_for_outbound_risk_sends(
        self,
        context: str,
        symbol: str = "",
    ) -> bool:
        symbol = str(symbol or "").upper()
        timeout_sec = self.outbound_gate_drain_timeout_sec
        deadline = time.perf_counter() + timeout_sec
        while True:
            with self._outbound_gate_condition:
                inflight = (
                    self._outbound_risk_sends_inflight_by_symbol.get(
                        symbol,
                        0,
                    )
                    if symbol
                    else self._outbound_risk_sends_inflight
                )
            with self.lock:
                settling = self._submit_settlement_count_locked(
                    risk_increasing_only=True,
                    symbol=symbol,
                )
            if inflight <= 0 and settling <= 0:
                return True
            remaining = deadline - time.perf_counter()
            if remaining <= 0.0:
                break
            with self._outbound_gate_condition:
                self._outbound_gate_condition.wait(
                    timeout=min(remaining, 0.05)
                )

        logger.critical(
            "[OMS] Outbound risk-send drain timed out "
            f"context={context} symbol={symbol or '*'} inflight={inflight} "
            f"settling={settling} "
            f"timeout={timeout_sec:.3f}s"
        )
        try:
            self._audit(
                "outbound_gate_drain_timeout",
                context=context,
                symbol=symbol,
                inflight=inflight,
                settling=settling,
                timeout_sec=timeout_sec,
            )
        except JournalError as exc:
            logger.critical(
                "[OMS] Could not persist outbound gate timeout: "
                f"{type(exc).__name__}:{exc}"
            )
        return False

    def _wait_for_outbound_order_sends(self, context: str) -> bool:
        timeout_sec = self.outbound_gate_drain_timeout_sec
        deadline = time.perf_counter() + timeout_sec
        while True:
            with self._outbound_gate_condition:
                inflight = self._outbound_order_sends_inflight
            with self.lock:
                settling = self._submit_settlement_count_locked()
            if inflight <= 0 and settling <= 0:
                return True
            remaining = deadline - time.perf_counter()
            if remaining <= 0.0:
                break
            with self._outbound_gate_condition:
                self._outbound_gate_condition.wait(
                    timeout=min(remaining, 0.05)
                )

        logger.critical(
            "[OMS] Outbound order-send drain timed out "
            f"context={context} inflight={inflight} settling={settling} "
            f"timeout={timeout_sec:.3f}s"
        )
        try:
            self._audit(
                "outbound_order_gate_drain_timeout",
                context=context,
                inflight=inflight,
                settling=settling,
                timeout_sec=timeout_sec,
            )
        except JournalError as exc:
            logger.critical(
                "[OMS] Could not persist outbound order gate timeout: "
                f"{type(exc).__name__}:{exc}"
            )
        return False

    def close_outbound_gate(self, reason: str, wait: bool = True) -> bool:
        """Seal new risk sends while leaving cancels and reductions available."""
        with self.lock:
            self._close_outbound_gate_locked(reason, hold="operator")
        if not wait:
            return True
        return self._wait_for_outbound_risk_sends(reason or "operator")

    def begin_shutdown(self, reason: str = "operator_shutdown") -> bool:
        """Latch shutdown and wait until every order-send call has returned."""
        reason = str(reason or "operator_shutdown")
        first_request = False
        audit_error = None
        with self.lock:
            if not self._shutdown_requested:
                first_request = True
                self._shutdown_requested = True
                self._shutdown_reason = reason
                self._shutdown_cancel_verified = False
            self._close_outbound_gate_locked(reason, hold="shutdown")
            self._lifecycle_generation += 1
            if self.state != LifecycleState.HALTED:
                self.state = LifecycleState.FROZEN
                self.last_freeze_reason = f"Shutdown: {reason}"
                try:
                    self._sync_capability_mode(f"shutdown:{reason}")
                except Exception as exc:
                    audit_error = exc

        if audit_error is not None:
            self._fail_closed_on_journal_error(
                audit_error,
                "begin_shutdown_capability_transition",
            )
            return False

        if first_request:
            try:
                self._audit("shutdown_started", reason=reason)
            except Exception as exc:
                logger.critical(
                    "[OMS] Could not persist shutdown latch: "
                    f"{type(exc).__name__}:{exc}"
                )
                self._fail_closed_on_journal_error(
                    exc,
                    "begin_shutdown",
                )
                return False
        sends_drained = self._wait_for_outbound_order_sends(f"shutdown:{reason}")
        with self.lock:
            reconcile_thread = self._reconcile_thread
            calibration_enforcement_thread = (
                self._rpi_calibration_enforcement_thread
            )
        reconcile_stopped = True
        if (
            reconcile_thread is not None
            and reconcile_thread.is_alive()
            and not reconcile_thread.is_current()
        ):
            reconcile_thread.join(timeout=self.outbound_gate_drain_timeout_sec)
            reconcile_stopped = not reconcile_thread.is_alive()
            if not reconcile_stopped:
                logger.critical("[OMS] Reconcile worker did not stop before shutdown")
        calibration_enforcement_stopped = True
        if (
            calibration_enforcement_thread is not None
            and calibration_enforcement_thread.is_alive()
            and not calibration_enforcement_thread.is_current()
        ):
            calibration_enforcement_thread.join(
                timeout=self.outbound_gate_drain_timeout_sec
            )
            calibration_enforcement_stopped = (
                not calibration_enforcement_thread.is_alive()
            )
            if not calibration_enforcement_stopped:
                logger.critical(
                    "[OMS] RPI calibration enforcement worker did not stop "
                    "before shutdown"
                )
        return bool(
            sends_drained
            and reconcile_stopped
            and calibration_enforcement_stopped
        )

    def verify_preconnect_shutdown_no_order_path(
        self,
        source: str = "preconnect_shutdown",
    ) -> bool:
        """Durably prove that an unconnected shutdown had no local send path."""
        source = str(source or "preconnect_shutdown")
        failure_reason = ""
        journal_error = None
        with self.lock:
            active_orders = self._collect_local_active_orders_locked()
            with self._outbound_gate_condition:
                gate_open = self._outbound_gate_open
                gate_holds = sorted(self._outbound_gate_holds)
                order_sends = self._outbound_order_sends_inflight
                risk_sends = self._outbound_risk_sends_inflight
                shutdown_requested = self._shutdown_requested
                if not shutdown_requested:
                    failure_reason = "shutdown_not_latched"
                elif gate_open:
                    failure_reason = "outbound_gate_open"
                elif active_orders:
                    failure_reason = "local_active_or_unknown_orders"
                elif order_sends != 0 or risk_sends != 0:
                    failure_reason = "outbound_sends_inflight"

                payload = {
                    "source": source,
                    "shutdown_requested": shutdown_requested,
                    "outbound_gate_open": gate_open,
                    "outbound_gate_holds": gate_holds,
                    "local_active_or_unknown_order_count": len(active_orders),
                    "order_sends_inflight": order_sends,
                    "risk_sends_inflight": risk_sends,
                    "state": self.state.value,
                }
                try:
                    if failure_reason:
                        payload["reason"] = failure_reason
                        committed_seq = self.audit_logger.audit(
                            "preconnect_shutdown_no_order_path_rejected",
                            payload,
                        )
                    else:
                        committed_seq = self.audit_logger.audit(
                            "preconnect_shutdown_no_order_path_verified",
                            payload,
                        )
                    if not committed_seq:
                        raise JournalError(
                            "Pre-connect shutdown proof was not committed"
                        )
                    if not failure_reason:
                        self._shutdown_cancel_verified = True
                        return True
                except Exception as exc:
                    self._shutdown_cancel_verified = False
                    journal_error = exc
            if failure_reason or journal_error is not None:
                self._shutdown_cancel_verified = False
                self._close_outbound_gate_locked(
                    "preconnect_shutdown_no_order_path_unverified",
                    hold="shutdown",
                )

        if journal_error is not None:
            self._fail_closed_on_journal_error(
                journal_error,
                "preconnect_shutdown_no_order_path",
            )
        return False

    def get_outbound_gate_snapshot(self) -> dict:
        with self.lock:
            calibration_snapshot = self._rpi_calibration_snapshot_locked()
            with self._outbound_gate_condition:
                return {
                    "open": self._outbound_gate_open,
                    "epoch": self._outbound_gate_epoch,
                    "reason": self._outbound_gate_reason,
                    "all_order_seal_reason": (
                        self._outbound_all_order_seal_reason
                    ),
                    "holds": sorted(self._outbound_gate_holds),
                    "risk_sends_inflight": self._outbound_risk_sends_inflight,
                    "risk_sends_inflight_by_symbol": dict(
                        self._outbound_risk_sends_inflight_by_symbol
                    ),
                    "order_sends_inflight": self._outbound_order_sends_inflight,
                    "submit_settlements_inflight": len(
                        self._submit_settlement_inflight_oids
                    ),
                    "queued_submit_cancels": len(
                        self._submit_cancel_requested_oids
                    ),
                    "shutdown_requested": self._shutdown_requested,
                    "shutdown_cancel_verified": (
                        self._shutdown_cancel_verified
                    ),
                    "drain_timeout_sec": self.outbound_gate_drain_timeout_sec,
                    "rpi_calibration": calibration_snapshot,
                }
