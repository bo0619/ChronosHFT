# file: data/recorder.py

import os
import csv
from datetime import datetime
from event.type import OrderBook, AggTradeData, Event, EVENT_ORDERBOOK, EVENT_AGG_TRADE, EVENT_LOG

class DataRecorder:
    """
    高保真行情录制器 (Step 5 V2)
    1. Depth Recorder: 录制 Top 5 盘口
    2. Trade Recorder: 录制逐笔成交
    """
    def __init__(self, engine, symbols: list):
        self.engine = engine
        self.symbols = symbols
        self.save_path = "storage"
        
        # 文件句柄缓存
        self.depth_files = {}
        self.depth_writers = {}
        self.trade_files = {}
        self.trade_writers = {}
        
        if not os.path.exists(self.save_path):
            os.makedirs(self.save_path)
            
        self._init_files()
        
        # 注册监听
        self.engine.register(EVENT_ORDERBOOK, self.on_orderbook)
        self.engine.register(EVENT_AGG_TRADE, self.on_agg_trade)
        
        self.log(f"双流录制已启动 (Top5 Depth + Trades): {self.symbols}")

    def _init_files(self):
        today = datetime.now().strftime("%Y%m%d")
        for symbol in self.symbols:
            # --- 1. 初始化深度文件 ---
            f_depth_name = f"{self.save_path}/{symbol}_depth_{today}.csv"
            exists_depth = os.path.exists(f_depth_name)
            f_depth = open(f_depth_name, "a", newline="", encoding="utf-8")
            w_depth = csv.writer(f_depth)
            
            if not exists_depth:
                # 生成 Top 5 表头: bid1_p, bid1_v, ..., bid5_p, bid5_v, ask1...
                header = ["datetime", "symbol"]
                for i in range(1, 6): header.extend([f"bid{i}_p", f"bid{i}_v"])
                for i in range(1, 6): header.extend([f"ask{i}_p", f"ask{i}_v"])
                w_depth.writerow(header)
                
            self.depth_files[symbol] = f_depth
            self.depth_writers[symbol] = w_depth
            
            # --- 2. 初始化成交文件 ---
            f_trade_name = f"{self.save_path}/{symbol}_trades_{today}.csv"
            exists_trade = os.path.exists(f_trade_name)
            f_trade = open(f_trade_name, "a", newline="", encoding="utf-8")
            w_trade = csv.writer(f_trade)
            
            if not exists_trade:
                w_trade.writerow(["datetime", "symbol", "price", "qty", "maker_is_buyer"])
                
            self.trade_files[symbol] = f_trade
            self.trade_writers[symbol] = w_trade

    def on_orderbook(self, event: Event):
        ob: OrderBook = event.data
        if ob.symbol not in self.depth_writers: return
        
        # 提取 Top 5 Bids (价格从高到低)
        sorted_bids = sorted(ob.bids.items(), key=lambda x: x[0], reverse=True)[:5]
        # 提取 Top 5 Asks (价格从低到高)
        sorted_asks = sorted(ob.asks.items(), key=lambda x: x[0])[:5]
        
        # 补全不足 5 档的情况 (极少见，但为了防 Crash)
        while len(sorted_bids) < 5: sorted_bids.append((0, 0))
        while len(sorted_asks) < 5: sorted_asks.append((0, 0))
        
        row = [ob.datetime.strftime("%Y-%m-%d %H:%M:%S.%f"), ob.symbol]
        
        # 写入 Bids
        for price, vol in sorted_bids:
            row.extend([price, vol])
            
        # 写入 Asks
        for price, vol in sorted_asks:
            row.extend([price, vol])
            
        self.depth_writers[ob.symbol].writerow(row)

    def on_agg_trade(self, event: Event):
        trade: AggTradeData = event.data
        if trade.symbol not in self.trade_writers: return
        
        row = [
            trade.datetime.strftime("%Y-%m-%d %H:%M:%S.%f"),
            trade.symbol,
            trade.price,
            trade.quantity,
            1 if trade.maker_is_buyer else 0 # 1代表卖方主动(价格跌), 0代表买方主动(价格涨)
        ]
        self.trade_writers[trade.symbol].writerow(row)

    def close(self):
        for f in self.depth_files.values(): f.close()
        for f in self.trade_files.values(): f.close()

    def log(self, msg):
        self.engine.put(Event(EVENT_LOG, f"[DataRecorder] {msg}"))