import queue


class SidecarTransport:
    """Parent-side process and queue transport for the risk sidecar."""

    @staticmethod
    def start_process(
        owner,
        multiprocessing_module,
        process_target,
        perf_counter,
    ) -> None:
        owner.context = multiprocessing_module.get_context("spawn")
        owner.command_queue = owner.context.Queue(maxsize=32)
        owner.heartbeat_queue = owner.context.Queue(maxsize=1)
        owner.status_queue = owner.context.Queue(maxsize=8)
        owner.process = owner.context.Process(
            target=process_target,
            args=(
                owner.command_queue,
                owner.status_queue,
                owner.settings,
                owner.heartbeat_queue,
            ),
            name="ChronosRiskSupervisor",
            daemon=False,
        )
        owner.process.start()
        owner.started_at = perf_counter()

    @staticmethod
    def drain_status(owner, now: float) -> None:
        while True:
            try:
                status = owner.status_queue.get_nowait()
            except queue.Empty:
                break
            except (OSError, ValueError) as exc:
                owner.last_status = {}
                owner.last_status_received_at = 0.0
                owner.last_status_protocol_error = (
                    f"status_queue_error:{type(exc).__name__}:{exc}"
                )
                break
            if not isinstance(status, dict):
                owner.last_status = {}
                owner.last_status_received_at = 0.0
                owner.last_status_protocol_error = "status_not_object"
                continue
            if str(status.get("session_id", "") or "") != owner.session_id:
                continue
            try:
                validated = owner._validate_status_payload(status)
            except (TypeError, ValueError, OverflowError) as exc:
                owner.last_status = {}
                owner.last_status_received_at = 0.0
                owner.last_status_protocol_error = str(exc)
                continue
            if validated["sequence"] <= int(
                owner.last_status.get("sequence", 0) or 0
            ):
                continue
            owner.last_status = validated
            owner.last_status_received_at = now
            owner.last_status_protocol_error = ""

    @staticmethod
    def request_control(
        owner,
        command_type: str,
        timeout_sec,
        payload: dict,
        reliable_put,
        request_id_factory,
        perf_counter,
        sleep,
    ) -> dict:
        command_type = str(command_type or "").upper()
        if not owner.enabled:
            result = owner._control_failure_result(
                command_type,
                "supervisor_disabled",
                **payload,
            )
            if command_type == "QUIESCE":
                result.update(
                    {
                        "accepted": True,
                        "quiesced": True,
                        "persisted": True,
                    }
                )
            elif command_type == "RESUME_SHUTDOWN":
                result.update(
                    {
                        "accepted": True,
                        "quiesced": False,
                        "kill_latched": True,
                        "persisted": True,
                    }
                )
            elif command_type != "STOP":
                result["accepted"] = True
            return result
        if owner.process is None or not owner.process.is_alive():
            return owner._control_failure_result(
                command_type,
                "supervisor_process_down",
                **payload,
            )
        request_id = request_id_factory(16)
        enqueued = reliable_put(
            owner.command_queue,
            {
                "type": command_type,
                "session_id": owner.session_id,
                "request_id": request_id,
                **payload,
            },
            owner.control_enqueue_timeout_sec,
        )
        if not enqueued:
            return owner._control_failure_result(
                command_type,
                "supervisor_control_queue_full",
                request_id,
                **payload,
            )
        timeout = (
            owner.rearm_command_timeout_sec
            if timeout_sec is None
            else max(0.0, float(timeout_sec or 0.0))
        )
        deadline = perf_counter() + timeout
        process_down_at = 0.0
        while perf_counter() <= deadline:
            now = perf_counter()
            process_alive = owner.process.is_alive()
            if process_alive:
                owner._send_heartbeat(now)
            owner._drain_status(now)
            ack = owner._read_control_ack(command_type, request_id)
            if ack is not None:
                return ack
            if not process_alive:
                if process_down_at <= 0.0:
                    process_down_at = now
                if command_type != "STOP" or now - process_down_at >= 0.5:
                    return owner._control_failure_result(
                        command_type,
                        "supervisor_process_down_before_ack",
                        request_id,
                        **payload,
                    )
            sleep(0.02)
        return owner._control_failure_result(
            command_type,
            f"supervisor_{command_type.lower()}_timeout",
            request_id,
            **payload,
        )

    @staticmethod
    def enqueue_abort(owner, token: str, reliable_put) -> bool:
        if not owner.enabled:
            return True
        if owner.process is None or not owner.process.is_alive():
            return False
        return reliable_put(
            owner.command_queue,
            {
                "type": "ABORT_REARM",
                "session_id": owner.session_id,
                "token": str(token or ""),
            },
            owner.control_enqueue_timeout_sec,
        )

    @staticmethod
    def close_channels(owner) -> None:
        for channel in (
            owner.command_queue,
            owner.heartbeat_queue,
            owner.status_queue,
        ):
            close = getattr(channel, "close", None)
            if callable(close):
                close()
