"""Venue dead-man-switch supervision owned outside RiskManager."""

from __future__ import annotations

from infrastructure.oms_risk_port import RiskOMSPort


class VenueDMSController:
    """Own DMS health, renewal authorization, and fail-closed latching."""

    def __init__(
        self,
        *,
        root_config: dict,
        oms: RiskOMSPort | None,
        logger,
    ):
        self.root_config = root_config
        self.oms = oms
        self.logger = logger
        self.renewal_authorized = True
        self.supervisor_healthy = True
        self.failure_reason = ""
        self.last_renewal_result = None

    @property
    def enabled(self) -> bool:
        return bool(
            (
                self.root_config.get("oms", {}).get(
                    "venue_dead_man_switch",
                    {},
                )
                or {}
            ).get("enabled", False)
        )

    def status_snapshot(self) -> dict:
        return {
            "venue_dms_renewal_authorized": bool(self.renewal_authorized),
            "venue_dms_supervisor_healthy": bool(self.supervisor_healthy),
            "venue_dms_failure_reason": self.failure_reason,
            "last_venue_dms_renewal_result": self.last_renewal_result,
        }

    def set_supervisor_health(self, healthy: bool) -> None:
        previous = bool(self.supervisor_healthy)
        self.supervisor_healthy = bool(healthy)
        if self.supervisor_healthy or not previous:
            return
        oms_state = str(
            getattr(getattr(self.oms, "state", None), "value", "") or ""
        ).upper()
        if self.enabled and oms_state == "LIVE":
            self.latch_failure("independent_supervisor_unhealthy")

    def latch_failure(self, reason: str) -> bool:
        reason = str(reason or "renewal_health_invalid")
        self.renewal_authorized = False
        self.failure_reason = reason
        self.last_renewal_result = False
        latch_reason = f"venue_dead_man_switch:{reason}"

        self.logger.critical(
            "[Risk] Venue dead-man switch safety latch engaged: "
            f"{reason}"
        )
        handle_unhealthy = getattr(
            self.oms,
            "handle_venue_dead_man_switch_unhealthy",
            None,
        )
        if callable(handle_unhealthy):
            try:
                if handle_unhealthy(reason) is False:
                    return False
            except Exception as exc:
                self.logger.critical(
                    "[Risk] DMS safety handler failed: "
                    f"{type(exc).__name__}:{exc}"
                )

        freeze = getattr(self.oms, "freeze_system", None)
        if callable(freeze):
            try:
                freeze(latch_reason, cancel_active_orders=True)
                return False
            except Exception as exc:
                self.logger.critical(
                    "[Risk] DMS freeze/cancel request failed: "
                    f"{type(exc).__name__}:{exc}"
                )

        halt = getattr(self.oms, "halt_system", None)
        if callable(halt):
            try:
                halt(latch_reason)
            except Exception as exc:
                self.logger.critical(
                    "[Risk] DMS fallback halt/cancel request failed: "
                    f"{type(exc).__name__}:{exc}"
                )
        return False

    def renew(self, *, active: bool, kill_switch_triggered: bool) -> bool:
        if not self.enabled:
            return True
        if self.oms is None:
            return False
        if not self.renewal_authorized or not self.supervisor_healthy:
            return False

        oms_state = str(
            getattr(getattr(self.oms, "state", None), "value", "") or ""
        ).upper()
        if oms_state != "LIVE":
            return True

        snapshot_reader = getattr(
            self.oms,
            "get_venue_dead_man_switch_snapshot",
            None,
        )
        if not callable(snapshot_reader):
            return self.latch_failure("health_snapshot_unavailable")
        try:
            snapshot = snapshot_reader()
        except Exception as exc:
            return self.latch_failure(
                f"health_snapshot_failed:{type(exc).__name__}:{exc}"
            )
        if not isinstance(snapshot, dict):
            return self.latch_failure("health_snapshot_invalid")
        if not bool(snapshot.get("enabled", False)):
            return self.latch_failure("unexpectedly_disabled")
        if not bool(snapshot.get("valid", False)):
            return self.latch_failure(
                str(snapshot.get("reason", "") or "renewal_health_invalid")
            )

        if not active or kill_switch_triggered or self._shutdown_started():
            return False
        if self._new_risk_blocked():
            return True

        renewal_allowed = getattr(
            self.oms,
            "can_renew_venue_dead_man_switch",
            None,
        )
        if not callable(renewal_allowed) or not bool(renewal_allowed()):
            return self.latch_failure("renewal_not_permitted")

        renew = getattr(
            self.oms,
            "request_venue_dead_man_switch_renewal",
            None,
        )
        if not callable(renew):
            renew = getattr(self.oms, "renew_venue_dead_man_switch", None)
        if not callable(renew):
            return self.latch_failure("renewal_method_unavailable")
        try:
            renewed = bool(renew())
        except Exception as exc:
            return self.latch_failure(
                f"renewal_request_failed:{type(exc).__name__}:{exc}"
            )

        self.last_renewal_result = renewed
        if renewed:
            return True
        try:
            failed_snapshot = snapshot_reader()
        except Exception:
            failed_snapshot = {}
        failure_reason = (
            str(failed_snapshot.get("reason", "") or "")
            if isinstance(failed_snapshot, dict)
            else ""
        )
        return self.latch_failure(
            failure_reason or "renewal_request_rejected"
        )

    def _shutdown_started(self) -> bool:
        shutdown_started = getattr(self.oms, "is_shutdown_started", None)
        return True if not callable(shutdown_started) else bool(shutdown_started())

    def _new_risk_blocked(self) -> bool:
        can_open_new_risk = getattr(self.oms, "can_open_new_risk", None)
        return bool(
            not callable(can_open_new_risk)
            or not can_open_new_risk()
        )
