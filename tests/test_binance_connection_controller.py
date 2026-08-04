import threading

from event.type import GatewayState
from gateway.binance.connection_controller import (
    BinanceConnectionController,
    BinanceConnectionDependencies,
)


class FakeWebSocket:
    def __init__(self, *, connected=True, trace=None):
        self.connected = connected
        self.closed = False
        self.trace = trace if trace is not None else []

    def wait_until_connected(self, *, timeout_sec):
        self.trace.append(("wait", timeout_sec))
        return self.connected

    def close(self):
        self.closed = True
        self.trace.append("ws.close")
        return True


def make_controller(*, connected=True, resync=True, start_streams=True):
    lock = threading.RLock()
    state = {
        "book_generation": 0,
        "gateway_state": GatewayState.DISCONNECTED,
        "health": [],
        "trace": [],
        "resync": [],
        "created_ws": [],
    }

    def begin_generation(symbols):
        state["book_generation"] += 1
        state["trace"].append(("begin_generation", tuple(symbols)))
        return state["book_generation"]

    def create_ws(_generation):
        ws = FakeWebSocket(connected=connected, trace=state["trace"])
        state["created_ws"].append(ws)
        return ws

    def sync_book(symbol, **kwargs):
        state["resync"].append((symbol, kwargs))
        return resync

    dependencies = BinanceConnectionDependencies(
        transport_lock=lock,
        begin_generation_locked=begin_generation,
        generation_matches_locked=lambda generation: (
            generation is None or generation == state["book_generation"]
        ),
        begin_book_shutdown_locked=lambda: state["trace"].append(
            "book.shutdown"
        ),
        resync_book=sync_book,
        join_book_recovery_threads=lambda: True,
        book_recovery_threads_stopped=lambda: True,
        apply_account_configuration=lambda: True,
        create_ws=create_ws,
        start_streams=lambda ws, symbols, generation: (
            state["trace"].append(
                ("streams.start", ws, tuple(symbols), generation)
            )
            or start_streams
        ),
        stop_user_stream=lambda: (
            state["trace"].append("user.stop") or True
        ),
        invalidate_user_stream=lambda: (
            state["trace"].append("user.invalidate") or 1
        ),
        stream_ready_timeout_sec=lambda: 5.0,
        set_state=lambda value: state.__setitem__("gateway_state", value),
        get_state=lambda: state["gateway_state"],
        emit_health=state["health"].append,
        close_session=lambda: state["trace"].append("session.close"),
        venue_name=lambda: "BINANCE",
        log_info=lambda message: state["trace"].append(("info", message)),
        log_warning=lambda message: state["trace"].append(
            ("warning", message)
        ),
        log_error=lambda message: state["trace"].append(("error", message)),
    )
    return BinanceConnectionController(dependencies), state


def test_connect_commits_only_after_stream_and_book_readiness():
    controller, state = make_controller()

    assert controller.connect(["btcusdt"])
    assert controller.active
    assert controller.symbols == ["BTCUSDT"]
    assert state["gateway_state"] == GatewayState.READY
    assert state["resync"] == [
        ("BTCUSDT", {"expected_generation": 1})
    ]
    assert state["health"] == []


def test_connect_timeout_fails_current_generation_closed():
    controller, state = make_controller(connected=False)

    assert not controller.connect(["BTCUSDT"])
    assert not controller.active
    assert state["gateway_state"] == GatewayState.ERROR
    assert state["created_ws"][0].closed
    assert state["health"] == [
        "FREEZE_VENUE:BINANCE:STREAM_READY_TIMEOUT"
    ]


def test_recovery_replaces_transport_and_emits_verification_identity():
    controller, state = make_controller()
    controller.symbols = ["BTCUSDT"]
    old_ws = FakeWebSocket(trace=state["trace"])
    controller.ws = old_ws

    assert controller.recover({"owner": "sidecar", "epoch": 7})
    assert old_ws.closed
    assert controller.ws is state["created_ws"][0]
    assert state["gateway_state"] == GatewayState.READY
    assert state["health"] == ["VERIFY_VENUE:BINANCE:7:sidecar"]


def test_shutdown_invalidates_lease_before_closing_transports():
    controller, state = make_controller()
    controller.active = True
    controller.symbols = ["BTCUSDT"]
    controller.ws = FakeWebSocket(trace=state["trace"])

    assert controller.begin_shutdown()
    assert controller.closing
    assert not controller.active
    assert state["gateway_state"] == GatewayState.DISCONNECTED
    assert state["trace"].index("user.invalidate") < state["trace"].index(
        "ws.close"
    )
    assert state["trace"].index("ws.close") < state["trace"].index(
        "user.stop"
    )


def test_stale_failure_cannot_overwrite_newer_generation():
    controller, state = make_controller()
    controller.active = True
    state["book_generation"] = 2
    state["gateway_state"] = GatewayState.READY

    assert not controller.mark_failure_if_current(1)
    assert controller.active
    assert state["gateway_state"] == GatewayState.READY


def test_connection_state_is_owned_without_gateway_back_reference():
    controller, _state = make_controller()

    assert {"active", "closing", "ws", "symbols", "lifecycle_lock"} <= set(
        controller.__dict__
    )
    assert "owner" not in controller.__dict__
    assert "gateway" not in controller.__dict__
