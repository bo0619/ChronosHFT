# file: oms/order_manager.py

import time
import threading
from infrastructure.logger import logger
from event.type import OrderSubmitted, Status_ALLTRADED, Status_CANCELLED, Status_REJECTED, OrderStatus

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
                "last_ack_time": 0,
                "status": "PENDING"
            }

    def on_order_update(self, order_id, status):
        with self.lock:
            if order_id in self.monitored_orders:
                if status in [OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED]:
                    del self.monitored_orders[order_id]
                else:
                    self.monitored_orders[order_id]["last_ack_time"] = time.time()

    def _check_loop(self):
        while self.active:
            time.sleep(1.0)
            now = time.time()
            timeout_detected = False
            
            with self.lock:
                for oid, info in list(self.monitored_orders.items()):
                    # 掉单检测
                    if info["last_ack_time"] == 0:
                        if now - info["submit_time"] > self.ACK_TIMEOUT:
                            logger.error(f"[OMS] ACK Timeout: {oid}. Market Data Gap or WS Down.")
                            timeout_detected = True
                            break 
            
            if timeout_detected and self.dirty_callback:
                self.dirty_callback("Order ACK Timeout")

    def stop(self):
        self.active = False