# file: sim_engine/core.py

import heapq
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

@dataclass(order=True)
class SimEvent:
    timestamp: datetime
    priority: int 
    callback: Callable = field(compare=False)
    args: tuple = field(compare=False, default=())

class SimulationEngine:
    def __init__(self):
        self._queue = [] 
        self.is_running = False
        self.latency_model = None # 将在运行时注入

    def schedule(self, timestamp: datetime, callback: Callable, args=(), priority=10):
        heapq.heappush(self._queue, SimEvent(timestamp, priority, callback, args))

    def run(self, event_engine=None):
        """
        运行仿真
        :param event_engine: 传入 EventEngine 以开启同步驱动模式 (防止多线程竞争)
        """
        self.is_running = True
        while self._queue and self.is_running:
            # 1. 取出事件
            sim_event = heapq.heappop(self._queue)
            
            # 2. 执行回调 (如交易所撮合)
            sim_event.callback(*sim_event.args)
            
            # 3. 同步驱动：处理所有由此产生的业务逻辑事件 (Strategy/OMS)
            if event_engine:
                event_engine.process_existing_events()
            
    def stop(self):
        self.is_running = False