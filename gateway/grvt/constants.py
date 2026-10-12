"""GRVT mainnet market-data endpoints and public stream settings."""

from data.ref_data import GRVT_MARKET_DATA_URL_MAIN

MARKET_DATA_URL_MAIN = GRVT_MARKET_DATA_URL_MAIN
WS_URL_MAIN = "wss://market-data.grvt.io/ws/full"

BOOK_STREAM = "v1.book.d"
TRADE_STREAM = "v1.trade"
TICKER_STREAM = "v1.ticker.s"
# Fastest delta-book rate GRVT publishes (50, 100, 500, 1000 ms).
BOOK_RATE_MS = 50
# Trade selector limit (50, 200, 500, 1000); it sizes the replay on subscribe.
TRADE_LIMIT = 50
# Full ticker snapshots (500, 1000, 5000 ms) carry mark, index and funding.
TICKER_RATE_MS = 500
