"""Stable structural contract from Risk into the OMS boundary."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol, runtime_checkable

from event.type import OMSCapabilityMode, Side


class RiskOMSExposurePort(Protocol):
    """Exposure queries needed by pre-trade and kill-switch checks."""

    net_positions: Mapping[str, float]

    def estimate_account_gross_notional(
        self,
        *,
        symbol: str,
        side: Side,
        volume: float,
        order_price: float,
    ) -> float | None: ...


class RiskOMSAccountPort(Protocol):
    """Account checks and truth fields consumed by RiskManager."""

    balance: float
    equity: float
    external_cash_flow_total: float

    def check_margin(self, notional_value: float) -> bool: ...


class RiskOMSJournalPort(Protocol):
    """Durable risk-state operations supplied by the shared journal."""

    def append(self, kind: str, payload: dict) -> int: ...

    def load(self) -> list[dict]: ...

    def iter_records(
        self,
        *,
        respect_replay_policy: bool = False,
    ) -> Iterable[dict]: ...


class RiskOMSGatewayView(Protocol):
    gateway_name: str


@runtime_checkable
class RiskOMSPort(Protocol):
    """The complete OMS surface currently supported for the risk domain.

    This protocol is deliberately located outside both ``risk`` and ``oms``.
    It turns the existing duck-typed boundary into a versionable contract and
    prevents Risk from importing OMS implementation classes or exceptions.
    Data attributes are read-only from Risk's perspective; mutation is only
    available through the methods declared below.
    """

    account: RiskOMSAccountPort
    config: Mapping[str, Any]
    exposure: RiskOMSExposurePort
    gateway: RiskOMSGatewayView
    journal: RiskOMSJournalPort
    manual_rearm_required: bool
    mode_override_reason: str
    state: Any

    def can_open_new_risk(self) -> bool: ...

    def can_renew_venue_dead_man_switch(self) -> bool: ...

    def clear_symbol_freeze(
        self,
        symbol: str,
        reason: str = "",
        *,
        expected_epoch: int | None = None,
        expected_reason: str | None = None,
    ) -> bool: ...

    def clear_trading_mode(self, **kwargs) -> bool: ...

    def clear_venue_freeze(self, venue: str, reason: str = "", **kwargs) -> bool: ...

    def emergency_reduce_only_flatten(
        self,
        reason: str,
        symbol: str = "",
    ) -> int: ...

    def freeze_symbol(
        self,
        symbol: str,
        reason: str,
        cancel_active_orders: bool = True,
    ) -> int: ...

    def freeze_system(
        self,
        reason: str,
        cancel_active_orders: bool = False,
    ) -> Any: ...

    def freeze_venue(
        self,
        venue: str,
        reason: str,
        cancel_active_orders: bool = True,
    ) -> int: ...

    def get_capability_snapshot(self) -> dict: ...

    def get_known_account_order_symbols(self) -> list[str]: ...

    def get_local_order_truth_snapshot(self) -> dict: ...

    def get_symbol_freeze_epoch(self, symbol: str) -> int: ...

    def get_symbol_freeze_owners(self, symbol: str) -> dict: ...

    def get_symbol_freeze_reason(self, symbol: str) -> str: ...

    def get_venue_dead_man_switch_snapshot(self) -> dict: ...

    def get_venue_freeze_epoch(self, venue: str = "") -> int: ...

    def get_venue_freeze_reason(self, venue: str = "") -> str: ...

    def halt_system(self, reason: str) -> Any: ...

    def handle_durability_failure(
        self,
        exc: Exception,
        context: str,
        symbol: str = "",
    ) -> Any: ...

    def handle_venue_dead_man_switch_unhealthy(self, reason: str) -> Any: ...

    def has_trading_mode_constraint(self, prefixes=()) -> bool: ...

    def is_shutdown_started(self) -> bool: ...

    def poll_external_cash_flow_truth(self, query=None, now=None) -> bool: ...

    def record_risk_control_heartbeat(
        self,
        source: str,
        status: str = "healthy",
        reason: str = "",
        observed_at: float | None = None,
    ) -> bool: ...

    def renew_venue_dead_man_switch(self) -> bool: ...

    def request_venue_dead_man_switch_renewal(self) -> bool: ...

    def set_trading_mode(
        self,
        mode: OMSCapabilityMode,
        reason: str,
    ) -> Any: ...


RISK_OMS_PORT_MEMBERS = frozenset(RiskOMSPort.__annotations__) | frozenset(
    name
    for name, value in RiskOMSPort.__dict__.items()
    if callable(value) and not name.startswith("__")
)
