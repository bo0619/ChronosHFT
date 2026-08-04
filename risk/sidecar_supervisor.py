"""Parent-side lifecycle for the independent risk sidecar process."""

import math
import multiprocessing
import queue
import secrets
import time

from risk.sidecar_health import SidecarOmsHealth
from risk.sidecar_protocol import SidecarProtocol
from risk.sidecar_settings import SidecarSupervisorConfiguration
from risk.sidecar_status import SidecarStatusProjection
from risk.sidecar_transport import SidecarTransport
from risk.sidecar_values import finite_float as _finite_float


SUPERVISOR_SOURCE = "independent_supervisor"


def _put_latest(target_queue, payload):
    return SidecarTransport.put_latest(target_queue, payload)


def _put_reliable(target_queue, payload, timeout_sec: float) -> bool:
    return SidecarTransport.put_reliable(
        target_queue,
        payload,
        timeout_sec,
    )


class SidecarParentSupervisor:
    """Parent-side controller for the independent risk supervision process."""

    def __init__(
        self,
        oms,
        config: dict,
        risk_manager=None,
        *,
        process_target,
    ):
        self._process_target = process_target
        self.oms = oms
        self.risk_manager = risk_manager
        configuration = SidecarSupervisorConfiguration.from_root(
            config,
            risk_manager,
            _finite_float,
            SUPERVISOR_SOURCE,
        )
        self.config = configuration.supervisor_config
        self.enabled = configuration.enabled
        self.heartbeat_interval_sec = configuration.heartbeat_interval_sec
        self.status_max_age_sec = configuration.status_max_age_sec
        self.stop_timeout_sec = configuration.stop_timeout_sec
        self.control_enqueue_timeout_sec = (
            configuration.control_enqueue_timeout_sec
        )
        self.recovery_checks = configuration.recovery_checks
        self.recovery_snapshot_max_age_sec = (
            configuration.recovery_snapshot_max_age_sec
        )
        self.rearm_command_timeout_sec = (
            configuration.rearm_command_timeout_sec
        )
        self.settings = SidecarProtocol.with_launch_contract(
            configuration.child_settings
        )
        self.session_id = secrets.token_hex(16)
        self.settings["session_id"] = self.session_id
        self.context = None
        self.command_queue = None
        self.heartbeat_queue = None
        self.status_queue = None
        self.process = None
        self.heartbeat_sequence = 0
        self.last_heartbeat_sent_at = 0.0
        self.parent_heartbeat_suspended_reason = ""
        self.last_status = {}
        self.last_status_received_at = 0.0
        self.last_status_protocol_error = ""
        self.started_at = 0.0
        self.recovery_count = 0
        self.last_recovery_snapshot_sequence = 0

    def start(self) -> bool:
        if not self.enabled:
            return True
        if self.process is not None and self.process.is_alive():
            return True
        self.last_status = {}
        self.last_status_received_at = 0.0
        self.last_status_protocol_error = ""
        self.last_recovery_snapshot_sequence = 0
        self.recovery_count = 0
        self.heartbeat_sequence = 0
        self.last_heartbeat_sent_at = 0.0
        SidecarTransport.start_process(
            self,
            multiprocessing,
            self._process_target,
            time.perf_counter,
        )
        self._send_heartbeat(self.started_at)
        self._apply_oms_health(False, "supervisor_starting")
        return True

    def pulse_parent_heartbeat(self) -> bool:
        """Emit only the liveness pulse, without applying parent-side risk state."""
        if not self.enabled:
            return True
        if self.parent_heartbeat_suspended_reason:
            return False
        process = self.process
        if process is None or not process.is_alive():
            return False
        self._send_heartbeat(time.perf_counter())
        return True

    def _send_heartbeat(self, now: float):
        if self.parent_heartbeat_suspended_reason:
            return
        if now - self.last_heartbeat_sent_at < self.heartbeat_interval_sec:
            return
        self.heartbeat_sequence += 1
        payload = SidecarProtocol.parent_message(
            "HEARTBEAT",
            self.session_id,
            sequence=self.heartbeat_sequence,
            sent_monotonic=now,
        )
        if self.heartbeat_queue is not None:
            delivered = _put_latest(self.heartbeat_queue, payload)
        else:
            try:
                self.command_queue.put_nowait(payload)
                delivered = True
            except (OSError, ValueError, queue.Full):
                delivered = False
        if delivered:
            self.last_heartbeat_sent_at = now

    @staticmethod
    def _validate_status_payload(status: dict) -> dict:
        return SidecarProtocol.validate_status(status, _finite_float)

    def _drain_status(self, now: float):
        SidecarTransport.drain_status(self, now)

    def _record_oms_heartbeat(self, healthy: bool, reason: str):
        return SidecarOmsHealth.record_heartbeat(
            self,
            healthy,
            reason,
            SUPERVISOR_SOURCE,
        )

    def _reset_recovery_progress(self):
        SidecarOmsHealth.reset_recovery_progress(self)

    def _apply_oms_health(self, healthy: bool, reason: str) -> bool:
        return SidecarOmsHealth.apply(
            self,
            healthy,
            reason,
            time.perf_counter,
            math.isfinite,
        )

    def tick(self) -> bool:
        return SidecarOmsHealth.tick(self, time.perf_counter)

    def wait_until_healthy(self, timeout_sec: float = 10.0) -> bool:
        return SidecarOmsHealth.wait_until_healthy(
            self,
            timeout_sec,
            time.perf_counter,
            time.sleep,
        )

    def _control_failure_result(
        self,
        command_type: str,
        failure_reason: str,
        request_id: str = "",
        **payload,
    ) -> dict:
        return SidecarProtocol.control_failure(
            command_type,
            self.last_status,
            failure_reason,
            request_id,
            **payload,
        )

    def _read_control_ack(
        self,
        command_type: str,
        request_id: str,
    ):
        return SidecarProtocol.read_control_ack(
            command_type,
            request_id,
            self.last_status,
        )

    def _request_sidecar_control(
        self,
        command_type: str,
        timeout_sec: float = None,
        **payload,
    ) -> dict:
        return SidecarTransport.request_control(
            self,
            command_type,
            timeout_sec,
            payload,
            _put_reliable,
            secrets.token_hex,
            time.perf_counter,
            time.sleep,
        )

    def prepare_rearm(self, reason: str, timeout_sec: float = None) -> dict:
        return self._request_sidecar_control(
            "PREPARE_REARM",
            timeout_sec=timeout_sec,
            reason=str(reason or "operator_rearm"),
        )

    def commit_rearm(self, token: str, timeout_sec: float = None) -> dict:
        return self._request_sidecar_control(
            "COMMIT_REARM",
            timeout_sec=timeout_sec,
            token=str(token or ""),
        )

    def quiesce(
        self,
        reason: str = "parent_quiesce",
        timeout_sec: float = None,
    ) -> dict:
        result = self._request_sidecar_control(
            "QUIESCE",
            timeout_sec=timeout_sec,
            reason=str(reason or "parent_quiesce"),
        )
        if self.enabled and bool(result.get("quiesced", False)):
            self._apply_oms_health(False, "supervisor_quiesced")
        return result

    def resume_shutdown_guard(
        self,
        reason: str = "shutdown account truth drift",
        timeout_sec: float = None,
    ) -> dict:
        result = self._request_sidecar_control(
            "RESUME_SHUTDOWN",
            timeout_sec=timeout_sec,
            reason=str(reason or "shutdown account truth drift"),
        )
        if self.enabled and bool(result.get("accepted", False)):
            self._apply_oms_health(
                False,
                "supervisor_shutdown_guard_active",
            )
        if self.enabled and not (
            result.get("accepted") is True
            and result.get("kill_latched") is True
            and result.get("persisted") is True
        ):
            self.suspend_parent_heartbeat(
                "shutdown_guard_handoff_unconfirmed:"
                f"{result.get('reason', 'unknown')}"
            )
        return result

    def suspend_parent_heartbeat(self, reason: str) -> bool:
        """Force the sidecar stale-parent kill path after control-plane loss."""
        if not self.enabled:
            return False
        self.parent_heartbeat_suspended_reason = str(
            reason or "parent_requested_sidecar_takeover"
        )
        return True

    def abort_rearm(self, token: str) -> bool:
        return SidecarTransport.enqueue_abort(self, token, _put_reliable)

    def get_status_snapshot(self) -> dict:
        return SidecarStatusProjection.build(
            enabled=self.enabled,
            process=self.process,
            parent_heartbeat_suspended_reason=(
                self.parent_heartbeat_suspended_reason
            ),
            last_status=self.last_status,
            last_status_received_at=self.last_status_received_at,
            last_status_protocol_error=self.last_status_protocol_error,
            status_max_age_sec=self.status_max_age_sec,
            settings=self.settings,
            now=time.perf_counter(),
        )

    def stop(self, cancel_orders: bool = True) -> dict:
        if not self.enabled:
            return {
                "accepted": not bool(cancel_orders),
                "reason": (
                    "supervisor_disabled"
                    if not cancel_orders
                    else "supervisor_disabled_cancel_not_attempted"
                ),
                "request_id": "",
                "quiesced": True,
                "cancel_requested": bool(cancel_orders),
                "cancel_attempted": False,
                "cancel_ok": None,
                "process_exited": True,
                "forced_terminated": False,
            }
        process = self.process
        if process is None:
            return {
                "accepted": False,
                "reason": "supervisor_process_missing",
                "request_id": "",
                "quiesced": bool(
                    self.last_status.get("quiesced", False)
                ),
                "cancel_requested": bool(cancel_orders),
                "cancel_attempted": False,
                "cancel_ok": None,
                "process_exited": True,
                "forced_terminated": False,
            }

        if process.is_alive():
            result = self._request_sidecar_control(
                "STOP",
                timeout_sec=self.stop_timeout_sec,
                cancel_orders=bool(cancel_orders),
            )
        else:
            result = self._control_failure_result(
                "STOP",
                "supervisor_process_down_before_ack",
                cancel_orders=bool(cancel_orders),
            )

        forced_terminated = False
        if bool(result.get("accepted", False)) and bool(
            result.get("quiesced", False)
        ):
            process.join(min(1.0, self.stop_timeout_sec))
            if process.is_alive():
                process.terminate()
                process.join(2.0)
                forced_terminated = True

        process_exited = not process.is_alive()
        result = {
            **result,
            "process_exited": process_exited,
            "forced_terminated": forced_terminated,
        }
        if process_exited:
            self._apply_oms_health(False, "supervisor_stopped")
            SidecarTransport.close_channels(self)
        else:
            self._apply_oms_health(
                False,
                "supervisor_stop_not_acknowledged:"
                f"{result.get('reason', 'unknown')}",
            )
        return result

