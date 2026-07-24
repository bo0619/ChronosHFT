"""Pure funding-rate and funding-window risk decisions.

The guard uses a corrected exchange-time anchor advanced by a monotonic clock.
It performs no I/O and does not depend on the OMS, gateway, or strategy layer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math


ALLOW = "ALLOW"
REDUCE_ONLY = "REDUCE_ONLY"
_SCHEDULE_EPSILON_SEC = 1e-3


def _finite_float(value) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


@dataclass(frozen=True, slots=True)
class FundingGuardPolicy:
    """Immutable limits for one symbol's funding guard."""

    enabled: bool = True
    require_snapshot: bool = True
    max_snapshot_age_ms: float = 3_000.0
    pre_funding_reduce_only_sec: float = 600.0
    post_funding_hold_sec: float = 120.0
    max_abs_funding_rate: float = 0.0005
    max_next_funding_horizon_sec: float = 32_400.0
    recovery_updates: int = 5

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be boolean")
        if not isinstance(self.require_snapshot, bool):
            raise ValueError("require_snapshot must be boolean")

        positive_fields = (
            "max_snapshot_age_ms",
            "pre_funding_reduce_only_sec",
            "post_funding_hold_sec",
            "max_abs_funding_rate",
            "max_next_funding_horizon_sec",
        )
        for field in positive_fields:
            value = _finite_float(getattr(self, field))
            if value is None or value <= 0.0:
                raise ValueError(f"{field} must be positive and finite")
            object.__setattr__(self, field, value)

        if (
            float(self.max_next_funding_horizon_sec)
            <= float(self.pre_funding_reduce_only_sec)
        ):
            raise ValueError(
                "max_next_funding_horizon_sec must exceed "
                "pre_funding_reduce_only_sec"
            )
        if (
            isinstance(self.recovery_updates, bool)
            or not isinstance(self.recovery_updates, int)
            or self.recovery_updates < 1
        ):
            raise ValueError("recovery_updates must be a positive integer")


@dataclass(frozen=True, slots=True)
class FundingObservation:
    """One mark-price funding observation in exchange and monotonic time."""

    observation_id: str
    funding_rate: float | None
    next_funding_epoch: float | None
    corrected_received_epoch: float | None
    received_monotonic: float | None
    clock_healthy: bool = True


