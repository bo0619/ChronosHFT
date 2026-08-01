import sys

import pytest

from infrastructure.process_resources import (
    ProcessResourceMonitor,
    _expand_linux_process_tree,
    _normalize_pids,
    _summarize_processes,
)


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeProvider:
    def __init__(
        self,
        *,
        rss_bytes=100,
        threads=2,
        fds=3,
        total_threads=None,
        total_fds=None,
    ):
        self.rss_bytes = rss_bytes
        self.threads = threads
        self.fds = fds
        self.total_threads = threads if total_threads is None else total_threads
        self.total_fds = fds if total_fds is None else total_fds
        self.cpu_seconds = 1.0
        self.calls = 0

    def __call__(self, pids):
        self.calls += 1
        self.cpu_seconds += 0.25
        return {
            "available": True,
            "reason": "sampled",
            "process_count": len(tuple(pids)),
            "missing_pids": [],
            "rss_bytes": self.rss_bytes,
            "main_thread_count": self.threads,
            "main_fd_count": self.fds,
            "total_thread_count": self.total_threads,
            "total_fd_count": self.total_fds,
            "cpu_seconds": self.cpu_seconds,
        }


def monitor_config(**overrides):
    config = {
        "enabled": True,
        "sample_interval_sec": 5.0,
        "rss_warn_bytes": 100,
        "rss_freeze_bytes": 200,
        "max_main_threads": 10,
        "max_main_fds": 20,
        "max_total_threads": 15,
        "max_total_fds": 30,
        "max_processes": 128,
        "cpu_warn_percent_one_core": 150.0,
        "breach_checks": 3,
        "recovery_checks": 2,
        "history_samples": 3,
        "require_available_on_linux": True,
        "require_complete_process_tree_on_linux": True,
    }
    config.update(overrides)
    return config


def test_resource_monitor_normalizes_optional_child_pids_safely():
    assert _normalize_pids(
        (7, "8", None, "not-a-pid", True, -1, 7, float("inf"))
    ) == (7, 8)


def test_process_summary_aggregates_parent_and_child_resources():
    summary = _summarize_processes(
        [
            {
                "pid": 1,
                "rss_bytes": 100,
                "thread_count": 3,
                "fd_count": 4,
                "cpu_seconds": 1.5,
            },
            {
                "pid": 2,
                "rss_bytes": 200,
                "thread_count": 5,
                "fd_count": 7,
                "cpu_seconds": 2.5,
            },
        ],
        [3],
    )

    assert summary["rss_bytes"] == 300
    assert summary["main_thread_count"] == 3
    assert summary["main_fd_count"] == 4
    assert summary["total_thread_count"] == 8
    assert summary["total_fd_count"] == 11
    assert summary["cpu_seconds"] == 4.0
    assert summary["missing_pids"] == [3]


def test_process_tree_discovery_is_recursive_deduplicated_and_bounded():
    children = {
        1: (2, 3),
        2: (4,),
        3: (4,),
        4: (),
    }

    expanded, complete, reason = _expand_linux_process_tree(
        (1,),
        child_provider=lambda pid: children[pid],
        max_processes=4,
    )

    assert expanded == (1, 2, 3, 4)
    assert complete is True
    assert reason == "complete"

    expanded, complete, reason = _expand_linux_process_tree(
        (1,),
        child_provider=lambda pid: children[pid],
        max_processes=3,
    )

    assert expanded == (1, 2, 3)
    assert complete is False
    assert reason == "process_limit_exceeded:3"


def test_process_tree_discovery_reports_unreadable_descendant():
    def children(pid):
        if pid == 2:
            raise PermissionError("hidden")
        return (2,) if pid == 1 else ()

    expanded, complete, reason = _expand_linux_process_tree(
        (1,),
        child_provider=children,
    )

    assert expanded == (1, 2)
    assert complete is False
    assert reason == "pid_2:PermissionError"


def test_resource_monitor_caches_samples_and_bounds_history():
    clock = FakeClock()
    provider = FakeProvider()
    monitor = ProcessResourceMonitor(
        monitor_config(),
        sample_provider=provider,
        monotonic=clock,
        main_pid=1,
    )

    first = monitor.sample([2])
    assert first["available"] is True
    assert monitor.sample([2]) == first
    assert provider.calls == 1

    for _index in range(4):
        clock.advance(5.0)
        monitor.sample([2])

    snapshot = monitor.snapshot()
    assert provider.calls == 5
    assert snapshot["history_depth"] == 3
    assert snapshot["history_capacity"] == 3
    assert snapshot["process_count"] == 2


