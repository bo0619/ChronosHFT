"""Translate Lighter public WebSocket frames into the Paper feed's shapes.

The Paper gateway's book synchronizer, stale-event guard and matching engine
consume Binance-shaped depth/trade/mark records. Lighter frames map onto them
directly:

* ``subscribed/order_book`` is a full snapshot whose ``nonce`` plays the role
  of Binance's ``lastUpdateId``.
* ``update/order_book`` is continuous when its ``begin_nonce`` equals the
  previous frame's ``nonce``, which is exactly Binance's ``pu``/``u`` rule.
* ``update/trade`` carries trades; ``subscribed/trade`` replays history and is
  dropped so old prints never reach Paper matching.
* ``market_stats`` carries mark/index price and the estimated funding rate.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

ORDER_BOOK_CHANNEL = "order_book"
TRADE_CHANNEL = "trade"
MARKET_STATS_CHANNEL = "market_stats"
PUBLIC_CHANNELS = (ORDER_BOOK_CHANNEL, TRADE_CHANNEL, MARKET_STATS_CHANNEL)


def subscribe_frames(market_ids) -> list[dict]:
    return [
        {"type": "subscribe", "channel": f"{channel}/{int(market_id)}"}
        for market_id in market_ids
        for channel in PUBLIC_CHANNELS
    ]


def resubscribe_book_frames(market_id: int) -> list[dict]:
    channel = f"{ORDER_BOOK_CHANNEL}/{int(market_id)}"
    return [
        {"type": "unsubscribe", "channel": channel},
        {"type": "subscribe", "channel": channel},
    ]


def timestamp_ms(value) -> int:
    """Normalize a Lighter timestamp given in s, ms or us to milliseconds."""
    try:
        raw = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if raw <= 0:
        return 0
    if raw >= 10**14:
        return raw // 1000
    if raw < 10**11:
        return raw * 1000
    return raw


def _levels(levels) -> list[list[str]]:
    if not isinstance(levels, list):
        raise ValueError("Lighter order book levels must be an array")
    parsed = []
    for level in levels:
        if not isinstance(level, Mapping):
            raise ValueError("Lighter order book level must be an object")
        parsed.append([str(level["price"]), str(level["size"])])
    return parsed


def _channel_market_id(message: Mapping) -> int:
    channel = str(message.get("channel", "") or "")
    _, separator, market = channel.partition(":")
    if not separator:
        _, separator, market = channel.partition("/")
    if not separator:
        raise ValueError(f"Lighter channel has no market id: {channel!r}")
    return int(market)


@dataclass(frozen=True, slots=True)
class LighterBookSnapshot:
    symbol: str
    snapshot: dict


@dataclass(frozen=True, slots=True)
class LighterStreamRecord:
    """A Binance-shaped ``(stream, data)`` record for the Paper handler."""

    stream: str
    data: dict


@dataclass(slots=True)
class LighterMarketTranslator:
    market_symbols: Mapping[int, str]
    unknown_market_ids: set[int] = field(default_factory=set)

    def reset(self) -> None:
        """Lighter frames carry their own snapshot type; nothing to reset."""

    def translate(
        self,
        message: Mapping,
    ) -> tuple[LighterBookSnapshot | None, list[LighterStreamRecord]]:
        kind = str(message.get("type", "") or "")
        if kind not in {
            "subscribed/order_book",
            "update/order_book",
            "update/trade",
            "subscribed/market_stats",
            "update/market_stats",
        }:
            return None, []
        market_id = _channel_market_id(message)
        symbol = self.market_symbols.get(market_id)
        if not symbol:
            self.unknown_market_ids.add(market_id)
            return None, []
        event_ms = timestamp_ms(message.get("timestamp"))
        if kind == "subscribed/order_book":
            return LighterBookSnapshot(
                symbol,
                self._snapshot(message, event_ms),
            ), []
        if kind == "update/order_book":
            return None, [self._delta(symbol, message, event_ms)]
        if kind == "update/trade":
            return None, self._trades(symbol, message, event_ms)
        return None, [self._mark(symbol, message, event_ms)]

    @staticmethod
    def _snapshot(message: Mapping, event_ms: int) -> dict:
        book = message.get("order_book")
        if not isinstance(book, Mapping):
            raise ValueError("Lighter order book snapshot has no order_book")
        return {
            "lastUpdateId": int(book["nonce"]),
            "bids": _levels(book.get("bids", [])),
            "asks": _levels(book.get("asks", [])),
            "E": event_ms,
        }

    @staticmethod
    def _delta(symbol: str, message: Mapping, event_ms: int):
        book = message.get("order_book")
        if not isinstance(book, Mapping):
            raise ValueError("Lighter order book update has no order_book")
        nonce = int(book["nonce"])
        begin_nonce = int(book["begin_nonce"])
        return LighterStreamRecord(
            f"{symbol.lower()}@depth",
            {
                "e": "depthUpdate",
                "s": symbol,
                "E": event_ms,
                "T": event_ms,
                "U": min(begin_nonce + 1, nonce),
                "u": nonce,
                "pu": begin_nonce,
                "b": _levels(book.get("bids", [])),
                "a": _levels(book.get("asks", [])),
            },
        )

    @staticmethod
    def _trades(symbol: str, message: Mapping, event_ms: int):
        records = []
        for key in ("trades", "liquidation_trades"):
            trades = message.get(key) or []
            if not isinstance(trades, list):
                raise ValueError(f"Lighter {key} must be an array")
            for trade in trades:
                if not isinstance(trade, Mapping):
                    continue
                trade_ms = timestamp_ms(trade.get("timestamp")) or event_ms
                records.append(
                    LighterStreamRecord(
                        f"{symbol.lower()}@aggtrade",
                        {
                            "e": "aggTrade",
                            "s": symbol,
                            "a": int(trade.get("trade_id", -1)),
                            "p": str(trade["price"]),
                            "q": str(trade["size"]),
                            # A maker ask means the taker bought.
                            "m": not bool(trade.get("is_maker_ask", False)),
                            "T": trade_ms,
                            "E": trade_ms,
                        },
                    )
                )
        records.sort(key=lambda record: record.data["a"])
        return records

    @staticmethod
    def _mark(symbol: str, message: Mapping, event_ms: int):
        stats = message.get("market_stats")
        if not isinstance(stats, Mapping):
            raise ValueError("Lighter market_stats frame has no market_stats")
        return LighterStreamRecord(
            f"{symbol.lower()}@markprice",
            {
                "e": "markPriceUpdate",
                "s": symbol,
                "E": event_ms,
                "p": str(stats["mark_price"]),
                "i": str(stats.get("index_price", stats["mark_price"])),
                "r": str(
                    stats.get(
                        "current_funding_rate",
                        stats.get("funding_rate", "0"),
                    )
                ),
                # Lighter does not publish the next settlement time.
                "T": 0,
            },
        )


__all__ = [
    "LighterBookSnapshot",
    "LighterMarketTranslator",
    "LighterStreamRecord",
    "resubscribe_book_frames",
    "subscribe_frames",
    "timestamp_ms",
]
