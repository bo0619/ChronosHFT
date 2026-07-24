from concurrent.futures import ThreadPoolExecutor

from gateway.binance.rest_metrics import (
    BinanceRestMetrics,
    parse_binance_rate_limit_headers,
)
from ui.web_dashboard import _rest_telemetry_sections


def test_parse_binance_rate_limit_headers_is_case_insensitive_and_strict():
    parsed = parse_binance_rate_limit_headers(
        {
            "x-mbx-used-weight-1m": " 17 ",
            "X-MBX-ORDER-COUNT-10s": "2",
            "X-MBX-ORDER-COUNT-1M": 4,
            "X-MBX-USED-WEIGHT": "99",
            "X-MBX-USED-WEIGHT-1H": "-1",
            "X-MBX-ORDER-COUNT-1D": "1.5",
            "X-OTHER": "ignored",
        }
    )

    assert parsed == {
        "used_weight": {"1M": 17},
        "order_count": {"10S": 2, "1M": 4},
    }


def test_response_metrics_track_latest_peaks_failures_and_rate_limit_resets():
    metrics = BinanceRestMetrics()
    metrics.record_response(
        method="get",
        endpoint="/fapi/v2/account",
        status_code=200,
        headers={
            "X-MBX-USED-WEIGHT-1M": "10",
            "X-MBX-ORDER-COUNT-10S": "1",
        },
        rtt_ns=3_000_000,
        completed_monotonic_ns=100,
    )
    metrics.record_response(
        method="GET",
        endpoint="/fapi/v2/account",
        status_code=429,
        headers={
            "X-MBX-USED-WEIGHT-1M": "12",
            "X-MBX-ORDER-COUNT-10S": "2",
        },
        rtt_ns=7_000_000,
        completed_monotonic_ns=200,
    )
    metrics.record_response(
        method="GET",
        endpoint="/fapi/v2/account",
        status_code=200,
        headers={
            "X-MBX-USED-WEIGHT-1M": "3",
            "X-MBX-ORDER-COUNT-10S": "0",
        },
        rtt_ns=2_000_000,
        completed_monotonic_ns=300,
    )

    snapshot = metrics.snapshot()

    assert snapshot["attempt_count"] == 3
    assert snapshot["success_count"] == 2
    assert snapshot["failure_count"] == 1
    assert snapshot["exception_count"] == 0
    assert snapshot["http_429_count"] == 1
    assert snapshot["http_418_count"] == 0
    assert snapshot["latest_status_code"] == 200
    assert snapshot["latest_rtt_ms"] == 2.0
    assert snapshot["peak_rtt_ms"] == 7.0
    assert snapshot["latest_method"] == "GET"
    assert snapshot["latest_endpoint"] == "/fapi/v2/account"
    assert snapshot["rate_limits"]["used_weight"]["1M"] == {
        "latest": 3,
        "peak": 12,
        "latest_completed_monotonic_ns": 300,
    }
    assert snapshot["rate_limits"]["order_count"]["10S"] == {
        "latest": 0,
        "peak": 2,
        "latest_completed_monotonic_ns": 300,
    }

    route = snapshot["routes"]["GET /fapi/v2/account"]
    assert route["attempt_count"] == 3
    assert route["success_count"] == 2
    assert route["failure_count"] == 1
    assert route["http_429_count"] == 1
    assert route["latest_rtt_ms"] == 2.0
    assert route["peak_rtt_ms"] == 7.0


def test_exception_and_http_418_are_distinct_failures_per_route():
    metrics = BinanceRestMetrics()
    metrics.record_exception(
        method="POST",
        endpoint="/fapi/v1/order",
        exception_type="Timeout",
        rtt_ns=11_000_000,
        completed_monotonic_ns=100,
    )
    metrics.record_response(
        method="DELETE",
        endpoint="/fapi/v1/order",
        status_code=418,
        headers={},
        rtt_ns=4_000_000,
        completed_monotonic_ns=200,
    )

    snapshot = metrics.snapshot()

    assert snapshot["attempt_count"] == 2
    assert snapshot["failure_count"] == 2
    assert snapshot["exception_count"] == 1
    assert snapshot["http_418_count"] == 1
    assert snapshot["latest_status_code"] == 418
    assert snapshot["latest_exception_type"] == ""
    assert snapshot["routes"]["POST /fapi/v1/order"]["exception_count"] == 1
    assert snapshot["routes"]["POST /fapi/v1/order"]["latest_status_code"] is None
    assert snapshot["routes"]["DELETE /fapi/v1/order"]["http_418_count"] == 1


