# file: execution/twap.py

import time
from .algo_base import AlgoTemplate

class TWAPAlgo(AlgoTemplate):
    def __init__(self, algo_id, symbol, direction, total_vol, engine, strategy, duration, interval=60):
        super().__init__(algo_id, symbol, direction, total_vol, engine, strategy)
        self.duration = duration   # 总时长 (秒)
        self.interval = interval   # 切片间隔 (秒)
        
        self.slice_vol = total_vol / (duration / interval)
        self.next_run_time = time.time()
        self.end_time = time.time() + duration

    def on_tick(self, ob):
        if self.finished: return
        
        now = time.time()
        if now > self.end_time:
            self.stop()
            return

        if now >= self.next_run_time:
            self.place_slice(ob)
            self.next_run_time = now + self.interval

    def place_slice(self, ob):
        # 简单 TWAP：市价吃单 (Taker) 或 对手价限价 (Aggressive Limit)
        # 这里用对手价
        price = ob.get_best_ask()[0] if self.direction == "BUY" else ob.get_best_bid()[0]
        
        if self.direction == "BUY":
            oid = self.strategy.buy(self.symbol, price, self.slice_vol)
        else:
            oid = self.strategy.sell(self.symbol, price, self.slice_vol)
            
        if oid:
            self.active_orders[oid] = self.slice_vol