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
    核心思想：
    1. 主线程将 LogRecord 推入 Queue (内存操作，极快)
    2. 独立 Worker 线程从 Queue 取出并写入文件/控制台 (IO操作，较慢)
    3. 避免 IO 阻塞交易主线程
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
        self.queue = queue.Queue() # 无界队列，充当 RingBuffer 角色
        self.worker_thread = None
        self.logger = logging.getLogger("HFT_Engine")
        self.logger.setLevel(logging.DEBUG)
        self.ui_callback = None # 用于将日志回调给 UI 界面

    def init_logging(self, config: dict):
        """初始化日志配置"""
        sys_conf = config.get("system", {})
        log_path = sys_conf.get("log_path", "logs")
        log_level = sys_conf.get("log_level", "INFO")
        
        if not os.path.exists(log_path):
            os.makedirs(log_path)
            
        # 1. 文件 Handler (按天轮转)
        file_name = os.path.join(log_path, f"hft_trading_{datetime.now().strftime('%Y%m%d')}.log")
        file_handler = TimedRotatingFileHandler(file_name, when="MIDNIGHT", interval=1, backupCount=7, encoding="utf-8")
        file_fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_fmt)
        self.logger.addHandler(file_handler)
        
        # 2. 控制台 Handler (可选，如果启用了UI则通常关闭)
        if sys_conf.get("log_console", False):
            stream_handler = logging.StreamHandler(sys.stdout)
            stream_handler.setFormatter(file_fmt)
            self.logger.addHandler(stream_handler)
            
        # 启动异步线程
        self.active = True
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()
        
        self.info("Async Logger Initialized.")

    def set_ui_callback(self, callback):
        """注册 UI 的回调函数，用于在界面显示日志"""
        self.ui_callback = callback

    def _worker_loop(self):
        """后台写入线程"""
        while self.active:
            try:
                # 阻塞获取，降低 CPU 占用
                record = self.queue.get(timeout=1.0)
                
                # 1. 写入 Python logging 系统 (文件/终端)
                level, msg = record
                if level == "INFO": self.logger.info(msg)
                elif level == "ERROR": self.logger.error(msg)
                elif level == "DEBUG": self.logger.debug(msg)
                elif level == "WARNING": self.logger.warning(msg)
                
                # 2. 回调给 UI (如果有)
                if self.ui_callback:
                    # UI 更新通常需要极快，直接回调
                    self.ui_callback(f"[{level}] {msg}")
                    
            except queue.Empty:
                pass
            except Exception as e:
                # 自身报错直接打印，防止死循环
                print(f"Logger Error: {e}")

    def stop(self):
        self.info("Logger stopping...")
        self.active = False
        if self.worker_thread:
            self.worker_thread.join()

    # --- 对外接口 (非阻塞) ---
    def info(self, msg): self.queue.put(("INFO", msg))
    def error(self, msg): self.queue.put(("ERROR", msg))
    def debug(self, msg): self.queue.put(("DEBUG", msg))
    def warn(self, msg): self.queue.put(("WARNING", msg))

# 全局单例
logger = AsyncLogger()