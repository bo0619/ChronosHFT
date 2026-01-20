# file: data/orderbook.py

from event.type import OrderBook, OrderBookGapError
from datetime import datetime
from infrastructure.logger import logger

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
            self.bids[float(entry[0])] = float(entry[1])
            
        for entry in snapshot_data['asks']:
            self.asks[float(entry[0])] = float(entry[1])
            
        self.last_update_id = snapshot_data['lastUpdateId']
        self.initialized = True
        logger.info(f"[{self.symbol}] OrderBook Snapshot Loaded. ID={self.last_update_id}")

    def process_delta(self, delta: dict):
        """
        处理增量更新
        Binance Logic:
        1. Drop any event where u is < lastUpdateId in the snapshot.
        2. The first processed event should have U <= lastUpdateId+1 AND u >= lastUpdateId+1.
        3. While listening to the stream, each new event's pu should be equal to the previous event's u.
        """
        u = delta['u'] # Final update ID
        U = delta['U'] # First update ID
        pu = delta['pu'] # Previous update ID

        if not self.initialized:
            return

        # 1. 丢弃过期数据 (Discard stale data)
        if u < self.last_update_id:
            return

        # 2. 丢包检测 (Gap Detection)
        # 正常情况下，新包的 pu 应该等于本地的 last_update_id
        # 但是，如果是快照后的第一个包，只需要保证 U <= last_update_id + 1 <= u
        
        if pu != self.last_update_id:
            # 还有一个特例：快照刚下载完，可能收到重叠的包，这时候 pu < last_update_id，已经在上面 return 了
            # 如果走到这里，说明 pu > last_update_id，也就是中间缺了数据
            
            # 检查是否是刚初始化的衔接包
            # 币安要求：The first processed event should have U <= lastUpdateId AND u >= lastUpdateId
            if U <= self.last_update_id and u >= self.last_update_id:
                # 这是一个衔接包，数据是连续的，可以通过
                pass
            else:
                # 真正的丢包发生了！
                logger.error(f"[{self.symbol}] OrderBook Gap Detected! Local={self.last_update_id}, Remote_PU={pu}")
                self.initialized = False # 标记为失效
                raise OrderBookGapError(f"Gap detected for {self.symbol}")

        # 3. 更新盘口
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
        
        # 4. 更新 ID
        self.last_update_id = u

    def generate_event_data(self):
        # 只有初始化成功才发送数据，防止脏数据污染策略
        if not self.initialized:
            return None
            
        return OrderBook(
            symbol=self.symbol,
            exchange="BINANCE",
            datetime=datetime.now(),
            bids=self.bids.copy(), 
            asks=self.asks.copy()
        )