def test_resource_monitor_requires_persistent_breach_and_one_shot_freeze():
    clock = FakeClock()
    provider = FakeProvider(rss_bytes=250)
    monitor = ProcessResourceMonitor(
        monitor_config(),
        sample_provider=provider,
        monotonic=clock,
        main_pid=1,
    )

    for expected_count in (1, 2):
        snapshot = monitor.sample(force=True)
        assert snapshot["healthy"] is True
        assert snapshot["breach_count"] == expected_count
        assert monitor.consume_fail_closed_reason() == ""
        clock.advance(5.0)

    snapshot = monitor.sample(force=True)
    assert snapshot["healthy"] is False
    reason = monitor.consume_fail_closed_reason()
    assert reason.startswith("rss_bytes:250>=200")
    assert monitor.consume_fail_closed_reason() == ""

    provider.rss_bytes = 50
    clock.advance(5.0)
    assert monitor.sample(force=True)["healthy"] is False
    clock.advance(5.0)
    recovered = monitor.sample(force=True)
    assert recovered["healthy"] is True
    assert recovered["status"] == "healthy"


@pytest.mark.parametrize(
    "provider,expected_reason",
    (
        (
            FakeProvider(threads=2, total_threads=16),
            "total_threads:16>=15",
        ),
        (
            FakeProvider(fds=3, total_fds=31),
            "total_fds:31>=30",
        ),
    ),
)
def test_child_resource_leak_triggers_total_limit(provider, expected_reason):
    monitor = ProcessResourceMonitor(
        monitor_config(breach_checks=1),
        sample_provider=provider,
        monotonic=FakeClock(),
        main_pid=1,
    )

    snapshot = monitor.sample([2], force=True)

    assert snapshot["healthy"] is False
    assert snapshot["main_thread_count"] < snapshot["max_main_threads"]
    assert snapshot["main_fd_count"] < snapshot["max_main_fds"]
    assert expected_reason in monitor.consume_fail_closed_reason()


def test_incomplete_process_discovery_fails_closed_on_linux(monkeypatch):
    provider = FakeProvider()

    def incomplete(pids):
        snapshot = provider(pids)
        snapshot["process_discovery_complete"] = False
        snapshot["process_discovery_reason"] = "process_limit_exceeded:128"
        return snapshot

    monkeypatch.setattr(sys, "platform", "linux")
    monitor = ProcessResourceMonitor(
        monitor_config(breach_checks=1),
        sample_provider=incomplete,
        monotonic=FakeClock(),
        main_pid=1,
    )

    snapshot = monitor.sample(force=True)

    assert snapshot["healthy"] is False
    assert monitor.consume_fail_closed_reason() == (
        "process_discovery:process_limit_exceeded:128"
    )


def test_unavailable_monitor_only_breaches_on_linux():
    clock = FakeClock()

    def unavailable(_pids):
        return {"available": False, "reason": "missing_proc"}

    monitor = ProcessResourceMonitor(
        monitor_config(breach_checks=1),
        sample_provider=unavailable,
        monotonic=clock,
        main_pid=1,
    )
    snapshot = monitor.sample(force=True)

    assert snapshot["healthy"] is (not sys.platform.startswith("linux"))
    if sys.platform.startswith("linux"):
        assert monitor.consume_fail_closed_reason() == "missing_proc"


@pytest.mark.parametrize(
    "field,value",
    (
        ("sample_interval_sec", 0),
        ("rss_warn_bytes", True),
        ("rss_freeze_bytes", 0),
        ("max_main_threads", "96"),
        ("max_main_fds", -1),
        ("max_total_threads", 0),
        ("max_total_fds", True),
        ("max_processes", 0),
        ("max_processes", 4097),
        ("cpu_warn_percent_one_core", float("nan")),
        ("breach_checks", 0),
        ("recovery_checks", 0),
        ("history_samples", 721),
    ),
)
def test_resource_monitor_configuration_is_strict(field, value):
    config = monitor_config()
    config[field] = value
    with pytest.raises(ValueError, match=field):
        ProcessResourceMonitor(config)


def test_resource_monitor_rejects_inverted_rss_thresholds():
    with pytest.raises(ValueError, match="rss_warn_bytes"):
        ProcessResourceMonitor(
            monitor_config(rss_warn_bytes=200, rss_freeze_bytes=200)
        )


@pytest.mark.parametrize(
    "field,value",
    (
        ("max_total_threads", 9),
        ("max_total_fds", 19),
    ),
)
def test_resource_monitor_rejects_total_limit_below_main(field, value):
    with pytest.raises(ValueError, match=field):
        ProcessResourceMonitor(monitor_config(**{field: value}))
