# file: oms/engine.py

import uuid
import threading
import time
from datetime import datetime
from infrastructure.logger import logger
from event.type import Event, OrderIntent, OrderRequest, OrderStatus, ExchangeOrderUpdate, CancelRequest
from event.type import EVENT_ORDER_UPDATE, EVENT_TRADE_UPDATE, EVENT_POSITION_UPDATE, EVENT_ORDER_SUBMITTED, EVENT_SYSTEM_HEALTH
from event.type import OrderSubmitted, TradeData, LifecycleState, BootstrapEvent, Side

from .order import Order
from .exposure import ExposureManager
from .validator import OrderValidator
from .account_manager import AccountManager
from .order_manager import OrderManager 
from .sequence import SequenceValidator

class OMS:
    """
    [Core] Deterministic OMS
    Architecture:
    1. Input: Monotonic Event Stream (Updates) + Strategy Intents
    2. Process: Validate -> Log -> Apply -> Output
    3. State: Rebuildable from Log
    """
    def __init__(self, event_engine, gateway, config):
        self.event_engine = event_engine
        self.gateway = gateway
        self.config = config
        
        self.state = LifecycleState.BOOTSTRAP
        
        # [Immutable History]
        self.event_log = [] 
        
        # [State]
        self.orders = {} # client_oid -> Order
        
        self.lock = threading.RLock()

        # [Components]
        self.sequence = SequenceValidator()
        self.validator = OrderValidator(config)
        self.exposure = ExposureManager()
        self.account = AccountManager(event_engine, self.exposure, config)
        
        # OrderMonitor 负责超时检测，触发 Halt
        self.order_monitor = OrderManager(event_engine, gateway, self.halt_system)

    def bootstrap(self):
        """
        [Phase 1] 启动引导：构建初始状态事件
        """
        logger.info("OMS: Bootstrapping...")
        
        # IO: 拉取快照
        acc = self.gateway.get_account_info()
        pos = self.gateway.get_all_positions()
        
        if not acc or not pos:
            self.halt_system("Bootstrap API Error")
            return

        # 构造 Bootstrap Event
        pos_list = []
        for p in pos:
            amt = float(p["positionAmt"])
            if amt != 0:
                pos_list.append((p["symbol"], amt, float(p["entryPrice"])))

        boot_event = BootstrapEvent(
            timestamp=time.time(),
            balance=float(acc["totalWalletBalance"]),
            used_margin=float(acc["totalInitialMargin"]),
            positions=pos_list
        )
        
        # Apply
        self._append_and_process(Event("eBootstrap", boot_event))
        
        self.state = LifecycleState.LIVE
        logger.info("OMS: System is LIVE.")

    def halt_system(self, reason: str):
        """
        [Panic] 遇到不可恢复错误
        """
        if self.state == LifecycleState.HALTED: return
        
        self.state = LifecycleState.HALTED
        logger.critical(f"🛑 OMS HALTED: {reason}")
        self.event_engine.put(Event(EVENT_SYSTEM_HEALTH, f"HALT:{reason}"))
        
        # 尝试紧急撤单
        try:
            for s in self.config["symbols"]: self.gateway.cancel_all_orders(s)
        except: pass

    # -----------------------------------------------------------
    # 下行 (Strategy -> OMS)
    # -----------------------------------------------------------
    def submit_order(self, intent: OrderIntent) -> str:
        if self.state != LifecycleState.LIVE: return None

        client_oid = str(uuid.uuid4())
        
        with self.lock:
            if not self.validator.validate_params(intent): return None
            
            notional = intent.price * intent.volume
            if not self.account.check_margin(notional): return None
            
            ok, msg = self.exposure.check_risk(intent.symbol, intent.side, intent.volume, 20000.0)
            if not ok: return None

            # [State Mutation] 本地创建订单
            # 在更严格的 Event Sourcing 中，这里应该生成 OrderCreatedEvent 并 apply
            # 但为了性能，Submit 路径保持同步函数调用，Update 路径保持 Event Sourcing
            order = Order(client_oid, intent)
            self.orders[client_oid] = order
            order.mark_submitting()
            
            self.exposure.update_open_orders(self.orders)
            self.account.calculate()

        # IO: 发送 (注入 client_oid)
        from event.type import OrderRequest
        req = OrderRequest(
            symbol=intent.symbol, price=intent.price, volume=intent.volume,
            side=intent.side.value, order_type=intent.order_type,
            time_in_force=intent.time_in_force, post_only=intent.is_post_only,
            is_rpi=intent.is_rpi
        )
        
        # 传递 client_oid，让 Gateway 填入 newClientOrderId
        exchange_oid = self.gateway.send_order(req, client_oid)
        
        if exchange_oid:
            # 记录到监控
            from event.type import OrderSubmitted
            event_data = OrderSubmitted(req, client_oid, time.time())
            self.order_monitor.on_order_submitted(Event(EVENT_ORDER_SUBMITTED, event_data))
        else:
            with self.lock:
                order.mark_rejected("Gateway Error")
                self.exposure.update_open_orders(self.orders)
                self.account.calculate()

        return client_oid

    def cancel_order(self, client_oid: str):
        with self.lock:
            order = self.orders.get(client_oid)
            if not order or not order.is_active(): return
            
            # 优先用 exchange_oid 撤，没有则用 client_oid (Gateway 会处理)
            target_id = order.exchange_oid if order.exchange_oid else client_oid
            req = CancelRequest(order.intent.symbol, target_id)
        
        self.gateway.cancel_order(req)

    def cancel_all_orders(self, symbol: str):
        self.gateway.cancel_all_orders(symbol)
        # 不主动修改状态，等待 CANCELED 回报

    # -----------------------------------------------------------
    # 上行 (Exchange -> OMS) - The Deterministic Path
    # -----------------------------------------------------------
    def on_exchange_update(self, event):
        """
        入口：所有外部状态变更必须走这里
        """
        self._append_and_process(event)

    def _append_and_process(self, event):
        if self.state == LifecycleState.HALTED: return

        # 1. 序列检查
        if event.type == "eExchangeOrderUpdate":
            update: ExchangeOrderUpdate = event.data
            if not self.sequence.check(update.seq):
                self.halt_system(f"Seq Gap! Exp {self.sequence.last_seq+1} Got {update.seq}")
                return

        # 2. 持久化
        self.event_log.append(event)
        
        # 3. 应用状态
        self._apply_event(event)

    def _apply_event(self, event):
        """
        纯内存状态更新，无 IO，无随机性
        """
        with self.lock:
            
            # --- Case A: Bootstrap ---
            if event.type == "eBootstrap":
                data: BootstrapEvent = event.data
                self.account.force_sync(data.balance, data.used_margin)
                self.exposure.net_positions.clear()
                self.exposure.avg_prices.clear()
                for sym, vol, price in data.positions:
                    self.exposure.force_sync(sym, vol, price)
                self.account.calculate()
                return

            # --- Case B: Order Update ---
            if event.type == "eExchangeOrderUpdate":
                update: ExchangeOrderUpdate = event.data
                
                # [Strict Lookup] 只认 ClientOID
                order = self.orders.get(update.client_oid)
                
                if not order:
                    # 如果 client_oid 为空（可能是被动强平单？），这里记录 Critical
                    # 但不一定要 Halt，这属于"非受控订单"
                    logger.warn(f"Unknown Order Update: {update.client_oid} / {update.exchange_oid}")
                    return

                prev_status = order.status
                
                # 状态机
                if update.status == "NEW":
                    order.mark_new(update.exchange_oid)
                elif update.status in ["CANCELED", "EXPIRED"]:
                    order.mark_cancelled()
                elif update.status == "REJECTED":
                    order.mark_rejected()
                elif update.status in ["FILLED", "PARTIALLY_FILLED"]:
                    # 增量成交计算
                    delta = update.cum_filled_qty - order.filled_volume
                    if delta > 1e-9:
                        order.add_fill(delta, update.filled_price)
                        self.exposure.on_fill(order.intent.symbol, order.intent.side, delta, update.filled_price)
                        
                        # 资金更新
                        fee = delta * update.filled_price * self.config["backtest"]["taker_fee"]
                        self.account.update_balance(0, fee)
                        
                        # 输出 Trade Event
                        trade_data = TradeData(
                            symbol=order.intent.symbol, order_id=order.client_oid,
                            trade_id=f"T{int(update.update_time*1000)}", 
                            side=order.intent.side.value, price=update.filled_price, 
                            volume=delta, datetime=datetime.now()
                        )
                        self.event_engine.put(Event(EVENT_TRADE_UPDATE, trade_data))

                # 刷新衍生状态
                if order.exchange_oid:
                    self.order_monitor.on_order_update(order.exchange_oid, order.status)
                self.exposure.update_open_orders(self.orders)
                self.account.calculate()
                
                # 输出 Order Event
                if order.status != prev_status or update.status == "PARTIALLY_FILLED":
                    self.event_engine.put(Event(EVENT_ORDER_UPDATE, order.to_snapshot()))
                    if update.status in ["FILLED", "PARTIALLY_FILLED"]:
                        pos_data = self.exposure.get_position_data(order.intent.symbol)
                        self.event_engine.put(Event(EVENT_POSITION_UPDATE, pos_data))

    def rebuild_from_log(self):
        """
        [Replay] 灾难恢复
        """
        logger.info("OMS: Rebuilding from EventLog...")
        # Reset State
        self.orders.clear()
        self.exposure = ExposureManager()
        self.account = AccountManager(self.event_engine, self.exposure, self.config)
        self.sequence.reset()
        
        # Replay
        for evt in self.event_log:
            self._apply_event(evt)
            
        logger.info(f"OMS: Rebuild done. {len(self.orders)} orders restored.")

    def stop(self):
        self.order_monitor.stop()