# file: strategy/hybrid_glft/selector.py
import time
from dataclasses import dataclass
from typing import Optional
from .detector import TrendSignal
from .predictor import MLPrediction

@dataclass
class StrategyMode:
    mode: str # "MARKET_MAKING", "MOMENTUM_BUY", "MOMENTUM_SELL"
    momentum_strength: float
    transition_time: float

class HybridModeSelector:
    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
    
    def select_mode(self, rule_sig: TrendSignal, ml_pred: Optional[MLPrediction]) -> StrategyMode:
        if ml_pred and ml_pred.p_trend > self.threshold:
            mode = "MOMENTUM_BUY" if ml_pred.direction == "UP" else "MOMENTUM_SELL"
            strength = ml_pred.momentum_strength
        elif abs(rule_sig.strength) > self.threshold:
            mode = "MOMENTUM_BUY" if rule_sig.direction == "UP" else "MOMENTUM_SELL"
            strength = abs(rule_sig.strength)
        else:
            mode = "MARKET_MAKING"
            strength = 0.0
            
        return StrategyMode(mode, strength, time.time())