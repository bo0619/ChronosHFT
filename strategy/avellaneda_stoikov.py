# file: strategy/avellaneda_stoikov.py

import time
import math
import numpy as np
from collections import deque
from .base import StrategyTemplate
from event.type import OrderBook, TradeData, PositionData
from data.ref_data import ref_data_manager

class AvellanedaStoikovStrategy(StrategyTemplate):
    """
    经典的 Avellaneda-Stoikov 做市策略 (Crypto 适配版)
    周期：1秒
    核心：基于库存风险调整报价中心，基于波动率调整价差
    """
    def __init__(self, engine, gateway, risk_manager):
        super().__init__(engine, gateway, risk_manager)
        
        self.config = self._load_strategy_config()
        self.as_conf = self.config.get("as_parameters", {})
        
        # --- 核心参数 ---
        self.gamma = self.as_conf.get("gamma", 0.05)  # 风险厌恶系数
        self.k = self.as_conf.get("k", 1.5)           # 订单流密度参数
        self.vol_window = self.as_conf.get("vol_window", 60) # 波动率计算窗口(样本数)
        self.interval = self.config.get("cycle_interval", 1.0) # 1秒周期
        self.min_spread = self.as_conf.get("min_spread_ratio", 0.0002) # 最小价差保护
        
        self.lot_multiplier = self.config.get("lot_multiplier", 1.0)
        
        # --- 运行时状态 ---
        self.mid_prices = deque(maxlen=self.vol_window)
        self.last_recalc_time = 0.0
        self.current_sigma = 0.0 # 当前波动率
        
        # 记录目标挂单价格，用于防止微小变动频繁撤挂
        self.target_bid = 0.0
        self.target_ask = 0.0
        
        print(f"[{self.name}] 启动 A-S 模型: Gamma={self.gamma}, K={self.k}, Cycle={self.interval}s")

    def _load_strategy_config(self):
        try:
            import json
            with open("config.json", "r") as f: return json.load(f).get("strategy", {})
        except: return {}

    def _calculate_volatility(self):
        """计算短期回报率的标准差"""
        if len(self.mid_prices) < 2:
            return 0.0
        
        prices = np.array(self.mid_prices)
        # 计算对数收益率: ln(Pt / Pt-1)
        log_returns = np.log(prices[1:] / prices[:-1])
        # 标准差
        vol = np.std(log_returns)
        return vol

    def _calculate_safe_vol(self, symbol, price):
        """计算下单量 (复用之前的逻辑)"""
        info = ref_data_manager.get_info(symbol)
        if not info: return 0.0
        safe_min = max(5.0, info.min_notional) * 1.1
        qty_val = safe_min / price
        target = max(info.min_qty, qty_val) * self.lot_multiplier
        return ref_data_manager.round_qty(symbol, target)

    def on_orderbook(self, ob: OrderBook):
        # 1. 数据收集
        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 == 0: return
        
        mid_price = (bid_1 + ask_1) / 2.0
        self.mid_prices.append(mid_price)
        
        # 2. 周期控制 (1秒一次)
        now = time.time()
        if now - self.last_recalc_time < self.interval:
            return
        
        self.last_recalc_time = now
        
        # --- A-S 核心计算 ---
        
        # A. 更新波动率 (Sigma)
        # 注意：这里计算的是 timeframe 级别的波动率。
        # A-S 公式中的 sigma^2 通常指单位时间的方差。
        self.current_sigma = self._calculate_volatility()
        sigma_sq = self.current_sigma ** 2
        
        if sigma_sq == 0: 
            # 初始阶段波动率为0，使用默认宽 Spread 保护
            reservation_price = mid_price
            spread = mid_price * 0.001 
        else:
            # B. 计算保留价格 (Reservation Price)
            # r = s - q * gamma * sigma^2
            # 注意量纲：q 是币的数量。
            # 如果 q 很大 (如 DOGE 10000个)，gamma 应该很小。
            # 或者我们将 q 归一化为“份数”。这里直接用绝对数量。
            inventory_risk = self.pos * self.gamma * sigma_sq * mid_price # 乘mid_price是为了把比例转为价格差?
            # 修正 A-S 原版公式 r = s - q * gamma * sigma^2
            # 原版假设 s 是布朗运动 dS = sigma dW。sigma 是绝对价格波动。
            # 我们算的 current_sigma 是收益率波动。所以绝对波动率 = price * current_sigma
            # 绝对方差 = (price * current_sigma)^2
            
            abs_variance = (mid_price * self.current_sigma) ** 2
            reservation_price = mid_price - (self.pos * self.gamma * abs_variance)
            
            # C. 计算最优价差 (Optimal Spread)
            # delta = (2/gamma) * ln(1 + gamma/k)
            # 这是一个基于模型的理论 Spread。
            # 在 Crypto 这种离散盘口，如果 delta 太小，需要强制扩大到 tick size
            
            # 2/gamma * ln(1 + gamma/k)
            # 如果 gamma 很小，这个值会很大；如果 gamma 很大，这个值会很小。
            # spread_width = delta (单边) * 2 ? 原文公式算出的是 half_spread 还是 full?
            # A-S 原文 result: delta_b + delta_a = (2/gamma) * ln(1 + gamma/k)
            # 所以我们计算的是 total spread。
            
            spread_val = (2.0 / self.gamma) * math.log(1.0 + self.gamma / self.k)
            
            # 这里的 spread_val 是“半价差”还是“全价差”取决于 K 的定义。
            # 实际上，我们需要将其转换为价格单位。
            # 由于 A-S 的效用函数是基于 PnL (金额) 的，这里的 spread_val 是绝对价格单位。
            # 但是 K 的量纲是 (1/价格)。
            
            # 为了工程稳健，我们加一个 Min Spread 保护
            min_spread_val = mid_price * self.min_spread
            final_spread = max(spread_val, min_spread_val)
            
            # 避免 Spread 亦过大 (比如 K 设置不合理)
            final_spread = min(final_spread, mid_price * 0.05) # 最大 5%

        # D. 计算目标挂单价
        target_bid = reservation_price - final_spread / 2.0
        target_ask = reservation_price + final_spread / 2.0
        
        # 3. 价格规整与安全检查
        target_bid = ref_data_manager.round_price(ob.symbol, target_bid)
        target_ask = ref_data_manager.round_price(ob.symbol, target_ask)
        
        # 防止价格倒挂 (Reservation Price 偏离太远可能导致 Bid > Ask)
        if target_bid >= target_ask:
            target_bid = mid_price - ref_data_manager.get_info(ob.symbol).tick_size * 5
            target_ask = mid_price + ref_data_manager.get_info(ob.symbol).tick_size * 5
            
        # 防止 Bid > Market Ask (变成 Taker) 
        # A-S 策略本质是 Maker，如果算出的价格穿了盘口，说明我们极度想建仓/平仓
        # 此时可以用 PostOnly 确保 Maker，或者允许 Taker。
        # 这里我们保守一点：Clamping 到盘口以内
        if target_bid >= ask_1: target_bid = ask_1 - ref_data_manager.get_info(ob.symbol).tick_size
        if target_ask <= bid_1: target_ask = bid_1 + ref_data_manager.get_info(ob.symbol).tick_size

        # 4. 执行逻辑 (Quote Flickering)
        order_vol = self._calculate_safe_vol(ob.symbol, mid_price)
        if order_vol <= 0: return
        
        # 智能撤挂 (Smart Refresh)
        # 只有当目标价格变动超过一定幅度才修改，但在 1s 周期下，我们通常每次都重挂
        # 因为 1s 已经是很长的时间了，盘口早就变了。
        
        # 为了演示清晰，我们采用：撤销所有 -> 挂新单
        self.cancel_all()
        
        # 智能下单 (Smart Entry/Exit from base.py is not needed here as we use explicit prices)
        # 但我们之前写的 _smart_buy/sell 依然很有用，用来处理平仓逻辑
        self._smart_buy(ob.symbol, target_bid, order_vol)
        self._smart_sell(ob.symbol, target_ask, order_vol)
        
        # 打印状态
        # self.log(f"r={reservation_price:.2f} spread={final_spread:.2f} pos={self.pos}")

    def _smart_buy(self, symbol, price, vol):
        # 优先平空
        if self.pos < 0: # 当前持有空单
            # 假如持有 -10，想买 2。只需平空 2。
            return self.exit_short(symbol, price, vol)
        else:
            return self.entry_long(symbol, price, vol)

    def _smart_sell(self, symbol, price, vol):
        # 优先平多
        if self.pos > 0:
            return self.exit_long(symbol, price, vol)
        else:
            return self.entry_short(symbol, price, vol)

    def on_trade(self, trade: TradeData):
        self.log(f"成交: {trade.side} @ {trade.price} Vol={trade.volume}")