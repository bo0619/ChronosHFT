import re
import threading


_RATE_LIMIT_HEADER = re.compile(
    r"^x-mbx-(used-weight|order-count)-([1-9][0-9]*[smhd])$",
    re.IGNORECASE,
)
_RATE_LIMIT_KINDS = {
    "used-weight": "used_weight",
    "order-count": "order_count",
}


def parse_binance_rate_limit_headers(headers) -> dict:
    """Return recognized Binance request-weight and order-count headers."""
    parsed = {
        "used_weight": {},
        "order_count": {},
    }
    if headers is None:
        return parsed

    try:
        items = headers.items()
    except (AttributeError, TypeError):
        return parsed

    try:
        for raw_name, raw_value in items:
            match = _RATE_LIMIT_HEADER.fullmatch(str(raw_name).strip())
            if match is None:
                continue

            value_text = str(raw_value).strip()
            if not value_text.isdecimal():
                continue
            try:
                value = int(value_text)
            except (TypeError, ValueError):
                continue

            kind = _RATE_LIMIT_KINDS[match.group(1).lower()]
            interval = match.group(2).upper()
            parsed[kind][interval] = value
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return parsed
    return parsed


def _new_counters() -> dict:
    return {
        "attempt_count": 0,
        "success_count": 0,
        "failure_count": 0,
        "exception_count": 0,
        "http_429_count": 0,
        "http_418_count": 0,
        "latest_status_code": None,
        "latest_exception_type": "",
        "latest_rtt_ms": None,
        "peak_rtt_ms": None,
        "latest_completed_monotonic_ns": None,
    }


class BinanceRestMetrics:
    """Thread-safe, process-local telemetry for Binance REST wire attempts."""

    def __init__(self):
        self._lock = threading.Lock()
        self._totals = _new_counters()
        self._rate_limits = {
            "used_weight": {},
            "order_count": {},
        }
        self._routes = {}
        self._latest_method = ""
        self._latest_endpoint = ""

    def record_response(
        self,
        *,
        method,
        endpoint,
        status_code,
        headers,
        rtt_ns,
        completed_monotonic_ns,
    ):
        method = str(method or "").upper()
        endpoint = str(endpoint or "")
        status_code = int(status_code)
        rtt_ms = self._normalize_rtt_ms(rtt_ns)
        completed_monotonic_ns = int(completed_monotonic_ns)
        rate_limits = parse_binance_rate_limit_headers(headers)
        success = 200 <= status_code < 300

        with self._lock:
            self._record_attempt_locked(
                self._totals,
                status_code=status_code,
                exception_type="",
                rtt_ms=rtt_ms,
                completed_monotonic_ns=completed_monotonic_ns,
                success=success,
            )
            if self._is_latest(self._totals, completed_monotonic_ns):
                self._latest_method = method
                self._latest_endpoint = endpoint

            route = self._route_locked(method, endpoint)
            self._record_attempt_locked(
                route,
                status_code=status_code,
                exception_type="",
                rtt_ms=rtt_ms,
                completed_monotonic_ns=completed_monotonic_ns,
                success=success,
            )
            self._record_rate_limits_locked(
                rate_limits,
                completed_monotonic_ns=completed_monotonic_ns,
            )

    def record_exception(
        self,
        *,
        method,
        endpoint,
        exception_type,
        rtt_ns,
        completed_monotonic_ns,
    ):
        method = str(method or "").upper()
        endpoint = str(endpoint or "")
        exception_type = str(exception_type or "Exception")
        rtt_ms = self._normalize_rtt_ms(rtt_ns)
        completed_monotonic_ns = int(completed_monotonic_ns)

        with self._lock:
            self._record_attempt_locked(
                self._totals,
                status_code=None,
                exception_type=exception_type,
                rtt_ms=rtt_ms,
                completed_monotonic_ns=completed_monotonic_ns,
                success=False,
            )
            if self._is_latest(self._totals, completed_monotonic_ns):
                self._latest_method = method
                self._latest_endpoint = endpoint

            route = self._route_locked(method, endpoint)
            self._record_attempt_locked(
                route,
                status_code=None,
                exception_type=exception_type,
                rtt_ms=rtt_ms,
                completed_monotonic_ns=completed_monotonic_ns,
                success=False,
            )

    def snapshot(self) -> dict:
        with self._lock:
            snapshot = dict(self._totals)
            snapshot["latest_method"] = self._latest_method
            snapshot["latest_endpoint"] = self._latest_endpoint
            snapshot["rate_limits"] = {
                kind: {
                    interval: dict(values)
                    for interval, values in intervals.items()
                }
                for kind, intervals in self._rate_limits.items()
            }
            snapshot["routes"] = {
                route_key: dict(values)
                for route_key, values in self._routes.items()
            }
            return snapshot

    @staticmethod
    def _normalize_rtt_ms(rtt_ns) -> float:
        return max(0, int(rtt_ns)) / 1_000_000.0

    @staticmethod
    def _is_latest(counters: dict, completed_monotonic_ns: int) -> bool:
        latest = counters["latest_completed_monotonic_ns"]
        return latest is None or completed_monotonic_ns >= latest

    def _route_locked(self, method: str, endpoint: str) -> dict:
        route_key = f"{method} {endpoint}"
        route = self._routes.get(route_key)
        if route is None:
            route = _new_counters()
            route["method"] = method
            route["endpoint"] = endpoint
            self._routes[route_key] = route
        return route

    def _record_attempt_locked(
        self,
        counters: dict,
        *,
        status_code,
        exception_type: str,
        rtt_ms: float,
        completed_monotonic_ns: int,
        success: bool,
    ):
        counters["attempt_count"] += 1
        if success:
            counters["success_count"] += 1
        else:
            counters["failure_count"] += 1
        if exception_type:
            counters["exception_count"] += 1
        if status_code == 429:
            counters["http_429_count"] += 1
        if status_code == 418:
            counters["http_418_count"] += 1

        peak_rtt_ms = counters["peak_rtt_ms"]
        if peak_rtt_ms is None or rtt_ms > peak_rtt_ms:
            counters["peak_rtt_ms"] = rtt_ms
        if self._is_latest(counters, completed_monotonic_ns):
            counters["latest_status_code"] = status_code
            counters["latest_exception_type"] = exception_type
            counters["latest_rtt_ms"] = rtt_ms
            counters["latest_completed_monotonic_ns"] = completed_monotonic_ns

    def _record_rate_limits_locked(
        self,
        rate_limits: dict,
        *,
        completed_monotonic_ns: int,
    ):
        for kind, intervals in rate_limits.items():
            stored_intervals = self._rate_limits[kind]
            for interval, value in intervals.items():
                stored = stored_intervals.get(interval)
                if stored is None:
                    stored_intervals[interval] = {
                        "latest": value,
                        "peak": value,
                        "latest_completed_monotonic_ns": completed_monotonic_ns,
                    }
                    continue

                stored["peak"] = max(stored["peak"], value)
                if completed_monotonic_ns >= stored["latest_completed_monotonic_ns"]:
                    stored["latest"] = value
                    stored["latest_completed_monotonic_ns"] = completed_monotonic_ns
