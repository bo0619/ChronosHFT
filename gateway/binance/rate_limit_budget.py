"""Compatibility exports for the host-scoped Binance rate-limit budget.

The coordinator is infrastructure shared by the parent runtime and the
independent risk sidecar.  Keep this module as a stable import path for
gateway callers while ownership lives outside the gateway package.
"""

from infrastructure.binance_rate_limit_budget import (
    DEFAULT_EMERGENCY_RESERVE,
    DEFAULT_REQUEST_WEIGHT_LIMIT,
    DEFAULT_STATE_PATH,
    DEFAULT_TRADING_RESERVE,
    BinanceRateLimitBudget,
    RateLimitDecision,
)

__all__ = [
    "DEFAULT_EMERGENCY_RESERVE",
    "DEFAULT_REQUEST_WEIGHT_LIMIT",
    "DEFAULT_STATE_PATH",
    "DEFAULT_TRADING_RESERVE",
    "BinanceRateLimitBudget",
    "RateLimitDecision",
]
