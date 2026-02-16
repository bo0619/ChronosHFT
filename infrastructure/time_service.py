# file: infrastructure/time_service.py

import time
import requests
import threading
from .logger import logger

class TimeService:
    _instance = None
    
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(TimeService, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "offset"): return
        self.offset = 0
        self.active = False
        # [修改] 严格对应表格: Server Time
        self.url = "https://fapi.binance.com/fapi/v1/time"

    def start(self, testnet=False):
        if testnet:
            self.url = "https://testnet.binancefuture.com/fapi/v1/time"
        
        logger.info(f"TimeService connecting to: {self.url}")
        self._sync()
        
        self.active = True
        threading.Thread(target=self._auto_sync_loop, daemon=True).start()

    # ... (其余 stop, now, _sync, _auto_sync_loop 代码保持不变)
    # 确保 _sync 方法中 requests.get(self.url) 调用正确
    def stop(self): 
        self.active = False

    def now(self): 
        return int(time.time() * 1000 + self.offset)
    
    def _sync(self):
        try:
            t0 = time.time() * 1000
            res = requests.get(self.url, timeout=5).json()
            server_time = res["serverTime"]
            t1 = time.time() * 1000
            rtt = t1 - t0
            self.offset = ((server_time - t0) + (server_time - t1)) / 2
            logger.info(f"Time Synced. Offset: {self.offset:.2f}ms")
        except Exception as e:
            logger.error(f"Time Sync Failed: {e}")
            
    def _auto_sync_loop(self):
        while self.active:
            time.sleep(600)
            self._sync()

time_service = TimeService()