"""Binance USD-M ``bookTicker`` for one symbol on its own connection.

The live GRVT maker prices off this feed. Hot symbols sharing a connection
were measured to lag by hundreds of milliseconds, so each symbol gets its
own socket, on the ``/public`` route the recordings used
(``wss://fstream.binance.com/public/stream?streams=<symbol>@bookTicker``).
The callback receives ``(received_ts, bid, ask, event_ms)`` on the websocket
thread and must not block. The client reconnects after a drop.

A quiet symbol can go seconds without a ticker, so silence is not a dead
feed: the socket is pinged every ``PING_INTERVAL_SEC`` and each pong goes to
``on_alive(received_ts)``. A pong later than ``PING_TIMEOUT_SEC`` drops the
connection.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable

import websocket

BASE_URL = "wss://fstream.binance.com/public/stream?streams="
RECONNECT_DELAY_SEC = 1.0
PING_INTERVAL_SEC = 2.0
PING_TIMEOUT_SEC = 1.5


def parse_book_ticker(raw: str) -> tuple[float, float, float] | None:
    """``(bid, ask, event_ms)`` from a bookTicker message, plain or wrapped in a combined stream."""
    message = json.loads(raw)
    if not isinstance(message, dict):
        return None
    message = message.get("data", message)
    if not isinstance(message, dict) or message.get("e") != "bookTicker":
        return None
    return float(message["b"]), float(message["a"]), float(message.get("E") or message.get("T") or 0)


class BinanceBookTickerFeed:
    def __init__(self, symbol: str, on_ticker: Callable[[float, float, float, float], None], *,
                 on_status: Callable[[str], None] | None = None,
                 on_alive: Callable[[float], None] | None = None, base_url: str = BASE_URL,
                 app_factory=websocket.WebSocketApp, clock=time.time):
        self.symbol = symbol.upper()
        self.url = f"{base_url}{symbol.lower()}@bookTicker"
        self.on_ticker = on_ticker
        self.on_status = on_status or (lambda status: None)
        self.on_alive = on_alive or (lambda received: None)
        self.app_factory, self.clock = app_factory, clock
        self.stop = threading.Event()
        self.app = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"binance-{self.symbol}", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.stop.set()
        if self.app is not None:
            self.app.close()

    def _run(self) -> None:
        while not self.stop.is_set():
            self.app = self.app_factory(self.url, on_open=lambda ws: self.on_status("connected"),
                                        on_message=self._on_raw, on_error=lambda ws, e: self.on_status(f"error: {e}"),
                                        on_close=lambda ws, *a: self.on_status("closed"),
                                        on_pong=lambda ws, data: self.on_alive(self.clock()))
            self.app.run_forever(ping_interval=PING_INTERVAL_SEC, ping_timeout=PING_TIMEOUT_SEC)
            if not self.stop.is_set():
                self.on_status("reconnecting")
                self.stop.wait(RECONNECT_DELAY_SEC)

    def _on_raw(self, _ws, raw: str) -> None:
        received = self.clock()
        parsed = parse_book_ticker(raw)
        if parsed is not None:
            bid, ask, event_ms = parsed
            self.on_ticker(received, bid, ask, event_ms)


__all__ = ["BinanceBookTickerFeed", "parse_book_ticker"]
