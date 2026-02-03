# file: strategy/avellaneda_stoikov.py

import time
import math
import numpy as np
from collections import deque
from .base import StrategyTemplate
from event.type import OrderBook, TradeData, OrderStateSnapshot, Side, OrderIntent
from data.ref_data import ref_data_manager

class AvellanedaStoikovStrategy(StrategyTemplate):
    """
    经典的 Avellaneda-Stoikov 做市策略
    适配 OMS 核心架构 (Step 11)
    """
    def __init__(self, engine, oms):
        # [修改] 适配新的基类构造函数
        super().__init__(engine, oms, "AvellanedaStoikov")
        
        self.config = self._load_strategy_config()
        self.as_conf = self.config.get("as_parameters", {})
        
        # --- A-S 模型参数 ---
        self.gamma = self.as_conf.get("gamma", 0.05)
        self.k = self.as_conf.get("k", 1.5)
        self.vol_window = self.as_conf.get("vol_window", 60)
        self.interval = self.config.get("cycle_interval", 1.0)
        self.min_spread_ratio = self.as_conf.get("min_spread_ratio", 0.0002)
        
        self.lot_multiplier = self.config.get("lot_multiplier", 1.0)
        
        # --- 运行时状态 ---
        self.mid_prices = deque(maxlen=self.vol_window)
        self.last_recalc_time = 0.0
        self.current_sigma_sq = 0.0 # 当前方差
        
        print(f"[{self.name}] A-S 模型已启动 (OMS驱动): Gamma={self.gamma}, K={self.k}, Cycle={self.interval}s")

    def _load_strategy_config(self):
        try:
            import json
            with open("config.json", "r") as f: return json.load(f).get("strategy", {})
        except: return {}

    def _calculate_volatility_sq(self):
        """计算短期回报率的方差 (sigma^2)"""
        if len(self.mid_prices) < 5: # 需要足够样本
            return 0.0
        
        prices = np.array(self.mid_prices)
        log_returns = np.log(prices[1:] / prices[:-1])
        
        # 方差
        return np.var(log_returns)

    def _calculate_safe_vol(self, symbol, price):
        """计算符合交易所限制的下单量"""
        info = ref_data_manager.get_info(symbol)
        if not info: return 0.0
        safe_min = max(5.0, info.min_notional) * 1.1
        qty_val = safe_min / price
        target = max(info.min_qty, qty_val) * self.lot_multiplier
        return ref_data_manager.round_qty(symbol, target)

    def on_orderbook(self, ob: OrderBook):
        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 == 0: return
        
        mid_price = (bid_1 + ask_1) / 2.0
        self.mid_prices.append(mid_price)
        
        # --- 1. 周期控制 ---
        now = time.time()
        if now - self.last_recalc_time < self.interval:
            return
        
        self.last_recalc_time = now
        
        # --- 2. [规范操作] 先清场 ---
        # 无论价格变没变，都撤销旧报价，准备发布新报价
        # 注意：这里的 cancel_all 现在调用的是 OMS 接口
        self.cancel_all(ob.symbol)

        # --- 3. A-S 核心计算 ---
        
        # A. 更新波动率
        self.current_sigma_sq = self._calculate_volatility_sq()
        
        # B. 计算保留价格 (Reservation Price)
        # r = s - q * gamma * sigma^2 * T (T=1)
        # 这里的 q 是 self.pos (净持仓)
        inventory_risk_adjustment = self.pos * self.gamma * self.current_sigma_sq
        reservation_price = mid_price - inventory_risk_adjustment
        
        # C. 计算最优价差 (Optimal Spread)
        # δ_a + δ_b = (2/gamma) * ln(1 + gamma/k)
        if self.k > 0:
            optimal_spread = (2.0 / self.gamma) * math.log(1.0 + self.gamma / self.k)
        else:
            optimal_spread = mid_price * 0.001 # Fallback
            
        # D. 结合波动率调整价差 (工程实践)
        # 波动越大，价差应该越宽，以保护自己
        # Spread = OptimalSpread + VolatilityAdjustment
        # 这里的 sigma 是收益率标准差，本身就是比例
        volatility_adjustment = self.current_sigma_sq * self.gamma * mid_price # 简单的线性调整
        
        # 总价差
        total_spread = optimal_spread + volatility_adjustment
        
        # E. 应用最小价差保护
        min_spread_val = mid_price * self.min_spread_ratio
        final_spread = max(total_spread, min_spread_val)

        # 4. 计算目标挂单价
        target_bid = reservation_price - final_spread / 2.0
        target_ask = reservation_price + final_spread / 2.0
        
        # 5. 规整化与安全检查
        # 策略层负责规整，OMS层负责最终校验
        target_bid = ref_data_manager.round_price(ob.symbol, target_bid)
        target_ask = ref_data_manager.round_price(ob.symbol, target_ask)
        
        # 6. 执行新挂单
        order_vol = self._calculate_safe_vol(ob.symbol, mid_price)
        if order_vol <= 0: return
        
        # 挂买单 (Bid)
        # 使用 PostOnly 确保我们是 Maker
        intent_buy = OrderIntent(
            strategy_id=self.name,
            symbol=ob.symbol,
            side=Side.BUY,
            price=target_bid,
            volume=order_vol,
            is_post_only=True
        )
        self.send_intent(intent_buy)
        
        # 挂卖单 (Ask)
        intent_sell = OrderIntent(
            strategy_id=self.name,
            symbol=ob.symbol,
            side=Side.SELL,
            price=target_ask,
            volume=order_vol,
            is_post_only=True
        )
        self.send_intent(intent_sell)
        
        # 打印日志
        # self.log(f"Quoting Bid={target_bid} Ask={target_ask} | r={reservation_price:.2f} s={final_spread:.2f}")

    def on_trade(self, trade: TradeData):
        # A-S 模型不依赖 Trade 流，但可以打印日志
        # self.log(f"成交: {trade.side} @ {trade.price} Vol={trade.volume}")
        pass

    def on_order(self, snapshot: OrderStateSnapshot):
        # 调用基类处理 active_orders
        super().on_order(snapshot)
        # 可选：如果订单被 Reject，可以在这里加入重试或调整逻辑