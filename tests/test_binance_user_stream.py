import threading
from types import SimpleNamespace

from gateway.binance.user_stream import (
    BinanceUserStreamController,
    BinanceUserStreamDependencies,
)


class FakeWebSocket:
    def __init__(self):
        self.market_symbols = None
        self.listen_key = None
        self.closed = False

    def start_market_stream(self, symbols):
        self.market_symbols = symbols

    def start_user_stream(self, listen_key):
        self.listen_key = listen_key

    def close(self):
        self.closed = True


def make_controller(
    *,
    listen_key="lease-1",
    listen_key_factory=None,
    keepalive_status=200,
):
    state = {
        "active": True,
        "closing": False,
        "transport_generation": 3,
        "faults": [],
        "keepalive_calls": 0,
    }
    lock = threading.RLock()

    def keep_alive():
        state["keepalive_calls"] += 1
        return SimpleNamespace(status_code=keepalive_status)

    controller = BinanceUserStreamController(
        BinanceUserStreamDependencies(
            create_listen_key=(
                listen_key_factory
                if listen_key_factory is not None
                else lambda: listen_key
            ),
            keep_alive_listen_key=keep_alive,
            emit_fault=lambda code, detail="", **kwargs: state["faults"].append(
                (code, detail, kwargs)
            ),
            is_transport_active=lambda: state["active"],
            is_transport_closing=lambda: state["closing"],
            transport_generation_matches_locked=lambda generation: (
                generation is None
                or generation == state["transport_generation"]
            ),
            transport_lock=lock,
            log_critical=lambda message: state.setdefault(
                "critical",
                [],
            ).append(message),
        )
    )
    return controller, state


def test_start_owns_lease_generation_and_worker_lifecycle():
    controller, _state = make_controller()
    ws = FakeWebSocket()

    assert controller.start(ws, ["BTCUSDT"], transport_generation=3)
    assert controller.listen_key == "lease-1"
    assert controller.generation == 1
    assert ws.market_symbols == ["BTCUSDT"]
    assert ws.listen_key == "lease-1"
    assert controller.thread is not None
    assert controller.thread.is_alive()
    assert controller.stop()


def test_missing_listen_key_closes_candidate_transport():
    controller, _state = make_controller(listen_key=None)
    ws = FakeWebSocket()

    assert not controller.start(ws, ["BTCUSDT"], transport_generation=3)
    assert ws.closed
    assert controller.thread is None


def test_keepalive_failure_is_generation_fenced_and_reported():
    controller, state = make_controller(keepalive_status=500)
    controller.generation = 4

    assert not controller.keep_alive_once(4, transport_generation=3)
    assert state["keepalive_calls"] == 1
    assert state["faults"] == [
        (
            "USER_STREAM_KEEPALIVE_FAILED",
            "status=500",
            {
                "expected_generation": 3,
                "expected_keep_alive_generation": 4,
            },
        )
    ]


def test_stale_transport_or_lease_generation_never_touches_rest():
    controller, state = make_controller()
    controller.generation = 5

    assert not controller.keep_alive_once(4, transport_generation=3)
    assert not controller.keep_alive_once(5, transport_generation=2)
    assert state["keepalive_calls"] == 0


def test_invalidate_stops_old_worker_generation():
    controller, _state = make_controller()
    ws = FakeWebSocket()
    assert controller.start(ws, ["BTCUSDT"], transport_generation=3)
    old_generation = controller.generation

    assert controller.invalidate() == old_generation + 1
    assert controller.stop_event.is_set()
    assert controller.stop()


def test_late_listen_key_cannot_restart_shutdown_transport():
    listen_key_requested = threading.Event()
    release_listen_key = threading.Event()

    def create_listen_key():
        listen_key_requested.set()
        assert release_listen_key.wait(1.0)
        return "late-lease"

    controller, state = make_controller(
        listen_key_factory=create_listen_key
    )
    ws = FakeWebSocket()
    result = []
    worker = threading.Thread(
        target=lambda: result.append(
            controller.start(
                ws,
                ["BTCUSDT"],
                transport_generation=3,
            )
        )
    )
    worker.start()
    assert listen_key_requested.wait(1.0)

    with controller.dependencies.transport_lock:
        state["closing"] = True
        state["active"] = False
        state["transport_generation"] = 4
    release_listen_key.set()
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert result == [False]
    assert ws.closed
    assert ws.listen_key is None
    assert controller.thread is None
