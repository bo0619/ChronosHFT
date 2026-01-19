# file: data/orderbook.py

from event.type import OrderBook
from datetime import datetime

class LocalOrderBook:
    def __init__(self, symbol):
        self.symbol = symbol
        self.bids = {} 
        self.asks = {} 
        self.last_update_id = 0 
        self.initialized = False

    def init_snapshot(self, snapshot_data: dict):
        self.bids.clear()
        self.asks.clear()
        for entry in snapshot_data['bids']:
            price = float(entry[0])
            vol = float(entry[1])
            self.bids[price] = vol
        for entry in snapshot_data['asks']:
            price = float(entry[0])
            vol = float(entry[1])
            self.asks[price] = vol
        self.last_update_id = snapshot_data['lastUpdateId']
        self.initialized = True
        print(f"[LocalOrderBook] {self.symbol} 初始化完成. LastUpdateId={self.last_update_id}")

    def process_delta(self, delta: dict):
        u = delta['u'] 
        if not self.initialized: return
        if u < self.last_update_id: return
        for entry in delta['b']:
            price = float(entry[0])
            qty = float(entry[1])
            if qty == 0:
                if price in self.bids: del self.bids[price]
            else:
                self.bids[price] = qty
        for entry in delta['a']:
            price = float(entry[0])
            qty = float(entry[1])
            if qty == 0:
                if price in self.asks: del self.asks[price]
            else:
                self.asks[price] = qty
        self.last_update_id = u

    def generate_event_data(self):
        ob = OrderBook(
            symbol=self.symbol,
            exchange="BINANCE",
            datetime=datetime.now(),
            bids=self.bids.copy(), 
            asks=self.asks.copy()
        )
        return ob