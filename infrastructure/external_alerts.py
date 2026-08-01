from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4


ALERT_SCHEMA = "chronoshft.external_alert.v1"
ALERT_FAILURE_SCHEMA = "chronoshft.external_alert_failure.v1"
LEVEL_RANK = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}
_URL_PATTERN = re.compile(r"(?i)\bhttps?://[^\s<>'\"]+")
_SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b(signature|listenkey|api[_-]?key|api[_-]?secret|"
        r"token|password)=([^&\s]+)"
    ),
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)([^\s]+)"),
)
_SAFE_LABEL_PATTERN = re.compile(r"[^A-Za-z0-9_.:-]+")


def redact_alert_text(value: Any, limit: int = 1024) -> str:
    """Return bounded operational text without credentials or URLs."""
    rendered = str(value or "")
    rendered = _URL_PATTERN.sub("<redacted:url>", rendered)
    for pattern in _SECRET_PATTERNS:
        rendered = pattern.sub(
            lambda match: f"{match.group(1)}=<redacted>",
            rendered,
        )
    if len(rendered) > limit:
        rendered = rendered[: max(0, limit - 3)] + "..."
    return rendered


def validate_https_webhook_url(value: Any) -> bool:
    """Validate a secret webhook endpoint without returning or rendering it."""
    candidate = str(value or "").strip()
    if not candidate:
        return False
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return False
    return bool(
        parsed.scheme.lower() == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
    )


class AlertTransport(Protocol):
    def send(
        self,
        payload: Mapping[str, Any],
        *,
        connect_timeout_sec: float,
        read_timeout_sec: float,
    ) -> None: ...

    def close(self) -> None: ...


class AlertDeliveryError(RuntimeError):
    """Sanitized transport failure safe for state and durable audit records."""

    def __init__(
        self,
        kind: str,
        *,
        retryable: bool,
        status_code: int | None = None,
    ):
        super().__init__(str(kind or "transport_failure"))
        self.kind = _safe_label(kind, "transport_failure")
        self.retryable = bool(retryable)
        self.status_code = (
            int(status_code)
            if isinstance(status_code, int) and 100 <= status_code <= 599
            else None
        )


class HttpsWebhookTransport:
    """HTTPS JSON transport which never exposes its credential-bearing URL."""

    def __init__(self, webhook_url: str, session=None):
        if not validate_https_webhook_url(webhook_url):
            raise ValueError("external alert webhook endpoint must be valid HTTPS")
        if session is None:
            import requests

            session = requests.Session()
        self._webhook_url = str(webhook_url)
        self._session = session
        if hasattr(self._session, "trust_env"):
            self._session.trust_env = False

    def __repr__(self) -> str:
        return "HttpsWebhookTransport(endpoint=<redacted>)"

    def send(
        self,
        payload: Mapping[str, Any],
        *,
        connect_timeout_sec: float,
        read_timeout_sec: float,
    ) -> None:
        try:
            response = self._session.post(
                self._webhook_url,
                json=dict(payload),
                timeout=(connect_timeout_sec, read_timeout_sec),
                allow_redirects=False,
                stream=True,
            )
        except Exception as exc:
            try:
                import requests
            except ImportError:
                requests = None
            if requests is not None and isinstance(exc, requests.Timeout):
                raise AlertDeliveryError(
                    "timeout",
                    retryable=True,
                ) from None
            if requests is not None and isinstance(
                exc,
                requests.RequestException,
            ):
                raise AlertDeliveryError(
                    "network",
                    retryable=True,
                ) from None
            raise AlertDeliveryError(
                "transport_exception",
                retryable=False,
            ) from None

        try:
            status_code = int(getattr(response, "status_code", 0) or 0)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        if 200 <= status_code < 300:
            return
        if status_code == 429:
            kind = "http_429"
            retryable = True
        elif 500 <= status_code < 600:
            kind = "http_5xx"
            retryable = True
        else:
            kind = "http_non_success"
            retryable = False
        raise AlertDeliveryError(
            kind,
            retryable=retryable,
            status_code=status_code,
        )

    def close(self) -> None:
        close = getattr(self._session, "close", None)
        if callable(close):
            close()


