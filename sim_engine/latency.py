# file: sim_engine/latency.py

import random
import math
from collections import deque

class AdvancedLatencyModel:
    def __init__(self, config):
        bt_conf = config.get("backtest", {})
        self.mu = math.log(bt_conf.get("latency_ms_mean", 20) / 1000.0)
        self.sigma = bt_conf.get("latency_sigma", 0.5)
        
        self.message_timestamps = deque()
        self.window_size = 1.0
        
    def record_message(self, current_time):
        self.message_timestamps.append(current_time.timestamp())
        now_ts = current_time.timestamp()
        while self.message_timestamps and (now_ts - self.message_timestamps[0] > self.window_size):
            self.message_timestamps.popleft()
            
    def get_latency(self) -> float:
        base_latency = random.lognormvariate(self.mu, self.sigma)
        
        # 负载因子
        msg_rate = len(self.message_timestamps)
        load_penalty = 0.0
        if msg_rate > 100:
            load_penalty = (msg_rate - 100) / 1000.0
            
        final_latency = base_latency * (1 + load_penalty)
        return min(final_latency, 1.0)