"""Restore durable OMS lifecycle and guard state after journal replay."""

from __future__ import annotations

from event.type import LifecycleState, OMSCapabilityMode

from .component import OMSComponent


class OMSRecoveryStateRestorer(OMSComponent):
    """Apply a validated journal rebuild summary to shared OMS state."""

    OWNER_READS = frozenset(
        {
            "_capture_guard_cleanup_snapshot_locked",
            "_has_active_guards",
            "_mode_constraint_key",
            "_outbound_gate_holds",
            "_refresh_selected_mode_constraint",
            "_refresh_symbol_guard_effective_locked",
            "_refresh_venue_guard_effective_locked",
            "_rpi_calibration",
            "_symbol_guard_owner",
            "_sync_capability_mode",
            "_venue_guard_owner",
            "account",
            "rebuild_summary",
        }
    )
    OWNER_WRITES = frozenset(
        {
            "_recovered_guard_cleanup_snapshot",
            "_rpi_calibration_budget_exhausted",
            "_rpi_calibration_cumulative_notional_microu",
            "_rpi_calibration_effective_loss_cap_microu",
            "_rpi_calibration_expired",
            "_rpi_calibration_expiry_reason",
            "_rpi_calibration_last_reserved_exchange_ns",
            "_rpi_calibration_peak_observed_loss_microu",
            "_rpi_calibration_permit_activated",
            "_rpi_calibration_permit_start_notional_microu",
            "_rpi_calibration_permit_start_order_count",
            "_rpi_calibration_reservation_exchange_ns",
            "_rpi_calibration_reservation_ids",
            "_rpi_calibration_reserved_order_count",
            "_rpi_calibration_restart_rearm_blocked",
            "_rpi_calibration_start_equity_microu",
            "_rpi_calibration_start_external_cash_flow_microu",
            "external_cash_flow_ids",
            "external_cash_flow_scan_end_ms",
            "last_freeze_reason",
            "last_halt_reason",
            "manual_rearm_required",
            "mode_constraint_generation",
            "mode_constraint_generations",
            "mode_constraints",
            "recovered_guard_cleanup_pending",
            "state",
            "strategy_guards",
            "strategy_symbol_guards",
            "symbol_guard_epoch_counters",
            "symbol_guard_epochs",
            "symbol_guard_records",
            "symbol_guards",
            "trade_cursors",
            "trade_scan_end_ms",
            "venue_guard_epoch_counters",
            "venue_guard_epochs",
            "venue_guard_records",
            "venue_guards",
        }
    )

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
