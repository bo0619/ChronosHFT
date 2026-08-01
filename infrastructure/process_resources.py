from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Mapping
import math
import os
from pathlib import Path
import sys
import time
from typing import Any


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _positive_float(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be positive and finite")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be positive and finite") from exc
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise ValueError(f"{field} must be positive and finite")
    return parsed


def _read_linux_process(pid: int) -> dict[str, Any]:
    process_dir = Path("/proc") / str(pid)
    status = {}
    with (process_dir / "status").open("r", encoding="ascii") as handle:
        for raw_line in handle:
            key, separator, value = raw_line.partition(":")
            if separator and key in {"VmRSS", "Threads"}:
                status[key] = value.strip()

    rss_parts = status.get("VmRSS", "").split()
    if len(rss_parts) != 2 or rss_parts[1] != "kB":
        raise OSError(f"VmRSS is unavailable for pid {pid}")
    rss_bytes = int(rss_parts[0]) * 1024
    thread_count = int(status.get("Threads", "0") or 0)
    if rss_bytes < 0 or thread_count <= 0:
        raise OSError(f"invalid process status for pid {pid}")

    with os.scandir(process_dir / "fd") as entries:
        fd_count = sum(1 for _entry in entries)

    stat_text = (process_dir / "stat").read_text(encoding="ascii")
    command_end = stat_text.rfind(")")
    if command_end < 0:
        raise OSError(f"invalid process stat for pid {pid}")
    stat_fields = stat_text[command_end + 2 :].split()
    if len(stat_fields) < 13:
        raise OSError(f"incomplete process stat for pid {pid}")
    clock_ticks = float(os.sysconf("SC_CLK_TCK"))
    cpu_seconds = (float(stat_fields[11]) + float(stat_fields[12])) / clock_ticks
    return {
        "pid": pid,
        "rss_bytes": rss_bytes,
        "thread_count": thread_count,
        "fd_count": fd_count,
        "cpu_seconds": cpu_seconds,
    }


def _normalize_pids(pids: Iterable[int]) -> tuple[int, ...]:
    normalized = []
    seen = set()
    for raw_pid in pids:
        if isinstance(raw_pid, bool):
            continue
        try:
            pid = int(raw_pid)
        except (TypeError, ValueError, OverflowError):
            continue
        if pid <= 0 or pid in seen:
            continue
        normalized.append(pid)
        seen.add(pid)
    return tuple(normalized)


def _read_linux_child_pids(pid: int) -> tuple[int, ...]:
    """Return children forked by any thread in one Linux process."""

    task_dir = Path("/proc") / str(pid) / "task"
    children = []
    with os.scandir(task_dir) as task_entries:
        for task_entry in task_entries:
            if not task_entry.name.isdigit() or not task_entry.is_dir(
                follow_symlinks=False
            ):
                continue
            try:
                raw_children = (
                    Path(task_entry.path) / "children"
                ).read_text(encoding="ascii")
            except (FileNotFoundError, ProcessLookupError):
                continue
            children.extend(raw_children.split())
    return _normalize_pids(children)


def _expand_linux_process_tree(
    root_pids: Iterable[int],
    *,
    child_provider: Callable[[int], Iterable[int]] = _read_linux_child_pids,
    max_processes: int = 128,
) -> tuple[tuple[int, ...], bool, str]:
    """Breadth-first process discovery with a strict traversal bound."""

    roots = _normalize_pids(root_pids)
    process_limit = _positive_int(max_processes, "max_processes")
    if len(roots) > process_limit:
        return roots[:process_limit], False, "root_process_limit_exceeded"

    discovered = list(roots)
    seen = set(roots)
    pending = deque(roots)
    discovery_errors = []
    while pending:
        parent_pid = pending.popleft()
        try:
            children = _normalize_pids(child_provider(parent_pid))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, TypeError, ValueError) as exc:
            discovery_errors.append(
                f"pid_{parent_pid}:{type(exc).__name__}"
            )
            continue
        for child_pid in children:
            if child_pid in seen:
                continue
            if len(discovered) >= process_limit:
                return (
                    tuple(discovered),
                    False,
                    f"process_limit_exceeded:{process_limit}",
                )
            discovered.append(child_pid)
            seen.add(child_pid)
            pending.append(child_pid)

    if discovery_errors:
        return tuple(discovered), False, ",".join(discovery_errors[:8])
    return tuple(discovered), True, "complete"


