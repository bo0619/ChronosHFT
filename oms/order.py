import time
from typing import Dict, Set

from event.type import (
    ExecutionPolicy,
    OrderIntent,
    OrderStateSnapshot,
    OrderStatus,
    Side,
)

TERMINAL_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
    OrderStatus.REJECTED_LOCALLY,
    OrderStatus.EXPIRED,
}

_ALLOWED_TRANSITIONS: Dict[OrderStatus, Set[OrderStatus]] = {
    OrderStatus.CREATED: {OrderStatus.SUBMITTING, OrderStatus.REJECTED_LOCALLY},
    OrderStatus.SUBMITTING: {OrderStatus.PENDING_ACK, OrderStatus.REJECTED_LOCALLY},
    OrderStatus.PENDING_ACK: {
        OrderStatus.NEW,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLING,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
        OrderStatus.REJECTED_LOCALLY,
    },
    OrderStatus.NEW: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLING,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    },
    OrderStatus.PARTIALLY_FILLED: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLING,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
    },
    OrderStatus.CANCELLING: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
    },
    OrderStatus.FILLED: set(),
    OrderStatus.CANCELLED: set(),
    OrderStatus.REJECTED: set(),
    OrderStatus.REJECTED_LOCALLY: set(),
    OrderStatus.EXPIRED: set(),
}


