"""Translate GRVT public WebSocket frames into the Paper feed's shapes.

GRVT's ``full`` JSON-RPC stream wraps every payload as
``{"stream", "selector", "sequence_number", "prev_sequence_number", "feed"}``:

* ``v1.book.d`` sends a full book on subscribe, then only changed levels
  (``size == "0"`` removes a level). The snapshot's ``sequence_number`` plays
  the role of Binance's ``lastUpdateId`` and ``prev_sequence_number`` is
  Binance's ``pu``, so the existing continuity and resync rules apply.
* ``v1.trade`` carries one trade per message. Snapshot payloads (sequence
  number 0) replay history and are dropped so old prints never reach Paper
  matching.
* ``v1.ticker.s`` carries mark/index price and the funding rate (in
  percentage points).

Prices and sizes are decimal strings; times are unix nanoseconds.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .constants import (
    BOOK_RATE_MS,
    BOOK_STREAM,
    TICKER_RATE_MS,
    TICKER_STREAM,
    TRADE_LIMIT,
    TRADE_STREAM,
)


def _subscribe(method: str, stream: str, selectors, request_id: int) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": method,
        "params": {"stream": stream, "selectors": list(selectors)},
        "id": int(request_id),
    }


def book_selector(instrument: str) -> str:
    return f"{instrument}@{BOOK_RATE_MS}"


def subscribe_frames(instruments) -> list[dict]:
    instruments = list(instruments)
    return [
        _subscribe("subscribe", BOOK_STREAM, map(book_selector, instruments), 1),
        _subscribe(
            "subscribe",
            TRADE_STREAM,
            (f"{instrument}@{TRADE_LIMIT}" for instrument in instruments),
            2,
        ),
        _subscribe(
            "subscribe",
            TICKER_STREAM,
            (f"{instrument}@{TICKER_RATE_MS}" for instrument in instruments),
            3,
        ),
    ]


def resubscribe_book_frames(instrument: str, request_id: int = 4) -> list[dict]:
    selectors = [book_selector(instrument)]
    return [
        _subscribe("unsubscribe", BOOK_STREAM, selectors, request_id),
        _subscribe("subscribe", BOOK_STREAM, selectors, request_id + 1),
    ]


def timestamp_ms(value) -> int:
    """Normalize a GRVT timestamp (ns, also s/ms/us) to milliseconds."""
    try:
        raw = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if raw <= 0:
        return 0
    if raw >= 10**17:
        return raw // 1_000_000
    if raw >= 10**14:
        return raw // 1000
    if raw < 10**11:
        return raw * 1000
    return raw


def trade_id_number(value) -> int:
    """Map GRVT's ``"<execution>-<n>"`` trade id to a monotonic integer."""
    text = str(value or "").strip()
    head, _, tail = text.partition("-")
    try:
        return int(head) * 100_000 + (int(tail) if tail else 0)
    except ValueError:
        return -1


def _levels(levels) -> list[list[str]]:
    if not isinstance(levels, list):
        raise ValueError("GRVT order book levels must be an array")
    parsed = []
    for level in levels:
        if not isinstance(level, Mapping):
            raise ValueError("GRVT order book level must be an object")
        parsed.append([str(level["price"]), str(level["size"])])
    return parsed


def _instrument(selector: str) -> str:
    return str(selector or "").partition("@")[0]


@dataclass(frozen=True, slots=True)
class GrvtBookSnapshot:
    symbol: str
    snapshot: dict


@dataclass(frozen=True, slots=True)
class GrvtStreamRecord:
    """A Binance-shaped ``(stream, data)`` record for the Paper handler."""

    stream: str
    data: dict


@dataclass(slots=True)
class GrvtMarketTranslator:
    instrument_symbols: Mapping[str, str]
    unknown_instruments: set[str] = field(default_factory=set)
    # Symbols whose next book message is the subscribe snapshot.
    _awaiting_book: set[str] = field(default_factory=set)

    def reset(self) -> None:
        """A new connection: the first book message per market is a snapshot."""
        self._awaiting_book = set(self.instrument_symbols.values())

    def translate(
        self,
        message: Mapping,
    ) -> tuple[GrvtBookSnapshot | None, list[GrvtStreamRecord]]:
        stream = str(message.get("stream", "") or "")
        feed = message.get("feed")
        if not isinstance(feed, Mapping) or stream not in {
            BOOK_STREAM,
            TRADE_STREAM,
            TICKER_STREAM,
        }:
            return None, []
        instrument = str(feed.get("instrument") or _instrument(message.get("selector")))
        symbol = self.instrument_symbols.get(instrument)
        if not symbol:
            self.unknown_instruments.add(instrument)
            return None, []
        sequence = int(message.get("sequence_number", 0) or 0)
        event_ms = timestamp_ms(feed.get("event_time"))
        if stream == BOOK_STREAM:
            if sequence == 0 or symbol in self._awaiting_book:
                self._awaiting_book.discard(symbol)
                return GrvtBookSnapshot(
                    symbol,
                    {
                        "lastUpdateId": sequence,
                        "bids": _levels(feed.get("bids", [])),
                        "asks": _levels(feed.get("asks", [])),
                        "E": event_ms,
                    },
                ), []
            previous = int(message.get("prev_sequence_number", 0) or 0)
            return None, [
                GrvtStreamRecord(
                    f"{symbol.lower()}@depth",
                    {
                        "e": "depthUpdate",
                        "s": symbol,
                        "E": event_ms,
                        "T": event_ms,
                        "U": min(previous + 1, sequence),
                        "u": sequence,
                        "pu": previous,
                        "b": _levels(feed.get("bids", [])),
                        "a": _levels(feed.get("asks", [])),
                    },
                )
            ]
        if stream == TRADE_STREAM:
            if sequence == 0:
                return None, []
            return None, [
                GrvtStreamRecord(
                    f"{symbol.lower()}@aggtrade",
                    {
                        "e": "aggTrade",
                        "s": symbol,
                        "a": trade_id_number(feed.get("trade_id")),
                        "p": str(feed["price"]),
                        "q": str(feed["size"]),
                        # A taker buyer means the maker sold.
                        "m": not bool(feed.get("is_taker_buyer", False)),
                        "T": event_ms,
                        "E": event_ms,
                    },
                )
            ]
        return None, [self._mark(symbol, feed, event_ms)]

    @staticmethod
    def _mark(symbol: str, feed: Mapping, event_ms: int) -> GrvtStreamRecord:
        mark = str(feed["mark_price"])
        funding_pct = feed.get("funding_rate")
        if funding_pct in (None, "", {}):
            funding_pct = feed.get("funding_rate_8h_curr")
        try:
            funding = float(funding_pct or 0.0) / 100.0
        except (TypeError, ValueError):
            funding = 0.0
        index = feed.get("index_price")
        return GrvtStreamRecord(
            f"{symbol.lower()}@markprice",
            {
                "e": "markPriceUpdate",
                "s": symbol,
                "E": event_ms,
                "p": mark,
                "i": str(index) if index not in (None, "", {}) else mark,
                "r": repr(funding),
                "T": timestamp_ms(feed.get("next_funding_time")),
            },
        )


__all__ = [
    "GrvtBookSnapshot",
    "GrvtMarketTranslator",
    "GrvtStreamRecord",
    "book_selector",
    "resubscribe_book_frames",
    "subscribe_frames",
    "timestamp_ms",
    "trade_id_number",
]
