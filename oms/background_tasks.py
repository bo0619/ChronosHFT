"""Bounded, keyed background work for OMS safety tasks."""

from __future__ import annotations

import heapq
import itertools
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from event.type import LifecycleState
from infrastructure.logger import logger

from .component import OMSComponent


class BackgroundTaskRejected(RuntimeError):
    """Raised when bounded background work cannot be accepted."""


class BackgroundTaskCancelled(RuntimeError):
    """Marks delayed work cancelled by an orderly executor shutdown."""


class BackgroundTaskHandle:
    """Small join-compatible handle used by OMS shutdown barriers."""

    def __init__(self, key: str, name: str, lane: str):
        self.key = str(key)
        self.name = str(name)
        self.lane = str(lane)
        self._done = threading.Event()
        self._started = threading.Event()
        self._worker_ident: int | None = None
        self._result: Any = None
        self._exception: BaseException | None = None

    def _mark_started(self) -> None:
        self._worker_ident = threading.get_ident()
        self._started.set()

    def _mark_done(
        self,
        *,
        result: Any = None,
        exception: BaseException | None = None,
    ) -> None:
        self._result = result
        self._exception = exception
        self._done.set()

    def join(self, timeout: float | None = None) -> None:
        self._done.wait(timeout)

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    def is_alive(self) -> bool:
        return not self._done.is_set()

    def is_current(self) -> bool:
        return self._worker_ident == threading.get_ident()

    def done(self) -> bool:
        return self._done.is_set()

    def exception(self) -> BaseException | None:
        return self._exception if self._done.is_set() else None

    def result(self, timeout: float | None = None):
        if not self._done.wait(timeout):
            raise TimeoutError(f"background task {self.key!r} did not finish")
        if self._exception is not None:
            raise self._exception
        return self._result


@dataclass(frozen=True)
class BackgroundTaskSubmission:
    accepted: bool
    created: bool
    handle: BackgroundTaskHandle | None
    reason: str = ""

    def __bool__(self) -> bool:
        return self.accepted


@dataclass(frozen=True)
class _Task:
    handle: BackgroundTaskHandle
    callback: Callable
    args: tuple
    kwargs: dict
    lane: str
    run_at: float = 0.0


