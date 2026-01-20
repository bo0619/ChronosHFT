# file: oms/order_manager.py

import time
import threading
from infrastructure.logger import logger
from event.type import OrderRequest, OrderData, CancelRequest, OrderSubmitted # [NEW]
from event.type import Status_SUBMITTED, Status_ALLTRADED, Status_CANCELLED, Status_REJECTED

class OrderManager:
    def __init__(self, engine, gateway):
        self.engine = engine
        self.gateway = gateway
        
        self.active_orders = {}
        self.lock = threading.RLock()
        self.TIMEOUT_SECONDS = 5.0 
        
        self.active = True
        self.check_thread = threading.Thread(target=self._check_loop, daemon=True)
        self.check_thread.start()

    def on_order_submitted(self, event): # [修改] 接收 Event 对象
        """
        监听 EVENT_ORDER_SUBMITTED
        记录发出的订单，开始掉单计时
        """
        data: OrderSubmitted = event.data
        req = data.req
        order_id = data.order_id
        
        with self.lock:
            self.active_orders[order_id] = {
                "req": req,
                "submit_time": data.timestamp,
                "last_update_time": time.time(),
                "status": "PENDING_ACK",
                "filled": 0.0
            }
        # 刷新一下保证金计算（因为有新的 Pending Order 占用了额度）
        # 这里由 AccountManager 或者是 OMS Facade 来协调更佳，
        # 但 OrderManager 主要是记账，AccountManager 会在 calculate 时读取这里的数据。

    def on_order_update(self, order: OrderData):
        with self.lock:
            if order.order_id not in self.active_orders: return
            
            info = self.active_orders[order.order_id]
            info["last_update_time"] = time.time()
            info["status"] = order.status
            info["filled"] = order.traded
            
            if order.status in [Status_ALLTRADED, Status_CANCELLED, Status_REJECTED]:
                del self.active_orders[order.order_id]

    def get_open_orders_cost(self):
        total_cost = 0.0
        with self.lock:
            for oid, info in self.active_orders.items():
                req = info["req"]
                remaining_vol = req.volume - info.get("filled", 0.0)
                if remaining_vol > 0:
                    total_cost += remaining_vol * req.price
        return total_cost

    def _check_loop(self):
        while self.active:
            time.sleep(1.0)
            now = time.time()
            lost_orders = []
            
            with self.lock:
                for oid, info in self.active_orders.items():
                    if now - info["last_update_time"] > self.TIMEOUT_SECONDS:
                        lost_orders.append((oid, info))
            
            for oid, info in lost_orders:
                logger.warn(f"Order Timeout: ID={oid}")
                cancel_req = CancelRequest(info["req"].symbol, oid)
                self.gateway.cancel_order(cancel_req)
                
                with self.lock:
                    if oid in self.active_orders:
                        self.active_orders[oid]["last_update_time"] = now

    def stop(self):
        self.active = False