"""Clock implementations for the isolated risk runtime."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import time


@dataclass(frozen=True, slots=True)
class FunctionRuntimeClock:
    """Adapt injected process functions to the RuntimeClock contract."""

    monotonic_fn: Callable[[], float]
    wall_time_fn: Callable[[], float]
    sleep_fn: Callable[[float], None]

    def monotonic(self) -> float:
        return float(self.monotonic_fn())

    def wall_time(self) -> float:
        return float(self.wall_time_fn())

    def utc_now_ms(self) -> int:
        return int(self.wall_time() * 1000.0)

    def sleep(self, seconds: float) -> None:
        self.sleep_fn(float(seconds))


def system_runtime_clock() -> FunctionRuntimeClock:
    return FunctionRuntimeClock(
        monotonic_fn=time.perf_counter,
        wall_time_fn=time.time,
        sleep_fn=time.sleep,
    )
