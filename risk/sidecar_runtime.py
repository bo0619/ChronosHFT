"""Child-process command dispatch and status publication loop."""

import queue

from risk.sidecar_protocol import SidecarProtocol


class SidecarRuntime:
    @staticmethod
    def dispatch_command(core, command: dict, session_id: str) -> bool:
        command = SidecarProtocol.validate_control_command(command, session_id)
        if command is None:
            return False
        command_type = command["type"]
        if command_type == "HEARTBEAT":
            core.receive_parent_heartbeat(
                command.get("sequence", 0),
                sent_monotonic=command.get("sent_monotonic"),
            )
        elif command_type == "QUIESCE":
            core.request_quiesce(
                command.get("request_id", ""),
                command.get("reason", ""),
            )
        elif command_type == "RESUME_SHUTDOWN":
            core.request_shutdown_resume(
                command.get("request_id", ""),
                command.get("reason", ""),
            )
        elif command_type == "STOP":
            core.request_stop(
                command.get("request_id", ""),
                command.get("cancel_orders", True),
            )
        elif command_type == "PREPARE_REARM":
            core.prepare_rearm(
                command.get("request_id", ""),
                command.get("reason", ""),
            )
        elif command_type == "COMMIT_REARM":
            core.commit_rearm(
                command.get("request_id", ""),
                command.get("token", ""),
            )
        elif command_type == "ABORT_REARM":
            core.abort_rearm(command.get("token", ""))
        else:
            return False
        return True

    @classmethod
    def drain_commands(
        cls,
        command_queue,
        core,
        session_id: str,
    ) -> None:
        while True:
            try:
                command = command_queue.get_nowait()
            except queue.Empty:
                break
            cls.dispatch_command(core, command, session_id)

    @staticmethod
    def drain_latest_heartbeat(
        heartbeat_queue,
        core,
        session_id: str,
    ) -> None:
        if heartbeat_queue is None:
            return
        latest_heartbeat = None
        while True:
            try:
                candidate = heartbeat_queue.get_nowait()
            except queue.Empty:
                break
            validated = SidecarProtocol.validate_heartbeat(
                candidate,
                session_id,
            )
            if validated is not None:
                latest_heartbeat = validated
        if latest_heartbeat is not None:
            core.receive_parent_heartbeat(
                latest_heartbeat.get("sequence", 0),
                sent_monotonic=latest_heartbeat.get("sent_monotonic"),
            )

    @staticmethod
    def status_signature(status: dict) -> tuple:
        return (
            status["healthy"],
            status["reason"],
            status["risk_action"],
            status["funding_action"],
            status["funding_reason"],
            status["stage"],
            status["exchange_healthy"],
            status["last_cancel_ok"],
            status["last_cancel_reason"],
            status["last_flatten_ok"],
            status["last_flatten_count"],
            status["last_flatten_reason"],
            status["flat_verification_count"],
            status["risk_snapshot_sequence"],
            status["risk_snapshot_captured_monotonic"],
            status["parent_heartbeat_error"],
            status["state_generation"],
            status.get("writer_epoch", 0),
            status.get("owner_epoch", 0),
            status.get("safety_epoch", 0),
            status.get("state_sha256", ""),
            status.get("last_flat_proof"),
            status.get("last_flat_proof_error", ""),
            status["state_load_error"],
            status["state_persist_error"],
            status["last_rearm_request_id"],
            status["last_rearm_phase"],
            status["last_rearm_accepted"],
            status["last_rearm_reason"],
            status["quiesced"],
            status["last_quiesce_request_id"],
            status["last_quiesce_accepted"],
            status["last_quiesce_reason"],
            status["last_shutdown_resume_request_id"],
            status["last_shutdown_resume_accepted"],
            status["last_shutdown_resume_reason"],
            status["last_stop_request_id"],
            status["last_stop_accepted"],
            status["last_stop_reason"],
            status["last_stop_cancel_attempted"],
            status["last_stop_cancel_ok"],
        )

    @classmethod
    def run(
        cls,
        command_queue,
        status_queue,
        settings: dict,
        exchange,
        *,
        snapshot_exchange,
        heartbeat_queue,
        snapshot_worker_factory,
        core_factory,
        isolated_exchange_type,
        put_latest,
        perf_counter,
        wall_time,
        getpid,
        sleep,
    ) -> None:
        SidecarProtocol.validate_launch_contract(settings)
        session_id = str(settings.get("session_id", "") or "")
        status_interval_sec = max(
            0.05,
            float(settings.get("status_interval_sec", 0.25) or 0.25),
        )
        loop_interval_sec = min(0.05, status_interval_sec)
        snapshot_exchange = (
            exchange if snapshot_exchange is None else snapshot_exchange
        )
        if (
            snapshot_exchange is exchange
            and isinstance(exchange, isolated_exchange_type)
        ):
            raise ValueError(
                "risk sidecar requires an isolated snapshot exchange client"
            )
        snapshot_worker = snapshot_worker_factory(snapshot_exchange)
        snapshot_worker.start()
        status_sequence = 0
        last_status_at = 0.0
        last_status_signature = None
        core = None

        try:
            core = core_factory(
                exchange,
                settings,
                snapshot_worker=snapshot_worker,
            )
            while True:
                cls.drain_commands(command_queue, core, session_id)
                cls.drain_latest_heartbeat(
                    heartbeat_queue,
                    core,
                    session_id,
                )

                now = perf_counter()
                status, keep_running = core.step(now)
                signature = cls.status_signature(status)
                if (
                    signature != last_status_signature
                    or now - last_status_at >= status_interval_sec
                    or not keep_running
                ):
                    status_sequence += 1
                    put_latest(
                        status_queue,
                        SidecarProtocol.child_status(
                            {
                                **status,
                                "session_id": session_id,
                                "sequence": status_sequence,
                                "pid": getpid(),
                                "reported_at": wall_time(),
                            },
                            handshake_complete=True,
                        ),
                    )
                    last_status_at = now
                    last_status_signature = signature
                if not keep_running:
                    break
                sleep(loop_interval_sec)
        finally:
            snapshot_stopped = snapshot_worker.stop()
            close_core = getattr(core, "close", None)
            if callable(close_core):
                close_core()
            if snapshot_exchange is not exchange:
                close = getattr(exchange, "close", None)
                if callable(close):
                    close()
            if snapshot_stopped:
                close = getattr(snapshot_exchange, "close", None)
                if callable(close):
                    close()
