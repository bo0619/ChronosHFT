import queue

import pytest

from risk.sidecar_protocol import SidecarProtocol
from risk.sidecar_runtime import SidecarRuntime


def _message(message_type, session_id="session-1", **payload):
    return SidecarProtocol.parent_message(
        message_type,
        session_id,
        **payload,
    )


class _CommandCore:
    def __init__(self):
        self.calls = []

    def receive_parent_heartbeat(self, sequence, *, sent_monotonic=None):
        self.calls.append(("HEARTBEAT", sequence, sent_monotonic))

    def request_quiesce(self, request_id, reason):
        self.calls.append(("QUIESCE", request_id, reason))

    def request_shutdown_resume(self, request_id, reason):
        self.calls.append(("RESUME_SHUTDOWN", request_id, reason))

    def request_stop(self, request_id, cancel_orders):
        self.calls.append(("STOP", request_id, cancel_orders))

    def prepare_rearm(self, request_id, reason):
        self.calls.append(("PREPARE_REARM", request_id, reason))

    def commit_rearm(self, request_id, token):
        self.calls.append(("COMMIT_REARM", request_id, token))

    def abort_rearm(self, token):
        self.calls.append(("ABORT_REARM", token))


def test_command_dispatch_routes_only_the_active_session():
    core = _CommandCore()
    commands = [
        _message("HEARTBEAT", sequence=7, sent_monotonic=12.5),
        _message("QUIESCE", request_id="q1", reason="operator"),
        _message(
            "RESUME_SHUTDOWN",
            request_id="r1",
            reason="truth_drift",
        ),
        _message("STOP", request_id="s1", cancel_orders=False),
        _message(
            "PREPARE_REARM",
            request_id="p1",
            reason="operator_ack",
        ),
        _message("COMMIT_REARM", request_id="c1", token="token-1"),
        _message("ABORT_REARM", token="token-2"),
        _message("STOP", "old-session", request_id="ignored"),
    ]
    command_queue = queue.Queue()
    for command in commands:
        command_queue.put(command)

    SidecarRuntime.drain_commands(command_queue, core, "session-1")

    assert core.calls == [
        ("HEARTBEAT", 7, 12.5),
        ("QUIESCE", "q1", "operator"),
        ("RESUME_SHUTDOWN", "r1", "truth_drift"),
        ("STOP", "s1", False),
        ("PREPARE_REARM", "p1", "operator_ack"),
        ("COMMIT_REARM", "c1", "token-1"),
        ("ABORT_REARM", "token-2"),
    ]


def test_command_dispatch_ignores_malformed_and_unknown_messages():
    core = _CommandCore()

    assert SidecarRuntime.dispatch_command(core, None, "session-1") is False
    assert (
        SidecarRuntime.dispatch_command(
            core,
            _message("UNKNOWN"),
            "session-1",
        )
        is False
    )
    assert (
        SidecarRuntime.dispatch_command(
            core,
            _message(
                "STOP",
                request_id="stop-1",
                cancel_orders="false",
            ),
            "session-1",
        )
        is False
    )
    assert core.calls == []


def test_heartbeat_channel_applies_only_the_latest_message():
    heartbeat_queue = queue.Queue()
    heartbeat_queue.put(
        _message("HEARTBEAT", sequence=1, sent_monotonic=10.0)
    )
    heartbeat_queue.put(
        _message("HEARTBEAT", sequence=3, sent_monotonic=12.0)
    )
    core = _CommandCore()

    SidecarRuntime.drain_latest_heartbeat(
        heartbeat_queue,
        core,
        "session-1",
    )

    assert core.calls == [("HEARTBEAT", 3, 12.0)]


def test_heartbeat_channel_ignores_malformed_messages_without_exiting():
    heartbeat_queue = queue.Queue()
    heartbeat_queue.put(
        _message("HEARTBEAT", sequence=4, sent_monotonic=13.0)
    )
    heartbeat_queue.put(None)
    heartbeat_queue.put(
        _message("HEARTBEAT", sequence="5", sent_monotonic=14.0)
    )
    core = _CommandCore()

    SidecarRuntime.drain_latest_heartbeat(
        heartbeat_queue,
        core,
        "session-1",
    )

    assert core.calls == [("HEARTBEAT", 4, 13.0)]


