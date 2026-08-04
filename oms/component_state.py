"""Owned state cells and narrow attribute bindings for OMS components."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any


# Recovery and reset workflows may hydrate another component's state, but the
# storage owner remains singular and explicit. Any newly shared writer must be
# assigned here or OMS module import fails closed.
MULTI_WRITER_STATE_OWNERS = {
    "_account_state_event_time": "OMSExchangeEventProcessor",
    "_exchange_account_event_time": "OMSExchangeEventProcessor",
    "_lifecycle_generation": "OMSLifecycleController",
    "_outbound_all_order_seal_reason": "OMSLifecycleController",
    "_recovered_guard_cleanup_snapshot": "OMSGuardManager",
    "_rpi_calibration_budget_exhausted": "RpiCalibrationRuntime",
    "_rpi_calibration_cumulative_notional_microu": "RpiCalibrationRuntime",
    "_rpi_calibration_effective_loss_cap_microu": "RpiCalibrationRuntime",
    "_rpi_calibration_expired": "RpiCalibrationRuntime",
    "_rpi_calibration_expiry_reason": "RpiCalibrationRuntime",
    "_rpi_calibration_last_reserved_exchange_ns": "RpiCalibrationRuntime",
    "_rpi_calibration_peak_observed_loss_microu": "RpiCalibrationRuntime",
    "_rpi_calibration_permit_activated": "RpiCalibrationRuntime",
    "_rpi_calibration_permit_start_notional_microu": "RpiCalibrationRuntime",
    "_rpi_calibration_permit_start_order_count": "RpiCalibrationRuntime",
    "_rpi_calibration_reserved_order_count": "RpiCalibrationRuntime",
    "_rpi_calibration_restart_rearm_blocked": "RpiCalibrationRuntime",
    "_rpi_calibration_start_equity_microu": "RpiCalibrationRuntime",
    "_rpi_calibration_start_external_cash_flow_microu": "RpiCalibrationRuntime",
    "_rpi_calibration_terminal_empty_snapshots": "RpiCalibrationRuntime",
    "_rpi_calibration_terminal_generation": "RpiCalibrationRuntime",
    "_rpi_calibration_terminal_pending_reason": "RpiCalibrationRuntime",
    "_rpi_calibration_terminal_verified": "RpiCalibrationRuntime",
    "_shutdown_cancel_verified": "OMSCancellationManager",
    "capability_mode": "OMSCapabilityManager",
    "capability_reason": "OMSCapabilityManager",
    "external_cash_flow_scan_end_ms": "OMSAccountTruth",
    "last_freeze_reason": "OMSLifecycleController",
    "last_halt_reason": "OMSLifecycleController",
    "manual_rearm_required": "OMSLifecycleController",
    "mode_constraint_generation": "OMSCapabilityManager",
    "reconcile_retry_scheduled": "OMSReconciler",
    "recovered_guard_cleanup_pending": "OMSGuardManager",
    "state": "OMSLifecycleController",
    "symbol_guard_epoch_counters": "OMSGuardManager",
    "symbol_guard_records": "OMSGuardManager",
    "venue_guard_epoch_counters": "OMSGuardManager",
    "venue_guard_records": "OMSGuardManager",
}


@dataclass(frozen=True, slots=True)
class OMSAttributeBinding:
    read: Callable[[], Any]
    write: Callable[[Any], None] | None


class OMSStateRegistry:
    """Partition shared values into cells owned by one named component."""

    __slots__ = ("_field_owners", "_last_writers", "_values")

    def __init__(self, field_owners: Mapping[str, str]) -> None:
        self._field_owners = dict(field_owners)
        self._values: dict[str, dict[str, Any]] = {
            owner: {} for owner in set(field_owners.values())
        }
        self._last_writers: dict[str, str] = {}

    @property
    def field_owners(self) -> Mapping[str, str]:
        return dict(self._field_owners)

    def manages(self, name: str) -> bool:
        return name in self._field_owners

    def owner_of(self, name: str) -> str:
        try:
            return self._field_owners[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def read(self, name: str) -> Any:
        owner = self.owner_of(name)
        try:
            return self._values[owner][name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def write(self, writer: str, name: str, value: Any) -> None:
        owner = self.owner_of(name)
        self._values[owner][name] = value
        self._last_writers[name] = str(writer)

    def last_writer(self, name: str) -> str | None:
        return self._last_writers.get(name)


class OMSSharedField:
    """Facade compatibility descriptor backed by ``OMSStateRegistry``."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    @staticmethod
    def _registry(instance) -> OMSStateRegistry:
        registry = vars(instance).get("_component_state")
        if registry is None:
            owners = getattr(type(instance), "_component_state_field_owners")
            registry = OMSStateRegistry(owners)
            vars(instance)["_component_state"] = registry
        return registry

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        return self._registry(instance).read(self.name)

    def __set__(self, instance, value) -> None:
        self._registry(instance).write("OMSFacade", self.name, value)


def build_state_owners(component_types: Iterable[type]) -> dict[str, str]:
    """Derive unique storage ownership from component write manifests."""

    writers: dict[str, set[str]] = {}
    initializer_fields: set[str] = set()
    component_names = set()
    for component_type in component_types:
        name = component_type.__name__
        component_names.add(name)
        fields = set(component_type.OWNER_WRITES)
        if name == "OMSInitializer":
            initializer_fields.update(fields)
            continue
        for field in fields:
            writers.setdefault(field, set()).add(name)

    owners: dict[str, str] = {}
    for field in sorted(initializer_fields | set(writers)):
        field_writers = writers.get(field, set())
        if len(field_writers) == 1:
            owners[field] = next(iter(field_writers))
            continue
        if not field_writers:
            owners[field] = "OMSInitializer"
            continue
        declared_owner = MULTI_WRITER_STATE_OWNERS.get(field)
        if declared_owner not in field_writers:
            raise RuntimeError(
                f"OMS shared field {field!r} has writers "
                f"{sorted(field_writers)!r} but no valid canonical owner"
            )
        owners[field] = declared_owner

    stale = set(MULTI_WRITER_STATE_OWNERS).difference(
        field
        for field, field_writers in writers.items()
        if len(field_writers) > 1
    )
    if stale:
        raise RuntimeError(
            f"stale OMS multi-writer ownership declarations: {sorted(stale)!r}"
        )
    unknown = set(owners.values()).difference(component_names)
    if unknown:
        raise RuntimeError(f"unknown OMS state owners: {sorted(unknown)!r}")
    return owners
