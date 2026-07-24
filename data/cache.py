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
        self.book_update_wall_times = {}
        self.mark_update_wall_times = {}
        self.trade_update_wall_times = {}
        self._lock = threading.RLock()

    def update_book(self, ob: OrderBook):
        received_at = self._received_monotonic(ob)
        received_wall = self._received_wall(ob)
        with self._lock:
            self.books[ob.symbol] = ob
            self.book_update_times[ob.symbol] = received_at
            self.book_update_wall_times[ob.symbol] = received_wall

    def update_mark_price(self, mp: MarkPriceData):
        update_time = self._received_monotonic(mp)
        received_wall = self._received_wall(mp)
        with self._lock:
            self.mark_prices[mp.symbol] = mp
            self.mark_update_times[mp.symbol] = update_time
            self.mark_update_wall_times[mp.symbol] = received_wall

    def update_trade(self, tr: AggTradeData):
        update_time = self._received_monotonic(tr)
        received_wall = self._received_wall(tr)
        with self._lock:
            self.last_trades[tr.symbol] = tr
            self.trade_update_times[tr.symbol] = update_time
            self.trade_update_wall_times[tr.symbol] = received_wall

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
        """Return prices and source ages using the process monotonic clock."""
        now = time.perf_counter() if now is None else float(now)
        with self._lock:
            mark = self.mark_prices.get(symbol)
            book = self.books.get(symbol)
            trade = self.last_trades.get(symbol)
            mark_time = float(self.mark_update_times.get(symbol, 0.0) or 0.0)
            book_time = float(self.book_update_times.get(symbol, 0.0) or 0.0)
            trade_time = float(self.trade_update_times.get(symbol, 0.0) or 0.0)
            mark_wall_time = float(
                self.mark_update_wall_times.get(symbol, 0.0) or 0.0
            )
            book_wall_time = float(
                self.book_update_wall_times.get(symbol, 0.0) or 0.0
            )
            trade_wall_time = float(
                self.trade_update_wall_times.get(symbol, 0.0) or 0.0
            )

            bid = ask = 0.0
            if book is not None:
                bid = float(book.get_best_bid()[0] or 0.0)
                ask = float(book.get_best_ask()[0] or 0.0)

            next_funding_epoch = float(
                getattr(mark, "next_funding_timestamp", 0.0) or 0.0
            )
            if next_funding_epoch <= 0.0 and mark is not None:
                next_funding_time = getattr(mark, "next_funding_time", None)
                timestamp = getattr(next_funding_time, "timestamp", None)
                if callable(timestamp):
                    try:
                        next_funding_epoch = float(timestamp())
                    except (OSError, OverflowError, TypeError, ValueError):
                        next_funding_epoch = 0.0

            return {
                "symbol": symbol,
                "mark_price": float(getattr(mark, "mark_price", 0.0) or 0.0),
                "funding_rate": (
                    getattr(mark, "funding_rate", None)
                    if mark is not None
                    else None
                ),
                "next_funding_epoch": next_funding_epoch,
                "mark_exchange_timestamp": float(
                    getattr(mark, "exchange_timestamp", 0.0) or 0.0
                ),
                "mark_received_monotonic": float(
                    getattr(mark, "received_monotonic", 0.0) or 0.0
                ),
                "mark_corrected_received_timestamp": float(
                    getattr(
                        mark,
                        "corrected_received_timestamp",
                        0.0,
                    )
                    or 0.0
                ),
                "bid_price": bid,
                "ask_price": ask,
                "last_trade_price": float(getattr(trade, "price", 0.0) or 0.0),
                "mark_age_ms": max(0.0, (now - mark_time) * 1000.0) if mark_time else None,
                "book_age_ms": max(0.0, (now - book_time) * 1000.0) if book_time else None,
                "trade_age_ms": max(0.0, (now - trade_time) * 1000.0) if trade_time else None,
                "mark_update_time": mark_time,
                "book_update_time": book_time,
                "trade_update_time": trade_time,
                "mark_update_wall_time": mark_wall_time,
                "book_update_wall_time": book_wall_time,
                "trade_update_wall_time": trade_wall_time,
                "update_clock": "monotonic",
            }

    @staticmethod
    def _received_monotonic(data) -> float:
        return float(
            getattr(data, "received_monotonic", 0.0)
            or getattr(data, "dispatch_monotonic", 0.0)
            or time.perf_counter()
        )

    @staticmethod
    def _received_wall(data) -> float:
        return float(
            getattr(data, "received_timestamp", 0.0)
            or getattr(data, "dispatch_timestamp", 0.0)
            or time.time()
        )


data_cache = LiveDataCache()
