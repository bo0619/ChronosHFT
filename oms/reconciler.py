"""Exchange reconciliation and full-reset orchestration."""

from __future__ import annotations

import threading
import time

from infrastructure.logger import logger
from infrastructure.time_service import time_service

from event.type import LifecycleState

from .component import OMSComponent


class OMSReconciler(OMSComponent):
    """Own truth reconciliation, retry and full-reset workflows."""

    def _normalize_remote_open_orders(self, remote_orders):
        if not isinstance(remote_orders, (list, tuple)):
            raise ValueError("remote open-orders snapshot must be a list")
        normalized = []
        for order in remote_orders:
            if not isinstance(order, dict):
                raise ValueError("remote open-orders entry must be an object")
            symbol = str(order.get("symbol", "") or "").upper().strip()
            if not symbol:
                raise ValueError("remote open-orders entry is missing symbol")
            identifiers = tuple(
                sorted(
                    oid
                    for oid in [
                        (
                            str(order.get("orderId"))
                            if order.get("orderId") is not None
                            else ""
                        ),
                        order.get("clientOrderId") or "",
                    ]
                    if oid
                )
            )
            normalized.append(
                {
                    "symbol": symbol,
                    "identifiers": identifiers,
                    "side": str(order.get("side", "") or "").upper(),
                }
            )
        with self.lock:
            self._known_account_order_symbols.update(
                item["symbol"] for item in normalized
            )
        normalized.sort(
            key=lambda item: (
                item["symbol"],
                item["identifiers"],
                item["side"],
            )
        )
        return normalized

    def _collect_local_active_orders_locked(self):
        normalized = []
        for order in self.orders.values():
            if not order.is_active():
                continue
            identifiers = tuple(
                sorted(
                    oid
                    for oid in [order.client_oid, order.exchange_oid]
                    if oid
                )
            )
            normalized.append(
                {
                    "symbol": str(order.intent.symbol or "").upper(),
                    "identifiers": identifiers,
                    "side": order.intent.side.value,
                }
            )
        normalized.sort(
            key=lambda item: (
                item["symbol"],
                item["identifiers"],
                item["side"],
            )
        )
        return normalized

    def _collect_exchange_position_drift_locked(
        self,
        exchange_positions,
        tracked_symbols=None,
    ):
        drift = {}
        symbols = set(tracked_symbols or [])
        symbols.update(exchange_positions.keys())
        symbols.update(
            symbol
            for symbol, volume in self.exposure.net_positions.items()
            if abs(volume) > 1e-6 and (not symbols or symbol in symbols)
        )

        for symbol in symbols:
            local_pos = self.exposure.net_positions.get(symbol, 0.0)
            payload = exchange_positions.get(symbol, {})
            exchange_pos = float(payload.get("volume", 0.0))
            if abs(local_pos - exchange_pos) > 1e-6:
                drift[symbol] = {
                    "local": local_pos,
                    "exchange": exchange_pos,
                    "entry_price": float(payload.get("entry_price", 0.0)),
                }
        return drift

    def _has_active_orders_locked(self, symbols=None):
        tracked_symbols = set(symbols or [])
        for order in self.orders.values():
            if not order.is_active():
                continue
            if tracked_symbols and order.intent.symbol not in tracked_symbols:
                continue
            return True
        return False

    def _schedule_pending_reconcile_requests(
        self,
        *,
        resubmit_after_current: bool = False,
    ) -> bool:
        with self.lock:
            if (
                not self._pending_reconcile_requests
                or self._shutdown_requested
                or self._stopped
                or self.state in {
                    LifecycleState.RECONCILING,
                    LifecycleState.HALTED,
                }
            ):
                return False
        handle = self._submit_background_task(
            "reconcile:request",
            self._drain_pending_reconcile_requests,
            name="OMSReconcileRequest",
            resubmit_after_current=resubmit_after_current,
        )
        return handle is not None

    def _queue_reconcile_request_locked(
        self,
        reason: str,
        suspicious_oid: str = "",
    ) -> bool:
        reason = str(reason or "exchange_truth_anomaly")
        suspicious_oid = str(suspicious_oid or "")
        if self._shutdown_requested or self._stopped:
            return False
        if self.state == LifecycleState.HALTED:
            return False

        request = (reason, suspicious_oid)
        if request not in self._pending_reconcile_requests:
            if (
                len(self._pending_reconcile_requests)
                >= self._max_pending_reconcile_requests
            ):
                self._latch_background_task_failure(
                    "reconcile:request",
                    "pending_anomaly_limit",
                )
                return False
            self._pending_reconcile_requests.append(request)

        if self.state != LifecycleState.RECONCILING:
            if self.state != LifecycleState.FROZEN:
                self._lifecycle_generation += 1
            self.state = LifecycleState.FROZEN
            self.last_freeze_reason = reason
            self._sync_capability_mode(reason)
        return self._schedule_pending_reconcile_requests()

    def _drain_pending_reconcile_requests(self) -> None:
        with self.lock:
            if (
                self._shutdown_requested
                or self._stopped
                or self.state in {
                    LifecycleState.RECONCILING,
                    LifecycleState.HALTED,
                }
                or not self._pending_reconcile_requests
            ):
                return
            requests = list(self._pending_reconcile_requests)
            self._pending_reconcile_requests.clear()

        reasons = [item[0] for item in requests]
        suspicious_oids = [item[1] for item in requests if item[1]]
        combined_reason = "; ".join(reasons[:8])
        if len(reasons) > 8:
            combined_reason += f"; +{len(reasons) - 8} more"
        self.trigger_reconcile(
            combined_reason,
            suspicious_oid=(suspicious_oids[0] if suspicious_oids else None),
        )
        with self.lock:
            needs_successor = bool(
                self._pending_reconcile_requests
                and self.state
                not in {
                    LifecycleState.RECONCILING,
                    LifecycleState.HALTED,
                }
                and not self._shutdown_requested
                and not self._stopped
            )
        if needs_successor:
            self._schedule_pending_reconcile_requests(
                resubmit_after_current=True,
            )

    def trigger_reconcile(
        self,
        reason: str,
        suspicious_oid: str = None,
        recovery_venue: str = "",
        recovery_epoch: int | None = None,
        recovery_owner: str = "",
        recovery_reason: str = "",
    ):
        with self.lock:
            if self._shutdown_requested or self._stopped:
                return False
            if self.state in [LifecycleState.RECONCILING, LifecycleState.HALTED]:
                return False

        self.freeze_system(
            f"Awaiting reconcile: {reason}",
            cancel_active_orders=True,
        )

        suppression = ""
        reconcile_args = None
        with self.lock:
            # freeze_system performs network cancellation outside its state
            # transition. Revalidate after that work so a concurrent HALT or
            # shutdown cannot be overwritten by a late reconcile start.
            if (
                self._shutdown_requested
                or self._stopped
                or self.state in {LifecycleState.RECONCILING, LifecycleState.HALTED}
            ):
                return False
            if self.state != LifecycleState.FROZEN:
                return False

            now = time.perf_counter()
            if (
                self.last_reconcile_failure_ts
                and now - self.last_reconcile_failure_ts
                < self.reconcile_api_cooldown_sec
            ):
                suppression = "api_failure"
            elif (
                now - self.last_reconcile_request_ts
                < self.reconcile_min_interval_sec
            ):
                suppression = "min_interval"

            if suppression:
                self._audit(
                    "reconcile_suppressed",
                    reason=reason,
                    suspicious_oid=suspicious_oid,
                    cooldown=suppression,
                )
            else:
                self.last_reconcile_request_ts = now
                logger.warning(f"OMS dirty: {reason}. State -> RECONCILING")
                self.state = LifecycleState.RECONCILING
                self._lifecycle_generation += 1
                reconcile_generation = self._lifecycle_generation
                self._sync_capability_mode(reason)
                self._audit(
                    "reconcile_requested",
                    state=self.state.value,
                    reason=reason,
                    suspicious_oid=suspicious_oid,
                )
                reconcile_args = (
                    suspicious_oid,
                    recovery_venue,
                    recovery_epoch,
                    reconcile_generation,
                    recovery_owner,
                    recovery_reason,
                )

        if suppression:
            logger.warning(
                f"[OMS] Reconcile suppressed by {suppression}: {reason}"
            )
            self._schedule_reconcile_retry(reason, suspicious_oid=suspicious_oid)
            return False
        if reconcile_args is None:
            return False

        published = threading.Event()

        def execute_reconcile():
            published.wait()
            self._execute_reconcile(*reconcile_args)

        reconcile_thread = self._submit_background_task(
            "reconcile:execute",
            execute_reconcile,
            name="OMSReconcile",
        )
        with self.lock:
            if reconcile_thread is None:
                if self.state == LifecycleState.RECONCILING:
                    self.state = LifecycleState.FROZEN
                    self._lifecycle_generation += 1
                    self._sync_capability_mode(
                        "reconcile_worker_unavailable"
                    )
                published.set()
                return False
            self._reconcile_thread = reconcile_thread
        published.set()
        return True

    def _schedule_reconcile_retry(
        self,
        reason: str,
        suspicious_oid: str = None,
        delay_sec: float = None,
    ):
        with self.lock:
            if (
                self._shutdown_requested
                or self._stopped
                or self.reconcile_retry_scheduled
                or self.state == LifecycleState.HALTED
            ):
                return False
            now = time.perf_counter()
            if delay_sec is None:
                cooldown_remaining = 0.0
                if self.last_reconcile_failure_ts:
                    cooldown_remaining = max(
                        0.0,
                        self.reconcile_api_cooldown_sec
                        - (now - self.last_reconcile_failure_ts),
                    )
                interval_remaining = max(
                    0.0,
                    self.reconcile_min_interval_sec
                    - (now - self.last_reconcile_request_ts),
                )
                delay_sec = max(
                    cooldown_remaining,
                    interval_remaining,
                    0.05,
                )
            delay_sec = max(float(delay_sec), 0.05)
            self.reconcile_retry_scheduled = True
        self._audit(
            "reconcile_retry_scheduled",
            reason=reason,
            suspicious_oid=suspicious_oid,
            delay_sec=delay_sec,
        )

        def _retry():
            with self.lock:
                self.reconcile_retry_scheduled = False
                if self._shutdown_requested or self._stopped:
                    return
                should_retry = self.state == LifecycleState.FROZEN
            if not should_retry:
                return
            self.trigger_reconcile(reason, suspicious_oid=suspicious_oid)

        handle = self._submit_background_task(
            "reconcile:retry",
            _retry,
            name="OMSReconcileRetry",
            delay_sec=delay_sec,
        )
        if handle is None:
            with self.lock:
                self.reconcile_retry_scheduled = False
            return False
        return True

    def _execute_reconcile(
        self,
        suspicious_oid: str,
        recovery_venue: str = "",
        recovery_epoch: int | None = None,
        reconcile_generation: int | None = None,
        recovery_owner: str = "",
        recovery_reason: str = "",
    ):
        with self.lock:
            if self._shutdown_requested or self._stopped:
                return
            if reconcile_generation is None:
                reconcile_generation = self._lifecycle_generation
        self._audit("reconcile_started", suspicious_oid=suspicious_oid)
        try:
            remote_positions = self.query_positions()
            remote_orders = self.query_open_orders()
            remote_account = self.query_account_info()

            recovery_symbols = set(self.config.get("symbols", []))
            if remote_positions is not None:
                recovery_symbols.update(
                    str(position.get("symbol", "") or "").upper()
                    for position in remote_positions
                    if isinstance(position, dict)
                    and str(position.get("symbol", "") or "").strip()
                )
            if remote_orders is not None:
                recovery_symbols.update(
                    item["symbol"]
                    for item in self._normalize_remote_open_orders(remote_orders)
                )
            trade_backfill_ok = self._backfill_trade_history(
                symbols=recovery_symbols,
                end_time_ms=time_service.now(),
            )

            if (
                not trade_backfill_ok
                or remote_positions is None
                or remote_orders is None
                or not remote_account
                or not self._refresh_missing_local_order_terminals(remote_orders)
            ):
                self.consecutive_reconcile_api_failures += 1
                self.last_reconcile_failure_ts = time.perf_counter()
                attempt = self.consecutive_reconcile_api_failures
                self._audit(
                    "reconcile_api_unreachable",
                    failures=attempt,
                    suspicious_oid=suspicious_oid,
                )
                if attempt >= self.reconcile_api_failure_threshold:
                    self.halt_system("Reconcile API unreachable")
                else:
                    logger.error(
                        f"[Reconcile] API unreachable ({attempt}/{self.reconcile_api_failure_threshold}); "
                        "keeping FROZEN and backing off."
                    )
                    self.freeze_system("Reconcile API unreachable")
                    self._schedule_reconcile_retry(
                        "Reconcile API retry",
                        suspicious_oid=suspicious_oid,
                    )
                return

            self.consecutive_reconcile_api_failures = 0
            self.last_reconcile_failure_ts = 0.0

            with self.lock:
                remote_map = {
                    pos["symbol"]: float(pos["positionAmt"])
                    for pos in remote_positions
                    if float(pos["positionAmt"]) != 0
                }
                local_map = {
                    symbol: volume
                    for symbol, volume in self.exposure.net_positions.items()
                    if volume != 0
                }
                local_active_orders = self._collect_local_active_orders_locked()
                local_balance = float(self.account.balance or 0.0)
                local_balance_synced = bool(self.account.exchange_balance_synced)

            configured_symbols = {
                str(symbol or "").upper()
                for symbol in self.config.get("symbols", [])
                if str(symbol or "").strip()
            }
            off_config_positions = {
                symbol: amount
                for symbol, amount in remote_map.items()
                if symbol not in configured_symbols and abs(amount) > 1e-9
            }
            if off_config_positions:
                self._audit(
                    "reconcile_reset",
                    case="off_config_nonzero_positions",
                    remote_positions=off_config_positions,
                )
                self._perform_full_reset()
                return

            remote_balance = float(
                remote_account.get("totalWalletBalance", 0.0) or 0.0
            )
            truth_config = self.config.get("oms", {}).get("truth_monitor", {}) or {}
            balance_tolerance = max(
                0.0,
                float(truth_config.get("account_balance_tolerance", 1.0) or 1.0),
            )
            if (
                not local_balance_synced
                or abs(local_balance - remote_balance) > balance_tolerance
            ):
                self._audit(
                    "reconcile_reset",
                    case="account_balance_mismatch",
                    local_balance=local_balance,
                    remote_balance=remote_balance,
                    tolerance=balance_tolerance,
                    local_balance_synced=local_balance_synced,
                )
                self._perform_full_reset()
                return

            for symbol in set(remote_map) | set(local_map):
                if abs(local_map.get(symbol, 0.0) - remote_map.get(symbol, 0.0)) > 1e-6:
                    logger.error(
                        f"[Reconcile] Position mismatch {symbol}: "
                        f"Local={local_map.get(symbol, 0.0)}, Exch={remote_map.get(symbol, 0.0)}"
                    )
                    self._audit("reconcile_reset", case="position_mismatch", symbol=symbol)
                    self._perform_full_reset()
                    return

            remote_active_orders = self._normalize_remote_open_orders(remote_orders)
            off_config_orders = [
                item
                for item in remote_active_orders
                if item["symbol"] not in configured_symbols
            ]
            if off_config_orders:
                self._audit(
                    "reconcile_reset",
                    case="off_config_open_orders",
                    remote_active_orders=off_config_orders,
                )
                self._perform_full_reset()
                return
            if local_active_orders != remote_active_orders:
                self._audit(
                    "reconcile_reset",
                    case="open_order_mismatch",
                    local_active_orders=local_active_orders,
                    remote_active_orders=remote_active_orders,
                    suspicious_oid=suspicious_oid,
                )
                self._perform_full_reset()
                return

            remote_has_suspicious = False
            local_has_suspicious = False
            if suspicious_oid:
                remote_has_suspicious = any(
                    suspicious_oid in order["identifiers"]
                    for order in remote_active_orders
                )
                local_has_suspicious = any(
                    suspicious_oid in order["identifiers"]
                    for order in local_active_orders
                )

            if remote_has_suspicious and not local_has_suspicious:
                self._audit(
                    "reconcile_reset",
                    case="missing_local_order",
                    suspicious_oid=suspicious_oid,
                )
                self._perform_full_reset()
            else:
                with self.lock:
                    if self._shutdown_requested or self._stopped:
                        self._audit(
                            "reconcile_resume_suppressed",
                            reason="shutdown_requested",
                        )
                        return
                    if (
                        self.state != LifecycleState.RECONCILING
                        or self._lifecycle_generation != reconcile_generation
                    ):
                        self._audit(
                            "reconcile_resume_suppressed",
                            reason="lifecycle_superseded",
                            current_state=self.state.value,
                            expected_generation=reconcile_generation,
                            current_generation=self._lifecycle_generation,
                        )
                        return
                    self.state = LifecycleState.LIVE
                    self._lifecycle_generation += 1
                    self._sync_capability_mode("reconcile_cleared")
                    self.last_freeze_reason = ""
                    self._clear_recovered_guards_if_pending("reconcile_cleared")
                    self._audit("reconcile_cleared", state=self.state.value)
                logger.info("[Reconcile] False alarm. Resuming LIVE.")

        except Exception as exc:
            self.halt_system(f"Reconcile critical error: {exc}")
        finally:
            if recovery_venue:
                try:
                    self._complete_venue_recovery_verification(
                        recovery_venue,
                        recovery_epoch,
                        recovery_owner,
                        recovery_reason,
                    )
                except Exception as exc:
                    self.halt_system(
                        "Venue recovery verification failed: "
                        f"{type(exc).__name__}:{exc}"
                    )
            with self.lock:
                if (
                    self._reconcile_thread is not None
                    and self._reconcile_thread.is_current()
                ):
                    self._reconcile_thread = None
                schedule_pending = bool(
                    self._pending_reconcile_requests
                    and self.state != LifecycleState.HALTED
                    and not self._shutdown_requested
                    and not self._stopped
                )
            if schedule_pending:
                self._schedule_pending_reconcile_requests()

    def _complete_venue_recovery_verification(
        self,
        venue: str,
        expected_epoch: int | None,
        expected_owner: str = "",
        expected_reason: str = "",
    ) -> bool:
        venue = str(venue or "").upper()
        with self.lock:
            if self.state != LifecycleState.LIVE:
                return False
            records = self._ensure_venue_guard_records_locked(venue)
            record = records.get(str(expected_owner or ""))
            current_epoch = (
                int(record.get("epoch", 0) or 0)
                if record is not None
                else 0
            )
            current_reason = (
                str(record.get("reason", "") or "")
                if record is not None
                else ""
            )
            if (
                not expected_owner
                or expected_epoch is None
                or current_epoch != int(expected_epoch)
                or current_reason != str(expected_reason or "")
            ):
                self.state = LifecycleState.FROZEN
                self._lifecycle_generation += 1
                self.last_freeze_reason = (
                    f"Venue recovery owner changed for {venue}: "
                    f"owner={expected_owner} expected_epoch={expected_epoch} "
                    f"current_epoch={current_epoch}"
                )
                self._sync_capability_mode("venue_recovery_epoch_changed")
                self._audit(
                    "venue_recovery_verification_stale",
                    venue=venue,
                    expected_owner=expected_owner,
                    expected_epoch=expected_epoch,
                    current_epoch=current_epoch,
                    expected_reason=expected_reason,
                    current_reason=current_reason,
                )
                return False

        cleared = self.clear_venue_freeze(
            venue,
            reason="truth_verified_after_transport_recovery",
            expected_epoch=expected_epoch,
            expected_reason=expected_reason,
            expected_owner=expected_owner,
        )
        if cleared:
            self._audit(
                "venue_recovery_verified",
                venue=venue,
                epoch=int(expected_epoch),
                owner=expected_owner,
            )
        return cleared

    def _exchange_snapshot_signature(self, account, positions, open_orders):
        normalized_positions = tuple(
            sorted(
                (
                    str(pos.get("symbol", "") or ""),
                    float(pos.get("positionAmt", 0.0) or 0.0),
                    float(pos.get("entryPrice", 0.0) or 0.0),
                )
                for pos in positions
                if pos.get("symbol")
            )
        )
        normalized_orders = tuple(
            (
                item["symbol"],
                item["identifiers"],
                item["side"],
            )
            for item in self._normalize_remote_open_orders(open_orders)
        )
        # Exclude mark-to-market fields such as initial margin and available
        # balance: they legitimately move while prices change. The barrier is
        # for structural order/position state plus settled wallet balance.
        account_signature = float(account.get("totalWalletBalance", 0.0) or 0.0)
        return normalized_positions, normalized_orders, account_signature

    def _capture_stable_exchange_snapshot(self, require_no_open_orders=False):
        previous_signature = None
        stable_count = 0
        last_payload = None

        for attempt in range(1, self.snapshot_max_attempts + 1):
            account_floor = time_service.now() / 1000.0
            open_orders = self.query_open_orders()
            account = self.query_account_info()
            positions_floor = time_service.now() / 1000.0
            positions = self.query_positions()
            snapshot_end_ms = time_service.now()
            if open_orders is None or not account or positions is None:
                raise RuntimeError("API failed while acquiring stable exchange snapshot")

            signature = self._exchange_snapshot_signature(account, positions, open_orders)
            if signature == previous_signature:
                stable_count += 1
            else:
                stable_count = 1
                previous_signature = signature

            normalized_orders = self._normalize_remote_open_orders(open_orders)
            last_payload = {
                "open_orders": open_orders,
                "account": account,
                "positions": positions,
                "account_floor": account_floor,
                "positions_floor": positions_floor,
                "end_time_ms": snapshot_end_ms,
                "attempt": attempt,
            }
            if (
                stable_count >= self.snapshot_stability_required
                and (not require_no_open_orders or not normalized_orders)
            ):
                self._audit(
                    "stable_snapshot_acquired",
                    attempts=attempt,
                    stable_count=stable_count,
                    end_time_ms=snapshot_end_ms,
                )
                return last_payload

            time.sleep(self.snapshot_settle_interval_sec)

        residual = (
            self._normalize_remote_open_orders(last_payload["open_orders"])
            if last_payload
            else []
        )
        raise RuntimeError(
            "exchange snapshot did not stabilize"
            + (f"; residual open orders={residual}" if residual else "")
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

            residual_orders = self._normalize_remote_open_orders(remote_orders)
            if residual_orders:
                raise RuntimeError(
                    f"remote open orders still present after cancel-all: {residual_orders}"
                )

            with self.lock:
                previously_tracked_symbols = set(self.exposure.net_positions.keys())
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
