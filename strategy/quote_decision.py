"""Pure execution-policy decisions applied to continuous model quotes."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from strategy.quote_math import QuoteOffsets, depths_bps_to_prices


@dataclass(frozen=True, slots=True)
class QuoteDecisionInput:
    reference_price: float
    best_bid: float
    best_ask: float
    tick_size: float
    configured_min_spread_bps: float
    passive_fee_bps: float
    formula_quote: QuoteOffsets


@dataclass(frozen=True, slots=True)
class QuoteDecision:
    target_bid: float
    target_ask: float
    quote_center_price: float
    effective_min_spread_bps: float
    effective_half_spread_bps: float
    effective_bid_depth_bps: float
    effective_ask_depth_bps: float
    post_only_adjusted: bool

    def spread_bps(self, mid_price: float) -> float:
        mid = _positive_finite(mid_price, "mid_price")
        return (self.target_ask - self.target_bid) / mid * 10_000.0


def _finite(value: object, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be finite") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{field} must be finite")
    return parsed


def _positive_finite(value: object, field: str) -> float:
    parsed = _finite(value, field)
    if parsed <= 0.0:
        raise ValueError(f"{field} must be positive")
    return parsed


def _nonnegative_finite(value: object, field: str) -> float:
    parsed = _finite(value, field)
    if parsed < 0.0:
        raise ValueError(f"{field} must be nonnegative")
    return parsed


def _round_to_tick(price: float, tick_size: float, *, up: bool) -> float:
    price_decimal = Decimal(str(price))
    tick_decimal = Decimal(str(tick_size))
    rounding = ROUND_CEILING if up else ROUND_FLOOR
    ticks = (price_decimal / tick_decimal).to_integral_value(
        rounding=rounding
    )
    return float(ticks * tick_decimal)


class QuoteDecisionEngine:
    """Convert model offsets into passive, tick-valid executable prices."""

    __slots__ = ()

    def decide(self, request: QuoteDecisionInput) -> QuoteDecision | None:
        if not isinstance(request, QuoteDecisionInput):
            raise TypeError("quote decision input is required")
        reference_price = _positive_finite(
            request.reference_price,
            "reference_price",
        )
        best_bid = _positive_finite(request.best_bid, "best_bid")
        best_ask = _positive_finite(request.best_ask, "best_ask")
        tick_size = _positive_finite(request.tick_size, "tick_size")
        if best_bid >= best_ask:
            raise ValueError("best_bid must be below best_ask")
        configured_min_spread_bps = _nonnegative_finite(
            request.configured_min_spread_bps,
            "configured_min_spread_bps",
        )
        passive_fee_bps = _nonnegative_finite(
            request.passive_fee_bps,
            "passive_fee_bps",
        )
        formula_half_spread_bps = _nonnegative_finite(
            request.formula_quote.half_spread_bps,
            "formula_half_spread_bps",
        )
        center_offset_bps = _finite(
            request.formula_quote.center_offset_bps,
            "center_offset_bps",
        )

        effective_min_spread_bps = max(
            configured_min_spread_bps,
            passive_fee_bps,
        )
        effective_half_spread_bps = max(
            formula_half_spread_bps,
            effective_min_spread_bps / 2.0,
        )
        bid_depth_bps = effective_half_spread_bps - center_offset_bps
        ask_depth_bps = effective_half_spread_bps + center_offset_bps
        target_bid, target_ask = depths_bps_to_prices(
            reference_price,
            bid_depth_bps,
            ask_depth_bps,
        )
        target_bid = _round_to_tick(target_bid, tick_size, up=False)
        target_ask = _round_to_tick(target_ask, tick_size, up=True)

        post_only_adjusted = False
        if target_bid >= best_ask:
            target_bid = best_ask - tick_size
            post_only_adjusted = True
        if target_ask <= best_bid:
            target_ask = best_bid + tick_size
            post_only_adjusted = True
        if (
            not math.isfinite(target_bid)
            or not math.isfinite(target_ask)
            or target_bid <= 0.0
            or target_bid >= target_ask
        ):
            return None

        try:
            quote_center_price = reference_price * math.exp(
                center_offset_bps / 10_000.0
            )
        except OverflowError as exc:
            raise ValueError("quote center is non-finite") from exc
        if not math.isfinite(quote_center_price) or quote_center_price <= 0.0:
            raise ValueError("quote center is non-finite")
        return QuoteDecision(
            target_bid=target_bid,
            target_ask=target_ask,
            quote_center_price=quote_center_price,
            effective_min_spread_bps=effective_min_spread_bps,
            effective_half_spread_bps=effective_half_spread_bps,
            effective_bid_depth_bps=bid_depth_bps,
            effective_ask_depth_bps=ask_depth_bps,
            post_only_adjusted=post_only_adjusted,
        )


__all__ = [
    "QuoteDecision",
    "QuoteDecisionEngine",
    "QuoteDecisionInput",
]
