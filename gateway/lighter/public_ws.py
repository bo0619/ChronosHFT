"""Lighter public market-data WebSocket for the Paper gateway."""

from __future__ import annotations

import json

from gateway.venue_paper import STREAM_NAME, VenuePublicWs

from .constants import WS_URL_MAIN
from .market_data import (
    LighterMarketTranslator,
    resubscribe_book_frames,
    subscribe_frames,
)


class LighterPublicWs(VenuePublicWs):
    def __init__(
        self,
        record_callback,
        error_callback,
        *,
        market_ids: dict[str, int],
        url: str = WS_URL_MAIN,
    ):
        self.market_ids = {
            str(symbol).upper(): int(market_id)
            for symbol, market_id in market_ids.items()
        }
        super().__init__(
            record_callback,
            error_callback,
            url=url,
            translator=LighterMarketTranslator(
                {market_id: symbol for symbol, market_id in self.market_ids.items()}
            ),
            symbols=self.market_ids,
        )

    def _subscribe_frames(self):
        return subscribe_frames(self.market_ids.values())

    def _resubscribe_book_frames(self, symbol):
        return resubscribe_book_frames(self.market_ids[symbol])

    def _handle_control(self, message):
        kind = str(message.get("type", "") or "")
        if kind == "ping":
            self._send({"type": "pong"})
            return True
        if kind == "error":
            self._report("venue_error", json.dumps(message, sort_keys=True))
            return True
        return False


__all__ = ["LighterPublicWs", "STREAM_NAME"]
