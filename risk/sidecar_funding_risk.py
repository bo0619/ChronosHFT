"""Funding-risk state and evaluation for the independent sidecar."""

from __future__ import annotations

from risk.funding_guard import (
    ALLOW,
    FundingGuardPolicy,
    FundingGuardState,
    FundingObservation,
    evaluate_funding_guard,
)


class SidecarFundingRiskController:
    """Own funding observations and recovery state without a core backref."""

    __slots__ = (
        "action",
        "decisions",
        "exchange_poll_interval_sec",
        "guard_states",
        "observations",
        "policy",
        "reason",
        "symbols",
    )

    def __init__(
        self,
        policy: FundingGuardPolicy,
        symbols: tuple[str, ...],
        exchange_poll_interval_sec: float,
        *,
        now: float,
    ):
        self.policy = policy
        self.symbols = tuple(symbols)
        self.exchange_poll_interval_sec = float(exchange_poll_interval_sec)
        self.observations: dict[str, FundingObservation] = {}
        self.guard_states = {
            symbol: FundingGuardState(
                reason="funding_guard:startup_hold",
                post_hold_until_monotonic=(
                    now + policy.post_funding_hold_sec
                ),
            )
            for symbol in self.symbols
            if policy.enabled
        }
        self.decisions = {}
        self.action = "REDUCE_ONLY" if policy.enabled else "NONE"
        self.reason = (
            "funding_guard:snapshot_unavailable" if policy.enabled else ""
        )

    @staticmethod
    def observation_from_record(record) -> FundingObservation | None:
        if not isinstance(record, dict):
            return None
        return FundingObservation(
            observation_id=str(record.get("observation_id", "") or ""),
            funding_rate=record.get("funding_rate"),
            next_funding_epoch=record.get("next_funding_epoch"),
            corrected_received_epoch=record.get("corrected_received_epoch"),
            received_monotonic=record.get("received_monotonic"),
            clock_healthy=record.get("clock_healthy") is True,
        )

    def initial_metrics(self) -> dict:
        return {
            "enabled": self.policy.enabled,
            "healthy": not self.policy.enabled,
            "action": self.action,
            "reason": self.reason,
            "symbols": {},
        }

    def ingest(self, snapshot: dict) -> None:
        if not self.policy.enabled:
            return
        raw_observations = snapshot.get("funding_observations")
        if not isinstance(raw_observations, dict):
            self.observations = {}
            return
        self.observations = {
            symbol: observation
            for symbol in self.symbols
            if (
                observation := self.observation_from_record(
                    raw_observations.get(symbol)
                )
            )
            is not None
        }

    def evaluate(self, now: float) -> tuple[str, str, dict]:
        policy = self.policy
        if not policy.enabled:
            self.action = "NONE"
            self.reason = ""
            return self.action, self.reason, self.initial_metrics()
        if not self.symbols:
            self.action = "REDUCE_ONLY"
            self.reason = "funding_guard:symbols_missing"
            return (
                self.action,
                self.reason,
                {
                    "enabled": True,
                    "healthy": False,
                    "action": self.action,
                    "reason": self.reason,
                    "symbols": {},
                },
            )

        symbol_metrics = {}
        blocked_reason = ""
        for symbol in self.symbols:
            previous = self.guard_states.get(symbol)
            if previous is None:
                previous = FundingGuardState(
                    reason="funding_guard:startup_hold",
                    post_hold_until_monotonic=(
                        now + policy.post_funding_hold_sec
                    ),
                )
            decision, next_state = evaluate_funding_guard(
                policy,
                self.observations.get(symbol),
                previous,
                now_monotonic=now,
            )
            self.guard_states[symbol] = next_state
            self.decisions[symbol] = decision
            if decision.action != ALLOW and not blocked_reason:
                blocked_reason = f"{decision.reason}:{symbol}"
            symbol_metrics[symbol] = {
                "healthy": decision.healthy,
                "action": decision.action,
                "reason": decision.reason,
                "observation_valid": decision.observation_valid,
                "snapshot_age_ms": decision.snapshot_age_ms,
                "seconds_to_funding": decision.seconds_to_funding,
                "funding_rate": decision.funding_rate,
                "post_hold_remaining_sec": (
                    decision.post_hold_remaining_sec
                ),
                "consecutive_healthy_updates": (
                    decision.consecutive_healthy_updates
                ),
                "required_recovery_updates": decision.required_recovery_updates,
            }

        self.action = "REDUCE_ONLY" if blocked_reason else "NONE"
        self.reason = blocked_reason
        return (
            self.action,
            self.reason,
            {
                "enabled": True,
                "healthy": not blocked_reason,
                "action": self.action,
                "reason": self.reason,
                "poll_interval_sec": self.exchange_poll_interval_sec,
                "max_snapshot_age_ms": policy.max_snapshot_age_ms,
                "symbols": symbol_metrics,
            },
        )
