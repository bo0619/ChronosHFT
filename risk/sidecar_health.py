"""Parent-side OMS health propagation and recovery gating."""


class SidecarOmsHealth:
    CONSTRAINT_PREFIX = ("independent_supervisor:",)

    @staticmethod
    def record_heartbeat(owner, healthy: bool, reason: str, source: str):
        record = getattr(owner.oms, "record_risk_control_heartbeat", None)
        if callable(record):
            return bool(
                record(
                    source=source,
                    healthy=healthy,
                    reason=str(reason or ""),
                )
            )
        return False

    @staticmethod
    def reset_recovery_progress(owner) -> None:
        owner.recovery_count = 0
        try:
            snapshot_sequence = int(
                owner.last_status.get("risk_snapshot_sequence", 0) or 0
            )
        except (AttributeError, TypeError, ValueError):
            snapshot_sequence = 0
        owner.last_recovery_snapshot_sequence = max(0, snapshot_sequence)

    @classmethod
    def apply(
        cls,
        owner,
        healthy: bool,
        reason: str,
        perf_counter,
        isfinite,
    ) -> bool:
        heartbeat_recorded = owner._record_oms_heartbeat(healthy, reason)
        if not healthy:
            owner._reset_recovery_progress()
            risk_action = str(
                owner.last_status.get("risk_action", "") or ""
            ).upper()
            if risk_action == "KILL":
                trigger_kill = getattr(
                    owner.risk_manager,
                    "trigger_kill_switch",
                    None,
                )
                if callable(trigger_kill):
                    trigger_kill(
                        f"IndependentSupervisor: {reason or 'hard risk breach'}"
                    )
                else:
                    halt = getattr(owner.oms, "halt_system", None)
                    if callable(halt):
                        halt(
                            "IndependentSupervisor: "
                            f"{reason or 'hard risk breach'}"
                        )
            set_mode = getattr(owner.oms, "set_trading_mode", None)
            if callable(set_mode):
                from event.type import OMSCapabilityMode

                set_mode(
                    OMSCapabilityMode.REDUCE_ONLY,
                    f"independent_supervisor:{reason or 'unhealthy'}",
                )
            return False

        has_constraint = getattr(
            owner.oms,
            "has_trading_mode_constraint",
            None,
        )
        constrained = bool(
            callable(has_constraint)
            and has_constraint(cls.CONSTRAINT_PREFIX)
        )
        if not constrained:
            owner._reset_recovery_progress()
            return heartbeat_recorded

        try:
            snapshot_sequence = int(
                owner.last_status.get("risk_snapshot_sequence", 0) or 0
            )
            snapshot_captured_monotonic = float(
                owner.last_status.get(
                    "risk_snapshot_captured_monotonic",
                    0.0,
                )
                or 0.0
            )
        except (AttributeError, TypeError, ValueError):
            owner._reset_recovery_progress()
            return False
        now = perf_counter()
        snapshot_age = now - min(now, snapshot_captured_monotonic)
        if (
            snapshot_sequence <= 0
            or not isfinite(snapshot_captured_monotonic)
            or snapshot_captured_monotonic <= 0.0
            or snapshot_captured_monotonic > now
            or snapshot_age > owner.recovery_snapshot_max_age_sec
        ):
            owner._reset_recovery_progress()
            return False
        if snapshot_sequence < owner.last_recovery_snapshot_sequence:
            owner._reset_recovery_progress()
            return False
        if snapshot_sequence > owner.last_recovery_snapshot_sequence:
            owner.last_recovery_snapshot_sequence = snapshot_sequence
            owner.recovery_count += 1
        if owner.recovery_count < owner.recovery_checks:
            return False

        clear_mode = getattr(owner.oms, "clear_trading_mode", None)
        if callable(clear_mode):
            clear_mode(
                reason="independent risk supervisor recovered",
                prefixes=cls.CONSTRAINT_PREFIX,
            )
        owner._reset_recovery_progress()
        return heartbeat_recorded

    @staticmethod
    def tick(owner, perf_counter) -> bool:
        if not owner.enabled:
            return True
        now = perf_counter()
        if owner.process is None or not owner.process.is_alive():
            owner._apply_oms_health(False, "supervisor_process_down")
            return False
        owner._send_heartbeat(now)
        owner._drain_status(now)
        if not owner.last_status:
            reason = (
                "supervisor_status_invalid:"
                f"{owner.last_status_protocol_error}"
                if owner.last_status_protocol_error
                else "supervisor_status_missing"
            )
            owner._apply_oms_health(False, reason)
            return False
        status_age = max(0.0, now - owner.last_status_received_at)
        if status_age > owner.status_max_age_sec:
            owner._apply_oms_health(False, "supervisor_status_stale")
            return False
        healthy = bool(owner.last_status.get("healthy", False))
        reason = str(owner.last_status.get("reason", "") or "")
        return owner._apply_oms_health(healthy, reason)

    @staticmethod
    def wait_until_healthy(
        owner,
        timeout_sec: float,
        perf_counter,
        sleep,
    ) -> bool:
        if not owner.enabled:
            return True
        deadline = perf_counter() + max(0.0, float(timeout_sec or 0.0))
        while perf_counter() <= deadline:
            if owner.tick():
                return True
            sleep(0.05)
        return False
