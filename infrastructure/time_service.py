# file: infrastructure/time_service.py

import time
import requests
import threading
from .logger import logger

class TimeService:
    """
    时间同步服务 (NTP逻辑)
    维护 本地时间 与 交易所时间 的偏移量
    """
    _instance = None
    
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(TimeService, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "offset"): return
        self.offset = 0 # ms (Server - Local)
        self.is_synced = False
        self.sync_thread = None
        self.active = False
        # 币安 REST API 时间接口
        self.url = "https://fapi.binance.com/fapi/v1/time"

    def start(self, testnet=False):
        if testnet:
            self.url = "https://testnet.binancefuture.com/fapi/v1/time"
            
        logger.info("Starting Time Sync Service...")
        
        # 首次同步 (阻塞，确保启动时时间准确)
        self._sync()
        
        # 启动后台定期校正 (每 10 分钟校正一次)
        self.active = True
        self.sync_thread = threading.Thread(target=self._auto_sync_loop, daemon=True)
        self.sync_thread.start()

    def stop(self):
        self.active = False

    def now(self) -> int:
        """获取校正后的当前时间戳 (毫秒)"""
        return int(time.time() * 1000 + self.offset)

    def _sync(self):
        """执行一次 NTP 对时"""
        try:
            # 1. 记录发送时间 t0
            t0 = time.time() * 1000
            
            # 2. 请求服务器时间
            res = requests.get(self.url, timeout=5)
            server_time = res.json()["serverTime"]
            
            # 3. 记录接收时间 t1
            t1 = time.time() * 1000
            
            # 4. 计算网络往返延迟 (RTT)
            rtt = t1 - t0
            
            # 5. 估算偏移量: ServerTime = LocalTime + Offset + RTT/2
            # Offset = ServerTime - (LocalTime + RTT/2)
            # 这里的 LocalTime 近似为 t1 (接收时刻)
            # 更精确公式: Offset = ((server_time - t0) + (server_time - t1)) / 2
            
            new_offset = ((server_time - t0) + (server_time - t1)) / 2
            
            self.offset = new_offset
            self.is_synced = True
            logger.info(f"Time Synced. Offset: {self.offset:.2f}ms, RTT: {rtt:.2f}ms")
            
        except Exception as e:
            logger.error(f"Time Sync Failed: {e}")

    def _auto_sync_loop(self):
        while self.active:
            time.sleep(600) # 10分钟
            self._sync()

# 全局单例
time_service = TimeService()