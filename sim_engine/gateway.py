# file: sim_engine/gateway.py

import uuid
import random
from datetime import timedelta

# [修复] 确保导入了 OrderData 和 Status_REJECTED
from event.type import OrderRequest, OrderData, Event, EVENT_LOG, EVENT_ORDER_UPDATE, Status_REJECTED, CancelRequest

class SimGateway:
    """
    基础仿真网关 (Step 8 版本)
    仅模拟延迟，无故障注入。
    """
    def __init__(self, sim_engine, exchange, clock, config, event_engine=None):
        self.sim_engine = sim_engine
        self.exchange = exchange
        self.clock = clock
        # 兼容旧接口
        
        bt_conf = config["backtest"]
        self.lat_mean = bt_conf.get("latency_ms_mean", 20) / 1000.0
        self.lat_std = bt_conf.get("latency_ms_std", 5) / 1000.0

    def send_order(self, req: OrderRequest):
        order_id = str(uuid.uuid4())[:8]
        
        # 1. 计算随机延迟
        latency = max(0, random.gauss(self.lat_mean, self.lat_std))
        arrival_time = self.clock.now() + timedelta(seconds=latency)
        
        # 2. 注册事件
        self.sim_engine.schedule(
            arrival_time,
            self.exchange.on_order_arrival,
            (req, order_id),
            priority=5
        )
        return order_id

    def log(self, msg): pass
    def connect(self, s): pass


class ChaosGateway:
    """
    混沌仿真网关 (Step 9 版本)
    包含：
    1. 延迟模型 (引用 Engine 中的高级模型)
    2. 丢包模拟 (Packet Loss)
    3. 拒单模拟 (Order Rejection)
    """
    def __init__(self, sim_engine, exchange, clock, config, event_engine):
        self.sim_engine = sim_engine
        self.event_engine = event_engine
        self.exchange = exchange
        self.clock = clock
        
        # 引用 Engine 中初始化的 Log-Normal 模型
        # 如果 sim_engine 没有 latency_model (比如 Step 8 代码混用)，则回退到基础计算
        self.latency_model = getattr(sim_engine, 'latency_model', None)
        
        chaos = config.get("chaos", {})
        self.loss_rate = chaos.get("packet_loss_rate", 0.0)
        self.reject_rate = chaos.get("order_reject_rate", 0.0)

    def send_order(self, req: OrderRequest):
        order_id = str(uuid.uuid4())[:8]
        
        # 1. [Chaos] 模拟丢包 (Packet Loss)
        if random.random() < self.loss_rate:
            return order_id 

        # 2. [Chaos] 模拟交易所拒单 (Order Rejected)
        if random.random() < self.reject_rate:
            # 获取延迟
            latency = self._get_latency() / 2 # 拒单通常很快
            reject_time = self.clock.now() + timedelta(seconds=latency)
            
            def reject_callback():
                # [修复] 这里需要 OrderData 类
                o = OrderData(
                    req.symbol, order_id, req.direction, req.action, 
                    req.price, req.volume, 0, Status_REJECTED, self.clock.now()
                )
                self.event_engine.put(Event(EVENT_ORDER_UPDATE, o))
            
            self.sim_engine.schedule(reject_time, reject_callback, priority=5)
            return order_id

        # 3. 正常路径
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
        """
        处理撤单请求
        """
        # 1. [Chaos] 模拟丢包 (撤单指令丢失)
        if random.random() < self.loss_rate:
            return 

        # 2. 计算延迟 (撤单和下单走一样的网络路径)
        latency = self._get_latency()
        arrival_time = self.clock.now() + timedelta(seconds=latency)
        
        # 3. 注册撤单到达事件
        self.sim_engine.schedule(
            arrival_time,
            self.exchange.on_cancel_arrival, # 交易所处理撤单
            (req,),
            priority=5
        )

    def cancel_all_orders(self, symbol):
        # 仿真环境简化处理：遍历策略记录的活跃订单逐个撤单
        # 真实的 CancelAll 也是有延迟的，这里暂不模拟原子级的 CancelAll
        pass

    def _get_latency(self):
        if self.latency_model:
            return self.latency_model.get_latency()
        else:
            return 0.02 # Fallback

    def log(self, msg): pass
    def connect(self, s): pass