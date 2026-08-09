"""Matching and queue policy for the single-threaded Binance Paper venue."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Callable, Mapping

from event.type import (
    AggTradeData,
    OrderBook,
    OrderRequest,
    TIF_FOK,
    TIF_IOC,
    TIF_RPI,
)
from infrastructure.commission_truth import resolve_passive_fee_rate

from .paper_state import PaperOrder, PaperPosition


@dataclass(slots=True)
class PaperVenueState:
    """Mutable state with one owner: the Paper venue worker."""

    balances: dict[str, float] = field(default_factory=dict)
    orders: dict[str, PaperOrder] = field(default_factory=dict)
    exchange_to_client: dict[str, str] = field(default_factory=dict)
    positions: dict[str, PaperPosition] = field(default_factory=dict)
    books: dict[str, OrderBook] = field(default_factory=dict)
    liquidity: dict[str, dict[str, dict[float, float]]] = field(
        default_factory=dict
    )
    marks: dict[str, float] = field(default_factory=dict)
    last_market_trade_id: dict[str, int] = field(default_factory=dict)
    trades: deque[dict] = field(default_factory=deque)
    dms_deadlines: dict[str, float] = field(default_factory=dict)
    cancel_generations: dict[str, int] = field(default_factory=dict)
    accept_sequence: int = 0
    exchange_sequence: int = 0
    event_sequence: int = 0
    paper_trade_sequence: int = 0


@dataclass(frozen=True, slots=True)
class PaperMatchingPolicy:
    """Immutable policy input for deterministic matching decisions."""

    rpi_fill_model: str
    cancel_ahead_fraction: float
    market_order_max_slippage_bps: float
    maker_fee: float
    taker_fee: float
    rpi_commission_rate: float
    rpi_commission_rates: Mapping[str, float]

    def __post_init__(self):
        object.__setattr__(
            self,
            "rpi_commission_rates",
            MappingProxyType(dict(self.rpi_commission_rates)),
        )


@dataclass(frozen=True, slots=True)
class PaperMatchingPort:
    """Only venue effects matching is allowed to request."""

    apply_fill: Callable[..., object]
    expire_order: Callable[[PaperOrder, str], object]


class PaperMatchingEngine:
    """Select simulated fills without owning balances or event publication."""

    __slots__ = ("_policy", "_port", "_state")

    def __init__(
        self,
        state: PaperVenueState,
        port: PaperMatchingPort,
        policy: PaperMatchingPolicy,
    ):
        self._state = state
        self._port = port
        self._policy = policy

    @property
    def state(self) -> PaperVenueState:
        return self._state

    def match_immediate(self, order: PaperOrder):
        state = self._state
        if not order.active or not order.committed:
            return
        request = order.request
        liquidity = state.liquidity.get(request.symbol)
        if not liquidity:
            if request.order_type == "MARKET" or request.time_in_force in {
                TIF_IOC,
                TIF_FOK,
            }:
                self._port.expire_order(order, "PAPER_NO_LIQUIDITY")
            else:
                self.insert_into_local_queue(order)
            return

        side_key = "asks" if request.side == "BUY" else "bids"
        levels = liquidity[side_key]
        prices = (
            sorted(levels)
            if request.side == "BUY"
            else sorted(levels, reverse=True)
        )
        eligible_prices = [
            price
            for price in prices
            if self.price_is_executable(order, price)
        ]

        if request.time_in_force == TIF_FOK:
            known_quantity = sum(
                max(0.0, levels.get(price, 0.0))
                for price in eligible_prices
            )
            fill_cap = self.reduce_only_fill_cap(order)
            required = min(order.remaining, fill_cap)
            if required <= 1e-12 or known_quantity + 1e-12 < required:
                self._port.expire_order(order, "PAPER_FOK_UNFILLED")
                return

        for price in eligible_prices:
            if not order.active or order.remaining <= 1e-12:
                break
            available = max(0.0, float(levels.get(price, 0.0) or 0.0))
            if available <= 1e-12:
                continue
            quantity = min(
                order.remaining,
                available,
                self.reduce_only_fill_cap(order),
            )
            if quantity <= 1e-12:
                break
            levels[price] = max(0.0, available - quantity)
            self._port.apply_fill(order, quantity, price, is_maker=False)

        if order.active and order.remaining > 1e-12:
            if request.order_type == "MARKET" or request.time_in_force in {
                TIF_IOC,
                TIF_FOK,
            }:
                self._port.expire_order(order, "PAPER_IMMEDIATE_REMAINDER")
            else:
                self.insert_into_local_queue(order)

    def on_market_trade(self, trade: AggTradeData) -> bool:
        state = self._state
        if trade.price <= 0.0 or trade.quantity <= 0.0:
            return False
        last_id = int(state.last_market_trade_id.get(trade.symbol, -1))
        if int(trade.trade_id) >= 0 and int(trade.trade_id) <= last_id:
            return False
        if int(trade.trade_id) >= 0:
            state.last_market_trade_id[trade.symbol] = int(trade.trade_id)

        maker_side = "BUY" if trade.maker_is_buyer else "SELL"
        candidates = sorted(
            (
                order
                for order in state.orders.values()
                if order.active
                and order.committed
                and order.request.symbol == trade.symbol
                and order.request.side == maker_side
                and order.request.order_type == "LIMIT"
                and order.request.time_in_force not in {TIF_IOC, TIF_FOK}
            ),
            key=self.passive_match_priority,
        )
        for order in candidates:
            if (
                order.request.time_in_force == TIF_RPI
                and self._policy.rpi_fill_model != "public_trade_proxy"
            ):
                continue
            price_relation = self.passive_trade_relation(
                order,
                float(trade.price),
            )
            if price_relation == "not_reached":
                continue
            if self.reduce_only_fill_cap(order) <= 1e-12:
                self._port.expire_order(
                    order,
                    "PAPER_REDUCE_ONLY_EXHAUSTED",
                )
                continue
            ahead_before = max(0.0, order.queue_ahead)
            if price_relation == "through":
                quantity = min(
                    order.remaining,
                    self.reduce_only_fill_cap(order),
                )
            else:
                order.queue_ahead = max(
                    0.0,
                    ahead_before - float(trade.quantity),
                )
                eligible_quantity = max(
                    0.0,
                    float(trade.quantity) - ahead_before,
                )
                quantity = min(
                    order.remaining,
                    eligible_quantity,
                    self.reduce_only_fill_cap(order),
                )
            if quantity <= 1e-12:
                continue
            self._port.apply_fill(
                order,
                quantity,
                float(order.request.price),
                is_maker=True,
                fill_context={
                    "fill_trigger": price_relation,
                    "market_trade_id": int(trade.trade_id),
                    "market_trade_price": float(trade.price),
                    "market_trade_qty": float(trade.quantity),
                    "market_trade_exchange_time": float(
                        trade.exchange_timestamp or 0.0
                    ),
                    "market_trade_received_time": float(
                        trade.received_timestamp or 0.0
                    ),
                    "market_trade_clock_offset_ms": (
                        float(trade.clock_offset_ms)
                        if trade.clock_offset_ms is not None
                        else None
                    ),
                    "market_trade_transport_latency_ms": (
                        (
                            float(trade.corrected_received_timestamp)
                            - float(trade.exchange_timestamp)
                        )
                        * 1000.0
                        if trade.corrected_received_timestamp > 0.0
                        and trade.exchange_timestamp > 0.0
                        else None
                    ),
                    "market_trade_local_age_ms": (
                        max(
                            0.0,
                            (
                                time.perf_counter()
                                - float(trade.received_monotonic)
                            )
                            * 1000.0,
                        )
                        if trade.received_monotonic > 0.0
                        else None
                    ),
                    "queue_ahead_before": ahead_before,
                },
            )
        return True

    def on_book(self, book: OrderBook) -> bool:
        state = self._state
        previous = state.books.get(book.symbol)
        if previous is not None and self._policy.cancel_ahead_fraction > 0.0:
            self.apply_conservative_cancel_ahead(previous, book)
        state.books[book.symbol] = book
        state.liquidity[book.symbol] = {
            "bids": {
                float(price): float(qty) for price, qty in book.bids.items()
            },
            "asks": {
                float(price): float(qty) for price, qty in book.asks.items()
            },
        }
        return True

    @staticmethod
    def local_queue_priority(order: PaperOrder):
        # Binance gives non-RPI orders priority over RPI at the same level.
        is_rpi = order.request.time_in_force == TIF_RPI
        return (1 if is_rpi else 0, order.accept_seq)

    @classmethod
    def passive_match_priority(cls, order: PaperOrder):
        price = float(order.request.price)
        price_priority = -price if order.request.side == "BUY" else price
        return (price_priority, *cls.local_queue_priority(order))

    @staticmethod
    def same_local_level(left: PaperOrder, right: PaperOrder) -> bool:
        return (
            left.request.symbol == right.request.symbol
            and left.request.side == right.request.side
            and abs(
                float(left.request.price) - float(right.request.price)
            )
            <= 1e-12
        )

    def insert_into_local_queue(self, order: PaperOrder):
        state = self._state
        if order.queue_inserted or not order.active or not order.committed:
            return
        self.set_initial_queue_ahead(order)
        order.queue_inserted = True
        order_priority = self.local_queue_priority(order)
        for candidate in state.orders.values():
            if (
                candidate.client_oid != order.client_oid
                and candidate.active
                and candidate.committed
                and candidate.queue_inserted
                and self.same_local_level(candidate, order)
                and order_priority < self.local_queue_priority(candidate)
            ):
                candidate.queue_ahead += order.remaining

    def set_initial_queue_ahead(self, order: PaperOrder):
        state = self._state
        if not order.active or order.request.order_type != "LIMIT":
            return
        book = state.books.get(order.request.symbol)
        if book is None:
            order.queue_ahead = 0.0
            return
        own_side = book.bids if order.request.side == "BUY" else book.asks
        external_ahead = max(
            0.0,
            float(own_side.get(float(order.request.price), 0.0) or 0.0),
        )
        local_ahead = sum(
            candidate.remaining
            for candidate in state.orders.values()
            if candidate.client_oid != order.client_oid
            and candidate.active
            and candidate.committed
            and candidate.queue_inserted
            and self.same_local_level(candidate, order)
            and self.local_queue_priority(candidate)
            < self.local_queue_priority(order)
        )
        order.queue_ahead = external_ahead + local_ahead

    def remove_from_later_local_queue(
        self,
        order: PaperOrder,
        removed_quantity: float,
    ):
        state = self._state
        if removed_quantity <= 1e-12 or not order.queue_inserted:
            return
        removed_priority = self.local_queue_priority(order)
        for candidate in state.orders.values():
            if (
                candidate.active
                and candidate.committed
                and candidate.queue_inserted
                and self.same_local_level(candidate, order)
                and removed_priority < self.local_queue_priority(candidate)
            ):
                candidate.queue_ahead = max(
                    0.0,
                    candidate.queue_ahead - removed_quantity,
                )
        order.queue_inserted = False

    def apply_conservative_cancel_ahead(
        self,
        previous: OrderBook,
        current: OrderBook,
    ):
        state = self._state
        for order in state.orders.values():
            if (
                not order.active
                or not order.committed
                or order.request.symbol != current.symbol
            ):
                continue
            previous_side = (
                previous.bids
                if order.request.side == "BUY"
                else previous.asks
            )
            current_side = (
                current.bids
                if order.request.side == "BUY"
                else current.asks
            )
            price = float(order.request.price)
            # Missing unpublished levels do not prove external cancellation.
            if price not in previous_side or price not in current_side:
                continue
            reduction = max(
                0.0,
                float(previous_side[price]) - float(current_side[price]),
            )
            order.queue_ahead = max(
                0.0,
                order.queue_ahead
                - reduction * self._policy.cancel_ahead_fraction,
            )

    @staticmethod
    def would_cross(
        request: OrderRequest,
        best_bid: float,
        best_ask: float,
    ) -> bool:
        if request.order_type == "MARKET":
            return True
        if request.side == "BUY":
            return (
                best_ask > 0.0
                and float(request.price) >= best_ask - 1e-12
            )
        return best_bid > 0.0 and float(request.price) <= best_bid + 1e-12

    def price_is_executable(
        self,
        order: PaperOrder,
        external_price: float,
    ) -> bool:
        state = self._state
        request = order.request
        if request.order_type == "LIMIT":
            if request.side == "BUY":
                return external_price <= float(request.price) + 1e-12
            return external_price >= float(request.price) - 1e-12

        book = state.books.get(request.symbol)
        if book is None or self._policy.market_order_max_slippage_bps <= 0.0:
            return True
        best_price = (
            float(book.get_best_ask()[0])
            if request.side == "BUY"
            else float(book.get_best_bid()[0])
        )
        if best_price <= 0.0:
            return False
        distance_bps = (
            abs(external_price - best_price) / best_price * 10_000.0
        )
        return (
            distance_bps
            <= self._policy.market_order_max_slippage_bps + 1e-9
        )

    @staticmethod
    def passive_trade_relation(
        order: PaperOrder,
        trade_price: float,
    ) -> str:
        order_price = float(order.request.price)
        tolerance = max(1e-12, abs(order_price) * 1e-12)
        if abs(trade_price - order_price) <= tolerance:
            return "at_price"
        if order.request.side == "BUY":
            return "through" if trade_price < order_price else "not_reached"
        return "through" if trade_price > order_price else "not_reached"

    def reduce_only_fill_cap(self, order: PaperOrder) -> float:
        if not order.request.reduce_only:
            return order.remaining
        position = self._state.positions.get(
            order.request.symbol,
            PaperPosition(),
        )
        if position.quantity > 1e-12 and order.request.side == "SELL":
            return min(order.remaining, position.quantity)
        if position.quantity < -1e-12 and order.request.side == "BUY":
            return min(order.remaining, abs(position.quantity))
        return 0.0

    def fee_rate(self, order: PaperOrder, is_maker: bool) -> float:
        if order.request.time_in_force == TIF_RPI:
            return self.rpi_fee_rate(order.request.symbol)
        rate = self._policy.maker_fee if is_maker else self._policy.taker_fee
        return max(0.0, rate)

    def rpi_fee_rate(self, symbol: str) -> float:
        policy = self._policy
        return max(
            0.0,
            resolve_passive_fee_rate(
                maker_rate=policy.maker_fee,
                symbol=symbol,
                is_rpi=True,
                rpi_commission_rates=policy.rpi_commission_rates,
                default_rpi_commission_rate=policy.rpi_commission_rate,
            ),
        )


__all__ = [
    "PaperMatchingEngine",
    "PaperMatchingPolicy",
    "PaperMatchingPort",
    "PaperVenueState",
]
