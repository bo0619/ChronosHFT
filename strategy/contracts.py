"""Structural contracts between strategy implementations and their runtime."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from event.type import (
    AccountData,
    AggTradeData,
    OrderBook,
    OrderStateSnapshot,
    PositionData,
    TradeData,
)


@runtime_checkable
class StrategyRuntimeContract(Protocol):
    """Event surface consumed by :class:`strategy.runtime.StrategyRuntime`."""

    name: str

    def on_orderbook(self, orderbook: OrderBook): ...

    def on_market_trade(self, trade: AggTradeData): ...

    def on_order(self, snapshot: OrderStateSnapshot): ...

    def on_trade(self, trade: TradeData): ...

    def on_position(self, position: PositionData): ...

    def on_account_update(self, account: AccountData): ...

    def on_system_health(self, message): ...


__all__ = ["StrategyRuntimeContract"]
