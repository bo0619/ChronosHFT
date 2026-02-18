# file: risk/manager.py
#
# ============================================================
# BUG FIX CHANGELOG
#
# [FIX-H] trigger_kill_switch(): EMERGENCY STOP 无法真正阻断新订单
#
#   原代码：
#     self.kill_switch_triggered = True
#     logger.critical(...)
#     for s in symbols: self.gateway.cancel_all_orders(s)
#
#   问题：
#     kill_switch_triggered 标志只被 check_order(OrderRequest) 检查，
#     而在新的 OMS 架构中，发单路径是：
#       Strategy → OMS.submit_order(OrderIntent) → Gateway.send_order()
#     OMS.submit_order 只检查自身的 LifecycleState，并不检查 RiskManager 的标志，
#     导致熔断后策略仍可通过 OMS 持续下单。
#     日志中 EMERGENCY STOP 与 Order Successfully Sent 交替出现即为此证明。
#
#   修复：
#     trigger_kill_switch 同时调用 self.oms.halt_system(reason)，
#     将 OMS 状态设为 LifecycleState.HALTED。
#     OMS.submit_order 首行检查：
#       if self.state != LifecycleState.LIVE: return None
#     因此 HALTED 状态下所有新订单在 OMS 层被拒绝，
#     形成"RiskManager 熔断 → OMS 硬阻断"的双层防御。
# ============================================================

import time
import math
import numpy as np
from collections import deque
from event.type import (
    OrderRequest, OrderData, Event,
    EVENT_LOG, EVENT_ORDER_UPDATE, EVENT_MARK_PRICE,
    EVENT_ACCOUNT_UPDATE, EVENT_ORDERBOOK,
    Status_ALLTRADED, Status_CANCELLED, Status_REJECTED,
)
from infrastructure.logger import logger
from data.cache import data_cache


class RiskManager:
    def __init__(self, engine, config: dict, oms=None, gateway=None):
        self.engine  = engine
        self.oms     = oms
        self.gateway = gateway
        self.config  = config.get("risk", {})

        # --- 开关 ---
        self.active               = self.config.get("active", True)
        self.kill_switch_triggered = False
        self.kill_reason           = ""

        # --- 阈值加载 ---
        limits = self.config.get("limits", {})
        self.max_order_qty       = limits.get("max_order_qty",       1000.0)
        self.max_order_notional  = limits.get("max_order_notional",  5000.0)
        self.max_pos_notional    = limits.get("max_pos_notional",   20000.0)
        self.max_daily_loss      = limits.get("max_daily_loss",       500.0)

        sanity = self.config.get("price_sanity", {})
        self.max_deviation_pct = sanity.get("max_deviation_pct", 0.05)

        tech = self.config.get("tech_health", {})
        self.max_latency_ms       = tech.get("max_latency_ms",          1000)
        self.max_orders_per_sec   = tech.get("max_order_count_per_sec",   20)

        # --- 运行时状态 ---
        self.order_history  = deque()
        self.daily_pnl      = 0.0
        self.initial_equity = 0.0

        # 注册监听
        self.engine.register(EVENT_ORDER_UPDATE,   self.on_order_update)
        self.engine.register(EVENT_MARK_PRICE,     self.on_mark_price)
        self.engine.register(EVENT_ACCOUNT_UPDATE, self.on_account_update)
        self.engine.register(EVENT_ORDERBOOK,      self.on_orderbook)

    # ==========================
    # 1. 预交易风控 (Pre-Trade)
    # ==========================
    def check_order(self, req: OrderRequest) -> bool:
        """
        下单前的最后一道防线（适用于 Legacy Gateway 直调路径）
        OMS 路径已由 halt_system 在更上游阻断。
        """
        if self.kill_switch_triggered:
            return False

        if not self.active:
            return True

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

        # 1.4 OMS 相关检查（持仓与资金）
        if self.oms:
            current_vol  = self.oms.exposure.net_positions.get(req.symbol, 0.0)
            new_notional = (abs(current_vol) + req.volume) * req.price
            if new_notional > self.max_pos_notional:
                self._log_warn(f"拦截下单: 预估持仓 {new_notional:.2f} > {self.max_pos_notional}")
                return False

            if not self.oms.account.check_margin(notional):
                return False

        self.order_history.append(now)
        return True

    # ==========================
    # 2. 盘中监控
    # ==========================
    def on_mark_price(self, event: Event):
        if self.kill_switch_triggered:
            return
        data = event.data
        if abs(data.mark_price - data.index_price) / data.index_price > 0.05:
            self.trigger_kill_switch(f"黑天鹅: 现货/期货价差异常 {data.symbol}")

    def on_orderbook(self, event: Event):
        if self.kill_switch_triggered:
            return
        ob = event.data
        latency_ms = (time.time() - ob.datetime.timestamp()) * 1000
        if latency_ms > self.max_latency_ms:
            pass  # 可在此加高延迟告警

    def on_account_update(self, event: Event):
        if self.kill_switch_triggered:
            return
        acc = event.data
        if self.initial_equity == 0:
            self.initial_equity = acc.equity
        drawdown = self.initial_equity - acc.equity
        if drawdown > self.max_daily_loss:
            self.trigger_kill_switch(f"触及日内最大亏损: -{drawdown:.2f}")

    def on_order_update(self, event: Event):
        pass

    # ==========================
    # 3. 熔断机制
    # ==========================
    def trigger_kill_switch(self, reason: str):
        """
        [FIX-H] 双层硬熔断：
          1. RiskManager 标志 → 拦截 Legacy check_order 路径
          2. OMS.halt_system → 将 OMS 状态置为 HALTED，
             阻断 Strategy → OMS.submit_order 路径（这是主路径）

        修复前：只有标志1，OMS 路径不受影响，策略继续下单。
        修复后：两条路径全部阻断，真正做到"熔断即停单"。
        """
        if self.kill_switch_triggered:
            return

        self.kill_switch_triggered = True
        self.kill_reason           = reason
        logger.critical(f"🔥 KILL SWITCH TRIGGERED: {reason} 🔥")

        # ── 步骤1：撤销所有在途挂单 ───────────────────────────
        if self.gateway:
            symbols = list(self.oms.exposure.net_positions.keys()) if self.oms else []
            for s in symbols:
                try:
                    self.gateway.cancel_all_orders(s)
                except Exception as e:
                    logger.error(f"[KillSwitch] cancel_all_orders({s}) failed: {e}")

        # ── 步骤2：[FIX-H] 将 OMS 置为 HALTED，硬阻断发单路径 ─
        # OMS.submit_order 首行：if self.state != LifecycleState.LIVE: return None
        # HALTED 状态下所有新订单在 OMS 层被拒绝，无需依赖 RiskManager 标志传递。
        if self.oms:
            try:
                self.oms.halt_system(f"KillSwitch: {reason}")
            except Exception as e:
                logger.error(f"[KillSwitch] oms.halt_system failed: {e}")

    def _log_warn(self, msg: str):
        self.engine.put(Event(EVENT_LOG, f"[Risk] {msg}"))