class OMSBackgroundTaskExecutor:
    """Fixed workers, bounded admission and keyed task coalescing.

    Safety work uses dedicated workers so reconciliation or history queries
    cannot starve safety controls. Dead-man cancellation has a separate
    emergency worker and admission reserve. Each lane has its own scheduler,
    and every delayed task counts against a fixed per-lane admission bound.
    """

    DEFAULT_LANE = "default"
    SAFETY_LANE = "safety"
    EMERGENCY_LANE = "emergency"
    LANES = frozenset({DEFAULT_LANE, SAFETY_LANE, EMERGENCY_LANE})

    # These keys must remain runnable even when ordinary safety work is stuck
    # in a venue call. Callers can also request EMERGENCY_LANE explicitly.
    _EMERGENCY_KEYS = frozenset({"dms:safety-cancel"})
    _EMERGENCY_PREFIXES = (
        "emergency:",
        "journal-failure:",
        "shutdown-cancel:",
    )
    _AUTO_SUCCESSOR_PREFIXES = (
        "cancel-retry:",
        "cancel-all-retry:",
    )

    def __init__(
        self,
        *,
        max_workers: int = 8,
        safety_workers: int = 2,
        queue_capacity: int = 64,
        safety_queue_capacity: int = 16,
        emergency_queue_capacity: int = 4,
        max_pending_tasks: int | None = None,
        thread_name_prefix: str = "ChronosOMSWorker",
        error_handler: Callable[[str, str, BaseException], None] | None = None,
    ):
        max_workers = int(max_workers)
        safety_workers = int(safety_workers)
        queue_capacity = int(queue_capacity)
        safety_queue_capacity = int(safety_queue_capacity)
        emergency_queue_capacity = int(emergency_queue_capacity)
        if max_workers < 2:
            raise ValueError("max_workers must be at least two")
        if not 1 <= safety_workers < max_workers:
            raise ValueError(
                "safety_workers must reserve at least one but not all workers"
            )
        if (
            queue_capacity <= 0
            or safety_queue_capacity <= 0
            or emergency_queue_capacity <= 0
        ):
            raise ValueError("background queue capacities must be positive")

        default_pending_bound = (
            max_workers + queue_capacity + safety_queue_capacity
        )
        if max_pending_tasks is None:
            max_pending_tasks = default_pending_bound
        max_pending_tasks = int(max_pending_tasks)
        if max_pending_tasks < max_workers:
            raise ValueError("max_pending_tasks must cover every worker")

        self.max_workers = max_workers
        self.safety_workers = safety_workers
        self.queue_capacity = queue_capacity
        self.safety_queue_capacity = safety_queue_capacity
        self.emergency_queue_capacity = emergency_queue_capacity
        self.max_pending_tasks = max_pending_tasks
        self.thread_name_prefix = str(
            thread_name_prefix or "ChronosOMSWorker"
        )
        self._error_handler = error_handler
        self._queues: dict[str, queue.Queue[_Task]] = {
            self.DEFAULT_LANE: queue.Queue(maxsize=queue_capacity),
            self.SAFETY_LANE: queue.Queue(maxsize=safety_queue_capacity),
            self.EMERGENCY_LANE: queue.Queue(
                maxsize=emergency_queue_capacity
            ),
        }
        self._condition = threading.Condition(threading.RLock())
        self._scheduled: dict[str, list[tuple[float, int, _Task]]] = {
            lane: [] for lane in self.LANES
        }
        self._serial = itertools.count(1)
        self._active_by_key: dict[str, BackgroundTaskHandle] = {}
        self._rerun_by_key: dict[str, _Task] = {}
        self._accepting = True
        self._closing = False

        default_workers = max_workers - safety_workers
        emergency_workers = 1
        general_safety_workers = safety_workers - emergency_workers
        self.default_workers = default_workers
        self.general_safety_workers = general_safety_workers
        self.emergency_workers = emergency_workers

        # Partition the total pending bound so ordinary work cannot consume
        # the slots required to admit safety and emergency work.
        emergency_capacity = emergency_workers + emergency_queue_capacity
        safety_capacity = (
            general_safety_workers + safety_queue_capacity
            if general_safety_workers > 0
            else 0
        )
        max_emergency_limit = (
            max_pending_tasks - default_workers - general_safety_workers
        )
        emergency_limit = min(
            emergency_capacity,
            max(1, max_emergency_limit),
        )
        remaining_after_emergency = max_pending_tasks - emergency_limit
        safety_limit = min(
            safety_capacity,
            max(0, remaining_after_emergency - default_workers),
        )
        default_limit = (
            max_pending_tasks - emergency_limit - safety_limit
        )
        self._pending_limits = {
            self.DEFAULT_LANE: default_limit,
            self.SAFETY_LANE: safety_limit,
            self.EMERGENCY_LANE: emergency_limit,
        }

        self._workers = [
            threading.Thread(
                target=self._worker_loop,
                args=(self.DEFAULT_LANE,),
                daemon=True,
                name=f"{self.thread_name_prefix}-Default-{index + 1}",
            )
            for index in range(default_workers)
        ]
        self._workers.extend(
            threading.Thread(
                target=self._worker_loop,
                args=(self.SAFETY_LANE,),
                daemon=True,
                name=f"{self.thread_name_prefix}-Safety-{index + 1}",
            )
            for index in range(general_safety_workers)
        )
        self._workers.extend(
            threading.Thread(
                target=self._worker_loop,
                args=(self.EMERGENCY_LANE,),
                daemon=True,
                name=(
                    f"{self.thread_name_prefix}-Emergency-{index + 1}"
                ),
            )
            for index in range(emergency_workers)
        )
        self._schedulers = [
            threading.Thread(
                target=self._scheduler_loop,
                args=(lane,),
                daemon=True,
                name=(
                    f"{self.thread_name_prefix}-"
                    f"{lane.title()}-Scheduler"
                ),
            )
            for lane in (
                self.DEFAULT_LANE,
                self.SAFETY_LANE,
                self.EMERGENCY_LANE,
            )
        ]
        for worker in self._workers:
            worker.start()
        for scheduler in self._schedulers:
            scheduler.start()

    def _effective_lane(self, key: str, requested_lane: str) -> str:
        if (
            key in self._EMERGENCY_KEYS
            or key.startswith(self._EMERGENCY_PREFIXES)
        ):
            return self.EMERGENCY_LANE
        if (
            requested_lane == self.SAFETY_LANE
            and self.general_safety_workers <= 0
        ):
            # A two-worker configuration reserves one worker for default work
            # and one for emergency cancellation. Other safety work remains
            # bounded on the default lane instead of entering an unserved
            # queue.
            return self.DEFAULT_LANE
        return requested_lane

    def _pending_count_locked(self, lane: str | None = None) -> int:
        active = (
            len(self._active_by_key)
            if lane is None
            else sum(
                1
                for handle in self._active_by_key.values()
                if handle.lane == lane
            )
        )
        reruns = (
            len(self._rerun_by_key)
            if lane is None
            else sum(
                1
                for task in self._rerun_by_key.values()
                if task.lane == lane
            )
        )
        return active + reruns

    def _admission_rejection_locked(self, lane: str) -> str:
        if self._pending_count_locked(lane) >= self._pending_limits[lane]:
            return f"background_{lane}_pending_limit"
        if self._pending_count_locked() >= self.max_pending_tasks:
            return "background_pending_limit"
        return ""

    def _report_error(
        self,
        handle: BackgroundTaskHandle,
        exc: BaseException,
    ) -> None:
        if self._error_handler is None:
            return
        try:
            self._error_handler(handle.key, handle.name, exc)
        except BaseException:
            pass

    def _finish_handle(
        self,
        handle: BackgroundTaskHandle,
        *,
        result: Any = None,
        exception: BaseException | None = None,
    ) -> _Task | None:
        successor = None
        with self._condition:
            # Publish completion and transfer the keyed successor atomically.
            # Otherwise a submitter can observe done=True and overtake a rerun
            # which was already accepted for this key.
            handle._mark_done(result=result, exception=exception)
            if self._active_by_key.get(handle.key) is handle:
                rerun = self._rerun_by_key.pop(handle.key, None)
                if rerun is None or self._closing:
                    self._active_by_key.pop(handle.key, None)
                    if rerun is not None:
                        cancelled = BackgroundTaskCancelled(
                            "background executor is closing"
                        )
                        rerun.handle._mark_done(exception=cancelled)
                else:
                    self._active_by_key[handle.key] = rerun.handle
                    if rerun.run_at > time.perf_counter():
                        # Delayed successors return to the lane scheduler
                        # without another admission decision. Immediate work
                        # stays on this worker to preserve key serialization.
                        heapq.heappush(
                            self._scheduled[rerun.lane],
                            (
                                rerun.run_at,
                                next(self._serial),
                                rerun,
                            ),
                        )
                    else:
                        successor = rerun
            self._condition.notify_all()
        return successor

    def _worker_loop(self, lane: str) -> None:
        work_queue = self._queues[lane]
        while True:
            try:
                task = work_queue.get(timeout=0.1)
            except queue.Empty:
                with self._condition:
                    if self._closing and work_queue.empty():
                        return
                continue

            with self._condition:
                self._condition.notify_all()
            try:
                current_task = task
                while current_task is not None:
                    current_task.handle._mark_started()
                    try:
                        result = current_task.callback(
                            *current_task.args,
                            **current_task.kwargs,
                        )
                    except BaseException as exc:
                        self._report_error(current_task.handle, exc)
                        current_task = self._finish_handle(
                            current_task.handle,
                            exception=exc,
                        )
                    else:
                        current_task = self._finish_handle(
                            current_task.handle,
                            result=result,
                        )
            finally:
                work_queue.task_done()

    def _cancel_task_locked(self, task: _Task, reason: str) -> None:
        if self._active_by_key.get(task.handle.key) is task.handle:
            self._active_by_key.pop(task.handle.key, None)
        task.handle._mark_done(exception=BackgroundTaskCancelled(reason))

    def _cancel_scheduled_lane_locked(self, lane: str) -> None:
        scheduled = [item[2] for item in self._scheduled[lane]]
        self._scheduled[lane].clear()
        for task in scheduled:
            self._cancel_task_locked(
                task,
                f"delayed background task {task.handle.key!r} cancelled",
            )
        self._condition.notify_all()

    def _cancel_reruns_locked(self) -> None:
        reruns = list(self._rerun_by_key.values())
        self._rerun_by_key.clear()
        for task in reruns:
            task.handle._mark_done(
                exception=BackgroundTaskCancelled(
                    f"background rerun {task.handle.key!r} cancelled"
                )
            )
        if reruns:
            self._condition.notify_all()

    def _scheduler_loop(self, lane: str) -> None:
        scheduled = self._scheduled[lane]
        target_queue = self._queues[lane]
        while True:
            with self._condition:
                while not scheduled and not self._closing:
                    self._condition.wait(timeout=0.1)
                if self._closing:
                    self._cancel_scheduled_lane_locked(lane)
                    return

                run_at, _serial, task = scheduled[0]
                remaining = run_at - time.perf_counter()
                if remaining > 0.0:
                    self._condition.wait(timeout=remaining)
                    continue
                heapq.heappop(scheduled)

                while True:
                    if self._closing:
                        self._cancel_task_locked(
                            task,
                            "background executor is closing",
                        )
                        self._cancel_scheduled_lane_locked(lane)
                        return
                    try:
                        target_queue.put_nowait(task)
                    except queue.Full:
                        self._condition.wait(timeout=0.05)
                        continue
                    self._condition.notify_all()
                    break

    def submit(
        self,
        key: str,
        callback: Callable,
        *args,
        name: str = "",
        lane: str = DEFAULT_LANE,
        delay_sec: float = 0.0,
        resubmit_after_current: bool = False,
        **kwargs,
    ) -> BackgroundTaskSubmission:
        key = str(key or "").strip()
        lane = str(lane or self.DEFAULT_LANE).strip().lower()
        if not key:
            raise ValueError("background task key is required")
        if not callable(callback):
            raise TypeError("background task callback must be callable")
        if lane not in self.LANES:
            raise ValueError(f"unsupported background task lane: {lane}")
        lane = self._effective_lane(key, lane)
        task_name = str(name or key)
        delay_sec = max(0.0, float(delay_sec or 0.0))
        run_at = (
            time.perf_counter() + delay_sec
            if delay_sec > 0.0
            else 0.0
        )

        with self._condition:
            if not self._accepting:
                return BackgroundTaskSubmission(
                    False,
                    False,
                    None,
                    "background_executor_closed",
                )
            existing = self._active_by_key.get(key)
            if existing is not None and not existing.done():
                auto_successor = bool(
                    existing.is_current()
                    and key.startswith(self._AUTO_SUCCESSOR_PREFIXES)
                )
                if not (resubmit_after_current or auto_successor):
                    return BackgroundTaskSubmission(
                        True,
                        False,
                        existing,
                        "background_task_coalesced",
                    )
                if existing.lane != lane:
                    return BackgroundTaskSubmission(
                        False,
                        False,
                        None,
                        "background_key_lane_conflict",
                    )
                existing_rerun = self._rerun_by_key.get(key)
                if existing_rerun is not None:
                    return BackgroundTaskSubmission(
                        True,
                        False,
                        existing_rerun.handle,
                        "background_rerun_coalesced",
                    )
                admission_rejection = self._admission_rejection_locked(lane)
                if admission_rejection:
                    return BackgroundTaskSubmission(
                        False,
                        False,
                        None,
                        admission_rejection,
                    )
                handle = BackgroundTaskHandle(key, task_name, lane)
                self._rerun_by_key[key] = _Task(
                    handle,
                    callback,
                    tuple(args),
                    dict(kwargs),
                    lane,
                    run_at,
                )
                return BackgroundTaskSubmission(
                    True,
                    True,
                    handle,
                    "background_rerun_queued",
                )

            admission_rejection = self._admission_rejection_locked(lane)
            if admission_rejection:
                return BackgroundTaskSubmission(
                    False,
                    False,
                    None,
                    admission_rejection,
                )

            handle = BackgroundTaskHandle(key, task_name, lane)
            task = _Task(
                handle,
                callback,
                tuple(args),
                dict(kwargs),
                lane,
                run_at,
            )
            if delay_sec <= 0.0:
                try:
                    self._queues[lane].put_nowait(task)
                except queue.Full:
                    return BackgroundTaskSubmission(
                        False,
                        False,
                        None,
                        f"background_{lane}_queue_full",
                    )
            else:
                heapq.heappush(
                    self._scheduled[lane],
                    (
                        run_at,
                        next(self._serial),
                        task,
                    ),
                )
            self._active_by_key[key] = handle
            self._condition.notify_all()
            return BackgroundTaskSubmission(True, True, handle)

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        deadline = (
            None
            if timeout is None
            else time.perf_counter() + max(0.0, float(timeout))
        )
        with self._condition:
            while self._active_by_key or self._rerun_by_key:
                if deadline is None:
                    self._condition.wait(timeout=0.1)
                    continue
                remaining = deadline - time.perf_counter()
                if remaining <= 0.0:
                    return False
                self._condition.wait(timeout=min(0.1, remaining))
            return True

    def shutdown(self, timeout: float | None = None) -> bool:
        deadline = (
            None
            if timeout is None
            else time.perf_counter() + max(0.0, float(timeout))
        )
        with self._condition:
            self._accepting = False
            self._closing = True
            self._cancel_reruns_locked()
            self._condition.notify_all()

        threads = [*self._schedulers, *self._workers]
        current_thread = threading.current_thread()
        for thread in threads:
            if thread is current_thread:
                continue
            remaining = (
                None
                if deadline is None
                else max(0.0, deadline - time.perf_counter())
            )
            thread.join(remaining)
        with self._condition:
            drained = bool(
                not self._active_by_key
                and not self._rerun_by_key
                and all(
                    not scheduled
                    for scheduled in self._scheduled.values()
                )
                and all(
                    work_queue.empty()
                    for work_queue in self._queues.values()
                )
            )
        return bool(
            drained and all(not thread.is_alive() for thread in threads)
        )

    def snapshot(self) -> dict:
        with self._condition:
            active = {
                key: {
                    "name": handle.name,
                    "lane": handle.lane,
                    "started": handle._started.is_set(),
                    "done": handle.done(),
                    "rerun": key in self._rerun_by_key,
                }
                for key, handle in self._active_by_key.items()
            }
            return {
                "accepting": self._accepting,
                "closing": self._closing,
                "max_workers": self.max_workers,
                "safety_workers": self.safety_workers,
                "general_safety_workers": self.general_safety_workers,
                "emergency_workers": self.emergency_workers,
                "queue_capacity": self.queue_capacity,
                "safety_queue_capacity": self.safety_queue_capacity,
                "emergency_queue_capacity": (
                    self.emergency_queue_capacity
                ),
                "max_pending_tasks": self.max_pending_tasks,
                "pending_limits": dict(self._pending_limits),
                "pending": {
                    lane: self._pending_count_locked(lane)
                    for lane in self.LANES
                },
                "queued": {
                    lane: work_queue.qsize()
                    for lane, work_queue in self._queues.items()
                },
                "scheduled": sum(
                    len(scheduled)
                    for scheduled in self._scheduled.values()
                ),
                "scheduled_by_lane": {
                    lane: len(scheduled)
                    for lane, scheduled in self._scheduled.items()
                },
                "reruns": len(self._rerun_by_key),
                "active": active,
            }


