import threading
import time
import math
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
        self.max_reduce_orders_per_sec = tech.get(
            "max_reduce_order_count_per_sec",
            self.max_orders_per_sec,
        )
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
        self._reduce_order_timestamps: deque = deque()
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

        try:
            price = float(intent.price)
            volume = float(intent.volume)
        except (TypeError, ValueError):
            return False, "non_numeric_price_or_volume"
        if not math.isfinite(price) or not math.isfinite(volume):
            return False, "non_finite_price_or_volume"
        if price <= 0 or volume <= 0:
            return False, "non_positive_price_or_volume"
        intent.price = price
        intent.volume = volume

        notional = price * volume
        if not math.isfinite(notional):
            return False, "non_finite_order_notional"

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

        mark_price = self._finite_or_zero(snapshot["mark_price"])
        if mark_price <= 0 and not self.freshness_enabled:
            mark_price = self._finite_or_zero(
                data_cache.get_mark_price(intent.symbol)
            )
        if mark_price > 0 and not intent.reduce_only:
            deviation = abs(intent.price - mark_price) / mark_price
            if deviation > self.max_deviation_pct:
                return (
                    False,
                    f"price_deviation:{deviation*100:.3f}%>{self.max_deviation_pct*100:.1f}%"
                    f"(order={intent.price},mark={mark_price})",
                )

        bid_price = self._finite_or_zero(snapshot["bid_price"])
        ask_price = self._finite_or_zero(snapshot["ask_price"])
        if not self.freshness_enabled and (bid_price <= 0 or ask_price <= 0):
            bid_price, ask_price = data_cache.get_best_quote(intent.symbol)
            bid_price = self._finite_or_zero(bid_price)
            ask_price = self._finite_or_zero(ask_price)
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

        reject, reason = self._check_rate_limit(
            reduce_only=intent.reduce_only,
        )
        if reject:
            return False, reason

        return True, ""

    def _validate_market_data_freshness(self, snapshot: dict) -> str:
        mark_price = self._finite_or_zero(
            snapshot.get("mark_price", 0.0)
        )
        mark_age_ms = snapshot.get("mark_age_ms")
        if self.require_mark_price and mark_price <= 0:
            return "market_data_unavailable:mark_price"
        if mark_price > 0 and mark_age_ms is None:
            return "market_data_timestamp_missing:mark_price"
        if mark_age_ms is not None:
            try:
                mark_age_ms = float(mark_age_ms)
            except (TypeError, ValueError):
                return "market_data_timestamp_invalid:mark_price"
            if not math.isfinite(mark_age_ms) or mark_age_ms < 0.0:
                return "market_data_timestamp_invalid:mark_price"
        if (
            mark_price > 0
            and self.max_mark_age_ms > 0
            and mark_age_ms > self.max_mark_age_ms
        ):
            return (
                f"market_data_stale:mark_price:{mark_age_ms:.1f}ms"
                f">{self.max_mark_age_ms:.1f}ms"
            )

        bid_price = self._finite_or_zero(
            snapshot.get("bid_price", 0.0)
        )
        ask_price = self._finite_or_zero(
            snapshot.get("ask_price", 0.0)
        )
        book_age_ms = snapshot.get("book_age_ms")
        if self.require_book and (bid_price <= 0 or ask_price <= 0):
            return "market_data_unavailable:book"
        if (bid_price > 0 or ask_price > 0) and book_age_ms is None:
            return "market_data_timestamp_missing:book"
        if book_age_ms is not None:
            try:
                book_age_ms = float(book_age_ms)
            except (TypeError, ValueError):
                return "market_data_timestamp_invalid:book"
            if not math.isfinite(book_age_ms) or book_age_ms < 0.0:
                return "market_data_timestamp_invalid:book"
        if (
            (bid_price > 0 or ask_price > 0)
            and self.max_book_age_ms > 0
            and book_age_ms > self.max_book_age_ms
        ):
            return (
                f"market_data_stale:book:{book_age_ms:.1f}ms"
                f">{self.max_book_age_ms:.1f}ms"
            )
        return ""

    @staticmethod
    def _finite_or_zero(value) -> float:
        try:
            normalized = float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return normalized if math.isfinite(normalized) else 0.0

    def _check_rate_limit(
        self,
        *,
        reduce_only: bool = False,
    ) -> tuple[bool, str]:
        with self._rate_lock:
            now = time.perf_counter()
            cutoff = now - 1.0
            timestamps = (
                self._reduce_order_timestamps
                if reduce_only
                else self._order_timestamps
            )
            limit = (
                self.max_reduce_orders_per_sec
                if reduce_only
                else self.max_orders_per_sec
            )

            while timestamps and timestamps[0] < cutoff:
                timestamps.popleft()

            current_count = len(timestamps)
            if current_count >= limit:
                channel = "reduce_only" if reduce_only else "risk"
                return (
                    True,
                    f"rate_limit:{channel}:{current_count}>={limit}/s",
                )

            timestamps.append(now)
            return False, ""