def parse_binance_premium_index_payload(
    payload: Mapping,
    *,
    expected_symbol: str,
    corrected_received_epoch: float,
    received_monotonic: float,
    max_source_age_ms: float,
    clock_healthy: bool = True,
) -> FundingObservation:
    """Strictly normalize one Binance USD-M premium-index response.

    ``corrected_received_epoch`` must come from an exchange-synchronized clock.
    Comparing it with Binance's payload ``time`` prevents a freshly received
    but stale response from resetting the monotonic snapshot-age clock.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("funding payload must be an object")
    symbol = str(expected_symbol or "").strip().upper()
    if not symbol:
        raise ValueError("expected funding symbol is required")
    payload_symbol = str(payload.get("symbol", "") or "").strip().upper()
    if payload_symbol != symbol:
        raise ValueError(
            f"funding payload symbol mismatch: {payload_symbol or 'missing'}"
        )

    def required_finite(field: str) -> float:
        raw = payload.get(field)
        if isinstance(raw, bool):
            raise ValueError(f"funding payload {field} must be finite")
        parsed = _finite_float(raw)
        if parsed is None:
            raise ValueError(f"funding payload {field} must be finite")
        return parsed

    funding_rate = required_finite("lastFundingRate")
    next_funding_ms = required_finite("nextFundingTime")
    source_time_ms = required_finite("time")
    if (
        next_funding_ms <= 0.0
        or not next_funding_ms.is_integer()
    ):
        raise ValueError(
            "funding payload nextFundingTime must be a positive integer"
        )
    if source_time_ms <= 0.0 or not source_time_ms.is_integer():
        raise ValueError("funding payload time must be a positive integer")

    corrected_epoch = _finite_float(corrected_received_epoch)
    if corrected_epoch is None or corrected_epoch <= 0.0:
        raise ValueError(
            "corrected funding receipt epoch must be positive and finite"
        )
    monotonic = _finite_float(received_monotonic)
    if monotonic is None or monotonic < 0.0:
        raise ValueError(
            "funding receipt monotonic time must be finite and non-negative"
        )
    source_age_limit = _finite_float(max_source_age_ms)
    if source_age_limit is None or source_age_limit <= 0.0:
        raise ValueError("funding source age limit must be positive and finite")
    if not isinstance(clock_healthy, bool):
        raise ValueError("funding clock health must be boolean")

    source_age_ms = corrected_epoch * 1_000.0 - source_time_ms
    if source_age_ms > source_age_limit:
        raise ValueError("funding payload source time is stale")
    if source_age_ms < -source_age_limit:
        raise ValueError("funding payload source time is from the future")

    observation_id = (
        f"{symbol}:{int(source_time_ms)}:{int(next_funding_ms)}:"
        f"{format(funding_rate, '.17g')}"
    )
    return FundingObservation(
        observation_id=observation_id,
        funding_rate=funding_rate,
        next_funding_epoch=next_funding_ms / 1_000.0,
        corrected_received_epoch=corrected_epoch,
        received_monotonic=monotonic,
        clock_healthy=clock_healthy,
    )


@dataclass(frozen=True, slots=True)
class FundingGuardState:
    """Immutable state carried between funding-guard evaluations."""

    blocked: bool = True
    reason: str = "funding_guard:snapshot_unavailable"
    last_observation_id: str = ""
    last_next_funding_epoch: float | None = None
    post_hold_until_monotonic: float = 0.0
    consecutive_healthy_updates: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.blocked, bool):
            raise ValueError("blocked must be boolean")
        if self.last_next_funding_epoch is not None:
            next_epoch = _finite_float(self.last_next_funding_epoch)
            if next_epoch is None or next_epoch <= 0.0:
                raise ValueError(
                    "last_next_funding_epoch must be positive and finite"
                )
            object.__setattr__(
                self,
                "last_next_funding_epoch",
                next_epoch,
            )
        hold_until = _finite_float(self.post_hold_until_monotonic)
        if hold_until is None or hold_until < 0.0:
            raise ValueError(
                "post_hold_until_monotonic must be finite and non-negative"
            )
        object.__setattr__(
            self,
            "post_hold_until_monotonic",
            hold_until,
        )
        if (
            isinstance(self.consecutive_healthy_updates, bool)
            or not isinstance(self.consecutive_healthy_updates, int)
            or self.consecutive_healthy_updates < 0
        ):
            raise ValueError(
                "consecutive_healthy_updates must be a non-negative integer"
            )


@dataclass(frozen=True, slots=True)
class FundingGuardDecision:
    """The effective action and diagnostics for one transition."""

    action: str
    reason: str
    observation_valid: bool
    current_observation_safe: bool
    observation_advanced: bool
    snapshot_age_ms: float | None
    exchange_now_epoch: float | None
    seconds_to_funding: float | None
    funding_rate: float | None
    post_hold_remaining_sec: float
    consecutive_healthy_updates: int
    required_recovery_updates: int

    @property
    def healthy(self) -> bool:
        return self.action == ALLOW

    @property
    def blocks_open_risk(self) -> bool:
        return self.action == REDUCE_ONLY

    @property
    def allows_reduce_only(self) -> bool:
        return True


def _decision(
    *,
    state: FundingGuardState,
    policy: FundingGuardPolicy,
    observation_valid: bool,
    current_observation_safe: bool,
    observation_advanced: bool,
    snapshot_age_ms: float | None = None,
    exchange_now_epoch: float | None = None,
    seconds_to_funding: float | None = None,
    funding_rate: float | None = None,
    now_monotonic: float | None = None,
) -> FundingGuardDecision:
    post_hold_remaining_sec = 0.0
    if now_monotonic is not None:
        post_hold_remaining_sec = max(
            0.0,
            state.post_hold_until_monotonic - now_monotonic,
        )
    return FundingGuardDecision(
        action=REDUCE_ONLY if state.blocked else ALLOW,
        reason=state.reason,
        observation_valid=observation_valid,
        current_observation_safe=current_observation_safe,
        observation_advanced=observation_advanced,
        snapshot_age_ms=snapshot_age_ms,
        exchange_now_epoch=exchange_now_epoch,
        seconds_to_funding=seconds_to_funding,
        funding_rate=funding_rate,
        post_hold_remaining_sec=post_hold_remaining_sec,
        consecutive_healthy_updates=state.consecutive_healthy_updates,
        required_recovery_updates=policy.recovery_updates,
    )


def _blocked_transition(
    *,
    previous: FundingGuardState,
    policy: FundingGuardPolicy,
    reason: str,
    observation_id: str = "",
    observation_valid: bool,
    observation_advanced: bool,
    last_next_funding_epoch: float | None = None,
    post_hold_until_monotonic: float | None = None,
    snapshot_age_ms: float | None = None,
    exchange_now_epoch: float | None = None,
    seconds_to_funding: float | None = None,
    funding_rate: float | None = None,
    now_monotonic: float | None = None,
) -> tuple[FundingGuardDecision, FundingGuardState]:
    state = FundingGuardState(
        blocked=True,
        reason=reason,
        last_observation_id=(
            observation_id or previous.last_observation_id
        ),
        last_next_funding_epoch=(
            previous.last_next_funding_epoch
            if last_next_funding_epoch is None
            else last_next_funding_epoch
        ),
        post_hold_until_monotonic=(
            previous.post_hold_until_monotonic
            if post_hold_until_monotonic is None
            else post_hold_until_monotonic
        ),
        consecutive_healthy_updates=0,
    )
    return (
        _decision(
            state=state,
            policy=policy,
            observation_valid=observation_valid,
            current_observation_safe=False,
            observation_advanced=observation_advanced,
            snapshot_age_ms=snapshot_age_ms,
            exchange_now_epoch=exchange_now_epoch,
            seconds_to_funding=seconds_to_funding,
            funding_rate=funding_rate,
            now_monotonic=now_monotonic,
        ),
        state,
    )


def evaluate_funding_guard(
    policy: FundingGuardPolicy,
    observation: FundingObservation | None,
    state: FundingGuardState | None = None,
    *,
    now_monotonic: float,
) -> tuple[FundingGuardDecision, FundingGuardState]:
    """Advance the guard and return ``(decision, next_state)``.

    A newly constructed state starts blocked. Recovery therefore requires the
    configured number of distinct healthy observations. Re-evaluating the same
    observation ID never advances the recovery counter.
    """

    previous = state or FundingGuardState()
    now = _finite_float(now_monotonic)

    if not policy.enabled:
        disabled_state = FundingGuardState(
            blocked=False,
            reason="",
            last_observation_id=previous.last_observation_id,
            last_next_funding_epoch=previous.last_next_funding_epoch,
        )
        return (
            _decision(
                state=disabled_state,
                policy=policy,
                observation_valid=True,
                current_observation_safe=True,
                observation_advanced=False,
                now_monotonic=now,
            ),
            disabled_state,
        )

    if now is None or now < 0.0:
        return _blocked_transition(
            previous=previous,
            policy=policy,
            reason="funding_guard:monotonic_clock_invalid",
            observation_valid=False,
            observation_advanced=False,
        )

    if observation is None:
        if not policy.require_snapshot:
            snapshot_optional_state = FundingGuardState(
                blocked=False,
                reason="",
                last_observation_id=previous.last_observation_id,
                last_next_funding_epoch=(
                    previous.last_next_funding_epoch
                ),
                post_hold_until_monotonic=(
                    previous.post_hold_until_monotonic
                ),
            )
            return (
                _decision(
                    state=snapshot_optional_state,
                    policy=policy,
                    observation_valid=True,
                    current_observation_safe=True,
                    observation_advanced=False,
                    now_monotonic=now,
                ),
                snapshot_optional_state,
            )
        else:
            return _blocked_transition(
                previous=previous,
                policy=policy,
                reason="funding_guard:snapshot_unavailable",
                observation_valid=False,
                observation_advanced=False,
                now_monotonic=now,
            )

    observation_id = str(observation.observation_id or "").strip()
    observation_advanced = bool(
        observation_id
        and observation_id != previous.last_observation_id
    )
    if not observation_id:
        return _blocked_transition(
            previous=previous,
            policy=policy,
            reason="funding_guard:observation_id_missing",
            observation_valid=False,
            observation_advanced=False,
            now_monotonic=now,
        )
    if observation.clock_healthy is not True:
        return _blocked_transition(
            previous=previous,
            policy=policy,
            reason="funding_guard:exchange_clock_unhealthy",
            observation_id=observation_id,
            observation_valid=False,
            observation_advanced=observation_advanced,
            now_monotonic=now,
        )

    received_monotonic = _finite_float(observation.received_monotonic)
    if received_monotonic is None or received_monotonic < 0.0:
        return _blocked_transition(
            previous=previous,
            policy=policy,
            reason="funding_guard:received_monotonic_invalid",
            observation_id=observation_id,
            observation_valid=False,
            observation_advanced=observation_advanced,
            now_monotonic=now,
        )
    age_sec = now - received_monotonic
    if age_sec < 0.0:
        return _blocked_transition(
            previous=previous,
            policy=policy,
            reason="funding_guard:observation_from_future",
            observation_id=observation_id,
            observation_valid=False,
            observation_advanced=observation_advanced,
            now_monotonic=now,
        )
    snapshot_age_ms = age_sec * 1_000.0
    if snapshot_age_ms > policy.max_snapshot_age_ms:
        return _blocked_transition(
            previous=previous,
            policy=policy,
            reason="funding_guard:snapshot_stale",
            observation_id=observation_id,
            observation_valid=False,
            observation_advanced=observation_advanced,
            snapshot_age_ms=snapshot_age_ms,
            now_monotonic=now,
        )

    corrected_received_epoch = _finite_float(
        observation.corrected_received_epoch
    )
    if (
        corrected_received_epoch is None
        or corrected_received_epoch <= 0.0
    ):
        return _blocked_transition(
            previous=previous,
            policy=policy,
            reason="funding_guard:exchange_time_anchor_invalid",
            observation_id=observation_id,
            observation_valid=False,
            observation_advanced=observation_advanced,
            snapshot_age_ms=snapshot_age_ms,
            now_monotonic=now,
        )
    exchange_now_epoch = corrected_received_epoch + age_sec

    funding_rate = _finite_float(observation.funding_rate)
    if funding_rate is None:
        return _blocked_transition(
            previous=previous,
            policy=policy,
            reason="funding_guard:funding_rate_invalid",
            observation_id=observation_id,
            observation_valid=False,
            observation_advanced=observation_advanced,
            snapshot_age_ms=snapshot_age_ms,
            exchange_now_epoch=exchange_now_epoch,
            now_monotonic=now,
        )

    next_funding_epoch = _finite_float(observation.next_funding_epoch)
    if next_funding_epoch is None or next_funding_epoch <= 0.0:
        return _blocked_transition(
            previous=previous,
            policy=policy,
            reason="funding_guard:next_funding_time_invalid",
            observation_id=observation_id,
            observation_valid=False,
            observation_advanced=observation_advanced,
            snapshot_age_ms=snapshot_age_ms,
            exchange_now_epoch=exchange_now_epoch,
            funding_rate=funding_rate,
            now_monotonic=now,
        )

    seconds_to_funding = next_funding_epoch - exchange_now_epoch
    if seconds_to_funding < 0.0:
        return _blocked_transition(
            previous=previous,
            policy=policy,
            reason="funding_guard:next_funding_time_elapsed",
            observation_id=observation_id,
            observation_valid=False,
            observation_advanced=observation_advanced,
            snapshot_age_ms=snapshot_age_ms,
            exchange_now_epoch=exchange_now_epoch,
            seconds_to_funding=seconds_to_funding,
            funding_rate=funding_rate,
            now_monotonic=now,
        )
    if seconds_to_funding > policy.max_next_funding_horizon_sec:
        return _blocked_transition(
            previous=previous,
            policy=policy,
            reason="funding_guard:next_funding_horizon_exceeded",
            observation_id=observation_id,
            observation_valid=False,
            observation_advanced=observation_advanced,
            snapshot_age_ms=snapshot_age_ms,
            exchange_now_epoch=exchange_now_epoch,
            seconds_to_funding=seconds_to_funding,
            funding_rate=funding_rate,
            now_monotonic=now,
        )

    previous_next = previous.last_next_funding_epoch
    post_hold_until = previous.post_hold_until_monotonic
    trusted_next_funding_epoch = next_funding_epoch
    if previous_next is not None:
        schedule_delta = next_funding_epoch - previous_next
        if schedule_delta < -_SCHEDULE_EPSILON_SEC:
            return _blocked_transition(
                previous=previous,
                policy=policy,
                reason="funding_guard:next_funding_time_regressed",
                observation_id=observation_id,
                observation_valid=False,
                observation_advanced=observation_advanced,
                snapshot_age_ms=snapshot_age_ms,
                exchange_now_epoch=exchange_now_epoch,
                seconds_to_funding=seconds_to_funding,
                funding_rate=funding_rate,
                now_monotonic=now,
            )
        if schedule_delta > _SCHEDULE_EPSILON_SEC:
            old_seconds_to_funding = previous_next - exchange_now_epoch
            if (
                old_seconds_to_funding
                <= policy.pre_funding_reduce_only_sec
            ):
                post_hold_until = max(
                    post_hold_until,
                    now + policy.post_funding_hold_sec,
                )
    if abs(funding_rate) >= policy.max_abs_funding_rate:
        return _blocked_transition(
            previous=previous,
            policy=policy,
            reason="funding_guard:funding_rate_limit",
            observation_id=observation_id,
            observation_valid=True,
            observation_advanced=observation_advanced,
            last_next_funding_epoch=trusted_next_funding_epoch,
            post_hold_until_monotonic=post_hold_until,
            snapshot_age_ms=snapshot_age_ms,
            exchange_now_epoch=exchange_now_epoch,
            seconds_to_funding=seconds_to_funding,
            funding_rate=funding_rate,
            now_monotonic=now,
        )
    if seconds_to_funding <= policy.pre_funding_reduce_only_sec:
        return _blocked_transition(
            previous=previous,
            policy=policy,
            reason="funding_guard:pre_funding_window",
            observation_id=observation_id,
            observation_valid=True,
            observation_advanced=observation_advanced,
            last_next_funding_epoch=trusted_next_funding_epoch,
            post_hold_until_monotonic=post_hold_until,
            snapshot_age_ms=snapshot_age_ms,
            exchange_now_epoch=exchange_now_epoch,
            seconds_to_funding=seconds_to_funding,
            funding_rate=funding_rate,
            now_monotonic=now,
        )
    if now < post_hold_until:
        return _blocked_transition(
            previous=previous,
            policy=policy,
            reason="funding_guard:post_funding_hold",
            observation_id=observation_id,
            observation_valid=True,
            observation_advanced=observation_advanced,
            last_next_funding_epoch=trusted_next_funding_epoch,
            post_hold_until_monotonic=post_hold_until,
            snapshot_age_ms=snapshot_age_ms,
            exchange_now_epoch=exchange_now_epoch,
            seconds_to_funding=seconds_to_funding,
            funding_rate=funding_rate,
            now_monotonic=now,
        )

    healthy_updates = previous.consecutive_healthy_updates
    if previous.blocked and observation_advanced:
        healthy_updates += 1
    recovered = (
        not previous.blocked
        or healthy_updates >= policy.recovery_updates
    )
    if recovered:
        next_state = FundingGuardState(
            blocked=False,
            reason="",
            last_observation_id=observation_id,
            last_next_funding_epoch=trusted_next_funding_epoch,
            post_hold_until_monotonic=post_hold_until,
            consecutive_healthy_updates=0,
        )
    else:
        next_state = FundingGuardState(
            blocked=True,
            reason=(
                "funding_guard:recovery_pending:"
                f"{healthy_updates}/{policy.recovery_updates}"
            ),
            last_observation_id=observation_id,
            last_next_funding_epoch=trusted_next_funding_epoch,
            post_hold_until_monotonic=post_hold_until,
            consecutive_healthy_updates=healthy_updates,
        )
    return (
        _decision(
            state=next_state,
            policy=policy,
            observation_valid=True,
            current_observation_safe=True,
            observation_advanced=observation_advanced,
            snapshot_age_ms=snapshot_age_ms,
            exchange_now_epoch=exchange_now_epoch,
            seconds_to_funding=seconds_to_funding,
            funding_rate=funding_rate,
            now_monotonic=now,
        ),
        next_state,
    )


__all__ = [
    "ALLOW",
    "REDUCE_ONLY",
    "FundingGuardDecision",
    "FundingGuardPolicy",
    "FundingGuardState",
    "FundingObservation",
    "evaluate_funding_guard",
    "parse_binance_premium_index_payload",
]
