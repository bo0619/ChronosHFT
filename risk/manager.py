# file: risk/manager.py

import time
import math
import numpy as np
from collections import deque
from event.type import OrderRequest, OrderData, Event, EVENT_LOG, EVENT_ORDER_UPDATE, EVENT_MARK_PRICE, EVENT_ACCOUNT_UPDATE, EVENT_ORDERBOOK
from event.type import Status_ALLTRADED, Status_CANCELLED, Status_REJECTED
from infrastructure.logger import logger
from data.cache import data_cache # 需要引入数据缓存获取实时价格

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
        self.max_deviation_pct = sanity.get("max_deviation_pct", 0.05) # 5% 偏离限制
        
        tech = self.config.get("tech_health", {})
        self.max_latency_ms = tech.get("max_latency_ms", 1000)
        self.max_orders_per_sec = tech.get("max_order_count_per_sec", 20)
        
        # --- 运行时状态 ---
        self.order_history = deque() # 用于频率限制
        self.daily_pnl = 0.0
        self.initial_equity = 0.0 # 初始权益，用于计算Drawdown
        
        # 注册监听
        self.engine.register(EVENT_ORDER_UPDATE, self.on_order_update)
        self.engine.register(EVENT_MARK_PRICE, self.on_mark_price)
        self.engine.register(EVENT_ACCOUNT_UPDATE, self.on_account_update)
        self.engine.register(EVENT_ORDERBOOK, self.on_orderbook) # 用于延迟监控

    # ==========================
    # 1. 预交易风控 (Pre-Trade)
    # ==========================
    def check_order(self, req: OrderRequest) -> bool:
        """
        下单前的最后一道防线
        返回 True 表示通过，False 表示拦截
        """
        if self.kill_switch_triggered:
            self._log_warn(f"拦截下单: 系统已熔断 ({self.kill_reason})")
            return False

        if not self.active: return True

        # 1.1 频率限制 (Rate Limit)
        now = time.time()
        while self.order_history and self.order_history[0] < now - 1.0:
            self.order_history.popleft()
        if len(self.order_history) >= self.max_orders_per_sec:
            self._log_warn("拦截下单: 频率超限")
            return False
        
        # 1.2 单笔规模 (Size & Notional)
        if req.volume > self.max_order_qty:
            self._log_warn(f"拦截下单: 数量 {req.volume} > {self.max_order_qty}")
            return False
        
        notional = req.price * req.volume
        if notional > self.max_order_notional:
            self._log_warn(f"拦截下单: 金额 {notional:.2f} > {self.max_order_notional}")
            return False

        # 1.3 价格偏离 (Price Sanity)
        # 获取当前 MarkPrice 或 MidPrice
        mark_price = data_cache.get_mark_price(req.symbol)
        if mark_price > 0:
            deviation = abs(req.price - mark_price) / mark_price
            if deviation > self.max_deviation_pct:
                self._log_warn(f"拦截下单: 价格偏离 {deviation*100:.2f}% > {self.max_deviation_pct*100}%")
                return False

        # 1.4 自成交防范 (STP - Self Trade Prevention) (简易版)
        # 检查是否会吃掉自己的挂单
        if self.oms:
            # 这里的逻辑是：如果我要买，且我的买价 >= 我已有的卖单价 -> 拦截
            # 需要遍历 active_orders，比较耗时，HFT中需要优化数据结构(如维护自有的Bids/Asks)
            # 这里暂时略过，依赖交易所 STP 设置
            pass

        # 1.5 保证金与最大持仓 (Position Guard)
        if self.oms:
            # 预估成交后的总持仓价值
            current_pos = self.oms.position.positions.get(req.symbol)
            current_vol = current_pos.volume if current_pos else 0
            
            # 简单相加 (绝对值叠加，保守估计)
            new_notional = (abs(current_vol) + req.volume) * req.price
            if new_notional > self.max_pos_notional:
                self._log_warn(f"拦截下单: 预估持仓 {new_notional:.2f} > {self.max_pos_notional}")
                return False
            
            # 保证金检查
            if not self.oms.check_risk(notional):
                self._log_warn(f"拦截下单: 保证金不足")
                return False

        # --- 通过 ---
        self.order_history.append(now)
        return True

    # ==========================
    # 2. 盘中监控 (Real-time Monitoring)
    # ==========================
    def on_mark_price(self, event: Event):
        """
        监听标记价格：
        1. 检查黑天鹅 (Volatility Spike)
        2. 检查强平风险 (Liquidation Risk)
        """
        if self.kill_switch_triggered: return
        data = event.data
        
        # 2.1 黑天鹅探测
        # 这里简单用 价格/指数价格 偏离度，或者短时剧烈波动
        if abs(data.mark_price - data.index_price) / data.index_price > 0.05:
            self.trigger_kill_switch(f"黑天鹅: 现货/期货价差异常 {data.symbol}")

    def on_orderbook(self, event: Event):
        """
        监听行情：
        1. 检查网络延迟 (System Health)
        """
        if self.kill_switch_triggered: return
        ob = event.data
        
        # 7. 交易所/API健康度
        # 计算行情延迟: 本地接收时间 - 数据生成时间
        latency_ms = (time.time() - ob.datetime.timestamp()) * 1000
        if latency_ms > self.max_latency_ms:
            # 延迟过高，不一定马上Kill，可以先报警或暂停策略
            self._log_warn(f"高延迟警告: {latency_ms:.1f}ms")
            # 如果持续高延迟，可以触发熔断 (需计数器，此处略)

    def on_account_update(self, event: Event):
        """
        监听资产：
        1. 检查日内回撤
        2. 检查总亏损
        """
        if self.kill_switch_triggered: return
        acc = event.data
        
        if self.initial_equity == 0:
            self.initial_equity = acc.equity
            
        # 5. PnL / Drawdown 监控
        drawdown = self.initial_equity - acc.equity
        if drawdown > self.max_daily_loss:
            self.trigger_kill_switch(f"触及日内最大亏损: -{drawdown:.2f}")

    def on_order_update(self, event: Event):
        """
        监听订单：
        1. 统计拒单率 (API Health)
        """
        # 可以维护一个由 Rejected 触发的计数器，如果短时间过多则熔断
        pass

    # ==========================
    # 3. 熔断机制 (Kill Switch)
    # ==========================
    def trigger_kill_switch(self, reason: str):
        """
        红色按钮：立即停止一切
        """
        if self.kill_switch_triggered: return
        
        self.kill_switch_triggered = True
        self.kill_reason = reason
        logger.critical(f"🔥 KILL SWITCH TRIGGERED: {reason} 🔥")
        
        # 8. Kill Switch 动作
        # A. 停止策略发新单 (check_order 会拦截)
        
        # B. 撤销所有挂单
        if self.gateway:
            # 遍历所有 Symbol 撤单
            # 这里简化，假设 Config 知道所有 symbols
            # 最好是在 OMS 中维护 symbol 列表
            symbols = self.oms.position.positions.keys() if self.oms else []
            for s in symbols:
                self.gateway.cancel_all_orders(s)
                
        # C. (可选) 紧急平仓 / 冻结账户
        # 有些机构会选择 Close All，有些选择 Freeze。这里仅 Cancel All。

    def _log_warn(self, msg):
        self.engine.put(Event(EVENT_LOG, f"[Risk] {msg}"))