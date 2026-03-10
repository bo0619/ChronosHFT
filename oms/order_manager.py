import threading
import time

from infrastructure.logger import logger
from event.type import OrderSubmitted, OrderStatus


class OrderManager:
    def __init__(self, engine, gateway, dirty_callback=None, monitor_config=None, start_thread=True):
        self.engine = engine
        self.gateway = gateway
        self.dirty_callback = dirty_callback

        self.monitored_orders = {}
        self.lock = threading.RLock()

        monitor_config = monitor_config or {}
        self.ACK_TIMEOUT = float(monitor_config.get("ack_timeout_sec", 5.0))
        self.ACK_TIMEOUT_RECHECK = float(monitor_config.get("ack_timeout_recheck_sec", 60.0))
        self.CHECK_INTERVAL = float(monitor_config.get("monitor_check_interval_sec", 1.0))

        self.active = True
        self.check_thread = None
        if start_thread:
            self.check_thread = threading.Thread(target=self._check_loop, daemon=True)
            self.check_thread.start()

    def on_order_submitted(self, event):
        data: OrderSubmitted = event.data
        with self.lock:
            self.monitored_orders[data.order_id] = {
                "symbol": data.req.symbol,
                "submit_time": data.timestamp,
                "last_ack_time": 0.0,
                "status": OrderStatus.PENDING_ACK,
                "ack_timeout_reported": False,
                "last_timeout_reported_at": 0.0,
            }

    def on_order_update(self, order_id, status):
        with self.lock:
            if order_id not in self.monitored_orders:
                return

            if status in {
                OrderStatus.FILLED,
                OrderStatus.CANCELLED,
                OrderStatus.REJECTED,
                OrderStatus.REJECTED_LOCALLY,
                OrderStatus.EXPIRED,
            }:
                del self.monitored_orders[order_id]
                return

            self.monitored_orders[order_id]["status"] = status
            if status in {
                OrderStatus.NEW,
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.CANCELLING,
            }:
                self.monitored_orders[order_id]["last_ack_time"] = time.time()
                self.monitored_orders[order_id]["ack_timeout_reported"] = False
                self.monitored_orders[order_id]["last_timeout_reported_at"] = 0.0

    def _check_once(self, now=None):
        now = time.time() if now is None else now
        suspicious_oid = None

        with self.lock:
            for oid, info in list(self.monitored_orders.items()):
                if info["last_ack_time"] != 0:
                    continue
                if now - info["submit_time"] <= self.ACK_TIMEOUT:
                    continue
                if info["ack_timeout_reported"]:
                    elapsed_since_report = now - info["last_timeout_reported_at"]
                    if elapsed_since_report < self.ACK_TIMEOUT_RECHECK:
                        continue

                logger.error(f"[OMS] ACK timeout: {oid}. User stream may be stale.")
                info["ack_timeout_reported"] = True
                info["last_timeout_reported_at"] = now
                suspicious_oid = oid
                break

        if suspicious_oid and self.dirty_callback:
            self.dirty_callback("Order ACK Timeout", suspicious_oid=suspicious_oid)

    def _check_loop(self):
        while self.active:
            time.sleep(self.CHECK_INTERVAL)
            self._check_once()

    def stop(self):
        self.active = False