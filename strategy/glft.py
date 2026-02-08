# file: strategy/glft_strategy.py

import time
import math
import numpy as np
from collections import deque, defaultdict

from .base import StrategyTemplate
from event.type import OrderBook, TradeData, OrderIntent, Side, AggTradeData, OrderStateSnapshot
from event.type import Event, EVENT_STRATEGY_UPDATE, StrategyData

# Alpha & Math Modules
from alpha.factors import GLFTCalibrator
from alpha.signal import OnlineRidgePredictor
from alpha.gate import AlphaGate
from alpha.engine import FeatureEngine

# Data Modules
from data.ref_data import ref_data_manager
from data.cache import data_cache

class GLFTStrategy(StrategyTemplate):
    """
    [Final] GLFT 自适应做市策略 (修复配置访问及多币种隔离)
    架构：Strategy -> OMS -> Gateway
    """
    def __init__(self, engine, oms):
        # 接口对齐：engine, oms, name
        super().__init__(engine, oms, "GLFT_Pro")
        
        # 加载完整配置
        full_config = self._load_full_config()
        # 提取策略层专用配置
        self.strat_conf = full_config.get("strategy", {})
        
        # --- 基础参数 ---
        self.gamma_base = self.strat_conf.get("gamma", 0.1) 
        self.lot_multiplier = self.strat_conf.get("lot_multiplier", 1.0)
        self.cycle_interval = self.strat_conf.get("cycle_interval", 1.0)
        
        # 执行层参数
        self.exec_conf = self.strat_conf.get("execution", {})
        self.min_spread_bps = self.exec_conf.get("min_spread_bps", 5.0)
        
        # --- 全局组件 ---
        self.feature_engine = FeatureEngine()
        
        # --- Per-Symbol 组件池 ---
        self.calibrators = {}  
        self.models = {}       
        self.gates = {}        
        
        self.last_run_times = defaultdict(float)
        
        print(f"[{self.name}] 策略启动. Gamma={self.gamma_base}, Interval={self.cycle_interval}s")

    def _load_full_config(self):
        """加载完整配置文件"""
        try:
            import json
            with open("config.json", "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"Strategy Config Load Error: {e}")
            return {}

    def _get_components(self, symbol):
        """Lazy Initialization 为每个币种创建独立的模型"""
        if symbol not in self.calibrators:
            # 1. 校准器
            self.calibrators[symbol] = GLFTCalibrator(window=1000)
            # 2. 9维在线学习模型
            self.models[symbol] = OnlineRidgePredictor(num_features=9, lambda_reg=1.0)
            # 3. 信号门控
            self.gates[symbol] = AlphaGate(
                max_bps=5.0, 
                decay_factor=0.9, 
                inventory_dampening=0.1
            )
        return self.calibrators[symbol], self.models[symbol], self.gates[symbol]

    def _calculate_safe_vol(self, symbol, price):
        """计算符合交易所规则的下单量"""
        info = ref_data_manager.get_info(symbol)
        if not info: return 0.0
        min_vol_by_val = (info.min_notional * 1.1) / price
        base_vol = max(info.min_qty, min_vol_by_val)
        target_vol = base_vol * self.lot_multiplier
        return ref_data_manager.round_qty(symbol, target_vol)

    def on_orderbook(self, ob: OrderBook):
        """
        核心 Tick 驱动
        全面考虑：配置校验、多币种隔离、ML预测、GLFT定价、安全钳、OMS 交互
        """
        symbol = ob.symbol
        # 获取该币种独立组件
        calibrator, model, gate = self._get_components(symbol)
        
        # 1. 更新特征与校准器
        calibrator.on_orderbook(ob)
        self.feature_engine.on_orderbook(ob)
        
        # 2. 频率控制 (1秒周期)
        now = time.time()
        if now - self.last_run_times[symbol] < self.cycle_interval:
            return
        self.last_run_times[symbol] = now

        # 3. 基础价格
        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 == 0 or ask_1 == 0: return
        mid = (bid_1 + ask_1) / 2.0
        
        # 4. [ML] 信号生成
        features = self.feature_engine.get_features(symbol)
        raw_pred_bps = model.update_and_predict(features, mid)
        
        # 5. [Inventory] 持仓归一化
        # 直接从 OMS Exposure 获取净持仓
        current_pos = self.oms.exposure.net_positions.get(symbol, 0.0)
        order_vol = self._calculate_safe_vol(symbol, mid)
        if order_vol <= 0: return
        q_norm = current_pos / order_vol
        
        # 6. [AlphaGate] 信号过滤
        final_alpha_bps = gate.process(raw_pred_bps, q_norm)
        fair_mid = mid * (1 + final_alpha_bps / 10000.0)

        # 7. [Adaptive Risk] 动态风险厌恶
        acc = self.oms.account
        gamma_adaptive = self.gamma_base
        if acc.equity > 0:
            usage = acc.used_margin / acc.equity
            if usage > 0.5:
                gamma_adaptive *= (1 + (usage - 0.5) * 4)

        # 8. [GLFT] 核心参数获取
        sigma = max(1.0, calibrator.sigma_bps)
        A = max(0.1, calibrator.A)
        k = max(0.1, calibrator.k)

        # 9. [GLFT Math] 计算价差与偏移
        # 半价差 bps
        base_half_spread_bps = (1.0 / gamma_adaptive) * math.log(1.0 + gamma_adaptive / k)
        # 库存偏移 bps
        inventory_skew_bps = q_norm * (gamma_adaptive * (sigma ** 2)) / (2 * A * k)
        
        # 10. 计算目标价 (相对于 Fair Value)
        base_spread_price = mid * (base_half_spread_bps / 10000.0)
        skew_price = mid * (inventory_skew_bps / 10000.0)
        
        target_bid = fair_mid - base_spread_price - skew_price
        target_ask = fair_mid + base_spread_price - skew_price

        # 11. [Sanity Check] 安全限制
        info = ref_data_manager.get_info(symbol)
        tick_size = info.tick_size
        
        # 最小价差限制
        min_half_price = mid * (self.min_spread_bps / 20000.0)
        if (target_ask - target_bid) < (min_half_price * 2):
            center = (target_bid + target_ask) / 2
            target_bid = center - min_half_price
            target_ask = center + min_half_price

        # 价格精度规整
        target_bid = ref_data_manager.round_price(symbol, target_bid)
        target_ask = ref_data_manager.round_price(symbol, target_ask)
        
        # 边界检查：不允许 Taker
        if target_bid >= ask_1: target_bid = ask_1 - tick_size
        if target_ask <= bid_1: target_ask = bid_1 + tick_size
        
        # 倒挂保护
        if target_bid >= target_ask:
            target_bid = mid - tick_size
            target_ask = mid + tick_size

        # 12. [Execution] 指令下达
        # 第一步：先清场 (由 OMS 执行)
        self.cancel_all(symbol)
        
        # 第二步：检查持仓限额并发送 Intent
        # 这里的 max_pos_notional 访问 config 的 risk 路径
        # 我们之前已经将 full_config 加载过
        max_notional = self.strat_conf.get("max_pos_usdt", 20000.0)
        current_val = abs(current_pos) * mid
        
        # 买单
        if current_val < max_notional or current_pos < 0:
            self.send_intent(OrderIntent(
                strategy_id=self.name, symbol=symbol, side=Side.BUY,
                price=target_bid, volume=order_vol, is_post_only=True, is_rpi=False
            ))
            
        # 卖单
        if current_val < max_notional or current_pos > 0:
            self.send_intent(OrderIntent(
                strategy_id=self.name, symbol=symbol, side=Side.SELL,
                price=target_ask, volume=order_vol, is_post_only=True, is_rpi=False
            ))

        # 13. 更新 UI 数据
        strat_data = StrategyData(
            symbol=symbol, fair_value=fair_mid, alpha_bps=final_alpha_bps,
            gamma=gamma_adaptive, k=k, A=A, sigma=sigma
        )
        self.engine.put(Event(EVENT_STRATEGY_UPDATE, strat_data))
        
        # 14. 周期清理
        self.feature_engine.reset_interval(symbol)

    def on_market_trade(self, trade: AggTradeData):
        """市场成交流驱动模型学习"""
        self.feature_engine.on_trade(trade)
        if trade.symbol in self.calibrators:
            # 使用缓存中的标记价格作为参考点
            mid = data_cache.get_mark_price(trade.symbol)
            self.calibrators[trade.symbol].on_market_trade(trade, mid)

    def on_trade(self, trade: TradeData):
        """自有成交回报"""
        self.log(f"FILL: {trade.symbol} {trade.side} {trade.volume} @ {trade.price}")

    def on_order(self, snapshot: OrderStateSnapshot):
        """订单状态更新回调"""
        super().on_order(snapshot)