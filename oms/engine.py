# file: oms/engine.py

import uuid
import threading
import time
from datetime import datetime
from infrastructure.logger import logger
from event.type import Event, OrderIntent, OrderRequest, OrderStatus, ExchangeOrderUpdate, CancelRequest
from event.type import EVENT_ORDER_UPDATE, EVENT_TRADE_UPDATE, EVENT_POSITION_UPDATE, EVENT_ORDER_SUBMITTED, EVENT_SYSTEM_HEALTH
from event.type import OrderSubmitted, TradeData, SystemHealthData

# 引入子组件
from .order import Order
from .exposure import ExposureManager
from .validator import OrderValidator
from .account_manager import AccountManager
from .order_manager import OrderManager
from data.cache import data_cache

class OMS:
    """
    OMS Core Engine (Single Source of Truth)
    集成：状态机、资金风控、生命周期管理、健康对账
    """
    def __init__(self, event_engine, gateway, config):
        self.event_engine = event_engine
        self.gateway = gateway
        self.config = config
        
        # 核心数据：订单注册表
        self.orders = {}          # client_oid -> Order
        self.exchange_id_map = {} # exchange_oid -> Order
        
        self.lock = threading.RLock() # 递归锁

        # --- 子组件 ---
        self.validator = OrderValidator(config)
        self.exposure = ExposureManager()
        self.order_monitor = OrderManager(event_engine, gateway)
        self.account = AccountManager(event_engine, self.exposure, config)

        # --- [NEW] 统计计数器 (用于 Dashboard Fill Ratio) ---
        self.total_submitted_count = 0
        self.total_filled_count = 0

        # --- [NEW] 启动对账线程 ---
        self.active = True
        self.reconcile_thread = threading.Thread(target=self._reconcile_loop, daemon=True)
        self.reconcile_thread.start()

    def submit_order(self, intent: OrderIntent) -> str:
        """[下行] 策略发单入口"""
        client_oid = str(uuid.uuid4())
        
        with self.lock:
            # 1. 静态校验
            if not self.validator.validate_params(intent):
                logger.warn(f"OMS Reject: Invalid Params {intent}")
                return None
            
            # 2. 资金检查
            notional = intent.price * intent.volume
            if not self.account.check_margin(notional):
                return None

            # 3. 仓位限额检查
            max_notional = self.config["risk"]["limits"].get("max_pos_notional", 20000.0)
            ok, msg = self.exposure.check_risk(
                intent.symbol, intent.side, intent.volume, max_notional
            )
            if not ok:
                logger.warn(f"OMS Reject: Exposure Limit - {msg}")
                return None

            # 4. 创建订单
            order = Order(client_oid, intent)
            self.orders[client_oid] = order
            order.mark_submitting()
            
            # 5. 更新内部状态 (预占用)
            self.exposure.update_open_orders(self.orders)
            self.account.calculate() 
            
            # [统计] 增加提交计数
            self.total_submitted_count += 1
            
        # 6. 发送给网关
        from event.type import OrderRequest
        req = OrderRequest(
            symbol=intent.symbol, price=intent.price, volume=intent.volume,
            side=intent.side.value, order_type=intent.order_type,
            time_in_force=intent.time_in_force, post_only=intent.is_post_only,
            is_rpi=intent.is_rpi
        )
        
        exchange_oid = self.gateway.send_order(req)
        
        if exchange_oid:
            # 通知 OrderManager
            event_data = OrderSubmitted(req, exchange_oid, time.time())
            self.order_monitor.on_order_submitted(Event(EVENT_ORDER_SUBMITTED, event_data))
            
            with self.lock:
                self.exchange_id_map[exchange_oid] = order
        else:
            with self.lock:
                order.mark_rejected("Gateway Send Failed")
                self.exposure.update_open_orders(self.orders)
                self.account.calculate()
                
        return client_oid

    def cancel_order(self, client_oid: str):
        """[下行] 策略撤单入口"""
        req_cancel = None
        with self.lock:
            order = self.orders.get(client_oid)
            if not order or not order.is_active(): return
            
            exch_oid = order.exchange_oid or client_oid
            symbol = order.intent.symbol
            from event.type import CancelRequest
            req_cancel = CancelRequest(symbol, exch_oid)
            
        if req_cancel:
            self.gateway.cancel_order(req_cancel)

    def cancel_all_orders(self, symbol: str):
        """[下行] 一键清场"""
        # 1. 发送 API
        self.gateway.cancel_all_orders(symbol)
        
        # 2. 乐观更新状态 (防止UI滞后)
        with self.lock:
            for order in self.orders.values():
                if order.intent.symbol == symbol and order.is_active():
                    order.status = OrderStatus.CANCELLING

    # -----------------------------------------------------------
    # 处理交易所回报 (核心逻辑)
    # -----------------------------------------------------------
    def on_exchange_update(self, event):
        """
        [上行] Gateway 收到 WebSocket 消息后推送到这里
        """
        update: ExchangeOrderUpdate = event.data
        events_to_push = []
        
        # 在锁外初始化变量
        delta_qty = 0.0
        
        with self.lock:
            # 1. 寻找订单
            order = self.orders.get(update.client_oid)
            if not order and update.exchange_oid:
                order = self.exchange_id_map.get(update.exchange_oid)
            
            if not order: return

            # 2. 状态流转
            prev_status = order.status
            ex_status = update.status
            
            if ex_status == "NEW":
                order.mark_new(update.exchange_oid)
                if update.exchange_oid:
                    self.exchange_id_map[update.exchange_oid] = order
                    
            elif ex_status in ["CANCELED", "EXPIRED"]:
                order.mark_cancelled()
                
            elif ex_status == "REJECTED":
                order.mark_rejected()
                
            elif ex_status in ["FILLED", "PARTIALLY_FILLED"]:
                # 计算增量成交
                delta_qty = update.cum_filled_qty - order.filled_volume
                
                if delta_qty > 1e-9:
                    # [统计] 增加成交计数 (用于 Fill Ratio)
                    self.total_filled_count += 1
                    
                    # A. 更新订单
                    order.add_fill(delta_qty, update.filled_price)
                    
                    # B. 更新 Exposure
                    self.exposure.on_fill(
                        order.intent.symbol, order.intent.side, 
                        delta_qty, update.filled_price
                    )
                    
                    # C. 更新 Account
                    taker_fee = self.config["backtest"].get("taker_fee", 0.0005)
                    fee = delta_qty * update.filled_price * taker_fee
                    self.account.update_balance(0, fee) 
                    
                    # D. 生成 Trade 事件
                    trade_data = TradeData(
                        symbol=order.intent.symbol,
                        order_id=order.client_oid,
                        trade_id=str(int(time.time()*1000)), 
                        side=order.intent.side.value,
                        price=update.filled_price,
                        volume=delta_qty,
                        datetime=datetime.now()
                    )
                    events_to_push.append(Event(EVENT_TRADE_UPDATE, trade_data))

            # 3. 级联更新
            if order.exchange_oid:
                self.order_monitor.on_order_update(order.exchange_oid, order.status)

            self.exposure.update_open_orders(self.orders)
            self.account.calculate()
            
            # 4. 推送事件
            if order.status != prev_status or ex_status == "PARTIALLY_FILLED":
                snapshot = order.to_snapshot()
                events_to_push.append(Event(EVENT_ORDER_UPDATE, snapshot))
                
                if delta_qty > 0:
                    pos_data = self.exposure.get_position_data(order.intent.symbol)
                    events_to_push.append(Event(EVENT_POSITION_UPDATE, pos_data))

        for evt in events_to_push:
            self.event_engine.put(evt)

    # -----------------------------------------------------------
    # 对账循环 (Reconciliation) - 支撑 Dashboard 的核心
    # -----------------------------------------------------------
    def _reconcile_loop(self):
        """
        定期回答: 风险大不大? 系统撒谎没? 还能跑吗?
        """
        while self.active:
            time.sleep(5) # 每5秒对账一次
            
            try:
                # 1. IO 操作
                remote_pos_list = self.gateway.get_all_positions()
                remote_orders = self.gateway.get_open_orders()
                
                if remote_pos_list is None or remote_orders is None:
                    continue

                # 2. 内存计算 (加锁)
                pos_diffs = {}
                local_order_count = 0
                cancelling_count = 0
                total_exposure = 0.0
                
                with self.lock:
                    # A. 仓位对账
                    rem_map = {p['symbol']: float(p['positionAmt']) for p in remote_pos_list if float(p['positionAmt']) != 0}
                    loc_map = {s: v for s, v in self.exposure.net_positions.items() if v != 0}
                    
                    all_syms = set(rem_map.keys()) | set(loc_map.keys())
                    for s in all_syms:
                        loc = loc_map.get(s, 0.0)
                        rem = rem_map.get(s, 0.0)
                        if abs(loc - rem) > 1e-6:
                            pos_diffs[s] = (loc, rem, loc - rem)
                        
                        mp = data_cache.get_mark_price(s)
                        total_exposure += abs(loc) * mp

                    # B. 订单对账与健康检查
                    for o in self.orders.values():
                        if o.is_active():
                            local_order_count += 1
                            if o.status == OrderStatus.CANCELLING:
                                cancelling_count += 1
                
                # 3. 统计指标
                remote_order_count = len(remote_orders)
                is_sync_error = (len(pos_diffs) > 0) or (abs(local_order_count - remote_order_count) > 2) # 允许轻微误差
                
                fill_ratio = 0.0
                if self.total_submitted_count > 0:
                    fill_ratio = self.total_filled_count / self.total_submitted_count
                
                # 4. 推送健康报告
                health = SystemHealthData(
                    total_exposure=total_exposure,
                    margin_ratio=self.account.used_margin / self.account.equity if self.account.equity else 0,
                    pos_diffs=pos_diffs,
                    order_count_local=local_order_count,
                    order_count_remote=remote_order_count,
                    is_sync_error=is_sync_error,
                    cancelling_count=cancelling_count,
                    fill_ratio=fill_ratio,
                    api_weight=0,
                    timestamp=time.time()
                )
                self.event_engine.put(Event(EVENT_SYSTEM_HEALTH, health))
                
            except Exception as e:
                logger.error(f"Reconcile Error: {e}")

    def sync_with_exchange(self):
        """启动同步"""
        logger.info("OMS: Syncing with Exchange...")
        # 1. 清空
        self.exposure.net_positions.clear()
        self.exposure.avg_prices.clear()
        self.exposure.open_buy_qty.clear()
        self.exposure.open_sell_qty.clear()
        
        # 2. 资金
        acc = self.gateway.get_account_info()
        if acc:
            self.account.force_sync(float(acc["totalWalletBalance"]), float(acc["totalInitialMargin"]))
            
        # 3. 持仓
        pos = self.gateway.get_all_positions()
        if pos:
            for p in pos:
                amt = float(p["positionAmt"])
                if amt != 0:
                    self.exposure.force_sync(p["symbol"], amt, float(p["entryPrice"]))
                    self.event_engine.put(Event(EVENT_POSITION_UPDATE, self.exposure.get_position_data(p["symbol"])))
        
        logger.info("OMS: Sync Complete.")

    def stop(self):
        self.active = False
        self.order_monitor.stop()