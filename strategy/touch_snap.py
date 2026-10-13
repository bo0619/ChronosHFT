"""Paper-only touch snapping for large-tick markets.

The quote decision floors bids and ceils asks to the tick. When one tick is
wide in bps (KORUUSDT: 0.01 at ~14 USDT is ~7 bps) and the model's spread is
only slightly wider than the market's, both quotes land one tick behind the
touch. They then fill only when the price trades through them, which selects
adverse fills.

``snap_to_touch`` moves a side onto the touch when the model's continuous
price is less than ``snap_ticks`` ticks behind it, provided that side still
earns at least the one-way passive fee against the reference price and the
quoted spread stays at least the effective minimum. A model price further
behind (for example an inventory skew of more than ``snap_ticks``) is left
alone, so inventory control still works. Live never snaps.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from strategy.quote_math import depths_bps_to_prices

# Tolerance for float comparisons of prices and bps.
_EPSILON = 1e-12


def touch_snap_ticks(execution_config: object, *, live: bool) -> float:
    """Return the configured Paper snap distance in ticks, 0 when disabled."""
    if live or not isinstance(execution_config, Mapping):
        return 0.0
    try:
        value = float(execution_config.get("paper_touch_snap_ticks", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0.0 else 0.0


def snap_to_touch(
    *,
    target_bid: float,
    target_ask: float,
    reference_price: float,
    bid_depth_bps: float,
    ask_depth_bps: float,
    best_bid: float,
    best_ask: float,
    tick_size: float,
    snap_ticks: float,
    min_spread_bps: float,
    passive_fee_bps: float,
) -> tuple[float, float]:
    """Return the bid and ask after snapping eligible sides to the touch.

    ``passive_fee_bps`` is the round-trip passive fee, so each side must clear
    half of it.
    """
    if snap_ticks <= 0.0 or tick_size <= 0.0 or reference_price <= 0.0:
        return target_bid, target_ask
    model_bid, model_ask = depths_bps_to_prices(
        reference_price,
        bid_depth_bps,
        ask_depth_bps,
    )
    tolerance = snap_ticks * tick_size
    one_way_fee_bps = max(0.0, passive_fee_bps) / 2.0

    def edge_bps(price: float, side_sign: float) -> float:
        return side_sign * (reference_price - price) / reference_price * 10_000.0

    # Gap in ticks between the model price and the touch, if snapping is
    # allowed on that side.
    bid_gap = None
    if (
        target_bid < best_bid - _EPSILON
        and best_bid - model_bid < tolerance - _EPSILON
        and edge_bps(best_bid, 1.0) > one_way_fee_bps + _EPSILON
    ):
        bid_gap = (best_bid - model_bid) / tick_size
    ask_gap = None
    if (
        target_ask > best_ask + _EPSILON
        and model_ask - best_ask < tolerance - _EPSILON
        and edge_bps(best_ask, -1.0) > one_way_fee_bps + _EPSILON
    ):
        ask_gap = (model_ask - best_ask) / tick_size

    def spread_ok(bid: float, ask: float) -> bool:
        spread_bps = (ask - bid) / reference_price * 10_000.0
        return bid < ask and spread_bps >= min_spread_bps - _EPSILON

    candidates = []
    if bid_gap is not None and ask_gap is not None:
        candidates.append((best_bid, best_ask))
    # One side only: the one whose model price is closer to its touch first.
    sides = []
    if bid_gap is not None:
        sides.append((bid_gap, (best_bid, target_ask)))
    if ask_gap is not None:
        sides.append((ask_gap, (target_bid, best_ask)))
    candidates.extend(prices for _gap, prices in sorted(sides, key=lambda item: item[0]))
    for bid, ask in candidates:
        if spread_ok(bid, ask):
            return bid, ask
    return target_bid, target_ask


def snap_decision_to_touch(
    decision,
    *,
    reference_price: float,
    best_bid: float,
    best_ask: float,
    tick_size: float,
    passive_fee_bps: float,
    execution_config: object,
    live: bool,
) -> tuple[float, float]:
    """Apply :func:`snap_to_touch` to a ``QuoteDecision``."""
    return snap_to_touch(
        target_bid=decision.target_bid,
        target_ask=decision.target_ask,
        reference_price=reference_price,
        bid_depth_bps=decision.effective_bid_depth_bps,
        ask_depth_bps=decision.effective_ask_depth_bps,
        best_bid=best_bid,
        best_ask=best_ask,
        tick_size=tick_size,
        snap_ticks=touch_snap_ticks(execution_config, live=live),
        min_spread_bps=decision.effective_min_spread_bps,
        passive_fee_bps=passive_fee_bps,
    )


__all__ = ["snap_decision_to_touch", "snap_to_touch", "touch_snap_ticks"]
