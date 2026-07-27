from collections import deque
from threading import Condition, Thread
import time

from infrastructure.logger import logger


class StrategyRuntime:
    def __init__(
        self,
        strategy,
        config=None,
        start_thread=True,
        failure_callback=None,
    ):
        self.strategy = strategy
        self.config = config or {}
        if failure_callback is not None and not callable(failure_callback):
            raise TypeError("strategy runtime failure callback must be callable")
        self.failure_callback = failure_callback
        self.queue_warn_depth = int(self.config.get("queue_warn_depth", 100))
        self.slow_handler_ms = float(self.config.get("slow_handler_ms", 100.0))
        self.alert_interval_sec = float(self.config.get("alert_interval_sec", 5.0))
        self.shutdown_timeout_sec = max(
            0.0,
            float(self.config.get("shutdown_timeout_sec", 5.0) or 0.0),
        )

        self._condition = Condition()
        self._control_queue = deque()
        self._market_queue = deque()
        self._pending_market = {}
        self._active = False
        self._thread = None
        self._async_stop_thread = None
        self._async_stop_result = None
        self._async_stop_error = None
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
            "handler_error_count": 0,
            "async_error_count": 0,
            "last_error_at": 0.0,
            "last_error_kind": "",
            "last_error_message": "",
        }
        if start_thread:
            self.start()

    def start(self):
        if self._active:
            return
        with self._condition:
            self._rebase_pending_work_locked(time.perf_counter())
            self._active = True
            self._async_stop_thread = None
            self._async_stop_result = None
            self._async_stop_error = None
        if self._thread is None or not self._thread.is_alive():
            self._thread = Thread(
                target=self._run,
                daemon=True,
                name=f"StrategyRuntime-{getattr(self.strategy, 'name', 'worker')}",
            )
            self._thread.start()
        logger.info(f"[StrategyRuntime] started for {getattr(self.strategy, 'name', 'strategy')}")

    def _rebase_pending_work_locked(self, started_at: float) -> None:
        """Treat pre-start state snapshots as startup work, not live backlog."""
        self._control_queue = deque(
            (kind, started_at, payload)
            for kind, _enqueued_at, payload in self._control_queue
        )
        self._pending_market = {
            key: (started_at, payload)
            for key, (_enqueued_at, payload) in self._pending_market.items()
        }

    def stop(self, timeout_sec=None):
        self._active = False
        with self._condition:
            self._condition.notify_all()
        timeout = (
            self.shutdown_timeout_sec
            if timeout_sec is None
            else max(0.0, float(timeout_sec or 0.0))
        )
        deadline = time.perf_counter() + timeout
        if self._thread and self._thread.is_alive():
            self._thread.join(
                timeout=max(0.0, deadline - time.perf_counter())
            )
        if self._thread and self._thread.is_alive():
            logger.critical(
                "[StrategyRuntime] worker did not stop before "
                f"timeout={timeout:.3f}s"
            )
            return False
        stop_async_workers = getattr(self.strategy, "stop_async_workers", None)
        if callable(stop_async_workers):
            if self._async_stop_thread is None:
                def stop_async():
                    try:
                        self._async_stop_result = stop_async_workers()
                    except BaseException as exc:
                        self._async_stop_error = exc

                self._async_stop_thread = Thread(
                    target=stop_async,
                    daemon=True,
                    name=(
                        "StrategyAsyncStop-"
                        f"{getattr(self.strategy, 'name', 'strategy')}"
                    ),
                )
                self._async_stop_thread.start()
            self._async_stop_thread.join(
                timeout=max(0.0, deadline - time.perf_counter())
            )
            if self._async_stop_thread.is_alive():
                logger.critical(
                    "[StrategyRuntime] strategy async workers did not stop "
                    f"before timeout={timeout:.3f}s"
                )
                return False
            if self._async_stop_error is not None:
                raise self._async_stop_error
            if self._async_stop_result is False:
                logger.critical(
                    "[StrategyRuntime] strategy async workers did not stop"
                )
                return False
        logger.info(f"[StrategyRuntime] stopped for {getattr(self.strategy, 'name', 'strategy')}")
        return not self._thread or not self._thread.is_alive()

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
            snapshot["active"] = bool(self._active)
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
                self._pending_market[key] = (enqueued_at, payload)
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
        now = time.perf_counter()
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
            if block and not self._active:
                return None
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
            logger.error(
                f"[StrategyRuntime] handler failed {kind}: "
                f"{type(exc).__name__}:{exc}"
            )
            with self._condition:
                self._inflight = {"kind": "", "started_at": 0.0, "enqueued_at": 0.0}
            self._report_failure(
                kind=kind,
                payload=payload,
                handler=handler,
                error=exc,
                phase="handler",
            )
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
                now = time.perf_counter()
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
                self._report_failure(
                    kind="async_worker",
                    payload=None,
                    handler=poll,
                    error=exc,
                    phase="async_poll",
                )

    def _report_failure(
        self,
        *,
        kind: str,
        payload,
        handler,
        error: BaseException,
        phase: str,
    ) -> None:
        failed_at = time.time()
        message = f"{type(error).__name__}:{error}"
        with self._condition:
            counter = (
                "async_error_count"
                if phase.startswith("async")
                else "handler_error_count"
            )
            self._stats[counter] += 1
            self._stats["last_error_at"] = failed_at
            self._stats["last_error_kind"] = kind
            self._stats["last_error_message"] = message

        if self.failure_callback is None:
            logger.critical(
                "[StrategyRuntime] failure callback unavailable: "
                f"phase={phase} kind={kind}"
            )
            return
        try:
            self.failure_callback(
                {
                    "phase": phase,
                    "kind": kind,
                    "payload": payload,
                    "handler_name": getattr(
                        handler,
                        "__qualname__",
                        getattr(handler, "__name__", repr(handler)),
                    ),
                    "error": error,
                    "message": message,
                    "failed_at": failed_at,
                }
            )
        except BaseException as callback_error:
            logger.critical(
                "[StrategyRuntime] fail-closed callback raised: "
                f"{type(callback_error).__name__}:{callback_error}"
            )

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
