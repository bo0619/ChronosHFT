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
    Deterministic & Self-Healing OMS Engine
    """
    def __init__(self, event_engine, gateway, config):
        self.event_engine = event_engine
        self.gateway = gateway
        self.config = config
        
        self.state = LifecycleState.BOOTSTRAP
        
        # [Immutable History]
        self.event_log = [] 
        
        # [State]
        self.orders = {}             # client_oid -> Order
        self.exchange_id_map = {}    # [修复] 辅助索引：exchange_oid -> Order
        
        self.lock = threading.RLock()

        # [Components]
        self.sequence = SequenceValidator()
        self.validator = OrderValidator(config)
        self.exposure = ExposureManager()
        self.account = AccountManager(event_engine, self.exposure, config)
        
        # OrderMonitor 负责超时检测，触发 RECONCILING
        self.order_monitor = OrderManager(event_engine, gateway, self.trigger_reconcile)

    def bootstrap(self):
        """[Phase 1] 启动引导"""
        logger.info("OMS: Bootstrapping State...")
        self._perform_full_reset()
        
    def halt_system(self, reason: str):
        if self.state == LifecycleState.HALTED: return
        self.state = LifecycleState.HALTED
        logger.critical(f"🛑 OMS HALTED: {reason}")
        self.event_engine.put(Event(EVENT_SYSTEM_HEALTH, f"HALT:{reason}"))
        try:
            for s in self.config["symbols"]: self.gateway.cancel_all_orders(s)
        except: pass

    # -----------------------------------------------------------
    # 自愈逻辑 (Self-Healing)
    # -----------------------------------------------------------
    def trigger_reconcile(self, reason: str, suspicious_oid: str = None):
        """触发对账流程，暂时冻结交易"""
        if self.state in [LifecycleState.RECONCILING, LifecycleState.HALTED]:
            return

        logger.warning(f"⚠️  OMS Dirty: {reason}. State -> RECONCILING.")
        self.state = LifecycleState.RECONCILING
        
        # 异步执行对账，不阻塞事件线程
        threading.Thread(target=self._execute_reconcile, args=(suspicious_oid,), daemon=True).start()

    def _execute_reconcile(self, suspicious_oid: str):
        """对账核心逻辑：判断 A/B/C 三种不一致情况"""
        logger.info("[Reconcile] Investigating inconsistency...")
        try:
            # 1. 获取真值
            rem_pos = self.gateway.get_all_positions()
            rem_ords = self.gateway.get_open_orders()
            
            if rem_pos is None or rem_ords is None:
                self.halt_system("Reconcile API unreachable")
                return

            # --- A. 检查持仓是否真的不平 ---
            is_pos_mismatch = False
            with self.lock:
                rem_map = {p['symbol']: float(p['positionAmt']) for p in rem_pos if float(p['positionAmt']) != 0}
                loc_map = {s: v for s, v in self.exposure.net_positions.items() if v != 0}
                
                all_syms = set(rem_map.keys()) | set(loc_map.keys())
                for s in all_syms:
                    if abs(loc_map.get(s, 0) - rem_map.get(s, 0)) > 1e-6:
                        is_pos_mismatch = True
                        logger.error(f"[Reconcile] Position Mismatch {s}: Local={loc_map.get(s,0)}, Exch={rem_map.get(s,0)}")
                        break

            if is_pos_mismatch:
                logger.warn("[Reconcile] Case C (Pos Mismatch) detected. Resetting everything.")
                self._perform_full_reset()
                return

            # --- B. 检查挂单 ---
            # 如果怀疑某个订单，且该订单在交易所确实存在，但本地找不到
            is_missing_order = False
            if suspicious_oid:
                if any(str(o['orderId']) == suspicious_oid for o in rem_ords):
                    is_missing_order = True
            
            if is_missing_order:
                logger.warn("[Reconcile] Case B (Missing Order) detected. Clearing orders and resetting.")
                self._perform_full_reset()
            else:
                # --- 情况 A: 噪音或延迟回报 ---
                logger.info("[Reconcile] Case A: False alarm or phantom update. Resuming LIVE.")
                self.state = LifecycleState.LIVE

        except Exception as e:
            self.halt_system(f"Reconcile Critical Error: {e}")

    def _perform_full_reset(self):
        """执行彻底的状态刷新，重置所有内部索引"""
        logger.info("[OMS] Performing Full State Reset...")
        try:
            # 1. 物理清场
            for s in self.config["symbols"]:
                self.gateway.cancel_all_orders(s)
            time.sleep(1.0) # 等待撤单在网关层处理完毕

            # 2. 获取最新真值
            acc = self.gateway.get_account_info()
            pos = self.gateway.get_all_positions()
            
            if not acc or not pos:
                raise Exception("Account/Position API failed during reset")

            # 3. 内存重置 (Atomic within lock)
            with self.lock:
                self.orders.clear()
                self.exchange_id_map.clear() # [修复] 现在属性已存在
                
                self.exposure.net_positions.clear()
                self.exposure.avg_prices.clear()
                self.exposure.open_buy_qty.clear()
                self.exposure.open_sell_qty.clear()
                
                # 重新填充
                for p in pos:
                    amt = float(p["positionAmt"])
                    if amt != 0:
                        sym = p["symbol"]
                        self.exposure.net_positions[sym] = amt
                        self.exposure.avg_prices[sym] = float(p["entryPrice"])
                
                # 资金同步
                self.account.force_sync(float(acc["totalWalletBalance"]), float(acc["totalInitialMargin"]))
                
                # 清理监控
                self.order_monitor.monitored_orders.clear()

            self.state = LifecycleState.LIVE
            logger.info("OMS: Reset complete. System is CLEAN & LIVE.")
            
        except Exception as e:
            self.halt_system(f"Reset Failed: {e}")

    # -----------------------------------------------------------
    # 下行指令
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
        
        exchange_oid = self.gateway.send_order(req, client_oid)
        
        if exchange_oid:
            from event.type import OrderSubmitted
            event_data = OrderSubmitted(req, client_oid, time.time())
            self.order_monitor.on_order_submitted(Event(EVENT_ORDER_SUBMITTED, event_data))
            
            # 建立辅助映射
            with self.lock:
                self.exchange_id_map[exchange_oid] = order
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
            target_id = order.exchange_oid if order.exchange_oid else client_oid
            req = CancelRequest(order.intent.symbol, target_id)
        self.gateway.cancel_order(req)

    def cancel_all_orders(self, symbol: str):
        self.gateway.cancel_all_orders(symbol)

    # -----------------------------------------------------------
    # 上行回报 (Deterministic Logic)
    # -----------------------------------------------------------
    def on_exchange_update(self, event):
        self._append_and_process(event)

    def _append_and_process(self, event):
        if self.state == LifecycleState.HALTED: return

        if event.type == "eExchangeOrderUpdate":
            update: ExchangeOrderUpdate = event.data
            if not self.sequence.check(update.seq):
                self.trigger_reconcile(f"Seq Gap {update.seq}")
                return

        self.event_log.append(event)
        self._apply_event(event)

    def _apply_event(self, event):
        with self.lock:
            if event.type == "eBootstrap":
                pass # 运行时不使用 eBootstrap 事件，逻辑在 reset 中

            if event.type == "eExchangeOrderUpdate":
                update: ExchangeOrderUpdate = event.data
                
                # 1. 查找订单 (利用双向索引)
                order = self.orders.get(update.client_oid)
                if not order and update.exchange_oid:
                    order = self.exchange_id_map.get(update.exchange_oid)
                
                # 如果依然找不到 -> 触发对账
                if not order:
                    self.trigger_reconcile(f"Unknown Order {update.exchange_oid}", update.exchange_oid)
                    return

                prev_status = order.status
                
                # 状态机流转
                if update.status == "NEW":
                    order.mark_new(update.exchange_oid)
                    self.exchange_id_map[update.exchange_oid] = order # 补录映射
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

                # 级联更新子系统状态
                self.order_monitor.on_order_update(order.client_oid, order.status)
                self.exposure.update_open_orders(self.orders)
                self.account.calculate()
                
                # 只有状态实质改变才广播，减少 UI 压力
                if order.status != prev_status or update.status == "PARTIALLY_FILLED":
                    self.event_engine.put(Event(EVENT_ORDER_UPDATE, order.to_snapshot()))
                    if update.status in ["FILLED", "PARTIALLY_FILLED"]:
                        pos_data = self.exposure.get_position_data(order.intent.symbol)
                        self.event_engine.put(Event(EVENT_POSITION_UPDATE, pos_data))

    def rebuild_from_log(self):
        logger.info("OMS: Rebuilding from EventLog...")
        with self.lock:
            self.orders.clear()
            self.exchange_id_map.clear()
            self.exposure = ExposureManager()
            self.account = AccountManager(self.event_engine, self.exposure, self.config)
            self.sequence.reset()
            for evt in self.event_log:
                self._apply_event(evt)
        logger.info(f"OMS: Rebuild done. {len(self.orders)} orders restored.")

    def stop(self):
        self.order_monitor.stop()