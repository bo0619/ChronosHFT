# file: gateway/binance/ws_api.py

import threading
import time
import json
import websocket
from infrastructure.logger import logger
from .constants import *

class BinanceWsApi:
    def __init__(self, callback, error_callback, testnet=False):
        self.base_url = WS_URL_TEST if testnet else WS_URL_MAIN
        self.callback = callback        # 接收 (msg)
        self.error_callback = error_callback # 接收 (err_msg)
        self.active = False
        self.ws = None
        self.thread = None

    def start_market_stream(self, symbols):
        """
        Market Stream: <symbol>@depth / @aggTrade
        Binance Combined Stream URL 格式
        """
        streams = []
        for s in symbols:
            sl = s.lower()
            # 订阅 深度、成交、标记价格、RPI
            streams += [f"{sl}@depth@100ms", f"{sl}@aggTrade", f"{sl}@markPrice@1s", f"{sl}@rpiDepth"]
        
        # 构造 Combined URL
        url = self.base_url.replace("/ws", "") + "/stream?streams=" + "/".join(streams)
        self._start_thread(url, "MarketWS")

    def start_user_stream(self, listen_key):
        """
        User Stream: <listenKey>
        """
        url = f"{self.base_url}/{listen_key}"
        self._start_thread(url, "UserWS")

    def _start_thread(self, url, name):
        self.active = True
        # 这里的 daemon=True 确保主程序退出时线程自动结束
        threading.Thread(target=self._run, args=(url, name), daemon=True).start()

    def _run(self, url, name):
        logger.info(f"[{name}] Connecting...")
        
        while self.active:
            try:
                # [关键修复] 使用 lambda 适配 websocket-client 的回调签名
                # on_message(ws, msg) -> self.callback(msg)
                # on_error(ws, err)   -> self.error_callback(err)
                # on_open(ws)         -> log
                
                self.ws = websocket.WebSocketApp(
                    url,
                    on_open=lambda ws: logger.info(f"[{name}] Connected."),
                    on_message=lambda ws, msg: self.callback(msg),
                    on_error=lambda ws, err: self.error_callback(f"[{name}] Error: {err}"),
                    on_close=lambda ws, code, msg: logger.info(f"[{name}] Closed: {code} {msg}")
                )
                
                # 阻塞运行，直到断开
                self.ws.run_forever(ping_interval=30)
                
            except Exception as e:
                self.error_callback(f"[{name}] Exception: {e}")
                
            # 断线重连等待
            if self.active:
                logger.info(f"[{name}] Reconnecting in 5s...")
                time.sleep(5)