"""Single-writer ledger mutations and venue events for Binance Paper."""

from __future__ import annotations

import time
from collections import deque
from typing import Protocol

from event.type import ExchangeAccountUpdate, ExchangeOrderUpdate, OrderBook
from infrastructure.logger import logger

from .paper_state import (
    PaperOrder,
    PaperPosition,
    TERMINAL_ORDER_STATUSES,
)


class PaperLedgerOwner(Protocol):
    """Gateway state and callbacks required by the Paper ledger."""

    _orders: dict[str, PaperOrder]
    _exchange_to_client: dict[str, str]
    _positions: dict[str, PaperPosition]
    _books: dict[str, OrderBook]
    _balances: dict[str, float]
    _trades: deque[dict]
    _event_sequence: int
    _paper_trade_sequence: int
    _worker_running: bool
    balance_asset: str
    symbols: list[str]
    max_order_history: int

    def _reduce_only_fill_cap(self, order: PaperOrder): ...

    def _fee_rate(self, order: PaperOrder, is_maker: bool): ...

    def _apply_position_fill(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
    ): ...

    def _quote_asset(self, symbol: str): ...

    def _mark_price(self, symbol: str): ...

    def _emit_order_event(self, order: PaperOrder, **kwargs): ...

    def _emit_account_update(self, symbol: str, **kwargs): ...

    def _remove_from_later_local_queue(
        self,
        order: PaperOrder,
        removed_quantity: float,
    ): ...

    def _account_metrics(self): ...

    def _submit_worker(self, kind: str, payload) -> bool: ...

    def on_order_update(self, update: ExchangeOrderUpdate): ...

    def on_account_update(self, update: ExchangeAccountUpdate): ...


