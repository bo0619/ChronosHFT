# file: alpha/factors.py

import math
import numpy as np
from collections import deque
from event.type import OrderBook, TradeData, AggTradeData

class FactorBase:
    def __init__(self, name):
        self.name = name
        self.value = 0.0
    def on_orderbook(self, ob: OrderBook): pass
    def on_trade(self, trade: TradeData): pass

class GLFTCalibrator:
    """
    GLFT 在线参数校准器 (修复版)
    """
    def __init__(self, window=100):
        self.window = window
        self.mid_prices = deque(maxlen=window)
        
        # 初始参数 (以 bps 为单位)
        self.sigma_bps = 10.0 # [修复] 给一个合理的默认值 (例如 10 bps)
        self.A = 10.0      
        self.k = 0.5       
        
        self.learning_rate = 0.005 # [优化] 降低学习率，防止参数跳变
        self.last_mid = 0.0
        self.is_warmed_up = False # [NEW] 预热标志

    def on_orderbook(self, ob: OrderBook):
        bid, _ = ob.get_best_bid()
        ask, _ = ob.get_best_ask()
        if bid == 0: return
        
        mid = (bid + ask) / 2
        
        # [修复] 过滤掉初始的 0 或异常跳变
        if self.last_mid > 0:
            # 过滤掉超过 10% 的异常 tick (数据清洗)
            if abs(mid - self.last_mid) / self.last_mid < 0.1:
                ret_bps = (mid / self.last_mid - 1) * 10000
                self.mid_prices.append(ret_bps)
                
                # 只有收集足够数据才开始计算 Sigma
                if len(self.mid_prices) >= 10:
                    std_dev = np.std(self.mid_prices)
                    # 平滑更新，防止突变
                    self.sigma_bps = 0.9 * self.sigma_bps + 0.1 * (std_dev * math.sqrt(10)) # 年化/秒化调整视需求而定，这里保持简单量级
                    
                    # [修复] 再次钳制 Sigma，防止计算出天文数字
                    # 正常 crypto 波动率 1秒内通常 < 50bps
                    self.sigma_bps = min(self.sigma_bps, 100.0) 
                    
                    self.is_warmed_up = True
        
        self.last_mid = mid

    def on_market_trade(self, trade: AggTradeData, current_mid: float):
        # 未预热前不更新 A, k，防止脏数据破坏模型
        if not self.is_warmed_up or current_mid <= 0: return
        
        delta_mkt = abs(trade.price / current_mid - 1) * 10000
        
        # 异常值过滤：如果成交偏离中间价超过 100bps (1%)，视为异常数据不学习
        if delta_mkt > 100: return

        prediction = self.A * math.exp(-self.k * delta_mkt)
        error = 1.0 - prediction
        
        self.A += self.learning_rate * error * math.exp(-self.k * delta_mkt)
        # 增加 k 的学习稳定性，防止 k 掉到 0
        grad_k = error * (-delta_mkt) * prediction
        self.k -= self.learning_rate * grad_k
        
        # 参数约束
        self.A = max(0.1, min(200.0, self.A))
        self.k = max(0.1, min(10.0, self.k)) # [修复] 提高 k 的下限到 0.1