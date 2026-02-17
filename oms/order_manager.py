# file: oms/order_manager.py

import time
import threading
from infrastructure.logger import logger
from event.type import OrderRequest, OrderData, CancelRequest, OrderSubmitted, OrderStatus
# 注意：这里不需要 import Order，因为我们不直接操作 Order 对象，只操作 metadata

class OrderManager:
    def __init__(self, engine, gateway, dirty_callback=None):
        self.engine = engine
        self.gateway = gateway
        self.dirty_callback = dirty_callback
        
        # key: client_oid (UUID), value: dict
        self.monitored_orders = {}
        self.lock = threading.RLock()
        
        self.ACK_TIMEOUT = 5.0
        
        self.active = True
        self.check_thread = threading.Thread(target=self._check_loop, daemon=True)
        self.check_thread.start()

    def on_order_submitted(self, event):
        """监听 EVENT_ORDER_SUBMITTED"""
        data: OrderSubmitted = event.data
        req = data.req
        order_id = data.order_id # 这是 client_oid (UUID)
        
        with self.lock:
            self.monitored_orders[order_id] = {
                "symbol": req.symbol,
                "submit_time": data.timestamp,
                "last_ack_time": 0, # 0 表示还没收到 NEW
                "status": "PENDING",
                "is_rpi": getattr(req, "is_rpi", False)
            }

    def on_order_update(self, order_id, status): # order_id 必须是 UUID
        """由 OMS Engine 调用"""
        with self.lock:
            if order_id not in self.monitored_orders:
                return

            info = self.monitored_orders[order_id]
            
            # 终结状态：移除监控
            if status in [OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED]:
                del self.monitored_orders[order_id]
            else:
                # 活跃状态：更新心跳
                info["last_ack_time"] = time.time()
                info["status"] = "ACTIVE"

    def _check_loop(self):
        while self.active:
            time.sleep(1.0)
            now = time.time()
            timeout_detected = False
            
            with self.lock:
                # 复制 items 以便在循环中安全删除
                for oid, info in list(self.monitored_orders.items()):
                    # 1. 掉单检测 (ACK Timeout)
                    # 无论是 RPI 还是普通单，发出去 5 秒没动静都是异常
                    if info["last_ack_time"] == 0:
                        if now - info["submit_time"] > self.ACK_TIMEOUT:
                            logger.error(f"[OMS] ACK Timeout: {oid} (RPI={info['is_rpi']}). WS Gap or Order Lost.")
                            timeout_detected = True
                            break 
                    
                    # 2. 长时挂单检测 (RPI 豁免)
                    # if not info["is_rpi"] and ... (可选逻辑)
            
            if timeout_detected and self.dirty_callback:
                self.dirty_callback(f"Order ACK Timeout {oid}")

    def stop(self):
        self.active = False