"""Venue-neutral contracts used by the independent risk sidecar."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Mapping, Protocol


class SnapshotPurpose(str, Enum):
    """Select the consistency and scope required from an exchange read."""

    MONITORING = "MONITORING"
    FLAT_PROOF = "FLAT_PROOF"


@dataclass(frozen=True, slots=True)
class StateVersion:
    """Optimistic-concurrency token for durable sidecar state."""

    writer_epoch: int
    owner_epoch: int
    safety_epoch: int
    generation: int
    state_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "writer_epoch",
            "owner_epoch",
            "safety_epoch",
            "generation",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        digest = str(self.state_sha256 or "")
        if digest and (
            len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("state_sha256 must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class CashFlowTruth:
    """Cash-flow projections with separate daily and deployment horizons."""

    risk_day: str
    daily_external_cash_flow_total: float
    deployment_external_cash_flow_total: float
    ledger_generation: int
    complete_through_ms: int
    captured_monotonic: float

    def __post_init__(self) -> None:
        for name in (
            "daily_external_cash_flow_total",
            "deployment_external_cash_flow_total",
            "captured_monotonic",
        ):
            if not math.isfinite(float(getattr(self, name))):
                raise ValueError(f"{name} must be finite")
        if not self.risk_day:
            raise ValueError("risk_day is required")
        if (
            isinstance(self.ledger_generation, bool)
            or self.ledger_generation < 0
        ):
            raise ValueError("ledger_generation must be non-negative")
        if (
            isinstance(self.complete_through_ms, bool)
            or self.complete_through_ms <= 0
        ):
            raise ValueError("complete_through_ms must be positive")
        if self.captured_monotonic < 0.0:
            raise ValueError("captured_monotonic must be non-negative")

    def as_snapshot_fields(self) -> dict:
        """Project both new fields and the legacy daily compatibility field."""
        return {
            "daily_external_cash_flow_total": float(
                self.daily_external_cash_flow_total
            ),
            "deployment_external_cash_flow_total": float(
                self.deployment_external_cash_flow_total
            ),
            "external_cash_flow_total": float(
                self.daily_external_cash_flow_total
            ),
            "cash_flow_risk_day": self.risk_day,
            "cash_flow_ledger_generation": self.ledger_generation,
            "cash_flow_complete_through_ms": self.complete_through_ms,
        }


@dataclass(frozen=True, slots=True)
class AccountTruthSnapshot:
    """Normalized account truth returned by a risk exchange adapter."""

    account_scope_id: str
    truth_sequence: int
    captured_monotonic: float
    captured_utc_ms: int
    orders_scope: str
    positions_scope: str
    complete: bool
    consistency_digest: str
    account: Mapping = field(default_factory=dict)
    positions: tuple[Mapping, ...] = ()
    open_orders: tuple[Mapping, ...] = ()
    cash_flow: CashFlowTruth | None = None
    funding: Mapping = field(default_factory=dict)
    clock_health: Mapping = field(default_factory=dict)

    @property
    def account_wide(self) -> bool:
        return (
            self.orders_scope == "ACCOUNT_WIDE"
            and self.positions_scope == "ACCOUNT_WIDE"
        )


@dataclass(frozen=True, slots=True)
class TruthResult:
    ok: bool
    snapshot: AccountTruthSnapshot | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ActionResult:
    """Result of an exchange action; success never proves account flatness."""

    ok: bool
    action_id: str
    reason: str = ""
    submitted_count: int = 0


@dataclass(frozen=True, slots=True)
class FlatProof:
    """Persistable account-wide proof bound to all relevant generations."""

    proof_id: str
    purpose: str
    account_scope_id: str
    deployment_id: str
    writer_epoch: int
    owner_epoch: int
    safety_epoch: int
    first_truth_sequence: int
    last_truth_sequence: int
    sample_count: int
    open_order_count: int
    nonzero_position_count: int
    snapshot_digest: str
    verified_monotonic: float
    valid_until_monotonic: float

    def is_valid(self, now: float, version: StateVersion) -> bool:
        return bool(
            self.open_order_count == 0
            and self.nonzero_position_count == 0
            and self.sample_count >= 2
            and self.last_truth_sequence > self.first_truth_sequence
            and float(now) <= self.valid_until_monotonic
            and self.writer_epoch == version.writer_epoch
            and self.owner_epoch == version.owner_epoch
            and self.safety_epoch == version.safety_epoch
        )


class RuntimeClock(Protocol):
    def monotonic(self) -> float: ...

    def utc_now_ms(self) -> int: ...

    def sleep(self, seconds: float) -> None: ...


class ExchangeClockPort(Protocol):
    def sync(self, *, force: bool = False) -> bool: ...

    def timestamp_ms(self) -> int: ...

    def health(self) -> Mapping: ...


class RiskExchangePort(Protocol):
    """The complete venue-neutral surface consumed by the sidecar core."""

    def read_account_truth(self, purpose: SnapshotPurpose) -> TruthResult: ...

    def cancel_all_account_orders(self, action_id: str) -> ActionResult: ...

    def flatten_all_account_positions(self, action_id: str) -> ActionResult: ...

    def close(self) -> None: ...
