import math
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
        symbol = str(getattr(mp, "symbol", "") or "").upper().strip()
        try:
            mark_price = float(getattr(mp, "mark_price", 0.0))
            raw_index_price = getattr(mp, "index_price", None)
            index_price = (
                mark_price
                if raw_index_price is None
                else float(raw_index_price)
            )
            raw_funding_rate = getattr(mp, "funding_rate", None)
            funding_rate = (
                0.0
                if raw_funding_rate is None
                else float(raw_funding_rate)
            )
        except (TypeError, ValueError):
            return False
        if (
            not symbol
            or not math.isfinite(mark_price)
            or mark_price <= 0.0
            or not math.isfinite(index_price)
            or index_price <= 0.0
            or not math.isfinite(funding_rate)
        ):
            return False
        update_time = self._received_monotonic(mp)
        received_wall = self._received_wall(mp)
        mp.symbol = symbol
        mp.mark_price = mark_price
        mp.index_price = index_price
        mp.funding_rate = funding_rate
        with self._lock:
            self.mark_prices[symbol] = mp
            self.mark_update_times[symbol] = update_time
            self.mark_update_wall_times[symbol] = received_wall
        return True

    def update_trade(self, tr: AggTradeData):
        symbol = str(getattr(tr, "symbol", "") or "").upper().strip()
        try:
            trade_id = int(getattr(tr, "trade_id", -1))
            price = float(getattr(tr, "price", 0.0))
            quantity = float(getattr(tr, "quantity", 0.0))
        except (TypeError, ValueError):
            return False
        if (
            not symbol
            or trade_id < 0
            or not math.isfinite(price)
            or price <= 0.0
            or not math.isfinite(quantity)
            or quantity <= 0.0
        ):
            return False
        update_time = self._received_monotonic(tr)
        received_wall = self._received_wall(tr)
        tr.symbol = symbol
        tr.trade_id = trade_id
        tr.price = price
        tr.quantity = quantity
        with self._lock:
            self.last_trades[symbol] = tr
            self.trade_update_times[symbol] = update_time
            self.trade_update_wall_times[symbol] = received_wall
        return True

    def get_book(self, symbol):
        with self._lock:
            return self.books.get(symbol)

    def get_mark_price(self, symbol):
        with self._lock:
            data = self.mark_prices.get(symbol)
            if data:
                mark_price = self._finite_or_zero(data.mark_price)
                if mark_price > 0.0:
                    return mark_price

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
            return self._finite_or_zero(trade.price) if trade else 0.0

    def get_risk_snapshot(self, symbol: str, now: float = None) -> dict:
        """Return prices and source ages using the process monotonic clock."""
        now = time.perf_counter() if now is None else float(now)
        if not math.isfinite(now):
            now = time.perf_counter()
        with self._lock:
            mark = self.mark_prices.get(symbol)
            book = self.books.get(symbol)
            trade = self.last_trades.get(symbol)
            mark_time = self._finite_or_zero(
                self.mark_update_times.get(symbol, 0.0)
            )
            book_time = self._finite_or_zero(
                self.book_update_times.get(symbol, 0.0)
            )
            trade_time = self._finite_or_zero(
                self.trade_update_times.get(symbol, 0.0)
            )
            mark_wall_time = self._finite_or_zero(
                self.mark_update_wall_times.get(symbol, 0.0) or 0.0
            )
            book_wall_time = self._finite_or_zero(
                self.book_update_wall_times.get(symbol, 0.0) or 0.0
            )
            trade_wall_time = self._finite_or_zero(
                self.trade_update_wall_times.get(symbol, 0.0) or 0.0
            )

            bid = ask = 0.0
            if book is not None:
                bid = self._finite_or_zero(book.get_best_bid()[0])
                ask = self._finite_or_zero(book.get_best_ask()[0])

            next_funding_epoch = self._finite_or_zero(
                getattr(mark, "next_funding_timestamp", 0.0) or 0.0
            )
            if next_funding_epoch <= 0.0 and mark is not None:
                next_funding_time = getattr(mark, "next_funding_time", None)
                timestamp = getattr(next_funding_time, "timestamp", None)
                if callable(timestamp):
                    try:
                        next_funding_epoch = self._finite_or_zero(
                            timestamp()
                        )
                    except (OSError, OverflowError, TypeError, ValueError):
                        next_funding_epoch = 0.0

            return {
                "symbol": symbol,
                "mark_price": self._finite_or_zero(
                    getattr(mark, "mark_price", 0.0)
                ),
                "funding_rate": (
                    getattr(mark, "funding_rate", None)
                    if mark is not None
                    else None
                ),
                "next_funding_epoch": next_funding_epoch,
                "mark_exchange_timestamp": self._finite_or_zero(
                    getattr(mark, "exchange_timestamp", 0.0) or 0.0
                ),
                "mark_received_monotonic": self._finite_or_zero(
                    getattr(mark, "received_monotonic", 0.0) or 0.0
                ),
                "mark_corrected_received_timestamp": self._finite_or_zero(
                    getattr(
                        mark,
                        "corrected_received_timestamp",
                        0.0,
                    )
                    or 0.0
                ),
                "bid_price": bid,
                "ask_price": ask,
                "last_trade_price": self._finite_or_zero(
                    getattr(trade, "price", 0.0)
                ),
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
        value = float(
            getattr(data, "received_monotonic", 0.0)
            or getattr(data, "dispatch_monotonic", 0.0)
            or time.perf_counter()
        )
        return value if math.isfinite(value) and value > 0.0 else time.perf_counter()

    @staticmethod
    def _received_wall(data) -> float:
        value = float(
            getattr(data, "received_timestamp", 0.0)
            or getattr(data, "dispatch_timestamp", 0.0)
            or time.time()
        )
        return value if math.isfinite(value) and value > 0.0 else time.time()

    @staticmethod
    def _finite_or_zero(value) -> float:
        try:
            normalized = float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return normalized if math.isfinite(normalized) else 0.0


data_cache = LiveDataCache()
