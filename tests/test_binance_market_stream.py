import pytest

from event.type import EVENT_AGG_TRADE, EVENT_MARK_PRICE, EVENT_ORDERBOOK
from gateway.binance.market_stream import BinanceMarketStreamParser


def make_envelope(stream, data):
    return BinanceMarketStreamParser.envelope(
        {"stream": stream, "data": data},
        received_timestamp=2.5,
        received_monotonic=7.0,
        corrected_received_timestamp=2.502,
        clock_offset_ms=2.0,
    )


def test_agg_trade_normalization_is_transport_independent():
    envelope = make_envelope(
        "btcusdt@aggTrade",
        {
            "E": 2_000,
            "T": 1_900,
            "s": "BTCUSDT",
            "a": 42,
            "p": "101.5",
            "q": "0.25",
            "m": True,
        },
    )

    update = BinanceMarketStreamParser.normalize(envelope)

    assert envelope.requires_ingress_freshness
    assert envelope.event_time_ms == 2_000
    assert update.event_type == EVENT_AGG_TRADE
    assert update.payload.trade_id == 42
    assert update.payload.price == 101.5
    assert update.payload.exchange_timestamp == 1.9
    assert update.payload.received_monotonic == 7.0


@pytest.mark.parametrize(
    ("field", "value"),
    [("p", "NaN"), ("p", "0"), ("q", "inf"), ("q", "0")],
)
def test_agg_trade_rejects_invalid_numeric_truth(field, value):
    data = {
        "E": 2_000,
        "T": 1_900,
        "s": "BTCUSDT",
        "a": 42,
        "p": "101.5",
        "q": "0.25",
        "m": False,
    }
    data[field] = value

    with pytest.raises(ValueError, match="invalid aggTrade payload"):
        BinanceMarketStreamParser.normalize(
            make_envelope("btcusdt@aggTrade", data)
        )


def test_mark_price_uses_event_time_not_next_funding_time():
    envelope = make_envelope(
        "btcusdt@markPrice@1s",
        {
            "E": 2_000,
            "T": 3_000,
            "s": "BTCUSDT",
            "p": "100.5",
            "i": "100.0",
            "r": "0.0001",
        },
    )

    update = BinanceMarketStreamParser.normalize(envelope)

    assert not envelope.requires_ingress_freshness
    assert envelope.event_time_ms == 2_000
    assert update.event_type == EVENT_MARK_PRICE
    assert update.payload.exchange_timestamp == 2.0
    assert update.payload.next_funding_timestamp == 3.0


def test_depth_metadata_is_copied_without_mutating_wire_payload():
    wire_data = {"E": 2_000, "s": "BTCUSDT", "U": 1, "u": 2}
    envelope = make_envelope("btcusdt@depth@100ms", wire_data)

    update = BinanceMarketStreamParser.normalize(envelope)

    assert update.event_type == EVENT_ORDERBOOK
    assert update.payload["_local_received_timestamp"] == 2.5
    assert update.payload["_local_received_monotonic"] == 7.0
    assert update.payload["_local_clock_offset_ms"] == 2.0
    assert "_local_received_timestamp" not in wire_data


def test_unknown_combined_stream_is_ignored():
    envelope = make_envelope(
        "btcusdt@unsupported",
        {"E": 2_000, "s": "BTCUSDT"},
    )

    assert BinanceMarketStreamParser.normalize(envelope) is None
