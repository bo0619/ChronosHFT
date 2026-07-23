# file: data/orderbook.py

import heapq
import math
import time
from datetime import datetime

from event.type import OrderBook, OrderBookGapError
from infrastructure.logger import logger


class LocalOrderBook:
    def __init__(self, symbol, publish_depth_levels=5, emit_full_book=False):
        self.symbol = symbol
        self.bids = {}
        self.asks = {}
        self.last_update_id = 0
        self.initialized = False
        self.last_exchange_ts = 0.0
        self.last_received_ts = 0.0
        self.last_received_monotonic = 0.0
        self.last_clock_offset_ms = None
        self.last_corrected_received_ts = 0.0
        self.publish_depth_levels = max(1, int(publish_depth_levels or 1))
        self.emit_full_book = bool(emit_full_book)
        self.best_bid_price = 0.0
        self.best_bid_volume = 0.0
        self.best_ask_price = 0.0
        self.best_ask_volume = 0.0
        self.top_bids = ()
        self.top_asks = ()

    def init_snapshot(self, snapshot_data: dict):
        self.initialized = False
        bids = self._parse_levels(snapshot_data["bids"], "bid")
        asks = self._parse_levels(snapshot_data["asks"], "ask")
        last_update_id = int(snapshot_data["lastUpdateId"])
        if last_update_id < 0:
            raise ValueError("lastUpdateId must be non-negative")
        self._validate_book(bids, asks)

        self.bids = bids
        self.asks = asks
        self.last_update_id = last_update_id
        self.initialized = True
        self._recompute_best_quotes()
        logger.info(f"[{self.symbol}] OrderBook Snapshot Loaded. ID={self.last_update_id}")

    def process_delta(
        self,
        delta: dict,
        *,
        received_timestamp: float = None,
        received_monotonic: float = None,
        clock_offset_ms: float = None,
        corrected_received_timestamp: float = None,
    ):
        """
        Process Binance incremental depth updates.

        ``received_timestamp`` is the local wall-clock timestamp captured at
        the WebSocket callback boundary. ``received_monotonic`` is the paired
        process-local monotonic timestamp used for freshness and processing
        latency.  The private dictionary keys are retained so buffered deltas
        can carry the original ingress time through snapshot resynchronization.
        """
        if received_timestamp is None:
            received_timestamp = delta.get("_local_received_timestamp")
        if received_monotonic is None:
            received_monotonic = delta.get("_local_received_monotonic")
        if clock_offset_ms is None:
            clock_offset_ms = delta.get("_local_clock_offset_ms")
        if corrected_received_timestamp is None:
            corrected_received_timestamp = delta.get(
                "_local_corrected_received_timestamp"
            )
        if not received_timestamp:
            received_timestamp = time.time()
        if not received_monotonic:
            received_monotonic = time.perf_counter()
        if clock_offset_ms is not None:
            clock_offset_ms = float(clock_offset_ms)
        if (
            corrected_received_timestamp is None
            and clock_offset_ms is not None
        ):
            corrected_received_timestamp = (
                float(received_timestamp) + clock_offset_ms / 1000.0
            )

        try:
            u = int(delta["u"])
            U = int(delta["U"])
            pu = int(delta["pu"])
            if U < 0 or u < U or pu < 0:
                raise ValueError(f"invalid sequence U={U} u={u} pu={pu}")
            bid_updates = self._parse_levels(delta["b"], "bid", keep_zero=True)
            ask_updates = self._parse_levels(delta["a"], "ask", keep_zero=True)
        except (KeyError, TypeError, ValueError) as exc:
            self._reject_integrity(str(exc))

        if not self.initialized:
            return

        if u < self.last_update_id:
            return

        if pu != self.last_update_id:
            if U <= self.last_update_id and u >= self.last_update_id:
                pass
            else:
                logger.error(f"[{self.symbol}] OrderBook Gap Detected! Local={self.last_update_id}, Remote_PU={pu}")
                self.initialized = False
                raise OrderBookGapError(f"Gap detected for {self.symbol}")

        bid_levels_dirty = False
        ask_levels_dirty = False
        for price, qty in bid_updates.items():
            bid_levels_dirty = self._apply_bid_update(price, qty) or bid_levels_dirty

        for price, qty in ask_updates.items():
            ask_levels_dirty = self._apply_ask_update(price, qty) or ask_levels_dirty

        if bid_levels_dirty:
            self._recompute_published_bid_levels()
        if ask_levels_dirty:
            self._recompute_published_ask_levels()

        try:
            self._validate_book(self.bids, self.asks)
        except ValueError as exc:
            self._reject_integrity(str(exc))

        self.last_update_id = u
        self.last_exchange_ts = self._extract_exchange_ts(delta)
        self.last_received_ts = float(received_timestamp)
        self.last_received_monotonic = float(received_monotonic)
        self.last_clock_offset_ms = clock_offset_ms
        self.last_corrected_received_ts = float(
            corrected_received_timestamp or 0.0
        )

    def generate_event_data(self):
        if not self.initialized:
            return None

        received_ts = self.last_received_ts or time.time()
        received_monotonic = self.last_received_monotonic or time.perf_counter()
        clock_offset_ms = self.last_clock_offset_ms
        if clock_offset_ms is not None:
            clock_offset_ms = float(clock_offset_ms)
        corrected_received_ts = float(self.last_corrected_received_ts or 0.0)
        if not corrected_received_ts and clock_offset_ms is not None:
            corrected_received_ts = received_ts + clock_offset_ms / 1000.0
        dispatch_ts = time.time()
        dispatch_monotonic = time.perf_counter()
        bids = self.bids.copy() if self.emit_full_book else {price: volume for price, volume in self.top_bids}
        asks = self.asks.copy() if self.emit_full_book else {price: volume for price, volume in self.top_asks}
        depth_levels = max(len(bids), len(asks)) if self.emit_full_book else max(len(self.top_bids), len(self.top_asks))
        return OrderBook(
            symbol=self.symbol,
            exchange="BINANCE",
            datetime=datetime.fromtimestamp(received_ts),
            bids=bids,
            asks=asks,
            top_bids=tuple(self.top_bids),
            top_asks=tuple(self.top_asks),
            exchange_timestamp=self.last_exchange_ts,
            received_timestamp=received_ts,
            received_monotonic=received_monotonic,
            dispatch_timestamp=dispatch_ts,
            dispatch_monotonic=dispatch_monotonic,
            clock_offset_ms=clock_offset_ms,
            corrected_received_timestamp=corrected_received_ts,
            best_bid_price=self.best_bid_price,
            best_bid_volume=self.best_bid_volume,
            best_ask_price=self.best_ask_price,
            best_ask_volume=self.best_ask_volume,
            depth_levels=depth_levels,
        )

    def _extract_exchange_ts(self, delta: dict) -> float:
        raw_ts = delta.get('E') or delta.get('T') or 0
        return float(raw_ts) / 1000.0 if raw_ts else 0.0

    @staticmethod
    def _parse_levels(entries, side: str, *, keep_zero: bool = False):
        levels = {}
        for entry in entries:
            if len(entry) < 2:
                raise ValueError(f"invalid {side} level")
            price = float(entry[0])
            qty = float(entry[1])
            if not math.isfinite(price) or price <= 0.0:
                raise ValueError(f"invalid {side} price: {price!r}")
            if not math.isfinite(qty) or qty < 0.0:
                raise ValueError(f"invalid {side} quantity: {qty!r}")
            if qty > 0.0 or keep_zero:
                levels[price] = qty
        return levels

    @staticmethod
    def _validate_book(bids, asks):
        if not bids or not asks:
            raise ValueError("order book side is empty")
        best_bid = max(bids)
        best_ask = min(asks)
        if best_bid >= best_ask:
            raise ValueError(
                f"crossed order book best_bid={best_bid} best_ask={best_ask}"
            )

    def _reject_integrity(self, reason: str):
        self.initialized = False
        logger.error(f"[{self.symbol}] OrderBook integrity failure: {reason}")
        raise OrderBookGapError(
            f"Order book integrity failure for {self.symbol}: {reason}"
        )

    def _apply_bid_update(self, price: float, qty: float):
        current_best = self.best_bid_price
        levels_dirty = False
        if self._level_frontier_impacted(price, self.top_bids, descending=True):
            levels_dirty = True
        if qty == 0.0:
            if price in self.bids:
                del self.bids[price]
                if price == current_best:
                    self._recompute_best_bid()
            return levels_dirty

        self.bids[price] = qty
        if price >= current_best:
            self.best_bid_price = price
            self.best_bid_volume = qty
        return levels_dirty

    def _apply_ask_update(self, price: float, qty: float):
        current_best = self.best_ask_price
        levels_dirty = False
        if self._level_frontier_impacted(price, self.top_asks, descending=False):
            levels_dirty = True
        if qty == 0.0:
            if price in self.asks:
                del self.asks[price]
                if current_best == 0.0 or price == current_best:
                    self._recompute_best_ask()
            return levels_dirty

        self.asks[price] = qty
        if current_best == 0.0 or price <= current_best:
            self.best_ask_price = price
            self.best_ask_volume = qty
        return levels_dirty

    def _recompute_best_quotes(self):
        self._recompute_best_bid()
        self._recompute_best_ask()
        self._recompute_published_levels()

    def _recompute_best_bid(self):
        if not self.bids:
            self.best_bid_price = 0.0
            self.best_bid_volume = 0.0
            return
        price = max(self.bids.keys())
        self.best_bid_price = price
        self.best_bid_volume = self.bids[price]

    def _recompute_best_ask(self):
        if not self.asks:
            self.best_ask_price = 0.0
            self.best_ask_volume = 0.0
            return
        price = min(self.asks.keys())
        self.best_ask_price = price
        self.best_ask_volume = self.asks[price]

    def _recompute_published_levels(self):
        self._recompute_published_bid_levels()
        self._recompute_published_ask_levels()

    def _recompute_published_bid_levels(self):
        depth = self.publish_depth_levels
        self.top_bids = tuple(
            heapq.nlargest(depth, self.bids.items(), key=lambda item: item[0])
        )

    def _recompute_published_ask_levels(self):
        depth = self.publish_depth_levels
        self.top_asks = tuple(
            heapq.nsmallest(depth, self.asks.items(), key=lambda item: item[0])
        )

    def _level_frontier_impacted(self, price: float, levels, descending: bool):
        if self.emit_full_book:
            return True
        if not levels:
            return True
        level_prices = {level_price for level_price, _ in levels}
        if price in level_prices:
            return True
        if len(levels) < self.publish_depth_levels:
            return True
        frontier_price = levels[-1][0]
        if descending:
            return price >= frontier_price
        return price <= frontier_price
