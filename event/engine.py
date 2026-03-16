from collections import defaultdict, deque
from queue import Empty, Queue
from threading import Lock, Thread
import time

from infrastructure.logger import logger


class EventEngine:
    HOT_LANES = ("market", "execution")
    ALL_LANES = HOT_LANES + ("cold",)

    def __init__(self, profile_config=None):
        self._active = False
        self._queues = {lane: Queue() for lane in self.ALL_LANES}
        self._threads = {}
        self._handlers = {lane: defaultdict(list) for lane in self.ALL_LANES}
        self._queue_timestamps = {lane: deque() for lane in self.ALL_LANES}
        self._lane_stats = {
            lane: {
                "processed": 0,
                "last_event_type": "",
                "last_backlog_ms": 0.0,
                "max_backlog_ms": 0.0,
                "last_duration_ms": 0.0,
                "max_duration_ms": 0.0,
                "slow_handler_count": 0,
                "last_processed_at": 0.0,
            }
            for lane in self.ALL_LANES
        }
        self._handler_stats = {}
        self._lane_inflight = {
            lane: {
                "event_type": "",
                "handler_name": "",
                "started_at": 0.0,
                "handler_started_at": 0.0,
            }
            for lane in self.ALL_LANES
        }
        self._pending_cold = {}
        self._dispatch_seq = 0
        self._lock = Lock()
        self._alert_lock = Lock()
        self._last_depth_alert_at = {}
        self._last_backlog_alert_at = {}
        self._last_slow_handler_alert_at = {}
        self.profile_config = self._build_profile_config(profile_config or {})
        self._reset_threads()

    def _build_profile_config(self, profile_config):
        defaults = {
            "queue_warn_depth": {
                "market": 25,
                "execution": 25,
                "cold": 200,
            },
            "backlog_warn_ms": {
                "market": 200.0,
                "execution": 300.0,
                "cold": 2000.0,
            },
            "handler_slow_ms": {
                "market": 5.0,
                "execution": 20.0,
                "cold": 100.0,
            },
            "alert_interval_sec": 5.0,
        }
        normalized = {}
        for key in ("queue_warn_depth", "backlog_warn_ms", "handler_slow_ms"):
            normalized[key] = self._normalize_lane_thresholds(
                profile_config.get(key),
                defaults[key],
            )
        normalized["alert_interval_sec"] = float(
            profile_config.get("alert_interval_sec", defaults["alert_interval_sec"])
        )
        return normalized

    def _normalize_lane_thresholds(self, raw_value, defaults):
        if isinstance(raw_value, (int, float)):
            return {lane: float(raw_value) for lane in self.ALL_LANES}

        raw_value = raw_value or {}
        normalized = {}
        for lane in self.ALL_LANES:
            fallback = defaults[lane]
            if lane in self.HOT_LANES:
                fallback = raw_value.get("hot", fallback)
            else:
                fallback = raw_value.get("cold", fallback)
            normalized[lane] = float(raw_value.get(lane, fallback))
        return normalized

    def _reset_threads(self):
        self._threads = {
            lane: Thread(target=self._run_lane, args=(lane,), daemon=True, name=f"EventEngine-{lane}")
            for lane in self.ALL_LANES
        }

    def start(self):
        self._active = True
        for lane in self.ALL_LANES:
            thread = self._threads.get(lane)
            if thread is None or not thread.is_alive():
                self._threads[lane] = Thread(
                    target=self._run_lane,
                    args=(lane,),
                    daemon=True,
                    name=f"EventEngine-{lane}",
                )
                self._threads[lane].start()
        logger.info(">>> [EventEngine] started with market/execution/cold lanes")

    def stop(self):
        self._active = False
        for lane in self.ALL_LANES:
            thread = self._threads.get(lane)
            if thread and thread.is_alive():
                thread.join()
        logger.info(">>> [EventEngine] stopped")

    def put(self, event):
        dispatch_id = self._next_dispatch_id()
        hot_lanes = [lane for lane in self.HOT_LANES if event.type in self._handlers[lane]]
        cold_registered = event.type in self._handlers["cold"]
        if not hot_lanes and not cold_registered:
            return

        if cold_registered and hot_lanes:
            with self._lock:
                self._pending_cold[dispatch_id] = {
                    "event": event,
                    "remaining": len(hot_lanes),
                }

        for lane in hot_lanes:
            self._enqueue(lane, dispatch_id, event)

        if cold_registered and not hot_lanes:
            self._enqueue("cold", dispatch_id, event)

    def register(self, type_, handler):
        self.register_cold(type_, handler)

    def register_hot(self, type_, handler):
        self.register_execution(type_, handler)

    def register_market(self, type_, handler):
        self._handlers["market"][type_].append(handler)

    def register_execution(self, type_, handler):
        self._handlers["execution"][type_].append(handler)

    def register_cold(self, type_, handler):
        self._handlers["cold"][type_].append(handler)

    def get_queue_snapshot(self):
        market_depth = self._queues["market"].qsize()
        execution_depth = self._queues["execution"].qsize()
        cold_depth = self._queues["cold"].qsize()
        return {
            "market_depth": market_depth,
            "execution_depth": execution_depth,
            "hot_depth": market_depth + execution_depth,
            "cold_depth": cold_depth,
        }

    def get_metrics_snapshot(self):
        now = time.perf_counter()
        snapshot = {
            "queues": self.get_queue_snapshot(),
            "lanes": {},
            "config": {
                "queue_warn_depth": dict(self.profile_config["queue_warn_depth"]),
                "backlog_warn_ms": dict(self.profile_config["backlog_warn_ms"]),
                "handler_slow_ms": dict(self.profile_config["handler_slow_ms"]),
            },
        }
        with self._lock:
            for lane in self.ALL_LANES:
                stats = dict(self._lane_stats[lane])
                inflight = dict(self._lane_inflight[lane])
                queue_timestamps = self._queue_timestamps[lane]
                oldest_queued_ms = 0.0
                if queue_timestamps:
                    oldest_queued_ms = max(0.0, (now - queue_timestamps[0]) * 1000.0)
                inflight_ms = 0.0
                if inflight["started_at"]:
                    inflight_ms = max(0.0, (now - inflight["started_at"]) * 1000.0)
                handler_inflight_ms = 0.0
                if inflight["handler_started_at"]:
                    handler_inflight_ms = max(0.0, (now - inflight["handler_started_at"]) * 1000.0)
                stats.update(
                    {
                        "depth": self._queues[lane].qsize(),
                        "oldest_queued_ms": oldest_queued_ms,
                        "inflight_ms": inflight_ms,
                        "handler_inflight_ms": handler_inflight_ms,
                        "inflight_event_type": inflight["event_type"],
                        "inflight_handler_name": inflight["handler_name"],
                    }
                )
                snapshot["lanes"][lane] = stats
        return snapshot

    def get_handler_metrics_snapshot(self, limit=None):
        with self._lock:
            rows = []
            for (lane, event_type, handler_name), stats in self._handler_stats.items():
                row = dict(stats)
                row.update(
                    {
                        "lane": lane,
                        "event_type": event_type,
                        "handler_name": handler_name,
                    }
                )
                rows.append(row)
        rows.sort(key=lambda item: (item["max_ms"], item["avg_ms"]), reverse=True)
        if limit is not None:
            return rows[:limit]
        return rows

    def _next_dispatch_id(self):
        with self._lock:
            self._dispatch_seq += 1
            return self._dispatch_seq

    def _enqueue(self, lane: str, dispatch_id: int, event):
        enqueued_at = time.perf_counter()
        with self._lock:
            self._queue_timestamps[lane].append(enqueued_at)
        self._queues[lane].put((dispatch_id, enqueued_at, event))
        self._maybe_alert_queue_depth(lane)

    def _run_lane(self, lane: str):
        while self._active:
            try:
                dispatch_id, enqueued_at, event = self._queues[lane].get(timeout=1.0)
                self._note_dequeue(lane)
                try:
                    self._process_lane(lane, event, enqueued_at)
                finally:
                    if lane in self.HOT_LANES:
                        self._handoff_to_cold(dispatch_id)
            except Empty:
                pass

    def _note_dequeue(self, lane: str):
        with self._lock:
            if self._queue_timestamps[lane]:
                self._queue_timestamps[lane].popleft()

    def _handoff_to_cold(self, dispatch_id: int):
        if dispatch_id is None:
            return
        with self._lock:
            pending = self._pending_cold.get(dispatch_id)
            if not pending:
                return
            pending["remaining"] -= 1
            if pending["remaining"] > 0:
                return
            event = pending["event"]
            del self._pending_cold[dispatch_id]
        if event.type in self._handlers["cold"]:
            self._enqueue("cold", dispatch_id, event)

    def _process_lane(self, lane: str, event, enqueued_at: float):
        handlers = self._handlers[lane].get(event.type, ())
        started_at = time.perf_counter()
        backlog_ms = max(0.0, (started_at - enqueued_at) * 1000.0)
        self._set_lane_inflight(lane, event.type, started_at)
        self._record_lane_start(lane, event.type, backlog_ms, started_at)
        self._maybe_alert_backlog(lane, event.type, backlog_ms)

        try:
            for handler in handlers:
                handler_name = self._handler_name(handler)
                handler_started_at = time.perf_counter()
                self._set_lane_handler_inflight(lane, handler_name, handler_started_at)
                try:
                    handler(event)
                except Exception as exc:
                    logger.error(f"[EventEngine:{lane}] handler failed {event.type}: {exc}")
                finally:
                    elapsed_ms = max(0.0, (time.perf_counter() - handler_started_at) * 1000.0)
                    self._record_handler_metrics(lane, event.type, handler_name, elapsed_ms)
                    self._maybe_alert_slow_handler(lane, event.type, handler_name, elapsed_ms)
        finally:
            duration_ms = max(0.0, (time.perf_counter() - started_at) * 1000.0)
            self._record_lane_finish(lane, event.type, duration_ms)
            self._clear_lane_inflight(lane)

    def _set_lane_inflight(self, lane: str, event_type: str, started_at: float):
        with self._lock:
            self._lane_inflight[lane] = {
                "event_type": event_type,
                "handler_name": "",
                "started_at": started_at,
                "handler_started_at": 0.0,
            }

    def _set_lane_handler_inflight(self, lane: str, handler_name: str, started_at: float):
        with self._lock:
            self._lane_inflight[lane]["handler_name"] = handler_name
            self._lane_inflight[lane]["handler_started_at"] = started_at

    def _clear_lane_inflight(self, lane: str):
        with self._lock:
            self._lane_inflight[lane] = {
                "event_type": "",
                "handler_name": "",
                "started_at": 0.0,
                "handler_started_at": 0.0,
            }

    def _record_lane_start(self, lane: str, event_type: str, backlog_ms: float, started_at: float):
        with self._lock:
            stats = self._lane_stats[lane]
            stats["processed"] += 1
            stats["last_event_type"] = event_type
            stats["last_backlog_ms"] = backlog_ms
            stats["max_backlog_ms"] = max(stats["max_backlog_ms"], backlog_ms)
            stats["last_processed_at"] = started_at

    def _record_lane_finish(self, lane: str, event_type: str, duration_ms: float):
        with self._lock:
            stats = self._lane_stats[lane]
            stats["last_event_type"] = event_type
            stats["last_duration_ms"] = duration_ms
            stats["max_duration_ms"] = max(stats["max_duration_ms"], duration_ms)

    def _record_handler_metrics(self, lane: str, event_type: str, handler_name: str, elapsed_ms: float):
        slow_threshold_ms = self.profile_config["handler_slow_ms"][lane]
        with self._lock:
            key = (lane, event_type, handler_name)
            stats = self._handler_stats.setdefault(
                key,
                {
                    "count": 0,
                    "total_ms": 0.0,
                    "avg_ms": 0.0,
                    "last_ms": 0.0,
                    "max_ms": 0.0,
                    "slow_count": 0,
                },
            )
            stats["count"] += 1
            stats["total_ms"] += elapsed_ms
            stats["avg_ms"] = stats["total_ms"] / stats["count"]
            stats["last_ms"] = elapsed_ms
            stats["max_ms"] = max(stats["max_ms"], elapsed_ms)
            if elapsed_ms > slow_threshold_ms:
                stats["slow_count"] += 1
                self._lane_stats[lane]["slow_handler_count"] += 1

    def _maybe_alert_queue_depth(self, lane: str):
        warn_depth = self.profile_config["queue_warn_depth"][lane]
        depth = self._queues[lane].qsize()
        if depth < warn_depth:
            return
        if not self._should_emit_alert(self._last_depth_alert_at, lane):
            return
        logger.warning(f"[EventEngine:{lane}] queue depth warning: depth={depth} >= {int(warn_depth)}")

    def _maybe_alert_backlog(self, lane: str, event_type: str, backlog_ms: float):
        warn_ms = self.profile_config["backlog_warn_ms"][lane]
        if backlog_ms < warn_ms:
            return
        if not self._should_emit_alert(self._last_backlog_alert_at, lane):
            return
        logger.warning(
            f"[EventEngine:{lane}] backlog warning: event={event_type} backlog={backlog_ms:.1f}ms "
            f">= {warn_ms:.1f}ms"
        )

    def _maybe_alert_slow_handler(self, lane: str, event_type: str, handler_name: str, elapsed_ms: float):
        warn_ms = self.profile_config["handler_slow_ms"][lane]
        if elapsed_ms < warn_ms:
            return
        key = (lane, event_type, handler_name)
        if not self._should_emit_alert(self._last_slow_handler_alert_at, key):
            return
        logger.warning(
            f"[EventEngine:{lane}] slow handler: event={event_type} handler={handler_name} "
            f"elapsed={elapsed_ms:.1f}ms >= {warn_ms:.1f}ms"
        )

    def _should_emit_alert(self, registry: dict, key) -> bool:
        now = time.monotonic()
        interval_sec = self.profile_config["alert_interval_sec"]
        with self._alert_lock:
            last_at = registry.get(key, 0.0)
            if now - last_at < interval_sec:
                return False
            registry[key] = now
            return True

    def _handler_name(self, handler) -> str:
        return getattr(handler, "__qualname__", getattr(handler, "__name__", repr(handler)))

    def process_existing_events(self):
        for lane in self.HOT_LANES:
            while not self._queues[lane].empty():
                try:
                    dispatch_id, enqueued_at, event = self._queues[lane].get_nowait()
                    self._note_dequeue(lane)
                    self._process_lane(lane, event, enqueued_at)
                    self._handoff_to_cold(dispatch_id)
                except Empty:
                    break

        while not self._queues["cold"].empty():
            try:
                dispatch_id, enqueued_at, event = self._queues["cold"].get_nowait()
                self._note_dequeue("cold")
                self._process_lane("cold", event, enqueued_at)
                self._handoff_to_cold(dispatch_id)
            except Empty:
                break
