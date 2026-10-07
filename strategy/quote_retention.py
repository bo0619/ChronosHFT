"""Keep resting quotes in the exchange queue unless moving them is worth it.

Binance futures re-queues an order on every cancel/replace and on every
modify, so each non-essential reprice throws away earned queue position.
Binance also restricts symbols whose 10-minute order counts trip the
Futures Trading Quantitative Rules (orders cancelled within 5 seconds,
orders below 50 USD notional). This module decides when a resting quote
must move for risk, when it may move to chase the model, and caps how
many new quote orders a symbol may send per exchange 10-minute window.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from infrastructure.logger import logger

KEEP = "keep"
RISK_REQUOTE = "risk_requote"
REQUOTE = "requote"

# Binance evaluates quantitative rules on fixed UTC 10-minute cycles.
EXCHANGE_RULE_WINDOW_SEC = 600.0


def _nonnegative(value, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a nonnegative number") from exc
    if not math.isfinite(parsed) or parsed < 0.0:
        raise ValueError(f"{field} must be a nonnegative number")
    return parsed


@dataclass(frozen=True, slots=True)
class QueueRetentionPolicy:
    """How far a resting quote may drift from target before it moves.

    A quote that became more aggressive than target (bid above target, ask
    below target, or size larger than target) is adverse-selection risk and
    always moves after one tick or one size tolerance. A quote that became
    more conservative only moves past the tolerance band, and only after it
    has rested ``min_rest_sec``.
    """

    conservative_tolerance_ticks: float = 2.0
    conservative_tolerance_bps: float = 1.0
    min_rest_sec: float = 5.0
    size_tolerance_ratio: float = 0.1
    max_new_orders_per_symbol_per_10min: int = 1500

    @classmethod
    def from_config(cls, config) -> "QueueRetentionPolicy":
        if config is None:
            config = {}
        if not isinstance(config, dict):
            raise ValueError("glft.queue_retention must be an object")
        defaults = cls()
        max_new_orders = _nonnegative(
            config.get(
                "max_new_orders_per_symbol_per_10min",
                defaults.max_new_orders_per_symbol_per_10min,
            ),
            "glft.queue_retention.max_new_orders_per_symbol_per_10min",
        )
        if max_new_orders < 1.0 or not max_new_orders.is_integer():
            raise ValueError(
                "glft.queue_retention.max_new_orders_per_symbol_per_10min "
                "must be a positive integer"
            )
        return cls(
            conservative_tolerance_ticks=_nonnegative(
                config.get(
                    "conservative_tolerance_ticks",
                    defaults.conservative_tolerance_ticks,
                ),
                "glft.queue_retention.conservative_tolerance_ticks",
            ),
            conservative_tolerance_bps=_nonnegative(
                config.get(
                    "conservative_tolerance_bps",
                    defaults.conservative_tolerance_bps,
                ),
                "glft.queue_retention.conservative_tolerance_bps",
            ),
            min_rest_sec=_nonnegative(
                config.get("min_rest_sec", defaults.min_rest_sec),
                "glft.queue_retention.min_rest_sec",
            ),
            size_tolerance_ratio=_nonnegative(
                config.get(
                    "size_tolerance_ratio",
                    defaults.size_tolerance_ratio,
                ),
                "glft.queue_retention.size_tolerance_ratio",
            ),
            max_new_orders_per_symbol_per_10min=int(max_new_orders),
        )

    def classify(
        self,
        *,
        is_bid: bool,
        resting_price: float,
        resting_volume: float,
        target_price: float,
        target_volume: float,
        tick_size: float,
        qty_step: float,
        rest_age_sec: float,
    ) -> str:
        price_eps = tick_size * 1e-9
        qty_eps = qty_step * 1e-9
        # Positive means the resting quote sits closer to the touch than the
        # model wants, i.e. it is the side that gets picked off.
        aggressive_gap = (
            resting_price - target_price if is_bid else target_price - resting_price
        )
        if aggressive_gap >= tick_size - price_eps:
            return RISK_REQUOTE

        size_excess = resting_volume - target_volume
        if (
            size_excess >= qty_step - qty_eps
            and resting_volume
            > target_volume * (1.0 + self.size_tolerance_ratio) + qty_eps
        ):
            return RISK_REQUOTE

        conservative_gap = -aggressive_gap
        price_tolerance = max(
            self.conservative_tolerance_ticks * tick_size,
            self.conservative_tolerance_bps * target_price / 10_000.0,
        )
        price_drifted = (
            conservative_gap >= tick_size - price_eps
            and conservative_gap > price_tolerance + price_eps
        )
        size_short = (
            target_volume - resting_volume >= qty_step - qty_eps
            and target_volume
            > resting_volume * (1.0 + self.size_tolerance_ratio) + qty_eps
        )
        if (price_drifted or size_short) and rest_age_sec >= self.min_rest_sec:
            return REQUOTE
        return KEEP

    def classify_state(
        self,
        state: dict,
        key: str,
        is_bid: bool,
        target_price: float,
        target_volume: float,
        tick_size: float,
        qty_step: float,
        now: float,
    ) -> str:
        """Classify the resting ``bid``/``ask`` quote stored in ``state``."""
        resting_price = state.get(f"{key}_price")
        resting_volume = state.get(f"{key}_volume")
        if resting_price is None or resting_volume is None:
            return RISK_REQUOTE
        return self.classify(
            is_bid=is_bid,
            resting_price=float(resting_price),
            resting_volume=float(resting_volume),
            target_price=float(target_price),
            target_volume=float(target_volume),
            tick_size=tick_size,
            qty_step=qty_step,
            rest_age_sec=now - state.get(f"{key}_placed_at", float("-inf")),
        )


class QuoteOrderBudget:
    """Count new quote orders per symbol in exchange UTC 10-minute windows."""

    __slots__ = ("limit", "owner", "_window", "_counts", "_exhausted_logged")

    def __init__(self, limit: int, *, owner: str = ""):
        if int(limit) < 1:
            raise ValueError("quote order budget limit must be positive")
        self.limit = int(limit)
        self.owner = owner
        self._window: int | None = None
        self._counts: defaultdict[str, int] = defaultdict(int)
        self._exhausted_logged: set[str] = set()

    def _roll(self, wall_time: float) -> None:
        window = int(wall_time // EXCHANGE_RULE_WINDOW_SEC)
        if window != self._window:
            self._window = window
            self._counts.clear()
            self._exhausted_logged.clear()

    def has_capacity(self, symbol: str, wall_time: float) -> bool:
        self._roll(wall_time)
        if self._counts[symbol] < self.limit:
            return True
        if symbol not in self._exhausted_logged:
            self._exhausted_logged.add(symbol)
            logger.warning(
                f"[{self.owner}] {symbol} reached {self.limit} quote orders "
                "in this 10-minute exchange window; holding new quotes "
                "until it rolls"
            )
        return False

    def record(self, symbol: str, wall_time: float) -> None:
        self._roll(wall_time)
        self._counts[symbol] += 1

    def count(self, symbol: str, wall_time: float) -> int:
        self._roll(wall_time)
        return self._counts[symbol]


@dataclass(frozen=True, slots=True)
class QueueRetention:
    policy: QueueRetentionPolicy
    budget: QuoteOrderBudget

    @classmethod
    def from_config(cls, config, *, owner: str = "") -> "QueueRetention":
        policy = QueueRetentionPolicy.from_config(config)
        return cls(
            policy=policy,
            budget=QuoteOrderBudget(
                policy.max_new_orders_per_symbol_per_10min,
                owner=owner,
            ),
        )


__all__ = [
    "EXCHANGE_RULE_WINDOW_SEC",
    "KEEP",
    "QueueRetention",
    "QueueRetentionPolicy",
    "QuoteOrderBudget",
    "REQUOTE",
    "RISK_REQUOTE",
]
