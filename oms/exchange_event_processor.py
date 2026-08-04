"""Apply exchange order and account events to OMS state."""

from __future__ import annotations

import math
from datetime import datetime

from infrastructure.logger import logger

from event.type import (
    CommandOutcome,
    EVENT_TRADE_UPDATE,
    Event,
    ExchangeAccountUpdate,
    ExchangeOrderUpdate,
    LifecycleState,
    OrderStatus,
    TradeData,
)

from .component import OMSComponent
from .journal import JournalCorruptionError, JournalError
from .order import Order


class OMSExchangeEventProcessor(OMSComponent):
    """Own deterministic exchange-event and recovered-command application."""

    OWNER_READS = frozenset(
        {
            "CASH_FLOW_DIRTY_REASONS",
            "_account_state_event_time",
            "_audit",
            "_emit_order_update",
            "_emit_position_update",
            "_enforce_symbol_guard",
            "_exchange_account_event_time",
            "_exchange_position_event_time",
            "_execution_id",
            "_fail_closed_on_journal_error",
            "_get_fill_commission",
            "_has_active_orders_locked",
            "_install_symbol_guard_locked",
            "_lifecycle_generation",
            "_position_state_event_time",
            "_queue_reconcile_request_locked",
            "_record_execution",
            "_record_order_snapshot",
            "_schedule_rpi_calibration_runtime_enforcement",
            "_schedule_trade_tail_verification",
            "_submit_background_task",
            "_sync_capability_mode",
            "_write_tombstone",
            "account",
            "config",
            "event_engine",
            "event_log",
            "event_log_evictions",
            "event_log_max",
            "exchange_id_map",
            "execution_ids",
            "exposure",
            "last_freeze_reason",
            "lock",
            "mark_external_cash_flow_truth_unavailable",
            "order_monitor",
            "orders",
            "state",
            "terminated_oids",
            "trigger_reconcile",
        }
    )
    OWNER_WRITES = frozenset(
        {
            "_account_state_event_time",
            "_exchange_account_event_time",
            "_lifecycle_generation",
            "event_log_evictions",
            "last_freeze_reason",
            "state",
        }
    )

    _SUPPORTED_ORDER_UPDATE_STATUSES = {
        "NEW",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELED",
        "EXPIRED",
        "REJECTED",
    }
    _ORDER_UPDATE_NONNEGATIVE_FLOAT_FIELDS = (
        "filled_qty",
        "filled_price",
        "cum_filled_qty",
        "update_time",
        "received_timestamp",
        "received_monotonic",
        "dispatch_timestamp",
        "dispatch_monotonic",
        "corrected_received_timestamp",
    )
    _ORDER_UPDATE_OPTIONAL_FLOAT_FIELDS = (
        "commission",
        "realized_pnl",
        "clock_offset_ms",
    )
    _ACCOUNT_UPDATE_NONNEGATIVE_TIME_FIELDS = (
        "event_time",
        "received_timestamp",
        "received_monotonic",
        "dispatch_timestamp",
        "dispatch_monotonic",
        "corrected_received_timestamp",
    )

    def on_exchange_update(self, event):
        try:
            self._append_and_process(event)
        except JournalError as exc:
            symbol = str(getattr(event.data, "symbol", "") or "")
            self._fail_closed_on_journal_error(exc, "exchange_update", symbol)

    def on_exchange_account_update(self, event):
        update: ExchangeAccountUpdate = event.data
        invalid_detail = self._normalize_exchange_account_update_numbers(
            update
        )
        if invalid_detail:
            with self.lock:
                self._quarantine_invalid_account_update_locked(
                    update,
                    invalid_detail,
                )
            return
        if str(update.reason or "").upper() in self.CASH_FLOW_DIRTY_REASONS:
            self.mark_external_cash_flow_truth_unavailable(
                f"account_update:{str(update.reason).upper()}"
            )
        tracked_symbols = set(self.config.get("symbols", []))
        tracked_positions = {
            symbol: payload
            for symbol, payload in update.positions.items()
            if not tracked_symbols or symbol in tracked_symbols
        }

        event_time = float(update.event_time or 0.0)
        has_balance_update = bool(update.asset or update.balances)
        corrected_positions = {}
        corrected_with_active_order = {}

        with self.lock:
            # ACCOUNT_UPDATE.P is a partial delta: only symbols changed by this
            # account event are present. Absence must never be interpreted as a
            # flat position.
            for symbol, payload in tracked_positions.items():
                state_time = float(self._position_state_event_time.get(symbol, 0.0) or 0.0)
                if event_time and state_time and event_time + 1e-6 < state_time:
                    self._audit(
                        "stale_exchange_position_update_ignored",
                        symbol=symbol,
                        event_time=event_time,
                        state_time=state_time,
                    )
                    continue

                remote_volume = float(payload.get("volume", 0.0) or 0.0)
                remote_entry_price = float(payload.get("entry_price", 0.0) or 0.0)
                local_volume = float(self.exposure.net_positions.get(symbol, 0.0) or 0.0)
                if abs(local_volume - remote_volume) > 1e-9:
                    if not corrected_positions:
                        self._schedule_rpi_calibration_runtime_enforcement(
                            terminal_truth_changed=True,
                        )
                    corrected_positions[symbol] = {
                        "local": local_volume,
                        "exchange": remote_volume,
                        "entry_price": remote_entry_price,
                    }
                    corrected_with_active_order[symbol] = self._has_active_orders_locked({symbol})
                    self.exposure.force_sync(symbol, remote_volume, remote_entry_price)
                    self.exposure.reconcile_strategy_position(
                        symbol,
                        remote_volume,
                        remote_entry_price,
                    )

                if event_time:
                    self._position_state_event_time[symbol] = max(state_time, event_time)
                    self._exchange_position_event_time[symbol] = max(
                        float(self._exchange_position_event_time.get(symbol, 0.0) or 0.0),
                        event_time,
                    )

            account_state_time = float(self._account_state_event_time or 0.0)
            account_update_is_stale = bool(
                event_time
                and account_state_time
                and event_time + 1e-6 < account_state_time
            )
            if account_update_is_stale:
                self._audit(
                    "stale_exchange_account_update_ignored",
                    event_time=event_time,
                    state_time=account_state_time,
                )
                if corrected_positions:
                    self.account.calculate()
            elif has_balance_update:
                self.account.sync_exchange_balance(
                    update.wallet_balance,
                    available=update.available_balance,
                    asset=update.asset,
                    balances=update.balances,
                )
                if event_time:
                    self._account_state_event_time = max(account_state_time, event_time)
                    self._exchange_account_event_time = max(
                        float(self._exchange_account_event_time or 0.0),
                        event_time,
                    )

        if has_balance_update and not corrected_positions:
            self._schedule_rpi_calibration_runtime_enforcement()
        if not corrected_positions:
            return

        self._audit(
            "exchange_account_positions_synced",
            reason=update.reason,
            event_time=event_time,
            positions=corrected_positions,
        )

        for symbol in corrected_positions:
            self._emit_position_update(symbol)
            self._schedule_trade_tail_verification(
                symbol,
                reason="exchange_account_position_correction",
            )

        if self.state in {LifecycleState.HALTED, LifecycleState.RECONCILING}:
            return

        unexpected_positions = {
            symbol: payload
            for symbol, payload in corrected_positions.items()
            if not corrected_with_active_order.get(symbol, False)
        }
        if unexpected_positions:
            logger.error(
                "[OMS] Exchange position correction without an active local order: "
                f"{unexpected_positions}"
            )
            self.trigger_reconcile(
                "Unexpected exchange account position correction"
            )

    @classmethod
    def _normalize_exchange_account_update_numbers(
        cls,
        update: ExchangeAccountUpdate,
    ) -> str:
        normalized_scalars = {}
        for field in ("wallet_balance", "available_balance", "clock_offset_ms"):
            raw_value = getattr(update, field, None)
            if raw_value is None and field != "wallet_balance":
                normalized_scalars[field] = None
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                return f"{field}:not_numeric"
            if not math.isfinite(value):
                return f"{field}:not_finite"
            normalized_scalars[field] = value

        for field in cls._ACCOUNT_UPDATE_NONNEGATIVE_TIME_FIELDS:
            raw_value = getattr(update, field, 0.0)
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                return f"{field}:not_numeric"
            if not math.isfinite(value):
                return f"{field}:not_finite"
            if value < 0.0:
                return f"{field}:negative"
            normalized_scalars[field] = value

        raw_balances = getattr(update, "balances", {})
        if not isinstance(raw_balances, dict):
            return "balances:not_mapping"
        normalized_balances = {}
        for raw_asset, raw_payload in raw_balances.items():
            asset = str(raw_asset or "").upper().strip()
            if not asset:
                return "balances:empty_asset"
            if not isinstance(raw_payload, dict):
                return f"balances:{asset}:not_mapping"
            payload = dict(raw_payload)
            for field in (
                "wallet_balance",
                "available_balance",
                "cross_wallet_balance",
                "balance_change",
            ):
                raw_value = payload.get(field)
                if raw_value is None:
                    continue
                try:
                    value = float(raw_value)
                except (TypeError, ValueError):
                    return f"balances:{asset}:{field}:not_numeric"
                if not math.isfinite(value):
                    return f"balances:{asset}:{field}:not_finite"
                payload[field] = value
            normalized_balances[asset] = payload

        raw_positions = getattr(update, "positions", {})
        if not isinstance(raw_positions, dict):
            return "positions:not_mapping"
        normalized_positions = {}
        for raw_symbol, raw_payload in raw_positions.items():
            symbol = str(raw_symbol or "").upper().strip()
            if not symbol:
                return "positions:empty_symbol"
            if not isinstance(raw_payload, dict):
                return f"positions:{symbol}:not_mapping"
            payload = dict(raw_payload)
            for field in ("volume", "entry_price", "unrealized_pnl"):
                raw_value = payload.get(field, 0.0)
                try:
                    value = float(raw_value)
                except (TypeError, ValueError):
                    return f"positions:{symbol}:{field}:not_numeric"
                if not math.isfinite(value):
                    return f"positions:{symbol}:{field}:not_finite"
                if field == "entry_price" and value < 0.0:
                    return f"positions:{symbol}:{field}:negative"
                payload[field] = value
            normalized_positions[symbol] = payload

        for field, value in normalized_scalars.items():
            setattr(update, field, value)
        update.balances = normalized_balances
        update.positions = normalized_positions
        return ""

    def _quarantine_invalid_account_update_locked(
        self,
        update: ExchangeAccountUpdate,
        detail: str,
    ) -> None:
        reason = f"account_truth:invalid_update:{detail}"
        self._audit(
            "invalid_exchange_account_update",
            reason=reason,
            asset=str(getattr(update, "asset", "") or "").upper(),
            update_reason=str(getattr(update, "reason", "") or ""),
            detail=detail,
            wallet_balance=repr(
                getattr(update, "wallet_balance", None)
            )[:160],
            available_balance=repr(
                getattr(update, "available_balance", None)
            )[:160],
            balances=repr(getattr(update, "balances", None))[:1000],
            positions=repr(getattr(update, "positions", None))[:1000],
        )
        if self.state not in {
            LifecycleState.HALTED,
            LifecycleState.RECONCILING,
        }:
            previous_state = self.state
            self.state = LifecycleState.FROZEN
            self._lifecycle_generation += 1
            self.last_freeze_reason = reason
            self._sync_capability_mode(reason)
            self._audit(
                "lifecycle",
                state=self.state.value,
                reason=reason,
                previous_state=previous_state.value,
            )
        self._queue_reconcile_request_locked(
            f"Invalid exchange account update: {detail}",
        )

    def _append_and_process(self, event):
        if len(self.event_log) >= self.event_log_max:
            self.event_log_evictions += 1
        self.event_log.append(event)
        self._apply_event(event)

    def _quarantine_execution_gap_locked(
        self,
        order: Order,
        update: ExchangeOrderUpdate,
        delta: float,
        detail: str,
    ) -> None:
        reason = (
            f"execution_truth:{order.intent.symbol}:{order.client_oid}:"
            f"{detail}"
        )
        self._audit(
            "execution_truth_gap",
            reason=reason,
            client_oid=order.client_oid,
            exchange_oid=update.exchange_oid or order.exchange_oid,
            symbol=order.intent.symbol,
            status=update.status,
            local_filled=order.filled_volume,
            incoming_cumulative=update.cum_filled_qty,
            incoming_last_fill=update.filled_qty,
            missing_delta=delta,
            trade_id=update.trade_id,
        )
        if self.state not in {
            LifecycleState.HALTED,
            LifecycleState.RECONCILING,
        }:
            previous_state = self.state
            self.state = LifecycleState.FROZEN
            self._lifecycle_generation += 1
            self.last_freeze_reason = reason
            self._sync_capability_mode(reason)
            self._audit(
                "lifecycle",
                state=self.state.value,
                reason=reason,
                previous_state=previous_state.value,
            )
        self._queue_reconcile_request_locked(
            f"Unverified execution gap {order.client_oid}",
            order.client_oid,
        )

    @classmethod
    def _normalize_exchange_order_update_numbers(
        cls,
        update: ExchangeOrderUpdate,
    ) -> str:
        normalized = {}
        for field in cls._ORDER_UPDATE_NONNEGATIVE_FLOAT_FIELDS:
            raw_value = getattr(update, field, 0.0)
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                return f"{field}:not_numeric"
            if not math.isfinite(value):
                return f"{field}:not_finite"
            if value < 0.0:
                return f"{field}:negative"
            normalized[field] = value

        for field in cls._ORDER_UPDATE_OPTIONAL_FLOAT_FIELDS:
            raw_value = getattr(update, field, None)
            if raw_value is None:
                normalized[field] = None
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                return f"{field}:not_numeric"
            if not math.isfinite(value):
                return f"{field}:not_finite"
            normalized[field] = value

        for field, minimum in (("seq", 0), ("trade_id", -1)):
            raw_value = getattr(update, field, minimum)
            try:
                numeric_value = float(raw_value)
            except (TypeError, ValueError):
                return f"{field}:not_integer"
            if (
                not math.isfinite(numeric_value)
                or not numeric_value.is_integer()
            ):
                return f"{field}:not_integer"
            value = int(numeric_value)
            if value < minimum:
                return f"{field}:below_minimum"
            normalized[field] = value

        filled_qty = normalized["filled_qty"]
        cumulative_qty = normalized["cum_filled_qty"]
        tolerance = max(1e-9, abs(cumulative_qty) * 1e-9)
        if filled_qty > cumulative_qty + tolerance:
            return "filled_qty:exceeds_cumulative"

        for field, value in normalized.items():
            setattr(update, field, value)
        return ""

    def _quarantine_invalid_order_update_locked(
        self,
        update: ExchangeOrderUpdate,
        detail: str,
    ) -> None:
        suspicious_oid = str(
            getattr(update, "client_oid", "")
            or getattr(update, "exchange_oid", "")
            or ""
        )
        symbol = str(getattr(update, "symbol", "") or "").upper()
        reason = (
            f"execution_truth:{symbol or 'UNKNOWN'}:"
            f"{suspicious_oid or 'UNKNOWN'}:invalid_update:{detail}"
        )
        raw_values = {
            field: repr(getattr(update, field, None))[:160]
            for field in (
                *self._ORDER_UPDATE_NONNEGATIVE_FLOAT_FIELDS,
                *self._ORDER_UPDATE_OPTIONAL_FLOAT_FIELDS,
                "seq",
                "trade_id",
            )
        }
        self._audit(
            "invalid_exchange_order_update",
            reason=reason,
            client_oid=str(getattr(update, "client_oid", "") or ""),
            exchange_oid=str(getattr(update, "exchange_oid", "") or ""),
            symbol=symbol,
            status=str(getattr(update, "status", "") or ""),
            detail=detail,
            raw_values=raw_values,
        )
        if self.state not in {
            LifecycleState.HALTED,
            LifecycleState.RECONCILING,
        }:
            previous_state = self.state
            self.state = LifecycleState.FROZEN
            self._lifecycle_generation += 1
            self.last_freeze_reason = reason
            self._sync_capability_mode(reason)
            self._audit(
                "lifecycle",
                state=self.state.value,
                reason=reason,
                previous_state=previous_state.value,
            )
        self._queue_reconcile_request_locked(
            f"Invalid exchange order update: {detail}",
            suspicious_oid,
        )

    def _invalidate_rpi_terminal_truth_locked(self) -> None:
        self._schedule_rpi_calibration_runtime_enforcement(
            terminal_truth_changed=True,
        )

    def _apply_event(self, event):
        if event.type != "eExchangeOrderUpdate":
            return

        update: ExchangeOrderUpdate = event.data
        update.status = str(update.status or "").upper()
        if update.status == "EXPIRED_IN_MATCH":
            update.status = "EXPIRED"
        invalid_detail = self._normalize_exchange_order_update_numbers(update)
        with self.lock:
            order = self.orders.get(update.client_oid)
            if not order and update.exchange_oid:
                order = self.exchange_id_map.get(update.exchange_oid)

            if invalid_detail:
                self._invalidate_rpi_terminal_truth_locked()
                self._quarantine_invalid_order_update_locked(
                    update,
                    invalid_detail,
                )
                return

            if not order:
                suspicious = update.client_oid or update.exchange_oid
                tombstoned_oid = next(
                    (
                        oid
                        for oid in (update.client_oid, update.exchange_oid)
                        if oid and oid in self.terminated_oids
                    ),
                    "",
                )
                if (
                    tombstoned_oid
                    and update.status in {"CANCELED", "EXPIRED", "REJECTED"}
                    and update.cum_filled_qty <= 1e-9
                    and update.filled_qty <= 1e-9
                ):
                    self._audit(
                        "late_duplicate_ignored",
                        suspicious_oid=tombstoned_oid,
                        exchange_status=update.status,
                    )
                    return
                self._schedule_rpi_calibration_runtime_enforcement(
                    terminal_truth_changed=True,
                )
                if tombstoned_oid:
                    self._audit(
                        "late_tombstone_truth_conflict",
                        suspicious_oid=tombstoned_oid,
                        client_oid=update.client_oid,
                        exchange_oid=update.exchange_oid,
                        exchange_status=update.status,
                        incoming_cum=update.cum_filled_qty,
                    )
                self._audit(
                    "unknown_order_update",
                    client_oid=update.client_oid,
                    exchange_oid=update.exchange_oid,
                    status=update.status,
                )
                self._queue_reconcile_request_locked(
                    f"Unknown Order {suspicious}",
                    suspicious,
                )
                return

            if update.seq and update.seq <= order.last_update_seq:
                self._audit(
                    "stale_update_ignored",
                    client_oid=order.client_oid,
                    seq=update.seq,
                    last_seq=order.last_update_seq,
                )
                return

            if (
                not update.seq
                and update.update_time
                and order.last_exchange_update_time
                and update.update_time + 1e-6 < order.last_exchange_update_time
                and update.cum_filled_qty <= order.filled_volume + 1e-9
            ):
                self._audit(
                    "stale_exchange_time_update_ignored",
                    client_oid=order.client_oid,
                    update_time=update.update_time,
                    last_exchange_update_time=order.last_exchange_update_time,
                )
                return

            self._schedule_rpi_calibration_runtime_enforcement(
                terminal_truth_changed=True,
            )

            if update.status not in self._SUPPORTED_ORDER_UPDATE_STATUSES:
                status_label = (update.status or "EMPTY")[:64]
                self._audit(
                    "unhandled_exchange_status",
                    client_oid=order.client_oid,
                    exchange_oid=update.exchange_oid,
                    symbol=order.intent.symbol,
                    status=status_label,
                )
                self._quarantine_invalid_order_update_locked(
                    update,
                    f"status:unsupported:{status_label}",
                )
                return

            if update.exchange_oid and order.exchange_oid and order.exchange_oid != update.exchange_oid:
                self._audit(
                    "exchange_oid_mismatch",
                    client_oid=order.client_oid,
                    local_exchange_oid=order.exchange_oid,
                    incoming_exchange_oid=update.exchange_oid,
                )
                self._queue_reconcile_request_locked(
                    f"Exchange OID mismatch {order.client_oid}",
                    order.client_oid,
                )
                return

            semantic_mismatches = {}
            if (
                update.order_type
                and update.order_type != order.intent.order_type
            ):
                semantic_mismatches["order_type"] = {
                    "expected": order.intent.order_type,
                    "actual": update.order_type,
                }
            if (
                update.time_in_force
                and order.intent.order_type == "LIMIT"
                and update.time_in_force != order.intent.time_in_force
            ):
                semantic_mismatches["time_in_force"] = {
                    "expected": order.intent.time_in_force,
                    "actual": update.time_in_force,
                }
            if semantic_mismatches:
                reason = f"exchange_order_semantics_mismatch:{order.client_oid}"
                self._audit(
                    "exchange_order_semantics_mismatch",
                    client_oid=order.client_oid,
                    symbol=order.intent.symbol,
                    mismatches=semantic_mismatches,
                )
                symbol = str(order.intent.symbol or "").upper()
                epoch, previous_owner_reason = (
                    self._install_symbol_guard_locked(symbol, reason)
                )
                if previous_owner_reason != reason:
                    logger.error(f"[OMS] Symbol frozen {symbol}: {reason}")
                self._submit_background_task(
                    f"freeze-symbol:{symbol}",
                    self._enforce_symbol_guard,
                    symbol,
                    reason,
                    epoch,
                    True,
                    name=f"FreezeSymbol-{symbol}",
                    safety=True,
                )

            if update.cum_filled_qty + 1e-9 < order.filled_volume:
                self._audit(
                    "cum_fill_regression",
                    client_oid=order.client_oid,
                    incoming_cum=update.cum_filled_qty,
                    local_cum=order.filled_volume,
                )
                self._queue_reconcile_request_locked(
                    f"Cum fill regression {order.client_oid}",
                    order.client_oid,
                )
                return

            incoming_delta = update.cum_filled_qty - order.filled_volume
            if incoming_delta > 1e-9:
                tolerance = max(1e-9, abs(incoming_delta) * 1e-9)
                gap_detail = ""
                if update.status not in {"FILLED", "PARTIALLY_FILLED"}:
                    gap_detail = "terminal_snapshot_ahead_of_trade_history"
                elif update.trade_id < 0:
                    gap_detail = "missing_trade_id"
                elif update.filled_qty <= 0.0:
                    gap_detail = "missing_last_fill_quantity"
                elif update.filled_price <= 0.0:
                    gap_detail = "missing_last_fill_price"
                elif abs(incoming_delta - update.filled_qty) > tolerance:
                    gap_detail = (
                        "cumulative_delta_exceeds_last_fill:"
                        f"{incoming_delta:.12g}!={update.filled_qty:.12g}"
                    )
                if gap_detail:
                    self._quarantine_execution_gap_locked(
                        order,
                        update,
                        incoming_delta,
                        gap_detail,
                    )
                    return

                execution_id = self._execution_id(order, update)
                if execution_id in self.execution_ids:
                    self._audit(
                        "duplicate_execution_ignored",
                        execution_id=execution_id,
                        client_oid=order.client_oid,
                        trade_id=update.trade_id,
                    )
                    return

            previous_status = order.status
            had_fill = False

            try:
                if update.status == "NEW":
                    order.mark_new(
                        exchange_oid=update.exchange_oid,
                        update_time=update.update_time,
                        seq=update.seq,
                    )
                    if update.exchange_oid:
                        self.exchange_id_map[update.exchange_oid] = order

                elif update.status == "CANCELED":
                    order.mark_cancelled(
                        update_time=update.update_time,
                        seq=update.seq,
                        exchange_status=update.status,
                    )
                    self._write_tombstone(order)

                elif update.status == "EXPIRED":
                    order.mark_expired(update_time=update.update_time, seq=update.seq)
                    self._write_tombstone(order)

                elif update.status == "REJECTED":
                    order.mark_rejected(
                        reason="exchange_rejected",
                        update_time=update.update_time,
                        seq=update.seq,
                        exchange_status=update.status,
                    )
                    self._write_tombstone(order)

                elif update.status in ["FILLED", "PARTIALLY_FILLED"]:
                    delta = update.cum_filled_qty - order.filled_volume
                    if delta > 1e-9:
                        fill_notional = delta * update.filled_price
                        fee = self._get_fill_commission(update, order, fill_notional)
                        # Execution truth is committed before mutating order,
                        # exposure, or account projections. If the process dies
                        # after this point, replay can finish applying the fill.
                        if not self._record_execution(
                            order,
                            update,
                            delta,
                            fee,
                        ):
                            self._audit(
                                "duplicate_execution_ignored",
                                execution_id=self._execution_id(order, update),
                                client_oid=order.client_oid,
                                trade_id=update.trade_id,
                            )
                            return
                        had_fill = order.add_fill(
                            delta,
                            update.filled_price,
                            update_time=update.update_time,
                            seq=update.seq,
                            exchange_status=update.status,
                        )
                        symbol = order.intent.symbol
                        if had_fill:
                            self.exposure.on_strategy_fill(
                                order.intent.strategy_id,
                                symbol,
                                order.intent.side,
                                delta,
                                update.filled_price,
                            )
                        exchange_position_time = float(
                            self._exchange_position_event_time.get(symbol, 0.0) or 0.0
                        )
                        position_already_synced = bool(
                            update.update_time
                            and exchange_position_time + 1e-6 >= update.update_time
                        )
                        if position_already_synced:
                            local_realized_pnl = 0.0
                            self._audit(
                                "fill_position_already_covered",
                                client_oid=order.client_oid,
                                symbol=symbol,
                                fill_time=update.update_time,
                                exchange_position_time=exchange_position_time,
                            )
                        else:
                            local_realized_pnl = self.exposure.on_fill(
                                symbol,
                                order.intent.side,
                                delta,
                                update.filled_price,
                            )
                            if update.update_time:
                                self._position_state_event_time[symbol] = max(
                                    float(self._position_state_event_time.get(symbol, 0.0) or 0.0),
                                    update.update_time,
                                )
                        self.exposure.reconcile_strategy_position(
                            symbol,
                            self.exposure.net_positions[symbol],
                            self.exposure.avg_prices[symbol],
                        )
                        realized_pnl = (
                            update.realized_pnl
                            if update.realized_pnl is not None
                            else local_realized_pnl
                        )
                        account_already_synced = bool(
                            update.update_time
                            and float(self._exchange_account_event_time or 0.0) + 1e-6
                            >= update.update_time
                        )
                        if account_already_synced:
                            self._audit(
                                "fill_account_already_covered",
                                client_oid=order.client_oid,
                                fill_time=update.update_time,
                                exchange_account_time=self._exchange_account_event_time,
                            )
                        else:
                            self.account.update_balance(realized_pnl, fee)
                            if update.update_time:
                                self._account_state_event_time = max(
                                    float(self._account_state_event_time or 0.0),
                                    update.update_time,
                                )

                        trade_data = TradeData(
                            symbol=order.intent.symbol,
                            order_id=order.client_oid,
                            trade_id=(
                                str(update.trade_id)
                                if update.trade_id >= 0
                                else f"T{int(update.update_time * 1000)}"
                            ),
                            side=order.intent.side.value,
                            price=update.filled_price,
                            volume=delta,
                            datetime=datetime.now(),
                        )
                        self.event_engine.put(Event(EVENT_TRADE_UPDATE, trade_data))
                    else:
                        order.note_exchange_update(
                            exchange_status=update.status,
                            update_time=update.update_time,
                            seq=update.seq,
                            exchange_oid=update.exchange_oid,
                        )

                    if update.status == "FILLED":
                        order.mark_filled(update_time=update.update_time, seq=update.seq)
                        self._write_tombstone(order)

            except ValueError as exc:
                self._audit(
                    "invalid_transition",
                    client_oid=order.client_oid,
                    current_status=order.status.value,
                    incoming_status=update.status,
                    error=str(exc),
                )
                self._queue_reconcile_request_locked(
                    f"Invalid transition {order.client_oid}",
                    order.client_oid,
                )
                return

            self.order_monitor.on_order_update(order.client_oid, order.status)
            self.exposure.update_open_orders(self.orders)
            self.account.calculate()

            if order.status != previous_status or had_fill:
                self._record_order_snapshot(
                    order,
                    "exchange_update",
                    exchange_status=update.status,
                    seq=update.seq,
                    cum_filled_qty=update.cum_filled_qty,
                )
                self._emit_order_update(order)
                if had_fill:
                    self._emit_position_update(order.intent.symbol)
                    self._schedule_trade_tail_verification(
                        order.intent.symbol,
                        trade_id=update.trade_id,
                        reason="user_stream_fill",
                    )

    def _apply_recovered_execution(self, order: Order, payload: dict):
        execution_id = str(payload.get("execution_id", "") or "")
        if not execution_id:
            raise JournalCorruptionError(
                f"Execution record without execution_id for {order.client_oid}"
            )
        self.execution_ids.add(execution_id)

        exchange_oid = str(payload.get("exchange_oid", "") or "")
        if exchange_oid:
            order.exchange_oid = exchange_oid

        try:
            cumulative_qty = float(payload.get("cum_filled_qty", 0.0) or 0.0)
            fill_price = float(payload.get("fill_price", 0.0) or 0.0)
            exchange_time = float(payload.get("exchange_time", 0.0) or 0.0)
        except (TypeError, ValueError) as exc:
            raise JournalCorruptionError(
                f"Malformed execution record {execution_id}: {exc}"
            ) from exc
        if (
            not math.isfinite(cumulative_qty)
            or cumulative_qty <= 0.0
            or not math.isfinite(fill_price)
            or fill_price <= 0.0
            or not math.isfinite(exchange_time)
            or exchange_time < 0.0
        ):
            raise JournalCorruptionError(
                f"Invalid execution values for {execution_id}"
            )

        delta = cumulative_qty - order.filled_volume
        if delta <= 1e-9:
            return

        pre_status = order.status
        order.add_fill(
            delta,
            fill_price,
            update_time=exchange_time,
            exchange_status=str(payload.get("exchange_status", "") or "PARTIALLY_FILLED"),
        )
        if order.status == OrderStatus.FILLED:
            return
        if pre_status == OrderStatus.CANCELLED:
            order.mark_cancelled(update_time=exchange_time)
        elif pre_status == OrderStatus.EXPIRED:
            order.mark_expired(update_time=exchange_time)

    def _apply_recovered_command_result(self, order: Order, payload: dict):
        command_type = str(payload.get("command_type", "") or "").upper()
        try:
            outcome = CommandOutcome(str(payload.get("outcome", "") or ""))
        except ValueError as exc:
            raise JournalCorruptionError(
                f"Invalid command outcome for {order.client_oid}: {payload.get('outcome')}"
            ) from exc

        error_message = str(
            payload.get("error_message", "")
            or payload.get("error_code", "")
            or "recovered_command_result"
        )
        exchange_oid = str(payload.get("exchange_oid", "") or "")

        if command_type == "SUBMIT":
            if order.status == OrderStatus.CREATED:
                order.mark_submitting()
            if order.status != OrderStatus.SUBMITTING:
                return
            if outcome == CommandOutcome.ACKNOWLEDGED:
                order.mark_pending_ack(exchange_oid)
            elif outcome == CommandOutcome.UNKNOWN:
                order.mark_submit_unknown(error_message)
            else:
                order.mark_rejected_locally(error_message)
            return

        if command_type == "CANCEL" and order.is_active():
            if order.status != OrderStatus.CANCELLING:
                try:
                    order.mark_cancelling()
                except ValueError:
                    pass
            if order.status == OrderStatus.CANCELLING:
                order.mark_cancel_unknown(
                    "recovered_cancel_ack_requires_truth"
                    if outcome == CommandOutcome.ACKNOWLEDGED
                    else error_message
                )
