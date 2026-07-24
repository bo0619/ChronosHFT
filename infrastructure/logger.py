# file: infrastructure/logger.py

import os
import sys
import queue
import threading
import logging
import time
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

from infrastructure.external_alerts import redact_alert_text


class AsyncLogger:
    """
    高性能异步日志模块 (单例模式)
    支持：
    1. 异步写入文件 (TimedRotatingFileHandler)
    2. 实时回调给 TUI 界面 (ui_callback)
    3. 标准控制台输出 (可选)
    """
    _instance = None
    
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(AsyncLogger, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "initialized"):
            return
        self.initialized = True
        
        self.active = False
        self.queue = queue.Queue()
        self.worker_thread = None
        self.logger = logging.getLogger("HFT_Engine")
        self.logger.setLevel(logging.DEBUG)
        
        # [核心] 用于 TUI 显示的钩子函数
        self.ui_callback = None
        self.alert_callback = None

    def init_logging(self, config: dict):
        sys_conf = config.get("system", {})
        log_path = sys_conf.get("log_path", "logs")
        
        if not os.path.exists(log_path):
            os.makedirs(log_path)
            
        # 1. 配置本地文件记录 (按天轮转)
        file_name = os.path.join(log_path, f"hft_trading_{datetime.now().strftime('%Y%m%d')}.log")
        file_handler = TimedRotatingFileHandler(file_name, when="MIDNIGHT", interval=1, backupCount=7, encoding="utf-8")
        file_fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_fmt)
        self.logger.addHandler(file_handler)
        
        # 2. 如果配置允许，同时在终端控制台打印 (开发调试用)
        if sys_conf.get("log_console", False):
            stream_handler = logging.StreamHandler(sys.stdout)
            stream_handler.setFormatter(file_fmt)
            self.logger.addHandler(stream_handler)
            
        # 启动后台 IO 线程
        self.active = True
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()
        
        self.info("Async Logger Initialized. (UI Callback support added)")

    def set_ui_callback(self, callback):
        """
        [修复点] 注册 UI 回调函数
        每当有新日志产生，worker 线程会调用此函数
        """
        self.ui_callback = callback

    def set_alert_callback(self, callback):
        """Register a non-blocking WARNING+ sink executed on the log worker."""
        self.alert_callback = callback

    def _worker_loop(self):
        """异步 worker，负责处理队列中的所有日志请求"""
        while self.active:
            record = None
            try:
                # 阻塞获取日志，超时 1s 检查一次 active 状态
                record = self.queue.get(timeout=1.0)
                level, msg = record
                msg = redact_alert_text(msg, 16384)
                
                # A. 写入系统 Logger (进入文件)
                if level == "INFO":
                    self.logger.info(msg)
                elif level == "ERROR":
                    self.logger.error(msg)
                elif level == "DEBUG":
                    self.logger.debug(msg)
                elif level == "WARNING":
                    self.logger.warning(msg)
                elif level == "CRITICAL":
                    self.logger.critical(msg)
                
                # B. [关键] 如果有 UI 回调，实时推送给 Dashboard
                if self.ui_callback:
                    try:
                        self.ui_callback(f"[{level}] {msg}")
                    except Exception:
                        # 防止 UI 崩溃导致日志线程挂掉
                        pass

                if (
                    self.alert_callback
                    and level in {"WARNING", "ERROR", "CRITICAL"}
                ):
                    try:
                        self.alert_callback(level, msg)
                    except Exception:
                        # Alert delivery errors must not recurse into logging.
                        pass
                    
            except queue.Empty:
                pass
            except Exception as e:
                print(
                    f"Logger Internal Error: {type(e).__name__}",
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

    def stop(self):
        self.info("Logger stopping...")
        self.active = False
        if self.worker_thread:
            self.worker_thread.join()

    # --- 公开接口 (主线程调用这些，只会往 Queue 里丢数据，速度极快) ---
    def info(self, msg): self.queue.put(("INFO", msg))
    def error(self, msg): self.queue.put(("ERROR", msg))
    def debug(self, msg): self.queue.put(("DEBUG", msg))
    def warning(self, msg): self.queue.put(("WARNING", msg))
    def critical(self, msg): self.queue.put(("CRITICAL", msg))

# 全局单例
logger = AsyncLogger()
