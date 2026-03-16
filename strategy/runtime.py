from collections import deque
from threading import Condition, Thread
import time

from infrastructure.logger import logger


class StrategyRuntime:
    def __init__(self, strategy, config=None, start_thread=True):
        self.strategy = strategy
        self.config = config or {}
        self.queue_warn_depth = int(self.config.get("queue_warn_depth", 100))
        self.slow_handler_ms = float(self.config.get("slow_handler_ms", 100.0))
        self.alert_interval_sec = float(self.config.get("alert_interval_sec", 5.0))

        self._condition = Condition()
        self._control_queue = deque()
        self._market_queue = deque()
        self._pending_market = {}
        self._active = False
        self._thread = None
        self._last_alert_at = 0.0
        self._inflight = {
            "kind": "",
            "started_at": 0.0,
            "enqueued_at": 0.0,
        }
        self._stats = {
            "control_depth": 0,
            "market_depth": 0,
            "max_control_depth": 0,
            "max_market_depth": 0,
            "coalesced_market_events": 0,
            "processed": 0,
            "last_kind": "",
            "last_wait_ms": 0.0,
            "max_wait_ms": 0.0,
            "last_handler_ms": 0.0,
            "max_handler_ms": 0.0,
            "slow_handler_count": 0,
        }
        if start_thread:
            self.start()

    def start(self):
        if self._active:
            return
        self._active = True
        if self._thread is None or not self._thread.is_alive():
            self._thread = Thread(
                target=self._run,
                daemon=True,
                name=f"StrategyRuntime-{getattr(self.strategy, 'name', 'worker')}",
            )
            self._thread.start()
        logger.info(f"[StrategyRuntime] started for {getattr(self.strategy, 'name', 'strategy')}")

    def stop(self):
        self._active = False
        with self._condition:
            self._condition.notify_all()
        if self._thread and self._thread.is_alive():
            self._thread.join()
        stop_async_workers = getattr(self.strategy, "stop_async_workers", None)
        if callable(stop_async_workers):
            stop_async_workers()
        logger.info(f"[StrategyRuntime] stopped for {getattr(self.strategy, 'name', 'strategy')}")

    def on_orderbook(self, orderbook):
        self._submit_market("orderbook", getattr(orderbook, "symbol", ""), orderbook)

    def on_market_trade(self, trade):
        self._submit_market("market_trade", getattr(trade, "symbol", ""), trade)

    def on_order(self, snapshot):
        self._submit_control("order", snapshot)

    def on_trade(self, trade):
        self._submit_control("trade", trade)

    def on_position(self, position):
        self._submit_control("position", position)

    def on_account_update(self, account):
        self._submit_control("account", account)

    def on_system_health(self, message):
        self._submit_control("system_health", message)

    def get_metrics_snapshot(self):
        with self._condition:
            snapshot = dict(self._stats)
            snapshot["control_depth"] = len(self._control_queue)
            snapshot["market_depth"] = len(self._market_queue)
            now = time.perf_counter()
            oldest_control_wait_ms = 0.0
            if self._control_queue:
                oldest_control_wait_ms = max(0.0, (now - self._control_queue[0][1]) * 1000.0)
            oldest_market_wait_ms = 0.0
            if self._market_queue:
                first_key = self._market_queue[0]
                first_seen_at = self._pending_market.get(first_key, (0.0, None))[0]
                if first_seen_at:
                    oldest_market_wait_ms = max(0.0, (now - first_seen_at) * 1000.0)
            inflight_ms = 0.0
            inflight_wait_ms = 0.0
            if self._inflight["started_at"]:
                inflight_ms = max(0.0, (now - self._inflight["started_at"]) * 1000.0)
            if self._inflight["enqueued_at"]:
                inflight_wait_ms = max(0.0, (now - self._inflight["enqueued_at"]) * 1000.0)
            snapshot["oldest_control_wait_ms"] = oldest_control_wait_ms
            snapshot["oldest_market_wait_ms"] = oldest_market_wait_ms
            snapshot["inflight_kind"] = self._inflight["kind"]
            snapshot["inflight_ms"] = inflight_ms
            snapshot["inflight_wait_ms"] = inflight_wait_ms
            async_metrics = self._get_async_worker_metrics()
            if async_metrics:
                snapshot["async_worker"] = async_metrics
            return snapshot

    def process_pending(self, max_items=None):
        processed = 0
        while True:
            if max_items is not None and processed >= max_items:
                break
            work = self._pop_next_work(block=False)
            if work is None:
                break
            self._execute(*work)
            processed += 1
        return processed

    def _submit_market(self, kind: str, symbol: str, payload):
        symbol = (symbol or "").upper()
        key = (kind, symbol)
        enqueued_at = time.perf_counter()
        with self._condition:
            if key in self._pending_market:
                first_seen_at, _ = self._pending_market[key]
                self._pending_market[key] = (first_seen_at, payload)
                self._stats["coalesced_market_events"] += 1
            else:
                self._pending_market[key] = (enqueued_at, payload)
                self._market_queue.append(key)
            self._refresh_depth_stats_locked()
            self._maybe_warn_backlog_locked()
            self._condition.notify()

    def _submit_control(self, kind: str, payload):
        enqueued_at = time.perf_counter()
        with self._condition:
            self._control_queue.append((kind, enqueued_at, payload))
            self._refresh_depth_stats_locked()
            self._maybe_warn_backlog_locked()
            self._condition.notify()

    def _refresh_depth_stats_locked(self):
        control_depth = len(self._control_queue)
        market_depth = len(self._market_queue)
        self._stats["control_depth"] = control_depth
        self._stats["market_depth"] = market_depth
        self._stats["max_control_depth"] = max(self._stats["max_control_depth"], control_depth)
        self._stats["max_market_depth"] = max(self._stats["max_market_depth"], market_depth)

    def _maybe_warn_backlog_locked(self):
        total_depth = len(self._control_queue) + len(self._market_queue)
        if total_depth < self.queue_warn_depth:
            return
        now = time.monotonic()
        if now - self._last_alert_at < self.alert_interval_sec:
            return
        self._last_alert_at = now
        logger.warning(
            f"[StrategyRuntime] backlog warning depth={total_depth} "
            f"(control={len(self._control_queue)} market={len(self._market_queue)})"
        )

    def _run(self):
        while self._active:
            work = self._pop_next_work(block=True)
            if work is None:
                self._poll_async_workers()
                continue
            self._execute(*work)

    def _pop_next_work(self, block: bool):
        with self._condition:
            while block and self._active and not self._control_queue and not self._market_queue:
                self._condition.wait(timeout=1.0)
            if not self._control_queue and not self._market_queue:
                return None
            if self._control_queue:
                kind, enqueued_at, payload = self._control_queue.popleft()
                self._refresh_depth_stats_locked()
                return kind, enqueued_at, payload

            key = self._market_queue.popleft()
            enqueued_at, payload = self._pending_market.pop(key, (time.perf_counter(), None))
            self._refresh_depth_stats_locked()
            return key[0], enqueued_at, payload

    def _execute(self, kind: str, enqueued_at: float, payload):
        if payload is None:
            return
        self._poll_async_workers()
        handler = self._resolve_handler(kind)
        if handler is None:
            return

        started_at = time.perf_counter()
        wait_ms = max(0.0, (started_at - enqueued_at) * 1000.0)
        with self._condition:
            self._inflight = {
                "kind": kind,
                "started_at": started_at,
                "enqueued_at": enqueued_at,
            }
        try:
            handler(payload)
        except Exception as exc:
            logger.error(f"[StrategyRuntime] handler failed {kind}: {exc}")
            with self._condition:
                self._inflight = {"kind": "", "started_at": 0.0, "enqueued_at": 0.0}
            return

        elapsed_ms = max(0.0, (time.perf_counter() - started_at) * 1000.0)
        with self._condition:
            self._inflight = {"kind": "", "started_at": 0.0, "enqueued_at": 0.0}
            self._stats["processed"] += 1
            self._stats["last_kind"] = kind
            self._stats["last_wait_ms"] = wait_ms
            self._stats["max_wait_ms"] = max(self._stats["max_wait_ms"], wait_ms)
            self._stats["last_handler_ms"] = elapsed_ms
            self._stats["max_handler_ms"] = max(self._stats["max_handler_ms"], elapsed_ms)
            if elapsed_ms >= self.slow_handler_ms:
                self._stats["slow_handler_count"] += 1
                now = time.monotonic()
                if now - self._last_alert_at >= self.alert_interval_sec:
                    self._last_alert_at = now
                    logger.warning(
                        f"[StrategyRuntime] slow handler kind={kind} "
                        f"elapsed={elapsed_ms:.1f}ms wait={wait_ms:.1f}ms"
                    )
        self._poll_async_workers()

    def _poll_async_workers(self):
        poll = getattr(self.strategy, "poll_async_workers", None)
        if callable(poll):
            try:
                poll()
            except Exception as exc:
                logger.error(f"[StrategyRuntime] async worker poll failed: {exc}")

    def _get_async_worker_metrics(self):
        get_metrics = getattr(self.strategy, "get_async_worker_metrics", None)
        if callable(get_metrics):
            try:
                return get_metrics() or {}
            except Exception:
                return {}
        return {}

    def _resolve_handler(self, kind: str):
        if kind == "orderbook":
            return getattr(self.strategy, "on_orderbook", None)
        if kind == "market_trade":
            return getattr(self.strategy, "on_market_trade", None)
        if kind == "order":
            return getattr(self.strategy, "on_order", None)
        if kind == "trade":
            return getattr(self.strategy, "on_trade", None)
        if kind == "position":
            return getattr(self.strategy, "on_position", None)
        if kind == "account":
            return getattr(self.strategy, "on_account_update", None)
        if kind == "system_health":
            return getattr(self.strategy, "on_system_health", None)
        return None
