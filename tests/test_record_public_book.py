"""Stream selection of the Binance research recorder (no network)."""

from pathlib import Path

import pytest

from scripts.record_public_book import WS_ROOT, Recorder


def recorder(**kwargs):
    return Recorder(["XRPUSDT", "SUIUSDT"], Path("/tmp/unused"), 1.0, 2.0, **kwargs)


def test_default_shares_one_book_and_one_trade_connection():
    urls = recorder().urls()
    assert urls == [f"{WS_ROOT}/public/stream?streams=xrpusdt@depth5@100ms/xrpusdt@bookTicker/"
                    "suiusdt@depth5@100ms/suiusdt@bookTicker",
                    f"{WS_ROOT}/market/stream?streams=xrpusdt@aggTrade/suiusdt@aggTrade"]


def test_ticker_only_with_a_connection_per_symbol():
    assert recorder(ticker_only=True, connection_per_symbol=True).urls() == [
        f"{WS_ROOT}/public/stream?streams=xrpusdt@bookTicker",
        f"{WS_ROOT}/public/stream?streams=suiusdt@bookTicker"]
    assert recorder(ticker_only=True).urls() == [
        f"{WS_ROOT}/public/stream?streams=xrpusdt@bookTicker/suiusdt@bookTicker"]


def test_connection_per_symbol_keeps_every_stream():
    urls = recorder(connection_per_symbol=True).urls()
    assert len(urls) == 4 and all(("xrpusdt" in u) != ("suiusdt" in u) for u in urls)


def test_ticker_only_excludes_all_streams():
    with pytest.raises(ValueError):
        recorder(ticker_only=True, all_streams=True)
