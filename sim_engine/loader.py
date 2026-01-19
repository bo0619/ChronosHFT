# file: sim_engine/loader.py

import os
import csv
from datetime import datetime
from event.type import OrderBook, AggTradeData

class DataLoader:
    def __init__(self, sim_engine, clock, data_path, symbol):
        self.sim_engine = sim_engine
        self.clock = clock
        self.data_path = data_path
        self.symbol = symbol

    def load_and_schedule(self, exchange):
        """加载数据并注册到仿真引擎"""
        # 寻找文件
        files_d = [f for f in os.listdir(self.data_path) if f.startswith(f"{self.symbol}_depth")]
        files_t = [f for f in os.listdir(self.data_path) if f.startswith(f"{self.symbol}_trades")]
        
        if not files_d or not files_t:
            raise FileNotFoundError(f"Data missing for {self.symbol} in {self.data_path}")

        events = []
        
        # 1. Load Depth
        print(f">>> Loading Depth: {files_d[0]}...")
        with open(os.path.join(self.data_path, files_d[0]), 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                dt = datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S.%f")
                ob = self._parse_ob(row, dt)
                # 注册 Market Event: 优先级 0 (最高)
                events.append((dt, 0, exchange.on_market_depth, (ob,)))

        # 2. Load Trades
        print(f">>> Loading Trades: {files_t[0]}...")
        with open(os.path.join(self.data_path, files_t[0]), 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                dt = datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S.%f")
                tr = self._parse_trade(row, dt)
                # 注册 Trade Event: 优先级 0
                events.append((dt, 0, exchange.on_market_trade, (tr,)))
        
        # 3. Sort & Schedule
        print(">>> Sorting Events (this may take a while)...")
        events.sort(key=lambda x: x[0])
        
        print(f">>> Scheduling {len(events)} Events...")
        for dt, prio, cb, args in events:
            # [修复核心] 使用默认参数 _dt=dt, _cb=cb 来强制立即绑定变量值
            # 这样每个闭包都会持有属于自己的 dt 和 cb，而不是共享循环结束后的最后一个值
            def wrapped_cb(*a, _dt=dt, _cb=cb):
                self.clock.update(_dt) # 更新时钟
                _cb(*a)
            
            self.sim_engine.schedule(dt, wrapped_cb, args, prio)
            
        print(">>> Data Loaded Successfully.")

    def _parse_ob(self, row, dt):
        ob = OrderBook(self.symbol, "BINANCE", dt)
        for i in range(1, 6):
            if row[f"bid{i}_p"]: ob.bids[float(row[f"bid{i}_p"])] = float(row[f"bid{i}_v"])
            if row[f"ask{i}_p"]: ob.asks[float(row[f"ask{i}_p"])] = float(row[f"ask{i}_v"])
        return ob

    def _parse_trade(self, row, dt):
        return AggTradeData(
            self.symbol, 
            0, 
            float(row["price"]), 
            float(row["qty"]), 
            bool(int(row["maker_is_buyer"])), 
            dt
        )