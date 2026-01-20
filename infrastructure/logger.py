# file: infrastructure/logger.py

import os
import sys
import queue
import threading
import logging
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

class AsyncLogger:
    """
    高性能异步日志模块 (单例模式)
    """
    _instance = None
    
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(AsyncLogger, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "initialized"): return
        self.initialized = True
        
        self.active = False
        self.queue = queue.Queue()
        self.worker_thread = None
        self.logger = logging.getLogger("HFT_Engine")
        self.logger.setLevel(logging.DEBUG)
        self.ui_callback = None

    def init_logging(self, config: dict):
        sys_conf = config.get("system", {})
        log_path = sys_conf.get("log_path", "logs")
        
        if not os.path.exists(log_path):
            os.makedirs(log_path)
            
        file_name = os.path.join(log_path, f"hft_trading_{datetime.now().strftime('%Y%m%d')}.log")
        file_handler = TimedRotatingFileHandler(file_name, when="MIDNIGHT", interval=1, backupCount=7, encoding="utf-8")
        file_fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_fmt)
        self.logger.addHandler(file_handler)
        
        if sys_conf.get("log_console", False):
            stream_handler = logging.StreamHandler(sys.stdout)
            stream_handler.setFormatter(file_fmt)
            self.logger.addHandler(stream_handler)
            
        self.active = True
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()
        
        self.info("Async Logger Initialized.")

    def set_ui_callback(self, callback):
        self.ui_callback = callback

    def _worker_loop(self):
        while self.active:
            try:
                record = self.queue.get(timeout=1.0)
                level, msg = record
                
                # [修复] 增加 CRITICAL 分支
                if level == "INFO": self.logger.info(msg)
                elif level == "ERROR": self.logger.error(msg)
                elif level == "DEBUG": self.logger.debug(msg)
                elif level == "WARNING": self.logger.warning(msg)
                elif level == "CRITICAL": self.logger.critical(msg) # [NEW]
                
                if self.ui_callback:
                    self.ui_callback(f"[{level}] {msg}")
                    
            except queue.Empty:
                pass
            except Exception as e:
                print(f"Logger Error: {e}")

    def stop(self):
        self.info("Logger stopping...")
        self.active = False
        if self.worker_thread:
            self.worker_thread.join()

    # --- 对外接口 ---
    def info(self, msg): self.queue.put(("INFO", msg))
    def error(self, msg): self.queue.put(("ERROR", msg))
    def debug(self, msg): self.queue.put(("DEBUG", msg))
    def warn(self, msg): self.queue.put(("WARNING", msg))
    
    # [NEW] 新增 critical 接口
    def critical(self, msg): self.queue.put(("CRITICAL", msg))

# 全局单例
logger = AsyncLogger()