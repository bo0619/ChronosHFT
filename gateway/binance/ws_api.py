# file: gateway/binance/ws_api.py

import threading
import time

import websocket

from infrastructure.logger import logger
from .constants import (
    WS_MARKET_URL_MAIN,
    WS_PRIVATE_URL_MAIN,
    WS_PUBLIC_URL_MAIN,
    WS_URL_TEST,
)


class BinanceWsApi:
    def __init__(self, callback, error_callback, testnet=False):
        self.testnet = bool(testnet)
        if self.testnet:
            # Binance Futures testnet still documents the legacy combined
            # stream layout. Keep it isolated from production URL routing so a
            # testnet compatibility change cannot regress the live endpoints.
            self.public_base_url = WS_URL_TEST
            self.market_base_url = WS_URL_TEST
            self.private_base_url = WS_URL_TEST
        else:
            self.public_base_url = WS_PUBLIC_URL_MAIN
            self.market_base_url = WS_MARKET_URL_MAIN
            self.private_base_url = WS_PRIVATE_URL_MAIN
        self.callback = callback
        self.error_callback = error_callback
        self.active = False
        self.ws = None
        self.lock = threading.RLock()
        self.stream_apps = {}
        self.stream_threads = {}
        self.close_requested = False
        self._close_event = threading.Event()
        self.connected_events = {
            "PublicWS": threading.Event(),
            "MarketWS": threading.Event(),
            "UserWS": threading.Event(),
        }

    def start_market_stream(self, symbols):
        public_streams = []
        market_streams = []
        for s in symbols:
            sl = s.lower()
            public_streams.append(f"{sl}@depth@100ms")
            market_streams.extend(
                [f"{sl}@aggTrade", f"{sl}@markPrice@1s"]
            )

        self._start_thread(
            self._combined_stream_url(
                self.public_base_url,
                public_streams,
            ),
            "PublicWS",
        )
        self._start_thread(
            self._combined_stream_url(
                self.market_base_url,
                market_streams,
            ),
            "MarketWS",
        )

    def start_user_stream(self, listen_key):
        if self.testnet:
            url = f"{self.private_base_url}/{listen_key}"
        else:
            url = f"{self.private_base_url}/ws/{listen_key}"
        self._start_thread(url, "UserWS")

    def _combined_stream_url(self, base_url, streams):
        if self.testnet:
            root = str(base_url).removesuffix("/ws")
        else:
            root = str(base_url).rstrip("/")
        return root + "/stream?streams=" + "/".join(streams)

    def _start_thread(self, url, name):
        with self.lock:
            existing = self.stream_threads.get(name)
            if existing is not None and existing.is_alive():
                logger.warning(f"[{name}] Stream worker is already running")
                return False
            self.active = True
            self.close_requested = False
            self._close_event.clear()
            self.connected_events.setdefault(name, threading.Event()).clear()
            thread = threading.Thread(
                target=self._run,
                args=(url, name),
                daemon=True,
                name=f"BinanceWs-{name}",
            )
            self.stream_threads[name] = thread
        thread.start()
        return True

    def wait_until_connected(
        self,
        names=("PublicWS", "MarketWS", "UserWS"),
        timeout_sec=10.0,
    ):
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
                start_aborted = False
                with self.lock:
                    if not self.active or self.close_requested:
                        start_aborted = True
                    else:
                        self.stream_apps[name] = ws_app
                        self.ws = ws_app
                if start_aborted:
                    ws_app.close()
                    return
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
                if self._close_event.wait(5.0):
                    break
        with self.lock:
            current = threading.current_thread()
            if self.stream_threads.get(name) is current:
                self.stream_threads.pop(name, None)

    def close(self):
        with self.lock:
            self.active = False
            self.close_requested = True
            self._close_event.set()
            stream_apps = list(self.stream_apps.values())
            stream_threads = [
                thread
                for thread in self.stream_threads.values()
                if thread is not threading.current_thread()
            ]
            self.stream_apps = {}
            self.ws = None
            for connected in self.connected_events.values():
                connected.clear()

        for ws_app in stream_apps:
            try:
                ws_app.close()
            except Exception:
                pass
        deadline = time.perf_counter() + 2.0
        for thread in stream_threads:
            if thread.is_alive():
                thread.join(timeout=max(0.0, deadline - time.perf_counter()))
        stopped = all(not thread.is_alive() for thread in stream_threads)
        if not stopped:
            logger.critical(
                "[BinanceWsApi] Stream workers did not stop before timeout"
            )
        return stopped

    def _is_active(self):
        with self.lock:
            return bool(self.active)

    def _handle_open(self, name, ws):
        reject_open = False
        with self.lock:
            if not self.active or self.close_requested:
                reject_open = True
            else:
                self.stream_apps[name] = ws
                self.ws = ws
                self.connected_events.setdefault(name, threading.Event()).set()
        if reject_open:
            ws.close()
            return
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
