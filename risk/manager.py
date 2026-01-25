# file: risk/manager.py

import time
import math
import numpy as np
from collections import deque
from event.type import OrderRequest, OrderData, Event, EVENT_LOG, EVENT_ORDER_UPDATE, EVENT_MARK_PRICE, EVENT_ACCOUNT_UPDATE, EVENT_ORDERBOOK
from event.type import Status_ALLTRADED, Status_CANCELLED, Status_REJECTED
from infrastructure.logger import logger
from data.cache import data_cache

class RiskManager:
    def __init__(self, engine, config: dict, oms=None, gateway=None):
        self.engine = engine
        self.oms = oms
        self.gateway = gateway
        self.config = config.get("risk", {})
        
        # --- 开关 ---
        self.active = self.config.get("active", True)
        self.kill_switch_triggered = False
        self.kill_reason = ""
        
        # --- 阈值加载 ---
        limits = self.config.get("limits", {})
        self.max_order_qty = limits.get("max_order_qty", 1000.0)
        self.max_order_notional = limits.get("max_order_notional", 5000.0)
        self.max_pos_notional = limits.get("max_pos_notional", 20000.0)
        self.max_daily_loss = limits.get("max_daily_loss", 500.0)
        
        sanity = self.config.get("price_sanity", {})
        self.max_deviation_pct = sanity.get("max_deviation_pct", 0.05)
        
        tech = self.config.get("tech_health", {})
        self.max_latency_ms = tech.get("max_latency_ms", 1000)
        self.max_orders_per_sec = tech.get("max_order_count_per_sec", 20)
        
        # --- 运行时状态 ---
        self.order_history = deque()
        self.daily_pnl = 0.0
        self.initial_equity = 0.0
        
        # 注册监听
        self.engine.register(EVENT_ORDER_UPDATE, self.on_order_update)
        self.engine.register(EVENT_MARK_PRICE, self.on_mark_price)
        self.engine.register(EVENT_ACCOUNT_UPDATE, self.on_account_update)
        self.engine.register(EVENT_ORDERBOOK, self.on_orderbook)

    # ==========================
    # 1. 预交易风控 (Pre-Trade)
    # ==========================
    def check_order(self, req: OrderRequest) -> bool:
        """
        下单前的最后一道防线
        """
        if self.kill_switch_triggered:
            # self._log_warn(f"拦截下单: 系统已熔断 ({self.kill_reason})")
            return False

        if not self.active: return True

        # 1.1 频率限制
        now = time.time()
        while self.order_history and self.order_history[0] < now - 1.0:
            self.order_history.popleft()
        if len(self.order_history) >= self.max_orders_per_sec:
            self._log_warn("拦截下单: 频率超限")
            return False
        
        # 1.2 单笔规模
        if req.volume > self.max_order_qty:
            self._log_warn(f"拦截下单: 数量 {req.volume} > {self.max_order_qty}")
            return False
        
        notional = req.price * req.volume
        if notional > self.max_order_notional:
            self._log_warn(f"拦截下单: 金额 {notional:.2f} > {self.max_order_notional}")
            return False

        # 1.3 价格偏离
        mark_price = data_cache.get_mark_price(req.symbol)
        if mark_price > 0:
            deviation = abs(req.price - mark_price) / mark_price
            if deviation > self.max_deviation_pct:
                self._log_warn(f"拦截下单: 价格偏离 {deviation*100:.2f}%")
                return False

        # 1.4 OMS 相关检查 (持仓与资金)
        if self.oms:
            # [修复] 访问 oms.exposure 而不是 oms.position
            # [修复] positions 字典现在直接存 float 数量
            current_vol = self.oms.exposure.positions.get(req.symbol, 0.0)
            
            # 预估持仓价值 (绝对值叠加)
            new_notional = (abs(current_vol) + req.volume) * req.price
            if new_notional > self.max_pos_notional:
                self._log_warn(f"拦截下单: 预估持仓 {new_notional:.2f} > {self.max_pos_notional}")
                return False
            
            # [修复] 保证金检查：访问 oms.account.check_margin
            # oms.check_risk 已经在 engine 重构中移除了，直接调用 account 组件
            if not self.oms.account.check_margin(notional):
                # self._log_warn(f"拦截下单: 保证金不足")
                return False

        # --- 通过 ---
        self.order_history.append(now)
        return True

    # ==========================
    # 2. 盘中监控
    # ==========================
    def on_mark_price(self, event: Event):
        if self.kill_switch_triggered: return
        data = event.data
        if abs(data.mark_price - data.index_price) / data.index_price > 0.05:
            self.trigger_kill_switch(f"黑天鹅: 现货/期货价差异常 {data.symbol}")

    def on_orderbook(self, event: Event):
        if self.kill_switch_triggered: return
        ob = event.data
        latency_ms = (time.time() - ob.datetime.timestamp()) * 1000
        if latency_ms > self.max_latency_ms:
            # self._log_warn(f"高延迟警告: {latency_ms:.1f}ms")
            pass

    def on_account_update(self, event: Event):
        if self.kill_switch_triggered: return
        acc = event.data
        if self.initial_equity == 0:
            self.initial_equity = acc.equity
        drawdown = self.initial_equity - acc.equity
        if drawdown > self.max_daily_loss:
            self.trigger_kill_switch(f"触及日内最大亏损: -{drawdown:.2f}")

    def on_order_update(self, event: Event):
        # 维护频率限制用的 active_order_count 逻辑移到了这里吗？
        # 注意：之前的代码里 RiskManager 维护了一个 active_order_count，但新版主要依赖 OMS Exposure。
        # 如果需要基于“订单个数”的风控，可以在这里维护。
        pass

    # ==========================
    # 3. 熔断机制
    # ==========================
    def trigger_kill_switch(self, reason: str):
        if self.kill_switch_triggered: return
        
        self.kill_switch_triggered = True
        self.kill_reason = reason
        logger.critical(f"🔥 KILL SWITCH TRIGGERED: {reason} 🔥")
        
        if self.gateway:
            # 简单的全撤单
            symbols = self.oms.exposure.positions.keys() if self.oms else []
            for s in symbols:
                self.gateway.cancel_all_orders(s)

    def _log_warn(self, msg):
        self.engine.put(Event(EVENT_LOG, f"[Risk] {msg}"))