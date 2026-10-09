"""Per-venue market-making execution mode.

``post_only`` rests passive GTX/RPI quotes (the existing behavior).
``market`` keeps no resting quotes and instead sends a MARKET order when the
model's inventory-adjusted reservation price is far enough beyond the touch
to pay the spread, the taker fee and a configured minimum edge.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from event.type import Side
from infrastructure.venue import (
    SUPPORTED_VENUES,
    VENUE_BINANCE,
    VENUE_GRVT,
    VENUE_LIGHTER,
)

EXECUTION_MODE_POST_ONLY = "post_only"
EXECUTION_MODE_MARKET = "market"
EXECUTION_MODES = (EXECUTION_MODE_POST_ONLY, EXECUTION_MODE_MARKET)


DEFAULT_MARKET_MIN_EDGE_BPS = 1.0
DEFAULT_MARKET_COOLDOWN_MS = 1000.0


def resolve_execution_mode(
    strategy_config: Mapping | None,
    venue: str = VENUE_BINANCE,
) -> str:
    """Return the configured mode for ``venue``; absent means post-only."""
    config = strategy_config if isinstance(strategy_config, Mapping) else {}
    modes = config.get("execution_modes", {})
    if modes is None:
        modes = {}
    if not isinstance(modes, Mapping):
        raise ValueError("strategy.execution_modes must be an object")
    venue_key = str(venue or "").strip().lower()
    if venue_key not in SUPPORTED_VENUES:
        raise ValueError(f"unsupported execution venue: {venue!r}")
    unknown = sorted(
        str(key) for key in modes if str(key).lower() not in SUPPORTED_VENUES
    )
    if unknown:
        raise ValueError(
            "strategy.execution_modes has unsupported venues: "
            + ", ".join(unknown)
        )
    mode = str(
        modes.get(venue_key, EXECUTION_MODE_POST_ONLY)
        or EXECUTION_MODE_POST_ONLY
    ).strip().lower()
    if mode not in EXECUTION_MODES:
        raise ValueError(
            f"strategy.execution_modes.{venue_key} must be one of "
            + ", ".join(EXECUTION_MODES)
        )
    return mode


@dataclass(frozen=True, slots=True)
class MarketExecutionPolicy:
    min_edge_bps: float = DEFAULT_MARKET_MIN_EDGE_BPS
    cooldown_ms: float = DEFAULT_MARKET_COOLDOWN_MS

    @classmethod
    def from_config(cls, strategy_config: Mapping | None):
        config = strategy_config if isinstance(strategy_config, Mapping) else {}
        section = config.get("market_execution", {}) or {}
        if not isinstance(section, Mapping):
            raise ValueError("strategy.market_execution must be an object")
        values = {}
        for field, default in (
            ("min_edge_bps", DEFAULT_MARKET_MIN_EDGE_BPS),
            ("cooldown_ms", DEFAULT_MARKET_COOLDOWN_MS),
        ):
            try:
                value = float(section.get(field, default))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"strategy.market_execution.{field} must be finite"
                ) from exc
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(
                    f"strategy.market_execution.{field} must be "
                    "finite and nonnegative"
                )
            values[field] = value
        return cls(**values)


@dataclass(frozen=True, slots=True)
class MarketOrderDecision:
    side: Side | None
    buy_edge_bps: float
    sell_edge_bps: float


def decide_market_order(
    *,
    reservation_price: float,
    best_bid: float,
    best_ask: float,
    taker_fee_bps: float,
    min_edge_bps: float,
) -> MarketOrderDecision:
    """Pick the side whose net taker edge clears ``min_edge_bps``.

    Buying at the ask is worth ``reservation - ask`` and selling at the bid is
    worth ``bid - reservation``, both net of the taker fee. At most one side
    can be positive because ``bid < ask``.
    """
    values = (reservation_price, best_bid, best_ask)
    if not all(math.isfinite(value) and value > 0.0 for value in values):
        raise ValueError("market decision prices must be positive and finite")
    if best_bid >= best_ask:
        raise ValueError("best_bid must be below best_ask")
    fee = max(0.0, float(taker_fee_bps))
    buy_edge = (reservation_price - best_ask) / reservation_price * 10_000.0
    sell_edge = (best_bid - reservation_price) / reservation_price * 10_000.0
    buy_edge -= fee
    sell_edge -= fee
    side = None
    if buy_edge >= min_edge_bps and buy_edge > 0.0:
        side = Side.BUY
    elif sell_edge >= min_edge_bps and sell_edge > 0.0:
        side = Side.SELL
    return MarketOrderDecision(
        side=side,
        buy_edge_bps=buy_edge,
        sell_edge_bps=sell_edge,
    )


__all__ = [
    "DEFAULT_MARKET_COOLDOWN_MS",
    "DEFAULT_MARKET_MIN_EDGE_BPS",
    "EXECUTION_MODES",
    "EXECUTION_MODE_MARKET",
    "EXECUTION_MODE_POST_ONLY",
    "MarketExecutionPolicy",
    "MarketOrderDecision",
    "SUPPORTED_VENUES",
    "VENUE_BINANCE",
    "VENUE_GRVT",
    "VENUE_LIGHTER",
    "decide_market_order",
    "resolve_execution_mode",
]
