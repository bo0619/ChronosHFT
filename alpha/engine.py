# file: alpha/engine.py

import math
import numpy as np
from collections import deque
from event.type import OrderBook, TradeData, AggTradeData

class FeatureEngine:
    """
    全景特征工程引擎 (9大核心微观特征)
    
    1. Orderbook Imbalance (L1)
    2. Depth Imbalance (L5 Slope)
    3. Microprice Spread
    4. Trade Sign Imbalance
    5. Trade Arrival Rate
    6. VWAP Drift
    7. d(Imbalance)/dt
    8. d(Spread)/dt
    9. d(MidPrice)/dt (Momentum)
    """
    def __init__(self):
        # --- 实时特征值 ---
        self.features = {
            "ob_imbalance": 0.0,
            "depth_imbalance": 0.0,
            "microprice_spread": 0.0,
            "trade_imbalance": 0.0,
            "trade_arrival_rate": 0.0,
            "vwap_drift": 0.0,
            "delta_imbalance": 0.0,
            "delta_spread": 0.0,
            "delta_mid": 0.0
        }

        # --- 历史状态 (用于计算差分) ---
        self.prev_mid = None
        self.prev_spread = None
        self.prev_imbalance = None
        
        # --- 交易流累积器 (Interval Accumulators) ---
        self.trade_count = 0
        self.buy_vol = 0.0
        self.sell_vol = 0.0
        self.total_turnover = 0.0 # Price * Vol
        self.total_vol = 0.0

    def on_orderbook(self, ob: OrderBook):
        """
        基于盘口快照计算静态特征 (L1 & L5)
        """
        # 1. 提取 L1 数据
        bid_1_p, bid_1_v = ob.get_best_bid()
        ask_1_p, ask_1_v = ob.get_best_ask()
        
        if bid_1_p == 0 or ask_1_p == 0: return

        mid = (bid_1_p + ask_1_p) / 2.0
        spread = ask_1_p - bid_1_p
        
        # --- 特征 1: Orderbook Imbalance (L1) ---
        # 范围 [-1, 1]
        imb = (bid_1_v - ask_1_v) / (bid_1_v + ask_1_v)
        self.features["ob_imbalance"] = imb

        # --- 特征 2: Depth Slope (L5 Imbalance) ---
        # 计算前5档的累积量，反映更深层的供需
        # 注意：ob.bids 是 dict，需要排序
        sorted_bids = sorted(ob.bids.items(), key=lambda x: x[0], reverse=True)[:5]
        sorted_asks = sorted(ob.asks.items(), key=lambda x: x[0])[:5]
        
        sum_bid_vol = sum(v for p, v in sorted_bids)
        sum_ask_vol = sum(v for p, v in sorted_asks)
        
        if sum_bid_vol + sum_ask_vol > 0:
            self.features["depth_imbalance"] = (sum_bid_vol - sum_ask_vol) / (sum_bid_vol + sum_ask_vol)
        else:
            self.features["depth_imbalance"] = 0.0

        # --- 特征 3: Microprice Spread (Microprice - Mid) ---
        # Microprice = (BidP * AskV + AskP * BidV) / (BidV + AskV)
        # 加权中间价，反映了 L1 量的重心
        microprice = (bid_1_p * ask_1_v + ask_1_p * bid_1_v) / (bid_1_v + ask_1_v)
        # 归一化为相对于 Spread 的偏移 [-0.5, 0.5] 左右
        # 如果 > 0 说明 Microprice 在 Mid 上方，看涨
        if spread > 0:
            self.features["microprice_spread"] = (microprice - mid) / spread
        else:
            self.features["microprice_spread"] = 0.0

        # --- 差分特征 (Delta Features) ---
        
        # 特征 7: Delta Imbalance
        if self.prev_imbalance is not None:
            self.features["delta_imbalance"] = imb - self.prev_imbalance
        self.prev_imbalance = imb
        
        # 特征 8: Delta Spread (归一化: Spread变化 / Mid)
        # Spread 变大通常意味着流动性变差或波动变大
        if self.prev_spread is not None:
            self.features["delta_spread"] = (spread - self.prev_spread) / mid * 10000 # bps
        self.prev_spread = spread
        
        # 特征 9: Delta Mid (Momentum)
        # 价格动量
        if self.prev_mid is not None:
            self.features["delta_mid"] = (mid - self.prev_mid) / self.prev_mid * 10000 # bps
        self.prev_mid = mid

    def on_trade(self, trade: AggTradeData):
        """
        基于成交流计算动态特征
        """
        # 累积基础数据
        self.trade_count += 1
        
        if trade.maker_is_buyer: 
            # Maker是买 -> Taker是卖 -> 主动卖
            self.sell_vol += trade.quantity
        else:
            # Maker是卖 -> Taker是买 -> 主动买
            self.buy_vol += trade.quantity
            
        self.total_turnover += trade.price * trade.quantity
        self.total_vol += trade.quantity

    def get_features(self):
        """
        计算 Trade 相关特征并返回特征向量
        在 Strategy 调用此方法时计算，因为 Trade 特征是基于 Interval 的
        """
        # --- 特征 4: Trade Sign Imbalance ---
        # 过去一段时间的主动买卖力量对比
        net_vol = self.buy_vol + self.sell_vol
        if net_vol > 0:
            self.features["trade_imbalance"] = (self.buy_vol - self.sell_vol) / net_vol
        else:
            self.features["trade_imbalance"] = 0.0
            
        # --- 特征 5: Trade Arrival Rate ---
        # 简单的计数，反映市场热度
        # 可以做 log 处理压缩数值范围
        self.features["trade_arrival_rate"] = np.log1p(self.trade_count)
        
        # --- 特征 6: VWAP Drift ---
        # 成交均价相对于当前 Mid 的偏移
        # 需要用到当前的 prev_mid (即最新的 mid)
        if self.total_vol > 0 and self.prev_mid and self.prev_mid > 0:
            vwap = self.total_turnover / self.total_vol
            # 归一化为 bps
            self.features["vwap_drift"] = (vwap - self.prev_mid) / self.prev_mid * 10000
        else:
            self.features["vwap_drift"] = 0.0

        # 返回固定顺序的向量 (供 ML 模型使用)
        return [
            self.features["ob_imbalance"],      # 1
            self.features["depth_imbalance"],   # 2
            self.features["microprice_spread"], # 3
            self.features["trade_imbalance"],   # 4
            self.features["trade_arrival_rate"],# 5
            self.features["vwap_drift"],        # 6
            self.features["delta_imbalance"],   # 7
            self.features["delta_spread"],      # 8
            self.features["delta_mid"]          # 9
        ]
    
    def reset_interval(self):
        """
        每个决策周期结束时调用，重置累积量
        """
        self.trade_count = 0
        self.buy_vol = 0.0
        self.sell_vol = 0.0
        self.total_turnover = 0.0
        self.total_vol = 0.0