def _status():
    return {
        "healthy": True,
        "reason": "",
        "risk_action": "NONE",
        "funding_action": "NONE",
        "funding_reason": "",
        "stage": "ARMED",
        "exchange_healthy": True,
        "last_cancel_ok": True,
        "last_cancel_reason": "",
        "last_flatten_ok": None,
        "last_flatten_count": 0,
        "last_flatten_reason": "",
        "flat_verification_count": 0,
        "risk_snapshot_sequence": 1,
        "risk_snapshot_captured_monotonic": 10.0,
        "parent_heartbeat_error": "",
        "state_generation": 1,
        "state_load_error": "",
        "state_persist_error": "",
        "last_rearm_request_id": "",
        "last_rearm_phase": "",
        "last_rearm_accepted": None,
        "last_rearm_reason": "",
        "quiesced": False,
        "last_quiesce_request_id": "",
        "last_quiesce_accepted": None,
        "last_quiesce_reason": "",
        "last_shutdown_resume_request_id": "",
        "last_shutdown_resume_accepted": None,
        "last_shutdown_resume_reason": "",
        "last_stop_request_id": "",
        "last_stop_accepted": None,
        "last_stop_reason": "",
        "last_stop_cancel_attempted": False,
        "last_stop_cancel_ok": None,
    }


class _Exchange:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _SnapshotWorker:
    def __init__(self, exchange):
        self.exchange = exchange
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        return True


class _StepCore:
    def __init__(self, exchange, settings, *, snapshot_worker):
        self.exchange = exchange
        self.settings = settings
        self.snapshot_worker = snapshot_worker
        self.steps = 0

    def step(self, now):
        self.steps += 1
        return _status(), self.steps == 1


def test_runtime_publishes_initial_and_terminal_status_then_closes_clients():
    command_queue = queue.Queue()
    status_queue = object()
    exchange = _Exchange()
    snapshot_exchange = _Exchange()
    workers = []
    published = []
    sleeps = []
    monotonic = iter((10.0, 10.01))
    wall = iter((100.0, 100.01))

    def make_worker(target):
        worker = _SnapshotWorker(target)
        workers.append(worker)
        return worker

    SidecarRuntime.run(
        command_queue,
        status_queue,
        SidecarProtocol.with_launch_contract(
            {"session_id": "session-1", "status_interval_sec": 1.0}
        ),
        exchange,
        snapshot_exchange=snapshot_exchange,
        heartbeat_queue=None,
        snapshot_worker_factory=make_worker,
        core_factory=_StepCore,
        isolated_exchange_type=_Exchange,
        put_latest=lambda target, payload: (
            published.append((target, payload)) or True
        ),
        perf_counter=lambda: next(monotonic),
        wall_time=lambda: next(wall),
        getpid=lambda: 4321,
        sleep=sleeps.append,
    )

    assert len(published) == 2
    assert [item[1]["sequence"] for item in published] == [1, 2]
    assert [item[1]["reported_at"] for item in published] == [100.0, 100.01]
    assert all(item[0] is status_queue for item in published)
    assert all(item[1]["session_id"] == "session-1" for item in published)
    assert all(item[1]["pid"] == 4321 for item in published)
    assert all(
        item[1]["protocol_version"] == SidecarProtocol.VERSION
        for item in published
    )
    assert all(
        item[1]["protocol_handshake_complete"] is True
        for item in published
    )
    assert all(
        set(item[1]["capabilities"])
        == SidecarProtocol.CHILD_CAPABILITIES
        for item in published
    )
    assert sleeps == [0.05]
    assert workers[0].started is True
    assert workers[0].stopped is True
    assert exchange.closed is True
    assert snapshot_exchange.closed is True


def test_live_exchange_requires_a_distinct_snapshot_client():
    exchange = _Exchange()

    with pytest.raises(
        ValueError,
        match="requires an isolated snapshot exchange client",
    ):
        SidecarRuntime.run(
            queue.Queue(),
            object(),
            SidecarProtocol.with_launch_contract({}),
            exchange,
            snapshot_exchange=None,
            heartbeat_queue=None,
            snapshot_worker_factory=_SnapshotWorker,
            core_factory=_StepCore,
            isolated_exchange_type=_Exchange,
            put_latest=lambda target, payload: True,
            perf_counter=lambda: 0.0,
            wall_time=lambda: 0.0,
            getpid=lambda: 1,
            sleep=lambda delay: None,
        )
