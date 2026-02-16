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
        
        self.order_monitor = OrderManager(event_engine, gateway, self.halt_system)

    def bootstrap(self):
        """[Phase 1] 启动引导"""
        logger.info("OMS: Bootstrapping...")
        
        acc = self.gateway.get_account_info()
        pos = self.gateway.get_all_positions()
        
        if not acc or not pos:
            self.halt_system("Bootstrap API Error")
            return

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
        
        self._append_and_process(Event("eBootstrap", boot_event))
        
        self.state = LifecycleState.LIVE
        logger.info("OMS: System is LIVE.")

    def halt_system(self, reason: str):
        if self.state == LifecycleState.HALTED: return
        self.state = LifecycleState.HALTED
        logger.critical(f"🛑 OMS HALTED: {reason}")
        self.event_engine.put(Event(EVENT_SYSTEM_HEALTH, f"HALT:{reason}"))
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

            order = Order(client_oid, intent)
            self.orders[client_oid] = order
            order.mark_submitting()
            
            self.exposure.update_open_orders(self.orders)
            self.account.calculate()

        # IO: 发送
        from event.type import OrderRequest
        req = OrderRequest(
            symbol=intent.symbol, price=intent.price, volume=intent.volume,
            side=intent.side.value, order_type=intent.order_type,
            time_in_force=intent.time_in_force, post_only=intent.is_post_only,
            is_rpi=intent.is_rpi
        )
        
        # 传递 client_oid 供 Gateway 使用 (newClientOrderId)
        exchange_oid = self.gateway.send_order(req, client_oid)
        
        if exchange_oid:
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
            
            # 优先用 exchange_oid 撤，没有则用 client_oid
            target_id = order.exchange_oid if order.exchange_oid else client_oid
            req = CancelRequest(order.intent.symbol, target_id)
        
        self.gateway.cancel_order(req)

    def cancel_all_orders(self, symbol: str):
        self.gateway.cancel_all_orders(symbol)

    # -----------------------------------------------------------
    # 上行 (Exchange -> OMS)
    # -----------------------------------------------------------
    def on_exchange_update(self, event):
        self._append_and_process(event)

    def _append_and_process(self, event):
        if self.state == LifecycleState.HALTED: return

        if event.type == "eExchangeOrderUpdate":
            update: ExchangeOrderUpdate = event.data
            if not self.sequence.check(update.seq):
                self.halt_system(f"Seq Gap! Exp {self.sequence.last_seq+1} Got {update.seq}")
                return

        self.event_log.append(event)
        self._apply_event(event)

    def _apply_event(self, event):
        """
        纯状态更新逻辑
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

            # --- Case B: Exchange Update ---
            if event.type == "eExchangeOrderUpdate":
                update: ExchangeOrderUpdate = event.data
                
                # [Strict Lookup] 只认 ClientOID
                order = self.orders.get(update.client_oid)
                
                if not order:
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
                    delta = update.cum_filled_qty - order.filled_volume
                    if delta > 1e-9:
                        order.add_fill(delta, update.filled_price)
                        self.exposure.on_fill(order.intent.symbol, order.intent.side, delta, update.filled_price)
                        
                        fee = delta * update.filled_price * self.config["backtest"]["taker_fee"]
                        self.account.update_balance(0, fee)
                        
                        trade_data = TradeData(
                            symbol=order.intent.symbol, order_id=order.client_oid,
                            trade_id=f"T{int(update.update_time*1000)}", 
                            side=order.intent.side.value, price=update.filled_price, 
                            volume=delta, datetime=datetime.now()
                        )
                        self.event_engine.put(Event(EVENT_TRADE_UPDATE, trade_data))

                # [修复点] 级联更新 OrderMonitor
                # 必须使用 order.client_oid (UUID)，因为 OrderMonitor 是按这个索引的
                self.order_monitor.on_order_update(order.client_oid, order.status)

                self.exposure.update_open_orders(self.orders)
                self.account.calculate()
                
                if order.status != prev_status or update.status == "PARTIALLY_FILLED":
                    self.event_engine.put(Event(EVENT_ORDER_UPDATE, order.to_snapshot()))
                    if update.status in ["FILLED", "PARTIALLY_FILLED"]:
                        pos_data = self.exposure.get_position_data(order.intent.symbol)
                        self.event_engine.put(Event(EVENT_POSITION_UPDATE, pos_data))

    def rebuild_from_log(self):
        logger.info("OMS: Rebuilding from EventLog...")
        self.orders.clear()
        self.exposure = ExposureManager()
        self.account = AccountManager(self.event_engine, self.exposure, self.config)
        self.sequence.reset()
        
        for evt in self.event_log:
            self._apply_event(evt)
            
        logger.info(f"OMS: Rebuild done. {len(self.orders)} orders restored.")

    def stop(self):
        self.order_monitor.stop()