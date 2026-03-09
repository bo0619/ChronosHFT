import threading
import time

from infrastructure.logger import logger
from event.type import OrderSubmitted, OrderStatus


class OrderManager:
    def __init__(self, engine, gateway, dirty_callback=None):
        self.engine = engine
        self.gateway = gateway
        self.dirty_callback = dirty_callback

        self.monitored_orders = {}
        self.lock = threading.RLock()

        self.ACK_TIMEOUT = 5.0

        self.active = True
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

    def _check_loop(self):
        while self.active:
            time.sleep(1.0)
            now = time.time()
            timeout_detected = False

            with self.lock:
                for oid, info in list(self.monitored_orders.items()):
                    if info["last_ack_time"] == 0 and now - info["submit_time"] > self.ACK_TIMEOUT:
                        logger.error(f"[OMS] ACK timeout: {oid}. User stream may be stale.")
                        timeout_detected = True
                        break

            if timeout_detected and self.dirty_callback:
                self.dirty_callback("Order ACK Timeout")

    def stop(self):
        self.active = False
