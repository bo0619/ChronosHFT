# file: event/engine.py

from queue import Queue, Empty
from threading import Thread
from collections import defaultdict
from .type import Event

class EventEngine:
    def __init__(self):
        self._queue = Queue()
        self._active = False
        self._thread = Thread(target=self._run)
        self._handlers = defaultdict(list)

    def start(self):
        """启动后台线程 (实盘模式)"""
        self._active = True
        self._thread.start()
        print(">>> [EventEngine] 核心引擎已启动 (Threaded Mode)")

    def stop(self):
        self._active = False
        if self._thread.is_alive():
            self._thread.join()
        print(">>> [EventEngine] 核心引擎已停止")

    def put(self, event: Event):
        self._queue.put(event)

    def register(self, type_: str, handler):
        self._handlers[type_].append(handler)

    def _run(self):
        """后台线程轮询"""
        while self._active:
            try:
                event = self._queue.get(timeout=1.0)
                self._process(event)
            except Empty:
                pass

    def _process(self, event: Event):
        if event.type in self._handlers:
            for handler in self._handlers[event.type]:
                try:
                    handler(event)
                except Exception as e:
                    print(f"[Error] 事件处理异常 {event.type}: {e}")

    # [NEW] 新增：同步处理方法 (回测专用)
    def process_existing_events(self):
        """
        处理当前队列中积压的所有事件，直到队列为空。
        用于回测时强制同步策略状态。
        """
        while not self._queue.empty():
            try:
                event = self._queue.get_nowait()
                self._process(event)
            except Empty:
                break