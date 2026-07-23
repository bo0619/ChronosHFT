# file: gateway/binance/ws_api.py

import threading
import time

import websocket

from infrastructure.logger import logger
from .constants import WS_URL_MAIN, WS_URL_TEST


class BinanceWsApi:
    def __init__(self, callback, error_callback, testnet=False):
        self.base_url = WS_URL_TEST if testnet else WS_URL_MAIN
        self.callback = callback
        self.error_callback = error_callback
        self.active = False
        self.ws = None
        self.lock = threading.RLock()
        self.stream_apps = {}
        self.close_requested = False
        self.connected_events = {
            "MarketWS": threading.Event(),
            "UserWS": threading.Event(),
        }

    def start_market_stream(self, symbols):
        streams = []
        for s in symbols:
            sl = s.lower()
            streams += [f"{sl}@depth@100ms", f"{sl}@aggTrade", f"{sl}@markPrice@1s"]

        url = self.base_url.replace("/ws", "") + "/stream?streams=" + "/".join(streams)
        self._start_thread(url, "MarketWS")

    def start_user_stream(self, listen_key):
        url = f"{self.base_url}/{listen_key}"
        self._start_thread(url, "UserWS")

    def _start_thread(self, url, name):
        with self.lock:
            self.active = True
            self.close_requested = False
            self.connected_events.setdefault(name, threading.Event()).clear()
        threading.Thread(target=self._run, args=(url, name), daemon=True).start()

    def wait_until_connected(self, names=("MarketWS", "UserWS"), timeout_sec=10.0):
        deadline = time.perf_counter() + max(0.0, float(timeout_sec))
        for name in names:
            with self.lock:
                connected = self.connected_events.setdefault(
                    name,
                    threading.Event(),
                )
            remaining = deadline - time.perf_counter()
            if remaining <= 0.0 or not connected.wait(remaining):
                return False
        with self.lock:
            return bool(
                self.active
                and all(
                    self.connected_events[name].is_set()
                    for name in names
                )
            )

    def _run(self, url, name):
        logger.info(f"[{name}] Connecting...")
        while self._is_active():
            fault_reported = {"value": False}
            ws_app = None
            try:
                ws_app = websocket.WebSocketApp(
                    url,
                    on_open=lambda ws: self._handle_open(name, ws),
                    on_message=lambda ws, msg: self.callback(msg),
                    on_error=lambda ws, err: self._handle_transport_fault(name, err, fault_reported),
                    on_close=lambda ws, code, msg: self._handle_close(name, ws, code, msg, fault_reported),
                )
                with self.lock:
                    self.stream_apps[name] = ws_app
                    self.ws = ws_app
                ws_app.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                self._handle_transport_fault(name, e, fault_reported)
            finally:
                with self.lock:
                    if self.stream_apps.get(name) is ws_app:
                        self.stream_apps.pop(name, None)
                    if self.ws is ws_app:
                        self.ws = None
                    self.connected_events.setdefault(
                        name,
                        threading.Event(),
                    ).clear()

            if self._is_active():
                logger.info(f"[{name}] Reconnecting in 5s...")
                time.sleep(5)

    def close(self):
        with self.lock:
            self.active = False
            self.close_requested = True
            stream_apps = list(self.stream_apps.values())
            self.stream_apps = {}
            self.ws = None
            for connected in self.connected_events.values():
                connected.clear()

        for ws_app in stream_apps:
            try:
                ws_app.close()
            except Exception:
                pass

    def _is_active(self):
        with self.lock:
            return bool(self.active)

    def _handle_open(self, name, ws):
        with self.lock:
            self.stream_apps[name] = ws
            self.ws = ws
            self.connected_events.setdefault(name, threading.Event()).set()
        logger.info(f"[{name}] Connected.")

    def _handle_transport_fault(self, name, err, fault_reported):
        detail = str(err)
        should_report = False
        with self.lock:
            if self.active and not self.close_requested and not fault_reported["value"]:
                fault_reported["value"] = True
                should_report = True
        if should_report:
            self.error_callback(
                {
                    "stream": name,
                    "kind": "transport_drop",
                    "detail": detail,
                }
            )

    def _handle_close(self, name, ws, code, msg, fault_reported):
        with self.lock:
            if self.stream_apps.get(name) is ws:
                self.stream_apps.pop(name, None)
            self.connected_events.setdefault(name, threading.Event()).clear()
            if self.ws is ws:
                self.ws = None
            should_report = self.active and not self.close_requested and not fault_reported["value"]
            if should_report:
                fault_reported["value"] = True
        logger.info(f"[{name}] Closed: {code} {msg}")
        if should_report:
            self.error_callback(
                {
                    "stream": name,
                    "kind": "remote_close",
                    "detail": f"code={code} msg={msg}",
                }
            )
