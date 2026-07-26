"""Independent sliding-window budget for outbound OMS messages."""

from __future__ import annotations

import math
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class OutboundMessageReservation:
    reservation_id: str
    message_kind: str
    reserved_at: float


class OutboundMessageBudget:
    NEW_ORDER = "NEW_ORDER"
    REDUCE_ORDER = "REDUCE_ORDER"
    CANCEL = "CANCEL"
    MESSAGE_KINDS = frozenset({NEW_ORDER, REDUCE_ORDER, CANCEL})

    def __init__(self, config: dict | None = None):
        config = config or {}
        self.enabled = bool(config.get("enabled", False))
        self.window_sec = max(
            0.05,
            float(config.get("window_sec", 1.0) or 1.0),
        )
        self.max_total = max(
            0,
            int(config.get("max_total_messages_per_window", 20) or 0),
        )
        self.max_new_orders = max(
            0,
            int(config.get("max_new_orders_per_window", 10) or 0),
        )
        self.max_reduce_orders = max(
            0,
            int(config.get("max_reduce_orders_per_window", 10) or 0),
        )
        self.max_cancels = max(
            0,
            int(config.get("max_cancel_messages_per_window", 20) or 0),
        )
        configured_reserved = max(
            0,
            int(config.get("reserved_risk_messages_per_window", 5) or 0),
        )
        self.reserved_risk_messages = (
            min(configured_reserved, self.max_total)
            if self.max_total > 0
            else configured_reserved
        )
        default_cancel_reserved = (
            min(configured_reserved, max(1, self.max_total // 4))
            if self.max_total > 0 and configured_reserved > 0
            else configured_reserved
        )
        configured_cancel_reserved = max(
            0,
            int(
                config.get(
                    "reserved_cancel_messages_per_window",
                    default_cancel_reserved,
                )
                or 0
            ),
        )
        self.reserved_cancel_messages = (
            min(configured_cancel_reserved, self.reserved_risk_messages)
            if self.max_total > 0
            else configured_cancel_reserved
        )
        if self.enabled:
            if self.max_total <= 0:
                raise ValueError(
                    "enabled outbound message budget requires a positive "
                    "max_total_messages_per_window"
                )
            if self.reserved_cancel_messages <= 0:
                raise ValueError(
                    "enabled outbound message budget must reserve at least "
                    "one cancel message per window"
                )
            if self.max_cancels <= 0:
                raise ValueError(
                    "enabled outbound message budget requires a positive "
                    "max_cancel_messages_per_window"
                )
            if self.max_cancels < self.reserved_cancel_messages:
                raise ValueError(
                    "max_cancel_messages_per_window must cover "
                    "reserved_cancel_messages_per_window"
                )
        self.history: deque[tuple[float, str, str]] = deque()
        self._lock = threading.RLock()
        self._last_observed_at = 0.0

    def _normalize_now_locked(self, now: float | None) -> float:
        observed_at = time.perf_counter() if now is None else float(now)
        if not math.isfinite(observed_at):
            raise ValueError("outbound message budget time must be finite")
        if observed_at < self._last_observed_at:
            raise ValueError("outbound message budget time moved backwards")
        self._last_observed_at = observed_at
        return observed_at

    def _prune_locked(self, now: float) -> None:
        cutoff = now - self.window_sec
        while self.history and self.history[0][0] <= cutoff:
            self.history.popleft()

    def _counts_locked(self) -> dict[str, int]:
        counts = {
            self.NEW_ORDER: 0,
            self.REDUCE_ORDER: 0,
            self.CANCEL: 0,
        }
        for _timestamp, message_kind, _reservation_id in self.history:
            if message_kind in counts:
                counts[message_kind] += 1
        counts["TOTAL"] = len(self.history)
        return counts

    def reserve_token(
        self,
        message_kind: str,
        now: float | None = None,
    ) -> tuple[OutboundMessageReservation | None, str]:
        if not self.enabled:
            return None, ""
        if message_kind not in self.MESSAGE_KINDS:
            raise ValueError(
                f"unsupported_outbound_message_kind:{message_kind}"
            )
        with self._lock:
            observed_at = self._normalize_now_locked(now)
            self._prune_locked(observed_at)
            counts = self._counts_locked()
            class_limits = {
                self.NEW_ORDER: self.max_new_orders,
                self.REDUCE_ORDER: self.max_reduce_orders,
                self.CANCEL: self.max_cancels,
            }
            class_limit = class_limits[message_kind]
            if class_limit > 0 and counts[message_kind] >= class_limit:
                return None, (
                    "outbound_message_budget:"
                    f"{message_kind.lower()}_limit:"
                    f"{counts[message_kind]}>={class_limit}"
                )

            total_count = counts["TOTAL"]
            if self.max_total > 0:
                if message_kind == self.NEW_ORDER:
                    opening_risk_ceiling = max(
                        0,
                        self.max_total - self.reserved_risk_messages,
                    )
                    if total_count >= opening_risk_ceiling:
                        return None, (
                            "outbound_message_budget:risk_capacity_reserved:"
                            f"{total_count}>={opening_risk_ceiling}"
                        )
                elif message_kind == self.REDUCE_ORDER:
                    reduce_ceiling = max(
                        0,
                        self.max_total - self.reserved_cancel_messages,
                    )
                    if total_count >= reduce_ceiling:
                        return None, (
                            "outbound_message_budget:cancel_capacity_reserved:"
                            f"{total_count}>={reduce_ceiling}"
                        )
                elif total_count >= self.max_total:
                    return None, (
                        "outbound_message_budget:total_limit:"
                        f"{total_count}>={self.max_total}"
                    )

            reservation = OutboundMessageReservation(
                reservation_id=uuid.uuid4().hex,
                message_kind=message_kind,
                reserved_at=observed_at,
            )
            self.history.append(
                (
                    observed_at,
                    message_kind,
                    reservation.reservation_id,
                )
            )
            return reservation, ""

    def reserve(self, message_kind: str, now: float | None = None) -> str:
        _reservation, rejection = self.reserve_token(message_kind, now)
        return rejection

    def rollback(self, reservation: OutboundMessageReservation | None) -> bool:
        if reservation is None or not self.enabled:
            return False
        with self._lock:
            for index in range(len(self.history) - 1, -1, -1):
                item = self.history[index]
                if item[2] != reservation.reservation_id:
                    continue
                del self.history[index]
                return True
            return False

    def snapshot(self, now: float | None = None) -> dict:
        with self._lock:
            observed_at = self._normalize_now_locked(now)
            self._prune_locked(observed_at)
            counts = self._counts_locked()
            opening_risk_ceiling = (
                max(0, self.max_total - self.reserved_risk_messages)
                if self.max_total > 0
                else None
            )
            return {
                "enabled": self.enabled,
                "window_sec": self.window_sec,
                "counts": counts,
                "limits": {
                    "total": self.max_total,
                    "new_orders": self.max_new_orders,
                    "reduce_orders": self.max_reduce_orders,
                    "cancels": self.max_cancels,
                    "reserved_risk_messages": self.reserved_risk_messages,
                    "reserved_cancel_messages": (
                        self.reserved_cancel_messages
                    ),
                    "opening_risk_ceiling": opening_risk_ceiling,
                    "reduce_order_ceiling": (
                        max(
                            0,
                            self.max_total - self.reserved_cancel_messages,
                        )
                        if self.max_total > 0
                        else None
                    ),
                },
            }
