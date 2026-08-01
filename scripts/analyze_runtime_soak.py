"""Evaluate persisted AWS runtime-resource samples from a Paper run."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import sqlite3


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = PROJECT_ROOT / "storage" / "paper" / "trades.sqlite3"
RUNTIME_SCHEMA = "chronoshft.paper_runtime_resources.v1"
MIB = 1024.0 * 1024.0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Analyze ChronosHFT AWS Paper runtime soak evidence"
    )
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    parser.add_argument(
        "--run-id",
        default="",
        help="Paper run ID (default: latest run with runtime samples)",
    )
    parser.add_argument("--minimum-hours", type=float, default=24.0)
    parser.add_argument("--warmup-minutes", type=float, default=30.0)
    parser.add_argument("--max-gap-multiplier", type=float, default=3.0)
    parser.add_argument(
        "--max-rss-slope-mib-per-hour",
        type=float,
        default=5.0,
        help="Maximum post-warmup linear RSS growth slope",
    )
    return parser.parse_args(argv)


def _finite(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _integer(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _latest_runtime_run_id(connection) -> str:
    row = connection.execute(
        """
        SELECT run_id
        FROM paper_system_events
        WHERE event_kind = 'runtime_resources'
        ORDER BY event_id DESC
        LIMIT 1
        """
    ).fetchone()
    return str(row[0]) if row else ""


def analyze_runtime_soak(
    connection,
    *,
    run_id: str = "",
    minimum_hours: float = 24.0,
    warmup_minutes: float = 30.0,
    max_gap_multiplier: float = 3.0,
    max_rss_slope_mib_per_hour: float = 5.0,
):
    minimum_hours = float(minimum_hours)
    warmup_minutes = float(warmup_minutes)
    max_gap_multiplier = float(max_gap_multiplier)
    max_rss_slope_mib_per_hour = float(max_rss_slope_mib_per_hour)
    if not math.isfinite(minimum_hours) or minimum_hours < 0.0:
        raise ValueError("minimum_hours must be non-negative and finite")
    if not math.isfinite(warmup_minutes) or warmup_minutes < 0.0:
        raise ValueError("warmup_minutes must be non-negative and finite")
    if not math.isfinite(max_gap_multiplier) or max_gap_multiplier <= 1.0:
        raise ValueError("max_gap_multiplier must exceed one")
    if (
        not math.isfinite(max_rss_slope_mib_per_hour)
        or max_rss_slope_mib_per_hour < 0.0
    ):
        raise ValueError(
            "max_rss_slope_mib_per_hour must be non-negative and finite"
        )

    selected_run_id = str(run_id or "").strip() or _latest_runtime_run_id(
        connection
    )
    if not selected_run_id:
        return {
            "ready": False,
            "run_id": "",
            "reasons": ["no_runtime_resource_samples"],
        }

    rows = connection.execute(
        """
        SELECT event_id, event_time, severity, state, message
        FROM paper_system_events
        WHERE run_id = ? AND event_kind = 'runtime_resources'
        ORDER BY event_id
        """,
        (selected_run_id,),
    )

    total_rows = 0
    valid_rows = 0
    invalid_rows = 0
    first_event_time = None
    last_event_time = None
    previous_event_time = None
    expected_interval_sec = None
    max_gap_sec = 0.0
    gap_count = 0
    status_counts = Counter()
    unavailable_count = 0
    unhealthy_count = 0
    discovery_incomplete_count = 0
    missing_pid_sample_count = 0
    watchdog_disabled_count = 0
    watchdog_error_max = 0
    watchdog_send_first = None
    watchdog_send_last = None

    regression_origin = None
    regression_count = 0
    sum_x = 0.0
    sum_y = 0.0
    sum_xx = 0.0
    sum_xy = 0.0
    rss_min = None
    rss_max = None
    rss_last = None
    max_main_threads = 0
    max_total_threads = 0
    max_main_fds = 0
    max_total_fds = 0
    max_process_count = 0
    cpu_count = 0
    cpu_sum = 0.0
    cpu_max = None
    latest_limits = {}

    decoded_rows = []
    for row in rows:
        total_rows += 1
        event_time = _finite(row[1])
        try:
            details = json.loads(str(row[4] or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            invalid_rows += 1
            continue
        if not isinstance(details, dict) or details.get("schema") != RUNTIME_SCHEMA:
            invalid_rows += 1
            continue
        resource = details.get("resource")
        watchdog = details.get("systemd_watchdog")
        if (
            event_time is None
            or not isinstance(resource, dict)
            or not isinstance(watchdog, dict)
        ):
            invalid_rows += 1
            continue
        decoded_rows.append((event_time, resource, watchdog))

    if decoded_rows:
        first_event_time = decoded_rows[0][0]
        last_event_time = decoded_rows[-1][0]
    warmup_cutoff = (
        first_event_time + warmup_minutes * 60.0
        if first_event_time is not None
        else None
    )

    for event_time, resource, watchdog in decoded_rows:
        valid_rows += 1
        status = str(resource.get("status", "unknown") or "unknown")
        status_counts[status] += 1
        if not bool(resource.get("available", False)):
            unavailable_count += 1
        if not bool(resource.get("healthy", False)):
            unhealthy_count += 1
        if resource.get("process_discovery_complete") is not True:
            discovery_incomplete_count += 1
        if resource.get("missing_pids"):
            missing_pid_sample_count += 1
        if watchdog.get("enabled") is not True:
            watchdog_disabled_count += 1

        watchdog_errors = _integer(watchdog.get("error_count"))
        if watchdog_errors is not None:
            watchdog_error_max = max(watchdog_error_max, watchdog_errors)
        watchdog_sends = _integer(watchdog.get("send_count"))
        if watchdog_sends is not None:
            if watchdog_send_first is None:
                watchdog_send_first = watchdog_sends
            watchdog_send_last = watchdog_sends

        interval = _finite(resource.get("sample_interval_sec"))
        if interval is not None and interval > 0.0:
            expected_interval_sec = interval
        if previous_event_time is not None:
            gap = max(0.0, event_time - previous_event_time)
            max_gap_sec = max(max_gap_sec, gap)
            if (
                expected_interval_sec is not None
                and gap > expected_interval_sec * max_gap_multiplier
            ):
                gap_count += 1
        previous_event_time = event_time

        if warmup_cutoff is not None and event_time < warmup_cutoff:
            continue
        rss = _integer(resource.get("rss_bytes"))
        if rss is not None and rss >= 0:
            if regression_origin is None:
                regression_origin = event_time
            x = event_time - regression_origin
            y = float(rss)
            regression_count += 1
            sum_x += x
            sum_y += y
            sum_xx += x * x
            sum_xy += x * y
            rss_min = rss if rss_min is None else min(rss_min, rss)
            rss_max = rss if rss_max is None else max(rss_max, rss)
            rss_last = rss

        main_threads = _integer(resource.get("main_thread_count"))
        total_threads = _integer(resource.get("total_thread_count"))
        main_fds = _integer(resource.get("main_fd_count"))
        total_fds = _integer(resource.get("total_fd_count"))
        process_count = _integer(resource.get("process_count"))
        max_main_threads = max(max_main_threads, main_threads or 0)
        max_total_threads = max(max_total_threads, total_threads or 0)
        max_main_fds = max(max_main_fds, main_fds or 0)
        max_total_fds = max(max_total_fds, total_fds or 0)
        max_process_count = max(max_process_count, process_count or 0)

        cpu = _finite(resource.get("cpu_percent_one_core"))
        if cpu is not None:
            cpu_count += 1
            cpu_sum += cpu
            cpu_max = cpu if cpu_max is None else max(cpu_max, cpu)

        for field in (
            "rss_freeze_bytes",
            "max_main_threads",
            "max_total_threads",
            "max_main_fds",
            "max_total_fds",
            "max_processes",
            "cpu_warn_percent_one_core",
        ):
            if resource.get(field) is not None:
                latest_limits[field] = resource.get(field)

    observed_duration_hours = (
        max(0.0, last_event_time - first_event_time) / 3600.0
        if first_event_time is not None and last_event_time is not None
        else 0.0
    )
    analyzed_duration_hours = (
        max(0.0, last_event_time - regression_origin) / 3600.0
        if regression_origin is not None and last_event_time is not None
        else 0.0
    )
    denominator = regression_count * sum_xx - sum_x * sum_x
    rss_slope_bytes_per_sec = (
        (regression_count * sum_xy - sum_x * sum_y) / denominator
        if regression_count >= 2 and denominator > 0.0
        else None
    )
    rss_slope_mib_per_hour = (
        rss_slope_bytes_per_sec * 3600.0 / MIB
        if rss_slope_bytes_per_sec is not None
        else None
    )
    expected_samples = (
        int((last_event_time - first_event_time) / expected_interval_sec) + 1
        if (
            first_event_time is not None
            and last_event_time is not None
            and expected_interval_sec is not None
            and expected_interval_sec > 0.0
        )
        else None
    )
    sample_completeness = (
        min(1.0, valid_rows / expected_samples)
        if expected_samples
        else None
    )

    reasons = []
    if observed_duration_hours < minimum_hours:
        reasons.append(
            "insufficient_duration:"
            f"{observed_duration_hours:.3f}<{minimum_hours:.3f}h"
        )
    if valid_rows < 2 or regression_count < 2:
        reasons.append("insufficient_valid_runtime_samples")
    if invalid_rows:
        reasons.append(f"invalid_runtime_samples:{invalid_rows}")
    if unavailable_count:
        reasons.append(f"resource_monitor_unavailable:{unavailable_count}")
    if unhealthy_count:
        reasons.append(f"resource_monitor_unhealthy:{unhealthy_count}")
    if discovery_incomplete_count:
        reasons.append(
            f"process_discovery_incomplete:{discovery_incomplete_count}"
        )
    if missing_pid_sample_count:
        reasons.append(f"missing_child_pid_samples:{missing_pid_sample_count}")
    if watchdog_disabled_count:
        reasons.append(f"systemd_watchdog_disabled:{watchdog_disabled_count}")
    if watchdog_error_max:
        reasons.append(f"systemd_watchdog_errors:{watchdog_error_max}")
    if (
        observed_duration_hours > 0.0
        and watchdog_send_first is not None
        and watchdog_send_last is not None
        and watchdog_send_last <= watchdog_send_first
    ):
        reasons.append("systemd_watchdog_did_not_advance")
    if gap_count:
        reasons.append(f"runtime_sampling_gaps:{gap_count}")
    if sample_completeness is not None and sample_completeness < 0.95:
        reasons.append(
            f"runtime_sample_completeness:{sample_completeness:.4f}<0.95"
        )
    if (
        rss_slope_mib_per_hour is None
        or rss_slope_mib_per_hour > max_rss_slope_mib_per_hour
    ):
        reasons.append(
            "rss_slope_mib_per_hour:"
            f"{rss_slope_mib_per_hour}>{max_rss_slope_mib_per_hour}"
        )

    threshold_checks = (
        ("rss_freeze_bytes", rss_max, "rss_peak"),
        ("max_main_threads", max_main_threads, "main_thread_peak"),
        ("max_total_threads", max_total_threads, "total_thread_peak"),
        ("max_main_fds", max_main_fds, "main_fd_peak"),
        ("max_total_fds", max_total_fds, "total_fd_peak"),
        ("max_processes", max_process_count, "process_peak"),
    )
    for limit_name, observed, reason_name in threshold_checks:
        limit = _integer(latest_limits.get(limit_name))
        if limit is None:
            reasons.append(f"missing_runtime_limit:{limit_name}")
        elif observed is not None and observed >= limit:
            reasons.append(f"{reason_name}:{observed}>={limit}")

    return {
        "ready": not reasons,
        "run_id": selected_run_id,
        "reasons": reasons,
        "policy": {
            "minimum_hours": minimum_hours,
            "warmup_minutes": warmup_minutes,
            "max_gap_multiplier": max_gap_multiplier,
            "max_rss_slope_mib_per_hour": max_rss_slope_mib_per_hour,
        },
        "samples": {
            "total_rows": total_rows,
            "valid_rows": valid_rows,
            "invalid_rows": invalid_rows,
            "expected_samples": expected_samples,
            "completeness": sample_completeness,
            "observed_duration_hours": observed_duration_hours,
            "analyzed_duration_hours": analyzed_duration_hours,
            "expected_interval_sec": expected_interval_sec,
            "max_gap_sec": max_gap_sec,
            "gap_count": gap_count,
            "status_counts": dict(sorted(status_counts.items())),
        },
        "resources": {
            "rss_min_bytes": rss_min,
            "rss_max_bytes": rss_max,
            "rss_last_bytes": rss_last,
            "rss_slope_mib_per_hour": rss_slope_mib_per_hour,
            "max_main_threads": max_main_threads,
            "max_total_threads": max_total_threads,
            "max_main_fds": max_main_fds,
            "max_total_fds": max_total_fds,
            "max_process_count": max_process_count,
            "cpu_average_percent_one_core": (
                cpu_sum / cpu_count if cpu_count else None
            ),
            "cpu_max_percent_one_core": cpu_max,
            "unavailable_count": unavailable_count,
            "unhealthy_count": unhealthy_count,
            "process_discovery_incomplete_count": (
                discovery_incomplete_count
            ),
            "missing_pid_sample_count": missing_pid_sample_count,
        },
        "systemd_watchdog": {
            "disabled_sample_count": watchdog_disabled_count,
            "error_count_max": watchdog_error_max,
            "send_count_first": watchdog_send_first,
            "send_count_last": watchdog_send_last,
        },
        "limits": latest_limits,
    }


def main(argv=None) -> int:
    args = parse_args(argv)
    database = Path(args.database)
    if not database.is_file():
        print(f"Runtime soak analysis failed: database not found: {database}")
        return 2
    try:
        with sqlite3.connect(
            f"{database.resolve().as_uri()}?mode=ro",
            uri=True,
        ) as connection:
            report = analyze_runtime_soak(
                connection,
                run_id=args.run_id,
                minimum_hours=args.minimum_hours,
                warmup_minutes=args.warmup_minutes,
                max_gap_multiplier=args.max_gap_multiplier,
                max_rss_slope_mib_per_hour=(
                    args.max_rss_slope_mib_per_hour
                ),
            )
    except (sqlite3.Error, ValueError) as exc:
        print(f"Runtime soak analysis failed: {type(exc).__name__}: {exc}")
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ready") else 2


if __name__ == "__main__":
    raise SystemExit(main())
