import logging
import os
import queue
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

from infrastructure.external_alerts import redact_alert_text


class AsyncLogger:
    """Bounded asynchronous file, console, dashboard, and alert logger."""

    _instance = None
    _LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    _HIGH_PRIORITY = {"WARNING", "ERROR", "CRITICAL"}

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "initialized"):
            return
        self.initialized = True

        self.active = False
        self.queue_capacity = 4096
        self.queue = queue.Queue(maxsize=self.queue_capacity)
        self.worker_thread = None
        self.logger = logging.getLogger("HFT_Engine")
        self.logger.setLevel(logging.DEBUG)
        self.minimum_level = logging.DEBUG
        self.minimum_level_name = "DEBUG"
        self.close_timeout_sec = 5.0
        self.ui_callback = None
        self.alert_callback = None
        self._metrics_lock = threading.Lock()
        self._dropped_by_level = {level: 0 for level in self._LEVELS}
        self._last_drop_at = 0.0
        self._last_overflow_notice_at = 0.0

    def init_logging(self, config: dict):
        if self.active:
            return
        system_config = config.get("system", {})
        log_path = system_config.get("log_path", "logs")
        level_name = str(
            system_config.get("log_level", "INFO") or "INFO"
        ).upper()
        if level_name not in self._LEVELS:
            raise ValueError(f"Unsupported system.log_level: {level_name}")
        self.minimum_level_name = level_name
        self.minimum_level = getattr(logging, level_name)
        self.close_timeout_sec = max(
            1.0,
            float(system_config.get("log_close_timeout_sec", 5.0) or 5.0),
        )
        configured_capacity = max(
            1,
            int(system_config.get("log_queue_capacity", 4096) or 4096),
        )
        self._resize_queue_before_start(configured_capacity)

        os.makedirs(log_path, exist_ok=True)
        max_bytes = system_config.get("log_max_bytes", 32 * 1024 * 1024)
        backup_count = system_config.get("log_backup_count", 7)
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 1024 * 1024
        ):
            raise ValueError("system.log_max_bytes must be at least 1 MiB")
        if (
            isinstance(backup_count, bool)
            or not isinstance(backup_count, int)
            or not 1 <= backup_count <= 100
        ):
            raise ValueError(
                "system.log_backup_count must be an integer between 1 and 100"
            )
        file_name = os.path.join(
            log_path,
            "hft_trading.log",
        )
        file_handler = RotatingFileHandler(
            file_name,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(self.minimum_level)
        formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s"
        )
        file_handler.setFormatter(formatter)
        self.logger.addHandler(file_handler)

        if system_config.get("log_console", False):
            stream_handler = logging.StreamHandler(sys.stdout)
            stream_handler.setFormatter(formatter)
            self.logger.addHandler(stream_handler)

        self.active = True
        self.worker_thread = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name="AsyncLogger",
        )
        self.worker_thread.start()
        self.info("Async Logger Initialized. (UI Callback support added)")

    def set_ui_callback(self, callback):
        self.ui_callback = callback

    def set_alert_callback(self, callback):
        """Register a non-blocking WARNING+ sink executed on the worker."""
        self.alert_callback = callback

    def _worker_loop(self):
        while self.active or not self.queue.empty():
            record = None
            try:
                record = self.queue.get(timeout=1.0)
                level, message = record
                log_method = getattr(self.logger, level.lower(), None)
                if callable(log_method):
                    log_method(message)

                if self.ui_callback:
                    try:
                        self.ui_callback(f"[{level}] {message}")
                    except Exception:
                        pass

                if self.alert_callback and level in self._HIGH_PRIORITY:
                    try:
                        self.alert_callback(level, message)
                    except Exception:
                        pass
            except queue.Empty:
                pass
            except Exception as exc:
                print(
                    f"Logger Internal Error: {type(exc).__name__}",
                    file=sys.stderr,
                    flush=True,
                )
            finally:
                if record is not None:
                    self.queue.task_done()

    def flush(self, timeout_sec: float = 1.0) -> bool:
        """Wait boundedly until all records already queued are dispatched."""
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        with self.queue.all_tasks_done:
            while self.queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return False
                self.queue.all_tasks_done.wait(timeout=remaining)
        return True

    def stop(self) -> bool:
        if self.active:
            self.info("Logger stopping...")
        self.active = False
        if self.worker_thread:
            self.worker_thread.join(timeout=self.close_timeout_sec)
        stopped = not self.worker_thread or not self.worker_thread.is_alive()
        if not stopped:
            return False
        for handler in list(self.logger.handlers):
            try:
                handler.flush()
                handler.close()
            finally:
                self.logger.removeHandler(handler)
        self.worker_thread = None
        return bool(stopped and self.queue.empty())

    def get_metrics_snapshot(self) -> dict:
        with self._metrics_lock:
            return {
                "active": bool(self.active),
                "queue_depth": self.queue.qsize(),
                "queue_capacity": self.queue_capacity,
                "minimum_level": self.minimum_level_name,
                "dropped_total": sum(self._dropped_by_level.values()),
                "dropped_by_level": dict(self._dropped_by_level),
                "last_drop_at": self._last_drop_at,
            }

    def _resize_queue_before_start(self, capacity: int) -> None:
        capacity = max(1, int(capacity))
        if self.queue.maxsize == capacity:
            self.queue_capacity = capacity
            return

        pending = []
        while True:
            try:
                pending.append(self.queue.get_nowait())
            except queue.Empty:
                break
            else:
                self.queue.task_done()
        retained = pending[-capacity:]
        for level, _message in pending[:-capacity]:
            self._record_drop(level, emit_notice=False)

        self.queue_capacity = capacity
        self.queue = queue.Queue(maxsize=capacity)
        for record in retained:
            self.queue.put_nowait(record)

    def _enqueue(self, level: str, message) -> bool:
        level = str(level or "INFO").upper()
        if level not in self._LEVELS:
            level = "INFO"
        if getattr(logging, level) < self.minimum_level:
            return True
        record = (level, redact_alert_text(message, 16384))
        try:
            self.queue.put_nowait(record)
            return True
        except queue.Full:
            pass

        if level in self._HIGH_PRIORITY:
            try:
                evicted_level, _evicted_message = self.queue.get_nowait()
            except queue.Empty:
                pass
            else:
                self.queue.task_done()
                self._record_drop(evicted_level)
            try:
                self.queue.put_nowait(record)
                return True
            except queue.Full:
                pass

        self._record_drop(level)
        return False

    def _record_drop(self, level: str, *, emit_notice: bool = True) -> None:
        now = time.time()
        with self._metrics_lock:
            normalized = str(level or "INFO").upper()
            if normalized not in self._dropped_by_level:
                normalized = "INFO"
            self._dropped_by_level[normalized] += 1
            self._last_drop_at = now
            should_notice = bool(
                emit_notice
                and now - self._last_overflow_notice_at >= 5.0
            )
            if should_notice:
                self._last_overflow_notice_at = now
        if should_notice:
            print(
                "AsyncLogger queue full; log records are being dropped",
                file=sys.stderr,
                flush=True,
            )

    def info(self, message):
        return self._enqueue("INFO", message)

    def error(self, message):
        return self._enqueue("ERROR", message)

    def debug(self, message):
        return self._enqueue("DEBUG", message)

    def warning(self, message):
        return self._enqueue("WARNING", message)

    def critical(self, message):
        return self._enqueue("CRITICAL", message)


logger = AsyncLogger()
