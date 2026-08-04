"""Non-blocking latest-value telemetry publication."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from threading import Condition, Thread
from typing import Any


class DashboardTelemetrySink:
    """Apply one readiness snapshot and one metrics tick to a dashboard."""

    def __init__(self, dashboard: Any) -> None:
        self.dashboard = dashboard

    def __call__(self, metrics: Mapping[str, Any]) -> None:
        readiness = metrics.get("runtime_readiness")
        if isinstance(readiness, Mapping):
            set_status = getattr(self.dashboard, "set_startup_status", None)
            if callable(set_status):
                ready = bool(readiness.get("ready", False))
                execution_enabled = bool(
                    readiness.get("execution_enabled", False)
                )
                reasons = readiness.get("reasons", ())
                if not isinstance(reasons, (list, tuple)):
                    reasons = (str(reasons),)
                set_status(
                    state="RUNNING" if ready else "RUNTIME_DEGRADED",
                    operating_mode=str(
                        readiness.get("operating_mode", "") or ""
                    ),
                    startup_blocked=False,
                    execution_enabled=execution_enabled,
                    restart_required=False,
                    reason="; ".join(str(item) for item in reasons if item),
                )
        self.dashboard.update_runtime_metrics(metrics)


class TelemetryPublisher:
    """Publish snapshots on a worker with a capacity-one mailbox.

    Producers never wait for component snapshot collection or JSON encoding.
    When the consumer is slow, an unconsumed tick is replaced by the newest
    value; readiness and telemetry therefore remain current without backlog.
    """

    def __init__(
        self,
        publish: Callable[[Mapping[str, Any]], Any],
        *,
        name: str = "runtime-telemetry",
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> None:
        if not callable(publish):
            raise TypeError("telemetry publish target must be callable")
        self._publish = publish
        self._monotonic = monotonic
        self._condition = Condition()
        self._pending: Mapping[str, Any] | None = None
        self._inflight = False
        self._stopping = False
        self._closed = False
        self._stats = {
            "submitted": 0,
            "published": 0,
            "replaced": 0,
            "publish_errors": 0,
            "last_error": "",
        }
        self._thread = Thread(target=self._run, daemon=True, name=name)
        self._thread.start()

    @classmethod
    def for_dashboard(cls, dashboard: Any) -> TelemetryPublisher:
        return cls(DashboardTelemetrySink(dashboard))

    def submit(self, snapshot: Mapping[str, Any]) -> bool:
        if not isinstance(snapshot, Mapping):
            raise TypeError("telemetry snapshot must be a mapping")
        with self._condition:
            if self._stopping or self._closed:
                return False
            self._stats["submitted"] += 1
            if self._pending is not None:
                self._stats["replaced"] += 1
            self._pending = snapshot
            self._condition.notify()
            return True

    def flush(self, timeout_sec: float = 5.0) -> bool:
        deadline = self._monotonic() + max(0.0, float(timeout_sec))
        with self._condition:
            while self._pending is not None or self._inflight:
                remaining = deadline - self._monotonic()
                if remaining <= 0.0:
                    return False
                self._condition.wait(timeout=remaining)
            return True

    def close(self, *, timeout_sec: float = 5.0, flush: bool = True) -> bool:
        timeout_sec = max(0.0, float(timeout_sec))
        deadline = self._monotonic() + timeout_sec
        flushed = self.flush(timeout_sec) if flush else True
        with self._condition:
            self._stopping = True
            if not flush:
                self._pending = None
            self._condition.notify_all()
        self._thread.join(timeout=max(0.0, deadline - self._monotonic()))
        with self._condition:
            stopped = not self._thread.is_alive()
            self._closed = stopped
        return bool(flushed and stopped)

    def metrics_snapshot(self) -> dict:
        with self._condition:
            return {
                **self._stats,
                "pending": self._pending is not None,
                "inflight": self._inflight,
                "closed": self._closed,
                "mailbox_capacity": 1,
            }

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._stopping:
                    self._condition.wait()
                if self._pending is None and self._stopping:
                    self._closed = True
                    self._condition.notify_all()
                    return
                snapshot = self._pending
                self._pending = None
                self._inflight = True

            try:
                self._publish(snapshot or {})
            except Exception as exc:
                with self._condition:
                    self._stats["publish_errors"] += 1
                    self._stats["last_error"] = (
                        f"{type(exc).__name__}:{exc}"
                    )
            else:
                with self._condition:
                    self._stats["published"] += 1
            finally:
                with self._condition:
                    self._inflight = False
                    self._condition.notify_all()


__all__ = ["DashboardTelemetrySink", "TelemetryPublisher"]
