"""Validated continuous quote equations in log-bps and seconds.

This module deliberately contains no strategy, OMS, gateway, tick-size, or
minimum-spread policy.  Inventory is measured in fixed notional lots and all
returned prices are continuous values for the execution layer to constrain.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from numbers import Real

import numpy as np


UNITS_VERSION = "chronoshft.log_bps_seconds_fixed_notional_lot.v1"
AS_FORMULA_VERSION = "avellaneda_stoikov.log_bps_finite_horizon.v1"
GLFT_FORMULA_VERSION = "glft.log_bps_asymptotic_model_a.v2"
PORTFOLIO_GLFT_FORMULA_VERSION = (
    "glft.log_bps_riccati_portfolio_model_a.v1"
)
ADAPTIVE_GLFT_FORMULA_VERSION = (
    "glft.log_bps_finite_horizon_adaptive_portfolio_model_a.v1"
)


@dataclass(frozen=True, slots=True)
class QuoteOffsets:
    """Continuous two-sided quote represented relative to the current mid."""

    bid_depth_bps: float
    ask_depth_bps: float
    center_offset_bps: float
    half_spread_bps: float
    bid_price: float
    ask_price: float


@dataclass(frozen=True, slots=True)
class PortfolioQuoteSolution:
    """Joint GLFT quotes and the convex inventory-risk geometry behind them."""

    quotes: tuple[QuoteOffsets, ...]
    risk_curvature_bps: tuple[tuple[float, ...], ...]
    marginal_inventory_risk_bps: tuple[float, ...]
    inventory_penalty_bps: float
    c1_bps: tuple[float, ...]
    c2_sqrt_s: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class AdaptivePortfolioQuoteSolution:
    """Side-specific finite-horizon portfolio GLFT solution."""

    quotes: tuple[QuoteOffsets, ...]
    risk_curvature_bps: tuple[tuple[float, ...], ...]
    marginal_inventory_risk_bps: tuple[float, ...]
    inventory_penalty_bps: float
    bid_c1_bps: tuple[float, ...]
    ask_c1_bps: tuple[float, ...]
    bid_c2_sqrt_s: tuple[float, ...]
    ask_c2_sqrt_s: tuple[float, ...]
    effective_c2_sqrt_s: tuple[float, ...]
    horizon_s: float | None


@dataclass(frozen=True, slots=True)
class GLFTQuoteScenario:
    """One bounded parameter scenario evaluated by robust GLFT quoting."""

    name: str
    bid_A_per_s: Sequence[float]
    ask_A_per_s: Sequence[float]
    bid_k_per_bps: Sequence[float]
    ask_k_per_bps: Sequence[float]
    covariance_bps2_per_s: Sequence[Sequence[float]]
    bid_adverse_cost_bps: Sequence[float]
    ask_adverse_cost_bps: Sequence[float]


@dataclass(frozen=True, slots=True)
class RobustPortfolioQuoteSolution:
    """Per-side conservative envelope across explicitly evaluated scenarios."""

    quotes: tuple[QuoteOffsets, ...]
    scenario_solutions: tuple[AdaptivePortfolioQuoteSolution, ...]
    scenario_names: tuple[str, ...]
    selected_bid_scenario: tuple[str, ...]
    selected_ask_scenario: tuple[str, ...]


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

    c1_bps, c2_sqrt_s = _glft_liquidity_terms(
        gamma_per_bps=gamma,
        A_per_s=intensity,
        k_per_bps=k,
        order_size_lots=order_size,
    )

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


def portfolio_glft_quote_offsets(
    *,
    mid_prices: Sequence[float],
    inventory_lots: Sequence[float],
    covariance_bps2_per_s: Sequence[Sequence[float]],
    gamma_per_bps: float,
    A_per_s: Sequence[float],
    k_per_bps: Sequence[float],
    order_size_lots: Sequence[float],
) -> PortfolioQuoteSolution:
    """Asymptotic multi-asset GLFT quotes from a Riccati risk potential.

    Independent exponential fill processes produce one scalar liquidity time
    scale ``c2_i`` per asset.  With ``D = diag(c2_i**2)`` and instantaneous
    log-return covariance ``Sigma``, the unique symmetric positive-semidefinite
    inventory curvature ``H`` solves ``H D^-1 H = Sigma``.  The quadratic
    certainty-equivalent penalty is ``Phi(q) = 0.5 q' H q``.
    Covariance axes must use one common fixed-notional inventory-lot value.

    Bid and ask risk charges are exact finite differences of ``Phi`` for an
    order of size ``Delta_i``.  For one asset, or diagonal covariance, this
    reduces exactly to the scalar GLFT Model A formula above.
    """

    mids = _finite_vector(mid_prices, "mid_prices", positive=True)
    size = len(mids)
    if size == 0:
        raise ValueError("portfolio GLFT requires at least one asset")
    inventories = _finite_vector(
        inventory_lots,
        "inventory_lots",
        expected_size=size,
    )
    gamma = _positive_finite(gamma_per_bps, "gamma_per_bps")
    intensities = _finite_vector(
        A_per_s,
        "A_per_s",
        expected_size=size,
        positive=True,
    )
    ks = _finite_vector(
        k_per_bps,
        "k_per_bps",
        expected_size=size,
        positive=True,
    )
    order_sizes = _finite_vector(
        order_size_lots,
        "order_size_lots",
        expected_size=size,
        positive=True,
    )
    covariance = _covariance_matrix(covariance_bps2_per_s, size)

    liquidity_terms = tuple(
        _glft_liquidity_terms(
            gamma_per_bps=gamma,
            A_per_s=intensities[index],
            k_per_bps=ks[index],
            order_size_lots=order_sizes[index],
        )
        for index in range(size)
    )
    c1 = np.asarray([terms[0] for terms in liquidity_terms], dtype=float)
    c2 = np.asarray([terms[1] for terms in liquidity_terms], dtype=float)
    d_sqrt = np.diag(c2)
    d_inverse_sqrt = np.diag(1.0 / c2)

    normalized_covariance = d_inverse_sqrt @ covariance @ d_inverse_sqrt
    normalized_covariance = 0.5 * (
        normalized_covariance + normalized_covariance.T
    )
    eigenvalues, eigenvectors = np.linalg.eigh(normalized_covariance)
    covariance_scale = max(1.0, float(np.linalg.norm(covariance, ord=2)))
    eigen_tolerance = 1e-10 * covariance_scale
    if float(eigenvalues.min()) < -eigen_tolerance:
        raise ValueError("covariance_bps2_per_s must be positive semidefinite")
    normalized_square_root = (
        eigenvectors
        @ np.diag(np.sqrt(np.clip(eigenvalues, 0.0, None)))
        @ eigenvectors.T
    )
    curvature = d_sqrt @ normalized_square_root @ d_sqrt
    curvature = 0.5 * (curvature + curvature.T)
    if not np.isfinite(curvature).all():
        raise ValueError("portfolio GLFT risk curvature is not finite")

    d_inverse = np.diag(1.0 / np.square(c2))
    reconstructed_covariance = curvature @ d_inverse @ curvature
    if not np.allclose(
        reconstructed_covariance,
        covariance,
        rtol=1e-8,
        atol=1e-10 * covariance_scale,
    ):
        raise ValueError("portfolio GLFT Riccati solution is numerically invalid")

    inventory = np.asarray(inventories, dtype=float)
    marginal_risk = curvature @ inventory
    inventory_penalty = 0.5 * float(inventory @ marginal_risk)
    if inventory_penalty < -1e-10 * covariance_scale:
        raise ValueError("portfolio GLFT inventory penalty is negative")
    inventory_penalty = max(0.0, inventory_penalty)

    quotes = []
    for index in range(size):
        half_spread = c1[index] + (
            0.5 * order_sizes[index] * curvature[index, index]
        )
        quotes.append(
            _build_quote_offsets(
                mids[index],
                half_spread_bps=float(half_spread),
                center_offset_bps=-float(marginal_risk[index]),
            )
        )

    return PortfolioQuoteSolution(
        quotes=tuple(quotes),
        risk_curvature_bps=tuple(
            tuple(float(value) for value in row) for row in curvature
        ),
        marginal_inventory_risk_bps=tuple(
            float(value) for value in marginal_risk
        ),
        inventory_penalty_bps=inventory_penalty,
        c1_bps=tuple(float(value) for value in c1),
        c2_sqrt_s=tuple(float(value) for value in c2),
    )


def adaptive_portfolio_glft_quote_offsets(
    *,
    mid_prices: Sequence[float],
    inventory_lots: Sequence[float],
    covariance_bps2_per_s: Sequence[Sequence[float]],
    gamma_per_bps: float,
    bid_A_per_s: Sequence[float],
    ask_A_per_s: Sequence[float],
    bid_k_per_bps: Sequence[float],
    ask_k_per_bps: Sequence[float],
    order_size_lots: Sequence[float],
    bid_adverse_cost_bps: Sequence[float],
    ask_adverse_cost_bps: Sequence[float],
    horizon_s: float | None,
) -> AdaptivePortfolioQuoteSolution:
    """Solve side-specific GLFT with finite-horizon portfolio inventory risk.

    For side liquidity curvatures ``c2_b`` and ``c2_a``, the symmetric
    quadratic Hamiltonian uses
    ``D_eff^-1 = 0.5 * (D_b^-1 + D_a^-1)``.  This exactly recovers the
    original GLFT ``D = diag(c2**2)`` when both sides are equal.

    With time-to-horizon ``T``, the inventory curvature solves
    ``dH/dT = Sigma - H D_eff^-1 H`` and ``H(0) = 0``.  Passing ``None`` uses
    the asymptotic algebraic Riccati solution.
    """

    mids = _finite_vector(mid_prices, "mid_prices", positive=True)
    size = len(mids)
    if size == 0:
        raise ValueError("adaptive portfolio GLFT requires at least one asset")
    inventories = _finite_vector(
        inventory_lots,
        "inventory_lots",
        expected_size=size,
    )
    gamma = _positive_finite(gamma_per_bps, "gamma_per_bps")
    bid_A = _finite_vector(
        bid_A_per_s,
        "bid_A_per_s",
        expected_size=size,
        positive=True,
    )
    ask_A = _finite_vector(
        ask_A_per_s,
        "ask_A_per_s",
        expected_size=size,
        positive=True,
    )
    bid_k = _finite_vector(
        bid_k_per_bps,
        "bid_k_per_bps",
        expected_size=size,
        positive=True,
    )
    ask_k = _finite_vector(
        ask_k_per_bps,
        "ask_k_per_bps",
        expected_size=size,
        positive=True,
    )
    order_sizes = _finite_vector(
        order_size_lots,
        "order_size_lots",
        expected_size=size,
        positive=True,
    )
    bid_adverse = _finite_vector(
        bid_adverse_cost_bps,
        "bid_adverse_cost_bps",
        expected_size=size,
    )
    ask_adverse = _finite_vector(
        ask_adverse_cost_bps,
        "ask_adverse_cost_bps",
        expected_size=size,
    )
    if min((*bid_adverse, *ask_adverse)) < 0.0:
        raise ValueError("adverse selection costs must be nonnegative")
    covariance = _covariance_matrix(covariance_bps2_per_s, size)
    if horizon_s is None:
        parsed_horizon = None
    else:
        parsed_horizon = _finite_real(horizon_s, "horizon_s")
        if parsed_horizon < 0.0:
            raise ValueError("horizon_s must be nonnegative")

    bid_terms = tuple(
        _glft_liquidity_terms(
            gamma_per_bps=gamma,
            A_per_s=bid_A[index],
            k_per_bps=bid_k[index],
            order_size_lots=order_sizes[index],
        )
        for index in range(size)
    )
    ask_terms = tuple(
        _glft_liquidity_terms(
            gamma_per_bps=gamma,
            A_per_s=ask_A[index],
            k_per_bps=ask_k[index],
            order_size_lots=order_sizes[index],
        )
        for index in range(size)
    )
    bid_c1 = np.asarray([item[0] for item in bid_terms], dtype=float)
    ask_c1 = np.asarray([item[0] for item in ask_terms], dtype=float)
    bid_c2 = np.asarray([item[1] for item in bid_terms], dtype=float)
    ask_c2 = np.asarray([item[1] for item in ask_terms], dtype=float)
    effective_d_inverse = 0.5 * (
        1.0 / np.square(bid_c2) + 1.0 / np.square(ask_c2)
    )
    effective_c2 = np.sqrt(1.0 / effective_d_inverse)
    curvature = _riccati_curvature(
        covariance,
        effective_c2,
        horizon_s=parsed_horizon,
    )

    inventory = np.asarray(inventories, dtype=float)
    marginal_risk = curvature @ inventory
    inventory_penalty = max(0.0, 0.5 * float(inventory @ marginal_risk))
    quotes = []
    for index in range(size):
        diagonal_charge = 0.5 * order_sizes[index] * curvature[index, index]
        bid_depth = (
            bid_c1[index]
            + bid_adverse[index]
            + marginal_risk[index]
            + diagonal_charge
        )
        ask_depth = (
            ask_c1[index]
            + ask_adverse[index]
            - marginal_risk[index]
            + diagonal_charge
        )
        quotes.append(
            _build_quote_from_depths(
                mids[index],
                bid_depth_bps=float(bid_depth),
                ask_depth_bps=float(ask_depth),
            )
        )

    return AdaptivePortfolioQuoteSolution(
        quotes=tuple(quotes),
        risk_curvature_bps=tuple(
            tuple(float(value) for value in row) for row in curvature
        ),
        marginal_inventory_risk_bps=tuple(float(value) for value in marginal_risk),
        inventory_penalty_bps=inventory_penalty,
        bid_c1_bps=tuple(float(value) for value in bid_c1),
        ask_c1_bps=tuple(float(value) for value in ask_c1),
        bid_c2_sqrt_s=tuple(float(value) for value in bid_c2),
        ask_c2_sqrt_s=tuple(float(value) for value in ask_c2),
        effective_c2_sqrt_s=tuple(float(value) for value in effective_c2),
        horizon_s=parsed_horizon,
    )


def robust_adaptive_portfolio_glft_quote_offsets(
    *,
    mid_prices: Sequence[float],
    inventory_lots: Sequence[float],
    gamma_per_bps: float,
    order_size_lots: Sequence[float],
    horizon_s: float | None,
    scenarios: Sequence[GLFTQuoteScenario],
) -> RobustPortfolioQuoteSolution:
    """Evaluate bounded scenarios and retain the widest depth on each side."""

    if isinstance(scenarios, (str, bytes)) or not isinstance(scenarios, Sequence):
        raise ValueError("scenarios must be a non-empty sequence")
    if not scenarios:
        raise ValueError("scenarios must be a non-empty sequence")
    normalized_names = tuple(str(scenario.name or "").strip() for scenario in scenarios)
    if any(not name for name in normalized_names):
        raise ValueError("every GLFT scenario requires a name")
    if len(set(normalized_names)) != len(normalized_names):
        raise ValueError("GLFT scenario names must be unique")

    solutions = tuple(
        adaptive_portfolio_glft_quote_offsets(
            mid_prices=mid_prices,
            inventory_lots=inventory_lots,
            covariance_bps2_per_s=scenario.covariance_bps2_per_s,
            gamma_per_bps=gamma_per_bps,
            bid_A_per_s=scenario.bid_A_per_s,
            ask_A_per_s=scenario.ask_A_per_s,
            bid_k_per_bps=scenario.bid_k_per_bps,
            ask_k_per_bps=scenario.ask_k_per_bps,
            order_size_lots=order_size_lots,
            bid_adverse_cost_bps=scenario.bid_adverse_cost_bps,
            ask_adverse_cost_bps=scenario.ask_adverse_cost_bps,
            horizon_s=horizon_s,
        )
        for scenario in scenarios
    )
    mids = _finite_vector(mid_prices, "mid_prices", positive=True)
    robust_quotes = []
    selected_bid = []
    selected_ask = []
    for index, mid in enumerate(mids):
        bid_scenario_index = max(
            range(len(solutions)),
            key=lambda scenario_index: solutions[scenario_index]
            .quotes[index]
            .bid_depth_bps,
        )
        ask_scenario_index = max(
            range(len(solutions)),
            key=lambda scenario_index: solutions[scenario_index]
            .quotes[index]
            .ask_depth_bps,
        )
        robust_quotes.append(
            _build_quote_from_depths(
                mid,
                bid_depth_bps=solutions[bid_scenario_index]
                .quotes[index]
                .bid_depth_bps,
                ask_depth_bps=solutions[ask_scenario_index]
                .quotes[index]
                .ask_depth_bps,
            )
        )
        selected_bid.append(normalized_names[bid_scenario_index])
        selected_ask.append(normalized_names[ask_scenario_index])
    return RobustPortfolioQuoteSolution(
        quotes=tuple(robust_quotes),
        scenario_solutions=solutions,
        scenario_names=normalized_names,
        selected_bid_scenario=tuple(selected_bid),
        selected_ask_scenario=tuple(selected_ask),
    )


def _riccati_curvature(
    covariance: np.ndarray,
    c2_sqrt_s: np.ndarray,
    *,
    horizon_s: float | None,
) -> np.ndarray:
    d_sqrt = np.diag(c2_sqrt_s)
    d_inverse_sqrt = np.diag(1.0 / c2_sqrt_s)
    normalized_covariance = d_inverse_sqrt @ covariance @ d_inverse_sqrt
    normalized_covariance = 0.5 * (
        normalized_covariance + normalized_covariance.T
    )
    eigenvalues, eigenvectors = np.linalg.eigh(normalized_covariance)
    covariance_scale = max(1.0, float(np.linalg.norm(covariance, ord=2)))
    if float(eigenvalues.min()) < -1e-10 * covariance_scale:
        raise ValueError("covariance_bps2_per_s must be positive semidefinite")
    clipped = np.clip(eigenvalues, 0.0, None)
    roots = np.sqrt(clipped)
    if horizon_s is None:
        transformed = roots
    else:
        transformed = roots * np.tanh(roots * horizon_s)
    normalized_curvature = (
        eigenvectors @ np.diag(transformed) @ eigenvectors.T
    )
    curvature = d_sqrt @ normalized_curvature @ d_sqrt
    curvature = 0.5 * (curvature + curvature.T)
    if not np.isfinite(curvature).all():
        raise ValueError("adaptive GLFT risk curvature is not finite")
    return curvature


def _glft_liquidity_terms(
    *,
    gamma_per_bps: float,
    A_per_s: float,
    k_per_bps: float,
    order_size_lots: float,
) -> tuple[float, float]:
    gamma_delta = _positive_finite(
        gamma_per_bps * order_size_lots,
        "gamma_per_bps * order_size_lots",
    )
    ratio = _positive_finite(
        gamma_delta / k_per_bps,
        "gamma_per_bps * order_size_lots / k_per_bps",
    )
    c1_bps = _positive_finite(
        math.log1p(ratio) / gamma_delta,
        "GLFT c1",
    )
    exponent = _positive_finite(
        k_per_bps / gamma_delta + 1.0,
        "GLFT c2 exponent",
    )
    log_c2_squared = _finite_real(
        math.log(gamma_per_bps)
        - math.log(2.0)
        - math.log(A_per_s)
        - math.log(order_size_lots)
        - math.log(k_per_bps)
        + exponent * math.log1p(ratio),
        "log(GLFT c2 squared)",
    )
    try:
        c2_sqrt_s = math.exp(0.5 * log_c2_squared)
    except OverflowError as exc:
        raise ValueError("GLFT c2 is not finite and positive") from exc
    return c1_bps, _positive_finite(c2_sqrt_s, "GLFT c2")


def _build_quote_from_depths(
    mid_price: float,
    *,
    bid_depth_bps: float,
    ask_depth_bps: float,
) -> QuoteOffsets:
    bid_depth = _finite_real(bid_depth_bps, "bid_depth_bps")
    ask_depth = _finite_real(ask_depth_bps, "ask_depth_bps")
    half_spread = 0.5 * (bid_depth + ask_depth)
    center_offset = 0.5 * (ask_depth - bid_depth)
    return _build_quote_offsets(
        mid_price,
        half_spread_bps=half_spread,
        center_offset_bps=center_offset,
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


def _finite_vector(
    values: Sequence[float],
    name: str,
    *,
    expected_size: int | None = None,
    positive: bool = False,
) -> tuple[float, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError(f"{name} must be a sequence")
    parsed = tuple(
        (
            _positive_finite(value, f"{name}[{index}]")
            if positive
            else _finite_real(value, f"{name}[{index}]")
        )
        for index, value in enumerate(values)
    )
    if expected_size is not None and len(parsed) != expected_size:
        raise ValueError(f"{name} must contain {expected_size} values")
    return parsed


def _covariance_matrix(
    values: Sequence[Sequence[float]],
    size: int,
) -> np.ndarray:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError("covariance_bps2_per_s must be a square matrix")
    rows = tuple(
        _finite_vector(
            row,
            f"covariance_bps2_per_s[{index}]",
            expected_size=size,
        )
        for index, row in enumerate(values)
    )
    if len(rows) != size:
        raise ValueError(
            f"covariance_bps2_per_s must contain {size} rows"
        )
    covariance = np.asarray(rows, dtype=float)
    scale = max(1.0, float(np.max(np.abs(covariance))))
    if not np.allclose(
        covariance,
        covariance.T,
        rtol=1e-10,
        atol=1e-10 * scale,
    ):
        raise ValueError("covariance_bps2_per_s must be symmetric")
    if np.any(np.diag(covariance) <= 0.0):
        raise ValueError(
            "covariance_bps2_per_s must have a positive diagonal"
        )
    eigenvalues = np.linalg.eigvalsh(0.5 * (covariance + covariance.T))
    if float(eigenvalues.min()) < -1e-10 * scale:
        raise ValueError("covariance_bps2_per_s must be positive semidefinite")
    return 0.5 * (covariance + covariance.T)


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
