"""Stateful funding guard orchestration outside RiskManager."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable

from data.cache import data_cache
from infrastructure.oms_risk_port import RiskOMSPort
from infrastructure.time_service import time_service
from risk.funding_guard import (
    FundingGuardDecision,
    FundingGuardPolicy,
    FundingGuardState,
    FundingObservation,
    evaluate_funding_guard,
)


class FundingRiskController:
    """Own funding observations, transitions, and OMS constraints."""

    def __init__(
        self,
        *,
        root_config: dict,
        risk_config: dict,
        oms: RiskOMSPort | None,
        set_trading_mode: Callable,
        clear_trading_mode: Callable,
        tracked_symbols: Callable[[], set[str]],
        reduce_only_mode,
    ) -> None:
        self.root_config = root_config
        self.oms = oms
        self.set_trading_mode = set_trading_mode
        self.clear_trading_mode = clear_trading_mode
        self.tracked_symbols = tracked_symbols
        self.reduce_only_mode = reduce_only_mode
        funding = risk_config.get("funding_guard", {})
        funding = funding if isinstance(funding, dict) else {}
        self.policy = FundingGuardPolicy(
            enabled=bool(funding.get("enabled", False)),
            require_snapshot=bool(
                funding.get(
                    "require_snapshot",
                    funding.get("enabled", False),
                )
            ),
            max_snapshot_age_ms=float(
                funding.get("max_snapshot_age_ms", 3000.0) or 3000.0
            ),
            pre_funding_reduce_only_sec=float(
                funding.get("pre_funding_reduce_only_sec", 600.0) or 600.0
            ),
            post_funding_hold_sec=float(
                funding.get("post_funding_hold_sec", 120.0) or 120.0
            ),
            max_abs_funding_rate=float(
                funding.get("max_abs_funding_rate", 0.0005) or 0.0005
            ),
            max_next_funding_horizon_sec=float(
                funding.get("max_next_funding_horizon_sec", 32_400.0)
                or 32_400.0
            ),
            recovery_updates=int(funding.get("recovery_updates", 5) or 5),
        )
        self.lock = threading.RLock()
        self.states: dict[str, FundingGuardState] = {}
        self.decisions: dict[str, FundingGuardDecision] = {}

    @staticmethod
    def next_funding_epoch(data) -> float:
        raw_timestamp = getattr(data, "next_funding_timestamp", 0.0)
        try:
            parsed = float(raw_timestamp or 0.0)
        except (TypeError, ValueError):
            parsed = 0.0
        if parsed > 0.0:
            return parsed
        next_funding_time = getattr(data, "next_funding_time", None)
        timestamp = getattr(next_funding_time, "timestamp", None)
        if not callable(timestamp):
            return 0.0
        try:
            return float(timestamp())
        except (OSError, OverflowError, TypeError, ValueError):
            return 0.0

    @staticmethod
    def observation_id(symbol: str, exchange_timestamp) -> str:
        try:
            exchange_timestamp = float(exchange_timestamp or 0.0)
        except (TypeError, ValueError):
            return ""
        if math.isfinite(exchange_timestamp) and exchange_timestamp > 0.0:
            return f"{symbol}:{exchange_timestamp:.6f}"
        return ""

    def evaluate_observation(
        self,
        symbol: str,
        observation: FundingObservation | None,
        *,
        now_monotonic: float,
    ) -> bool:
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            return False
        with self.lock:
            previous = self.states.get(symbol)
            if previous is None:
                previous = FundingGuardState(
                    reason="funding_guard:startup_hold",
                    post_hold_until_monotonic=(
                        now_monotonic + self.policy.post_funding_hold_sec
                    ),
                )
            decision, next_state = evaluate_funding_guard(
                self.policy,
                observation,
                previous,
                now_monotonic=now_monotonic,
            )
            self.states[symbol] = next_state
            self.decisions[symbol] = decision
        if decision.blocks_open_risk:
            constraint_reason = decision.reason
            if constraint_reason.startswith("funding_guard:recovery_pending:"):
                constraint_reason = "funding_guard:recovery_pending"
            self.set_trading_mode(
                self.reduce_only_mode,
                constraint_reason,
            )
            return False
        return True

    def evaluate_mark(self, data) -> bool:
        symbol = str(getattr(data, "symbol", "") or "").strip().upper()
        observation = FundingObservation(
            observation_id=self.observation_id(
                symbol,
                getattr(data, "exchange_timestamp", 0.0),
            ),
            funding_rate=getattr(data, "funding_rate", None),
            next_funding_epoch=self.next_funding_epoch(data),
            corrected_received_epoch=getattr(
                data,
                "corrected_received_timestamp",
                0.0,
            ),
            received_monotonic=getattr(data, "received_monotonic", 0.0),
            clock_healthy=bool(time_service.is_ready()),
        )
        healthy = self.evaluate_observation(
            symbol,
            observation,
            now_monotonic=time.perf_counter(),
        )
        if healthy:
            self.clear_constraint_if_healthy()
        return healthy

    def observation_from_snapshot(
        self,
        symbol: str,
        snapshot: dict,
    ) -> FundingObservation | None:
        if not isinstance(snapshot, dict):
            return None
        observation_id = self.observation_id(
            symbol,
            snapshot.get("mark_exchange_timestamp"),
        )
        if not observation_id:
            return None
        return FundingObservation(
            observation_id=observation_id,
            funding_rate=snapshot.get("funding_rate"),
            next_funding_epoch=snapshot.get("next_funding_epoch"),
            corrected_received_epoch=snapshot.get(
                "mark_corrected_received_timestamp"
            ),
            received_monotonic=snapshot.get("mark_received_monotonic"),
            clock_healthy=bool(time_service.is_ready()),
        )

    def clear_constraint_if_healthy(self) -> bool:
        configured_symbols = {
            str(symbol or "").strip().upper()
            for symbol in self.root_config.get("symbols", ())
            if str(symbol or "").strip()
        }
        with self.lock:
            if not configured_symbols or any(
                not self.decisions.get(symbol)
                or not self.decisions[symbol].healthy
                for symbol in configured_symbols
            ):
                return False
        if self.oms is None:
            return False
        if not self.oms.has_trading_mode_constraint(("funding_guard:",)):
            return True
        snapshot = self.oms.get_capability_snapshot()
        constraints = (
            snapshot.get("mode_constraints", {})
            if isinstance(snapshot, dict)
            else {}
        )
        record = (
            constraints.get("funding_guard:", {})
            if isinstance(constraints, dict)
            else {}
        )
        generation = (
            int(record.get("generation", 0) or 0)
            if isinstance(record, dict)
            else 0
        )
        reason = (
            str(record.get("reason", "") or "")
            if isinstance(record, dict)
            else ""
        )
        if generation <= 0 or not reason.startswith("funding_guard:"):
            return False
        return bool(
            self.clear_trading_mode(
                reason="funding guard recovered",
                prefixes=("funding_guard:",),
                expected_generations={"funding_guard:": generation},
            )
        )

    def check(self, *, active: bool, kill_switch_triggered: bool, now=None) -> bool:
        if not active or kill_switch_triggered:
            return False
        if not self.policy.enabled:
            return True
        now_monotonic = time.perf_counter() if now is None else float(now)
        symbols = sorted(self.tracked_symbols())
        if not symbols:
            return False
        healthy = True
        for symbol in symbols:
            snapshot = data_cache.get_risk_snapshot(symbol, now=now_monotonic)
            observation = self.observation_from_snapshot(symbol, snapshot)
            if not self.evaluate_observation(
                symbol,
                observation,
                now_monotonic=now_monotonic,
            ):
                healthy = False
        return self.clear_constraint_if_healthy() if healthy else False

    def status_snapshot(self) -> dict:
        with self.lock:
            decisions = dict(self.decisions)
        return {
            "enabled": bool(self.policy.enabled),
            "healthy": bool(
                not self.policy.enabled
                or (decisions and all(item.healthy for item in decisions.values()))
            ),
            "symbols": {
                symbol: {
                    "action": decision.action,
                    "reason": decision.reason,
                    "funding_rate": decision.funding_rate,
                    "seconds_to_funding": decision.seconds_to_funding,
                    "snapshot_age_ms": decision.snapshot_age_ms,
                    "post_hold_remaining_sec": (
                        decision.post_hold_remaining_sec
                    ),
                    "healthy_updates": decision.consecutive_healthy_updates,
                    "required_recovery_updates": (
                        decision.required_recovery_updates
                    ),
                }
                for symbol, decision in decisions.items()
            },
        }
