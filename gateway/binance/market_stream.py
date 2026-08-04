"""Pure Binance USD-M public market-stream normalization."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime

from event.type import (
    AggTradeData,
    MarkPriceData,
    EVENT_AGG_TRADE,
    EVENT_MARK_PRICE,
    EVENT_ORDERBOOK,
)


@dataclass(frozen=True)
class BinanceMarketEnvelope:
    """Transport metadata captured before payload-specific normalization."""

    stream: str
    data: dict
    symbol: str | None
    event_time_ms: int
    received_timestamp: float
    received_monotonic: float
    corrected_received_timestamp: float
    clock_offset_ms: float

    @property
    def requires_ingress_freshness(self) -> bool:
        return "@aggTrade" in self.stream or "@depth" in self.stream


@dataclass(frozen=True)
class BinanceMarketUpdate:
    event_type: str
    payload: object


class BinanceMarketStreamParser:
    """Convert Binance combined-stream payloads into domain events."""

    @staticmethod
    def envelope(
        msg: dict,
        *,
        received_timestamp: float | None = None,
        received_monotonic: float | None = None,
        corrected_received_timestamp: float | None = None,
        clock_offset_ms: float = 0.0,
        now=time.time,
        monotonic=time.perf_counter,
    ) -> BinanceMarketEnvelope:
        stream = str(msg["stream"])
        data = dict(msg["data"])
        received_timestamp = float(received_timestamp or now())
        received_monotonic = float(received_monotonic or monotonic())
        clock_offset_ms = float(clock_offset_ms)
        corrected_received_timestamp = float(
            corrected_received_timestamp
            or (received_timestamp + clock_offset_ms / 1000.0)
        )
        event_time_ms = int(
            data.get("E", 0)
            or (0 if "@markPrice" in stream else data.get("T", 0))
            or 0
        )
        return BinanceMarketEnvelope(
            stream=stream,
            data=data,
            symbol=data.get("s"),
            event_time_ms=event_time_ms,
            received_timestamp=received_timestamp,
            received_monotonic=received_monotonic,
            corrected_received_timestamp=corrected_received_timestamp,
            clock_offset_ms=clock_offset_ms,
        )

    @classmethod
    def normalize(
        cls,
        envelope: BinanceMarketEnvelope,
    ) -> BinanceMarketUpdate | None:
        if "@aggTrade" in envelope.stream:
            return BinanceMarketUpdate(
                EVENT_AGG_TRADE,
                cls._agg_trade(envelope),
            )
        if "@markPrice" in envelope.stream:
            return BinanceMarketUpdate(
                EVENT_MARK_PRICE,
                cls._mark_price(envelope),
            )
        if "@depth" in envelope.stream:
            return BinanceMarketUpdate(
                EVENT_ORDERBOOK,
                cls._depth(envelope),
            )
        return None

    @staticmethod
    def _agg_trade(envelope: BinanceMarketEnvelope) -> AggTradeData:
        data = envelope.data
        exchange_timestamp = (
            float(data.get("T", envelope.event_time_ms) or 0.0) / 1000.0
        )
        trade_id = int(data["a"])
        price = float(data["p"])
        quantity = float(data["q"])
        if (
            trade_id < 0
            or not math.isfinite(price)
            or price <= 0.0
            or not math.isfinite(quantity)
            or quantity <= 0.0
            or not math.isfinite(exchange_timestamp)
            or exchange_timestamp <= 0.0
        ):
            raise ValueError(
                f"invalid aggTrade payload for {envelope.symbol}"
            )
        return AggTradeData(
            envelope.symbol,
            trade_id,
            price,
            quantity,
            data["m"],
            datetime.fromtimestamp(exchange_timestamp),
            exchange_timestamp=exchange_timestamp,
            received_timestamp=envelope.received_timestamp,
            received_monotonic=envelope.received_monotonic,
            clock_offset_ms=envelope.clock_offset_ms,
            corrected_received_timestamp=(
                envelope.corrected_received_timestamp
            ),
        )

    @staticmethod
    def _mark_price(envelope: BinanceMarketEnvelope) -> MarkPriceData:
        data = envelope.data
        exchange_timestamp = (
            float(data.get("E", envelope.event_time_ms) or 0.0) / 1000.0
        )
        next_funding_timestamp = float(data["T"]) / 1000.0
        mark_price = float(data["p"])
        index_price = float(data["i"])
        funding_rate = float(data["r"])
        if (
            not math.isfinite(mark_price)
            or mark_price <= 0.0
            or not math.isfinite(index_price)
            or index_price <= 0.0
            or not math.isfinite(funding_rate)
            or not math.isfinite(exchange_timestamp)
            or exchange_timestamp <= 0.0
            or not math.isfinite(next_funding_timestamp)
            or next_funding_timestamp <= 0.0
        ):
            raise ValueError(
                f"invalid markPrice payload for {envelope.symbol}"
            )
        return MarkPriceData(
            envelope.symbol,
            mark_price,
            index_price,
            funding_rate,
            datetime.fromtimestamp(next_funding_timestamp),
            datetime.fromtimestamp(exchange_timestamp),
            exchange_timestamp=exchange_timestamp,
            received_timestamp=envelope.received_timestamp,
            received_monotonic=envelope.received_monotonic,
            clock_offset_ms=envelope.clock_offset_ms,
            corrected_received_timestamp=(
                envelope.corrected_received_timestamp
            ),
            next_funding_timestamp=next_funding_timestamp,
        )

    @staticmethod
    def _depth(envelope: BinanceMarketEnvelope) -> dict:
        data = dict(envelope.data)
        data["_local_received_timestamp"] = envelope.received_timestamp
        data["_local_received_monotonic"] = envelope.received_monotonic
        data["_local_clock_offset_ms"] = envelope.clock_offset_ms
        data["_local_corrected_received_timestamp"] = (
            envelope.corrected_received_timestamp
        )
        return data
