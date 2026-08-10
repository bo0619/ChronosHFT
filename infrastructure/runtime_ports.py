"""Narrow domain ports owned by the main process composition root."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ClockPort(Protocol):
    def monotonic(self) -> float: ...

    def monotonic_ns(self) -> int: ...

    def wall_time(self) -> float: ...

    def now_ms(self) -> int: ...

    def now_ns(self) -> int: ...

    def now_seconds(self) -> float: ...

    def health_snapshot(self) -> dict: ...

    def sleep(self, seconds: float) -> None: ...


@runtime_checkable
class MarketCachePort(Protocol):
    def get_mark_price(self, symbol: str) -> float: ...

    def get_best_quote(self, symbol: str) -> tuple[float, float]: ...

    def get_last_trade_price(self, symbol: str) -> float: ...

    def get_risk_snapshot(
        self,
        symbol: str,
        *,
        now: float | None = None,
    ) -> dict: ...


@runtime_checkable
class ReferenceDataPort(Protocol):
    def init(self, testnet: bool = False) -> None: ...

    def get_info(self, symbol: str): ...

    def supports_rpi(self, symbol: str) -> bool: ...

    def round_price(
        self,
        symbol: str,
        price: float,
        direction: str = "nearest",
    ) -> float: ...

    def round_qty(self, symbol: str, quantity: float) -> float: ...


@dataclass(frozen=True, slots=True)
class MainProcessClock:
    """Unify monotonic process time with exchange-corrected epoch time."""

    exchange_clock: Any
    monotonic_fn: Callable[[], float]
    monotonic_ns_fn: Callable[[], int]
    wall_time_fn: Callable[[], float]
    sleep_fn: Callable[[float], None]

    def monotonic(self) -> float:
        return float(self.monotonic_fn())

    def monotonic_ns(self) -> int:
        return int(self.monotonic_ns_fn())

    def wall_time(self) -> float:
        return float(self.wall_time_fn())

    def now_ms(self) -> int:
        return self.now_ns() // 1_000_000

    def now_ns(self) -> int:
        reader = getattr(self.exchange_clock, "now_ns", None)
        if not callable(reader):
            raise RuntimeError("exchange clock does not provide now_ns")
        return int(reader())

    def now_seconds(self) -> float:
        reader = getattr(self.exchange_clock, "now_seconds", None)
        if not callable(reader):
            raise RuntimeError("exchange clock does not provide now_seconds")
        return float(reader())

    def health_snapshot(self) -> dict:
        reader = getattr(self.exchange_clock, "health_snapshot", None)
        if not callable(reader):
            raise RuntimeError("exchange clock does not provide health_snapshot")
        try:
            snapshot = reader(notify_listeners=False)
        except TypeError:
            snapshot = reader()
        if not isinstance(snapshot, dict):
            raise RuntimeError("exchange clock health snapshot must be an object")
        return snapshot

    def sleep(self, seconds: float) -> None:
        self.sleep_fn(float(seconds))


@dataclass(frozen=True, slots=True)
class RuntimeDomainPorts:
    clock: ClockPort
    market_cache: MarketCachePort
    reference_data: ReferenceDataPort

    def __post_init__(self) -> None:
        if not isinstance(self.clock, ClockPort):
            raise TypeError("clock does not satisfy ClockPort")
        if not isinstance(self.market_cache, MarketCachePort):
            raise TypeError("market_cache does not satisfy MarketCachePort")
        if not isinstance(self.reference_data, ReferenceDataPort):
            raise TypeError(
                "reference_data does not satisfy ReferenceDataPort"
            )


@dataclass(frozen=True, slots=True)
class RuntimeDomainComposition:
    ports: RuntimeDomainPorts
    oms_type: Callable
    strategy_factory: Callable


def compose_runtime_domain(
    exchange_clock: Any,
    market_cache: MarketCachePort,
    reference_data: ReferenceDataPort,
    oms_type: Callable,
    strategy_factory: Callable,
) -> RuntimeDomainComposition:
    """Bind the process-wide ports to the factories that consume them."""

    ports = RuntimeDomainPorts(
        clock=MainProcessClock(
            exchange_clock=exchange_clock,
            monotonic_fn=time.perf_counter,
            monotonic_ns_fn=time.perf_counter_ns,
            wall_time_fn=time.time,
            sleep_fn=time.sleep,
        ),
        market_cache=market_cache,
        reference_data=reference_data,
    )
    return RuntimeDomainComposition(
        ports=ports,
        oms_type=partial(
            oms_type,
            clock=ports.clock,
            market_cache=ports.market_cache,
            reference_data=ports.reference_data,
        ),
        strategy_factory=partial(
            strategy_factory,
            clock=ports.clock,
            reference_data=ports.reference_data,
        ),
    )


def read_clock_health(clock_service: Any) -> dict:
    """Read clock telemetry without dispatching health listeners."""

    health_reader = getattr(clock_service, "health_snapshot", None)
    if not callable(health_reader):
        return {}
    try:
        health = health_reader(notify_listeners=False)
    except TypeError:
        health = health_reader()
    return health if isinstance(health, dict) else {}


__all__ = [
    "ClockPort",
    "MainProcessClock",
    "MarketCachePort",
    "ReferenceDataPort",
    "RuntimeDomainComposition",
    "RuntimeDomainPorts",
    "compose_runtime_domain",
    "read_clock_health",
]
