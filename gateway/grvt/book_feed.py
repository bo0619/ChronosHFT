"""GRVT public order book for one instrument, rebuilt from ``v1.book.d``.

The stream sends a full book on subscribe and then changed levels
(``size == 0`` removes one); the snapshot sits at the subscribe
acknowledgement's ``latest_sequence_number`` and each delta must continue
from the previous sequence (``prev_sequence_number``). On a gap the book is
dropped, ``on_status("gap")`` is reported and the market is resubscribed,
as ``scripts/record_grvt_book.py`` does. After each applied message the
callback receives ``(received_ts, bids, asks)``: the best ``depth`` levels
per side as ``{price: size}``, so a caller can take its own orders out.
It runs on the websocket thread and must not block.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable

import websocket

from .constants import BOOK_RATE_MS, BOOK_STREAM, WS_URL_MAIN
from .market_data import resubscribe_book_frames

RECONNECT_DELAY_SEC = 1.0


class GrvtBookBuilder:
    """Sequence-checked book state; feed it parsed JSON-RPC messages."""

    def __init__(self, instrument: str):
        self.instrument = instrument
        self.reset()

    def reset(self) -> None:
        self.bids: dict[float, float] | None = None
        self.asks: dict[float, float] | None = None
        self.last_seq: int | None = None
        self.snapshot_seq: int | None = None

    def apply(self, message: dict) -> str:
        """``"book"`` when the book changed, ``"gap"`` on a sequence gap, else ``""``."""
        result = message.get("result")
        if isinstance(result, dict) and result.get("stream") == BOOK_STREAM:
            for selector, sequence in zip(result.get("subs") or [], result.get("latest_sequence_number") or []):
                if selector.partition("@")[0] == self.instrument:
                    self.snapshot_seq = int(sequence)
            return ""
        feed = message.get("feed")
        if message.get("stream") != BOOK_STREAM or not isinstance(feed, dict) \
                or feed.get("instrument") != self.instrument:
            return ""
        levels = {side: {float(l["price"]): float(l["size"]) for l in feed.get(side) or []}
                  for side in ("bids", "asks")}
        sequence = int(message.get("sequence_number", 0) or 0)
        if sequence == 0:
            self.bids, self.asks = levels["bids"], levels["asks"]
            self.last_seq, self.snapshot_seq = self.snapshot_seq, None
            return "book"
        if self.bids is None:
            return ""  # waiting for the snapshot after a resubscribe
        previous = int(message.get("prev_sequence_number", 0) or 0)
        if self.last_seq is not None and previous != self.last_seq:
            self.reset()
            return "gap"
        for side, target in (("bids", self.bids), ("asks", self.asks)):
            for price, size in levels[side].items():
                if size <= 0.0:
                    target.pop(price, None)
                else:
                    target[price] = size
        self.last_seq = sequence
        return "book"

    def top(self, depth: int) -> tuple[dict[float, float], dict[float, float]]:
        if self.bids is None:
            return {}, {}
        bids = dict(sorted(self.bids.items(), reverse=True)[:depth])
        asks = dict(sorted(self.asks.items())[:depth])
        return bids, asks


class GrvtBookFeed:
    def __init__(self, instrument: str, on_book: Callable[[float, dict, dict], None], *,
                 on_status: Callable[[str], None] | None = None, depth: int = 10, url: str = WS_URL_MAIN,
                 app_factory=websocket.WebSocketApp, clock=time.time):
        self.instrument, self.on_book, self.depth, self.url = instrument, on_book, depth, url
        self.on_status = on_status or (lambda status: None)
        self.app_factory, self.clock = app_factory, clock
        self.builder = GrvtBookBuilder(instrument)
        self.request_id = 100
        self.stop = threading.Event()
        self.app = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"grvt-book-{self.instrument}", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.stop.set()
        if self.app is not None:
            self.app.close()

    def _run(self) -> None:
        while not self.stop.is_set():
            self.builder.reset()
            self.app = self.app_factory(self.url, on_open=self._on_open, on_message=self._on_raw,
                                        on_error=lambda ws, e: self.on_status(f"error: {e}"),
                                        on_close=lambda ws, *a: self.on_status("closed"))
            self.app.run_forever(ping_interval=20, ping_timeout=10)
            if not self.stop.is_set():
                self.on_status("reconnecting")
                self.stop.wait(RECONNECT_DELAY_SEC)

    def _on_open(self, ws) -> None:
        ws.send(json.dumps({"jsonrpc": "2.0", "method": "subscribe", "id": 1,
                            "params": {"stream": BOOK_STREAM, "selectors": [f"{self.instrument}@{BOOK_RATE_MS}"]}}))
        self.on_status("connected")

    def _on_raw(self, ws, raw: str) -> None:
        received = self.clock()
        message = json.loads(raw)
        if message.get("error"):
            self.on_status(f"error: {message['error']}")
            return
        outcome = self.builder.apply(message)
        if outcome == "gap":
            self.on_status("gap")
            self.request_id += 2
            for frame in resubscribe_book_frames(self.instrument, self.request_id):
                ws.send(json.dumps(frame))
        elif outcome == "book":
            bids, asks = self.builder.top(self.depth)
            self.on_book(received, bids, asks)


__all__ = ["GrvtBookBuilder", "GrvtBookFeed"]