def _summarize_processes(
    processes: list[dict[str, Any]],
    missing_pids: list[int],
) -> dict[str, Any]:
    if not processes:
        raise ValueError("at least one process sample is required")
    main = processes[0]
    return {
        "available": True,
        "reason": "sampled",
        "process_count": len(processes),
        "missing_pids": list(missing_pids),
        "rss_bytes": sum(item["rss_bytes"] for item in processes),
        "main_rss_bytes": main["rss_bytes"],
        "main_thread_count": main["thread_count"],
        "main_fd_count": main["fd_count"],
        "total_thread_count": sum(
            item["thread_count"] for item in processes
        ),
        "total_fd_count": sum(item["fd_count"] for item in processes),
        "cpu_seconds": sum(item["cpu_seconds"] for item in processes),
        "processes": processes,
    }


def sample_linux_processes(
    pids: Iterable[int],
    *,
    max_processes: int = 128,
) -> dict[str, Any]:
    if not sys.platform.startswith("linux") or not Path("/proc").is_dir():
        return {"available": False, "reason": "unsupported_platform"}

    roots = _normalize_pids(pids)
    if not roots:
        return {"available": False, "reason": "no_process_ids"}
    normalized, discovery_complete, discovery_reason = (
        _expand_linux_process_tree(
            roots,
            max_processes=max_processes,
        )
    )

    processes = []
    missing_pids = []
    for pid in normalized:
        try:
            processes.append(_read_linux_process(pid))
        except (FileNotFoundError, ProcessLookupError):
            missing_pids.append(pid)
        except (OSError, TypeError, ValueError) as exc:
            if pid == normalized[0]:
                return {
                    "available": False,
                    "reason": f"main_process_read_failed:{type(exc).__name__}",
                }
            missing_pids.append(pid)
    if not processes or processes[0]["pid"] != roots[0]:
        return {"available": False, "reason": "main_process_unavailable"}

    summary = _summarize_processes(processes, missing_pids)
    summary.update(
        {
            "process_discovery_complete": discovery_complete,
            "process_discovery_reason": discovery_reason,
            "explicit_process_count": len(roots),
            "discovered_process_count": max(0, len(normalized) - len(roots)),
            "process_limit": max_processes,
        }
    )
    return summary


