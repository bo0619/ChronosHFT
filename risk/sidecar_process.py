"""Child-process isolation, exchange initialization, and runtime handoff."""


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
        try:
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
                {
                    "session_id": settings.get("session_id", ""),
                    "sequence": 1,
                    "pid": getpid(),
                    "reported_at": wall_time(),
                    "healthy": False,
                    "reason": (
                        f"sidecar_init_failed:{type(exc).__name__}:{exc}"
                    ),
                },
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
