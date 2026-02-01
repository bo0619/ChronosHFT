# file: sim_engine/gateway.py

import uuid
import random
from datetime import timedelta
from event.type import OrderRequest, CancelRequest, Event, EVENT_LOG

class ChaosGateway:
    """
    混沌仿真网关
    负责：生成 ID -> 计算延迟 -> 注入丢包/拒单 -> 调度 Exchange 事件
    """
    def __init__(self, sim_engine, exchange, clock, config, event_engine):
        self.sim_engine = sim_engine
        self.event_engine = event_engine
        self.exchange = exchange
        self.clock = clock
        
        # 延迟模型引用
        self.latency_model = getattr(sim_engine, 'latency_model', None)
        
        chaos = config.get("chaos", {})
        self.loss_rate = chaos.get("packet_loss_rate", 0.0)
        self.reject_rate = chaos.get("order_reject_rate", 0.0)

    def send_order(self, req: OrderRequest):
        # 1. 立即返回 ID (模拟 Async IO 提交成功)
        order_id = str(uuid.uuid4())[:8]
        
        # 2. [Chaos] 丢包 (只丢请求，不回包，让 OMS 掉单逻辑去处理)
        if random.random() < self.loss_rate:
            return order_id 

        # 3. [Chaos] 拒单 (模拟交易所返回 REJECTED)
        # 这里需要调度一个未来的回报事件，但为了简化，我们在 Exchange 内部处理 Reject 逻辑
        # 或者在这里直接回调。为了架构统一，我们还是发给 Exchange，让 Exchange 决定结果。
        # 这里暂不模拟 Reject，主要模拟延迟。

        # 4. 计算延迟并调度
        latency = self._get_latency()
        arrival_time = self.clock.now() + timedelta(seconds=latency)
        
        self.sim_engine.schedule(
            arrival_time,
            self.exchange.on_order_arrival,
            (req, order_id),
            priority=5
        )
        return order_id

    def cancel_order(self, req: CancelRequest):
        """处理撤单请求"""
        if random.random() < self.loss_rate:
            return 

        latency = self._get_latency()
        arrival_time = self.clock.now() + timedelta(seconds=latency)
        
        self.sim_engine.schedule(
            arrival_time,
            self.exchange.on_cancel_arrival,
            (req,),
            priority=5
        )

    def cancel_all_orders(self, symbol):
        # 仿真环境暂不支持原子级 CancelAll，需由策略逐个撤单
        pass

    def _get_latency(self):
        if self.latency_model:
            return self.latency_model.get_latency()
        return 0.02

    def log(self, msg): pass
    def connect(self, s): pass