def test_latest_values_follow_monotonic_completion_not_observer_lock_order():
    metrics = BinanceRestMetrics()
    metrics.record_response(
        method="GET",
        endpoint="/newer",
        status_code=200,
        headers={"X-MBX-USED-WEIGHT-1M": "4"},
        rtt_ns=2_000_000,
        completed_monotonic_ns=200,
    )
    metrics.record_response(
        method="GET",
        endpoint="/older",
        status_code=500,
        headers={"X-MBX-USED-WEIGHT-1M": "9"},
        rtt_ns=8_000_000,
        completed_monotonic_ns=100,
    )

    snapshot = metrics.snapshot()

    assert snapshot["latest_endpoint"] == "/newer"
    assert snapshot["latest_status_code"] == 200
    assert snapshot["latest_rtt_ms"] == 2.0
    assert snapshot["peak_rtt_ms"] == 8.0
    assert snapshot["rate_limits"]["used_weight"]["1M"]["latest"] == 4
    assert snapshot["rate_limits"]["used_weight"]["1M"]["peak"] == 9


def test_snapshot_is_independent_and_concurrent_updates_are_not_lost():
    metrics = BinanceRestMetrics()

    def record_batch(batch_id):
        for offset in range(100):
            completed = batch_id * 1_000 + offset
            metrics.record_response(
                method="GET",
                endpoint="/fapi/v1/time",
                status_code=200,
                headers={"X-MBX-USED-WEIGHT-1M": str(completed)},
                rtt_ns=completed * 1_000,
                completed_monotonic_ns=completed,
            )

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(record_batch, range(4)))

    first = metrics.snapshot()
    first["routes"]["GET /fapi/v1/time"]["attempt_count"] = -1
    first["rate_limits"]["used_weight"]["1M"]["latest"] = -1
    second = metrics.snapshot()

    assert second["attempt_count"] == 400
    assert second["success_count"] == 400
    assert second["routes"]["GET /fapi/v1/time"]["attempt_count"] == 400
    assert second["rate_limits"]["used_weight"]["1M"]["latest"] == 3_099
    assert second["rate_limits"]["used_weight"]["1M"]["peak"] == 3_099


def test_dashboard_rest_sections_expose_rtt_routes_failures_and_limits():
    latency, api_limits = _rest_telemetry_sections(
        {
            "attempt_count": 5,
            "success_count": 3,
            "failure_count": 2,
            "exception_count": 1,
            "http_429_count": 1,
            "http_418_count": 1,
            "latest_method": "delete",
            "latest_endpoint": "/fapi/v1/order",
            "latest_status_code": 418,
            "latest_exception_type": "",
            "latest_rtt_ms": 4.5,
            "peak_rtt_ms": 11.0,
            "latest_completed_monotonic_ns": 900,
            "rate_limits": {
                "used_weight": {
                    "1M": {
                        "latest": 8,
                        "peak": 21,
                        "latest_completed_monotonic_ns": 900,
                    }
                },
                "order_count": {
                    "10S": {
                        "latest": 2,
                        "peak": 4,
                        "latest_completed_monotonic_ns": 900,
                    }
                },
            },
            "routes": {
                "POST /fapi/v1/order": {
                    "method": "POST",
                    "endpoint": "/fapi/v1/order",
                    "attempt_count": 2,
                    "success_count": 2,
                    "failure_count": 0,
                    "exception_count": 0,
                    "http_429_count": 0,
                    "http_418_count": 0,
                    "latest_status_code": 200,
                    "latest_exception_type": "",
                    "latest_rtt_ms": 3.0,
                    "peak_rtt_ms": 5.0,
                    "latest_completed_monotonic_ns": 700,
                }
            },
        }
    )

    assert latency["available"] is True
    assert latency["instrumented"] is True
    assert latency["clock"] == "monotonic"
    assert latency["latest_rtt_ms"] == 4.5
    assert latency["peak_rtt_ms"] == 11.0
    assert latency["routes"]["POST /fapi/v1/order"]["latest_rtt_ms"] == 3.0
    assert api_limits["available"] is True
    assert api_limits["http_429_count"] == 1
    assert api_limits["http_418_count"] == 1
    assert api_limits["used_weight"]["1M"]["latest"] == 8
    assert api_limits["used_weight"]["1M"]["peak"] == 21
    assert api_limits["order_count"]["10S"]["latest"] == 2
    assert api_limits["order_count"]["10S"]["peak"] == 4


def test_dashboard_rest_sections_distinguish_waiting_from_uninstrumented():
    waiting_latency, waiting_limits = _rest_telemetry_sections(
        {
            "attempt_count": 0,
            "success_count": 0,
            "failure_count": 0,
            "exception_count": 0,
            "http_429_count": 0,
            "http_418_count": 0,
            "latest_rtt_ms": None,
            "peak_rtt_ms": None,
            "rate_limits": {"used_weight": {}, "order_count": {}},
            "routes": {},
        }
    )
    missing_latency, missing_limits = _rest_telemetry_sections(None)

    assert waiting_latency["instrumented"] is True
    assert waiting_latency["reason"] == "awaiting_rest_attempt"
    assert waiting_limits["instrumented"] is True
    assert waiting_limits["reason"] == "awaiting_rate_limit_headers"
    assert missing_latency == {
        "available": False,
        "instrumented": False,
        "reason": "rest_metrics_unavailable",
    }
    assert missing_limits == missing_latency
