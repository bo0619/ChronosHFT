"""State records owned by the single-threaded Binance Paper venue."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from event.type import OrderRequest


ACTIVE_ORDER_STATUSES = frozenset({"NEW", "PARTIALLY_FILLED"})
TERMINAL_ORDER_STATUSES = frozenset(
    {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}
)


@dataclass(slots=True)
class PaperPosition:
    quantity: float = 0.0
    entry_price: float = 0.0


@dataclass(slots=True)
class PaperOrder:
    client_oid: str
    exchange_oid: str
    request: OrderRequest
    accept_seq: int
    created_ms: int
    created_monotonic: float
    update_ms: int
    status: str = "STAGED"
    committed: bool = False
    cum_filled_qty: float = 0.0
    cumulative_cost: float = 0.0
    avg_price: float = 0.0
    queue_ahead: float = 0.0
    fill_model: str = "orderbook"
    cancel_generation_at_stage: int = 0
    pending_cancel_reason: str = ""
    terminal_reason: str = ""
    queue_inserted: bool = False

    @property
    def remaining(self) -> float:
        return max(0.0, float(self.request.volume) - self.cum_filled_qty)

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_ORDER_STATUSES


@dataclass(slots=True)
class EngineCommand:
    kind: str
    payload: Any = None
    wait_for_result: bool = True
    completed: threading.Event = field(default_factory=threading.Event)
    state_lock: threading.RLock = field(default_factory=threading.RLock)
    abandoned: bool = False
    result: Any = None
    error: BaseException | None = None


__all__ = [
    "ACTIVE_ORDER_STATUSES",
    "EngineCommand",
    "PaperOrder",
    "PaperPosition",
    "TERMINAL_ORDER_STATUSES",
]
