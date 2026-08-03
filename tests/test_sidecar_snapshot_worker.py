import queue
import threading
from unittest.mock import patch

from risk.independent_supervisor import _RiskSnapshotWorker
from risk.sidecar_snapshot_worker import RiskSnapshotWorker


def _put_latest(target_queue, payload):
    try:
        target_queue.put_nowait(payload)
        return True
    except queue.Full:
        target_queue.get_nowait()
        target_queue.put_nowait(payload)
        return True


def _worker(exchange, monotonic=12.5, wall=100.0):
    return RiskSnapshotWorker(
        exchange,
        put_latest=_put_latest,
        perf_counter=lambda: monotonic,
        wall_time=lambda: wall,
    )


def test_submit_is_bounded_to_one_pending_snapshot():
    worker = _worker(object())

    assert worker.submit(1, 10.0) is True
    assert worker.submit(2, 11.0) is False
    assert worker.request_queue.get_nowait() == {
        "sequence": 1,
        "requested_monotonic": 10.0,
    }


def test_take_latest_drains_all_available_results():
    worker = _worker(object())
    worker.result_queue = queue.Queue()
    worker.result_queue.put({"sequence": 1})
    worker.result_queue.put({"sequence": 2})
    worker.result_queue.put({"sequence": 3})

    assert worker.take_latest() == {"sequence": 3}
    assert worker.take_latest() is None


class _SnapshotExchange:
    def get_risk_snapshot(self):
        return True, {"positions": []}, ""


def test_worker_publishes_a_full_snapshot_with_completion_times():
    worker = _worker(_SnapshotExchange())
    worker.start()
    try:
        assert worker.thread.name == "ChronosRiskSnapshot"
        assert worker.thread.daemon is True
        assert worker.submit(7, 10.5)
        result = worker.result_queue.get(timeout=1.0)
    finally:
        assert worker.stop(1.0)

    assert result == {
        "sequence": 7,
        "requested_monotonic": 10.5,
        "completed_monotonic": 12.5,
        "completed_at": 100.0,
        "full_snapshot": True,
        "healthy": True,
        "snapshot": {"positions": []},
        "reason": "",
    }


class _ChannelExchange:
    def check_account_channel(self):
        return False, "listen_key_stale"


def test_worker_falls_back_to_account_channel_health():
    worker = _worker(_ChannelExchange())
    worker.start()
    try:
        assert worker.submit(3, 2.0)
        result = worker.result_queue.get(timeout=1.0)
    finally:
        assert worker.stop(1.0)

    assert result["full_snapshot"] is False
    assert result["healthy"] is False
    assert result["snapshot"] == {}
    assert result["reason"] == "listen_key_stale"


class _FailingExchange:
    def get_risk_snapshot(self):
        raise OSError("network down")


def test_worker_converts_snapshot_exceptions_to_unhealthy_results():
    worker = _worker(_FailingExchange())
    worker.start()
    try:
        assert worker.submit(4, 3.0)
        result = worker.result_queue.get(timeout=1.0)
    finally:
        assert worker.stop(1.0)

    assert result["healthy"] is False
    assert result["snapshot"] == {}
    assert result["reason"] == "snapshot_exception:OSError:network down"


class _BlockingExchange:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def get_risk_snapshot(self):
        self.entered.set()
        self.release.wait(1.0)
        return True, {}, ""


def test_stop_reports_when_a_snapshot_query_is_still_blocked():
    exchange = _BlockingExchange()
    worker = _worker(exchange)
    worker.start()
    try:
        assert worker.submit(5, 4.0)
        assert exchange.entered.wait(1.0)
        assert worker.stop(0.01) is False
    finally:
        exchange.release.set()
        worker.thread.join(1.0)

    assert worker.thread.is_alive() is False


def test_private_facade_injects_the_original_patchable_clocks():
    with (
        patch(
            "risk.independent_supervisor.time.perf_counter",
            return_value=22.0,
        ),
        patch(
            "risk.independent_supervisor.time.time",
            return_value=200.0,
        ),
    ):
        worker = _RiskSnapshotWorker(_SnapshotExchange())

    worker.start()
    try:
        assert worker.submit(8, 20.0)
        result = worker.result_queue.get(timeout=1.0)
    finally:
        assert worker.stop(1.0)

    assert result["completed_monotonic"] == 22.0
    assert result["completed_at"] == 200.0
