"""Exchange reconciliation and full-reset orchestration."""

from __future__ import annotations

import threading
import time

from infrastructure.logger import logger
from infrastructure.time_service import time_service

from event.type import LifecycleState

from .component import OMSComponent
from .exchange_snapshot import (
    ExchangeSnapshotNormalizer,
    ExchangeSnapshotQueries,
    StableExchangeSnapshotCollector,
    StableSnapshotPolicy,
)


class OMSReconciler(OMSComponent):
    """Own truth reconciliation, retry and full-reset workflows."""

    LOCAL_STATE = frozenset(
        {
            "_snapshot_collector",
            "_snapshot_normalizer",
        }
    )

    OWNER_READS = frozenset(
        {
            "_audit",
            "_backfill_trade_history",
            "_clear_recovered_guards_if_pending",
            "_ensure_venue_guard_records_locked",
            "_known_account_order_symbols",
            "_latch_background_task_failure",
            "_max_pending_reconcile_requests",
            "_pending_reconcile_requests",
            "_perform_full_reset",
            "_refresh_missing_local_order_terminals",
            "_shutdown_requested",
            "_stopped",
            "_submit_background_task",
            "_sync_capability_mode",
            "account",
            "clear_venue_freeze",
            "config",
            "exposure",
            "freeze_system",
            "halt_system",
            "last_reconcile_failure_ts",
            "last_reconcile_request_ts",
            "lock",
            "orders",
            "query_account_info",
            "query_open_orders",
            "query_positions",
            "reconcile_api_cooldown_sec",
            "reconcile_api_failure_threshold",
            "reconcile_min_interval_sec",
            "snapshot_max_attempts",
            "snapshot_settle_interval_sec",
            "snapshot_stability_required",
        }
    )
    OWNER_WRITES = frozenset(
        {
            "_lifecycle_generation",
            "_reconcile_thread",
            "consecutive_reconcile_api_failures",
            "last_freeze_reason",
            "last_reconcile_failure_ts",
            "last_reconcile_request_ts",
            "reconcile_retry_scheduled",
            "state",
        }
    )

    def __init__(self, owner) -> None:
        super().__init__(owner)
        self._snapshot_normalizer = ExchangeSnapshotNormalizer()
        self._snapshot_collector = StableExchangeSnapshotCollector(
            queries=ExchangeSnapshotQueries(
                open_orders=lambda: self.query_open_orders(),
                account=lambda: self.query_account_info(),
                positions=lambda: self.query_positions(),
            ),
            policy=lambda: StableSnapshotPolicy(
                stability_required=self.snapshot_stability_required,
                max_attempts=self.snapshot_max_attempts,
                settle_interval_sec=self.snapshot_settle_interval_sec,
            ),
            normalize_account=lambda account, **kwargs: (
                self._normalize_remote_account(account, **kwargs)
            ),
            normalize_positions=lambda positions: (
                self._normalize_remote_positions(positions)
            ),
            normalize_open_orders=lambda orders: (
                self._normalize_remote_open_orders(orders)
            ),
            snapshot_signature=lambda account, positions, orders: (
                self._exchange_snapshot_signature(
                    account,
                    positions,
                    orders,
                )
            ),
            audit=lambda event, **fields: self._audit(event, **fields),
            now_ms=lambda: time_service.now(),
            sleep=lambda delay: time.sleep(delay),
        )

    def _normalize_remote_account(
        self,
        account,
        *,
        require_initial_margin: bool = False,
    ):
        return self._snapshot_normalizer.account(
            account,
            require_initial_margin=require_initial_margin,
        )

    def _normalize_remote_account_balances(self, account) -> dict:
        return self._snapshot_normalizer.account_balances(account)

    def _normalize_remote_positions(self, positions):
        return self._snapshot_normalizer.positions(positions)

    def _normalize_remote_open_orders(self, remote_orders):
        normalized = self._snapshot_normalizer.open_orders(remote_orders)
        with self.lock:
            self._known_account_order_symbols.update(
                item["symbol"] for item in normalized
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
            try:
                exchange_snapshot = (
                    self._snapshot_collector.capture(
                        require_initial_margin=False,
                    )
                )
            except (RuntimeError, ValueError) as exc:
                exchange_snapshot = None
                self._audit(
                    "reconcile_snapshot_unavailable",
                    reason=f"{type(exc).__name__}:{exc}",
                )

            remote_positions = (
                exchange_snapshot.positions
                if exchange_snapshot is not None
                else None
            )
            remote_orders = (
                exchange_snapshot.open_orders
                if exchange_snapshot is not None
                else None
            )
            remote_account = (
                exchange_snapshot.account
                if exchange_snapshot is not None
                else None
            )

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
                end_time_ms=(
                    exchange_snapshot.trade_watermark_ms
                    if exchange_snapshot is not None
                    else time_service.now()
                ),
            )
            if trade_backfill_ok and exchange_snapshot is not None:
                try:
                    confirmation = (
                        self._snapshot_collector.capture(
                            require_initial_margin=False,
                        )
                    )
                except (RuntimeError, ValueError) as exc:
                    trade_backfill_ok = False
                    self._audit(
                        "reconcile_snapshot_confirmation_failed",
                        reason=f"{type(exc).__name__}:{exc}",
                    )
                else:
                    if confirmation.signature != exchange_snapshot.signature:
                        trade_backfill_ok = False
                        self._audit(
                            "reconcile_snapshot_changed",
                            initial_end_time_ms=(
                                exchange_snapshot.end_time_ms
                            ),
                            confirmation_end_time_ms=(
                                confirmation.end_time_ms
                            ),
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
        normalized_orders = self._normalize_remote_open_orders(open_orders)
        return self._snapshot_normalizer.signature(
            account,
            positions,
            normalized_orders,
        )

    def _capture_stable_exchange_snapshot(self, require_no_open_orders=False):
        return self._snapshot_collector.capture(
            require_no_open_orders=require_no_open_orders,
        )
