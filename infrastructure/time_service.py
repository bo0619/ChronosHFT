import threading
import time

import requests

from .logger import logger


class TimeService:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(TimeService, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "offset"):
            return
        self.offset = 0
        self.active = False
        self.url = "https://fapi.binance.com/fapi/v1/time"
        self.listeners = []
        self.max_offset_ms = 250.0
        self.halt_offset_ms = 1000.0
        self.max_rtt_ms = 1500.0
        self.max_consecutive_failures = 3
        self.freeze_breach_threshold = 1
        self.halt_breach_threshold = 1
        self.recovery_success_threshold = 1
        self.sync_interval_sec = 600.0
        self.unhealthy_retry_sec = 15.0
        self.last_sync_time = 0.0
        self.last_rtt_ms = 0.0
        self.last_error = ""
        self.consecutive_failures = 0
        self.freeze_breach_count = 0
        self.halt_breach_count = 0
        self.recovery_success_count = 0
        self._health_state = "healthy"

    def configure(self, config=None):
        config = config or {}
        self.max_offset_ms = float(config.get("max_offset_ms", self.max_offset_ms) or self.max_offset_ms)
        self.halt_offset_ms = float(config.get("halt_offset_ms", self.halt_offset_ms) or self.halt_offset_ms)
        self.max_rtt_ms = float(config.get("max_rtt_ms", self.max_rtt_ms) or self.max_rtt_ms)
        self.max_consecutive_failures = max(
            1,
            int(config.get("max_consecutive_failures", self.max_consecutive_failures) or self.max_consecutive_failures),
        )
        self.freeze_breach_threshold = max(
            1,
            int(config.get("freeze_breach_threshold", self.freeze_breach_threshold) or self.freeze_breach_threshold),
        )
        self.halt_breach_threshold = max(
            1,
            int(config.get("halt_breach_threshold", self.halt_breach_threshold) or self.halt_breach_threshold),
        )
        self.recovery_success_threshold = max(
            1,
            int(config.get("recovery_success_threshold", self.recovery_success_threshold) or self.recovery_success_threshold),
        )
        self.sync_interval_sec = max(
            1.0,
            float(config.get("sync_interval_sec", self.sync_interval_sec) or self.sync_interval_sec),
        )
        self.unhealthy_retry_sec = max(
            1.0,
            float(config.get("unhealthy_retry_sec", self.unhealthy_retry_sec) or self.unhealthy_retry_sec),
        )

    def register_listener(self, listener):
        if listener not in self.listeners:
            self.listeners.append(listener)

    def clear_listeners(self):
        self.listeners.clear()

    def start(self, testnet=False):
        if testnet:
            self.url = "https://testnet.binancefuture.com/fapi/v1/time"
        else:
            self.url = "https://fapi.binance.com/fapi/v1/time"

        logger.info(f"TimeService connecting to: {self.url}")
        self._sync()

        self.active = True
        threading.Thread(target=self._auto_sync_loop, daemon=True).start()

    def stop(self):
        self.active = False

    def now(self):
        return int(time.time() * 1000 + self.offset)

    def _notify(self, severity: str, reason: str, **details):
        for listener in list(self.listeners):
            try:
                listener(severity, reason, details)
            except Exception as exc:
                logger.error(f"TimeService listener failed: {exc}")

    def _sync(self):
        try:
            t0 = time.time() * 1000
            response = requests.get(self.url, timeout=5)
            payload = response.json()
            server_time = payload["serverTime"]
            t1 = time.time() * 1000
            rtt = t1 - t0
            self.offset = ((server_time - t0) + (server_time - t1)) / 2
            self.last_sync_time = time.time()
            self.last_rtt_ms = rtt
            self.last_error = ""
            self.consecutive_failures = 0
            logger.info(f"Time Synced. Offset: {self.offset:.2f}ms")

            severity = ""
            reason = "time sync healthy"
            threshold_reached = False
            if abs(self.offset) >= self.halt_offset_ms:
                self.halt_breach_count += 1
                self.freeze_breach_count = max(self.freeze_breach_count, self.halt_breach_count)
                self.recovery_success_count = 0
                reason = (
                    f"clock offset {self.offset:.1f}ms exceeds halt threshold "
                    f"{self.halt_offset_ms:.1f}ms"
                )
                threshold_reached = self.halt_breach_count >= self.halt_breach_threshold
                severity = "halt" if threshold_reached else ""
            elif abs(self.offset) >= self.max_offset_ms:
                self.freeze_breach_count += 1
                self.halt_breach_count = 0
                self.recovery_success_count = 0
                reason = (
                    f"clock offset {self.offset:.1f}ms exceeds freeze threshold "
                    f"{self.max_offset_ms:.1f}ms"
                )
                threshold_reached = self.freeze_breach_count >= self.freeze_breach_threshold
                severity = "freeze" if threshold_reached else ""
            elif self.max_rtt_ms > 0 and rtt >= self.max_rtt_ms:
                self.freeze_breach_count += 1
                self.halt_breach_count = 0
                self.recovery_success_count = 0
                reason = f"time sync RTT {rtt:.1f}ms exceeds {self.max_rtt_ms:.1f}ms"
                threshold_reached = self.freeze_breach_count >= self.freeze_breach_threshold
                severity = "freeze" if threshold_reached else ""
            else:
                self.freeze_breach_count = 0
                self.halt_breach_count = 0
                self.recovery_success_count += 1

            previous_state = self._health_state
            if severity:
                self._health_state = severity
                self._notify(
                    severity,
                    reason,
                    offset_ms=self.offset,
                    rtt_ms=rtt,
                    consecutive_failures=self.consecutive_failures,
                )
            elif threshold_reached is False and reason != "time sync healthy":
                logger.warning(
                    f"Time Sync breach observed but below trigger threshold: {reason} "
                    f"(freeze={self.freeze_breach_count}/{self.freeze_breach_threshold} "
                    f"halt={self.halt_breach_count}/{self.halt_breach_threshold})"
                )
            elif previous_state != "healthy" and self.recovery_success_count >= self.recovery_success_threshold:
                self._health_state = "healthy"
                self._notify(
                    "recovered",
                    reason,
                    offset_ms=self.offset,
                    rtt_ms=rtt,
                    consecutive_failures=self.consecutive_failures,
                )
            else:
                if previous_state == "healthy":
                    self._health_state = "healthy"

            return True
        except Exception as exc:
            self.consecutive_failures += 1
            self.last_error = str(exc)
            logger.error(f"Time Sync Failed: {exc}")
            if self.consecutive_failures >= self.max_consecutive_failures:
                self._health_state = "halt"
                self._notify(
                    "halt",
                    f"time sync failed {self.consecutive_failures} times: {exc}",
                    consecutive_failures=self.consecutive_failures,
                    last_error=self.last_error,
                )
            return False

    def _auto_sync_loop(self):
        while self.active:
            interval = self.sync_interval_sec if self._health_state == "healthy" else self.unhealthy_retry_sec
            time.sleep(interval)
            self._sync()


time_service = TimeService()
