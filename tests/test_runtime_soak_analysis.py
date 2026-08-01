import json
import sqlite3

import pytest

from scripts.analyze_runtime_soak import analyze_runtime_soak


def make_connection():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE paper_system_events (
            event_id INTEGER PRIMARY KEY,
            run_id TEXT,
            event_time REAL,
            event_kind TEXT,
            severity TEXT,
            message TEXT,
            state TEXT,
            recorded_at_utc TEXT
        )
        """
    )
    return connection


def runtime_payload(*, rss_bytes=100_000_000, discovery_complete=True, sends=1):
    return json.dumps(
        {
            "schema": "chronoshft.paper_runtime_resources.v1",
            "resource": {
                "available": True,
                "healthy": True,
                "status": "healthy",
                "rss_bytes": rss_bytes,
                "rss_freeze_bytes": 1_342_177_280,
                "main_thread_count": 10,
                "max_main_threads": 96,
                "total_thread_count": 15,
                "max_total_threads": 112,
                "main_fd_count": 20,
                "max_main_fds": 4096,
                "total_fd_count": 30,
                "max_total_fds": 8192,
                "cpu_percent_one_core": 5.0,
                "cpu_warn_percent_one_core": 150.0,
                "process_count": 3,
                "max_processes": 128,
                "process_discovery_complete": discovery_complete,
                "missing_pids": [],
                "sample_interval_sec": 5.0,
            },
            "systemd_watchdog": {
                "enabled": True,
                "send_count": sends,
                "error_count": 0,
            },
        },
        separators=(",", ":"),
    )


def insert_runtime_samples(connection, run_id, payloads):
    connection.executemany(
        """
        INSERT INTO paper_system_events(
            event_id, run_id, event_time, event_kind, severity,
            message, state, recorded_at_utc
        ) VALUES (?, ?, ?, 'runtime_resources', 'INFO', ?, 'healthy', 'now')
        """,
        (
            (index, run_id, 1000.0 + (index - 1) * 5.0, payload)
            for index, payload in enumerate(payloads, start=1)
        ),
    )


def test_runtime_soak_report_accepts_complete_stable_latest_run():
    connection = make_connection()
    insert_runtime_samples(
        connection,
        "run-1",
        [runtime_payload(sends=index) for index in range(1, 5)],
    )

    report = analyze_runtime_soak(
        connection,
        minimum_hours=0.004,
        warmup_minutes=0.0,
    )

    assert report["ready"] is True
    assert report["run_id"] == "run-1"
    assert report["reasons"] == []
    assert report["samples"]["valid_rows"] == 4
    assert report["samples"]["completeness"] == 1.0
    assert report["resources"]["rss_slope_mib_per_hour"] == pytest.approx(0.0)
    assert report["systemd_watchdog"]["send_count_last"] == 4


def test_runtime_soak_report_rejects_growth_and_incomplete_discovery():
    connection = make_connection()
    insert_runtime_samples(
        connection,
        "run-1",
        [
            runtime_payload(rss_bytes=100_000_000, sends=1),
            runtime_payload(rss_bytes=120_000_000, sends=2),
            runtime_payload(
                rss_bytes=140_000_000,
                discovery_complete=False,
                sends=3,
            ),
        ],
    )

    report = analyze_runtime_soak(
        connection,
        minimum_hours=0.0,
        warmup_minutes=0.0,
        max_rss_slope_mib_per_hour=1.0,
    )

    assert report["ready"] is False
    assert "process_discovery_incomplete:1" in report["reasons"]
    assert any(
        reason.startswith("rss_slope_mib_per_hour:")
        for reason in report["reasons"]
    )


@pytest.mark.parametrize(
    "field,value,match",
    (
        ("minimum_hours", -1.0, "minimum_hours"),
        ("warmup_minutes", float("nan"), "warmup_minutes"),
        ("max_gap_multiplier", 1.0, "max_gap_multiplier"),
        (
            "max_rss_slope_mib_per_hour",
            -1.0,
            "max_rss_slope_mib_per_hour",
        ),
    ),
)
def test_runtime_soak_policy_is_strict(field, value, match):
    connection = make_connection()

    with pytest.raises(ValueError, match=match):
        analyze_runtime_soak(connection, **{field: value})
