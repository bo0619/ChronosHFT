# file: alpha/factors.py

import math
from collections import deque
import numpy as np
from event.type import OrderBook, TradeData, AggTradeData

class FactorBase:
    def __init__(self, name):
        self.name = name
        self.value = 0.0

    def on_orderbook(self, ob: OrderBook): pass
    def on_trade(self, trade: TradeData): pass

class BookImbalance(FactorBase):
    """
    静态盘口不平衡
    (BidQty - AskQty) / (BidQty + AskQty)
    范围: [-1, 1], >0 代表买压强
    """
    def __init__(self):
        super().__init__("BookImbalance")

    def on_orderbook(self, ob: OrderBook):
        bid_1_p, bid_1_v = ob.get_best_bid()
        ask_1_p, ask_1_v = ob.get_best_ask()
        
        if bid_1_v + ask_1_v > 0:
            self.value = (bid_1_v - ask_1_v) / (bid_1_v + ask_1_v)
        else:
            self.value = 0.0

class OrderFlowImbalance(FactorBase):
    """
    OFI (Order Flow Imbalance) - 动态订单流
    基于最佳买卖价的变化推断买卖压力
    Reference: Cont et al. (2014)
    """
    def __init__(self):
        super().__init__("OFI")
        self.prev_bid_p = None
        self.prev_bid_v = 0
        self.prev_ask_p = None
        self.prev_ask_v = 0
        self.ofi_accum = 0.0 # 累积值 (或者是滑动窗口平滑值)
        self.decay = 0.9     # 衰减因子 (EMA)

    def on_orderbook(self, ob: OrderBook):
        bid_p, bid_v = ob.get_best_bid()
        ask_p, ask_v = ob.get_best_ask()
        
        if self.prev_bid_p is None:
            self.prev_bid_p = bid_p
            self.prev_bid_v = bid_v
            self.prev_ask_p = ask_p
            self.prev_ask_v = ask_v
            return

        # 1. 计算 Bid 侧的 OFI
        # 价格上涨(买单激进) -> 正贡献
        # 价格下跌(买单撤退) -> 负贡献
        # 价格不变(量增加) -> 正贡献
        e_bid = 0.0
        if bid_p > self.prev_bid_p:
            e_bid = bid_v
        elif bid_p < self.prev_bid_p:
            e_bid = -self.prev_bid_v
        else:
            e_bid = bid_v - self.prev_bid_v

        # 2. 计算 Ask 侧的 OFI (卖单与买单相反)
        # 价格下跌(卖单激进) -> 负贡献 (对净流而言是卖压，数值为正，之后相减)
        # 这里我们要计算 Net OFI = Bid_Flow - Ask_Flow
        e_ask = 0.0
        if ask_p < self.prev_ask_p:
            e_ask = ask_v
        elif ask_p > self.prev_ask_p:
            e_ask = -self.prev_ask_v
        else:
            e_ask = ask_v - self.prev_ask_v

        # Net OFI
        ofi = e_bid - e_ask
        
        # 使用 EMA 平滑
        self.ofi_accum = self.ofi_accum * self.decay + ofi * (1 - self.decay)
        self.value = self.ofi_accum

        # 更新状态
        self.prev_bid_p = bid_p
        self.prev_bid_v = bid_v
        self.prev_ask_p = ask_p
        self.prev_ask_v = ask_v

class RealizedVolatility(FactorBase):
    """
    短期已实现波动率 (基于中间价回报)
    """
    def __init__(self, window=20):
        super().__init__("Volatility")
        self.window = window
        self.mid_prices = deque(maxlen=window+1)

    def on_orderbook(self, ob: OrderBook):
        bid, _ = ob.get_best_bid()
        ask, _ = ob.get_best_ask()
        if bid == 0: return
        
        mid = (bid + ask) / 2
        self.mid_prices.append(mid)
        
        if len(self.mid_prices) >= 2:
            # 计算 Log Return 序列
            prices = np.array(self.mid_prices)
            returns = np.log(prices[1:] / prices[:-1])
            # 标准差 * 放大系数 (为了数值好看)
            self.value = np.std(returns) * 10000

class GLFTCalibrator:
    """
    GLFT 在线参数校准器
    1. 计算归一化波动率 (Volatility in bps)
    2. 递归估计订单流参数 A 和 k (Intensity = A * exp(-k * delta))
    """
    def __init__(self, window=100):
        self.window = window
        self.mid_prices = deque(maxlen=window)
        
        # 初始参数 (以 bps 为单位)
        self.sigma_bps = 0.0
        self.A = 10.0      # 基础频率
        self.k = 0.5       # 衰减速率
        
        self.learning_rate = 0.01
        self.last_mid = 0.0

    def on_orderbook(self, ob: OrderBook):
        bid, _ = ob.get_best_bid()
        ask, _ = ob.get_best_ask()
        if bid == 0: return
        
        mid = (bid + ask) / 2
        if self.last_mid > 0:
            # 计算归一化收益率 (bps)
            ret_bps = (mid / self.last_mid - 1) * 10000
            self.mid_prices.append(ret_bps)
            
            if len(self.mid_prices) > 10:
                # 更新波动率 (sigma 是 bps/second)
                self.sigma_bps = np.std(self.mid_prices)
        
        self.last_mid = mid

    def on_market_trade(self, trade: AggTradeData, current_mid: float):
        """
        核心：通过真实成交数据点在线修正 A 和 k
        """
        if current_mid <= 0: return
        
        # 1. 计算该笔成交距离当时中间价的归一化距离 (bps)
        delta_mkt = abs(trade.price / current_mid - 1) * 10000
        
        # 2. 简单的递归最大似然估计 (Recursive MLE) 思想
        # 如果成交频繁发生在远处，k 会变小（深度好）；如果只发生在近处，k 会变大（深度差）
        # 这里使用随机梯度下降 (SGD) 思想更新参数
        prediction = self.A * math.exp(-self.k * delta_mkt)
        
        # 我们观察到了一次真实的成交事件（Target = 1）
        error = 1.0 - prediction
        
        # 更新 A (截距)
        self.A += self.learning_rate * error * math.exp(-self.k * delta_mkt)
        # 更新 k (斜率)
        self.k -= self.learning_rate * error * (-delta_mkt) * prediction
        
        # 参数约束
        self.A = max(0.1, min(100.0, self.A))
        self.k = max(0.01, min(5.0, self.k))