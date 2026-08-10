"""Encapsulated active and recently terminal OMS orders."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from types import MappingProxyType

from .order import Order


class OrderStore:
    """Own the OMS order index and bound terminal-order retention."""

    __slots__ = (
        "__active",
        "__lock",
        "__orders",
        "__terminal",
        "__terminal_limit",
        "__view",
    )

    def __init__(self, *, terminal_limit: int = 2000) -> None:
        if (
            isinstance(terminal_limit, bool)
            or not isinstance(terminal_limit, int)
            or terminal_limit <= 0
        ):
            raise ValueError("terminal_limit must be a positive integer")
        self.__lock = threading.RLock()
        self.__orders: dict[str, Order] = {}
        self.__active: dict[str, Order] = {}
        self.__terminal: OrderedDict[str, Order] = OrderedDict()
        self.__terminal_limit = terminal_limit
        self.__view: Mapping[str, Order] = MappingProxyType(self.__orders)

    @staticmethod
    def _validate_order(order: object) -> Order:
        if not isinstance(order, Order):
            raise TypeError("order store values must be Order instances")
        if not isinstance(order.client_oid, str):
            raise ValueError("order client_oid must be a string")
        client_oid = order.client_oid
        if not client_oid or client_oid != client_oid.strip():
            raise ValueError("order client_oid must be non-empty and canonical")
        return order

    @property
    def terminal_limit(self) -> int:
        return self.__terminal_limit

    def view(self) -> Mapping[str, Order]:
        """Return a live read-only view used by legacy read consumers."""

        return self.__view

    def active_view(self) -> Mapping[str, Order]:
        """Return a detached read-only active-order projection."""

        with self.__lock:
            return MappingProxyType(dict(self.__active))

    def get(self, client_oid: str) -> Order | None:
        with self.__lock:
            return self.__orders.get(str(client_oid or ""))

    def add(self, order: Order) -> tuple[Order, ...]:
        order = self._validate_order(order)
        client_oid = order.client_oid
        with self.__lock:
            if client_oid in self.__orders:
                raise ValueError(f"duplicate OMS client_oid: {client_oid}")
            self.__orders[client_oid] = order
            evicted = self.__index_locked(order)
            return evicted

    def remove(self, client_oid: str) -> Order | None:
        client_oid = str(client_oid or "")
        with self.__lock:
            order = self.__orders.pop(client_oid, None)
            if order is None:
                return None
            self.__active.pop(client_oid, None)
            self.__terminal.pop(client_oid, None)
            return order

    def reindex(self, order: Order) -> tuple[Order, ...]:
        """Refresh active/terminal projections after an order transition."""

        order = self._validate_order(order)
        client_oid = order.client_oid
        with self.__lock:
            if self.__orders.get(client_oid) is not order:
                raise ValueError(
                    f"cannot reindex unowned OMS order: {client_oid}"
                )
            evicted = self.__index_locked(order)
            return evicted

    def replace_active(self, orders: Iterable[Order]) -> None:
        """Atomically replace all retained state with validated active orders."""

        replacement: dict[str, Order] = {}
        for raw_order in orders:
            order = self._validate_order(raw_order)
            if not order.is_active():
                raise ValueError(
                    f"replacement order is not active: {order.client_oid}"
                )
            if order.client_oid in replacement:
                raise ValueError(
                    f"duplicate OMS client_oid: {order.client_oid}"
                )
            replacement[order.client_oid] = order
        with self.__lock:
            self.__orders.clear()
            self.__orders.update(replacement)
            self.__active = dict(replacement)
            self.__terminal.clear()

    def clear(self) -> tuple[Order, ...]:
        with self.__lock:
            removed = tuple(self.__orders.values())
            self.__orders.clear()
            self.__active.clear()
            self.__terminal.clear()
            return removed

    def __index_locked(self, order: Order) -> tuple[Order, ...]:
        client_oid = order.client_oid
        if order.is_active():
            self.__terminal.pop(client_oid, None)
            self.__active[client_oid] = order
            return ()
        self.__active.pop(client_oid, None)
        if not order.is_terminal():
            self.__terminal.pop(client_oid, None)
            return ()
        self.__terminal[client_oid] = order
        self.__terminal.move_to_end(client_oid)
        evicted = []
        while len(self.__terminal) > self.__terminal_limit:
            expired_oid, expired_order = self.__terminal.popitem(last=False)
            self.__orders.pop(expired_oid, None)
            evicted.append(expired_order)
        return tuple(evicted)


__all__ = ["OrderStore"]
