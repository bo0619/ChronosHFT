import threading
import time
from collections import deque

from data.cache import data_cache
from data.ref_data import ref_data_manager
from event.type import OrderIntent, TIF_GTX, TIF_RPI


class OrderValidator:
    def __init__(self, config: dict):
        risk = config.get("risk", {})
        limits = risk.get("limits", {})
        sanity = risk.get("price_sanity", {})
        tech = risk.get("tech_health", {})
        freshness = risk.get("market_data_freshness", {})

        self.max_order_qty = limits.get("max_order_qty", 1000.0)
        self.max_order_notional = limits.get("max_order_notional", 5000.0)
        self.max_deviation_pct = sanity.get("max_deviation_pct", 0.05)
        self.max_spread_pct = sanity.get("max_spread_pct", 0.015)
        self.max_orders_per_sec = tech.get("max_order_count_per_sec", 20)
        self.freshness_enabled = bool(freshness.get("enabled", False))
        self.require_mark_price = bool(freshness.get("require_mark_price", True))
        self.require_book = bool(freshness.get("require_book", True))
        self.max_mark_age_ms = max(
            0.0,
            float(freshness.get("max_mark_age_ms", 3000.0) or 0.0),
        )
        self.max_book_age_ms = max(
            0.0,
            float(freshness.get("max_book_age_ms", 1500.0) or 0.0),
        )

        self._order_timestamps: deque = deque()
        self._rate_lock = threading.Lock()

    def validate_params(self, intent: OrderIntent) -> tuple[bool, str]:
        if intent.time_in_force == TIF_RPI and intent.order_type != "LIMIT":
            return False, "rpi_requires_limit_order"
        if intent.is_post_only and intent.order_type != "LIMIT":
            return False, "post_only_requires_limit_order"
        if intent.is_post_only and intent.time_in_force not in {TIF_GTX, TIF_RPI}:
            return False, (
                "post_only_incompatible_time_in_force:"
                f"{intent.time_in_force}"
            )

        info = ref_data_manager.get_info(intent.symbol)
        if intent.time_in_force == TIF_RPI:
            if info is None:
                return False, f"rpi_capability_unknown:{intent.symbol}"
            if not info.supports_rpi:
                return False, f"rpi_unsupported_symbol:{intent.symbol}"

        if intent.price <= 0 or intent.volume <= 0:
            return False, "non_positive_price_or_volume"

        notional = intent.price * intent.volume

        if info and notional < max(info.min_notional, 5.0):
            return False, f"notional_below_min:{notional:.8f}"

        if intent.volume > self.max_order_qty:
            return False, f"qty_exceeded:{intent.volume}>{self.max_order_qty}"

        if not intent.reduce_only and notional > self.max_order_notional:
            return False, f"notional_exceeded:{notional:.2f}>{self.max_order_notional:.2f}"

        snapshot = data_cache.get_risk_snapshot(intent.symbol)
        if self.freshness_enabled and not intent.reduce_only:
            freshness_error = self._validate_market_data_freshness(snapshot)
            if freshness_error:
                return False, freshness_error

        mark_price = snapshot["mark_price"]
        if mark_price <= 0 and not self.freshness_enabled:
            mark_price = data_cache.get_mark_price(intent.symbol)
        if mark_price > 0 and not intent.reduce_only:
            deviation = abs(intent.price - mark_price) / mark_price
            if deviation > self.max_deviation_pct:
                return (
                    False,
                    f"price_deviation:{deviation*100:.3f}%>{self.max_deviation_pct*100:.1f}%"
                    f"(order={intent.price},mark={mark_price})",
                )

        bid_price = snapshot["bid_price"]
        ask_price = snapshot["ask_price"]
        if not self.freshness_enabled and (bid_price <= 0 or ask_price <= 0):
            bid_price, ask_price = data_cache.get_best_quote(intent.symbol)
        if (
            not intent.reduce_only
            and bid_price > 0
            and ask_price > 0
            and ask_price >= bid_price
        ):
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

    def _validate_market_data_freshness(self, snapshot: dict) -> str:
        mark_price = float(snapshot.get("mark_price", 0.0) or 0.0)
        mark_age_ms = snapshot.get("mark_age_ms")
        if self.require_mark_price and mark_price <= 0:
            return "market_data_unavailable:mark_price"
        if mark_price > 0 and mark_age_ms is None:
            return "market_data_timestamp_missing:mark_price"
        if (
            mark_price > 0
            and self.max_mark_age_ms > 0
            and float(mark_age_ms) > self.max_mark_age_ms
        ):
            return (
                f"market_data_stale:mark_price:{float(mark_age_ms):.1f}ms"
                f">{self.max_mark_age_ms:.1f}ms"
            )

        bid_price = float(snapshot.get("bid_price", 0.0) or 0.0)
        ask_price = float(snapshot.get("ask_price", 0.0) or 0.0)
        book_age_ms = snapshot.get("book_age_ms")
        if self.require_book and (bid_price <= 0 or ask_price <= 0):
            return "market_data_unavailable:book"
        if (bid_price > 0 or ask_price > 0) and book_age_ms is None:
            return "market_data_timestamp_missing:book"
        if (
            (bid_price > 0 or ask_price > 0)
            and self.max_book_age_ms > 0
            and float(book_age_ms) > self.max_book_age_ms
        ):
            return (
                f"market_data_stale:book:{float(book_age_ms):.1f}ms"
                f">{self.max_book_age_ms:.1f}ms"
            )
        return ""

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
