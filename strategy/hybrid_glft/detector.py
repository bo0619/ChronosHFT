# file: strategy/hybrid_glft/detector.py
import time
import numpy as np
from collections import deque
from dataclasses import dataclass
from typing import List
from event.type import OrderBook

@dataclass
class TrendSignal:
    strength: float
    confidence: float
    direction: str # "UP", "DOWN", "NEUTRAL"
    source: str
    dominant_factor: str
    timestamp: float

class TrendDetector:
    def __init__(self, window_sec: float = 10.0):
        self.window_sec = window_sec
        self.price_history = deque(maxlen=200)
        self.current_imbalance = 0.0
        self.current_depth_ratio = 1.0
    
    def on_orderbook(self, ob: OrderBook) -> None:
        bid_1, bid_1_v = ob.get_best_bid()
        ask_1, ask_1_v = ob.get_best_ask()
        if bid_1 == 0 or ask_1 == 0: return
        
        mid = (bid_1 + ask_1) / 2.0
        total_l1 = bid_1_v + ask_1_v
        self.current_imbalance = (bid_1_v - ask_1_v) / total_l1 if total_l1 > 0 else 0.0
        
        # 计算深度比 (L5)
        sorted_bids = sorted(ob.bids.items(), key=lambda x: x[0], reverse=True)[:5]
        sorted_asks = sorted(ob.asks.items(), key=lambda x: x[0])[:5]
        bid_vol_5 = sum(v for _, v in sorted_bids)
        ask_vol_5 = sum(v for _, v in sorted_asks)
        self.current_depth_ratio = bid_vol_5 / (ask_vol_5 + 1e-9)
        
        self.price_history.append((time.time(), mid))
    
    def compute_momentum(self, lookback: float = 5.0) -> float:
        now = time.time()
        valid = [p for t, p in self.price_history if now - t <= lookback]
        if len(valid) < 2: return 0.0
        return (valid[-1] / valid[0] - 1.0) * 10000

    def compute_trend_signal(self) -> TrendSignal:
        mom = self.compute_momentum()
        combined = 0.5 * self.current_imbalance + 0.5 * np.tanh(mom / 10.0)
        strength = np.tanh(combined)
        
        direction = "NEUTRAL"
        if strength > 0.2: direction = "UP"
        elif strength < -0.2: direction = "DOWN"
        
        return TrendSignal(strength, 0.8, direction, "rule", "mixed", time.time())

    def get_features(self) -> List[float]:
        mom = self.compute_momentum()
        return [self.current_imbalance, np.tanh(mom / 10.0), self.current_depth_ratio - 1.0]