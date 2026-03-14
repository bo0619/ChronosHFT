# file: data/orderbook.py

import time
from datetime import datetime

from event.type import OrderBook, OrderBookGapError
from infrastructure.logger import logger


class LocalOrderBook:
    def __init__(self, symbol):
        self.symbol = symbol
        self.bids = {}
        self.asks = {}
        self.last_update_id = 0
        self.initialized = False
        self.last_exchange_ts = 0.0
        self.last_received_ts = 0.0

    def init_snapshot(self, snapshot_data: dict):
        self.bids.clear()
        self.asks.clear()

        for entry in snapshot_data['bids']:
            self.bids[float(entry[0])] = float(entry[1])

        for entry in snapshot_data['asks']:
            self.asks[float(entry[0])] = float(entry[1])

        self.last_update_id = snapshot_data['lastUpdateId']
        self.initialized = True
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

        for entry in delta['b']:
            price = float(entry[0])
            qty = float(entry[1])
            if qty == 0:
                if price in self.bids:
                    del self.bids[price]
            else:
                self.bids[price] = qty

        for entry in delta['a']:
            price = float(entry[0])
            qty = float(entry[1])
            if qty == 0:
                if price in self.asks:
                    del self.asks[price]
            else:
                self.asks[price] = qty

        self.last_update_id = u
        self.last_exchange_ts = self._extract_exchange_ts(delta)
        self.last_received_ts = time.time()

    def generate_event_data(self):
        if not self.initialized:
            return None

        received_ts = self.last_received_ts or time.time()
        return OrderBook(
            symbol=self.symbol,
            exchange="BINANCE",
            datetime=datetime.fromtimestamp(received_ts),
            bids=self.bids.copy(),
            asks=self.asks.copy(),
            exchange_timestamp=self.last_exchange_ts,
            received_timestamp=received_ts,
        )

    def _extract_exchange_ts(self, delta: dict) -> float:
        raw_ts = delta.get('E') or delta.get('T') or 0
        return float(raw_ts) / 1000.0 if raw_ts else 0.0