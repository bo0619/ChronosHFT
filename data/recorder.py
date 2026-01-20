# file: data/recorder.py

import os
import pandas as pd
from datetime import datetime
from infrastructure.logger import logger
from event.type import OrderBook, AggTradeData, Event, EVENT_ORDERBOOK, EVENT_AGG_TRADE

class DataRecorder:
    """
    高性能录制器 (HDF5 + Buffer)
    """
    def __init__(self, engine, symbols: list):
        self.engine = engine
        self.symbols = symbols
        self.save_path = "storage"
        
        # Buffer: Symbol -> List[Dict]
        self.depth_buffer = {s: [] for s in symbols}
        self.trade_buffer = {s: [] for s in symbols}
        
        self.FLUSH_THRESHOLD = 1000 # 每1000条刷盘一次
        
        if not os.path.exists(self.save_path):
            os.makedirs(self.save_path)
            
        self.engine.register(EVENT_ORDERBOOK, self.on_orderbook)
        self.engine.register(EVENT_AGG_TRADE, self.on_agg_trade)
        
        logger.info(f"HDF5 Recorder Started: {self.symbols}")

    def on_orderbook(self, event: Event):
        ob: OrderBook = event.data
        if ob.symbol not in self.depth_buffer: return
        
        # 提取 Top 5
        sb = sorted(ob.bids.items(), key=lambda x: x[0], reverse=True)[:5]
        sa = sorted(ob.asks.items(), key=lambda x: x[0])[:5]
        while len(sb) < 5: sb.append((0,0))
        while len(sa) < 5: sa.append((0,0))
        
        row = {
            "datetime": ob.datetime,
            "symbol": ob.symbol
        }
        for i in range(5):
            row[f"bid{i+1}_p"] = sb[i][0]
            row[f"bid{i+1}_v"] = sb[i][1]
            row[f"ask{i+1}_p"] = sa[i][0]
            row[f"ask{i+1}_v"] = sa[i][1]
            
        self.depth_buffer[ob.symbol].append(row)
        
        if len(self.depth_buffer[ob.symbol]) >= self.FLUSH_THRESHOLD:
            self.flush(ob.symbol, "depth")

    def on_agg_trade(self, event: Event):
        t: AggTradeData = event.data
        if t.symbol not in self.trade_buffer: return
        
        row = {
            "datetime": t.datetime,
            "symbol": t.symbol,
            "price": t.price,
            "qty": t.quantity,
            "maker_is_buyer": t.maker_is_buyer
        }
        self.trade_buffer[t.symbol].append(row)
        
        if len(self.trade_buffer[t.symbol]) >= self.FLUSH_THRESHOLD:
            self.flush(t.symbol, "trade")

    def flush(self, symbol, data_type):
        """将 Buffer 写入 HDF5"""
        try:
            today = datetime.now().strftime("%Y%m%d")
            filename = f"{self.save_path}/{symbol}_{data_type}_{today}.h5"
            key = data_type # HDF5 key
            
            if data_type == "depth":
                buffer = self.depth_buffer[symbol]
                self.depth_buffer[symbol] = [] # Clear buffer
            else:
                buffer = self.trade_buffer[symbol]
                self.trade_buffer[symbol] = []
                
            if not buffer: return
            
            df = pd.DataFrame(buffer)
            
            # Append to HDF5
            # min_itemsize 预留字符串长度，format='table' 支持追加
            df.to_hdf(filename, key=key, mode='a', append=True, format='table', min_itemsize={'symbol': 10})
            
        except Exception as e:
            logger.error(f"Flush HDF5 Failed [{symbol} {data_type}]: {e}")

    def close(self):
        """程序退出时强制刷盘"""
        for symbol in self.symbols:
            self.flush(symbol, "depth")
            self.flush(symbol, "trade")
        logger.info("Recorder Closed & Flushed.")