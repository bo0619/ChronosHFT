from __future__ import annotations

import math
import multiprocessing
import os
import queue
import shutil
import signal
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from event.type import (
    AggTradeData,
    Event,
    OrderBook,
    EVENT_AGG_TRADE,
    EVENT_ORDERBOOK,
)
from infrastructure.logger import logger


_ROW = "ROW"
_FLUSH = "FLUSH"
_STOP = "STOP"


def _isolate_recorder_console_interrupts() -> None:
    """Let only the parent process coordinate recorder shutdown."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _apply_process_niceness(requested_niceness: int) -> tuple[int | None, str]:
    """Lower the HDF5 writer priority where the platform supports it."""
    if requested_niceness <= 0:
        return None, "disabled"

    setpriority = getattr(os, "setpriority", None)
    priority_process = getattr(os, "PRIO_PROCESS", None)
    if callable(setpriority) and priority_process is not None:
        try:
            setpriority(priority_process, 0, requested_niceness)
            getpriority = getattr(os, "getpriority", None)
            applied = (
                getpriority(priority_process, 0)
                if callable(getpriority)
                else requested_niceness
            )
            return int(applied), "applied"
        except OSError as exc:
            return None, f"setpriority_failed:{type(exc).__name__}:{exc}"

    nice = getattr(os, "nice", None)
    if callable(nice):
        try:
            return int(nice(requested_niceness)), "applied"
        except OSError as exc:
            return None, f"nice_failed:{type(exc).__name__}:{exc}"
    return None, "unsupported_platform"


def _put_writer_status(status_queue, payload: dict) -> None:
    try:
        status_queue.put_nowait(dict(payload))
    except queue.Full:
        # Status is advisory while the process exit code remains authoritative.
        pass


def _flush_hdf_buffer(
    save_path: str,
    buffers: dict,
    symbol: str,
    data_type: str,
    min_free_bytes: int = 0,
) -> int:
    source_buffer = buffers[data_type][symbol]
    if not source_buffer:
        return 0

    # Importing pandas/PyTables and all HDF5 work stays in the child process.
    # This prevents a batch flush from holding the trading process GIL.
    import pandas as pd

    batch = list(source_buffer)
    if min_free_bytes > 0:
        reserved_bytes = max(8 * 1024 * 1024, len(batch) * 8192)
        free_bytes = int(shutil.disk_usage(save_path).free)
        if free_bytes - reserved_bytes < min_free_bytes:
            raise RuntimeError(
                "recorder_disk_reserve_exhausted:"
                f"free={free_bytes}:required={reserved_bytes}:"
                f"reserve={min_free_bytes}"
            )
    today = datetime.now().strftime("%Y%m%d")
    filename = Path(save_path) / f"{symbol}_{data_type}_{today}.h5"
    frame = pd.DataFrame(batch)
    write_options = {}
    if not filename.exists():
        write_options["min_itemsize"] = {"symbol": 64}
    frame.to_hdf(
        filename,
        key=data_type,
        mode="a",
        append=True,
        format="table",
        **write_options,
    )
    del source_buffer[: len(batch)]
    return len(batch)


def _recorder_writer_main(
    command_queue,
    status_queue,
    save_path: str,
    symbols: tuple[str, ...],
    flush_threshold: int,
    min_free_bytes: int = 0,
    process_niceness: int = 0,
) -> None:
    try:
        _isolate_recorder_console_interrupts()
    except BaseException as exc:
        _put_writer_status(
            status_queue,
            {
                "type": "fatal",
                "ok": False,
                "reason": f"signal_isolation_failed:{type(exc).__name__}:{exc}",
                "written_records": 0,
            },
        )
        raise
    if process_niceness > 0:
        applied_niceness, niceness_reason = _apply_process_niceness(
            process_niceness
        )
        _put_writer_status(
            status_queue,
            {
                "type": "startup",
                "ok": applied_niceness is not None,
                "requested_process_niceness": process_niceness,
                "applied_process_niceness": applied_niceness,
                "reason": niceness_reason,
            },
        )
    os.makedirs(save_path, exist_ok=True)
    buffers = {
        "depth": {symbol: [] for symbol in symbols},
        "trade": {symbol: [] for symbol in symbols},
    }
    written_records = 0
    try:
        while True:
            command = command_queue.get()
            kind = command[0]
            if kind == _ROW:
                _kind, data_type, symbol, row = command
                target = buffers[data_type][symbol]
                target.append(row)
                if len(target) >= flush_threshold:
                    written_records += _flush_hdf_buffer(
                        save_path,
                        buffers,
                        symbol,
                        data_type,
                        min_free_bytes,
                    )
                continue

            if kind == _FLUSH:
                _kind, request_id, symbol, data_type = command
                flushed = _flush_hdf_buffer(
                    save_path,
                    buffers,
                    symbol,
                    data_type,
                    min_free_bytes,
                )
                written_records += flushed
                _put_writer_status(
                    status_queue,
                    {
                        "type": "flush",
                        "request_id": request_id,
                        "ok": True,
                        "flushed": flushed,
                    },
                )
                continue

            if kind == _STOP:
                for symbol in symbols:
                    written_records += _flush_hdf_buffer(
                        save_path,
                        buffers,
                        symbol,
                        "depth",
                        min_free_bytes,
                    )
                    written_records += _flush_hdf_buffer(
                        save_path,
                        buffers,
                        symbol,
                        "trade",
                        min_free_bytes,
                    )
                _put_writer_status(
                    status_queue,
                    {
                        "type": "stopped",
                        "ok": True,
                        "written_records": written_records,
                    },
                )
                return

            raise RuntimeError(f"unsupported recorder command: {kind!r}")
    except BaseException as exc:
        _put_writer_status(
            status_queue,
            {
                "type": "fatal",
                "ok": False,
                "reason": f"{type(exc).__name__}:{exc}",
                "written_records": written_records,
            },
        )
        raise


class DataRecorder:
    """Non-blocking HDF5 recorder with an isolated writer process."""

    def __init__(
        self,
        engine,
        symbols: list,
        *,
        save_path: str = "storage",
        flush_threshold: int = 1000,
        queue_capacity: int = 8192,
        close_timeout_sec: float = 30.0,
        min_free_bytes: int = 0,
        process_niceness: int = 0,
        multiprocessing_context=None,
    ):
        self.engine = engine
        self.symbols = tuple(
            dict.fromkeys(
                str(symbol or "").strip().upper()
                for symbol in symbols
                if str(symbol or "").strip()
            )
        )
        self.save_path = str(save_path or "storage")
        if isinstance(flush_threshold, bool) or not isinstance(
            flush_threshold,
            int,
        ) or flush_threshold <= 0:
            raise ValueError("flush_threshold must be a positive integer")
        if (
            isinstance(queue_capacity, bool)
            or not isinstance(queue_capacity, int)
            or queue_capacity < flush_threshold
        ):
            raise ValueError(
                "queue_capacity must be an integer no smaller than "
                "flush_threshold"
            )
        if (
            isinstance(min_free_bytes, bool)
            or not isinstance(min_free_bytes, int)
            or min_free_bytes < 0
        ):
            raise ValueError("min_free_bytes must be a non-negative integer")
        if (
            isinstance(process_niceness, bool)
            or not isinstance(process_niceness, int)
            or process_niceness < 0
            or process_niceness > 19
        ):
            raise ValueError("process_niceness must be an integer from 0 to 19")
        try:
            parsed_close_timeout = float(close_timeout_sec)
        except (TypeError, ValueError) as exc:
            raise ValueError("close_timeout_sec must be positive") from exc
        if not math.isfinite(parsed_close_timeout) or parsed_close_timeout <= 0.0:
            raise ValueError("close_timeout_sec must be positive")
        self.flush_threshold = flush_threshold
        self.queue_capacity = queue_capacity
        self.close_timeout_sec = parsed_close_timeout
        self.min_free_bytes = min_free_bytes
        self.process_niceness = process_niceness
        self._symbol_set = frozenset(self.symbols)
        self._lock = threading.RLock()
        self._healthy = True
        self._failure_reason = ""
        self._failure_logged = False
        self._closing = False
        self._closed = False
        self._stop_sent = False
        self._enqueued_records = 0
        self._dropped_records = 0
        self._flush_results = {}
        self._pending_flush_requests = set()
        self._writer_status = {}

        os.makedirs(self.save_path, exist_ok=True)
        context = (
            multiprocessing_context
            or multiprocessing.get_context("spawn")
        )
        self._command_queue = context.Queue(
            maxsize=self.queue_capacity
        )
        self._status_queue = context.Queue(maxsize=128)
        self._writer_process = context.Process(
            target=_recorder_writer_main,
            args=(
                self._command_queue,
                self._status_queue,
                self.save_path,
                self.symbols,
                self.flush_threshold,
                self.min_free_bytes,
                self.process_niceness,
            ),
            name="ChronosDataRecorder",
            daemon=True,
        )
        self._writer_process.start()

        register_cold = getattr(engine, "register_cold", None)
        if callable(register_cold):
            register_cold(EVENT_ORDERBOOK, self.on_orderbook)
            register_cold(EVENT_AGG_TRADE, self.on_agg_trade)
        else:
            engine.register(EVENT_ORDERBOOK, self.on_orderbook)
            engine.register(EVENT_AGG_TRADE, self.on_agg_trade)

        logger.info(
            "[DataRecorder] Isolated HDF5 writer started "
            f"pid={self._writer_process.pid} symbols={len(self.symbols)} "
            f"queue_capacity={self.queue_capacity} "
            f"process_niceness={self.process_niceness}"
        )

    def on_orderbook(self, event: Event):
        orderbook: OrderBook = event.data
        symbol = str(getattr(orderbook, "symbol", "") or "").upper()
        if symbol not in self._symbol_set:
            return

        bids = list(orderbook.get_top_bids(5))
        asks = list(orderbook.get_top_asks(5))
        while len(bids) < 5:
            bids.append((0, 0))
        while len(asks) < 5:
            asks.append((0, 0))

        row = {
            "datetime": orderbook.datetime,
            "symbol": symbol,
        }
        for index in range(5):
            row[f"bid{index + 1}_p"] = bids[index][0]
            row[f"bid{index + 1}_v"] = bids[index][1]
            row[f"ask{index + 1}_p"] = asks[index][0]
            row[f"ask{index + 1}_v"] = asks[index][1]
        self._enqueue((_ROW, "depth", symbol, row))

    def on_agg_trade(self, event: Event):
        trade: AggTradeData = event.data
        symbol = str(getattr(trade, "symbol", "") or "").upper()
        if symbol not in self._symbol_set:
            return
        self._enqueue(
            (
                _ROW,
                "trade",
                symbol,
                {
                    "datetime": trade.datetime,
                    "symbol": symbol,
                    "price": trade.price,
                    "qty": trade.quantity,
                    "maker_is_buyer": trade.maker_is_buyer,
                },
            )
        )

    def _enqueue(self, command) -> None:
        self._drain_status()
        with self._lock:
            if self._closing or self._closed:
                raise RuntimeError("data recorder is closing")
            if not self._healthy:
                raise RuntimeError(
                    "data recorder unavailable: "
                    f"{self._failure_reason or 'writer process failed'}"
                )
            if not self._writer_process.is_alive():
                self._mark_failure_locked(
                    "writer_process_exited:"
                    f"{self._writer_process.exitcode}"
                )
                raise RuntimeError(self._failure_reason)
            try:
                self._command_queue.put_nowait(command)
            except queue.Full as exc:
                self._dropped_records += 1
                self._mark_failure_locked("writer_queue_full")
                raise RuntimeError(
                    "data recorder writer queue is full"
                ) from exc
            self._enqueued_records += 1

    def _mark_failure_locked(self, reason: str) -> None:
        self._healthy = False
        if not self._failure_reason:
            self._failure_reason = str(reason or "writer_failed")
        if not self._failure_logged:
            self._failure_logged = True
            logger.critical(
                "[DataRecorder] Recorder unavailable: "
                f"{self._failure_reason}"
            )

    def _drain_status(self, wait_sec: float = 0.0) -> None:
        deadline = time.perf_counter() + max(0.0, float(wait_sec or 0.0))
        while True:
            try:
                if wait_sec > 0.0:
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0.0:
                        return
                    payload = self._status_queue.get(
                        timeout=min(0.05, remaining)
                    )
                else:
                    payload = self._status_queue.get_nowait()
            except (queue.Empty, OSError, ValueError):
                return

            if not isinstance(payload, dict):
                continue
            message_type = str(payload.get("type", "") or "")
            with self._lock:
                self._writer_status = dict(payload)
                if message_type == "flush":
                    request_id = str(
                        payload.get("request_id", "") or ""
                    )
                    if request_id in self._pending_flush_requests:
                        self._flush_results[request_id] = bool(
                            payload.get("ok", False)
                        )
                elif message_type == "fatal":
                    self._mark_failure_locked(
                        str(payload.get("reason", "") or "writer_fatal")
                    )

    def flush(
        self,
        symbol,
        data_type,
        *,
        timeout_sec: float = 30.0,
    ) -> bool:
        symbol = str(symbol or "").strip().upper()
        data_type = str(data_type or "").strip().lower()
        if symbol not in self._symbol_set:
            return False
        if data_type not in {"depth", "trade"}:
            return False
        request_id = uuid.uuid4().hex
        with self._lock:
            self._pending_flush_requests.add(request_id)
        try:
            self._enqueue((_FLUSH, request_id, symbol, data_type))
        except Exception:
            with self._lock:
                self._pending_flush_requests.discard(request_id)
            raise
        deadline = time.perf_counter() + max(0.0, float(timeout_sec))
        while time.perf_counter() < deadline:
            self._drain_status(wait_sec=0.05)
            with self._lock:
                if request_id in self._flush_results:
                    result = self._flush_results.pop(request_id)
                    self._pending_flush_requests.discard(request_id)
                    return result
                if not self._healthy:
                    self._pending_flush_requests.discard(request_id)
                    return False
        with self._lock:
            self._pending_flush_requests.discard(request_id)
            self._flush_results.pop(request_id, None)
        return False

    def get_metrics_snapshot(self) -> dict:
        self._drain_status()
        with self._lock:
            process = self._writer_process
            return {
                "healthy": bool(
                    self._healthy
                    and process is not None
                    and (self._closed or process.is_alive())
                ),
                "failure_reason": self._failure_reason,
                "writer_pid": getattr(process, "pid", None),
                "writer_exitcode": getattr(process, "exitcode", None),
                "enqueued_records": self._enqueued_records,
                "dropped_records": self._dropped_records,
                "queue_depth": self._queue_depth(),
                "queue_capacity": self.queue_capacity,
                "flush_threshold": self.flush_threshold,
                "min_free_bytes": self.min_free_bytes,
                "process_niceness": self.process_niceness,
                "pending_flush_requests": len(
                    self._pending_flush_requests
                ),
                "closing": self._closing,
                "closed": self._closed,
                "writer_status": dict(self._writer_status),
            }

    def _queue_depth(self):
        try:
            return int(self._command_queue.qsize())
        except (NotImplementedError, OSError):
            return None

    def close(self):
        with self._lock:
            if self._closed:
                return True
            self._closing = True
            process = self._writer_process
            if process is None:
                self._mark_failure_locked("writer_process_missing")
                return False
            if not self._stop_sent and process.is_alive():
                try:
                    self._command_queue.put(
                        (_STOP,),
                        timeout=min(5.0, self.close_timeout_sec),
                    )
                    self._stop_sent = True
                except queue.Full:
                    self._mark_failure_locked(
                        "writer_queue_full_during_close"
                    )
                    return False

        if process.is_alive():
            process.join(timeout=self.close_timeout_sec)
        self._drain_status(wait_sec=0.5)

        with self._lock:
            if process.is_alive():
                self._mark_failure_locked("writer_close_timeout")
                return False
            if process.exitcode != 0:
                self._mark_failure_locked(
                    f"writer_exitcode:{process.exitcode}"
                )
                return False
            if not self._healthy:
                return False
            self._closed = True
            logger.info(
                "[DataRecorder] Recorder closed and all queued rows flushed"
            )

        try:
            self._command_queue.close()
            self._command_queue.join_thread()
            self._status_queue.close()
            self._status_queue.join_thread()
        except (OSError, ValueError):
            pass
        return True
