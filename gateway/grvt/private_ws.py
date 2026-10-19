"""GRVT private websocket: order, order-state, fill and position feeds.

``wss://trades.grvt.io/ws/full`` speaks the same JSON-RPC as the public
market-data socket and authenticates with the REST session (``gravity``
cookie and ``X-Grvt-Account-Id`` header, see :mod:`gateway.grvt.rest_api`).
Streams, as in GRVT's SDK (``grvt_ccxt_ws.py``):

* ``v1.order``: full order objects on every change;
* ``v1.state``: order state only (status, booked and traded size), the
  smaller and faster feed for our own quotes;
* ``v1.fill``: one message per fill, with price, size, maker/taker, fee
  and fee rate;
* ``v1.position``: position after each trade.

A selector is the sub-account id (all instruments) or
``<sub_account_id>-<instrument>``. Messages arrive as
``{"stream", "selector", "sequence_number", "feed"}``; subscribe
acknowledgements carry ``result``. Each stream's ``sequence_number`` must
step by one; a gap is reported, since a missed fill or state change must
be reconciled over REST (``open_orders``, ``fill_history``, ``positions``)
before trading on.

The client reconnects with a fresh session after a drop and resubscribes;
callbacks run on the websocket thread and must not block.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterable

import websocket

WS_URLS = {"prod": "wss://trades.grvt.io/ws/full", "testnet": "wss://trades.testnet.grvt.io/ws/full"}
STREAMS = ("v1.order", "v1.state", "v1.fill", "v1.position")
RECONNECT_DELAY_SEC = 2.0


class GrvtPrivateWs:
    def __init__(self, *, rest, on_message: Callable[[str, dict], None],
                 on_gap: Callable[[str, int, int], None] | None = None,
                 on_status: Callable[[str], None] | None = None,
                 streams: Iterable[str] = STREAMS, instrument: str | None = None,
                 environment: str = "prod", app_factory=websocket.WebSocketApp):
        self.rest = rest
        self.on_message = on_message
        self.on_gap = on_gap or (lambda stream, expected, got: None)
        self.on_status = on_status or (lambda status: None)
        self.streams = tuple(streams)
        self.selector = rest.sub_account_id + (f"-{instrument}" if instrument else "")
        self.url = WS_URLS[environment]
        self.app_factory = app_factory
        self.last_sequence: dict[str, int] = {}
        self.subscribed: set[str] = set()
        self.stop = threading.Event()
        self.app = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="grvt-private-ws", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.stop.set()
        if self.app is not None:
            self.app.close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self.stop.is_set():
            headers = self.rest.auth_headers()
            header_list = [f"{name}: {value}" for name, value in headers.items() if name != "Content-Type"]
            self.app = self.app_factory(self.url, header=header_list, on_open=self._on_open,
                                        on_message=self._on_raw, on_error=self._on_error,
                                        on_close=self._on_close)
            self.app.run_forever(ping_interval=20, ping_timeout=10)
            if not self.stop.is_set():
                self.on_status("reconnecting")
                self.stop.wait(RECONNECT_DELAY_SEC)

    # -------------------------------------------------------------- handlers
    def subscribe_frames(self) -> list[dict]:
        return [{"jsonrpc": "2.0", "method": "subscribe", "id": index,
                 "params": {"stream": stream, "selectors": [self.selector]}}
                for index, stream in enumerate(self.streams, 1)]

    def _on_open(self, ws) -> None:
        self.subscribed.clear()
        self.last_sequence.clear()  # a new connection starts new sequences
        for frame in self.subscribe_frames():
            ws.send(json.dumps(frame))
        self.on_status("connected")

    def _on_raw(self, _ws, raw: str) -> None:
        self.handle(json.loads(raw))

    def handle(self, message: dict) -> None:
        result = message.get("result")
        if isinstance(result, dict) and result.get("stream"):
            self.subscribed.add(str(result["stream"]))
            return
        if message.get("error"):
            self.on_status(f"error: {message['error']}")
            return
        stream, feed = message.get("stream"), message.get("feed")
        if not stream or not isinstance(feed, dict):
            return
        sequence = int(message.get("sequence_number", 0) or 0)
        last = self.last_sequence.get(stream)
        if sequence and last is not None and sequence != last + 1:
            self.on_gap(stream, last + 1, sequence)
        if sequence:
            self.last_sequence[stream] = sequence
        self.on_message(stream, feed)

    def _on_error(self, _ws, error) -> None:
        self.on_status(f"error: {error}")

    def _on_close(self, _ws, *_args) -> None:
        self.on_status("closed")


def wait_until_subscribed(client: GrvtPrivateWs, timeout_sec: float = 10.0) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if set(client.streams) <= client.subscribed:
            return True
        time.sleep(0.05)
    return False


__all__ = ["GrvtPrivateWs", "STREAMS", "WS_URLS", "wait_until_subscribed"]
