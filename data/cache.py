import threading
import time

from event.type import AggTradeData, MarkPriceData, OrderBook


class LiveDataCache:
    """Thread-safe latest-value cache with per-symbol freshness watermarks."""

    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "books"):
            return
        self.books = {}
        self.mark_prices = {}
        self.last_trades = {}
        self.book_update_times = {}
        self.mark_update_times = {}
        self.trade_update_times = {}
        self._lock = threading.RLock()

    def update_book(self, ob: OrderBook):
        received_at = float(getattr(ob, "received_timestamp", 0.0) or time.time())
        with self._lock:
            self.books[ob.symbol] = ob
            self.book_update_times[ob.symbol] = received_at

    def update_mark_price(self, mp: MarkPriceData):
        source_time = getattr(mp, "datetime", None)
        update_time = source_time.timestamp() if source_time is not None else time.time()
        with self._lock:
            self.mark_prices[mp.symbol] = mp
            self.mark_update_times[mp.symbol] = update_time

    def update_trade(self, tr: AggTradeData):
        source_time = getattr(tr, "datetime", None)
        update_time = source_time.timestamp() if source_time is not None else time.time()
        with self._lock:
            self.last_trades[tr.symbol] = tr
            self.trade_update_times[tr.symbol] = update_time

    def get_book(self, symbol):
        with self._lock:
            return self.books.get(symbol)

    def get_mark_price(self, symbol):
        with self._lock:
            data = self.mark_prices.get(symbol)
            if data:
                return data.mark_price

            book = self.books.get(symbol)
            if book:
                bid, _ = book.get_best_bid()
                ask, _ = book.get_best_ask()
                if bid > 0 and ask > 0:
                    return (bid + ask) / 2
        return 0.0

    def get_best_quote(self, symbol):
        with self._lock:
            book = self.books.get(symbol)
            if not book:
                return 0.0, 0.0
            return book.get_best_bid()[0], book.get_best_ask()[0]

    def get_last_trade_price(self, symbol):
        with self._lock:
            trade = self.last_trades.get(symbol)
            return trade.price if trade else 0.0

    def get_risk_snapshot(self, symbol: str, now: float = None) -> dict:
        """Return native prices and the age of each source used by risk."""
        now = time.time() if now is None else float(now)
        with self._lock:
            mark = self.mark_prices.get(symbol)
            book = self.books.get(symbol)
            trade = self.last_trades.get(symbol)
            mark_time = float(self.mark_update_times.get(symbol, 0.0) or 0.0)
            book_time = float(self.book_update_times.get(symbol, 0.0) or 0.0)
            trade_time = float(self.trade_update_times.get(symbol, 0.0) or 0.0)

            bid = ask = 0.0
            if book is not None:
                bid = float(book.get_best_bid()[0] or 0.0)
                ask = float(book.get_best_ask()[0] or 0.0)

            return {
                "symbol": symbol,
                "mark_price": float(getattr(mark, "mark_price", 0.0) or 0.0),
                "bid_price": bid,
                "ask_price": ask,
                "last_trade_price": float(getattr(trade, "price", 0.0) or 0.0),
                "mark_age_ms": max(0.0, (now - mark_time) * 1000.0) if mark_time else None,
                "book_age_ms": max(0.0, (now - book_time) * 1000.0) if book_time else None,
                "trade_age_ms": max(0.0, (now - trade_time) * 1000.0) if trade_time else None,
                "mark_update_time": mark_time,
                "book_update_time": book_time,
                "trade_update_time": trade_time,
            }


data_cache = LiveDataCache()
