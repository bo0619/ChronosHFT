"""GRVT public market-data WebSocket for the Paper gateway."""

from __future__ import annotations

import json

from gateway.venue_paper import STREAM_NAME, VenuePublicWs

from .constants import WS_URL_MAIN
from .market_data import (
    GrvtMarketTranslator,
    resubscribe_book_frames,
    subscribe_frames,
)


class GrvtPublicWs(VenuePublicWs):
    def __init__(
        self,
        record_callback,
        error_callback,
        *,
        instruments: dict[str, str],
        url: str = WS_URL_MAIN,
    ):
        self.instruments = {
            str(symbol).upper(): str(instrument)
            for symbol, instrument in instruments.items()
        }
        super().__init__(
            record_callback,
            error_callback,
            url=url,
            translator=GrvtMarketTranslator(
                {
                    instrument: symbol
                    for symbol, instrument in self.instruments.items()
                }
            ),
            symbols=self.instruments,
        )
        self._request_id = 100

    def _subscribe_frames(self):
        return subscribe_frames(self.instruments.values())

    def _resubscribe_book_frames(self, symbol):
        self._request_id += 2
        return resubscribe_book_frames(self.instruments[symbol], self._request_id)

    def _handle_control(self, message):
        if "feed" in message:
            return False
        error = message.get("error")
        if error or ("code" in message and "message" in message):
            self._report(
                "venue_error",
                json.dumps(error or message, sort_keys=True),
            )
        # Subscribe/unsubscribe acknowledgements carry nothing to publish.
        return True


__all__ = ["GrvtPublicWs", "STREAM_NAME"]
