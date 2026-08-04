"""Transactional full OMS reset after stable exchange truth."""

from __future__ import annotations

import time

from event.type import LifecycleState
from infrastructure.logger import logger

from .component import OMSComponent


class OMSFullResetCoordinator(OMSComponent):
    """Rebuild local account/order state from a stable snapshot."""

    OWNER_READS = frozenset(
        {
            "_account_cancel_symbols",
            "_audit",
            "_backfill_trade_history",
            "_cancel_all_orders_unchecked",
            "_capture_guard_cleanup_snapshot_locked",
            "_capture_stable_exchange_snapshot",
            "_clear_recovered_guards_if_pending",
            "_emit_order_update",
            "_emit_position_update",
            "_ensure_venue_dead_man_switch_armed",
            "_exchange_position_event_time",
            "_normalize_remote_account_balances",
            "_normalize_remote_open_orders",
            "_position_state_event_time",
            "_prime_trade_history_baseline",
            "_record_order_snapshot",
            "_rpi_calibration_expired",
            "_schedule_rpi_calibration_runtime_enforcement",
            "_shutdown_requested",
            "_stopped",
            "_submit_cancel_requested_oids",
            "_submit_settlement_inflight_oids",
            "_sync_capability_mode",
            "_write_tombstone",
            "account",
            "backfill_external_cash_flow_history",
            "clear_transient_guards",
            "command_fence_timeout_sec",
            "config",
            "exchange_id_map",
            "exposure",
            "external_cash_flow_truth_enabled",
            "halt_system",
            "lock",
            "order_monitor",
            "orders",
            "query_open_orders",
            "trade_cursors",
        }
    )
    OWNER_WRITES = frozenset(
        {
            "_account_state_event_time",
            "_exchange_account_event_time",
            "_lifecycle_generation",
            "_rpi_calibration_terminal_empty_snapshots",
            "_rpi_calibration_terminal_generation",
            "_rpi_calibration_terminal_pending_reason",
            "_rpi_calibration_terminal_verified",
            "last_freeze_reason",
            "last_halt_reason",
            "manual_rearm_required",
            "reconcile_retry_scheduled",
            "state",
        }
    )

    def _perform_full_reset(self):
        with self.lock:
            reset_entry_state = self.state
            reset_entry_generation = self._lifecycle_generation
            if self._rpi_calibration_expired:
                self._rpi_calibration_terminal_generation += 1
                self._rpi_calibration_terminal_empty_snapshots = 0
                self._rpi_calibration_terminal_verified = False
                self._rpi_calibration_terminal_pending_reason = (
                    "full_reset_truth_replaced"
                )
            transient_guard_snapshot = self._capture_guard_cleanup_snapshot_locked(
                prefixes=("truth_plane:",)
            )
        logger.info("[OMS] Performing full state reset...")
        self._audit("full_reset_started", symbols=self.config.get("symbols", []))
        try:
            if not self._ensure_venue_dead_man_switch_armed("full_reset"):
                return
            command_fence_started = time.perf_counter()
            discovered_orders = self.query_open_orders()
            if discovered_orders is None:
                raise RuntimeError(
                    "account open-order discovery failed before reset cancel"
                )
            for symbol in self._account_cancel_symbols(discovered_orders):
                if not self._cancel_all_orders_unchecked(
                    symbol,
                    source="full_reset_initial",
                    bypass_message_budget=True,
                ):
                    raise RuntimeError(
                        f"initial mass cancel not admitted for {symbol}"
                    )

            initial_snapshot = self._capture_stable_exchange_snapshot(
                require_no_open_orders=True,
            )
            recovery_symbols = set(self._account_cancel_symbols())
            recovery_symbols.update(
                str(position.get("symbol", "") or "").upper()
                for position in initial_snapshot["positions"]
                if str(position.get("symbol", "") or "").strip()
            )
            with self.lock:
                establish_trade_baseline = bool(
                    self.state == LifecycleState.BOOTSTRAP
                    and not self.orders
                    and not self.trade_cursors
                )
            if establish_trade_baseline and not self._prime_trade_history_baseline(
                initial_snapshot["end_time_ms"],
                symbols=recovery_symbols,
            ):
                raise RuntimeError("trade history baseline failed during bootstrap")
            if self.external_cash_flow_truth_enabled and not self.backfill_external_cash_flow_history(
                end_time_ms=initial_snapshot["end_time_ms"],
                source="bootstrap_income_history",
            ):
                raise RuntimeError("external cash-flow history failed during bootstrap")
            if not self._backfill_trade_history(
                symbols=recovery_symbols,
                end_time_ms=initial_snapshot["end_time_ms"],
            ):
                raise RuntimeError("trade history backfill failed during reset")

            fence_remaining = max(
                0.0,
                self.command_fence_timeout_sec
                - (time.perf_counter() - command_fence_started),
            )
            if fence_remaining > 0:
                self._audit(
                    "command_fence_wait",
                    remaining_sec=fence_remaining,
                )
                time.sleep(fence_remaining)

            # A pre-freeze submit may arrive after the first mass cancel but
            # cannot arrive after its signed recvWindow expires. Cancel again
            # beyond that fence, then establish the committed snapshot.
            for symbol in self._account_cancel_symbols():
                if not self._cancel_all_orders_unchecked(
                    symbol,
                    source="full_reset_fenced",
                    bypass_message_budget=True,
                ):
                    raise RuntimeError(
                        f"fenced mass cancel not admitted for {symbol}"
                    )

            snapshot = self._capture_stable_exchange_snapshot(
                require_no_open_orders=True,
            )
            if self.external_cash_flow_truth_enabled and not self.backfill_external_cash_flow_history(
                end_time_ms=snapshot["end_time_ms"],
                source="reset_income_history",
            ):
                raise RuntimeError("external cash-flow history failed during reset")
            recovery_symbols.update(
                str(position.get("symbol", "") or "").upper()
                for position in snapshot["positions"]
                if str(position.get("symbol", "") or "").strip()
            )
            if not self._backfill_trade_history(
                symbols=recovery_symbols,
                end_time_ms=snapshot["end_time_ms"],
            ):
                raise RuntimeError("final trade history backfill failed during reset")

            remote_orders = snapshot["open_orders"]
            account = snapshot["account"]
            positions = snapshot["positions"]
            configured_symbols = {
                str(symbol or "").upper()
                for symbol in self.config.get("symbols", [])
                if str(symbol or "").strip()
            }
            off_config_positions = {
                str(pos.get("symbol", "") or "").upper(): float(
                    pos.get("positionAmt", 0.0) or 0.0
                )
                for pos in positions
                if str(pos.get("symbol", "") or "").strip()
                and str(pos.get("symbol", "") or "").upper()
                not in configured_symbols
                and abs(float(pos.get("positionAmt", 0.0) or 0.0)) > 1e-9
            }
            account_snapshot_floor = snapshot["account_floor"]
            positions_snapshot_floor = snapshot["positions_floor"]
            account_balances = self._normalize_remote_account_balances(account)

            residual_orders = self._normalize_remote_open_orders(remote_orders)
            if residual_orders:
                raise RuntimeError(
                    f"remote open orders still present after cancel-all: {residual_orders}"
                )

            with self.lock:
                previously_tracked_symbols = set(self.exposure.net_positions.keys())
                reset_terminal_orders = []
                reset_time = time.time()
                for order in self.orders.values():
                    if not order.is_active():
                        continue
                    order.mark_cancelled(
                        update_time=reset_time,
                        exchange_status="CANCELED",
                    )
                    self._record_order_snapshot(
                        order,
                        "full_reset_terminalized",
                    )
                    self._write_tombstone(order)
                    reset_terminal_orders.append(order)
                self.orders.clear()
                self._submit_settlement_inflight_oids.clear()
                self._submit_cancel_requested_oids.clear()
                self.exchange_id_map.clear()
                self.exposure.net_positions.clear()
                self.exposure.avg_prices.clear()
                self.exposure.open_buy_qty.clear()
                self.exposure.open_sell_qty.clear()
                self.exposure.update_open_orders(self.orders)

                for pos in positions:
                    amount = float(pos["positionAmt"])
                    if amount == 0:
                        continue
                    symbol = pos["symbol"]
                    self.exposure.force_sync(symbol, amount, float(pos["entryPrice"]))

                snapshot_symbols = previously_tracked_symbols | configured_symbols
                snapshot_symbols.update(
                    str(pos.get("symbol", "") or "").upper()
                    for pos in positions
                    if pos.get("symbol")
                )
                for symbol in snapshot_symbols:
                    self.exposure.reconcile_strategy_position(
                        symbol,
                        self.exposure.net_positions[symbol],
                        self.exposure.avg_prices[symbol],
                    )
                    self._position_state_event_time[symbol] = max(
                        float(self._position_state_event_time.get(symbol, 0.0) or 0.0),
                        positions_snapshot_floor,
                    )
                    self._exchange_position_event_time[symbol] = max(
                        float(self._exchange_position_event_time.get(symbol, 0.0) or 0.0),
                        positions_snapshot_floor,
                    )

                available_balance = account.get("availableBalance")
                self.account.force_sync(
                    float(account["totalWalletBalance"]),
                    float(account["totalInitialMargin"]),
                    float(available_balance) if available_balance is not None else None,
                    balances=account_balances,
                    maintenance_margin=account.get("totalMaintMargin"),
                    margin_balance=account.get("totalMarginBalance"),
                    margin_snapshot_time=time.time(),
                    margin_snapshot_monotonic=time.perf_counter(),
                )
                self._account_state_event_time = max(
                    float(self._account_state_event_time or 0.0),
                    account_snapshot_floor,
                )
                self._exchange_account_event_time = max(
                    float(self._exchange_account_event_time or 0.0),
                    account_snapshot_floor,
                )
                self.order_monitor.monitored_orders.clear()

            for order in reset_terminal_orders:
                self.order_monitor.on_order_update(order.client_oid, order.status)
                self._emit_order_update(order)

            for symbol in snapshot_symbols:
                self._emit_position_update(symbol)

            if off_config_positions:
                self._audit(
                    "full_reset_manual_halt",
                    case="off_config_nonzero_positions",
                    remote_positions=off_config_positions,
                )
                symbols = ",".join(sorted(off_config_positions))
                self.halt_system(
                    "Off-config nonzero positions require manual handling: "
                    f"{symbols}"
                )
                return

            with self.lock:
                if self._shutdown_requested or self._stopped:
                    if self.state != LifecycleState.HALTED:
                        self.state = LifecycleState.FROZEN
                        self._lifecycle_generation += 1
                    self._sync_capability_mode("shutdown_requested")
                    self._audit(
                        "full_reset_resume_suppressed",
                        reason="shutdown_requested",
                    )
                    return
                if (
                    reset_entry_state
                    not in {LifecycleState.BOOTSTRAP, LifecycleState.RECONCILING}
                    or self.state != reset_entry_state
                    or self._lifecycle_generation != reset_entry_generation
                ):
                    self._audit(
                        "full_reset_resume_suppressed",
                        reason="lifecycle_superseded",
                        entry_state=reset_entry_state.value,
                        current_state=self.state.value,
                        expected_generation=reset_entry_generation,
                        current_generation=self._lifecycle_generation,
                    )
                    return
                self.state = LifecycleState.LIVE
                self._lifecycle_generation += 1
                self._sync_capability_mode("full_reset_completed")
                self.manual_rearm_required = False
                self.last_freeze_reason = ""
                self.last_halt_reason = ""
                self.reconcile_retry_scheduled = False
                self.clear_transient_guards(
                    prefixes=("truth_plane:",),
                    guard_snapshot=transient_guard_snapshot,
                )
                self._clear_recovered_guards_if_pending("full_reset_completed")
                self._audit(
                    "full_reset_completed",
                    state=self.state.value,
                    balance=self.account.balance,
                    equity=self.account.equity,
                    positions=dict(self.exposure.net_positions),
                )
            logger.info("OMS: Reset complete. System is CLEAN and LIVE.")
            self._schedule_rpi_calibration_runtime_enforcement()

        except Exception as exc:
            self.halt_system(f"Reset failed: {exc}")
