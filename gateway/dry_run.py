# file: gateway/dry_run.py

import uuid
import time
import threading
from dataclasses import replace
from datetime import datetime

from event.type import Event, EVENT_ORDERBOOK, EVENT_ORDER_UPDATE, EVENT_TRADE_UPDATE
from event.type import OrderRequest, OrderData, TradeData, CancelRequest
# [修复] 移除旧的方向常量，引入 Side_BUY, Side_SELL
from event.type import Status_SUBMITTED, Status_ALLTRADED, Status_CANCELLED, Side_BUY, Side_SELL
from infrastructure.logger import logger

class DryRunGateway:
    """
    Dry Run 网关 (适配单向持仓版)
    职责：
    1. 接收策略的下单请求，保存在本地内存。
    2. 监听 EventEngine 的实时行情 (EVENT_ORDERBOOK)。
    3. 如果价格满足条件，生成虚拟成交事件。
    """
    def __init__(self, engine, config):
        self.engine = engine
        self.config = config.get("dry_run", {})
        
        # 模拟延迟 (毫秒)
        self.latency_ms = self.config.get("latency_ms", 20) / 1000.0
        
        # 本地维护的挂单簿: order_id -> {req, submit_time}
        self.active_orders = {}
        self.lock = threading.RLock()
        
        # 订阅实时行情用于撮合
        self.engine.register(EVENT_ORDERBOOK, self.on_market_data)
        
        logger.info(f"DryRun Gateway Initialized. Balance: {self.config.get('initial_balance')}")

    # --- 伪装成 BinanceGateway 的接口 ---

    def send_order(self, req: OrderRequest):
        """收到策略下单"""
        order_id = str(uuid.uuid4())[:8]
        
        # 模拟网络延迟 (异步处理)
        threading.Timer(self.latency_ms, self._simulate_order_arrival, args=(req, order_id)).start()
        
        logger.info(f"[DryRun] Order Sent: {req.symbol} {req.side} {req.price}")
        return order_id

    def cancel_order(self, req: CancelRequest):
        """收到策略撤单"""
        threading.Timer(self.latency_ms, self._simulate_order_cancel, args=(req,)).start()

    def cancel_all_orders(self, symbol):
        with self.lock:
            for oid in list(self.active_orders.keys()):
                req = self.active_orders[oid]["req"]
                if req.symbol == symbol:
                    self.cancel_order(CancelRequest(symbol, oid))

    # --- 内部仿真逻辑 ---

    def _simulate_order_arrival(self, req, order_id):
        """订单到达交易所（模拟）"""
        with self.lock:
            self.active_orders[order_id] = {
                "req": req,
                "id": order_id,
                "timestamp": time.time()
            }
        
        # 推送 SUBMITTED 状态
        order = OrderData(
            symbol=req.symbol, order_id=order_id, side=req.side,
            price=req.price, volume=req.volume, traded=0,
            status=Status_SUBMITTED, datetime=datetime.now()
        )
        self.engine.put(Event(EVENT_ORDER_UPDATE, order))

    def _simulate_order_cancel(self, req):
        """订单撤销到达（模拟）"""
        with self.lock:
            if req.order_id in self.active_orders:
                # 移除订单
                info = self.active_orders.pop(req.order_id)
                req_origin = info["req"]
                
                # 推送 CANCELLED 状态
                order = OrderData(
                    symbol=req.symbol, order_id=req.order_id, 
                    side=req_origin.side, price=req_origin.price, 
                    volume=req_origin.volume, traded=0,
                    status=Status_CANCELLED, datetime=datetime.now()
                )
                self.engine.put(Event(EVENT_ORDER_UPDATE, order))
                # logger.info(f"[DryRun] Order Cancelled: {req.order_id}")

    def on_market_data(self, event):
        """
        核心撮合逻辑：每当收到真实行情，检查是否成交
        """
        ob = event.data
        symbol = ob.symbol
        
        # 获取对手价
        best_bid = ob.get_best_bid()[0]
        best_ask = ob.get_best_ask()[0]
        
        if best_bid == 0 or best_ask == 0: return

        events_to_push = []
        orders_to_pop = []

        with self.lock:
            for oid, info in self.active_orders.items():
                req = info["req"]
                if req.symbol != symbol: continue
                
                matched = False
                
                # 撮合规则：穿价即成交
                # 买单 (Side_BUY)：如果 卖一价 <= 我的买价
                if req.side == Side_BUY:
                    if best_ask <= req.price:
                        matched = True
                
                # 卖单 (Side_SELL)：如果 买一价 >= 我的卖价
                elif req.side == Side_SELL:
                    if best_bid >= req.price:
                        matched = True

                if matched:
                    # 生成成交事件
                    trade = TradeData(
                        symbol=symbol, order_id=oid, trade_id=f"DRY-{uuid.uuid4().hex[:6]}",
                        side=req.side, price=req.price, volume=req.volume,
                        datetime=datetime.now()
                    )
                    events_to_push.append(Event(EVENT_TRADE_UPDATE, trade))
                    
                    # 生成订单结束事件
                    order = OrderData(
                        symbol=symbol, order_id=oid, side=req.side,
                        price=req.price, volume=req.volume, traded=req.volume,
                        status=Status_ALLTRADED, datetime=datetime.now()
                    )
                    events_to_push.append(Event(EVENT_ORDER_UPDATE, order))
                    
                    orders_to_pop.append(oid)

            # 移除已成交订单
            for oid in orders_to_pop:
                del self.active_orders[oid]

        # 推送事件 (在锁外推送，防止死锁)
        for e in events_to_push:
            self.engine.put(e)
            if e.type == EVENT_TRADE_UPDATE:
                t = e.data
                logger.info(f"[DryRun] Trade Filled: {t.symbol} {t.side} {t.price}")