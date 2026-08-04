"""Retryable, fail-closed shutdown coordination for the process runtime."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import Any, Callable

from infrastructure.runtime_resources import RuntimeResources


@dataclass(frozen=True)
class RuntimeShutdownServices:
    logger: Any
    call_with_risk_heartbeat: Callable
    verify_account_flat: Callable
    wait_for_kill_flatten_verification: Callable
    requires_live_canary_shutdown_truth: Callable


class RuntimeShutdownCoordinator:
    """Advance a persisted shutdown state machine to a terminal outcome."""

    def __init__(
        self,
        runtime: RuntimeResources,
        reason: str,
        services: RuntimeShutdownServices,
    ) -> None:
        self.runtime = runtime
        self.reason = str(reason or "main_exit")
        self.services = services
        self.logger = services.logger

        self.oms = runtime.get("oms")
        self.engine = runtime.get("engine")
        self.gateway = runtime.get("gateway")
        self.truth_provider = runtime.get("truth_provider")
        self.strategy_runtime = runtime.get("strategy_runtime")
        self.risk_controller = runtime.get("risk_controller")
        self.risk_supervisor = runtime.get("risk_supervisor")
        self.truth_monitor = runtime.get("truth_monitor")
        self.venue_supervisor = runtime.get("venue_supervisor")
        self.recorder = runtime.get("recorder")
        self.admin_control = runtime.get("admin_control")
        self.web_dashboard = runtime.get("web_dashboard")
        self.clock_service = runtime.get("time_service")
        self.event_engine_config = runtime.get("event_engine_config", {}) or {}
        self.phase = runtime.setdefault("_shutdown_phase", {})

        proof_marker = runtime.get("_account_shutdown_proof_required")
        self.account_proof_required = bool(
            proof_marker
            if proof_marker is not None
            else self.oms is not None and self.truth_provider is not None
        )
        self.live_canary_truth_required = bool(
            services.requires_live_canary_shutdown_truth(runtime)
        )
        supervisor_enabled = bool(
            self.risk_supervisor is not None
            and getattr(self.risk_supervisor, "enabled", False)
        )
        supervisor_marker = runtime.get("_risk_supervisor_started")
        supervisor_process = getattr(self.risk_supervisor, "process", None)
        supervisor_process_alive = bool(
            supervisor_process is not None and supervisor_process.is_alive()
        )
        self.supervisor_started = bool(
            supervisor_enabled
            and (
                bool(supervisor_marker)
                or supervisor_process_alive
                or supervisor_marker is None
            )
        )
        runtime["_risk_supervisor_started"] = self.supervisor_started
        self._restore_phase_state()

    @classmethod
    def execute(
        cls,
        runtime,
        reason: str,
        services: RuntimeShutdownServices,
    ) -> bool:
        if not runtime or runtime.get("_shutdown_complete"):
            return bool(runtime and runtime.get("_shutdown_verified", False))
        if runtime.get("_shutdown_in_progress"):
            return False
        resources = RuntimeResources.coerce(runtime)
        resources["_shutdown_in_progress"] = True
        coordinator = cls(resources, reason, services)
        try:
            return coordinator._execute()
        finally:
            if coordinator.dashboard_stopped:
                services.logger.set_ui_callback(None)
            resources["_shutdown_in_progress"] = False

    def _restore_phase_state(self) -> None:
        barrier = bool(self.phase.get("barrier_verified", False))
        proof_not_needed = not self.account_proof_required
        self.shutdown_barrier_verified = barrier
        self.cancel_verified = bool(barrier or proof_not_needed)
        self.independently_flat = self.cancel_verified
        self.kill_flatten_verified = self.cancel_verified
        self.flatten_verified = self.cancel_verified
        self.supervisor_quiesced = bool(
            self.phase.get(
                "supervisor_quiesced",
                not self.supervisor_started,
            )
        )
        self.supervisor_stopped = bool(
            self.phase.get(
                "supervisor_stopped",
                not self.supervisor_started,
            )
        )
        self.outbound_gate_closed = bool(
            self.phase.get("outbound_gate_closed", self.oms is None)
        )
        self.shutdown_latched = bool(
            self.phase.get("shutdown_latched", self.oms is None)
        )
        self.gateway_shutdown_latched = bool(
            self.phase.get(
                "gateway_shutdown_latched",
                self.gateway is None,
            )
        )
        self.strategy_stopped = bool(
            self.phase.get(
                "strategy_stopped",
                self.strategy_runtime is None,
            )
        )
        self.venue_supervisor_stopped = bool(
            self.phase.get(
                "venue_supervisor_stopped",
                self.venue_supervisor is None,
            )
        )
        self.truth_monitor_stopped = bool(
            self.phase.get(
                "truth_monitor_stopped",
                self.truth_monitor is None,
            )
        )
        self.gateway_closed = bool(
            self.phase.get("gateway_closed", self.gateway is None)
        )
        self.event_drained = bool(
            self.phase.get("event_drained", self.engine is None)
        )
        self.truth_provider_closed = bool(
            self.phase.get(
                "truth_provider_closed",
                self.truth_provider is None,
            )
        )
        self.recorder_closed = bool(
            self.phase.get("recorder_closed", self.recorder is None)
        )
        self.admin_control_closed = bool(
            self.phase.get(
                "admin_control_closed",
                self.admin_control is None,
            )
        )
        self.oms_stopped = bool(
            self.phase.get("oms_stopped", self.oms is None)
        )
        self.oms_clean = bool(
            self.phase.get("oms_clean", self.oms is None)
        )
        self.engine_stopped = bool(
            self.phase.get("engine_stopped", self.engine is None)
        )
        self.dashboard_stopped = bool(
            self.phase.get(
                "dashboard_stopped",
                self.web_dashboard is None,
            )
        )
        self.clock_stopped = bool(
            self.phase.get("clock_stopped", self.clock_service is None)
        )

    def _execute(self) -> bool:
        self._publish_shutting_down()
        self._close_outbound_gate()
        self._stop_strategy()
        if not self.shutdown_barrier_verified:
            if not self._establish_safety_barrier():
                return False
        if not self._stop_observers_and_transport():
            return False
        if not self._stop_durable_core():
            return False
        return self._finalize()

    def _run_step(self, name: str, callback: Callable):
        try:
            return True, callback()
        except BaseException as exc:
            self.logger.error(
                f"[Shutdown] {name} failed: {type(exc).__name__}:{exc}"
            )
            return False, None

    @staticmethod
    def _step_acknowledged(ok, result) -> bool:
        return bool(ok and result is not False)

    @staticmethod
    def _quiesce_acknowledged(result) -> bool:
        return bool(
            isinstance(result, dict)
            and result.get("accepted") is True
            and result.get("quiesced") is True
            and result.get("persisted") is True
        )

    @staticmethod
    def _stop_acknowledged(result) -> bool:
        return bool(
            isinstance(result, dict)
            and result.get("accepted") is True
            and result.get("quiesced") is True
            and result.get("cancel_requested") is False
            and result.get("process_exited") is True
            and result.get("forced_terminated") is False
        )

    @staticmethod
    def _shutdown_resume_acknowledged(result) -> bool:
        return bool(
            isinstance(result, dict)
            and result.get("accepted") is True
            and result.get("quiesced") is False
            and result.get("kill_latched") is True
            and result.get("persisted") is True
        )

    def _publish_shutting_down(self) -> None:
        if self.web_dashboard is None:
            return
        self._run_step(
            "dashboard_status",
            partial(
                self.web_dashboard.set_startup_status,
                state="SHUTTING_DOWN",
                operating_mode="CANCEL_ONLY",
                startup_blocked=False,
                execution_enabled=False,
                restart_required=False,
                reason=self.reason,
            ),
        )
        self._run_step(
            "dashboard_publish_shutting_down",
            partial(self.web_dashboard.publish_snapshot, force=True),
        )

    def _publish_shutdown_blocked(self, block_reason: str) -> None:
        self.runtime["_shutdown_verified"] = False
        self.runtime["_shutdown_retryable"] = True
        if self.web_dashboard is None or self.dashboard_stopped:
            return
        self._run_step(
            "dashboard_shutdown_blocked",
            partial(
                self.web_dashboard.set_startup_status,
                state="SHUTDOWN_BLOCKED",
                operating_mode="LOCKDOWN",
                startup_blocked=True,
                execution_enabled=False,
                restart_required=True,
                reason=block_reason,
            ),
        )
        self._run_step(
            "dashboard_publish_blocked",
            partial(self.web_dashboard.publish_snapshot, force=True),
        )

    def _close_outbound_gate(self) -> None:
        if self.outbound_gate_closed:
            return
        close_gate = getattr(self.oms, "close_outbound_gate", None)
        if callable(close_gate):
            ok, result = self._run_step(
                "oms_close_outbound_gate",
                partial(
                    close_gate,
                    f"shutdown:{self.reason}",
                    wait=False,
                ),
            )
            self.outbound_gate_closed = self._step_acknowledged(ok, result)
        self.phase["outbound_gate_closed"] = self.outbound_gate_closed

    def _stop_strategy_callback(self, timeout_sec: float):
        try:
            return self.strategy_runtime.stop(timeout_sec=timeout_sec)
        except TypeError:
            return self.strategy_runtime.stop()

    def _stop_strategy(self) -> None:
        if self.strategy_stopped:
            return
        timeout_sec = max(
            0.0,
            float(
                getattr(self.strategy_runtime, "shutdown_timeout_sec", 5.0)
                or 0.0
            ),
        )
        ok, result = self._run_step(
            "strategy_stop",
            partial(self._stop_strategy_callback, timeout_sec),
        )
        self.strategy_stopped = self._step_acknowledged(ok, result)
        self.phase["strategy_stopped"] = self.strategy_stopped

    def _establish_safety_barrier(self) -> bool:
        if not self._verify_parent_flatten():
            self._publish_shutdown_blocked(
                "kill/flatten did not reach FLAT_VERIFIED before "
                "shutdown latches"
            )
            self.logger.critical(
                "[Shutdown] OMS and Gateway shutdown latches were deferred "
                "so emergency cancellation and reduce-only flattening remain "
                "retryable"
            )
            return False
        self._latch_shutdown_paths()
        self._capture_pre_quiesce_truth()
        self._quiesce_sidecar_if_ready()
        self._verify_post_quiesce_truth()
        self._stop_sidecar_if_ready()
        self.shutdown_barrier_verified = bool(
            self.outbound_gate_closed
            and self.strategy_stopped
            and self.shutdown_latched
            and self.gateway_shutdown_latched
            and self.cancel_verified
            and self.flatten_verified
            and self.supervisor_quiesced
            and self.supervisor_stopped
        )
        self.phase["barrier_verified"] = self.shutdown_barrier_verified
        if self.shutdown_barrier_verified:
            return True
        self._publish_shutdown_blocked(
            "final account or sidecar shutdown proof failed"
        )
        self.logger.critical(
            "[Shutdown] Trading teardown remains retryable and dirty: "
            f"outbound_gate_closed={self.outbound_gate_closed} "
            f"strategy_stopped={self.strategy_stopped} "
            f"shutdown_latched={self.shutdown_latched} "
            f"gateway_shutdown_latched={self.gateway_shutdown_latched} "
            f"cancel_verified={self.cancel_verified} "
            f"flatten_verified={self.flatten_verified} "
            f"supervisor_quiesced={self.supervisor_quiesced} "
            f"supervisor_stopped={self.supervisor_stopped}"
        )
        return False

    def _verify_parent_flatten(self) -> bool:
        if not self.account_proof_required:
            self.kill_flatten_verified = True
            return True
        if self.risk_controller is None:
            self.kill_flatten_verified = False
            return False
        if not bool(
            getattr(self.risk_controller, "kill_switch_triggered", False)
        ):
            self._run_step(
                "risk_shutdown_flatten",
                partial(
                    self.risk_controller.trigger_kill_switch,
                    f"ProcessShutdown: {self.reason}",
                ),
            )
        elif str(
            getattr(self.risk_controller, "kill_state", "") or ""
        ).upper() == "FAILED":
            resume = getattr(
                self.risk_controller,
                "resume_kill_switch_supervision",
                None,
            )
            if callable(resume):
                self._run_step("risk_resume_shutdown_kill", resume)
        self.kill_flatten_verified = bool(
            getattr(self.risk_controller, "kill_switch_triggered", False)
            and self.services.wait_for_kill_flatten_verification(
                self.risk_controller,
                self.risk_supervisor,
            )
            and getattr(self.risk_controller, "kill_switch_triggered", False)
            and str(
                getattr(self.risk_controller, "kill_state", "") or ""
            ).upper()
            == "FLAT_VERIFIED"
        )
        return self.kill_flatten_verified

    def _latch_shutdown_paths(self) -> None:
        if not self.shutdown_latched:
            ok, result = self._run_step(
                "oms_begin_shutdown",
                partial(self.oms.begin_shutdown, self.reason),
            )
            self.shutdown_latched = self._step_acknowledged(ok, result)
            self.phase["shutdown_latched"] = self.shutdown_latched
        if self.shutdown_latched and not self.gateway_shutdown_latched:
            begin = getattr(self.gateway, "begin_shutdown", None)
            if callable(begin):
                ok, result = self._run_step(
                    "gateway_begin_shutdown",
                    begin,
                )
                self.gateway_shutdown_latched = self._step_acknowledged(
                    ok,
                    result,
                )
            else:
                self.gateway_shutdown_latched = self.gateway is None
            self.phase["gateway_shutdown_latched"] = (
                self.gateway_shutdown_latched
            )

    def _capture_pre_quiesce_truth(self) -> None:
        ready = bool(self.shutdown_latched and self.gateway_shutdown_latched)
        self.cancel_verified = bool(
            ready
            and self._verify_account_orders("process_shutdown_pre_quiesce")
        )
        self.independently_flat = bool(
            ready
            and self._verify_account_positions(
                "process_shutdown_pre_quiesce"
            )
        )
        self.flatten_verified = bool(
            self.kill_flatten_verified and self.independently_flat
        )
        if ready and not (self.cancel_verified and self.flatten_verified):
            self.kill_flatten_verified = self._restart_parent_kill_after_drift(
                "Pre-quiesce account truth drift",
                "risk_restart_after_pre_quiesce_drift",
            )
            self.cancel_verified = False
            self.independently_flat = False
            self.flatten_verified = False

    def _quiesce_sidecar_if_ready(self) -> None:
        if not self.supervisor_started or self.supervisor_quiesced:
            return
        ready = bool(
            self.outbound_gate_closed
            and self.gateway_shutdown_latched
            and self.strategy_stopped
            and self.shutdown_latched
            and self.cancel_verified
            and self.flatten_verified
        )
        if not ready:
            return
        quiesce = getattr(self.risk_supervisor, "quiesce", None)
        if not callable(quiesce):
            return
        ok, result = self._run_step(
            "risk_supervisor_quiesce",
            partial(quiesce, reason=f"ProcessShutdown: {self.reason}"),
        )
        self.supervisor_quiesced = bool(
            ok and self._quiesce_acknowledged(result)
        )
        self.phase["supervisor_quiesced"] = self.supervisor_quiesced

    def _verify_post_quiesce_truth(self) -> None:
        if not self.supervisor_started or not self.supervisor_quiesced:
            return
        self.cancel_verified = self._verify_account_orders(
            "process_shutdown_post_quiesce"
        )
        self.independently_flat = self._verify_account_positions(
            "process_shutdown_post_quiesce"
        )
        self.flatten_verified = bool(
            self.kill_flatten_verified and self.independently_flat
        )
        if self.cancel_verified and self.flatten_verified:
            return
        self._resume_sidecar_shutdown_guard()
        self.kill_flatten_verified = self._restart_parent_kill_after_drift(
            "Post-quiesce account truth drift",
            "risk_restart_after_post_quiesce_drift",
        )
        self.cancel_verified = False
        self.independently_flat = False
        self.flatten_verified = False

    def _resume_sidecar_shutdown_guard(self) -> None:
        resume = getattr(
            self.risk_supervisor,
            "resume_shutdown_guard",
            None,
        )
        if callable(resume):
            ok, result = self._run_step(
                "risk_supervisor_resume_shutdown_guard",
                partial(
                    resume,
                    reason=(
                        "Post-quiesce account truth drift: "
                        f"{self.reason}"
                    ),
                ),
            )
        else:
            ok, result = False, None
        resumed = bool(ok and self._shutdown_resume_acknowledged(result))
        if resumed:
            self.supervisor_quiesced = False
            self.supervisor_stopped = False
            self.phase["supervisor_quiesced"] = False
            self.phase["supervisor_stopped"] = False
            return
        self.logger.critical(
            "[Shutdown] Post-quiesce account truth failed and the "
            "independent shutdown guard could not be resumed"
        )

    def _stop_sidecar_if_ready(self) -> None:
        if not self.supervisor_started or self.supervisor_stopped:
            return
        ready = bool(
            self.outbound_gate_closed
            and self.strategy_stopped
            and self.shutdown_latched
            and self.gateway_shutdown_latched
            and self.cancel_verified
            and self.flatten_verified
            and self.supervisor_quiesced
        )
        if ready:
            ok, result = self._run_step(
                "risk_supervisor_stop",
                partial(self.risk_supervisor.stop, cancel_orders=False),
            )
            self.supervisor_stopped = bool(
                ok and self._stop_acknowledged(result)
            )
            self.phase["supervisor_stopped"] = self.supervisor_stopped
        if not self.supervisor_stopped:
            self.logger.critical(
                "[Shutdown] IndependentRiskSupervisor remains active or "
                "unclean because its quiesce/stop barrier was not fully "
                "acknowledged"
            )

    def _verify_account_orders(self, source: str) -> bool:
        if not self.account_proof_required:
            if self.oms is None:
                return True
            if not self.shutdown_latched:
                return False
            verify = getattr(
                self.oms,
                "verify_preconnect_shutdown_no_order_path",
                None,
            )
            if not callable(verify):
                return False
            ok, result = self._run_step(
                f"verified_preconnect_shutdown_{source}",
                partial(verify, source=source),
            )
            return bool(ok and result and self.shutdown_latched)
        if (
            not self.shutdown_latched
            or self.oms is None
            or self.truth_provider is None
        ):
            return False
        verify = partial(
            self.oms.cancel_all_account_orders_verified,
            self.truth_provider,
            source=source,
        )
        ok, result = self._run_step(
            f"verified_account_cancel_{source}",
            partial(
                self.services.call_with_risk_heartbeat,
                verify,
                self.risk_supervisor,
            ),
        )
        return bool(ok and result and self.shutdown_latched)

    def _verify_account_positions(self, source: str) -> bool:
        if not self.account_proof_required:
            return True
        if not self.live_canary_truth_required:
            return bool(self.kill_flatten_verified)
        if self.truth_provider is None:
            return False
        verify = partial(
            self.services.verify_account_flat,
            self.truth_provider,
            required_flat_snapshots=getattr(
                self.oms,
                "shutdown_empty_snapshots_required",
                2,
            ),
            settle_interval_sec=getattr(
                self.oms,
                "shutdown_cancel_settle_interval_sec",
                0.25,
            ),
        )
        ok, result = self._run_step(
            f"verified_account_flat_{source}",
            partial(
                self.services.call_with_risk_heartbeat,
                verify,
                self.risk_supervisor,
            ),
        )
        return bool(ok and result)

    def _restart_parent_kill_after_drift(
        self,
        drift_reason: str,
        step_name: str,
    ) -> bool:
        restart = getattr(
            self.risk_controller,
            "restart_kill_switch_after_truth_drift",
            None,
        )
        if not callable(restart):
            return False
        ok, result = self._run_step(
            step_name,
            partial(restart, drift_reason),
        )
        return bool(
            ok
            and result is not False
            and self.services.wait_for_kill_flatten_verification(
                self.risk_controller,
                self.risk_supervisor,
            )
        )

    def _stop_observers_and_transport(self) -> bool:
        if not self._close_admin_control():
            return False
        if not self._stop_truth_observers():
            return False
        if not self._close_gateway():
            return False
        if not self._drain_event_engine():
            return False
        return self._close_recorders()

    def _close_admin_control(self) -> bool:
        if not self.admin_control_closed:
            ok, result = self._run_step(
                "admin_control_close",
                self.admin_control.close,
            )
            self.admin_control_closed = self._step_acknowledged(ok, result)
            self.phase["admin_control_closed"] = self.admin_control_closed
        if self.admin_control_closed:
            return True
        self._publish_shutdown_blocked(
            "admin control session withdrawal failed"
        )
        return False

    def _stop_truth_observers(self) -> bool:
        if not self.venue_supervisor_stopped:
            ok, result = self._run_step(
                "venue_supervisor_stop",
                self.venue_supervisor.stop,
            )
            self.venue_supervisor_stopped = self._step_acknowledged(ok, result)
            self.phase["venue_supervisor_stopped"] = (
                self.venue_supervisor_stopped
            )
        if not self.truth_monitor_stopped:
            ok, result = self._run_step(
                "truth_monitor_stop",
                self.truth_monitor.stop,
            )
            self.truth_monitor_stopped = self._step_acknowledged(ok, result)
            self.phase["truth_monitor_stopped"] = self.truth_monitor_stopped
        if self.venue_supervisor_stopped and self.truth_monitor_stopped:
            return True
        self._publish_shutdown_blocked(
            "truth or venue monitor did not stop cleanly"
        )
        return False

    def _close_gateway(self) -> bool:
        if not self.gateway_closed:
            ok, result = self._run_step("gateway_close", self.gateway.close)
            self.gateway_closed = self._step_acknowledged(ok, result)
            self.phase["gateway_closed"] = self.gateway_closed
        if self.gateway_closed:
            return True
        self._publish_shutdown_blocked("gateway close failed")
        return False

    def _drain_event_engine(self) -> bool:
        if not self.event_drained:
            timeout_sec = max(
                0.0,
                float(
                    self.event_engine_config.get(
                        "shutdown_drain_timeout_sec",
                        5.0,
                    )
                ),
            )
            ok, result = self._run_step(
                "event_engine_drain",
                partial(self.engine.wait_until_idle, timeout_sec),
            )
            self.event_drained = bool(ok and result)
            self.phase["event_drained"] = self.event_drained
            if not self.event_drained:
                snapshot_ok, snapshot = self._run_step(
                    "event_engine_queue_snapshot",
                    self.engine.get_queue_snapshot,
                )
                self.logger.warning(
                    "[Shutdown] EventEngine drain timed out: "
                    f"{snapshot if snapshot_ok else 'unavailable'}"
                )
        if self.event_drained:
            return True
        self._publish_shutdown_blocked("event engine drain timed out")
        return False

    def _close_recorders(self) -> bool:
        if not self.recorder_closed:
            ok, result = self._run_step("recorder_close", self.recorder.close)
            self.recorder_closed = self._step_acknowledged(ok, result)
            self.phase["recorder_closed"] = self.recorder_closed
        if not self.truth_provider_closed:
            ok, result = self._run_step(
                "truth_provider_close",
                self.truth_provider.close,
            )
            self.truth_provider_closed = self._step_acknowledged(ok, result)
            self.phase["truth_provider_closed"] = self.truth_provider_closed
        if self.recorder_closed and self.truth_provider_closed:
            return True
        self._publish_shutdown_blocked(
            "recorder or truth provider close failed"
        )
        return False

    def _stop_durable_core(self) -> bool:
        clean_shutdown = bool(
            self.shutdown_barrier_verified
            and self.gateway_closed
            and self.event_drained
        )
        if not (self.oms_stopped and self.oms_clean):
            ok, result = self._run_step(
                "oms_stop",
                partial(
                    self.oms.stop,
                    clean_shutdown=clean_shutdown,
                    reason=self.reason,
                ),
            )
            if isinstance(result, dict):
                self.oms_stopped = bool(
                    self.oms_stopped
                    or (ok and result.get("stopped") is True)
                )
                self.oms_clean = bool(
                    self.oms_clean or (ok and result.get("clean") is True)
                )
            else:
                stopped = self._step_acknowledged(ok, result)
                self.oms_stopped = bool(self.oms_stopped or stopped)
                self.oms_clean = bool(
                    self.oms_clean or (stopped and clean_shutdown)
                )
            self.phase["oms_stopped"] = self.oms_stopped
            self.phase["oms_clean"] = self.oms_clean
        if not (self.oms_stopped and self.oms_clean):
            self._publish_shutdown_blocked(
                "OMS stop was incomplete or durably unclean"
            )
            return False
        if not self.engine_stopped:
            ok, result = self._run_step("event_engine_stop", self.engine.stop)
            self.engine_stopped = self._step_acknowledged(ok, result)
            self.phase["engine_stopped"] = self.engine_stopped
        if not self.engine_stopped:
            self._publish_shutdown_blocked("event engine stop failed")
            return False
        if not self._stop_dashboard():
            return False
        if not self.clock_stopped:
            ok, result = self._run_step(
                "time_service_stop",
                self.clock_service.stop,
            )
            self.clock_stopped = self._step_acknowledged(ok, result)
            self.phase["clock_stopped"] = self.clock_stopped
        return True

    def _stop_dashboard(self) -> bool:
        if not self.dashboard_stopped:
            self._run_step(
                "dashboard_publish_final",
                partial(self.web_dashboard.publish_snapshot, force=True),
            )
            ok, result = self._run_step(
                "dashboard_stop",
                self.web_dashboard.stop,
            )
            self.dashboard_stopped = self._step_acknowledged(ok, result)
            self.phase["dashboard_stopped"] = self.dashboard_stopped
        if self.dashboard_stopped:
            return True
        self._publish_shutdown_blocked("dashboard stop failed")
        return False

    def _finalize(self) -> bool:
        terminal = bool(
            self.strategy_stopped
            and self.venue_supervisor_stopped
            and self.truth_monitor_stopped
            and self.gateway_closed
            and self.event_drained
            and self.truth_provider_closed
            and self.recorder_closed
            and self.admin_control_closed
            and self.oms_stopped
            and self.oms_clean
            and self.engine_stopped
            and self.dashboard_stopped
            and self.clock_stopped
            and self.supervisor_stopped
        )
        verified = bool(self.oms_clean and terminal)
        self.runtime["_shutdown_verified"] = verified
        self.runtime["_shutdown_retryable"] = not terminal
        self.runtime["_shutdown_complete"] = terminal
        self.logger.info(
            "ChronosHFT Shutdown Complete. "
            f"verified_cancel={self.cancel_verified} "
            f"flatten_verified={self.flatten_verified} "
            f"supervisor_quiesced={self.supervisor_quiesced} "
            f"supervisor_stopped={self.supervisor_stopped} "
            f"event_drained={self.event_drained} "
            f"oms_clean={self.oms_clean} terminal={terminal}"
        )
        return verified
