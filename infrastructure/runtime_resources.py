"""Typed ownership for resources created during application startup."""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from typing import Generic, TypeVar


T = TypeVar("T")


class _ResourceSlot(Generic[T]):
    def __init__(self, key: str):
        self.key = key

    def __get__(self, instance, owner=None) -> T | None:
        if instance is None:
            return self
        return instance.get(self.key)

    def __set__(self, instance, value: T | None) -> None:
        instance[self.key] = value


class RuntimeResources(MutableMapping[str, object]):
    """Runtime resource registry with typed fields and legacy mapping access.

    Wrapping an existing mutable mapping keeps startup tests and partial-failure
    cleanup compatible while new application code uses named dependencies.
    """

    __slots__ = ("_values",)

    systemd_watchdog: object | None = _ResourceSlot("systemd_watchdog")
    external_alerts: object | None = _ResourceSlot("external_alerts")
    time_service: object | None = _ResourceSlot("time_service")
    engine: object | None = _ResourceSlot("engine")
    gateway: object | None = _ResourceSlot("gateway")
    truth_provider: object | None = _ResourceSlot("truth_provider")
    oms: object | None = _ResourceSlot("oms")
    risk_controller: object | None = _ResourceSlot("risk_controller")
    risk_supervisor: object | None = _ResourceSlot("risk_supervisor")
    strategy: object | None = _ResourceSlot("strategy")
    strategy_runtime: object | None = _ResourceSlot("strategy_runtime")
    data_recorder: object | None = _ResourceSlot("data_recorder")
    resource_monitor: object | None = _ResourceSlot("resource_monitor")
    live_evidence_recorder: object | None = _ResourceSlot(
        "live_evidence_recorder"
    )
    recorder: object | None = _ResourceSlot("recorder")
    truth_monitor: object | None = _ResourceSlot("truth_monitor")
    venue_supervisor: object | None = _ResourceSlot("venue_supervisor")
    admin_control: object | None = _ResourceSlot("admin_control")
    web_dashboard: object | None = _ResourceSlot("web_dashboard")
    event_bindings: object | None = _ResourceSlot("event_bindings")
    failure_policy: object | None = _ResourceSlot("failure_policy")
    control_loop: object | None = _ResourceSlot("control_loop")
    watchdog_state: object | None = _ResourceSlot("watchdog_state")

    def __init__(self, values: MutableMapping[str, object] | None = None):
        self._values = values if values is not None else {}

    @classmethod
    def coerce(
        cls,
        runtime: RuntimeResources | MutableMapping[str, object] | None,
    ) -> RuntimeResources:
        if isinstance(runtime, cls):
            return runtime
        if runtime is None:
            return cls()
        if not isinstance(runtime, MutableMapping):
            raise TypeError("runtime resources must be a mutable mapping")
        return cls(runtime)

    @property
    def config(self) -> dict:
        value = self.get("config", {})
        return value if isinstance(value, dict) else {}

    @config.setter
    def config(self, value: dict) -> None:
        self["config"] = value

    @property
    def event_engine_config(self) -> dict:
        value = self.get("event_engine_config", {})
        return value if isinstance(value, dict) else {}

    @event_engine_config.setter
    def event_engine_config(self, value: dict) -> None:
        self["event_engine_config"] = value

    @property
    def paper_trade(self) -> bool:
        return bool(self.get("paper_trade", False))

    @paper_trade.setter
    def paper_trade(self, value: bool) -> None:
        self["paper_trade"] = bool(value)

    @property
    def account_shutdown_proof_required(self) -> bool:
        return bool(self.get("_account_shutdown_proof_required", False))

    @account_shutdown_proof_required.setter
    def account_shutdown_proof_required(self, value: bool) -> None:
        self["_account_shutdown_proof_required"] = bool(value)

    @property
    def risk_supervisor_started(self) -> bool:
        return bool(self.get("_risk_supervisor_started", False))

    @risk_supervisor_started.setter
    def risk_supervisor_started(self, value: bool) -> None:
        self["_risk_supervisor_started"] = bool(value)

    def __getitem__(self, key: str) -> object:
        return self._values[key]

    def __setitem__(self, key: str, value: object) -> None:
        self._values[key] = value

    def __delitem__(self, key: str) -> None:
        del self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)
