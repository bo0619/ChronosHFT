# file: alpha/factors.py

import math
from collections import deque
import numpy as np
from event.type import OrderBook, TradeData

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