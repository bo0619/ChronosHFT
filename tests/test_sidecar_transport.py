import queue
from types import SimpleNamespace

from risk.sidecar_protocol import SidecarProtocol
from risk.sidecar_transport import SidecarTransport


def test_put_latest_replaces_the_oldest_item_when_capacity_is_full():
    channel = queue.Queue(maxsize=1)
    channel.put_nowait("old")

    assert SidecarTransport.put_latest(channel, "new") is True
    assert channel.get_nowait() == "new"


class _AlwaysFullChannel:
    def put_nowait(self, payload):
        raise queue.Full

    def get_nowait(self):
        raise queue.Empty


def test_put_latest_reports_when_capacity_cannot_be_recovered():
    assert (
        SidecarTransport.put_latest(_AlwaysFullChannel(), "status")
        is False
    )


class _ReliableChannel:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def put(self, payload, *, block, timeout):
        self.calls.append((payload, block, timeout))
        if self.error is not None:
            raise self.error


def test_put_reliable_bounds_timeout_and_converts_queue_errors():
    channel = _ReliableChannel()

    assert SidecarTransport.put_reliable(channel, "control", -1.0) is True
    assert channel.calls == [("control", True, 0.0)]

    failed = _ReliableChannel(OSError("closed"))
    assert SidecarTransport.put_reliable(failed, "control", 1.0) is False


class _Process:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.alive = True

    def start(self):
        self.started = True

    def is_alive(self):
        return self.alive


class _Context:
    def __init__(self):
        self.queues = []
        self.process = None

    def Queue(self, *, maxsize):
        channel = SimpleNamespace(maxsize=maxsize)
        self.queues.append(channel)
        return channel

    def Process(self, **kwargs):
        self.process = _Process(**kwargs)
        return self.process


def test_start_process_builds_isolated_channels_and_spawn_process():
    context = _Context()
    start_methods = []
    multiprocessing_module = SimpleNamespace(
        get_context=lambda method: (start_methods.append(method), context)[1]
    )
    owner = SimpleNamespace(
        settings=SidecarProtocol.with_launch_contract(
            {"session_id": "session-1"}
        )
    )
    process_target = object()

    SidecarTransport.start_process(
        owner,
        multiprocessing_module,
        process_target,
        lambda: 12.5,
    )

    assert start_methods == ["spawn"]
    assert [channel.maxsize for channel in context.queues] == [32, 1, 8]
    assert context.process.started is True
    assert context.process.kwargs == {
        "target": process_target,
        "args": (
            owner.command_queue,
            owner.status_queue,
            owner.settings,
            owner.heartbeat_queue,
        ),
        "name": "ChronosRiskSupervisor",
        "daemon": False,
    }
    assert owner.started_at == 12.5


def test_start_process_rejects_incompatible_contract_before_spawn():
    settings = SidecarProtocol.with_launch_contract({})
    settings["protocol_version"] += 1
    owner = SimpleNamespace(settings=settings)
    multiprocessing_module = SimpleNamespace(
        get_context=lambda method: (_ for _ in ()).throw(
            AssertionError("spawn context must not be created")
        )
    )

    try:
        SidecarTransport.start_process(
            owner,
            multiprocessing_module,
            object(),
            lambda: 12.5,
        )
    except ValueError as exc:
        assert "launch_protocol_version_incompatible" in str(exc)
    else:
        raise AssertionError("incompatible launch contract was accepted")


def test_drain_status_filters_session_and_sequence_before_committing():
    status_queue = queue.Queue()
    status_queue.put({"session_id": "other", "sequence": 10})
    status_queue.put({"session_id": "session-1", "sequence": 1})
    status_queue.put({"session_id": "session-1", "sequence": 3})
    owner = SimpleNamespace(
        status_queue=status_queue,
        session_id="session-1",
        last_status={"sequence": 2},
        last_status_received_at=1.0,
        last_status_protocol_error="old_error",
        _validate_status_payload=lambda status: status,
    )

    SidecarTransport.drain_status(owner, 20.0)

    assert owner.last_status == {"session_id": "session-1", "sequence": 3}
    assert owner.last_status_received_at == 20.0
    assert owner.last_status_protocol_error == ""


def test_request_control_uses_owner_facades_until_matching_ack():
    sent_heartbeats = []
    drained_at = []
    enqueued = []
    expected_ack = {"accepted": True, "request_id": "request-1"}
    owner = SimpleNamespace(
        enabled=True,
        process=_Process(),
        command_queue=object(),
        session_id="session-1",
        control_enqueue_timeout_sec=0.5,
        rearm_command_timeout_sec=1.0,
        _send_heartbeat=sent_heartbeats.append,
        _drain_status=drained_at.append,
        _read_control_ack=lambda command_type, request_id: expected_ack,
        _control_failure_result=lambda *args, **kwargs: {
            "accepted": False,
            "args": args,
            "payload": kwargs,
        },
    )
    clock_values = iter((10.0, 10.0, 10.0))

    result = SidecarTransport.request_control(
        owner,
        "quiesce",
        None,
        {"reason": "operator"},
        lambda channel, payload, timeout: (
            enqueued.append((channel, payload, timeout)) or True
        ),
        lambda size: "request-1",
        lambda: next(clock_values),
        lambda _delay: None,
    )

    assert result is expected_ack
    assert enqueued == [
        (
            owner.command_queue,
            {
                "protocol_version": SidecarProtocol.VERSION,
                "type": "QUIESCE",
                "session_id": "session-1",
                "request_id": "request-1",
                "reason": "operator",
            },
            0.5,
        )
    ]
    assert sent_heartbeats == [10.0]
    assert drained_at == [10.0]


def test_enqueue_abort_and_close_channels_share_owner_queue_state():
    enqueued = []
    channels = [SimpleNamespace(closed=False) for _ in range(3)]
    for channel in channels:
        channel.close = lambda target=channel: setattr(target, "closed", True)
    owner = SimpleNamespace(
        enabled=True,
        process=_Process(),
        command_queue=channels[0],
        heartbeat_queue=channels[1],
        status_queue=channels[2],
        session_id="session-1",
        control_enqueue_timeout_sec=0.5,
    )

    assert SidecarTransport.enqueue_abort(
        owner,
        "token-1",
        lambda channel, payload, timeout: (
            enqueued.append((channel, payload, timeout)) or True
        ),
    )
    SidecarTransport.close_channels(owner)

    assert enqueued == [
        (
            owner.command_queue,
            {
                "protocol_version": SidecarProtocol.VERSION,
                "type": "ABORT_REARM",
                "session_id": "session-1",
                "token": "token-1",
            },
            0.5,
        )
    ]
    assert all(channel.closed for channel in channels)
