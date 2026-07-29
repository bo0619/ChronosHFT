"""OMS startup, outbound-gate and shutdown lifecycle control."""

from __future__ import annotations

import time

from event.type import (
    EVENT_SYSTEM_HEALTH,
    Event,
    LifecycleState,
    OMSCapabilityMode,
)
from infrastructure.logger import logger

from .component import OMSComponent
from .journal import JournalError


class OMSLifecycleController(OMSComponent):
    """Own lifecycle transitions and the outbound command safety gate."""

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

    def _apply_rebuild_summary(self):
        summary = self.rebuild_summary or {}
        calibration_summary = summary.get("rpi_calibration", {}) or {}
        self._rpi_calibration_permit_activated = bool(
            calibration_summary.get("permit_activated", False)
        )
        self._rpi_calibration_expired = bool(
            calibration_summary.get("expired", False)
        )
        self._rpi_calibration_expiry_reason = str(
            calibration_summary.get("expiry_reason", "") or ""
        )
        self._rpi_calibration_budget_exhausted = bool(
            calibration_summary.get("budget_exhausted", False)
        )
        self._rpi_calibration_restart_rearm_blocked = bool(
            calibration_summary.get("restart_rearm_blocked", False)
        )
        self._rpi_calibration_reserved_order_count = max(
            0,
            int(calibration_summary.get("reserved_order_count", 0) or 0),
        )
        self._rpi_calibration_cumulative_notional_microu = max(
            0,
            int(
                calibration_summary.get(
                    "cumulative_submitted_notional_microu",
                    0,
                )
                or 0
            ),
        )
        self._rpi_calibration_last_reserved_exchange_ns = max(
            0,
            int(
                calibration_summary.get(
                    "last_reserved_exchange_ns",
                    0,
                )
                or 0
            ),
        )
        self._rpi_calibration_permit_start_order_count = max(
            0,
            int(
                calibration_summary.get(
                    "permit_start_order_count",
                    self._rpi_calibration_reserved_order_count,
                )
                or 0
            ),
        )
        self._rpi_calibration_permit_start_notional_microu = max(
            0,
            int(
                calibration_summary.get(
                    "permit_start_notional_microu",
                    self._rpi_calibration_cumulative_notional_microu,
                )
                or 0
            ),
        )
        self._rpi_calibration_start_equity_microu = max(
            0,
            int(
                calibration_summary.get(
                    "deployment_start_equity_microu",
                    0,
                )
                or 0
            ),
        )
        self._rpi_calibration_start_external_cash_flow_microu = int(
            calibration_summary.get(
                "deployment_start_external_cash_flow_microu",
                0,
            )
            or 0
        )
        self._rpi_calibration_peak_observed_loss_microu = max(
            0,
            int(
                calibration_summary.get(
                    "peak_observed_loss_microu",
                    0,
                )
                or 0
            ),
        )
        replayed_loss_cap_microu = int(
            calibration_summary.get(
                "effective_loss_cap_microu",
                0,
            )
            or 0
        )
        configured_loss_cap_microu = int(
            self._rpi_calibration.get(
                "max_calibration_loss_microu",
                0,
            )
            or 0
        )
        self._rpi_calibration_effective_loss_cap_microu = (
            min(
                replayed_loss_cap_microu,
                configured_loss_cap_microu,
            )
            if replayed_loss_cap_microu > 0
            and configured_loss_cap_microu > 0
            else max(
                replayed_loss_cap_microu,
                configured_loss_cap_microu,
            )
        )
        self._rpi_calibration_reservation_ids = set(
            str(item)
            for item in calibration_summary.get("reservation_ids", [])
            if str(item or "")
        )
        self._rpi_calibration_reservation_exchange_ns = {
            str(client_oid): max(0, int(reserved_at_ns or 0))
            for client_oid, reserved_at_ns in (
                calibration_summary.get("reservation_exchange_ns", {}) or {}
            ).items()
            if str(client_oid or "") and int(reserved_at_ns or 0) > 0
        }
        if self._rpi_calibration_expired:
            self._outbound_gate_holds.add("rpi_calibration_expired")
        if self._rpi_calibration_restart_rearm_blocked:
            self._outbound_gate_holds.add(
                "rpi_calibration_restart_rearm_blocked"
            )
        self.trade_cursors = {
            str(symbol).upper(): int(trade_id)
            for symbol, trade_id in summary.get("trade_cursors", {}).items()
        }
        self.trade_scan_end_ms = {
            str(symbol).upper(): int(end_time_ms)
            for symbol, end_time_ms in summary.get("trade_scan_end_ms", {}).items()
        }
        self.external_cash_flow_ids = set(
            str(income_id)
            for income_id in summary.get("external_cash_flow_ids", [])
            if str(income_id or "")
        )
        self.external_cash_flow_scan_end_ms = int(
            summary.get("external_cash_flow_scan_end_ms", 0) or 0
        )
        self.account.external_cash_flow_total = float(
            summary.get("external_cash_flow_total", 0.0) or 0.0
        )
        self.account.cash_flow_snapshot_synced = False
        self.account.cash_flow_snapshot_time = 0.0
        self.account.cash_flow_snapshot_monotonic = 0.0
        legacy_symbol_guards = {
            str(symbol or "").upper(): str(reason or "")
            for symbol, reason in (summary.get("symbol_guards", {}) or {}).items()
            if str(symbol or "").strip() and str(reason or "")
        }
        self.symbol_guards = {}
        self.symbol_guard_records = {}
        self.symbol_guard_epochs = {}
        self.symbol_guard_epoch_counters = {}
        raw_guard_records = summary.get("symbol_guard_records", {}) or {}
        for raw_symbol, raw_records in raw_guard_records.items():
            symbol = str(raw_symbol or "").upper()
            if not symbol or not isinstance(raw_records, dict):
                continue
            records = {}
            for raw_owner, raw_record in raw_records.items():
                if not isinstance(raw_record, dict):
                    continue
                guard_reason = str(raw_record.get("reason", "") or "")
                if not guard_reason:
                    continue
                guard_epoch = max(1, int(raw_record.get("epoch", 1) or 1))
                records[str(raw_owner or self._symbol_guard_owner(guard_reason))] = {
                    "reason": guard_reason,
                    "epoch": guard_epoch,
                }
            if records:
                self.symbol_guard_records[symbol] = records
                self.symbol_guard_epoch_counters[symbol] = max(
                    int(record["epoch"]) for record in records.values()
                )
                self._refresh_symbol_guard_effective_locked(symbol)
        for symbol, guard_reason in legacy_symbol_guards.items():
            if symbol in self.symbol_guard_records:
                continue
            self.symbol_guard_records[symbol] = {
                self._symbol_guard_owner(guard_reason): {
                    "reason": guard_reason,
                    "epoch": 1,
                }
            }
            self.symbol_guard_epoch_counters[symbol] = 1
            self._refresh_symbol_guard_effective_locked(symbol)
        legacy_venue_guards = {
            str(venue or "").upper(): str(reason or "")
            for venue, reason in (summary.get("venue_guards", {}) or {}).items()
            if str(venue or "").strip() and str(reason or "")
        }
        self.venue_guards = {}
        self.venue_guard_records = {}
        self.venue_guard_epochs = {}
        self.venue_guard_epoch_counters = {}
        raw_venue_guard_records = summary.get("venue_guard_records", {}) or {}
        for raw_venue, raw_records in raw_venue_guard_records.items():
            venue = str(raw_venue or "").upper()
            if not venue or not isinstance(raw_records, dict):
                continue
            records = {}
            for raw_owner, raw_record in raw_records.items():
                if not isinstance(raw_record, dict):
                    continue
                guard_reason = str(raw_record.get("reason", "") or "")
                if not guard_reason:
                    continue
                guard_epoch = max(1, int(raw_record.get("epoch", 1) or 1))
                records[str(raw_owner or self._venue_guard_owner(guard_reason))] = {
                    "reason": guard_reason,
                    "epoch": guard_epoch,
                }
            if records:
                self.venue_guard_records[venue] = records
                self.venue_guard_epoch_counters[venue] = max(
                    int(record["epoch"]) for record in records.values()
                )
                self._refresh_venue_guard_effective_locked(venue)
        for venue, guard_reason in legacy_venue_guards.items():
            if venue in self.venue_guard_records:
                continue
            self.venue_guard_records[venue] = {
                self._venue_guard_owner(guard_reason): {
                    "reason": guard_reason,
                    "epoch": 1,
                }
            }
            self.venue_guard_epoch_counters[venue] = 1
            self._refresh_venue_guard_effective_locked(venue)
        self.strategy_guards = dict(summary.get("strategy_guards", {}))
        self.strategy_symbol_guards = {
            tuple(key.split("|", 1)): value
            for key, value in summary.get("strategy_symbol_guards", {}).items()
            if "|" in key
        }
        self._recovered_guard_cleanup_snapshot = (
            self._capture_guard_cleanup_snapshot_locked()
        )
        self.mode_constraints = {}
        self.mode_constraint_generations = {}
        self.mode_constraint_generation = max(
            0,
            int(summary.get("mode_constraint_generation", 0) or 0),
        )
        for constraint_key, payload in (summary.get("mode_constraints", {}) or {}).items():
            mode_value = str((payload or {}).get("mode", "") or "")
            reason = str((payload or {}).get("reason", "") or "")
            if not mode_value or not reason:
                continue
            try:
                mode = OMSCapabilityMode(mode_value)
            except ValueError:
                continue
            constraint_key = str(constraint_key)
            generation = max(
                1,
                int(
                    (payload or {}).get(
                        "generation",
                        self.mode_constraint_generation + 1,
                    )
                    or 1
                ),
            )
            self.mode_constraints[constraint_key] = (mode, reason)
            self.mode_constraint_generations[constraint_key] = generation
            self.mode_constraint_generation = max(
                self.mode_constraint_generation,
                generation,
            )

        override_mode = str(summary.get("mode_override", "") or "")
        override_reason = str(summary.get("mode_override_reason", "") or "")
        if not self.mode_constraints and override_mode and override_reason:
            try:
                legacy_mode = OMSCapabilityMode(override_mode)
            except ValueError:
                legacy_mode = None
            if legacy_mode is not None:
                constraint_key = self._mode_constraint_key(override_reason)
                self.mode_constraint_generation += 1
                self.mode_constraints[constraint_key] = (
                    legacy_mode,
                    override_reason,
                )
                self.mode_constraint_generations[constraint_key] = (
                    self.mode_constraint_generation
                )
        self._refresh_selected_mode_constraint()

        self.last_freeze_reason = str(summary.get("last_freeze_reason", "") or "")
        self.last_halt_reason = str(summary.get("last_halt_reason", "") or "")
        self.manual_rearm_required = bool(summary.get("manual_rearm_required", False))
        unsafe_trade_symbols = sorted(
            {
                str(symbol or "").upper()
                for symbol in (
                    list(summary.get("untrusted_trade_cursor_symbols", []) or [])
                    + list(summary.get("unverified_execution_symbols", []) or [])
                )
                if str(symbol or "").strip()
            }
        )
        if unsafe_trade_symbols:
            for symbol in unsafe_trade_symbols:
                self.trade_cursors.pop(symbol, None)
                self.trade_scan_end_ms.pop(symbol, None)
            self.manual_rearm_required = True
            self.last_halt_reason = (
                "Legacy execution truth requires operator rearm: "
                + ",".join(unsafe_trade_symbols)
            )

        last_lifecycle = summary.get("last_lifecycle")
        dirty_shutdown = bool(summary.get("dirty_shutdown", False))
        recovered_active_orders = int(summary.get("recovered_active_orders", 0) or 0)
        pending_commands = int(summary.get("pending_commands", 0) or 0)
        if self.manual_rearm_required or last_lifecycle == LifecycleState.HALTED.value:
            self.state = LifecycleState.HALTED
            self.manual_rearm_required = True
            if not self.last_halt_reason:
                self.last_halt_reason = "Recovered halted state"
            self._sync_capability_mode("recovered_halted_state")
            return

        if recovered_active_orders or pending_commands:
            self.state = LifecycleState.FROZEN
            self.recovered_guard_cleanup_pending = True
            self.last_freeze_reason = (
                "Recovered orders require exchange truth: "
                f"active={recovered_active_orders}, pending_commands={pending_commands}"
            )
            self._sync_capability_mode("recovered_inflight_commands")
            return

        if dirty_shutdown:
            self.state = LifecycleState.FROZEN
            self.recovered_guard_cleanup_pending = self._has_active_guards()
            if not self.last_freeze_reason:
                self.last_freeze_reason = "Recovered unclean shutdown"
            self._sync_capability_mode("recovered_unclean_shutdown")
            return

        if self._has_active_guards() or last_lifecycle in {
            LifecycleState.FROZEN.value,
            LifecycleState.RECONCILING.value,
        }:
            self.state = LifecycleState.FROZEN
            self.recovered_guard_cleanup_pending = True
            if not self.last_freeze_reason:
                self.last_freeze_reason = "Recovered guarded state"
            self._sync_capability_mode("recovered_guarded_state")
            return

        self.state = LifecycleState.BOOTSTRAP
        self._sync_capability_mode("bootstrap")

    def _has_active_guards(self):
        return bool(
            self.symbol_guards
            or self.venue_guards
            or self.strategy_guards
            or self.strategy_symbol_guards
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

    def stop(self, clean_shutdown: bool = False, reason: str = ""):
        with self.lock:
            self._stopped = True
            self._outbound_all_order_seal_reason = reason or "oms_stop"
            self._close_outbound_gate_locked("oms_stop", hold="stopped")
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
            and drained
            and background_tasks_stopped
            and self._shutdown_requested
            and self._shutdown_cancel_verified
        )
        audit_ok = True
        paper_database = getattr(self, "paper_trade_database", None)
        paper_run_id = (
            str(getattr(paper_database, "run_id", "") or "")
            if paper_database is not None
            else ""
        )
        paper_audit_fields = (
            {"paper_run_id": paper_run_id} if paper_run_id else {}
        )
        try:
            if clean_shutdown:
                self._audit(
                    "oms_stopped",
                    state=self.state.value,
                    reason=reason or self._shutdown_reason,
                    cancel_verified=True,
                    manual_rearm_required=self.manual_rearm_required,
                    symbol_guard_count=len(self.symbol_guards),
                    venue_guard_count=len(self.venue_guards),
                    strategy_guard_count=len(self.strategy_guards),
                    strategy_symbol_guard_count=len(self.strategy_symbol_guards),
                    **paper_audit_fields,
                )
            else:
                self._audit(
                    "shutdown_cancel_unverified",
                    state=self.state.value,
                    reason=reason or self._shutdown_reason or "oms_stop_without_verification",
                    drain_completed=drained,
                    cancel_verified=self._shutdown_cancel_verified,
                    **paper_audit_fields,
                )
        except Exception as exc:
            audit_ok = False
            logger.critical(
                "[OMS] Shutdown audit could not be persisted: "
                f"{type(exc).__name__}:{exc}"
            )

        with self.lock:
            self._refresh_outbound_gate_locked("oms_stopped")

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
                        clean_shutdown=bool(clean_shutdown and audit_ok),
                        reason=reason or self._shutdown_reason or "oms_stop",
                    )
                )
            except Exception as exc:
                paper_database_stopped = False
                logger.critical(
                    "[OMS] Paper trade database did not stop cleanly: "
                    f"{type(exc).__name__}:{exc}"
                )

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
            "clean": bool(clean_shutdown and audit_ok and stopped),
        }