@dataclass(slots=True)
class _DeliveryReceipt:
    completed: threading.Event
    delivered: bool | None = None


@dataclass(slots=True)
class _PendingAlert:
    payload: dict[str, Any]
    receipt: _DeliveryReceipt | None = None


class _FailureSpoolSpaceError(RuntimeError):
    pass


def _safe_label(value: Any, fallback: str) -> str:
    rendered = _SAFE_LABEL_PATTERN.sub(
        "_",
        str(value or "").strip(),
    ).strip("_.:-")
    return (rendered or fallback)[:96]


def _finite_timestamp(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if math.isfinite(parsed) and parsed > 0.0 else fallback


def _utc_iso(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


class ExternalAlertService:
    """Bounded asynchronous external alert delivery with durable failures."""

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        transport: AlertTransport | None = None,
        environ: Mapping[str, str] | None = None,
        wall_time: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        event_id_factory: Callable[[], str] | None = None,
    ):
        alert_config = config.get("alert", {})
        if not isinstance(alert_config, Mapping):
            raise ValueError("alert configuration must be an object")
        live_launch = config.get("live_launch", {})
        if not isinstance(live_launch, Mapping):
            live_launch = {}

        self.enabled = bool(alert_config.get("active", False))
        self.deployment_id = _safe_label(
            live_launch.get("deployment_id"),
            "unbound",
        )
        self.stage = _safe_label(live_launch.get("stage"), "unknown")
        self.queue_capacity = max(
            1,
            int(alert_config.get("queue_capacity", 128) or 128),
        )
        self.connect_timeout_sec = max(
            0.01,
            float(alert_config.get("connect_timeout_sec", 1.0) or 1.0),
        )
        self.read_timeout_sec = max(
            0.01,
            float(alert_config.get("read_timeout_sec", 2.0) or 2.0),
        )
        self.max_attempts = max(
            1,
            int(alert_config.get("max_attempts", 3) or 3),
        )
        self.retry_backoff_sec = max(
            0.0,
            float(alert_config.get("retry_backoff_sec", 0.5) or 0.0),
        )
        self.startup_probe_timeout_sec = max(
            0.01,
            float(
                alert_config.get("startup_probe_timeout_sec", 12.0)
                or 12.0
            ),
        )
        self.recovery_probe_interval_sec = max(
            0.01,
            float(
                alert_config.get("recovery_probe_interval_sec", 30.0)
                or 30.0
            ),
        )
        self.shutdown_flush_timeout_sec = max(
            0.0,
            float(
                alert_config.get("shutdown_flush_timeout_sec", 3.0)
                or 0.0
            ),
        )
        self.failure_spool_fsync = bool(
            alert_config.get("failure_spool_fsync", True)
        )
        failure_spool_min_free_bytes = alert_config.get(
            "failure_spool_min_free_bytes",
            0,
        )
        if (
            isinstance(failure_spool_min_free_bytes, bool)
            or not isinstance(failure_spool_min_free_bytes, int)
            or failure_spool_min_free_bytes < 0
        ):
            raise ValueError(
                "alert.failure_spool_min_free_bytes must be a non-negative "
                "integer"
            )
        self.failure_spool_min_free_bytes = failure_spool_min_free_bytes
        spool_path = str(
            alert_config.get("failure_spool_path", "") or ""
        ).strip()
        if not spool_path:
            raise ValueError("external alert failure spool path is required")
        self._failure_spool_path = Path(spool_path)

        if transport is None:
            env_name = str(
                alert_config.get("webhook_url_env", "") or ""
            ).strip()
            environment = os.environ if environ is None else environ
            webhook_url = str(environment.get(env_name, "") or "").strip()
            if not validate_https_webhook_url(webhook_url):
                raise ValueError(
                    "external alert webhook environment value is unavailable "
                    "or invalid"
                )
            transport = HttpsWebhookTransport(webhook_url)
        self._transport = transport
        self._wall_time = wall_time
        self._monotonic = monotonic
        self._event_id_factory = event_id_factory or (
            lambda: uuid4().hex
        )

        self._condition = threading.Condition(threading.Lock())
        self._spool_lock = threading.Lock()
        self._high_priority: deque[_PendingAlert] = deque()
        self._normal_priority: deque[_PendingAlert] = deque()
        self._worker: threading.Thread | None = None
        self._started = False
        self._accepting = False
        self._stop_requested = False
        self._abort_requested = False
        self._worker_alive = False
        self._inflight = 0
        self._sequence = 0

        self._startup_probe_succeeded = False
        self._healthy = False
        self._health_reason = "not_started"
        self._next_recovery_probe_at = 0.0
        self._successful_deliveries = 0
        self._failed_deliveries = 0
        self._consecutive_failures = 0
        self._dropped_alerts = 0
        self._overflow_pending = 0
        self._overflow_last_level = ""
        self._overflow_last_source = ""
        self._overflow_last_code = ""
        self._failure_records = 0
        self._failure_spool_errors = 0
        self._failure_spool_free_bytes: int | None = None
        self._failure_spool_last_space_check_at = 0.0
        self._failure_spool_space_rejections = 0
        self._failure_spool_space_check_failures = 0
        self._last_success_at = 0.0
        self._last_failure_at = 0.0
        self._last_failure_kind = ""

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        **kwargs,
    ) -> ExternalAlertService | None:
        alert_config = config.get("alert", {})
        if not isinstance(alert_config, Mapping) or not bool(
            alert_config.get("active", False)
        ):
            return None
        return cls(config, **kwargs)

    def start(self) -> bool:
        if not self.enabled:
            return False
        with self._condition:
            if self._started:
                return bool(self._worker_alive)
        self._prepare_failure_spool()
        with self._condition:
            self._started = True
            self._accepting = True
            self._stop_requested = False
            self._abort_requested = False
            self._health_reason = "startup_probe_pending"
            worker = threading.Thread(
                target=self._worker_loop,
                name="ExternalAlertWorker",
                daemon=True,
            )
            self._worker = worker
            worker.start()
        return True

    def probe_startup(self, timeout_sec: float | None = None) -> bool:
        receipt = _DeliveryReceipt(threading.Event())
        accepted = self._enqueue(
            level="WARNING",
            source="external_alerts",
            code="startup_probe",
            message="ChronosHFT Live external alert startup probe",
            receipt=receipt,
            force=True,
        )
        if not accepted:
            return False
        timeout = (
            self.startup_probe_timeout_sec
            if timeout_sec is None
            else max(0.0, float(timeout_sec))
        )
        if not receipt.completed.wait(timeout):
            with self._condition:
                self._healthy = False
                self._health_reason = "startup_probe_timeout"
                self._last_failure_kind = "startup_probe_timeout"
                self._last_failure_at = self._wall_time()
            return False
        return receipt.delivered is True

    def enqueue_log(self, level: str, message: Any) -> bool:
        normalized_level = str(level or "INFO").upper()
        if LEVEL_RANK.get(normalized_level, 0) < LEVEL_RANK["WARNING"]:
            return False
        return self._enqueue(
            level=normalized_level,
            source="logger",
            code="log_record",
            message=message,
        )

    def enqueue_event(self, data: Any) -> bool:
        if isinstance(data, Mapping):
            level = data.get("level", "WARNING")
            message = data.get("msg", data.get("message", ""))
            timestamp = data.get("timestamp")
            source = data.get("source", "event_alert")
            code = data.get("code", "event_alert")
        else:
            level = getattr(data, "level", "WARNING")
            message = getattr(data, "msg", getattr(data, "message", data))
            timestamp = getattr(data, "timestamp", None)
            source = getattr(data, "source", "event_alert")
            code = getattr(data, "code", "event_alert")
        return self._enqueue(
            level=str(level or "WARNING").upper(),
            source=source,
            code=code,
            message=message,
            timestamp=timestamp,
        )

    def enqueue(
        self,
        *,
        level: str,
        source: str,
        code: str,
        message: Any,
        timestamp: float | None = None,
    ) -> bool:
        return self._enqueue(
            level=level,
            source=source,
            code=code,
            message=message,
            timestamp=timestamp,
        )

    def wait_until_idle(self, timeout_sec: float) -> bool:
        deadline = self._monotonic() + max(0.0, float(timeout_sec))
        with self._condition:
            while (
                self._high_priority
                or self._normal_priority
                or self._inflight
                or self._overflow_pending
            ):
                remaining = deadline - self._monotonic()
                if remaining <= 0.0:
                    return False
                self._condition.wait(timeout=remaining)
            return True

    def stop(self, timeout_sec: float | None = None) -> bool:
        timeout = (
            self.shutdown_flush_timeout_sec
            if timeout_sec is None
            else max(0.0, float(timeout_sec))
        )
        with self._condition:
            if not self._started:
                return True
            self._accepting = False
            self._stop_requested = True
            self._condition.notify_all()
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=timeout)
        stopped = worker is None or not worker.is_alive()
        if not stopped:
            with self._condition:
                self._abort_requested = True
                unsent_count = (
                    len(self._high_priority)
                    + len(self._normal_priority)
                    + self._inflight
                )
                self._healthy = False
                self._health_reason = "shutdown_flush_timeout"
                self._condition.notify_all()
            self._append_failure_record(
                {
                    "schema": ALERT_FAILURE_SCHEMA,
                    "record_type": "shutdown_unsent_summary",
                    "recorded_at_utc": _utc_iso(self._wall_time()),
                    "deployment_id": self.deployment_id,
                    "stage": self.stage,
                    "failure_kind": "shutdown_flush_timeout",
                    "unsent_count": int(unsent_count),
                }
            )
            return False
        self._close_transport()
        return True

    def get_health_snapshot(self) -> dict[str, Any]:
        with self._condition:
            worker_alive = bool(
                self._worker_alive
                and self._worker is not None
                and self._worker.is_alive()
            )
            healthy = bool(
                self.enabled
                and self._started
                and worker_alive
                and self._startup_probe_succeeded
                and self._healthy
                and self._failure_spool_errors == 0
            )
            return {
                "available": True,
                "enabled": bool(self.enabled),
                "healthy": healthy,
                "reason": (
                    self._health_reason
                    if not healthy
                    else "delivery_healthy"
                ),
                "worker_alive": worker_alive,
                "startup_probe_succeeded": bool(
                    self._startup_probe_succeeded
                ),
                "accepting": bool(self._accepting),
                "queue_depth": (
                    len(self._high_priority)
                    + len(self._normal_priority)
                ),
                "queue_capacity": int(self.queue_capacity),
                "inflight": int(self._inflight),
                "successful_deliveries": int(
                    self._successful_deliveries
                ),
                "failed_deliveries": int(self._failed_deliveries),
                "consecutive_failures": int(
                    self._consecutive_failures
                ),
                "dropped_alerts": int(self._dropped_alerts),
                "failure_records": int(self._failure_records),
                "failure_spool_errors": int(
                    self._failure_spool_errors
                ),
                "failure_spool_min_free_bytes": int(
                    self.failure_spool_min_free_bytes
                ),
                "failure_spool_free_bytes": (
                    self._failure_spool_free_bytes
                ),
                "failure_spool_last_space_check_at": float(
                    self._failure_spool_last_space_check_at
                ),
                "failure_spool_space_rejections": int(
                    self._failure_spool_space_rejections
                ),
                "failure_spool_space_check_failures": int(
                    self._failure_spool_space_check_failures
                ),
                "last_success_at": float(self._last_success_at),
                "last_failure_at": float(self._last_failure_at),
                "last_failure_kind": str(
                    self._last_failure_kind
                ),
            }

    snapshot = get_health_snapshot

    def _enqueue(
        self,
        *,
        level: str,
        source: str,
        code: str,
        message: Any,
        timestamp: float | None = None,
        receipt: _DeliveryReceipt | None = None,
        force: bool = False,
    ) -> bool:
        normalized_level = str(level or "WARNING").upper()
        if normalized_level not in LEVEL_RANK:
            normalized_level = "WARNING"
        if not force and LEVEL_RANK[normalized_level] < LEVEL_RANK["WARNING"]:
            return False
        now = self._wall_time()
        occurred_at = _finite_timestamp(timestamp, now)

        with self._condition:
            if not self._accepting:
                if receipt is not None:
                    receipt.delivered = False
                    receipt.completed.set()
                return False
            self._sequence += 1
            payload = {
                "schema": ALERT_SCHEMA,
                "event_id": _safe_label(
                    self._event_id_factory(),
                    f"event-{self._sequence}",
                ),
                "sequence": int(self._sequence),
                "deployment_id": self.deployment_id,
                "stage": self.stage,
                "occurred_at_utc": _utc_iso(occurred_at),
                "level": normalized_level,
                "source": _safe_label(source, "unknown"),
                "code": _safe_label(code, "alert"),
                "message": redact_alert_text(message, 1024),
            }
            pending = _PendingAlert(payload=payload, receipt=receipt)
            total_depth = (
                len(self._high_priority) + len(self._normal_priority)
            )
            high_priority = (
                LEVEL_RANK[normalized_level] >= LEVEL_RANK["ERROR"]
                or payload["code"] == "startup_probe"
            )
            if total_depth >= self.queue_capacity:
                if high_priority and self._normal_priority:
                    evicted = self._normal_priority.popleft()
                    self._record_drop_locked(evicted)
                else:
                    self._record_drop_locked(pending)
                    if receipt is not None:
                        receipt.delivered = False
                        receipt.completed.set()
                    self._condition.notify_all()
                    return False
            target = (
                self._high_priority
                if high_priority
                else self._normal_priority
            )
            target.append(pending)
            self._condition.notify()
            return True

    def _record_drop_locked(self, pending: _PendingAlert) -> None:
        payload = pending.payload
        self._dropped_alerts += 1
        self._overflow_pending += 1
        self._overflow_last_level = str(payload.get("level", ""))
        self._overflow_last_source = str(payload.get("source", ""))
        self._overflow_last_code = str(payload.get("code", ""))
        self._healthy = False
        self._health_reason = "queue_overflow"
        self._last_failure_kind = "queue_overflow"
        self._last_failure_at = self._wall_time()

    def _worker_loop(self) -> None:
        with self._condition:
            self._worker_alive = True
            self._condition.notify_all()
        try:
            while True:
                self._flush_overflow_summary()
                pending = self._next_pending()
                if pending is None:
                    break
                with self._condition:
                    self._inflight += 1
                try:
                    delivered = self._deliver(pending)
                finally:
                    with self._condition:
                        self._inflight = max(0, self._inflight - 1)
                        self._condition.notify_all()
                if pending.receipt is not None:
                    pending.receipt.delivered = delivered
                    pending.receipt.completed.set()
        finally:
            with self._condition:
                self._worker_alive = False
                self._accepting = False
                if not self._stop_requested:
                    self._healthy = False
                    self._health_reason = "worker_stopped"
                self._condition.notify_all()

    def _next_pending(self) -> _PendingAlert | None:
        with self._condition:
            while True:
                if self._abort_requested:
                    return None
                if self._high_priority:
                    return self._high_priority.popleft()
                if self._normal_priority:
                    return self._normal_priority.popleft()
                if self._stop_requested:
                    return None

                now = self._monotonic()
                if (
                    self._startup_probe_succeeded
                    and not self._healthy
                    and self._next_recovery_probe_at > 0.0
                    and now >= self._next_recovery_probe_at
                ):
                    self._sequence += 1
                    payload = {
                        "schema": ALERT_SCHEMA,
                        "event_id": _safe_label(
                            self._event_id_factory(),
                            f"event-{self._sequence}",
                        ),
                        "sequence": int(self._sequence),
                        "deployment_id": self.deployment_id,
                        "stage": self.stage,
                        "occurred_at_utc": _utc_iso(self._wall_time()),
                        "level": "WARNING",
                        "source": "external_alerts",
                        "code": "recovery_probe",
                        "message": (
                            "ChronosHFT external alert transport recovery probe"
                        ),
                    }
                    self._next_recovery_probe_at = (
                        now + self.recovery_probe_interval_sec
                    )
                    return _PendingAlert(payload=payload)

                timeout = 0.5
                if (
                    self._startup_probe_succeeded
                    and not self._healthy
                    and self._next_recovery_probe_at > now
                ):
                    timeout = min(
                        timeout,
                        self._next_recovery_probe_at - now,
                    )
                self._condition.wait(timeout=max(0.01, timeout))

    def _deliver(self, pending: _PendingAlert) -> bool:
        failure_kind = "transport_failure"
        status_code = None
        attempts = 0
        for attempt in range(1, self.max_attempts + 1):
            attempts = attempt
            try:
                self._transport.send(
                    pending.payload,
                    connect_timeout_sec=self.connect_timeout_sec,
                    read_timeout_sec=self.read_timeout_sec,
                )
            except AlertDeliveryError as exc:
                failure_kind = exc.kind
                status_code = exc.status_code
                if not exc.retryable or attempt >= self.max_attempts:
                    break
            except Exception:
                failure_kind = "transport_exception"
                break
            else:
                self._mark_delivery_success(pending)
                return True

            backoff = self.retry_backoff_sec * (2 ** (attempt - 1))
            if backoff > 0.0 and self._wait_for_abort(backoff):
                failure_kind = "delivery_aborted"
                break

        self._mark_delivery_failure(
            pending,
            kind=failure_kind,
            attempts=attempts,
            status_code=status_code,
        )
        return False

    def _wait_for_abort(self, timeout_sec: float) -> bool:
        deadline = self._monotonic() + timeout_sec
        with self._condition:
            while not self._abort_requested:
                remaining = deadline - self._monotonic()
                if remaining <= 0.0:
                    return False
                self._condition.wait(timeout=remaining)
            return True

    def _mark_delivery_success(self, pending: _PendingAlert) -> None:
        now = self._wall_time()
        with self._condition:
            self._successful_deliveries += 1
            self._consecutive_failures = 0
            self._last_success_at = now
            code = str(pending.payload.get("code", "") or "")
            if code == "startup_probe":
                self._startup_probe_succeeded = True
            if (
                self._startup_probe_succeeded
                and self._overflow_pending == 0
                and self._failure_spool_errors == 0
            ):
                self._healthy = True
                self._health_reason = "delivery_healthy"
                self._last_failure_kind = ""
                self._next_recovery_probe_at = 0.0
            self._condition.notify_all()

    def _mark_delivery_failure(
        self,
        pending: _PendingAlert,
        *,
        kind: str,
        attempts: int,
        status_code: int | None,
    ) -> None:
        now = self._wall_time()
        safe_kind = _safe_label(kind, "transport_failure")
        with self._condition:
            self._failed_deliveries += 1
            self._consecutive_failures += 1
            self._healthy = False
            self._health_reason = safe_kind
            self._last_failure_at = now
            self._last_failure_kind = safe_kind
            if self._startup_probe_succeeded:
                self._next_recovery_probe_at = (
                    self._monotonic()
                    + self.recovery_probe_interval_sec
                )
        payload = pending.payload
        record = {
            "schema": ALERT_FAILURE_SCHEMA,
            "record_type": "delivery_failure",
            "recorded_at_utc": _utc_iso(now),
            "event_id": str(payload.get("event_id", "")),
            "occurred_at_utc": str(payload.get("occurred_at_utc", "")),
            "deployment_id": self.deployment_id,
            "stage": self.stage,
            "level": str(payload.get("level", "")),
            "source": str(payload.get("source", "")),
            "code": str(payload.get("code", "")),
            "message": redact_alert_text(payload.get("message", ""), 1024),
            "failure_kind": safe_kind,
            "attempts": max(1, int(attempts)),
        }
        if status_code is not None:
            record["status_code"] = int(status_code)
        self._append_failure_record(record)

    def _flush_overflow_summary(self) -> None:
        with self._condition:
            if self._overflow_pending <= 0:
                return
            dropped_count = int(self._overflow_pending)
            level = self._overflow_last_level
            source = self._overflow_last_source
            code = self._overflow_last_code
        persisted = self._append_failure_record(
            {
                "schema": ALERT_FAILURE_SCHEMA,
                "record_type": "queue_overflow_summary",
                "recorded_at_utc": _utc_iso(self._wall_time()),
                "deployment_id": self.deployment_id,
                "stage": self.stage,
                "failure_kind": "queue_overflow",
                "dropped_count": dropped_count,
                "last_level": level,
                "last_source": source,
                "last_code": code,
            }
        )
        if persisted:
            with self._condition:
                self._overflow_pending = max(
                    0,
                    self._overflow_pending - dropped_count,
                )
                self._condition.notify_all()

    def _prepare_failure_spool(self) -> None:
        try:
            self._failure_spool_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            space_error = self._failure_spool_space_error(0)
            if space_error:
                raise _FailureSpoolSpaceError(space_error)
            with self._failure_spool_path.open(
                "a",
                encoding="utf-8",
            ) as handle:
                handle.flush()
                if self.failure_spool_fsync:
                    os.fsync(handle.fileno())
        except (OSError, ValueError, _FailureSpoolSpaceError):
            raise RuntimeError(
                "external alert failure spool is unavailable"
            ) from None

    def _failure_spool_space_error(self, required_bytes: int) -> str:
        if self.failure_spool_min_free_bytes <= 0:
            return ""
        try:
            free_bytes = int(
                shutil.disk_usage(self._failure_spool_path.parent).free
            )
        except (OSError, ValueError):
            with self._condition:
                self._failure_spool_last_space_check_at = self._wall_time()
                self._failure_spool_space_check_failures += 1
            return "failure_spool_space_check_failed"

        free_after_write = free_bytes - max(0, int(required_bytes))
        with self._condition:
            self._failure_spool_free_bytes = free_bytes
            self._failure_spool_last_space_check_at = self._wall_time()
            if free_after_write < self.failure_spool_min_free_bytes:
                self._failure_spool_space_rejections += 1
        if free_after_write < self.failure_spool_min_free_bytes:
            return "failure_spool_disk_reserve_exhausted"
        return ""

    def _append_failure_record(
        self,
        record: Mapping[str, Any],
    ) -> bool:
        safe_record = {
            str(key): (
                redact_alert_text(value, 1024)
                if isinstance(value, str)
                else value
            )
            for key, value in record.items()
            if str(key).lower()
            not in {
                "url",
                "webhook_url",
                "response",
                "response_body",
                "exception",
                "error_text",
            }
        }
        encoded = json.dumps(
            safe_record,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        encoded_bytes = len(encoded.encode("utf-8")) + 1
        failure_reason = "failure_spool_unavailable"
        try:
            with self._spool_lock:
                space_error = self._failure_spool_space_error(encoded_bytes)
                if space_error:
                    raise _FailureSpoolSpaceError(space_error)
                with self._failure_spool_path.open(
                    "a",
                    encoding="utf-8",
                ) as handle:
                    handle.write(encoded)
                    handle.write("\n")
                    handle.flush()
                    if self.failure_spool_fsync:
                        os.fsync(handle.fileno())
        except _FailureSpoolSpaceError as exc:
            failure_reason = str(exc)
        except (OSError, TypeError, ValueError):
            pass
        else:
            with self._condition:
                self._failure_records += 1
                self._condition.notify_all()
            return True

        with self._condition:
            self._failure_spool_errors += 1
            self._healthy = False
            self._health_reason = failure_reason
            self._last_failure_kind = failure_reason
            self._last_failure_at = self._wall_time()
            self._condition.notify_all()
        return False

    def _close_transport(self) -> None:
        close = getattr(self._transport, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
