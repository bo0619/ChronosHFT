from __future__ import annotations

from collections.abc import Callable, Mapping
import math
import os
import socket
import time
from typing import Any


class SystemdWatchdog:
    """Low-overhead sd_notify watchdog pulses tied to the main loop."""

    _PING_PAYLOAD = b"WATCHDOG=1"
    _PING_FRACTION = 0.4

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        monotonic: Callable[[], float] = time.perf_counter,
        socket_factory: Callable[..., Any] = socket.socket,
        address_family: int | None = getattr(socket, "AF_UNIX", None),
        pid: int | None = None,
    ) -> None:
        environment = os.environ if environ is None else environ
        self._monotonic = monotonic
        self._socket_factory = socket_factory
        self._address_family = address_family
        self._pid = int(pid or os.getpid())
        self._address: str | bytes | None = None
        self._last_attempt_at: float | None = None
        self._last_success_at: float | None = None
        self._send_count = 0
        self._error_count = 0
        self._last_error = ""
        self.enabled = False
        self.reason = "not_configured"
        self.watchdog_period_sec: float | None = None
        self.ping_interval_sec: float | None = None

        raw_address = str(environment.get("NOTIFY_SOCKET", "") or "")
        if not raw_address:
            return
        if self._address_family is None:
            self.reason = "unix_datagram_unsupported"
            return
        if raw_address.startswith("@"):
            self._address = b"\0" + raw_address[1:].encode(
                "utf-8",
                errors="strict",
            )
        elif raw_address.startswith("/"):
            self._address = raw_address
        else:
            self.reason = "invalid_notify_socket"
            return

        raw_watchdog_usec = str(
            environment.get("WATCHDOG_USEC", "") or ""
        ).strip()
        try:
            watchdog_usec = int(raw_watchdog_usec)
        except (TypeError, ValueError, OverflowError):
            self.reason = "invalid_watchdog_usec"
            return
        if watchdog_usec <= 0:
            self.reason = "invalid_watchdog_usec"
            return

        raw_watchdog_pid = str(
            environment.get("WATCHDOG_PID", "") or ""
        ).strip()
        if raw_watchdog_pid:
            try:
                watchdog_pid = int(raw_watchdog_pid)
            except (TypeError, ValueError, OverflowError):
                self.reason = "invalid_watchdog_pid"
                return
            if watchdog_pid != self._pid:
                self.reason = "watchdog_pid_mismatch"
                return

        self.watchdog_period_sec = watchdog_usec / 1_000_000.0
        self.ping_interval_sec = max(
            0.1,
            self.watchdog_period_sec * self._PING_FRACTION,
        )
        self.enabled = True
        self.reason = "configured"

    def pulse(self, *, force: bool = False) -> bool:
        """Send one due keepalive without raising into the trading loop."""

        if not self.enabled or self._address is None:
            return False
        now = float(self._monotonic())
        if not math.isfinite(now):
            self._record_error("non_finite_monotonic")
            return False
        if (
            not force
            and self._last_attempt_at is not None
            and self.ping_interval_sec is not None
            and now - self._last_attempt_at < self.ping_interval_sec
        ):
            return False

        self._last_attempt_at = now
        notify_socket = None
        try:
            notify_socket = self._socket_factory(
                self._address_family,
                socket.SOCK_DGRAM,
            )
            sent = notify_socket.sendto(self._PING_PAYLOAD, self._address)
            if sent != len(self._PING_PAYLOAD):
                raise OSError("partial sd_notify datagram")
        except (OSError, TypeError, ValueError) as exc:
            self._record_error(type(exc).__name__)
            return False
        finally:
            if notify_socket is not None:
                try:
                    notify_socket.close()
                except OSError:
                    pass

        self._send_count += 1
        self._last_success_at = now
        self._last_error = ""
        return True

    def _record_error(self, reason: str) -> None:
        self._error_count += 1
        self._last_error = str(reason or "notify_failed")

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "reason": self.reason,
            "watchdog_period_sec": self.watchdog_period_sec,
            "ping_interval_sec": self.ping_interval_sec,
            "send_count": self._send_count,
            "error_count": self._error_count,
            "last_error": self._last_error,
            "last_attempt_at_monotonic": self._last_attempt_at,
            "last_success_at_monotonic": self._last_success_at,
        }
