# file: sim_engine/core.py

import heapq
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Any

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

    def schedule(self, timestamp: datetime, callback: Callable, args=(), priority=10):
        heapq.heappush(self._queue, SimEvent(timestamp, priority, callback, args))

    def run(self, event_engine=None):
        """
        运行仿真
        :param event_engine: 如果传入，将在每个 SimEvent 后清空 EventEngine 的队列
        """
        self.is_running = True
        while self._queue and self.is_running:
            # 1. 取出下一个仿真事件 (例如：行情到达)
            sim_event = heapq.heappop(self._queue)
            
            # 2. 执行回调 (例如：更新 OrderBook -> 推送 Event)
            sim_event.callback(*sim_event.args)
            
            # 3. [关键修复] 立即驱动 EventEngine 消化所有衍生事件
            # 也就是：行情推给策略 -> 策略思考 -> 策略下达指令 -> 指令进入 Schedule
            # 这一系列动作必须在时间轴移动到下一个 SimEvent 之前完成
            if event_engine:
                event_engine.process_existing_events()
            
    def stop(self):
        self.is_running = False