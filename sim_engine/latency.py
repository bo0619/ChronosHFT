# file: sim_engine/latency.py

import random
import math
from collections import deque

class AdvancedLatencyModel:
    def __init__(self, config):
        bt_conf = config["backtest"]
        # 对数正态分布参数
        # 真实网络延迟通常服从 Log-Normal，有长尾效应
        self.mu = math.log(bt_conf.get("latency_base_ms", 10) / 1000.0)
        self.sigma = bt_conf.get("latency_sigma", 0.5)
        
        # 负载监控
        self.message_timestamps = deque()
        self.window_size = 1.0 # 1秒窗口
        
    def record_message(self, current_time):
        """记录一条市场消息到达，用于计算负载"""
        self.message_timestamps.append(current_time.timestamp())
        # 清理过期
        now_ts = current_time.timestamp()
        while self.message_timestamps and (now_ts - self.message_timestamps[0] > self.window_size):
            self.message_timestamps.popleft()
            
    def get_latency(self) -> float:
        """
        获取延迟 (秒)
        公式: BaseLatency * (1 + LoadFactor) * RandomNoise
        """
        # 1. 基础随机延迟 (Log-Normal)
        base_latency = random.lognormvariate(self.mu, self.sigma)
        
        # 2. 负载因子 (Load Factor)
        # 假设每秒超过 100 条消息开始拥堵，每增加 100 条延迟增加 10%
        msg_rate = len(self.message_timestamps)
        load_penalty = 0.0
        if msg_rate > 100:
            load_penalty = (msg_rate - 100) / 1000.0 # 简单的线性惩罚
            
        final_latency = base_latency * (1 + load_penalty)
        
        # 限制极值 (防止模拟出 10秒 这种不合理的延迟，除非断网)
        return min(final_latency, 1.0)