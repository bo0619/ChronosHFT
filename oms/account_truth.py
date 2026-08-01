"""Exchange order, execution and external cash-flow truth recovery."""

from __future__ import annotations

import hashlib
import math
import time
from datetime import datetime, timezone

from event.type import (
    EVENT_EXCHANGE_ORDER_UPDATE,
    Event,
    ExchangeOrderUpdate,
    ExecutionPolicy,
    OrderIntent,
    OrderStatus,
    Side,
    TIF_GTC,
    TIF_GTX,
    TIF_RPI,
)
from infrastructure.logger import logger
from infrastructure.time_service import time_service

from .component import OMSComponent
from .execution_identity import discard_cursor_covered_execution_ids
from .journal import JournalError
from .order import Order


class OMSAccountTruth(OMSComponent):
    """Own recovery of exchange orders, fills and account cash flows."""

    @staticmethod
    def _finite_truth_float(
        value,
        field: str,
        *,
        minimum: float | None = None,
    ) -> float:
        try:
            normalized = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field}:not_numeric") from exc
        if not math.isfinite(normalized):
            raise ValueError(f"{field}:not_finite")
        if minimum is not None and normalized < minimum:
            raise ValueError(f"{field}:below_minimum")
        return normalized

    def _execution_id(self, order: Order, update: ExchangeOrderUpdate) -> str:
        venue = str(getattr(self.gateway, "gateway_name", "UNKNOWN") or "UNKNOWN").upper()
        if update.trade_id >= 0:
            return f"{venue}:{order.intent.symbol}:{update.trade_id}"
        exchange_order_id = update.exchange_oid or order.exchange_oid or order.client_oid
        return (
            f"{venue}:{order.intent.symbol}:{exchange_order_id}:"
            f"{int(float(update.update_time or 0.0) * 1000)}:"
            f"{float(update.cum_filled_qty):.12g}"
        )

    def _record_execution(
        self,
        order: Order,
        update: ExchangeOrderUpdate,
        fill_qty: float,
        fee: float,
    ) -> bool:
        execution_id = self._execution_id(order, update)
        if execution_id in self.execution_ids:
            return False

        payload = {
            "execution_id": execution_id,
            "venue": str(
                getattr(self.gateway, "gateway_name", "UNKNOWN") or "UNKNOWN"
            ).upper(),
            "client_oid": order.client_oid,
            "exchange_oid": update.exchange_oid or order.exchange_oid,
            "strategy_id": order.intent.strategy_id,
            "symbol": order.intent.symbol,
            "side": order.intent.side.value,
            "fill_qty": fill_qty,
            "fill_price": update.filled_price,
            "cum_filled_qty": update.cum_filled_qty,
            "exchange_status": update.status,
            "exchange_time": update.update_time,
            "trade_id": update.trade_id,
            "commission": update.commission,
            "commission_asset": update.commission_asset,
            "booked_fee": fee,
            "realized_pnl": update.realized_pnl,
            "is_maker": update.is_maker,
            "order_type": order.intent.order_type,
            "time_in_force": order.intent.time_in_force,
            "is_rpi": order.intent.is_rpi,
            "reduce_only": order.intent.reduce_only,
            "pre_status": order.status.value,
        }
        paper_database = getattr(self, "paper_trade_database", None)
        if paper_database is not None:
            payload["paper_run_id"] = paper_database.run_id
            payload["fill_model"] = str(update.fill_model or "unknown")
            payload.update(
                {
                    "fill_trigger": str(update.fill_trigger or ""),
                    "market_trade_id": (
                        int(update.market_trade_id)
                        if update.market_trade_id >= 0
                        else None
                    ),
                    "market_trade_price": update.market_trade_price,
                    "market_trade_qty": update.market_trade_qty,
                    "market_trade_exchange_time": (
                        update.market_trade_exchange_time
                    ),
                    "market_trade_received_time": (
                        update.market_trade_received_time
                    ),
                    "market_trade_clock_offset_ms": (
                        update.market_trade_clock_offset_ms
                    ),
                    "market_trade_transport_latency_ms": (
                        update.market_trade_transport_latency_ms
                    ),
                    "market_trade_local_age_ms": (
                        update.market_trade_local_age_ms
                    ),
                    "queue_ahead_before": update.queue_ahead_before,
                    "best_bid_at_fill": update.best_bid_at_fill,
                    "best_ask_at_fill": update.best_ask_at_fill,
                    "mid_at_fill": update.mid_at_fill,
                    "quote_age_ms": update.quote_age_ms,
                }
            )
        committed_seq = self.audit_logger.audit("execution_record", payload)
        if paper_database is not None:
            metadata = self.journal.commit_metadata(committed_seq) or {}
            paper_database.record_execution(
                committed_seq,
                payload,
                journal_ts=str(metadata.get("ts", "") or ""),
                journal_hash=str(metadata.get("hash", "") or ""),
            )
        self.execution_ids.add(execution_id)
        return True

    def _on_order_truth_check(self, reason: str, suspicious_oid: str = None):
        if not suspicious_oid:
            return
        with self.lock:
            if suspicious_oid in self._order_truth_resolution_inflight:
                return
            self._order_truth_resolution_inflight.add(suspicious_oid)

        handle = self._submit_background_task(
            f"order-truth:{suspicious_oid}",
            self._resolve_order_truth,
            suspicious_oid,
            reason,
            name=f"OrderTruth-{suspicious_oid}",
        )
        if handle is None:
            with self.lock:
                self._order_truth_resolution_inflight.discard(
                    suspicious_oid
                )

    def _resolve_order_truth(self, client_oid: str, reason: str = ""):
        try:
            terminal_status = ""
            with self.lock:
                order = self.orders.get(client_oid)
                if not order:
                    return
                symbol = order.intent.symbol
                if order.is_terminal():
                    terminal_status = order.status.value
                target_id = order.exchange_oid or order.client_oid
                local_status = order.status

            if terminal_status:
                self._unknown_not_found_counts.pop(client_oid, None)
                self._audit(
                    "order_truth_resolved_by_terminal_update",
                    client_oid=client_oid,
                    symbol=symbol,
                    terminal_status=terminal_status,
                    reason=reason,
                )
                self._clear_order_truth_guard(symbol, client_oid)
                return

            remote = self.query_order(symbol, target_id)
            if remote is None:
                self._audit(
                    "order_truth_query_unavailable",
                    client_oid=client_oid,
                    symbol=symbol,
                    reason=reason,
                )
                if local_status in {OrderStatus.SUBMIT_UNKNOWN, OrderStatus.CANCEL_UNKNOWN}:
                    self.order_monitor.on_order_update(client_oid, local_status)
                return

            if remote.get("_query_status") == "NOT_FOUND":
                count = self._unknown_not_found_counts.get(client_oid, 0) + 1
                self._unknown_not_found_counts[client_oid] = count
                with self.lock:
                    order = self.orders.get(client_oid)
                    elapsed = (
                        time.perf_counter() - order.updated_monotonic
                        if order
                        else 0.0
                    )
                    current_status = order.status if order else None

                self._audit(
                    "order_truth_not_found",
                    client_oid=client_oid,
                    symbol=symbol,
                    confirmations=count,
                    elapsed_sec=elapsed,
                    local_status=current_status.value if current_status else "",
                )
                if (
                    current_status == OrderStatus.SUBMIT_UNKNOWN
                    and count >= self.unknown_order_min_not_found
                    and elapsed >= self.unknown_order_resolution_timeout_sec
                ):
                    with self.lock:
                        order = self.orders.get(client_oid)
                        if order and order.status == OrderStatus.SUBMIT_UNKNOWN:
                            order.mark_rejected_locally("exchange_confirmed_order_absent")
                            self._record_order_snapshot(order, "submit_unknown_absent")
                            self._emit_order_update(order)
                            self._write_tombstone(order)
                            self.exposure.update_open_orders(self.orders)
                            self.account.calculate()
                    self._clear_order_truth_guard(symbol, client_oid)
                    return

                if current_status in {OrderStatus.SUBMIT_UNKNOWN, OrderStatus.CANCEL_UNKNOWN}:
                    if (
                        current_status == OrderStatus.CANCEL_UNKNOWN
                        and count >= self.unknown_order_min_not_found
                        and elapsed >= self.unknown_order_resolution_timeout_sec
                    ):
                        self.trigger_reconcile(
                            "Cancel outcome remained unresolvable",
                            suspicious_oid=client_oid,
                        )
                        return
                    self.order_monitor.on_order_update(client_oid, current_status)
                elif current_status in {OrderStatus.CANCELLING, OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}:
                    self.trigger_reconcile("Order absent from targeted query", suspicious_oid=client_oid)
                return

            self._unknown_not_found_counts.pop(client_oid, None)
            if not self._backfill_trade_history(
                symbols={symbol},
                end_time_ms=time_service.now(),
            ):
                self.trigger_reconcile(
                    "Exact trade history unavailable during order truth query",
                    suspicious_oid=client_oid,
                )
                return
            if not self._apply_exchange_order_snapshot(
                remote,
                source="targeted_order_query",
            ):
                self.trigger_reconcile(
                    "Order snapshot is ahead of exact trade history",
                    suspicious_oid=client_oid,
                )
                return

            with self.lock:
                order = self.orders.get(client_oid)
                resolved_status = order.status if order else None

            if (
                local_status in {OrderStatus.CANCEL_UNKNOWN, OrderStatus.CANCELLING}
                and resolved_status in {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}
            ):
                self.cancel_order(client_oid)
            elif resolved_status not in {OrderStatus.SUBMIT_UNKNOWN, OrderStatus.CANCEL_UNKNOWN}:
                self._clear_order_truth_guard(symbol, client_oid)
        except Exception as exc:
            self._audit(
                "order_truth_resolution_failed",
                client_oid=client_oid,
                reason=reason,
                error=str(exc),
            )
            logger.error(f"[OMS] Order truth resolution failed for {client_oid}: {exc}")
        finally:
            with self.lock:
                self._order_truth_resolution_inflight.discard(client_oid)

    def _clear_order_truth_guard(self, symbol: str, client_oid: str = ""):
        symbol = str(symbol or "").upper()
        client_oid = str(client_oid or "")
        with self.lock:
            records = self._ensure_symbol_guard_records_locked(symbol)
            matches = [
                (
                    owner,
                    str(record.get("reason", "") or ""),
                    int(record.get("epoch", 0) or 0),
                )
                for owner, record in records.items()
                if str(record.get("reason", "") or "").startswith("order_truth:")
                and (
                    not client_oid
                    or str(record.get("reason", "") or "").endswith(
                        f":{client_oid}"
                    )
                )
            ]
        for owner, reason, epoch in matches:
            self.clear_symbol_freeze(
                symbol,
                reason="order truth resolved",
                expected_epoch=epoch,
                expected_reason=reason,
                expected_owner=owner,
            )

    def _create_recovered_order(self, remote: dict) -> Order:
        if not isinstance(remote, dict):
            raise ValueError("remote_order:not_mapping")
        symbol = str(remote.get("symbol", "") or "").upper()
        exchange_oid = str(remote.get("orderId", "") or "")
        client_oid = str(remote.get("clientOrderId", "") or "")
        if not symbol:
            raise ValueError("remote_order:missing_symbol")
        if not client_oid and not exchange_oid:
            raise ValueError("remote_order:missing_identifier")
        if not client_oid:
            client_oid = f"EXTERNAL_{symbol}_{exchange_oid}"
        side = Side(str(remote.get("side", "BUY") or "BUY").upper())
        volume = self._finite_truth_float(
            remote.get("origQty", remote.get("executedQty", 0.0))
            or 0.0,
            "remote_order.origQty",
            minimum=0.0,
        )
        if volume <= 0:
            volume = self._finite_truth_float(
                remote.get("qty", 0.0) or 0.0,
                "remote_order.qty",
                minimum=0.0,
            )
        if volume <= 0.0:
            raise ValueError("remote_order:non_positive_volume")
        price = self._finite_truth_float(
            remote.get("price", 0.0)
            or remote.get("avgPrice", 0.0)
            or remote.get("trade_price", 0.0)
            or 0.0,
            "remote_order.price",
            minimum=0.0,
        )
        if price <= 0.0:
            raise ValueError("remote_order:non_positive_price")
        intent = OrderIntent(
            strategy_id="exchange_recovery",
            symbol=symbol,
            side=side,
            price=price,
            volume=volume,
            order_type=str(remote.get("type", "LIMIT") or "LIMIT"),
            time_in_force=str(remote.get("timeInForce", TIF_GTC) or TIF_GTC),
            is_post_only=str(remote.get("timeInForce", "") or "").upper()
            in {TIF_GTX, TIF_RPI},
            reduce_only=bool(remote.get("reduceOnly", False)),
            policy=ExecutionPolicy.PASSIVE,
            tag="exchange_recovered",
        )
        order = Order(client_oid, intent)
        order.mark_submitting()
        order.mark_new(exchange_oid=exchange_oid)
        self.orders[client_oid] = order
        if exchange_oid:
            self.exchange_id_map[exchange_oid] = order
        self._schedule_rpi_calibration_runtime_enforcement(
            terminal_truth_changed=True,
        )
        self._audit(
            "external_order_recovered",
            client_oid=client_oid,
            exchange_oid=exchange_oid,
            symbol=symbol,
        )
        return order

    def _apply_exchange_order_snapshot(self, remote: dict, source: str = "order_query"):
        if not isinstance(remote, dict) or remote.get("_query_status"):
            return False
        status = str(remote.get("status", "") or "").upper()
        if status == "EXPIRED_IN_MATCH":
            status = "EXPIRED"
        if status not in {
            "NEW",
            "PARTIALLY_FILLED",
            "FILLED",
            "CANCELED",
            "EXPIRED",
            "REJECTED",
        }:
            self._schedule_rpi_calibration_runtime_enforcement(
                terminal_truth_changed=True,
            )
            self._audit(
                "unhandled_order_snapshot_status",
                status=status,
                source=source,
            )
            return False
        try:
            executed_qty = self._finite_truth_float(
                remote.get("executedQty", 0.0) or 0.0,
                "remote_order.executedQty",
                minimum=0.0,
            )
            avg_price = self._finite_truth_float(
                remote.get("avgPrice", 0.0)
                or remote.get("price", 0.0)
                or 0.0,
                "remote_order.avgPrice",
                minimum=0.0,
            )
            update_time_ms = self._finite_truth_float(
                remote.get("updateTime")
                or remote.get("time")
                or time_service.now(),
                "remote_order.updateTime",
                minimum=0.0,
            )
        except ValueError as exc:
            self._audit(
                "invalid_order_snapshot",
                source=source,
                detail=str(exc),
                client_oid=str(remote.get("clientOrderId", "") or ""),
                exchange_oid=str(remote.get("orderId", "") or ""),
            )
            return False
        if executed_qty > 0.0 and avg_price <= 0.0:
            self._audit(
                "invalid_order_snapshot",
                source=source,
                detail="remote_order:missing_fill_price",
                client_oid=str(remote.get("clientOrderId", "") or ""),
                exchange_oid=str(remote.get("orderId", "") or ""),
            )
            return False

        client_oid = str(remote.get("clientOrderId", "") or "")
        exchange_oid = str(remote.get("orderId", "") or "")
        with self.lock:
            order = self.orders.get(client_oid) if client_oid else None
            if not order and exchange_oid:
                order = self.exchange_id_map.get(exchange_oid)
            if not order:
                order = self._create_recovered_order(remote)

        remote_order_type = str(remote.get("type", "") or "").upper()
        remote_time_in_force = str(remote.get("timeInForce", "") or "").upper()
        if executed_qty > order.filled_volume + 1e-9:
            self._schedule_rpi_calibration_runtime_enforcement(
                terminal_truth_changed=True,
            )
            truth_reason = (
                f"execution_truth:{order.intent.symbol}:{order.client_oid}:"
                "order_snapshot_ahead"
            )
            self._audit(
                "order_snapshot_trade_truth_missing",
                client_oid=order.client_oid,
                exchange_oid=exchange_oid,
                local_filled=order.filled_volume,
                exchange_filled=executed_qty,
                source=source,
            )
            self.freeze_system(
                truth_reason,
                cancel_active_orders=False,
            )
            return False

        update = ExchangeOrderUpdate(
            client_oid=order.client_oid,
            exchange_oid=exchange_oid or order.exchange_oid,
            symbol=order.intent.symbol,
            status=status,
            filled_qty=max(0.0, executed_qty - order.filled_volume),
            filled_price=avg_price,
            cum_filled_qty=executed_qty,
            update_time=update_time_ms / 1000.0,
            seq=0,
            order_type=remote_order_type,
            time_in_force=remote_time_in_force,
        )
        self._apply_event(Event(EVENT_EXCHANGE_ORDER_UPDATE, update))
        self._audit(
            "order_snapshot_applied",
            client_oid=order.client_oid,
            exchange_oid=update.exchange_oid,
            exchange_status=status,
            source=source,
        )
        return True

    def _advance_trade_cursor(self, symbol: str, trade_id: int, trade_time: float, source: str):
        symbol = str(symbol or "").upper()
        trade_id = int(trade_id)
        if trade_id < 0 or str(source or "") == "user_stream":
            return False
        with self.lock:
            current = int(self.trade_cursors.get(symbol, -1))
            if trade_id <= current:
                return False
            self.audit_logger.audit(
                "trade_cursor_advanced",
                {
                    "symbol": symbol,
                    "trade_id": trade_id,
                    "trade_time": trade_time,
                    "source": source,
                },
            )
            self.trade_cursors[symbol] = trade_id
            discard_cursor_covered_execution_ids(
                self.execution_ids,
                symbol=symbol,
                trade_id=trade_id,
            )
        return True

    def _apply_exchange_trade(self, trade: dict) -> bool:
        if not isinstance(trade, dict):
            return False
        symbol = str(trade.get("symbol", "") or "").upper()
        try:
            trade_id = int(trade.get("id", -1))
        except (TypeError, ValueError):
            return False
        if not symbol or trade_id < 0:
            return False
        if trade_id <= int(self.trade_cursors.get(symbol, -1)):
            return True
        try:
            qty = self._finite_truth_float(
                trade.get("qty", 0.0) or 0.0,
                "trade.qty",
                minimum=0.0,
            )
            price = self._finite_truth_float(
                trade.get("price", 0.0) or 0.0,
                "trade.price",
                minimum=0.0,
            )
            trade_time_ms = self._finite_truth_float(
                trade.get("time", time_service.now())
                or time_service.now(),
                "trade.time",
                minimum=0.0,
            )
            commission = self._finite_truth_float(
                trade.get("commission", 0.0) or 0.0,
                "trade.commission",
            )
            realized_pnl = self._finite_truth_float(
                trade.get("realizedPnl", 0.0) or 0.0,
                "trade.realizedPnl",
            )
        except ValueError:
            return False
        if qty <= 0.0 or price <= 0.0:
            return False

        exchange_oid = str(trade.get("orderId", "") or "")
        with self.lock:
            order = self.exchange_id_map.get(exchange_oid)
        if not order:
            remote = self.query_order(symbol, exchange_oid)
            if not remote or remote.get("_query_status") == "NOT_FOUND":
                return False
            remote = dict(remote)
            remote["trade_price"] = trade.get("price", 0.0)
            with self.lock:
                order = self._create_recovered_order(remote)

        venue = str(
            getattr(self.gateway, "gateway_name", "UNKNOWN") or "UNKNOWN"
        ).upper()
        execution_id = f"{venue}:{symbol}:{trade_id}"
        with self.lock:
            if execution_id in self.execution_ids:
                self.rest_confirmed_execution_ids.add(execution_id)
                self._advance_trade_cursor(
                    symbol,
                    trade_id,
                    trade_time_ms / 1000.0,
                    source="rest_confirmed_duplicate",
                )
                self._audit(
                    "trade_backfill_duplicate_confirmed",
                    symbol=symbol,
                    trade_id=trade_id,
                    execution_id=execution_id,
                )
                return True

        original_terminal_status = order.status if order.is_terminal() else None
        original_terminal_time = order.last_exchange_update_time

        cumulative = min(order.intent.volume, order.filled_volume + qty)
        status = "FILLED" if cumulative >= order.intent.volume - 1e-8 else "PARTIALLY_FILLED"
        update = ExchangeOrderUpdate(
            client_oid=order.client_oid,
            exchange_oid=order.exchange_oid or exchange_oid,
            symbol=symbol,
            status=status,
            filled_qty=qty,
            filled_price=price,
            cum_filled_qty=cumulative,
            update_time=trade_time_ms / 1000.0,
            seq=0,
            commission=commission,
            commission_asset=str(trade.get("commissionAsset", "") or ""),
            realized_pnl=realized_pnl,
            is_maker=bool(trade.get("maker")) if "maker" in trade else None,
            trade_id=trade_id,
            fill_model=str(trade.get("_fillModel", "") or ""),
        )
        self._apply_event(Event(EVENT_EXCHANGE_ORDER_UPDATE, update))
        if order.filled_volume + 1e-9 < cumulative:
            self._audit(
                "trade_backfill_apply_failed",
                symbol=symbol,
                trade_id=trade_id,
                client_oid=order.client_oid,
                expected_cumulative=cumulative,
                actual_cumulative=order.filled_volume,
            )
            return False
        if (
            original_terminal_status in {OrderStatus.CANCELLED, OrderStatus.EXPIRED}
            and order.status == OrderStatus.PARTIALLY_FILLED
        ):
            with self.lock:
                if original_terminal_status == OrderStatus.CANCELLED:
                    order.mark_cancelled(
                        update_time=max(original_terminal_time, update.update_time),
                        exchange_status="CANCELED",
                    )
                else:
                    order.mark_expired(
                        update_time=max(original_terminal_time, update.update_time),
                    )
                self.order_monitor.on_order_update(order.client_oid, order.status)
                self.exposure.update_open_orders(self.orders)
                self.account.calculate()
                self._record_order_snapshot(order, "terminal_restored_after_trade_backfill")
                self._emit_order_update(order)
        with self.lock:
            self.rest_confirmed_execution_ids.add(execution_id)
        self._advance_trade_cursor(symbol, trade_id, update.update_time, source="rest_backfill")
        return True

    def _backfill_trade_history(self, symbols=None, end_time_ms: int = None) -> bool:
        query = getattr(self.gateway, "get_user_trades", None)
        if not callable(query):
            return True
        symbols = set(symbols or self.config.get("symbols", []))
        end_time_ms = int(end_time_ms or time_service.now())
        limit = 1000

        for symbol in sorted(symbols):
            cursor = int(self.trade_cursors.get(symbol, -1))
            request_from_id = (
                max(0, cursor - self.trade_recovery_id_overlap + 1)
                if cursor >= 0
                else None
            )
            page_count = 0
            trades = []
            while page_count < 20:
                page_count += 1
                if request_from_id is not None:
                    trades = self.query_user_trades(
                        symbol,
                        from_id=request_from_id,
                        limit=limit,
                    )
                else:
                    prior_scan = int(self.trade_scan_end_ms.get(symbol, 0))
                    start_time = max(
                        0,
                        (prior_scan or end_time_ms - self.trade_recovery_lookback_ms)
                        - self.trade_recovery_overlap_ms,
                    )
                    trades = self.query_user_trades(
                        symbol,
                        start_time=start_time,
                        end_time=end_time_ms,
                        limit=limit,
                    )
                if trades is None:
                    return False
                if not isinstance(trades, (list, tuple)):
                    return False
                normalized_trades = []
                for trade in trades:
                    if not isinstance(trade, dict):
                        return False
                    try:
                        trade_id = int(trade.get("id", -1))
                    except (TypeError, ValueError):
                        return False
                    trade_symbol = str(
                        trade.get("symbol", symbol) or symbol
                    ).upper()
                    if trade_id < 0 or trade_symbol != str(symbol).upper():
                        return False
                    normalized_trades.append(trade)
                trades = sorted(
                    normalized_trades,
                    key=lambda trade: int(trade["id"]),
                )
                for trade in trades:
                    if not self._apply_exchange_trade(trade):
                        return False
                    cursor = max(cursor, int(trade.get("id", -1)))
                if len(trades) < limit:
                    break
                page_max_id = max(
                    int(trade.get("id", -1))
                    for trade in trades
                )
                if request_from_id is not None and page_max_id < request_from_id:
                    return False
                request_from_id = page_max_id + 1
            if page_count >= 20 and len(trades) >= limit:
                return False
            self.trade_scan_end_ms[symbol] = end_time_ms
            self.audit_logger.audit(
                "trade_scan_completed",
                {
                    "symbol": symbol,
                    "end_time_ms": end_time_ms,
                    "cursor": int(self.trade_cursors.get(symbol, -1)),
                },
            )
        return True

    def _schedule_trade_tail_verification(
        self,
        symbol: str,
        trade_id: int | None = None,
        reason: str = "execution_update",
    ) -> bool:
        if not callable(getattr(self.gateway, "get_user_trades", None)):
            return True
        symbol = str(symbol or "").upper()
        if not symbol:
            return False
        venue = str(
            getattr(self.gateway, "gateway_name", "UNKNOWN") or "UNKNOWN"
        ).upper()
        execution_id = (
            f"{venue}:{symbol}:{int(trade_id)}"
            if trade_id is not None and int(trade_id) >= 0
            else ""
        )
        with self.lock:
            if self._shutdown_requested or self._stopped:
                return False
            if execution_id:
                self.trade_tail_expected_ids.setdefault(symbol, set()).add(
                    execution_id
                )
            if symbol in self.trade_tail_verification_inflight:
                return True
            self.trade_tail_verification_inflight.add(symbol)

        def verify_tail():
            cleaned = False
            try:
                last_ok = False
                pending = set()
                for attempt in range(1, self.trade_tail_verification_attempts + 1):
                    if self._shutdown_requested or self._stopped:
                        return
                    last_ok = self._backfill_trade_history(
                        symbols={symbol},
                        end_time_ms=time_service.now(),
                    )
                    with self.lock:
                        expected = set(
                            self.trade_tail_expected_ids.get(symbol, set())
                        )
                        pending = (
                            expected - self.rest_confirmed_execution_ids
                        )
                    if last_ok and not pending:
                        with self.lock:
                            expected = set(
                                self.trade_tail_expected_ids.get(
                                    symbol,
                                    set(),
                                )
                            )
                            pending = (
                                expected - self.rest_confirmed_execution_ids
                            )
                            if not pending:
                                self.trade_tail_verification_inflight.discard(
                                    symbol
                                )
                                self.trade_tail_expected_ids.pop(symbol, None)
                                self.rest_confirmed_execution_ids.difference_update(
                                    expected
                                )
                                cleaned = True
                        if not pending:
                            return
                    if attempt < self.trade_tail_verification_attempts:
                        time.sleep(self.trade_tail_verification_retry_sec)

                self._audit(
                    "trade_tail_verification_failed",
                    symbol=symbol,
                    reason=reason,
                    pending_execution_ids=sorted(pending),
                    rest_query_ok=last_ok,
                )
                self.trigger_reconcile(
                    f"Trade truth verification failed for {symbol}: {reason}"
                )
            finally:
                if not cleaned:
                    with self.lock:
                        self.trade_tail_verification_inflight.discard(symbol)
                        self.trade_tail_expected_ids.pop(symbol, None)

        handle = self._submit_background_task(
            f"trade-tail:{symbol}",
            verify_tail,
            name=f"TradeTruth-{symbol}",
            delay_sec=self.trade_tail_verification_delay_sec,
        )
        if handle is None:
            with self.lock:
                self.trade_tail_verification_inflight.discard(symbol)
            return False
        return True

    def _prime_trade_history_baseline(self, end_time_ms: int, symbols=None) -> bool:
        query = getattr(self.gateway, "get_user_trades", None)
        if not callable(query):
            return True
        start_time = max(0, int(end_time_ms) - self.trade_recovery_lookback_ms)
        symbols = set(symbols or self.config.get("symbols", []))
        for symbol in sorted(symbols):
            trades = self.query_user_trades(
                symbol,
                start_time=start_time,
                end_time=int(end_time_ms),
                limit=1000,
            )
            if trades is None:
                return False
            if not isinstance(trades, (list, tuple)):
                return False
            valid_ids = []
            for trade in trades:
                if not isinstance(trade, dict):
                    return False
                try:
                    trade_id = int(trade.get("id", -1))
                except (TypeError, ValueError):
                    return False
                trade_symbol = str(
                    trade.get("symbol", symbol) or symbol
                ).upper()
                if trade_id < 0 or trade_symbol != str(symbol).upper():
                    return False
                valid_ids.append(trade_id)
            if valid_ids:
                self._advance_trade_cursor(
                    symbol,
                    max(valid_ids),
                    float(end_time_ms) / 1000.0,
                    source="bootstrap_baseline",
                )
            self.trade_scan_end_ms[symbol] = int(end_time_ms)
            self.audit_logger.audit(
                "trade_scan_completed",
                {
                    "symbol": symbol,
                    "end_time_ms": int(end_time_ms),
                    "cursor": int(self.trade_cursors.get(symbol, -1)),
                    "source": "bootstrap_baseline",
                },
            )
        return True

    @staticmethod
    def _utc_day_start_ms(now_ms: int = None) -> int:
        now = datetime.fromtimestamp(
            float(now_ms or time_service.now()) / 1000.0,
            tz=timezone.utc,
        )
        return int(
            now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            * 1000
        )

    def _external_cash_flow_income_id(self, income: dict) -> str:
        income_type = str(
            income.get("incomeType", income.get("income_type", "")) or ""
        ).upper()
        transaction_id = income.get("tranId", income.get("trandId"))
        if transaction_id not in (None, ""):
            return f"{income_type}:{transaction_id}"
        fingerprint = "|".join(
            str(income.get(key, "") or "")
            for key in ("time", "asset", "income", "symbol", "info")
        )
        digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        return f"{income_type}:hash:{digest}"

    def _apply_external_cash_flow_rows(self, rows, source: str) -> int:
        if not isinstance(rows, (list, tuple)):
            raise ValueError("income_history_not_a_list")

        normalized_rows = sorted(
            (row for row in rows if isinstance(row, dict)),
            key=lambda row: (
                int(row.get("time", 0) or 0),
                str(row.get("incomeType", row.get("income_type", "")) or ""),
                self._external_cash_flow_income_id(row),
            ),
        )
        applied = 0
        with self.lock:
            for income in normalized_rows:
                income_type = str(
                    income.get("incomeType", income.get("income_type", "")) or ""
                ).upper()
                if income_type not in self.external_income_types:
                    continue
                asset = str(income.get("asset", "") or "").upper()
                if asset not in self.external_cash_flow_assets:
                    raise ValueError(f"unsupported_external_cash_flow_asset:{asset}")
                try:
                    amount = float(income.get("income", 0.0) or 0.0)
                except (TypeError, ValueError) as exc:
                    raise ValueError("invalid_external_cash_flow_amount") from exc
                if not math.isfinite(amount):
                    raise ValueError("non_finite_external_cash_flow_amount")

                income_id = self._external_cash_flow_income_id(income)
                if income_id in self.external_cash_flow_ids:
                    continue
                self.audit_logger.audit(
                    "external_cash_flow_record",
                    {
                        "income_id": income_id,
                        "income_type": income_type,
                        "asset": asset,
                        "amount": amount,
                        "income_time_ms": int(income.get("time", 0) or 0),
                        "symbol": str(income.get("symbol", "") or ""),
                        "source": source,
                    },
                )
                self.external_cash_flow_ids.add(income_id)
                self.account.external_cash_flow_total += amount
                applied += 1
        return applied

    def mark_external_cash_flow_truth_unavailable(self, reason: str = ""):
        if not self.external_cash_flow_truth_enabled:
            return
        with self.lock:
            self.account.mark_external_cash_flow_truth_unavailable()
        self._audit(
            "external_cash_flow_truth_unavailable",
            reason=reason or "income_history_unavailable",
        )

    def backfill_external_cash_flow_history(
        self,
        query=None,
        end_time_ms: int = None,
        source: str = "rest_income_history",
    ) -> bool:
        if not self.external_cash_flow_truth_enabled:
            return True
        query = query or self.query_income_history
        if not callable(query):
            self.mark_external_cash_flow_truth_unavailable("income_query_unavailable")
            return False

        end_time_ms = int(end_time_ms or time_service.now())
        day_start_ms = self._utc_day_start_ms(end_time_ms)
        if self.external_cash_flow_scan_end_ms:
            start_time_ms = max(
                day_start_ms,
                self.external_cash_flow_scan_end_ms
                - self.external_cash_flow_recovery_overlap_ms,
            )
        else:
            start_time_ms = max(
                day_start_ms,
                end_time_ms - self.external_cash_flow_recovery_lookback_ms,
            )

        limit = 1000
        page = 1
        last_page_full = False
        total_rows = 0
        try:
            while page <= self.external_cash_flow_max_pages:
                rows = query(
                    start_time=start_time_ms,
                    end_time=end_time_ms,
                    page=page,
                    limit=limit,
                )
                if rows is None:
                    self.mark_external_cash_flow_truth_unavailable(
                        "income_history_request_failed"
                    )
                    return False
                if not isinstance(rows, list):
                    raise ValueError("income_history_not_a_list")
                total_rows += len(rows)
                self._apply_external_cash_flow_rows(rows, source=source)
                last_page_full = len(rows) >= limit
                if not last_page_full:
                    break
                page += 1

            if last_page_full and page > self.external_cash_flow_max_pages:
                self.mark_external_cash_flow_truth_unavailable(
                    "income_history_page_limit_exceeded"
                )
                return False

            with self.lock:
                self.audit_logger.audit(
                    "cash_flow_scan_completed",
                    {
                        "start_time_ms": start_time_ms,
                        "end_time_ms": end_time_ms,
                        "rows": total_rows,
                        "source": source,
                    },
                )
                self.external_cash_flow_scan_end_ms = end_time_ms
                self.account.sync_external_cash_flow_truth(
                    self.account.external_cash_flow_total,
                    snapshot_time=time.time(),
                    snapshot_monotonic=time.perf_counter(),
                )
            self._audit(
                "external_cash_flow_truth_synced",
                rows=total_rows,
                total=self.account.external_cash_flow_total,
                start_time_ms=start_time_ms,
                end_time_ms=end_time_ms,
                source=source,
            )
            return True
        except (JournalError, ValueError, TypeError) as exc:
            self.mark_external_cash_flow_truth_unavailable(str(exc))
            fail_closed = getattr(self, "_fail_closed_on_journal_error", None)
            if isinstance(exc, JournalError) and callable(fail_closed):
                fail_closed(exc, "external_cash_flow_history")
            return False

    def poll_external_cash_flow_truth(self, query=None, now: float = None) -> bool:
        if not self.external_cash_flow_truth_enabled:
            return True
        now = time.perf_counter() if now is None else float(now)
        if (
            now - self.last_external_cash_flow_poll_at
            < self.external_cash_flow_poll_interval_sec
        ):
            return bool(self.account.cash_flow_snapshot_synced)
        self.last_external_cash_flow_poll_at = now
        return self.backfill_external_cash_flow_history(
            query=query,
            end_time_ms=int(time.time() * 1000),
            source="live_loop_income_history",
        )

    def _refresh_missing_local_order_terminals(self, remote_orders) -> bool:
        query = getattr(self.gateway, "get_order", None)
        if not callable(query):
            return True
        remote_identifiers = {
            identifier
            for item in self._normalize_remote_open_orders(remote_orders)
            for identifier in item["identifiers"]
        }
        with self.lock:
            missing = [
                (order.client_oid, order.intent.symbol, order.exchange_oid or order.client_oid)
                for order in self.orders.values()
                if order.is_active()
                and order.client_oid not in remote_identifiers
                and (not order.exchange_oid or order.exchange_oid not in remote_identifiers)
            ]

        for client_oid, symbol, target_id in missing:
            remote = self.query_order(symbol, target_id)
            if remote is None:
                return False
            if remote.get("_query_status") == "NOT_FOUND":
                self._audit(
                    "active_order_missing_from_exchange_history",
                    client_oid=client_oid,
                    symbol=symbol,
                )
                continue
            if not self._apply_exchange_order_snapshot(
                remote,
                source="reconcile_missing_terminal",
            ):
                return False
        return True
