# file: oms/engine.py

from datetime import datetime
import uuid
import threading
import time
from infrastructure.logger import logger
from event.type import (
    Event, OrderIntent, OrderRequest, OrderStatus, 
    ExchangeOrderUpdate, CancelRequest, SystemState,
    EVENT_ORDER_UPDATE, EVENT_TRADE_UPDATE, EVENT_POSITION_UPDATE, 
    EVENT_ORDER_SUBMITTED, EVENT_SYSTEM_HEALTH, SystemHealthData, 
    OrderSubmitted, TradeData
)

from .order import Order
from .exposure import ExposureManager
from .validator import OrderValidator
from .account_manager import AccountManager
from .order_manager import OrderManager
from data.cache import data_cache

class OMS:
    def __init__(self, event_engine, gateway, config):
        self.event_engine = event_engine
        self.gateway = gateway
        self.config = config
        
        # --- 核心系统状态 ---
        # 初始设为 DIRTY，确保必须经过一次成功同步才能交易
        self.system_state = SystemState.DIRTY 
        
        self.orders = {}          
        self.exchange_id_map = {} 
        self.lock = threading.RLock()

        self.validator = OrderValidator(config)
        self.exposure = ExposureManager()
        self.order_monitor = OrderManager(event_engine, gateway, self.set_dirty)
        self.account = AccountManager(event_engine, self.exposure, config)
        
        self.total_submitted_count = 0
        self.total_filled_count = 0
        
        self.active = True
        self.reconcile_thread = threading.Thread(target=self._reconcile_loop, daemon=True)
        self.reconcile_thread.start()

    def set_dirty(self, reason: str):
        if self.system_state == SystemState.DIRTY: return
        logger.error(f"[OMS] SYSTEM STATE -> DIRTY. Reason: {reason}")
        self.system_state = SystemState.DIRTY

    def submit_order(self, intent: OrderIntent) -> str:
        # 硬闸门
        if self.system_state != SystemState.CLEAN:
            # 只有 CLEAN 状态允许下单
            logger.warn(f"OMS blocked: System is {self.system_state.value}")
            return None

        client_oid = str(uuid.uuid4())
        
        with self.lock:
            if not self.validator.validate_params(intent): return None
            notional = intent.price * intent.volume
            if not self.account.check_margin(notional): return None

            max_not = self.config["risk"]["limits"].get("max_pos_notional", 20000.0)
            ok, msg = self.exposure.check_risk(intent.symbol, intent.side, intent.volume, max_not)
            if not ok:
                logger.warn(f"OMS Reject: {msg}")
                return None

            order = Order(client_oid, intent)
            self.orders[client_oid] = order
            order.mark_submitting()
            self.exposure.update_open_orders(self.orders)
            self.account.calculate() 
            
        req = OrderRequest(
            symbol=intent.symbol, price=intent.price, volume=intent.volume,
            side=intent.side.value, order_type=intent.order_type,
            time_in_force=intent.time_in_force, post_only=intent.is_post_only,
            is_rpi=intent.is_rpi
        )
        
        exchange_oid = self.gateway.send_order(req)
        if exchange_oid:
            event_data = OrderSubmitted(req, exchange_oid, time.time())
            self.event_engine.put(Event(EVENT_ORDER_SUBMITTED, event_data))
            with self.lock:
                self.exchange_id_map[exchange_oid] = order
                self.total_submitted_count += 1
        else:
            with self.lock:
                order.mark_rejected("Gateway Error")
                self.exposure.update_open_orders(self.orders)
                self.account.calculate()
        return client_oid

    def on_order_submitted(self, event: Event):
        self.order_monitor.on_order_submitted(event)
        with self.lock:
            self.exposure.update_open_orders(self.orders)
            self.account.calculate()

    def on_exchange_update(self, event: Event):
        update: ExchangeOrderUpdate = event.data
        events_to_push = []
        delta_qty = 0.0
        
        with self.lock:
            order = self.orders.get(update.client_oid)
            if not order and update.exchange_oid:
                order = self.exchange_id_map.get(update.exchange_oid)
            
            if not order: return

            prev_status = order.status
            ex_status = update.status 
            
            if ex_status == "NEW":
                order.mark_new(update.exchange_oid)
                if update.exchange_oid: self.exchange_id_map[update.exchange_oid] = order
            elif ex_status in ["CANCELED", "EXPIRED"]:
                order.mark_cancelled()
            elif ex_status == "REJECTED":
                order.mark_rejected()
            elif ex_status in ["FILLED", "PARTIALLY_FILLED"]:
                delta_qty = update.cum_filled_qty - order.filled_volume
                if delta_qty > 1e-9:
                    self.total_filled_count += 1
                    order.add_fill(delta_qty, update.filled_price)
                    self.exposure.on_fill(order.intent.symbol, order.intent.side, delta_qty, update.filled_price)
                    fee = delta_qty * update.filled_price * self.config["backtest"]["taker_fee"]
                    self.account.update_balance(0, fee) 
                    
                    trade_data = TradeData(
                        symbol=order.intent.symbol, order_id=order.client_oid,
                        trade_id=str(int(time.time()*1000)), side=order.intent.side.value,
                        price=update.filled_price, volume=delta_qty, datetime=datetime.now()
                    )
                    events_to_push.append(Event(EVENT_TRADE_UPDATE, trade_data))

            if order.exchange_oid:
                self.order_monitor.on_order_update(order.exchange_oid, order.status)

            self.exposure.update_open_orders(self.orders)
            self.account.calculate()
            
            if order.status != prev_status or ex_status == "PARTIALLY_FILLED":
                snapshot = order.to_snapshot()
                events_to_push.append(Event(EVENT_ORDER_UPDATE, snapshot))
                if delta_qty > 1e-9:
                    pos_data = self.exposure.get_position_data(order.intent.symbol)
                    events_to_push.append(Event(EVENT_POSITION_UPDATE, pos_data))

        for evt in events_to_push: self.event_engine.put(evt)

    def cancel_order(self, client_oid: str):
        req_cancel = None
        with self.lock:
            order = self.orders.get(client_oid)
            if not order or not order.is_active(): return
            exch_oid = order.exchange_oid or client_oid
            req_cancel = CancelRequest(order.intent.symbol, exch_oid)
        if req_cancel: self.gateway.cancel_order(req_cancel)

    def cancel_all_orders(self, symbol: str):
        self.gateway.cancel_all_orders(symbol)
        with self.lock:
            for order in self.orders.values():
                if order.intent.symbol == symbol and order.is_active():
                    order.status = OrderStatus.CANCELLING

    # --- 核心修复：Dirty -> Syncing -> Clean ---

    def trigger_full_sync(self):
        """强制同步自愈"""
        if self.system_state == SystemState.SYNCING: return
        
        self.system_state = SystemState.SYNCING
        logger.warn("[OMS] STARTING FULL RECOVERY SYNC...")
        
        try:
            # 1. 清场
            for s in self.config["symbols"]:
                self.gateway.cancel_all_orders(s)
            time.sleep(1.5) 
            
            with self.lock:
                # 2. 重置本地内存
                self.orders.clear()
                self.exchange_id_map.clear()
                self.exposure.net_positions.clear()
                self.exposure.avg_prices.clear()
                self.exposure.open_buy_qty.clear()
                self.exposure.open_sell_qty.clear()
                self.order_monitor.monitored_orders.clear()
            
            # 3. 拉取真值
            self.sync_with_exchange()
            
            # 4. 转为 CLEAN
            self.system_state = SystemState.CLEAN
            logger.info("[OMS] RECOVERY SYNC SUCCESS. SYSTEM IS CLEAN.")
        except Exception as e:
            logger.error(f"[OMS] RECOVERY SYNC FAILED: {e}")
            self.system_state = SystemState.FROZEN

    def sync_with_exchange(self):
        """
        拉取交易所真值并填充本地内存。
        成功执行后应具有转为 CLEAN 的能力。
        """
        success = True
        # 1. 同步余额
        acc_data = self.gateway.get_account_info()
        if acc_data:
            tb = float(acc_data["totalWalletBalance"])
            tm = float(acc_data["totalInitialMargin"])
            self.account.force_sync(tb, tm)
        else:
            success = False
        
        # 2. 同步持仓
        pos_res = self.gateway.get_all_positions()
        if pos_res:
            with self.lock:
                # 先清空当前持仓防止重复
                self.exposure.net_positions.clear()
                self.exposure.avg_prices.clear()
                
                for item in pos_res:
                    amt = float(item["positionAmt"])
                    if amt != 0:
                        sym = item["symbol"]
                        prc = float(item["entryPrice"])
                        self.exposure.force_sync(sym, amt, prc)
                        pos_data = self.exposure.get_position_data(sym)
                        self.event_engine.put(Event(EVENT_POSITION_UPDATE, pos_data))
        else:
            success = False

        if success and self.system_state != SystemState.CLEAN:
            # [关键修复] 同步成功后，如果当前不是 CLEAN，自动转正
            self.system_state = SystemState.CLEAN
            logger.info("[OMS] Manual Sync Successful. System state -> CLEAN.")
        
        return success

    def _reconcile_loop(self):
        """审计对账循环"""
        while self.active:
            time.sleep(5)
            if self.system_state == SystemState.SYNCING: continue
            
            try:
                rem_pos = self.gateway.get_all_positions()
                rem_ords = self.gateway.get_open_orders()
                if rem_pos is None or rem_ords is None: continue

                is_mismatch = False
                pos_diffs = {}
                
                with self.lock:
                    rem_map = {p['symbol']: float(p['positionAmt']) for p in rem_pos if float(p['positionAmt']) != 0}
                    loc_map = {s: v for s, v in self.exposure.net_positions.items() if v != 0}
                    
                    all_syms = set(rem_map.keys()) | set(loc_map.keys())
                    for s in all_syms:
                        l, r = loc_map.get(s, 0.0), rem_map.get(s, 0.0)
                        if abs(l - r) > 1e-6:
                            pos_diffs[s] = (l, r, l - r)
                            is_mismatch = True
                    
                    loc_cnt = len([o for o in self.orders.values() if o.is_active()])
                    if loc_cnt != len(rem_ords): is_mismatch = True
                    
                    total_exp = sum(abs(v) * data_cache.get_mark_price(s) for s, v in loc_map.items())

                # [逻辑优化]
                if is_mismatch:
                    # 只有不匹配时才标记为 DIRTY 并触发全同步
                    if self.system_state == SystemState.CLEAN:
                        self.set_dirty("Audit Mismatch")
                    threading.Thread(target=self.trigger_full_sync, daemon=True).start()
                else:
                    # 如果匹配且当前是 DIRTY，说明已经通过某种方式恢复了，转为 CLEAN
                    if self.system_state == SystemState.DIRTY:
                        self.system_state = SystemState.CLEAN
                        logger.info("[OMS] Audit matches. System state -> CLEAN.")

                # 推送健康数据给 UI
                health = SystemHealthData(
                    state=self.system_state,
                    total_exposure=total_exp,
                    margin_ratio=self.account.used_margin / self.account.equity if self.account.equity else 0,
                    pos_diffs=pos_diffs,
                    order_count_local=loc_cnt,
                    order_count_remote=len(rem_ords),
                    is_sync_error=is_mismatch,
                    cancelling_count=0,
                    fill_ratio=self.total_filled_count / self.total_submitted_count if self.total_submitted_count > 0 else 0,
                    api_weight=0,
                    timestamp=time.time()
                )
                self.event_engine.put(Event(EVENT_SYSTEM_HEALTH, health))

            except Exception as e:
                logger.error(f"Audit Error: {e}")

    def stop(self):
        self.active = False
        self.order_monitor.stop()