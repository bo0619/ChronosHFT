# file: sim_engine/loader.py

import os
import pandas as pd
from datetime import datetime
from event.type import OrderBook, AggTradeData

class DataLoader:
    def __init__(self, sim_engine, clock, data_path, symbol):
        self.sim_engine = sim_engine
        self.clock = clock
        self.data_path = data_path
        self.symbol = symbol

    def load_and_schedule(self, exchange):
        # 1. 搜索文件 (匹配 .h5)
        # Recorder 生成格式: {symbol}_depth_{date}.h5 / {symbol}_trade_{date}.h5
        files_d = [f for f in os.listdir(self.data_path) if f.startswith(f"{self.symbol}_depth") and f.endswith(".h5")]
        files_t = [f for f in os.listdir(self.data_path) if f.startswith(f"{self.symbol}_trade") and f.endswith(".h5")]
        
        if not files_d or not files_t:
            raise FileNotFoundError(f"No .h5 data found for {self.symbol} in {self.data_path}")

        events = []
        
        # 2. 加载 Depth (HDF5)
        path_d = os.path.join(self.data_path, files_d[0])
        print(f">>> Loading Depth H5: {files_d[0]}...")
        
        # key通常是 'depth' (取决于 recorder.py 中的 key 参数)
        df_d = pd.read_hdf(path_d, key="depth")
        
        # 遍历 DataFrame (itertuples 比 iterrows 快得多)
        for row in df_d.itertuples():
            # row.datetime 已经是 Timestamp 对象，直接转换
            dt = row.datetime.to_pydatetime()
            ob = self._parse_ob(row, dt)
            events.append((dt, 0, exchange.on_market_depth, (ob,)))

        # 3. 加载 Trades (HDF5)
        path_t = os.path.join(self.data_path, files_t[0])
        print(f">>> Loading Trade H5: {files_t[0]}...")
        
        df_t = pd.read_hdf(path_t, key="trade")
        
        for row in df_t.itertuples():
            dt = row.datetime.to_pydatetime()
            tr = self._parse_trade(row, dt)
            events.append((dt, 0, exchange.on_market_trade, (tr,)))
        
        # 4. 排序与调度
        print(f">>> Sorting {len(events)} Events...")
        events.sort(key=lambda x: x[0])
        
        print(f">>> Scheduling Events...")
        for dt, prio, cb, args in events:
            def wrapped_cb(*a, _dt=dt, _cb=cb):
                self.clock.update(_dt)
                _cb(*a)
            self.sim_engine.schedule(dt, wrapped_cb, args, prio)
            
        print(">>> Data Loaded & Scheduled.")

    def _parse_ob(self, row, dt):
        # row 是 pandas namedtuple
        ob = OrderBook(self.symbol, "BINANCE", dt)
        
        # HDF5 录制的是 bid1_p, bid1_v ...
        # 我们硬编码读取前5档
        for i in range(1, 6):
            bp = getattr(row, f"bid{i}_p", 0)
            bv = getattr(row, f"bid{i}_v", 0)
            ap = getattr(row, f"ask{i}_p", 0)
            av = getattr(row, f"ask{i}_v", 0)
            
            if bp > 0: ob.bids[float(bp)] = float(bv)
            if ap > 0: ob.asks[float(ap)] = float(av)
            
        return ob

    def _parse_trade(self, row, dt):
        return AggTradeData(
            self.symbol, 
            0, # trade_id not saved in H5 optimized schema usually, or ignore
            float(row.price), 
            float(row.qty), 
            bool(row.maker_is_buyer), 
            dt
        )