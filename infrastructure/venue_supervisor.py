import threading
import time

from infrastructure.logger import logger


class VenueSupervisor:
    def __init__(self, oms, gateway, config, start_thread=True):
        self.oms = oms
        self.gateway = gateway

        cfg = config.get("oms", {}).get("venue_supervisor", {})
        self.poll_interval_sec = float(cfg.get("poll_interval_sec", 5.0))
        self.recovery_delay_sec = float(cfg.get("recovery_delay_sec", 2.0))
        self.max_attempts = max(1, int(cfg.get("max_attempts", 5)))
        self.recoverable_prefixes = tuple(
            cfg.get(
                "recoverable_prefixes",
                [
                    "system_health:WS_TRANSPORT_DROP",
                    "system_health:WS_PARSE_ERROR",
                    "system_health:WS_HANDLER_FAILURE",
                    "system_health:USER_STREAM_EXPIRED",
                    "system_health:MARKET_DATA_STALE",
                ],
            )
        )

        self.active = False
        self.thread = None
        self.attempts_by_venue = {}
        self.last_attempt_ts_by_venue = {}

        if start_thread and self.poll_interval_sec > 0:
            self.start()

    def start(self):
        if self.active or self.poll_interval_sec <= 0:
            return
        self.active = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.active = False

    def _loop(self):
        while self.active:
            time.sleep(self.poll_interval_sec)
            try:
                self.poll_once()
            except Exception as exc:
                logger.error(f"[VenueSupervisor] Poll failed: {exc}")

    def poll_once(self):
        venue = getattr(self.gateway, "gateway_name", "UNKNOWN")
        reason = self.oms.get_venue_freeze_reason(venue)
        if not reason or not reason.startswith(self.recoverable_prefixes):
            self.attempts_by_venue.pop(venue, None)
            self.last_attempt_ts_by_venue.pop(venue, None)
            return False

        attempts = self.attempts_by_venue.get(venue, 0)
        last_attempt_ts = self.last_attempt_ts_by_venue.get(venue, 0.0)
        now = time.monotonic()
        if attempts >= self.max_attempts:
            logger.error(f"[VenueSupervisor] Recovery budget exhausted for {venue}: {reason}")
            return False
        if now - last_attempt_ts < self.recovery_delay_sec:
            return False

        self.attempts_by_venue[venue] = attempts + 1
        self.last_attempt_ts_by_venue[venue] = now
        logger.warning(
            f"[VenueSupervisor] Recovering {venue} "
            f"({self.attempts_by_venue[venue]}/{self.max_attempts}) because {reason}"
        )
        return bool(self.gateway.recover_connectivity())