class OMSBackgroundTaskManager(OMSComponent):
    """Own OMS admission policy and fail-closed handling for background work."""

    def _latch_background_task_failure(
        self,
        key: str,
        reason: str,
    ) -> None:
        failure_reason = (
            f"background_task_unavailable:{str(key or 'unknown')}:"
            f"{str(reason or 'rejected')}"
        )
        with self.lock:
            self._background_task_rejection_count += 1
            self._close_outbound_gate_locked(
                failure_reason,
                hold="background_task_failure",
            )
            if self.state != LifecycleState.HALTED:
                self._lifecycle_generation += 1
            self.state = LifecycleState.HALTED
            self.manual_rearm_required = True
            self.last_halt_reason = failure_reason
            self.last_freeze_reason = ""
            self._sync_capability_mode(failure_reason)
        logger.critical(f"[OMS] {failure_reason}")
        try:
            self._audit(
                "background_task_failure",
                task_key=key,
                reason=reason,
                rejection_count=self._background_task_rejection_count,
            )
        except Exception as exc:
            logger.critical(
                "[OMS] Could not persist background task failure: "
                f"{type(exc).__name__}:{exc}"
            )

    def _on_background_task_error(
        self,
        key: str,
        name: str,
        exc: BaseException,
    ) -> None:
        self._latch_background_task_failure(
            key,
            f"{name}:{type(exc).__name__}:{exc}",
        )

    def _submit_background_task(
        self,
        key: str,
        callback,
        *args,
        name: str = "",
        safety: bool = False,
        delay_sec: float = 0.0,
        resubmit_after_current: bool = False,
        fail_closed: bool = True,
        **kwargs,
    ):
        lane = (
            OMSBackgroundTaskExecutor.SAFETY_LANE
            if safety
            else OMSBackgroundTaskExecutor.DEFAULT_LANE
        )
        submission = self._background_tasks.submit(
            key,
            callback,
            *args,
            name=name,
            lane=lane,
            delay_sec=delay_sec,
            resubmit_after_current=resubmit_after_current,
            **kwargs,
        )
        if submission.accepted:
            return submission.handle
        if fail_closed:
            self._latch_background_task_failure(key, submission.reason)
        else:
            logger.error(
                f"[OMS] Background task rejected key={key}: "
                f"{submission.reason}"
            )
        return None

    def get_background_task_snapshot(self) -> dict:
        snapshot = self._background_tasks.snapshot()
        snapshot["rejection_count"] = self._background_task_rejection_count
        return snapshot
