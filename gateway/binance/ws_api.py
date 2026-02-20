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
        self.callback = callback        
        self.error_callback = error_callback 
        self.active = False
        self.ws = None

    def start_market_stream(self, symbols):
        streams = []
        for s in symbols:
            sl = s.lower()
            # [Removed] @rpiDepth
            streams += [f"{sl}@depth@100ms", f"{sl}@aggTrade", f"{sl}@markPrice@1s"]
        
        url = self.base_url.replace("/ws", "") + "/stream?streams=" + "/".join(streams)
        self._start_thread(url, "MarketWS")

    def start_user_stream(self, listen_key):
        url = f"{self.base_url}/{listen_key}"
        self._start_thread(url, "UserWS")

    def _start_thread(self, url, name):
        self.active = True
        threading.Thread(target=self._run, args=(url, name), daemon=True).start()

    def _run(self, url, name):
        logger.info(f"[{name}] Connecting...")
        while self.active:
            try:
                self.ws = websocket.WebSocketApp(
                    url,
                    on_open=lambda ws: logger.info(f"[{name}] Connected."),
                    on_message=lambda ws, msg: self.callback(msg),
                    on_error=lambda ws, err: self.error_callback(f"[{name}] Error: {err}"),
                    on_close=lambda ws, code, msg: logger.info(f"[{name}] Closed: {code} {msg}")
                )
                self.ws.run_forever(ping_interval=30)
            except Exception as e:
                self.error_callback(f"[{name}] Exception: {e}")
            
            if self.active:
                logger.info(f"[{name}] Reconnecting in 5s...")
                time.sleep(5)