"""Shared pure composition of adaptive market-making signals."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass


def _positive(value: object, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be positive and finite") from exc
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise ValueError(f"{field} must be positive and finite")
    return parsed


def _nonnegative(value: object, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be nonnegative and finite") from exc
    if not math.isfinite(parsed) or parsed < 0.0:
        raise ValueError(f"{field} must be nonnegative and finite")
    return parsed


@dataclass(frozen=True, slots=True)
class AdaptivePipelineInput:
    base_A_per_s: float
    base_k_per_bps: float
    bid_A_multiplier: float
    ask_A_multiplier: float
    bid_k_multiplier: float
    ask_k_multiplier: float
    bid_hawkes_multiplier: float
    ask_hawkes_multiplier: float
    bid_markout_cost_bps: float
    ask_markout_cost_bps: float
    bid_queue_cost_bps: float
    ask_queue_cost_bps: float
    bid_flow_cost_bps: float = 0.0
    ask_flow_cost_bps: float = 0.0


@dataclass(frozen=True, slots=True)
class AdaptiveQuoteContext:
    bid_A_per_s: float
    ask_A_per_s: float
    bid_k_per_bps: float
    ask_k_per_bps: float
    bid_adverse_cost_bps: float
    ask_adverse_cost_bps: float

    def as_formula_context(self) -> dict[str, float]:
        return asdict(self)


class AdaptiveQuotePipeline:
    """Combine bounded estimator outputs without owning runtime state."""

    __slots__ = ()

    def build(self, inputs: AdaptivePipelineInput) -> AdaptiveQuoteContext:
        if not isinstance(inputs, AdaptivePipelineInput):
            raise TypeError("adaptive pipeline input is required")
        base_A = _positive(inputs.base_A_per_s, "base_A_per_s")
        base_k = _positive(inputs.base_k_per_bps, "base_k_per_bps")
        bid_A_multiplier = _positive(
            inputs.bid_A_multiplier,
            "bid_A_multiplier",
        )
        ask_A_multiplier = _positive(
            inputs.ask_A_multiplier,
            "ask_A_multiplier",
        )
        bid_k_multiplier = _positive(
            inputs.bid_k_multiplier,
            "bid_k_multiplier",
        )
        ask_k_multiplier = _positive(
            inputs.ask_k_multiplier,
            "ask_k_multiplier",
        )
        bid_hawkes = _positive(
            inputs.bid_hawkes_multiplier,
            "bid_hawkes_multiplier",
        )
        ask_hawkes = _positive(
            inputs.ask_hawkes_multiplier,
            "ask_hawkes_multiplier",
        )
        bid_adverse = sum(
            _nonnegative(value, field)
            for value, field in (
                (inputs.bid_markout_cost_bps, "bid_markout_cost_bps"),
                (inputs.bid_queue_cost_bps, "bid_queue_cost_bps"),
                (inputs.bid_flow_cost_bps, "bid_flow_cost_bps"),
            )
        )
        ask_adverse = sum(
            _nonnegative(value, field)
            for value, field in (
                (inputs.ask_markout_cost_bps, "ask_markout_cost_bps"),
                (inputs.ask_queue_cost_bps, "ask_queue_cost_bps"),
                (inputs.ask_flow_cost_bps, "ask_flow_cost_bps"),
            )
        )
        return AdaptiveQuoteContext(
            bid_A_per_s=base_A * bid_A_multiplier * bid_hawkes,
            ask_A_per_s=base_A * ask_A_multiplier * ask_hawkes,
            bid_k_per_bps=base_k * bid_k_multiplier,
            ask_k_per_bps=base_k * ask_k_multiplier,
            bid_adverse_cost_bps=bid_adverse,
            ask_adverse_cost_bps=ask_adverse,
        )


__all__ = [
    "AdaptivePipelineInput",
    "AdaptiveQuoteContext",
    "AdaptiveQuotePipeline",
]
