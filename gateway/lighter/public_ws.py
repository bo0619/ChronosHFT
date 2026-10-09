"""Lighter public market-data WebSocket for the Paper gateway.

Reuses :class:`BinanceWsApi`'s worker, reconnect and close machinery with a
single Lighter connection. Order-book snapshots arrive on the stream itself,
so a book resync asks for one by resubscribing the market's channel.
"""

from __future__ import annotations

import json
import threading
import time

from gateway.binance.ws_api import BinanceWsApi
from infrastructure.logger import logger

from .constants import WS_URL_MAIN
from .market_data import (
    LighterMarketTranslator,
    resubscribe_book_frames,
    subscribe_frames,
)

STREAM_NAME = "PublicWS"


class LighterPublicWs(BinanceWsApi):
    def __init__(
        self,
        record_callback,
        error_callback,
        *,
        market_ids: dict[str, int],
        url: str = WS_URL_MAIN,
    ):
        super().__init__(self._on_raw_message, error_callback, testnet=False)
        self.url = url
        self.record_callback = record_callback
        self.market_ids = {
            str(symbol).upper(): int(market_id)
            for symbol, market_id in market_ids.items()
        }
        self.translator = LighterMarketTranslator(
            {market_id: symbol for symbol, market_id in self.market_ids.items()}
        )
        self._snapshot_condition = threading.Condition()
        self._snapshots: dict[str, dict] = {}

    # BinanceWsApi transport hooks -------------------------------------

    def start_market_stream(self, symbols):
        missing = [
            str(symbol).upper()
            for symbol in symbols
            if str(symbol).upper() not in self.market_ids
        ]
        if missing:
            raise ValueError(f"Lighter market ids unknown for {missing}")
        return self._start_thread(self.url, STREAM_NAME) is not False

    def wait_until_connected(self, names=(STREAM_NAME,), timeout_sec=10.0):
        # The Paper gateway waits for Binance's two public streams; Lighter
        # carries book, trades and marks on one connection.
        return super().wait_until_connected(
            names=(STREAM_NAME,),
            timeout_sec=timeout_sec,
        )

    def _handle_open(self, name, ws):
        with self.lock:
            rejected = not self.active or self.close_requested
        if rejected:
            ws.close()
            return
        for frame in subscribe_frames(self.market_ids.values()):
            ws.send(json.dumps(frame))
        super()._handle_open(name, ws)

    # Message handling ---------------------------------------------------

    def _on_raw_message(self, raw_message):
        message = json.loads(raw_message)
        if not isinstance(message, dict):
            return
        kind = str(message.get("type", "") or "")
        if kind == "ping":
            self._send({"type": "pong"})
            return
        if kind == "error":
            self.error_callback(
                {
                    "stream": STREAM_NAME,
                    "kind": "venue_error",
                    "detail": json.dumps(message, sort_keys=True)[:500],
                }
            )
            return
        try:
            snapshot, records = self.translator.translate(message)
        except (KeyError, TypeError, ValueError) as exc:
            self.error_callback(
                {
                    "stream": STREAM_NAME,
                    "kind": "handler_failure",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
            return
        if snapshot is not None:
            with self._snapshot_condition:
                self._snapshots[snapshot.symbol] = snapshot.snapshot
                self._snapshot_condition.notify_all()
        for record in records:
            self.record_callback(record.stream, record.data)

    def fetch_snapshot(self, symbol: str, timeout_sec: float = 10.0):
        """Return the next unused book snapshot, resubscribing if needed."""
        symbol = str(symbol or "").upper()
        deadline = time.perf_counter() + max(0.0, float(timeout_sec))
        with self._snapshot_condition:
            snapshot = self._snapshots.pop(symbol, None)
        if snapshot is not None:
            return snapshot
        market_id = self.market_ids.get(symbol)
        if market_id is None:
            return None
        for frame in resubscribe_book_frames(market_id):
            if not self._send(frame):
                return None
        with self._snapshot_condition:
            while symbol not in self._snapshots:
                remaining = deadline - time.perf_counter()
                if remaining <= 0.0 or not self._is_active():
                    logger.error(
                        f"[LighterPublicWs] Snapshot timeout for {symbol}"
                    )
                    return None
                self._snapshot_condition.wait(min(remaining, 0.25))
            return self._snapshots.pop(symbol)

    def _send(self, frame: dict) -> bool:
        with self.lock:
            ws_app = self.stream_apps.get(STREAM_NAME)
        if ws_app is None:
            return False
        try:
            ws_app.send(json.dumps(frame))
        except Exception as exc:
            logger.error(
                f"[LighterPublicWs] send failed: {type(exc).__name__}: {exc}"
            )
            return False
        return True


__all__ = ["LighterPublicWs", "STREAM_NAME"]
