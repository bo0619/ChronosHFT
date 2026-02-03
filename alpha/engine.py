# file: alpha/engine.py

import numpy as np
from event.type import OrderBook, TradeData, AggTradeData

class FeatureEngine:
    def __init__(self):
        self.imbalance = 0.0
        self.ofi = 0.0
        self.trade_imbalance = 0.0
        
        # 内部状态
        self.prev_bid_p = 0
        self.prev_ask_p = 0
        self.prev_bid_v = 0
        self.prev_ask_v = 0
        self.buy_vol = 0
        self.sell_vol = 0

    def on_orderbook(self, ob: OrderBook):
        bid_p, bid_v = ob.get_best_bid()
        ask_p, ask_v = ob.get_best_ask()
        if bid_p == 0: return

        # 特征 1: Imbalance
        self.imbalance = (bid_v - ask_v) / (bid_v + ask_v)

        # 特征 2: OFI
        if self.prev_bid_p > 0:
            e_bid = bid_v if bid_p > self.prev_bid_p else (-self.prev_bid_v if bid_p < self.prev_bid_p else bid_v - self.prev_bid_v)
            e_ask = ask_v if ask_p < self.prev_ask_p else (-self.prev_ask_v if ask_p > self.prev_ask_p else ask_v - self.prev_ask_v)
            self.ofi = e_bid - e_ask

        self.prev_bid_p, self.prev_ask_p = bid_p, ask_p
        self.prev_bid_v, self.prev_ask_v = bid_v, ask_v

    def on_trade(self, trade: AggTradeData):
        if trade.maker_is_buyer: # Taker sold
            self.sell_vol += trade.quantity
        else: # Taker bought
            self.buy_vol += trade.quantity
            
        # 特征 3: Trade Flow Imbalance
        tot = self.buy_vol + self.sell_vol
        if tot > 0:
            self.trade_imbalance = (self.buy_vol - self.sell_vol) / tot

    def get_features(self):
        # 归一化后返回
        return [self.imbalance, np.sign(self.ofi) * np.log1p(abs(self.ofi)), self.trade_imbalance]
    
    def reset_interval(self):
        self.buy_vol = 0
        self.sell_vol = 0