from collections import defaultdict
from queue import Empty, Queue
from threading import Thread

from infrastructure.logger import logger


class EventEngine:
    def __init__(self):
        self._hot_queue = Queue()
        self._cold_queue = Queue()
        self._active = False
        self._hot_thread = Thread(target=self._run_hot, daemon=True)
        self._cold_thread = Thread(target=self._run_cold, daemon=True)
        self._hot_handlers = defaultdict(list)
        self._cold_handlers = defaultdict(list)

    def start(self):
        self._active = True
        if not self._hot_thread.is_alive():
            self._hot_thread = Thread(target=self._run_hot, daemon=True)
            self._hot_thread.start()
        if not self._cold_thread.is_alive():
            self._cold_thread = Thread(target=self._run_cold, daemon=True)
            self._cold_thread.start()
        logger.info(">>> [EventEngine] started with split hot/cold lanes")

    def stop(self):
        self._active = False
        if self._hot_thread.is_alive():
            self._hot_thread.join()
        if self._cold_thread.is_alive():
            self._cold_thread.join()
        logger.info(">>> [EventEngine] stopped")

    def put(self, event):
        if event.type in self._hot_handlers:
            self._hot_queue.put(event)
            return
        if event.type in self._cold_handlers:
            self._cold_queue.put(event)

    def register(self, type_, handler):
        self.register_cold(type_, handler)

    def register_hot(self, type_, handler):
        self._hot_handlers[type_].append(handler)

    def register_cold(self, type_, handler):
        self._cold_handlers[type_].append(handler)

    def get_queue_snapshot(self):
        return {
            "hot_depth": self._hot_queue.qsize(),
            "cold_depth": self._cold_queue.qsize(),
        }

    def _run_hot(self):
        while self._active:
            try:
                event = self._hot_queue.get(timeout=1.0)
                self._process_hot(event)
                if event.type in self._cold_handlers:
                    self._cold_queue.put(event)
            except Empty:
                pass

    def _run_cold(self):
        while self._active:
            try:
                event = self._cold_queue.get(timeout=1.0)
                self._process_cold(event)
            except Empty:
                pass

    def _process_hot(self, event):
        self._dispatch(self._hot_handlers.get(event.type, ()), event, lane="hot")

    def _process_cold(self, event):
        self._dispatch(self._cold_handlers.get(event.type, ()), event, lane="cold")

    def _dispatch(self, handlers, event, lane: str):
        for handler in handlers:
            try:
                handler(event)
            except Exception as exc:
                logger.error(f"[EventEngine:{lane}] handler failed {event.type}: {exc}")

    def process_existing_events(self):
        while not self._hot_queue.empty():
            try:
                event = self._hot_queue.get_nowait()
                self._process_hot(event)
                if event.type in self._cold_handlers:
                    self._cold_queue.put(event)
            except Empty:
                break

        while not self._cold_queue.empty():
            try:
                event = self._cold_queue.get_nowait()
                self._process_cold(event)
            except Empty:
                break