class ProcessResourceMonitor:
    """Low-frequency, bounded process telemetry for a small Linux host."""

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        sample_provider: Callable[[Iterable[int]], Mapping[str, Any]] | None = None,
        monotonic: Callable[[], float] = time.perf_counter,
        main_pid: int | None = None,
    ) -> None:
        settings = dict(config or {})
        self.enabled = bool(settings.get("enabled", True))
        self.sample_interval_sec = _positive_float(
            settings.get("sample_interval_sec", 5.0),
            "resource_monitor.sample_interval_sec",
        )
        self.rss_warn_bytes = _positive_int(
            settings.get("rss_warn_bytes", 768 * 1024 * 1024),
            "resource_monitor.rss_warn_bytes",
        )
        self.rss_freeze_bytes = _positive_int(
            settings.get("rss_freeze_bytes", 1280 * 1024 * 1024),
            "resource_monitor.rss_freeze_bytes",
        )
        if self.rss_warn_bytes >= self.rss_freeze_bytes:
            raise ValueError(
                "resource_monitor.rss_warn_bytes must be less than "
                "rss_freeze_bytes"
            )
        self.max_main_threads = _positive_int(
            settings.get("max_main_threads", 96),
            "resource_monitor.max_main_threads",
        )
        self.max_main_fds = _positive_int(
            settings.get("max_main_fds", 4096),
            "resource_monitor.max_main_fds",
        )
        self.max_total_threads = _positive_int(
            settings.get("max_total_threads", 112),
            "resource_monitor.max_total_threads",
        )
        if self.max_total_threads < self.max_main_threads:
            raise ValueError(
                "resource_monitor.max_total_threads must be greater than or "
                "equal to max_main_threads"
            )
        self.max_total_fds = _positive_int(
            settings.get("max_total_fds", 8192),
            "resource_monitor.max_total_fds",
        )
        if self.max_total_fds < self.max_main_fds:
            raise ValueError(
                "resource_monitor.max_total_fds must be greater than or "
                "equal to max_main_fds"
            )
        self.max_processes = _positive_int(
            settings.get("max_processes", 128),
            "resource_monitor.max_processes",
        )
        if self.max_processes > 4096:
            raise ValueError("resource_monitor.max_processes must be <= 4096")
        self.require_complete_process_tree_on_linux = bool(
            settings.get("require_complete_process_tree_on_linux", True)
        )
        self.cpu_warn_percent_one_core = _positive_float(
            settings.get("cpu_warn_percent_one_core", 150.0),
            "resource_monitor.cpu_warn_percent_one_core",
        )
        self.breach_checks = _positive_int(
            settings.get("breach_checks", 3),
            "resource_monitor.breach_checks",
        )
        self.recovery_checks = _positive_int(
            settings.get("recovery_checks", 6),
            "resource_monitor.recovery_checks",
        )
        history_samples = _positive_int(
            settings.get("history_samples", 12),
            "resource_monitor.history_samples",
        )
        if history_samples > 720:
            raise ValueError("resource_monitor.history_samples must be <= 720")
        self.require_available_on_linux = bool(
            settings.get("require_available_on_linux", True)
        )

        self._sample_provider = sample_provider or (
            lambda pids: sample_linux_processes(
                pids,
                max_processes=self.max_processes,
            )
        )
        self._monotonic = monotonic
        self._main_pid = int(main_pid or os.getpid())
        self._history: deque[tuple[float, int, int, int]] = deque(
            maxlen=history_samples
        )
        self._last_sample_at = 0.0
        self._last_cpu_seconds: float | None = None
        self._last_cpu_sample_at = 0.0
        self._breach_count = 0
        self._recovery_count = 0
        self._latched_unhealthy = False
        self._pending_fail_closed_reason = ""
        self._snapshot = {
            "enabled": self.enabled,
            "available": False,
            "healthy": True,
            "status": "disabled" if not self.enabled else "not_sampled",
        }

    def sample(
        self,
        extra_pids: Iterable[int] = (),
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        now = self._monotonic()
        if not self.enabled:
            return dict(self._snapshot)
        if (
            not force
            and self._last_sample_at > 0.0
            and now - self._last_sample_at < self.sample_interval_sec
        ):
            return dict(self._snapshot)

        pids = (self._main_pid, *tuple(extra_pids))
        try:
            raw = dict(self._sample_provider(pids))
        except Exception as exc:
            raw = {
                "available": False,
                "reason": f"provider_failed:{type(exc).__name__}",
            }
        self._last_sample_at = now
        available = bool(raw.get("available", False))
        cpu_percent = self._cpu_percent(raw, now) if available else None
        rss_bytes = int(raw.get("rss_bytes", 0) or 0) if available else 0
        main_threads = (
            int(raw.get("main_thread_count", 0) or 0) if available else 0
        )
        main_fds = int(raw.get("main_fd_count", 0) or 0) if available else 0
        total_threads = (
            int(raw.get("total_thread_count", main_threads) or 0)
            if available
            else 0
        )
        total_fds = (
            int(raw.get("total_fd_count", main_fds) or 0)
            if available
            else 0
        )
        if available:
            self._history.append((now, rss_bytes, main_threads, main_fds))

        breaches = []
        warnings = []
        availability_required = (
            self.require_available_on_linux and sys.platform.startswith("linux")
        )
        process_tree_required = (
            self.require_complete_process_tree_on_linux
            and sys.platform.startswith("linux")
        )
        if not available:
            if availability_required:
                breaches.append(str(raw.get("reason", "unavailable")))
        else:
            if process_tree_required and not bool(
                raw.get("process_discovery_complete", True)
            ):
                breaches.append(
                    "process_discovery:"
                    f"{raw.get('process_discovery_reason', 'incomplete')}"
                )
            if rss_bytes >= self.rss_freeze_bytes:
                breaches.append(
                    f"rss_bytes:{rss_bytes}>={self.rss_freeze_bytes}"
                )
            elif rss_bytes >= self.rss_warn_bytes:
                warnings.append(
                    f"rss_bytes:{rss_bytes}>={self.rss_warn_bytes}"
                )
            if main_threads >= self.max_main_threads:
                breaches.append(
                    f"main_threads:{main_threads}>={self.max_main_threads}"
                )
            if main_fds >= self.max_main_fds:
                breaches.append(f"main_fds:{main_fds}>={self.max_main_fds}")
            if total_threads >= self.max_total_threads:
                breaches.append(
                    f"total_threads:{total_threads}>={self.max_total_threads}"
                )
            if total_fds >= self.max_total_fds:
                breaches.append(
                    f"total_fds:{total_fds}>={self.max_total_fds}"
                )
            if (
                cpu_percent is not None
                and cpu_percent >= self.cpu_warn_percent_one_core
            ):
                warnings.append(
                    "cpu_percent_one_core:"
                    f"{cpu_percent:.1f}>={self.cpu_warn_percent_one_core:.1f}"
                )

        if breaches:
            self._breach_count += 1
            self._recovery_count = 0
            if self._breach_count >= self.breach_checks:
                if not self._latched_unhealthy:
                    self._pending_fail_closed_reason = ";".join(breaches)
                self._latched_unhealthy = True
        else:
            self._breach_count = 0
            if self._latched_unhealthy:
                self._recovery_count += 1
                if self._recovery_count >= self.recovery_checks:
                    self._latched_unhealthy = False
                    self._recovery_count = 0
            else:
                self._recovery_count = 0

        status = "healthy"
        if self._latched_unhealthy:
            status = "unhealthy"
        elif breaches:
            status = "breach_pending"
        elif warnings:
            status = "warning"
        elif not available:
            status = "unavailable"
        self._snapshot = {
            "enabled": True,
            "available": available,
            "healthy": not self._latched_unhealthy,
            "status": status,
            "reason": str(raw.get("reason", "") or ""),
            "breaches": breaches,
            "warnings": warnings,
            "breach_count": self._breach_count,
            "breach_checks": self.breach_checks,
            "recovery_count": self._recovery_count,
            "recovery_checks": self.recovery_checks,
            "rss_bytes": rss_bytes if available else None,
            "rss_warn_bytes": self.rss_warn_bytes,
            "rss_freeze_bytes": self.rss_freeze_bytes,
            "rss_growth_bytes_per_min": self._rss_growth_per_minute(),
            "main_thread_count": main_threads if available else None,
            "max_main_threads": self.max_main_threads,
            "main_fd_count": main_fds if available else None,
            "max_main_fds": self.max_main_fds,
            "total_thread_count": total_threads if available else None,
            "max_total_threads": self.max_total_threads,
            "total_fd_count": total_fds if available else None,
            "max_total_fds": self.max_total_fds,
            "cpu_percent_one_core": cpu_percent,
            "cpu_warn_percent_one_core": self.cpu_warn_percent_one_core,
            "process_count": raw.get("process_count"),
            "explicit_process_count": raw.get("explicit_process_count"),
            "discovered_process_count": raw.get(
                "discovered_process_count"
            ),
            "process_discovery_complete": raw.get(
                "process_discovery_complete"
            ),
            "process_discovery_reason": raw.get(
                "process_discovery_reason"
            ),
            "max_processes": self.max_processes,
            "missing_pids": list(raw.get("missing_pids", ()) or ()),
            "sample_interval_sec": self.sample_interval_sec,
            "history_depth": len(self._history),
            "history_capacity": self._history.maxlen,
            "sampled_at_monotonic": now,
        }
        return dict(self._snapshot)

    def _cpu_percent(self, raw: Mapping[str, Any], now: float) -> float | None:
        try:
            cpu_seconds = float(raw.get("cpu_seconds"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(cpu_seconds) or cpu_seconds < 0.0:
            return None
        result = None
        if self._last_cpu_seconds is not None:
            elapsed = now - self._last_cpu_sample_at
            cpu_delta = cpu_seconds - self._last_cpu_seconds
            if elapsed > 0.0 and cpu_delta >= 0.0:
                result = cpu_delta / elapsed * 100.0
        self._last_cpu_seconds = cpu_seconds
        self._last_cpu_sample_at = now
        return result

    def _rss_growth_per_minute(self) -> float | None:
        if len(self._history) < 2:
            return None
        first_time, first_rss, _threads, _fds = self._history[0]
        last_time, last_rss, _threads, _fds = self._history[-1]
        elapsed = last_time - first_time
        if elapsed <= 0.0:
            return None
        return (last_rss - first_rss) / elapsed * 60.0

    def consume_fail_closed_reason(self) -> str:
        reason = self._pending_fail_closed_reason
        self._pending_fail_closed_reason = ""
        return reason

    def snapshot(self) -> dict[str, Any]:
        return dict(self._snapshot)