class Order:
    """Stateful OMS order with explicit transition rules."""

    def __init__(self, client_oid: str, intent: OrderIntent):
        self.client_oid = client_oid
        self.intent = intent

        self.exchange_oid = ""
        self.status = OrderStatus.CREATED

        self.filled_volume = 0.0
        self.avg_price = 0.0
        self.cumulative_cost = 0.0

        now = time.time()
        self.created_at = now
        self.updated_at = now

        self.error_msg = ""
        self.last_update_seq = 0
        self.last_exchange_status = ""

    def is_active(self):
        return self.status in {
            OrderStatus.SUBMITTING,
            OrderStatus.PENDING_ACK,
            OrderStatus.NEW,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.CANCELLING,
        }

    def is_terminal(self):
        return self.status in TERMINAL_STATUSES

    def to_snapshot(self) -> OrderStateSnapshot:
        return OrderStateSnapshot(
            client_oid=self.client_oid,
            exchange_oid=self.exchange_oid,
            symbol=self.intent.symbol,
            status=self.status,
            price=self.intent.price,
            volume=self.intent.volume,
            filled_volume=self.filled_volume,
            avg_price=self.avg_price,
            update_time=self.updated_at,
        )

    def to_record(self) -> dict:
        return {
            "client_oid": self.client_oid,
            "exchange_oid": self.exchange_oid,
            "status": self.status.value,
            "filled_volume": self.filled_volume,
            "avg_price": self.avg_price,
            "cumulative_cost": self.cumulative_cost,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error_msg": self.error_msg,
            "last_update_seq": self.last_update_seq,
            "last_exchange_status": self.last_exchange_status,
            "intent": {
                "strategy_id": self.intent.strategy_id,
                "symbol": self.intent.symbol,
                "side": self.intent.side.value,
                "price": self.intent.price,
                "volume": self.intent.volume,
                "order_type": self.intent.order_type,
                "time_in_force": self.intent.time_in_force,
                "is_post_only": self.intent.is_post_only,
                "policy": self.intent.policy.value,
                "tag": self.intent.tag,
            },
        }

    @classmethod
    def from_record(cls, payload: dict):
        intent_payload = payload.get("intent", {})
        intent = OrderIntent(
            strategy_id=intent_payload.get("strategy_id", "recovered"),
            symbol=intent_payload.get("symbol", ""),
            side=Side(intent_payload.get("side", Side.BUY.value)),
            price=float(intent_payload.get("price", 0.0)),
            volume=float(intent_payload.get("volume", 0.0)),
            order_type=intent_payload.get("order_type", "LIMIT"),
            time_in_force=intent_payload.get("time_in_force", "GTC"),
            is_post_only=bool(intent_payload.get("is_post_only", False)),
            policy=ExecutionPolicy(
                intent_payload.get("policy", ExecutionPolicy.PASSIVE.value)
            ),
            tag=intent_payload.get("tag", ""),
        )
        order = cls(payload.get("client_oid", ""), intent)
        order.exchange_oid = payload.get("exchange_oid", "")
        order.status = OrderStatus(payload.get("status", OrderStatus.CREATED.value))
        order.filled_volume = float(payload.get("filled_volume", 0.0))
        order.avg_price = float(payload.get("avg_price", 0.0))
        order.cumulative_cost = float(payload.get("cumulative_cost", 0.0))
        order.created_at = float(payload.get("created_at", time.time()))
        order.updated_at = float(payload.get("updated_at", order.created_at))
        order.error_msg = payload.get("error_msg", "")
        order.last_update_seq = int(payload.get("last_update_seq", 0))
        order.last_exchange_status = payload.get("last_exchange_status", "")
        return order

    def note_exchange_update(
        self,
        exchange_status: str = "",
        update_time: float = None,
        seq: int = 0,
        exchange_oid: str = "",
    ):
        if exchange_oid:
            self.exchange_oid = exchange_oid
        if exchange_status:
            self.last_exchange_status = exchange_status
        if seq:
            self.last_update_seq = max(self.last_update_seq, seq)
        self.updated_at = update_time if update_time else time.time()

    def mark_submitting(self):
        self._transition(OrderStatus.SUBMITTING)

    def mark_pending_ack(self, exchange_oid: str = ""):
        self._transition(OrderStatus.PENDING_ACK, exchange_oid=exchange_oid)

    def mark_new(self, exchange_oid: str = "", update_time: float = None, seq: int = 0):
        self._transition(
            OrderStatus.NEW,
            exchange_oid=exchange_oid,
            exchange_status="NEW",
            update_time=update_time,
            seq=seq,
        )

    def mark_cancelling(self):
        self._transition(OrderStatus.CANCELLING)

    def mark_cancelled(
        self,
        update_time: float = None,
        seq: int = 0,
        exchange_status: str = "CANCELED",
    ):
        self._transition(
            OrderStatus.CANCELLED,
            exchange_status=exchange_status,
            update_time=update_time,
            seq=seq,
        )

    def mark_expired(self, update_time: float = None, seq: int = 0):
        self._transition(
            OrderStatus.EXPIRED,
            exchange_status="EXPIRED",
            update_time=update_time,
            seq=seq,
        )

    def mark_rejected(
        self,
        reason: str = "",
        update_time: float = None,
        seq: int = 0,
        exchange_status: str = "REJECTED",
    ):
        self._transition(
            OrderStatus.REJECTED,
            reason=reason,
            exchange_status=exchange_status,
            update_time=update_time,
            seq=seq,
        )

    def mark_rejected_locally(self, reason: str):
        self._transition(OrderStatus.REJECTED_LOCALLY, reason=reason)

    def add_fill(
        self,
        fill_qty: float,
        fill_price: float,
        update_time: float = None,
        seq: int = 0,
        exchange_status: str = "PARTIALLY_FILLED",
    ):
        if fill_qty <= 0:
            return False

        remaining = max(0.0, self.intent.volume - self.filled_volume)
        if fill_qty > remaining + 1e-6:
            raise ValueError(
                f"Fill exceeds remaining volume for {self.client_oid}: {fill_qty} > {remaining}"
            )

        applied_qty = min(fill_qty, remaining)
        self.cumulative_cost += applied_qty * fill_price
        self.filled_volume += applied_qty
        if self.filled_volume > 0:
            self.avg_price = self.cumulative_cost / self.filled_volume

        next_status = (
            OrderStatus.FILLED
            if self.filled_volume >= self.intent.volume - 1e-8
            else OrderStatus.PARTIALLY_FILLED
        )
        self._transition(
            next_status,
            exchange_status=exchange_status,
            update_time=update_time,
            seq=seq,
        )
        return True

    def mark_filled(self, update_time: float = None, seq: int = 0):
        if self.filled_volume < self.intent.volume - 1e-8:
            raise ValueError(
                f"Cannot mark FILLED before volume completes for {self.client_oid}"
            )
        self._transition(
            OrderStatus.FILLED,
            exchange_status="FILLED",
            update_time=update_time,
            seq=seq,
        )

    def _transition(
        self,
        next_status: OrderStatus,
        reason: str = "",
        exchange_oid: str = "",
        exchange_status: str = "",
        update_time: float = None,
        seq: int = 0,
    ):
        if next_status == self.status:
            self.note_exchange_update(
                exchange_status=exchange_status,
                update_time=update_time,
                seq=seq,
                exchange_oid=exchange_oid,
            )
            if reason:
                self.error_msg = reason
            return False

        allowed = _ALLOWED_TRANSITIONS.get(self.status, set())
        if next_status not in allowed:
            raise ValueError(
                f"Invalid order transition {self.status.value} -> {next_status.value} for {self.client_oid}"
            )

        self.status = next_status
        if reason:
            self.error_msg = reason
        if exchange_oid:
            self.exchange_oid = exchange_oid
        self.note_exchange_update(
            exchange_status=exchange_status,
            update_time=update_time,
            seq=seq,
            exchange_oid=exchange_oid,
        )
        return True
