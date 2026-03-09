import threading
import time
from collections import deque

from data.cache import data_cache
from data.ref_data import ref_data_manager
from event.type import OrderIntent


class OrderValidator:
    def __init__(self, config: dict):
        limits = config.get("risk", {}).get("limits", {})
        sanity = config.get("risk", {}).get("price_sanity", {})
        tech = config.get("risk", {}).get("tech_health", {})

        self.max_order_qty = limits.get("max_order_qty", 1000.0)
        self.max_order_notional = limits.get("max_order_notional", 5000.0)
        self.max_deviation_pct = sanity.get("max_deviation_pct", 0.05)
        self.max_spread_pct = sanity.get("max_spread_pct", 0.015)
        self.max_orders_per_sec = tech.get("max_order_count_per_sec", 20)

        self._order_timestamps: deque = deque()
        self._rate_lock = threading.Lock()

    def validate_params(self, intent: OrderIntent) -> tuple[bool, str]:
        if intent.price <= 0 or intent.volume <= 0:
            return False, "non_positive_price_or_volume"

        notional = intent.price * intent.volume

        info = ref_data_manager.get_info(intent.symbol)
        if info and notional < max(info.min_notional, 5.0):
            return False, f"notional_below_min:{notional:.8f}"

        if intent.volume > self.max_order_qty:
            return False, f"qty_exceeded:{intent.volume}>{self.max_order_qty}"

        if notional > self.max_order_notional:
            return False, f"notional_exceeded:{notional:.2f}>{self.max_order_notional:.2f}"

        mark_price = data_cache.get_mark_price(intent.symbol)
        if mark_price > 0:
            deviation = abs(intent.price - mark_price) / mark_price
            if deviation > self.max_deviation_pct:
                return (
                    False,
                    f"price_deviation:{deviation*100:.3f}%>{self.max_deviation_pct*100:.1f}%"
                    f"(order={intent.price},mark={mark_price})",
                )

        bid_price, ask_price = data_cache.get_best_quote(intent.symbol)
        if bid_price > 0 and ask_price > 0 and ask_price >= bid_price:
            mid_price = (bid_price + ask_price) / 2.0
            if mid_price > 0:
                spread_pct = (ask_price - bid_price) / mid_price
                if spread_pct > self.max_spread_pct:
                    return (
                        False,
                        f"spread_too_wide:{spread_pct*100:.3f}%>{self.max_spread_pct*100:.3f}%",
                    )

        reject, reason = self._check_rate_limit()
        if reject:
            return False, reason

        return True, ""

    def _check_rate_limit(self) -> tuple[bool, str]:
        with self._rate_lock:
            now = time.monotonic()
            cutoff = now - 1.0

            while self._order_timestamps and self._order_timestamps[0] < cutoff:
                self._order_timestamps.popleft()

            current_count = len(self._order_timestamps)
            if current_count >= self.max_orders_per_sec:
                return True, f"rate_limit:{current_count}>={self.max_orders_per_sec}/s"

            self._order_timestamps.append(now)
            return False, ""
