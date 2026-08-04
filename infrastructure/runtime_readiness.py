"""Authoritative runtime readiness decision and immutable snapshot."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Callable


_READY = "ready"
_UNREADY = "unready"
_UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RuntimeReadinessSnapshot:
    """One coherent readiness decision shared by every runtime consumer."""

    ready: bool
    phase: str
    execution_enabled: bool
    operating_mode: str
    reasons: tuple[str, ...]
    components: tuple[tuple[str, str], ...]
    evaluated_at_monotonic: float

    def component_state(self, name: str) -> str:
        return dict(self.components).get(str(name), _UNKNOWN)

    def as_dict(self) -> dict:
        return {
            "ready": self.ready,
            "status": _READY if self.ready else "not_ready",
            "phase": self.phase,
            "execution_enabled": self.execution_enabled,
            "operating_mode": self.operating_mode,
            "reasons": list(self.reasons),
            "components": dict(self.components),
            "evaluated_at_monotonic": self.evaluated_at_monotonic,
        }


class RuntimeReadinessEvaluator:
    """Apply one fail-closed readiness policy to component observations."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._monotonic = monotonic

    def evaluate(
        self,
        components: Mapping[str, bool | None],
        *,
        required: Sequence[str],
        phase: str,
        execution_enabled: bool,
        require_execution: bool,
        operating_mode: str = "",
        component_reasons: Mapping[str, str] | None = None,
    ) -> RuntimeReadinessSnapshot:
        reasons_by_component = component_reasons or {}
        normalized: list[tuple[str, str]] = []
        reasons: list[str] = []
        required_names = tuple(dict.fromkeys(str(name) for name in required))

        for name in required_names:
            value = components.get(name)
            if value is True:
                state = _READY
            elif value is False:
                state = _UNREADY
                reasons.append(
                    str(reasons_by_component.get(name) or f"{name}_unready")
                )
            else:
                state = _UNKNOWN
                reasons.append(
                    str(reasons_by_component.get(name) or f"{name}_unknown")
                )
            normalized.append((name, state))

        if require_execution and not execution_enabled:
            reasons.append("execution_disabled")

        return RuntimeReadinessSnapshot(
            ready=not reasons,
            phase=str(phase or "unknown"),
            execution_enabled=bool(execution_enabled),
            operating_mode=str(operating_mode or ""),
            reasons=tuple(dict.fromkeys(reasons)),
            components=tuple(normalized),
            evaluated_at_monotonic=float(self._monotonic()),
        )


class RuntimeReadinessController:
    """Own mutable observations while delegating every decision to evaluator."""

    def __init__(self, evaluator: RuntimeReadinessEvaluator) -> None:
        self.evaluator = evaluator
        self.components: dict[str, bool | None] = {}
        self.required: tuple[str, ...] = ()

    def configure(self, *, paper_trade: bool) -> None:
        self.components = {
            "clock": None,
            "transport": None,
            "risk_supervisor": None,
            "truth": None,
            "account_truth": True if paper_trade else None,
            "risk_policy": None,
            "market_data": None,
            "alerts": None,
            "evidence": True if paper_trade else None,
            "resources": None,
            "oms": None,
            "execution": False,
        }
        required = [
            "clock",
            "transport",
            "risk_supervisor",
            "truth",
            "account_truth",
            "risk_policy",
            "market_data",
            "alerts",
            "resources",
            "oms",
        ]
        if not paper_trade:
            required.append("evidence")
        self.required = tuple(required)

    def evaluate(
        self,
        phase: str,
        *,
        require_execution: bool,
        operating_mode: str,
    ) -> RuntimeReadinessSnapshot:
        return self.evaluator.evaluate(
            self.components,
            required=self.required,
            phase=phase,
            execution_enabled=bool(self.components.get("execution", False)),
            require_execution=require_execution,
            operating_mode=operating_mode,
        )


__all__ = [
    "RuntimeReadinessController",
    "RuntimeReadinessEvaluator",
    "RuntimeReadinessSnapshot",
]
