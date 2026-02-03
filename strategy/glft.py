# file: strategy/glft_strategy.py

import time
import math
import numpy as np
from .base import StrategyTemplate
from event.type import OrderBook, TradeData, OrderIntent, Side, AggTradeData, Event, EVENT_STRATEGY_UPDATE, StrategyData
from alpha.factors import GLFTCalibrator
from alpha.engine import FeatureEngine
from alpha.signal import OnlineRidgePredictor
from data.ref_data import ref_data_manager
from data.cache import data_cache

class GLFTStrategy(StrategyTemplate):
    def __init__(self, engine, oms):
        super().__init__(engine, oms, "GLFT_Adaptive")
        
        # --- 策略配置 ---
        self.config = self._load_strategy_config()
        self.gamma_base = self.config.get("gamma", 0.1) 
        self.lot_multiplier = self.config.get("lot_multiplier", 1.0)
        self.cycle_interval = self.config.get("cycle_interval", 1.0)
        
        # --- 子组件 ---
        self.calibrator = GLFTCalibrator(window=100)
        
        # [NEW] 引入 ML 组件 (之前可能漏了初始化)
        self.feature_engine = FeatureEngine()
        self.ml_model = OnlineRidgePredictor(num_features=9)
        
        self.last_run_time = 0
        
        # [NEW] 参数打印定时器
        self.last_param_log_time = 0
        self.log_interval = 60 # 60秒打印一次

    def _load_strategy_config(self):
        try:
            import json
            with open("config.json", "r") as f: return json.load(f).get("strategy", {})
        except: return {}

    def _calculate_safe_vol(self, symbol, price):
        info = ref_data_manager.get_info(symbol)
        if not info: return 0.0
        # 动态计算满足最小金额的数量
        # 防止除零
        if price <= 0: return 0.0
        min_vol = max(info.min_qty, (info.min_notional * 1.1) / price)
        return ref_data_manager.round_qty(symbol, min_vol * self.lot_multiplier)

    def on_orderbook(self, ob: OrderBook):
        # 1. 基础数据准备
        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 == 0 or ask_1 == 0: return
        
        mid_price = (bid_1 + ask_1) / 2.0
        
        # 2. 更新校准器与特征引擎
        self.calibrator.on_orderbook(ob)
        self.feature_engine.on_orderbook(ob)

        # 3. 频率控制
        now = time.time()
        if now - self.last_run_time < self.cycle_interval:
            return
        self.last_run_time = now

        # 4. [ML] 在线学习与预测
        features = self.feature_engine.get_features()
        pred_bps = self.ml_model.update_and_predict(features, mid_price)
        fair_mid = mid_price * (1 + pred_bps / 10000.0)

        # 5. [Risk] 动态风险厌恶系数 (Adaptive Gamma)
        acc = self.oms.account
        if acc.equity > 0:
            usage_ratio = acc.used_margin / acc.equity
            # 资金占用 > 50% 时 gamma 开始变大
            gamma_adaptive = self.gamma_base * (1 + max(0, (usage_ratio - 0.5) * 5))
        else:
            gamma_adaptive = self.gamma_base

        # 6. 获取 GLFT 实时参数
        sigma = max(1.0, self.calibrator.sigma_bps)
        A = self.calibrator.A
        k = self.calibrator.k
        
        # 7. 计算单笔标准下单量
        order_vol = self._calculate_safe_vol(ob.symbol, mid_price)
        if order_vol <= 0: return

        # 8. [GLFT Core] 计算最优半价差
        base_half_spread_bps = (1.0 / gamma_adaptive) * math.log(1.0 + gamma_adaptive / k)
        
        # 9. [Inventory] 库存风险偏移
        q_norm = self.pos / order_vol
        inventory_skew_bps = q_norm * (gamma_adaptive * (sigma ** 2)) / (2 * A * k)
        
        # 10. 计算最终报价距离
        bid_delta_bps = base_half_spread_bps + inventory_skew_bps
        ask_delta_bps = base_half_spread_bps - inventory_skew_bps
        
        # 11. [修正] 获取最小价差配置 (修复 KeyError)
        # config 结构已经是 strategy 层级，直接取 execution
        exec_conf = self.config.get("execution", {}) 
        min_spread_bps = exec_conf.get("min_spread_bps", 5.0)
        
        bid_delta_bps = max(min_spread_bps, bid_delta_bps)
        ask_delta_bps = max(min_spread_bps, ask_delta_bps)

        target_bid = fair_mid * (1 - bid_delta_bps / 10000.0)
        target_ask = fair_mid * (1 + ask_delta_bps / 10000.0)

        # 12. 价格规整与安全钳
        tick_size = ref_data_manager.get_info(ob.symbol).tick_size
        target_bid = ref_data_manager.round_price(ob.symbol, target_bid)
        target_ask = ref_data_manager.round_price(ob.symbol, target_ask)
        
        # 防穿仓
        if target_bid >= ask_1: target_bid = ask_1 - tick_size
        if target_ask <= bid_1: target_ask = bid_1 + tick_size
        # 防倒挂
        if target_bid >= target_ask:
            mid_safe = (bid_1 + ask_1) / 2
            target_bid = mid_safe - tick_size * 2
            target_ask = mid_safe + tick_size * 2

        # 13. [Execution] 执行交易
        self.cancel_all(ob.symbol)
        
        # 买单 (PostOnly)
        self.send_intent(OrderIntent(
            strategy_id=self.name, symbol=ob.symbol, side=Side.BUY,
            price=target_bid, volume=order_vol, is_post_only=True
        ))
        
        # 卖单 (PostOnly)
        self.send_intent(OrderIntent(
            strategy_id=self.name, symbol=ob.symbol, side=Side.SELL,
            price=target_ask, volume=order_vol, is_post_only=True
        ))
        
        # 14. 周期收尾
        self.feature_engine.reset_interval()

        strat_data = StrategyData(
            symbol=ob.symbol,
            fair_value=fair_mid,
            alpha_bps=pred_bps,
            gamma=gamma_adaptive,
            k=k,
            A=A,
            sigma=sigma
        )
        self.engine.put(Event(EVENT_STRATEGY_UPDATE, strat_data))

    def on_trade(self, trade: TradeData):
        pass

    def on_market_trade(self, trade: AggTradeData):
        """接收 Market AggTrade 数据来校准 A 和 k"""
        mid = data_cache.get_mark_price(trade.symbol)
        self.calibrator.on_market_trade(trade, mid)