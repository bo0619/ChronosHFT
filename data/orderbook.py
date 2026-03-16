# file: data/orderbook.py

import heapq
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
        self.publish_depth_levels = max(1, int(publish_depth_levels or 1))
        self.emit_full_book = bool(emit_full_book)
        self.best_bid_price = 0.0
        self.best_bid_volume = 0.0
        self.best_ask_price = 0.0
        self.best_ask_volume = 0.0
        self.top_bids = ()
        self.top_asks = ()

    def init_snapshot(self, snapshot_data: dict):
        self.bids.clear()
        self.asks.clear()

        for entry in snapshot_data['bids']:
            self.bids[float(entry[0])] = float(entry[1])

        for entry in snapshot_data['asks']:
            self.asks[float(entry[0])] = float(entry[1])

        self.last_update_id = snapshot_data['lastUpdateId']
        self.initialized = True
        self._recompute_best_quotes()
        logger.info(f"[{self.symbol}] OrderBook Snapshot Loaded. ID={self.last_update_id}")

    def process_delta(self, delta: dict):
        """
        Process Binance incremental depth updates.
        """
        u = delta['u']
        U = delta['U']
        pu = delta['pu']

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
        for entry in delta['b']:
            bid_levels_dirty = self._apply_bid_update(float(entry[0]), float(entry[1])) or bid_levels_dirty

        for entry in delta['a']:
            ask_levels_dirty = self._apply_ask_update(float(entry[0]), float(entry[1])) or ask_levels_dirty

        if bid_levels_dirty:
            self._recompute_published_bid_levels()
        if ask_levels_dirty:
            self._recompute_published_ask_levels()

        self.last_update_id = u
        self.last_exchange_ts = self._extract_exchange_ts(delta)
        self.last_received_ts = time.time()

    def generate_event_data(self):
        if not self.initialized:
            return None

        received_ts = self.last_received_ts or time.time()
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
            best_bid_price=self.best_bid_price,
            best_bid_volume=self.best_bid_volume,
            best_ask_price=self.best_ask_price,
            best_ask_volume=self.best_ask_volume,
            depth_levels=depth_levels,
        )

    def _extract_exchange_ts(self, delta: dict) -> float:
        raw_ts = delta.get('E') or delta.get('T') or 0
        return float(raw_ts) / 1000.0 if raw_ts else 0.0

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
