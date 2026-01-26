# file: oms/order_manager.py

import time
import threading
from infrastructure.logger import logger
from event.type import OrderRequest, OrderData, CancelRequest, OrderSubmitted, OrderStatus
from .order import Order

class OrderManager:
    """
    [Updated] 订单生命周期管理器 - RPI 适配版
    """
    def __init__(self, engine, gateway):
        self.engine = engine
        self.gateway = gateway
        
        # 活跃订单缓存: order_id -> Order Object
        # 注意：这里我们改为直接引用 OMS 里的 Order 对象，
        # 或者仅维护一份轻量级的元数据。为了解耦，我们这里通过回调维护自己的清单。
        # 实际上 OMS Engine 已经持有了 Order 对象。
        # 为了不重复造轮子，OrderManager 主要负责“时间监控”。
        # 我们这里维护 {client_oid: {"submit_time": float, "last_ack": float, "is_rpi": bool}}
        self.monitored_orders = {}
        
        self.lock = threading.RLock()
        
        # 配置阈值
        self.ACK_TIMEOUT = 5.0       # 发单后多久没收到回报算“掉单”
        self.STALE_TIMEOUT = 60.0    # 普通单挂多久没成交算“陈旧”(可选风控)
        
        self.active = True
        self.check_thread = threading.Thread(target=self._check_loop, daemon=True)
        self.check_thread.start()

    def on_order_submitted(self, event):
        """
        监听 EVENT_ORDER_SUBMITTED
        """
        data: OrderSubmitted = event.data
        req = data.req
        order_id = data.order_id
        
        with self.lock:
            self.monitored_orders[order_id] = {
                "symbol": req.symbol,
                "submit_time": data.timestamp,
                "last_ack_time": 0, # 0 表示还没收到 NEW
                "status": "PENDING",
                "is_rpi": getattr(req, "is_rpi", False) # [NEW] 记录 RPI 属性
            }

    def on_order_update(self, order_id, status):
        """
        由 OMS Engine 调用，告知订单状态变化
        """
        with self.lock:
            if order_id not in self.monitored_orders:
                return

            info = self.monitored_orders[order_id]
            
            # 如果是终结状态，移除监控
            if status in [OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED]:
                del self.monitored_orders[order_id]
            else:
                # 更新活跃时间
                info["last_ack_time"] = time.time()
                info["status"] = "ACTIVE"

    def _check_loop(self):
        """僵尸订单检测循环"""
        while self.active:
            time.sleep(1.0)
            now = time.time()
            to_cancel = []
            
            with self.lock:
                for oid, info in self.monitored_orders.items():
                    # 1. 掉单检测 (ACK Timeout)
                    # 无论是 RPI 还是普通单，如果发出去 5 秒交易所没反应，都是异常
                    if info["last_ack_time"] == 0:
                        if now - info["submit_time"] > self.ACK_TIMEOUT:
                            logger.warn(f"[OMS] Order ACK Timeout: {oid} (RPI={info['is_rpi']})")
                            to_cancel.append((oid, info["symbol"]))
                            continue
                    
                    # 2. 长时挂单检测 (Stale Order) - [关键逻辑]
                    # 如果是 RPI 订单，跳过此检查！RPI 就是用来挂着的。
                    if info["is_rpi"]:
                        continue
                        
                    # 普通订单如果挂太久没动静，可能是程序死锁或逻辑遗漏，强制清理
                    # (这个功能是可选的，看你的策略风格，有些 Maker 策略也会挂很久)
                    # if now - info["last_ack_time"] > self.STALE_TIMEOUT:
                    #     logger.info(f"[OMS] Stale Normal Order Cleanup: {oid}")
                    #     to_cancel.append((oid, info["symbol"]))
            
            # 执行撤单
            for oid, symbol in to_cancel:
                try:
                    # 构造 CancelRequest
                    req = CancelRequest(symbol, oid)
                    self.gateway.cancel_order(req)
                    
                    # 更新时间防止在一秒内重复触发
                    with self.lock:
                        if oid in self.monitored_orders:
                            self.monitored_orders[oid]["submit_time"] = now 
                except:
                    pass

    def stop(self):
        self.active = False