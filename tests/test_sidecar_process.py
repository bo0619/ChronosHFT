from risk.sidecar_process import SidecarProcessBootstrap


class _Exchange:
    def __init__(self, name):
        self.name = name
        self.closed = False

    def close(self):
        self.closed = True


def test_bootstrap_creates_distinct_clients_and_hands_off_to_runtime():
    events = []
    clients = []
    loop_calls = []
    settings = {
        "api_key": "risk-key",
        "api_secret": "risk-secret",
        "testnet": True,
        "session_id": "session-1",
    }
    command_queue = object()
    status_queue = object()
    heartbeat_queue = object()

    def exchange_factory(api_key, api_secret, testnet, *, settings):
        events.append(("exchange", api_key, api_secret, testnet, settings))
        exchange = _Exchange(f"client-{len(clients) + 1}")
        clients.append(exchange)
        return exchange

    def run_loop(*args, **kwargs):
        events.append(("loop",))
        loop_calls.append((args, kwargs))

    SidecarProcessBootstrap.run(
        command_queue,
        status_queue,
        settings,
        heartbeat_queue,
        isolate_console_interrupts=lambda: events.append(("isolate",)),
        exchange_factory=exchange_factory,
        run_loop=run_loop,
        put_latest=lambda target, payload: (_ for _ in ()).throw(
            AssertionError("failure status must not be published")
        ),
        getpid=lambda: 1,
        wall_time=lambda: 2.0,
    )

    assert events[0] == ("isolate",)
    assert events[-1] == ("loop",)
    assert len(clients) == 2
    args, kwargs = loop_calls[0]
    assert args == (
        command_queue,
        status_queue,
        settings,
        clients[0],
    )
    assert kwargs == {
        "snapshot_exchange": clients[1],
        "heartbeat_queue": heartbeat_queue,
    }


def test_bootstrap_fails_closed_when_credentials_are_missing():
    published = []

    SidecarProcessBootstrap.run(
        object(),
        "status-queue",
        {"session_id": "session-1"},
        None,
        isolate_console_interrupts=lambda: None,
        exchange_factory=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("exchange must not be created")
        ),
        run_loop=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("runtime must not start")
        ),
        put_latest=lambda target, payload: (
            published.append((target, payload)) or True
        ),
        getpid=lambda: 4321,
        wall_time=lambda: 100.0,
    )

    assert published == [
        (
            "status-queue",
            {
                "session_id": "session-1",
                "sequence": 1,
                "pid": 4321,
                "reported_at": 100.0,
                "healthy": False,
                "reason": (
                    "sidecar_init_failed:ValueError:dedicated sidecar API "
                    "credentials are required"
                ),
            },
        )
    ]


def test_bootstrap_reports_console_isolation_failure_before_client_init():
    published = []

    SidecarProcessBootstrap.run(
        object(),
        object(),
        {
            "api_key": "risk-key",
            "api_secret": "risk-secret",
            "session_id": "session-2",
        },
        None,
        isolate_console_interrupts=lambda: (_ for _ in ()).throw(
            OSError("handler unavailable")
        ),
        exchange_factory=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("exchange must not be created")
        ),
        run_loop=lambda *args, **kwargs: None,
        put_latest=lambda target, payload: (
            published.append(payload) or True
        ),
        getpid=lambda: 10,
        wall_time=lambda: 20.0,
    )

    assert published[0]["reason"] == (
        "sidecar_init_failed:OSError:handler unavailable"
    )


def test_bootstrap_closes_first_client_if_second_client_init_fails():
    first = _Exchange("first")
    calls = 0
    published = []

    def exchange_factory(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return first
        raise RuntimeError("snapshot client failed")

    SidecarProcessBootstrap.run(
        object(),
        object(),
        {"api_key": "key", "api_secret": "secret"},
        None,
        isolate_console_interrupts=lambda: None,
        exchange_factory=exchange_factory,
        run_loop=lambda *args, **kwargs: None,
        put_latest=lambda target, payload: (
            published.append(payload) or True
        ),
        getpid=lambda: 1,
        wall_time=lambda: 2.0,
    )

    assert first.closed is True
    assert published[0]["reason"] == (
        "sidecar_init_failed:RuntimeError:snapshot client failed"
    )
