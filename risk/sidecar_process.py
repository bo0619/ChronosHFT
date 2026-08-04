"""Child-process isolation, exchange initialization, and runtime handoff."""

from risk.sidecar_protocol import SidecarProtocol


class SidecarProcessBootstrap:
    @staticmethod
    def run(
        command_queue,
        status_queue,
        settings: dict,
        heartbeat_queue,
        *,
        isolate_console_interrupts,
        exchange_factory,
        run_loop,
        put_latest,
        getpid,
        wall_time,
    ) -> None:
        exchange = None
        snapshot_exchange = None
        handshake_complete = False
        session_id = (
            str(settings.get("session_id", "") or "")
            if isinstance(settings, dict)
            else ""
        )
        try:
            SidecarProtocol.validate_launch_contract(settings)
            handshake_complete = True
            isolate_console_interrupts()
            api_key = str(settings.get("api_key", "") or "")
            api_secret = str(settings.get("api_secret", "") or "")
            if not api_key or not api_secret:
                raise ValueError(
                    "dedicated sidecar API credentials are required"
                )
            testnet = bool(settings.get("testnet", False))
            exchange = exchange_factory(
                api_key,
                api_secret,
                testnet,
                settings=settings,
            )
            snapshot_exchange = exchange_factory(
                api_key,
                api_secret,
                testnet,
                settings=settings,
            )
        except Exception as exc:
            close = getattr(exchange, "close", None)
            if callable(close):
                close()
            put_latest(
                status_queue,
                SidecarProtocol.child_status(
                    {
                        "session_id": session_id,
                        "sequence": 1,
                        "pid": getpid(),
                        "reported_at": wall_time(),
                        "healthy": False,
                        "reason": (
                            f"sidecar_init_failed:{type(exc).__name__}:{exc}"
                        ),
                    },
                    handshake_complete=handshake_complete,
                ),
            )
            return
        run_loop(
            command_queue,
            status_queue,
            settings,
            exchange,
            snapshot_exchange=snapshot_exchange,
            heartbeat_queue=heartbeat_queue,
        )
