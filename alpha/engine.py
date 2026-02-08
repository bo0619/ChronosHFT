# file: alpha/engine.py

import math
import numpy as np
from collections import defaultdict
from event.type import OrderBook, AggTradeData

class FeatureEngine:
    """
    全景特征工程引擎 (支持多币种隔离)
    
    Features:
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
        # 使用 defaultdict 自动管理多币种状态
        # Key: Symbol, Value: State Dict
        self.states = defaultdict(self._create_initial_state)

    def _create_initial_state(self):
        return {
            # --- 实时计算出的特征值 ---
            "features": {
                "ob_imbalance": 0.0,
                "depth_imbalance": 0.0,
                "microprice_spread": 0.0,
                "delta_imbalance": 0.0,
                "delta_spread": 0.0,
                "delta_mid": 0.0
                # Trade相关特征在 get_features 时动态计算
            },
            
            # --- 历史状态 (用于差分) ---
            "prev_mid": None,
            "prev_spread": None,
            "prev_imbalance": None,
            
            # --- 交易流累积器 (Interval Accumulators) ---
            "trade_count": 0,
            "buy_vol": 0.0,
            "sell_vol": 0.0,
            "total_turnover": 0.0,
            "total_vol": 0.0
        }

    def on_orderbook(self, ob: OrderBook):
        """
        基于盘口快照计算静态特征 (L1 & L5)
        """
        s = self.states[ob.symbol]
        
        # 1. 提取 L1 数据
        bid_1_p, bid_1_v = ob.get_best_bid()
        ask_1_p, ask_1_v = ob.get_best_ask()
        
        if bid_1_p == 0 or ask_1_p == 0: return

        mid = (bid_1_p + ask_1_p) / 2.0
        spread = ask_1_p - bid_1_p
        
        # --- 特征 1: Orderbook Imbalance (L1) ---
        imb = 0.0
        if bid_1_v + ask_1_v > 0:
            imb = (bid_1_v - ask_1_v) / (bid_1_v + ask_1_v)
        s["features"]["ob_imbalance"] = imb

        # --- 特征 2: Depth Slope (L5 Imbalance) ---
        # 提取 Top 5
        sorted_bids = sorted(ob.bids.items(), key=lambda x: x[0], reverse=True)[:5]
        sorted_asks = sorted(ob.asks.items(), key=lambda x: x[0])[:5]
        
        sum_bid_vol = sum(v for p, v in sorted_bids)
        sum_ask_vol = sum(v for p, v in sorted_asks)
        
        if sum_bid_vol + sum_ask_vol > 0:
            s["features"]["depth_imbalance"] = (sum_bid_vol - sum_ask_vol) / (sum_bid_vol + sum_ask_vol)
        else:
            s["features"]["depth_imbalance"] = 0.0

        # --- 特征 3: Microprice Spread ---
        if bid_1_v + ask_1_v > 0 and spread > 0:
            microprice = (bid_1_p * ask_1_v + ask_1_p * bid_1_v) / (bid_1_v + ask_1_v)
            s["features"]["microprice_spread"] = (microprice - mid) / spread
        else:
            s["features"]["microprice_spread"] = 0.0

        # --- 差分特征 (Delta) ---
        
        # 特征 7: Delta Imbalance
        if s["prev_imbalance"] is not None:
            s["features"]["delta_imbalance"] = imb - s["prev_imbalance"]
        s["prev_imbalance"] = imb
        
        # 特征 8: Delta Spread (bps)
        if s["prev_spread"] is not None and mid > 0:
            s["features"]["delta_spread"] = (spread - s["prev_spread"]) / mid * 10000
        s["prev_spread"] = spread
        
        # 特征 9: Delta Mid (Momentum bps)
        if s["prev_mid"] is not None and s["prev_mid"] > 0:
            s["features"]["delta_mid"] = (mid - s["prev_mid"]) / s["prev_mid"] * 10000
        s["prev_mid"] = mid

    def on_trade(self, trade: AggTradeData):
        """
        基于成交流累积数据
        """
        s = self.states[trade.symbol]
        s["trade_count"] += 1
        
        if trade.maker_is_buyer: 
            # Maker是买 -> Taker是卖 -> 主动卖
            s["sell_vol"] += trade.quantity
        else:
            # Maker是卖 -> Taker是买 -> 主动买
            s["buy_vol"] += trade.quantity
            
        s["total_turnover"] += trade.price * trade.quantity
        s["total_vol"] += trade.quantity

    def get_features(self, symbol: str):
        """
        [修复点] 必须接收 symbol 参数
        计算 Trade 相关特征并返回 9维特征向量
        """
        s = self.states[symbol]
        
        # --- 特征 4: Trade Sign Imbalance ---
        net_vol = s["buy_vol"] + s["sell_vol"]
        trade_imbalance = 0.0
        if net_vol > 0:
            trade_imbalance = (s["buy_vol"] - s["sell_vol"]) / net_vol
            
        # --- 特征 5: Trade Arrival Rate ---
        # Log处理，平滑数值
        trade_arrival = np.log1p(s["trade_count"])
        
        # --- 特征 6: VWAP Drift ---
        vwap_drift = 0.0
        # 需要最新的 mid price (从 on_orderbook 缓存的 prev_mid 获取)
        current_mid = s["prev_mid"]
        
        if s["total_vol"] > 0 and current_mid and current_mid > 0:
            vwap = s["total_turnover"] / s["total_vol"]
            # 归一化为 bps
            vwap_drift = (vwap - current_mid) / current_mid * 10000

        # 返回固定顺序的向量
        return [
            s["features"]["ob_imbalance"],      # 1
            s["features"]["depth_imbalance"],   # 2
            s["features"]["microprice_spread"], # 3
            trade_imbalance,                    # 4 (Dynamic)
            trade_arrival,                      # 5 (Dynamic)
            vwap_drift,                         # 6 (Dynamic)
            s["features"]["delta_imbalance"],   # 7
            s["features"]["delta_spread"],      # 8
            s["features"]["delta_mid"]          # 9
        ]
    
    def reset_interval(self, symbol: str):
        """
        [修复点] 必须接收 symbol 参数
        周期结束，重置累积量
        """
        if symbol in self.states:
            s = self.states[symbol]
            s["trade_count"] = 0
            s["buy_vol"] = 0.0
            s["sell_vol"] = 0.0
            s["total_turnover"] = 0.0
            s["total_vol"] = 0.0