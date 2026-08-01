import queue
from types import SimpleNamespace

from risk.sidecar_transport import SidecarTransport


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
    owner = SimpleNamespace(settings={"session_id": "session-1"})
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
                "type": "ABORT_REARM",
                "session_id": "session-1",
                "token": "token-1",
            },
            0.5,
        )
    ]
    assert all(channel.closed for channel in channels)
