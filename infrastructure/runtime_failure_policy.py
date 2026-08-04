"""Fail-closed reactions for runtime health and handler failures."""

from __future__ import annotations


class RuntimeFailurePolicy:
    """Own failure callbacks without closing over application state."""

    def __init__(
        self,
        *,
        oms,
        risk_controller,
        strategy_name: str = "",
        gateway_name: str,
        logger,
    ):
        self.oms = oms
        self.risk_controller = risk_controller
        self.strategy_name = str(strategy_name)
        self.gateway_name = str(gateway_name or "UNKNOWN")
        self.logger = logger
        self.clock_runtime_armed = False

    def bind_strategy(self, strategy_name: str) -> None:
        strategy_name = str(strategy_name or "")
        if not strategy_name:
            raise ValueError("strategy_name must be non-empty")
        if self.strategy_name and self.strategy_name != strategy_name:
            raise RuntimeError("runtime failure policy strategy already bound")
        self.strategy_name = strategy_name

    def arm_clock_runtime(self) -> None:
        self.clock_runtime_armed = True

    def on_time_service_health(self, severity, reason, _details) -> None:
        reason = str(reason or "unknown")
        if severity == "freeze":
            self.oms.freeze_system(
                f"TimeSync: {reason}",
                cancel_active_orders=True,
            )
            return
        if severity == "halt":
            if self.clock_runtime_armed:
                self.risk_controller.trigger_kill_switch(
                    f"TimeSync: {reason}"
                )
            else:
                self.oms.halt_system(f"TimeSync startup: {reason}")
            return
        if severity == "recovered" and self.oms.state.value == "FROZEN":
            if self.oms.last_freeze_reason.startswith("TimeSync:"):
                self.oms.trigger_reconcile("Time sync recovered")

    def on_event_engine_failure(self, failure: dict) -> None:
        lane = str(failure.get("lane", "") or "unknown")
        event_type = str(failure.get("event_type", "") or "unknown")
        failure_kind = str(failure.get("kind", "") or "unknown")
        handler_name = str(
            failure.get("handler_name", "") or "unavailable"
        )
        reason = (
            f"event_engine_failure:{failure_kind}:{lane}:"
            f"{event_type}:{handler_name}"
        )
        target = "strategy" if lane == "cold" else "venue"
        self._fail_closed(
            scope="EventEngine",
            reason=reason,
            target=target,
        )

    def on_strategy_runtime_failure(self, failure: dict) -> None:
        reason = (
            "strategy_runtime_failure:"
            f"{failure.get('phase', 'unknown')}:"
            f"{failure.get('kind', 'unknown')}:"
            f"{failure.get('handler_name', 'unavailable')}"
        )
        self._fail_closed(
            scope="StrategyRuntime",
            reason=reason,
            target="strategy",
        )

    def on_live_evidence_failure(self, reason) -> None:
        self.oms.freeze_system(
            f"LiveEvidence: {reason}",
            cancel_active_orders=True,
        )

    def _fail_closed(self, *, scope: str, reason: str, target: str) -> None:
        try:
            if target == "strategy":
                if not self.strategy_name:
                    raise RuntimeError("strategy failure target is not bound")
                self.oms.freeze_strategy(
                    self.strategy_name,
                    reason,
                    cancel_active_orders=True,
                )
            else:
                self.oms.freeze_venue(
                    self.gateway_name,
                    reason,
                    cancel_active_orders=True,
                )
        except BaseException as exc:
            self.logger.critical(
                f"[{scope}] Fail-closed freeze raised: "
                f"{type(exc).__name__}:{exc}; escalating kill-switch"
            )
            self.risk_controller.trigger_kill_switch(
                f"{reason}:freeze_failed:{type(exc).__name__}"
            )
