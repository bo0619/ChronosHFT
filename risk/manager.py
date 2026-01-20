# file: risk/manager.py

import time
from event.type import OrderRequest, OrderData, Event, EVENT_LOG, EVENT_ORDER_UPDATE
from event.type import Status_ALLTRADED, Status_CANCELLED, Status_REJECTED

class RiskManager:
    def __init__(self, engine, config: dict, oms=None): # [修改] 注入 OMS
        self.engine = engine
        self.oms = oms # 持有 OMS 引用用于保证金检查
        
        risk_config = config.get("risk", {})
        self.max_order_volume = risk_config.get("max_order_volume", 1.0)
        self.max_active_orders = risk_config.get("max_active_orders", 20)
        self.max_orders_per_sec = risk_config.get("max_orders_per_sec", 50)
        
        self.active_order_count = 0
        self.order_history = []
        
        self.engine.register(EVENT_ORDER_UPDATE, self.on_order_update)

    def check_order(self, req: OrderRequest) -> bool:
        """
        预交易风控检查 (Pre-trade Risk Check)
        包含：硬限额、频率限制、资金/保证金检查
        """
        # 1. 基础硬限额检查
        if req.volume > self.max_order_volume:
            self.log(f"拒绝: 单笔 {req.volume} > 上限 {self.max_order_volume}")
            return False
            
        if self.active_order_count >= self.max_active_orders:
            self.log(f"拒绝: 订单数 {self.active_order_count} 超限")
            return False
            
        # 2. 频率限制
        now = time.time()
        self.order_history = [t for t in self.order_history if now - t < 1.0]
        if len(self.order_history) >= self.max_orders_per_sec:
            self.log("拒绝: 频率过高")
            return False
            
        # 3. [NEW] 保证金检查 (通过 OMS)
        if self.oms:
            notional = req.price * req.volume
            if not self.oms.check_risk(notional):
                # self.log(f"拒绝: 保证金不足 (Req: {notional:.2f})")
                return False

        # --- 通过检查 ---
        self.order_history.append(now)
        self.active_order_count += 1
        return True

    def on_order_update(self, event: Event):
        order: OrderData = event.data
        if order.status in [Status_ALLTRADED, Status_CANCELLED, Status_REJECTED]:
            self.active_order_count -= 1
            if self.active_order_count < 0: self.active_order_count = 0

    def log(self, msg):
        self.engine.put(Event(EVENT_LOG, f"[RiskManager] {msg}"))