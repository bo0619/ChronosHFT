"""Bounded asynchronous exchange snapshot worker for the risk sidecar."""

import queue
import threading


class RiskSnapshotWorker:
    """Runs at most one ordinary exchange snapshot outside the control loop."""

    def __init__(
        self,
        exchange,
        *,
        put_latest,
        perf_counter,
        wall_time,
    ):
        self.exchange = exchange
        self._put_latest = put_latest
        self._perf_counter = perf_counter
        self._wall_time = wall_time
        self.request_queue = queue.Queue(maxsize=1)
        self.result_queue = queue.Queue(maxsize=1)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name="ChronosRiskSnapshot",
            daemon=True,
        )

    def start(self):
        self.thread.start()

    def submit(
        self,
        sequence: int,
        requested_monotonic: float,
    ) -> bool:
        try:
            self.request_queue.put_nowait(
                {
                    "sequence": int(sequence),
                    "requested_monotonic": float(requested_monotonic),
                }
            )
        except queue.Full:
            return False
        return True

    def take_latest(self):
        latest = None
        while True:
            try:
                latest = self.result_queue.get_nowait()
            except queue.Empty:
                return latest

    def stop(self, join_timeout_sec: float = 0.2) -> bool:
        self.stop_event.set()
        self.thread.join(max(0.0, float(join_timeout_sec)))
        return not self.thread.is_alive()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                request = self.request_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            if self.stop_event.is_set():
                break

            sequence = int(request.get("sequence", 0) or 0)
            requested_monotonic = float(
                request.get("requested_monotonic", 0.0) or 0.0
            )
            snapshot_query = getattr(
                self.exchange,
                "get_risk_snapshot",
                None,
            )
            full_snapshot = callable(snapshot_query)
            try:
                if full_snapshot:
                    healthy, snapshot, reason = snapshot_query()
                else:
                    healthy, reason = self.exchange.check_account_channel()
                    snapshot = {}
            except Exception as exc:
                healthy = False
                snapshot = {}
                reason = f"snapshot_exception:{type(exc).__name__}:{exc}"
            completed_monotonic = self._perf_counter()
            completed_at = self._wall_time()
            self._put_latest(
                self.result_queue,
                {
                    "sequence": sequence,
                    "requested_monotonic": requested_monotonic,
                    "completed_monotonic": completed_monotonic,
                    "completed_at": completed_at,
                    "full_snapshot": full_snapshot,
                    "healthy": bool(healthy),
                    "snapshot": snapshot,
                    "reason": str(reason or ""),
                },
            )