class PaperLedger:
    """Apply fills atomically on the Paper matching worker."""

    __slots__ = ("_owner",)

    def __init__(self, owner: PaperLedgerOwner):
        self._owner = owner

    def apply_fill(
        self,
        order: PaperOrder,
        quantity: float,
        price: float,
        *,
        is_maker: bool,
        fill_context: dict | None = None,
    ):
        owner = self._owner
        quantity = min(
            float(quantity),
            order.remaining,
            owner._reduce_only_fill_cap(order),
        )
        if quantity <= 1e-12:
            return False
        transaction_time = time.time()
        context = dict(fill_context) if isinstance(fill_context, dict) else {}
        book = owner._books.get(order.request.symbol)
        best_bid_at_fill = None
        best_ask_at_fill = None
        if book is not None:
            best_bid_at_fill = float(book.get_best_bid()[0] or 0.0) or None
            best_ask_at_fill = float(book.get_best_ask()[0] or 0.0) or None
        mid_at_fill = (
            (best_bid_at_fill + best_ask_at_fill) / 2.0
            if best_bid_at_fill is not None
            and best_ask_at_fill is not None
            else None
        )
        quote_age_ms = max(
            0.0,
            (time.perf_counter() - order.created_monotonic) * 1000.0,
        )
        realized_pnl = owner._apply_position_fill(
            order.request.symbol,
            order.request.side,
            quantity,
            float(price),
        )
        fee_rate = owner._fee_rate(order, is_maker)
        commission = quantity * float(price) * fee_rate
        quote_asset = owner._quote_asset(order.request.symbol)
        owner._balances.setdefault(quote_asset, 0.0)
        owner._balances[quote_asset] += realized_pnl - commission

        order.cum_filled_qty += quantity
        order.cumulative_cost += quantity * float(price)
        order.avg_price = order.cumulative_cost / order.cum_filled_qty
        order.update_ms = int(transaction_time * 1000)
        order.status = (
            "FILLED" if order.remaining <= 1e-9 else "PARTIALLY_FILLED"
        )

        owner._paper_trade_sequence += 1
        paper_trade_id = owner._paper_trade_sequence
        trade_payload = {
            "symbol": order.request.symbol,
            "id": paper_trade_id,
            "orderId": order.exchange_oid,
            "side": order.request.side,
            "price": f"{float(price):.12g}",
            "qty": f"{quantity:.12g}",
            "realizedPnl": f"{realized_pnl:.12g}",
            "commission": f"{commission:.12g}",
            "commissionAsset": quote_asset,
            "time": order.update_ms,
            "maker": bool(is_maker),
            "buyer": order.request.side == "BUY",
            "_simulated": True,
            "_fillModel": order.fill_model,
        }
        owner._trades.append(trade_payload)
        owner._emit_order_event(
            order,
            status=order.status,
            filled_qty=quantity,
            filled_price=float(price),
            transaction_time=transaction_time,
            commission=commission,
            commission_asset=quote_asset,
            realized_pnl=realized_pnl,
            is_maker=is_maker,
            trade_id=paper_trade_id,
            fill_trigger=str(
                context.get(
                    "fill_trigger",
                    "orderbook" if not is_maker else "",
                )
                or ""
            ),
            market_trade_id=int(
                context.get("market_trade_id", -1) or -1
            ),
            market_trade_price=context.get("market_trade_price"),
            market_trade_qty=context.get("market_trade_qty"),
            market_trade_exchange_time=context.get(
                "market_trade_exchange_time"
            ),
            market_trade_received_time=context.get(
                "market_trade_received_time"
            ),
            market_trade_clock_offset_ms=context.get(
                "market_trade_clock_offset_ms"
            ),
            market_trade_transport_latency_ms=context.get(
                "market_trade_transport_latency_ms"
            ),
            market_trade_local_age_ms=context.get(
                "market_trade_local_age_ms"
            ),
            queue_ahead_before=context.get("queue_ahead_before"),
            best_bid_at_fill=best_bid_at_fill,
            best_ask_at_fill=best_ask_at_fill,
            mid_at_fill=mid_at_fill,
            quote_age_ms=quote_age_ms,
        )
        owner._emit_account_update(
            order.request.symbol,
            transaction_time=transaction_time,
            reason="ORDER",
        )
        return True

    def apply_position_fill(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
    ) -> float:
        owner = self._owner
        position = owner._positions.setdefault(symbol, PaperPosition())
        current = float(position.quantity)
        average = float(position.entry_price)
        signed_quantity = quantity if side == "BUY" else -quantity
        next_quantity = current + signed_quantity
        realized = 0.0

        increasing = (
            abs(current) <= 1e-12
            or (current > 0.0 and signed_quantity > 0.0)
            or (current < 0.0 and signed_quantity < 0.0)
        )
        if increasing:
            total_quantity = abs(current) + quantity
            position.entry_price = (
                (abs(current) * average + quantity * price) / total_quantity
                if total_quantity > 0.0
                else 0.0
            )
        else:
            closing_quantity = min(abs(current), quantity)
            if current > 0.0:
                realized = (price - average) * closing_quantity
            else:
                realized = (average - price) * closing_quantity

        position.quantity = next_quantity
        if abs(next_quantity) <= 1e-9:
            position.quantity = 0.0
            position.entry_price = 0.0
        elif current > 0.0 > next_quantity or current < 0.0 < next_quantity:
            position.entry_price = price
        return realized

    def expire_order(self, order: PaperOrder, reason: str):
        owner = self._owner
        if not order.active:
            return False
        removed = order.remaining
        order.status = "EXPIRED"
        order.update_ms = int(time.time() * 1000)
        owner._remove_from_later_local_queue(order, removed)
        owner._emit_order_event(
            order,
            status="EXPIRED",
            transaction_time=order.update_ms / 1000.0,
        )
        logger.info(f"[BINANCE_PAPER] Expired {order.client_oid}: {reason}")
        return True

    def cancel_order(self, order: PaperOrder, reason: str):
        owner = self._owner
        if not order.active:
            return False
        removed = order.remaining
        order.status = "CANCELED"
        order.update_ms = int(time.time() * 1000)
        owner._remove_from_later_local_queue(order, removed)
        owner._emit_order_event(
            order,
            status="CANCELED",
            transaction_time=order.update_ms / 1000.0,
        )
        logger.info(f"[BINANCE_PAPER] Canceled {order.client_oid}: {reason}")
        return True

    def emit_order_event(
        self,
        order: PaperOrder,
        *,
        status: str,
        transaction_time: float,
        filled_qty: float = 0.0,
        filled_price: float = 0.0,
        commission: float | None = None,
        commission_asset: str = "",
        realized_pnl: float | None = None,
        is_maker: bool | None = None,
        trade_id: int = -1,
        fill_trigger: str = "",
        market_trade_id: int = -1,
        market_trade_price: float | None = None,
        market_trade_qty: float | None = None,
        market_trade_exchange_time: float | None = None,
        market_trade_received_time: float | None = None,
        market_trade_clock_offset_ms: float | None = None,
        market_trade_transport_latency_ms: float | None = None,
        market_trade_local_age_ms: float | None = None,
        queue_ahead_before: float | None = None,
        best_bid_at_fill: float | None = None,
        best_ask_at_fill: float | None = None,
        mid_at_fill: float | None = None,
        quote_age_ms: float | None = None,
    ):
        owner = self._owner
        owner._event_sequence += 1
        owner.on_order_update(
            ExchangeOrderUpdate(
                client_oid=order.client_oid,
                exchange_oid=order.exchange_oid,
                symbol=order.request.symbol,
                status=status,
                filled_qty=float(filled_qty),
                filled_price=float(filled_price),
                cum_filled_qty=float(order.cum_filled_qty),
                update_time=float(transaction_time),
                seq=owner._event_sequence,
                commission=commission,
                commission_asset=commission_asset,
                realized_pnl=realized_pnl,
                is_maker=is_maker,
                trade_id=int(trade_id),
                order_type=order.request.order_type,
                time_in_force=order.request.time_in_force,
                fill_model=order.fill_model,
                fill_trigger=fill_trigger,
                market_trade_id=market_trade_id,
                market_trade_price=market_trade_price,
                market_trade_qty=market_trade_qty,
                market_trade_exchange_time=market_trade_exchange_time,
                market_trade_received_time=market_trade_received_time,
                market_trade_clock_offset_ms=market_trade_clock_offset_ms,
                market_trade_transport_latency_ms=(
                    market_trade_transport_latency_ms
                ),
                market_trade_local_age_ms=market_trade_local_age_ms,
                queue_ahead_before=queue_ahead_before,
                best_bid_at_fill=best_bid_at_fill,
                best_ask_at_fill=best_ask_at_fill,
                mid_at_fill=mid_at_fill,
                quote_age_ms=quote_age_ms,
            )
        )

    def emit_account_update(
        self,
        symbol: str,
        *,
        transaction_time: float,
        reason: str,
    ):
        owner = self._owner
        metrics = owner._account_metrics()
        balances = {
            asset: {
                "wallet_balance": float(wallet),
                "available_balance": float(
                    metrics["available_by_asset"].get(asset, wallet)
                ),
            }
            for asset, wallet in owner._balances.items()
        }
        position = owner._positions.get(symbol, PaperPosition())
        mark = owner._mark_price(symbol)
        unrealized = (
            (mark - position.entry_price) * position.quantity
            if mark > 0.0 and abs(position.quantity) > 1e-12
            else 0.0
        )
        owner.on_account_update(
            ExchangeAccountUpdate(
                asset=owner.balance_asset,
                wallet_balance=float(metrics["wallet_balance"]),
                available_balance=float(metrics["available_balance"]),
                balances=balances,
                positions={
                    symbol: {
                        "volume": float(position.quantity),
                        "entry_price": float(position.entry_price),
                        "unrealized_pnl": float(unrealized),
                    }
                },
                reason=reason,
                event_time=float(transaction_time),
            )
        )

    def emit_full_account_update(self, reason: str):
        owner = self._owner
        if not owner._worker_running:
            return False
        return owner._submit_worker("emit_full_account", reason)

    def emit_full_account_update_internal(self, reason: str):
        owner = self._owner
        metrics = owner._account_metrics()
        balances = {
            asset: {
                "wallet_balance": float(wallet),
                "available_balance": float(
                    metrics["available_by_asset"].get(asset, wallet)
                ),
            }
            for asset, wallet in owner._balances.items()
        }
        positions = {}
        for symbol in owner.symbols:
            position = owner._positions.get(symbol, PaperPosition())
            mark = owner._mark_price(symbol)
            positions[symbol] = {
                "volume": float(position.quantity),
                "entry_price": float(position.entry_price),
                "unrealized_pnl": float(
                    (mark - position.entry_price) * position.quantity
                    if mark > 0.0 and abs(position.quantity) > 1e-12
                    else 0.0
                ),
            }
        owner.on_account_update(
            ExchangeAccountUpdate(
                asset=owner.balance_asset,
                wallet_balance=float(metrics["wallet_balance"]),
                available_balance=float(metrics["available_balance"]),
                balances=balances,
                positions=positions,
                reason=reason,
                event_time=time.time(),
            )
        )
        return True

    def prune_terminal_orders(self):
        owner = self._owner
        excess = len(owner._orders) - owner.max_order_history
        if excess <= 0:
            return
        removable = sorted(
            (
                order
                for order in owner._orders.values()
                if order.status in TERMINAL_ORDER_STATUSES
            ),
            key=lambda order: (order.update_ms, order.accept_seq),
        )
        for order in removable[:excess]:
            owner._orders.pop(order.client_oid, None)
            owner._exchange_to_client.pop(order.exchange_oid, None)
