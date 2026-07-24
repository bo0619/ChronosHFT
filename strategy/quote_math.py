"""Validated continuous quote equations in log-bps and seconds.

This module deliberately contains no strategy, OMS, gateway, tick-size, or
minimum-spread policy.  Inventory is measured in fixed notional lots and all
returned prices are continuous values for the execution layer to constrain.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from numbers import Real


UNITS_VERSION = "chronoshft.log_bps_seconds_fixed_notional_lot.v1"
AS_FORMULA_VERSION = "avellaneda_stoikov.log_bps_finite_horizon.v1"
GLFT_FORMULA_VERSION = "glft.log_bps_asymptotic_model_a.v2"


@dataclass(frozen=True, slots=True)
class QuoteOffsets:
    """Continuous two-sided quote represented relative to the current mid."""

    bid_depth_bps: float
    ask_depth_bps: float
    center_offset_bps: float
    half_spread_bps: float
    bid_price: float
    ask_price: float


def depths_bps_to_prices(
    mid_price: float,
    bid_depth_bps: float,
    ask_depth_bps: float,
) -> tuple[float, float]:
    """Convert log-bps depths to continuous bid and ask prices.

    A negative depth is valid at the formula boundary: sufficiently large
    inventory can move a quote through the mid.  Whether to suppress or
    constrain that side belongs to execution policy.
    """

    mid = _positive_finite(mid_price, "mid_price")
    bid_depth = _finite_real(bid_depth_bps, "bid_depth_bps")
    ask_depth = _finite_real(ask_depth_bps, "ask_depth_bps")

    try:
        bid_price = mid * math.exp(-bid_depth / 10_000.0)
        ask_price = mid * math.exp(ask_depth / 10_000.0)
    except OverflowError as exc:
        raise ValueError("log-bps depth produces a non-finite price") from exc

    return (
        _positive_finite(bid_price, "bid_price"),
        _positive_finite(ask_price, "ask_price"),
    )


def as_quote_offsets(
    mid_price: float,
    inventory_lots: float,
    sigma_bps_sqrt_s: float,
    gamma_per_bps: float,
    k_per_bps: float,
    horizon_s: float,
) -> QuoteOffsets:
    """Avellaneda-Stoikov finite-horizon quotes in log-bps.

    ``sigma_bps_sqrt_s`` is volatility in bps/sqrt(second), ``gamma_per_bps``
    and ``k_per_bps`` are inverse bps, and ``horizon_s`` is seconds remaining.
    """

    mid = _positive_finite(mid_price, "mid_price")
    inventory = _finite_real(inventory_lots, "inventory_lots")
    sigma = _positive_finite(sigma_bps_sqrt_s, "sigma_bps_sqrt_s")
    gamma = _positive_finite(gamma_per_bps, "gamma_per_bps")
    k = _positive_finite(k_per_bps, "k_per_bps")
    horizon = _positive_finite(horizon_s, "horizon_s")

    sigma_squared = _positive_finite(sigma * sigma, "sigma_bps_sqrt_s squared")
    risk_bps = _positive_finite(
        gamma * sigma_squared * horizon,
        "A-S risk term",
    )
    gamma_over_k = _positive_finite(gamma / k, "gamma_per_bps / k_per_bps")
    liquidity_half_spread_bps = _positive_finite(
        math.log1p(gamma_over_k) / gamma,
        "A-S liquidity half-spread",
    )
    half_spread_bps = _positive_finite(
        0.5 * risk_bps + liquidity_half_spread_bps,
        "A-S half-spread",
    )
    center_offset_bps = _finite_real(
        -inventory * risk_bps,
        "A-S center offset",
    )

    return _build_quote_offsets(
        mid,
        half_spread_bps=half_spread_bps,
        center_offset_bps=center_offset_bps,
    )


def glft_quote_offsets(
    mid_price: float,
    inventory_lots: float,
    sigma_bps_sqrt_s: float,
    gamma_per_bps: float,
    A_per_s: float,
    k_per_bps: float,
    order_size_lots: float = 1.0,
) -> QuoteOffsets:
    """GLFT asymptotic Model A quotes with ``xi = gamma``.

    ``A_per_s`` is the zero-depth fill intensity per second and
    ``order_size_lots`` is the fixed order size Delta in inventory lots.
    """

    mid = _positive_finite(mid_price, "mid_price")
    inventory = _finite_real(inventory_lots, "inventory_lots")
    sigma = _positive_finite(sigma_bps_sqrt_s, "sigma_bps_sqrt_s")
    gamma = _positive_finite(gamma_per_bps, "gamma_per_bps")
    intensity = _positive_finite(A_per_s, "A_per_s")
    k = _positive_finite(k_per_bps, "k_per_bps")
    order_size = _positive_finite(order_size_lots, "order_size_lots")

    gamma_delta = _positive_finite(
        gamma * order_size,
        "gamma_per_bps * order_size_lots",
    )
    ratio = _positive_finite(
        gamma_delta / k,
        "gamma_per_bps * order_size_lots / k_per_bps",
    )
    c1_bps = _positive_finite(
        math.log1p(ratio) / gamma_delta,
        "GLFT c1",
    )

    exponent = _positive_finite(
        k / gamma_delta + 1.0,
        "GLFT c2 exponent",
    )
    log_c2_squared = _finite_real(
        math.log(gamma)
        - math.log(2.0)
        - math.log(intensity)
        - math.log(order_size)
        - math.log(k)
        + exponent * math.log1p(ratio),
        "log(GLFT c2 squared)",
    )
    try:
        c2_sqrt_s = math.exp(0.5 * log_c2_squared)
    except OverflowError as exc:
        raise ValueError("GLFT c2 is not finite and positive") from exc
    c2_sqrt_s = _positive_finite(c2_sqrt_s, "GLFT c2")

    half_spread_bps = _positive_finite(
        c1_bps + 0.5 * order_size * sigma * c2_sqrt_s,
        "GLFT half-spread",
    )
    inventory_skew_bps = _finite_real(
        inventory * sigma * c2_sqrt_s,
        "GLFT inventory skew",
    )

    return _build_quote_offsets(
        mid,
        half_spread_bps=half_spread_bps,
        center_offset_bps=-inventory_skew_bps,
    )


def _build_quote_offsets(
    mid_price: float,
    *,
    half_spread_bps: float,
    center_offset_bps: float,
) -> QuoteOffsets:
    half_spread = _positive_finite(half_spread_bps, "half_spread_bps")
    center_offset = _finite_real(center_offset_bps, "center_offset_bps")
    bid_depth = _finite_real(
        half_spread - center_offset,
        "bid_depth_bps",
    )
    ask_depth = _finite_real(
        half_spread + center_offset,
        "ask_depth_bps",
    )
    bid_price, ask_price = depths_bps_to_prices(
        mid_price,
        bid_depth,
        ask_depth,
    )
    if not bid_price < ask_price:
        raise ValueError("quote prices must satisfy bid_price < ask_price")

    return QuoteOffsets(
        bid_depth_bps=bid_depth,
        ask_depth_bps=ask_depth,
        center_offset_bps=center_offset,
        half_spread_bps=half_spread,
        bid_price=bid_price,
        ask_price=ask_price,
    )


def _positive_finite(value: object, name: str) -> float:
    result = _finite_real(value, name)
    if result <= 0.0:
        raise ValueError(f"{name} must be greater than zero")
    return result


def _finite_real(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite real number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite real number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite real number")
    return result
