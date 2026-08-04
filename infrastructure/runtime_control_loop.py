"""Main runtime supervision loop with explicit collaborators."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from infrastructure.runtime_telemetry import TelemetryPublisher
from infrastructure.watchdog import (
    emit_event_engine_backlog_if_needed,
    emit_market_data_stale_if_needed,
    emit_strategy_runtime_backlog_if_needed,
)


@dataclass(frozen=True, slots=True)
class RuntimeControlServices:
    enforce_external_alert_health: Callable
    enforce_live_evidence_health: Callable
    run_live_risk_checks: Callable
    external_alert_health: Callable
    live_evidence_health: Callable
    build_runtime_resource_event: Callable


class RuntimeControlLoop:
    """Execute one observable supervision tick or run continuously."""

    def __init__(
        self,
        *,
        config: dict,
        paper_trade: bool,
        systemd_watchdog,
        external_alerts,
        live_evidence_recorder,
        oms,
        risk_controller,
        risk_supervisor,
        engine,
        event_engine_config: dict,
        gateway,
        strategy,
        strategy_runtime,
        data_recorder,
        resource_monitor,
        event_bindings,
        watchdog_state,
        web_dashboard,
        admin_control,
        logger,
        services: RuntimeControlServices,
        telemetry_publisher=None,
        readiness_evaluator=None,
        readiness_components: dict | None = None,
        readiness_required: tuple[str, ...] = (),
        clock_service=None,
        read_clock_health: Callable | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.paper_trade = bool(paper_trade)
        self.systemd_watchdog = systemd_watchdog
        self.external_alerts = external_alerts
        self.live_evidence_recorder = live_evidence_recorder
        self.oms = oms
        self.risk_controller = risk_controller
        self.risk_supervisor = risk_supervisor
        self.engine = engine
        self.event_engine_config = event_engine_config
        self.gateway = gateway
        self.strategy = strategy
        self.strategy_runtime = strategy_runtime
        self.data_recorder = data_recorder
        self.resource_monitor = resource_monitor
        self.event_bindings = event_bindings
        self.watchdog_state = watchdog_state
        self.web_dashboard = web_dashboard
        self.admin_control = admin_control
        self.logger = logger
        self.services = services
        self.sleep = sleep
        self.readiness_evaluator = readiness_evaluator
        self.readiness_components = (
            readiness_components if readiness_components is not None else {}
        )
        self.readiness_required = tuple(readiness_required)
        self.clock_service = clock_service
        self.read_clock_health = read_clock_health
        self._owns_telemetry_publisher = bool(
            telemetry_publisher is None and web_dashboard is not None
        )
        self.telemetry_publisher = telemetry_publisher
        if self.telemetry_publisher is None and web_dashboard is not None:
            self.telemetry_publisher = TelemetryPublisher.for_dashboard(
                web_dashboard
            )
        dashboard_config = (
            config.get("system", {}).get("web_dashboard", {}) or {}
        )
        self.telemetry_stop_timeout_sec = max(
            0.0,
            float(
                dashboard_config.get("telemetry_stop_timeout_sec", 5.0)
                or 0.0
            ),
        )
        self.external_alert_constraint_state: dict = {}
        self.last_recorded_resource_sample_at = None

    def run_forever(self) -> None:
        try:
            while True:
                self.sleep(0.1)
                self.run_once()
        finally:
            self.close()

    def close(self) -> bool:
        if not self._owns_telemetry_publisher:
            return True
        publisher = self.telemetry_publisher
        self._owns_telemetry_publisher = False
        if publisher is None:
            return True
        return bool(
            publisher.close(
                timeout_sec=self.telemetry_stop_timeout_sec,
                flush=True,
            )
        )

    def run_once(self) -> dict:
        self.systemd_watchdog.pulse()
        self.services.enforce_external_alert_health(
            self.external_alerts,
            self.oms,
            self.external_alert_constraint_state,
        )
        if not self.paper_trade:
            self.services.enforce_live_evidence_health(
                self.live_evidence_recorder,
                self.oms,
            )
        risk_ready = self.services.run_live_risk_checks(
            self.risk_controller,
            self.risk_supervisor,
        )
        self._update_watchdogs()

        resource_snapshot = self.resource_monitor.sample(
            self._monitored_child_pids()
        )
        self._record_resource_sample(resource_snapshot)
        resource_failure = self.resource_monitor.consume_fail_closed_reason()
        if resource_failure:
            self.oms.freeze_system(
                f"ProcessResources: {resource_failure}",
                cancel_active_orders=True,
            )

        runtime_metrics = self._runtime_metrics(resource_snapshot)
        readiness = self._update_readiness(
            runtime_metrics,
            risk_ready=risk_ready,
            resource_ready=not bool(resource_failure),
        )
        if readiness is not None:
            runtime_metrics["runtime_readiness"] = readiness.as_dict()
        if self.telemetry_publisher is not None:
            self.telemetry_publisher.submit(runtime_metrics)
        self.admin_control.poll_once()
        return runtime_metrics

    def _update_readiness(
        self,
        runtime_metrics: dict,
        *,
        risk_ready,
        resource_ready: bool,
    ):
        if self.readiness_evaluator is None:
            return None
        if risk_ready is not None:
            self.readiness_components["risk_policy"] = bool(risk_ready)
        if callable(self.read_clock_health) and self.clock_service is not None:
            try:
                clock_health = self.read_clock_health(self.clock_service)
                self.readiness_components["clock"] = bool(
                    clock_health.get("ready", False)
                )
            except Exception:
                self.readiness_components["clock"] = False
        self.readiness_components["resources"] = bool(resource_ready)
        self.readiness_components["alerts"] = self._health_ready(
            runtime_metrics.get("external_alerts")
        )
        if not self.paper_trade:
            self.readiness_components["evidence"] = self._health_ready(
                runtime_metrics.get("live_evidence")
            )
        gateway_active = getattr(self.gateway, "active", None)
        strategy_active = getattr(self.strategy_runtime, "_active", None)
        if gateway_active is not None:
            self.readiness_components["transport"] = bool(gateway_active)
        if gateway_active is not None and strategy_active is not None:
            self.readiness_components["execution"] = bool(
                gateway_active and strategy_active
            )
        execution_enabled = bool(
            self.readiness_components.get("execution", False)
        )
        capability_mode = getattr(self.oms, "capability_mode", "")
        capability_mode = getattr(capability_mode, "value", capability_mode)
        return self.readiness_evaluator.evaluate(
            self.readiness_components,
            required=self.readiness_required,
            phase="runtime",
            execution_enabled=execution_enabled,
            require_execution=True,
            operating_mode=str(capability_mode or ""),
        )

    @staticmethod
    def _health_ready(snapshot) -> bool:
        if not isinstance(snapshot, dict):
            return snapshot is not False
        if "healthy" in snapshot:
            return bool(snapshot["healthy"])
        if "ready" in snapshot:
            return bool(snapshot["ready"])
        return True

    def _update_watchdogs(self) -> None:
        self.watchdog_state.stale_watchdog_triggered = (
            emit_market_data_stale_if_needed(
                self.engine,
                self.watchdog_state.last_tick_time,
                self.watchdog_state.stale_watchdog_triggered,
            )
        )
        self.watchdog_state.event_engine = emit_event_engine_backlog_if_needed(
            self.engine,
            self.oms,
            getattr(self.gateway, "gateway_name", "UNKNOWN"),
            self.watchdog_state.event_engine,
            self.event_engine_config,
        )
        self.watchdog_state.strategy_runtime = (
            emit_strategy_runtime_backlog_if_needed(
                self.strategy_runtime,
                self.oms,
                self.strategy.name,
                self.watchdog_state.strategy_runtime,
                self.config.get("system", {}).get("strategy_runtime", {}),
            )
        )

    def _monitored_child_pids(self) -> list[int]:
        monitored_child_pids = []
        recorder_process = getattr(self.data_recorder, "_writer_process", None)
        recorder_pid = getattr(recorder_process, "pid", None)
        if isinstance(recorder_pid, int) and recorder_pid > 0:
            monitored_child_pids.append(recorder_pid)
        supervisor_process = getattr(self.risk_supervisor, "process", None)
        supervisor_pid = getattr(supervisor_process, "pid", None)
        if isinstance(supervisor_pid, int) and supervisor_pid > 0:
            monitored_child_pids.append(supervisor_pid)
        return monitored_child_pids

    def _record_resource_sample(self, resource_snapshot: dict) -> None:
        sampled_at = resource_snapshot.get("sampled_at_monotonic")
        if (
            not self.paper_trade
            or sampled_at is None
            or sampled_at == self.last_recorded_resource_sample_at
        ):
            return
        self.last_recorded_resource_sample_at = sampled_at
        self.event_bindings.record_paper_observation(
            "record_paper_system_event",
            "runtime_resources",
            self.services.build_runtime_resource_event(
                resource_snapshot,
                self.systemd_watchdog.snapshot(),
            ),
        )

    def _runtime_metrics(self, resource_snapshot: dict) -> dict:
        return {
            "logger": self.logger.get_metrics_snapshot(),
            "event_engine": self.engine.get_metrics_snapshot(),
            "event_handlers": self.engine.get_handler_metrics_snapshot(
                limit=50
            ),
            "strategy_runtime": self.strategy_runtime.get_metrics_snapshot(),
            "external_alerts": self.services.external_alert_health(
                self.external_alerts
            ),
            "live_evidence": self.services.live_evidence_health(
                self.live_evidence_recorder
            ),
            "process_resources": resource_snapshot,
            "systemd_watchdog": self.systemd_watchdog.snapshot(),
        }
