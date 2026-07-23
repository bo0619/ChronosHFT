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
        self.attempts_by_recovery = {}
        self.last_attempt_ts_by_recovery = {}
        self._stop_event = threading.Event()

        if start_thread and self.poll_interval_sec > 0:
            self.start()

    def start(self):
        if self.active or self.poll_interval_sec <= 0:
            return
        self.active = True
        self._stop_event.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.active = False
        self._stop_event.set()
        thread = self.thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.poll_interval_sec + 0.5))
        stopped = not thread or not thread.is_alive()
        if not stopped:
            logger.critical(
                "[VenueSupervisor] Recovery worker did not stop before timeout"
            )
        return stopped

    def _loop(self):
        while self.active:
            if self._stop_event.wait(self.poll_interval_sec):
                break
            try:
                self.poll_once()
            except Exception as exc:
                logger.error(f"[VenueSupervisor] Poll failed: {exc}")

    def poll_once(self):
        venue = getattr(self.gateway, "gateway_name", "UNKNOWN")
        get_owners = getattr(self.oms, "get_venue_freeze_owners", None)
        owners = get_owners(venue) if callable(get_owners) else {}
        candidates = [
            (
                str(owner or ""),
                int((record or {}).get("epoch", 0) or 0),
                str((record or {}).get("reason", "") or ""),
            )
            for owner, record in (owners or {}).items()
            if str((record or {}).get("reason", "") or "").startswith(
                self.recoverable_prefixes
            )
        ]
        context = None
        if candidates:
            owner, epoch, reason = max(
                candidates,
                key=lambda item: (item[1], item[0]),
            )
            context = {
                "venue": str(venue or "").upper(),
                "owner": owner,
                "epoch": epoch,
                "reason": reason,
            }
            recovery_key = (context["venue"], owner, epoch)
        else:
            # Compatibility for simple adapters without owner-aware guards.
            reason = self.oms.get_venue_freeze_reason(venue)
            if not reason or not reason.startswith(self.recoverable_prefixes):
                stale_keys = [
                    key
                    for key in self.attempts_by_recovery
                    if key[0] == str(venue or "").upper()
                ]
                for key in stale_keys:
                    self.attempts_by_recovery.pop(key, None)
                    self.last_attempt_ts_by_recovery.pop(key, None)
                return False
            recovery_key = (
                str(venue or "").upper(),
                "legacy",
                str(reason),
            )

        stale_keys = [
            key
            for key in self.attempts_by_recovery
            if key[0] == str(venue or "").upper() and key != recovery_key
        ]
        for key in stale_keys:
            self.attempts_by_recovery.pop(key, None)
            self.last_attempt_ts_by_recovery.pop(key, None)

        attempts = self.attempts_by_recovery.get(recovery_key, 0)
        last_attempt_ts = self.last_attempt_ts_by_recovery.get(
            recovery_key,
            0.0,
        )
        now = time.perf_counter()
        if attempts >= self.max_attempts:
            logger.error(
                f"[VenueSupervisor] Recovery budget exhausted for {venue}: "
                f"{reason}"
            )
            return False
        if now - last_attempt_ts < self.recovery_delay_sec:
            return False

        self.attempts_by_recovery[recovery_key] = attempts + 1
        self.last_attempt_ts_by_recovery[recovery_key] = now
        logger.warning(
            f"[VenueSupervisor] Recovering {venue} "
            f"({self.attempts_by_recovery[recovery_key]}/{self.max_attempts}) "
            f"because {reason}"
        )
        if context is None:
            return bool(self.gateway.recover_connectivity())
        return bool(
            self.gateway.recover_connectivity(
                recovery_context=context,
            )
        )
