import os

from infrastructure.systemd_watchdog import SystemdWatchdog


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeSocket:
    def __init__(self, sent, *, failure=None):
        self.sent = sent
        self.failure = failure
        self.closed = False

    def sendto(self, payload, address):
        if self.failure is not None:
            raise self.failure
        self.sent.append((payload, address))
        return len(payload)

    def close(self):
        self.closed = True


def watchdog_environment(**overrides):
    environment = {
        "NOTIFY_SOCKET": "@chronoshft-test",
        "WATCHDOG_USEC": "10000000",
        "WATCHDOG_PID": str(os.getpid()),
    }
    environment.update(overrides)
    return environment


def test_systemd_watchdog_is_disabled_without_systemd_environment():
    watchdog = SystemdWatchdog(environ={})

    assert watchdog.pulse(force=True) is False
    assert watchdog.snapshot()["reason"] == "not_configured"


def test_systemd_watchdog_throttles_abstract_socket_datagrams():
    clock = FakeClock()
    sent = []
    sockets = []

    def socket_factory(*_args):
        result = FakeSocket(sent)
        sockets.append(result)
        return result

    watchdog = SystemdWatchdog(
        environ=watchdog_environment(),
        monotonic=clock,
        socket_factory=socket_factory,
        address_family=1,
    )

    assert watchdog.pulse() is True
    clock.advance(3.9)
    assert watchdog.pulse() is False
    clock.advance(0.1)
    assert watchdog.pulse() is True

    assert sent == [
        (b"WATCHDOG=1", b"\0chronoshft-test"),
        (b"WATCHDOG=1", b"\0chronoshft-test"),
    ]
    assert all(item.closed for item in sockets)
    snapshot = watchdog.snapshot()
    assert snapshot["watchdog_period_sec"] == 10.0
    assert snapshot["ping_interval_sec"] == 4.0
    assert snapshot["send_count"] == 2
    assert snapshot["error_count"] == 0


def test_systemd_watchdog_rejects_wrong_pid_and_invalid_socket():
    wrong_pid = SystemdWatchdog(
        environ=watchdog_environment(WATCHDOG_PID=str(os.getpid() + 1)),
        address_family=1,
    )
    invalid_socket = SystemdWatchdog(
        environ=watchdog_environment(NOTIFY_SOCKET="relative.sock"),
        address_family=1,
    )

    assert wrong_pid.snapshot()["reason"] == "watchdog_pid_mismatch"
    assert invalid_socket.snapshot()["reason"] == "invalid_notify_socket"


def test_systemd_watchdog_contains_socket_failures_and_throttles_retries():
    clock = FakeClock()
    sockets = []

    def socket_factory(*_args):
        result = FakeSocket([], failure=OSError("unavailable"))
        sockets.append(result)
        return result

    watchdog = SystemdWatchdog(
        environ=watchdog_environment(),
        monotonic=clock,
        socket_factory=socket_factory,
        address_family=1,
    )

    assert watchdog.pulse() is False
    assert watchdog.pulse() is False
    assert len(sockets) == 1
    assert sockets[0].closed is True
    snapshot = watchdog.snapshot()
    assert snapshot["send_count"] == 0
    assert snapshot["error_count"] == 1
    assert snapshot["last_error"] == "OSError"
