from __future__ import annotations

import multiprocessing
import os
import queue
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
) -> int:
    source_buffer = buffers[data_type][symbol]
    if not source_buffer:
        return 0

    # Importing pandas/PyTables and all HDF5 work stays in the child process.
    # This prevents a batch flush from holding the trading process GIL.
    import pandas as pd

    batch = list(source_buffer)
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
                    )
                continue

            if kind == _FLUSH:
                _kind, request_id, symbol, data_type = command
                flushed = _flush_hdf_buffer(
                    save_path,
                    buffers,
                    symbol,
                    data_type,
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
                    )
                    written_records += _flush_hdf_buffer(
                        save_path,
                        buffers,
                        symbol,
                        "trade",
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
        queue_capacity: int = 50_000,
        close_timeout_sec: float = 30.0,
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
        self.flush_threshold = max(1, int(flush_threshold or 1))
        self.queue_capacity = max(
            self.flush_threshold,
            int(queue_capacity or self.flush_threshold),
        )
        self.close_timeout_sec = max(
            1.0,
            float(close_timeout_sec or 30.0),
        )
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
            f"queue_capacity={self.queue_capacity}"
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
                    self._flush_results[
                        str(payload.get("request_id", "") or "")
                    ] = bool(payload.get("ok", False))
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
        self._enqueue((_FLUSH, request_id, symbol, data_type))
        deadline = time.perf_counter() + max(0.0, float(timeout_sec))
        while time.perf_counter() < deadline:
            self._drain_status(wait_sec=0.05)
            with self._lock:
                if request_id in self._flush_results:
                    return self._flush_results.pop(request_id)
                if not self._healthy:
                    return False
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
