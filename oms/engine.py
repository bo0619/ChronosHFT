# file: oms/engine.py

import uuid
import threading
import time
from datetime import datetime
from infrastructure.logger import logger

from event.type import (
    Event, OrderIntent, OrderRequest, OrderStatus,
    ExchangeOrderUpdate, CancelRequest,
    EVENT_ORDER_UPDATE, EVENT_TRADE_UPDATE, EVENT_POSITION_UPDATE,
    EVENT_ORDER_SUBMITTED, EVENT_SYSTEM_HEALTH,
    OrderSubmitted, TradeData, LifecycleState, Side,
)

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
        self.gateway      = gateway
        self.config       = config

        self.state = LifecycleState.BOOTSTRAP

        # [Immutable History]
        self.event_log = []

        # [State]
        self.orders          = {}   # client_oid  -> Order
        self.exchange_id_map = {}   # exchange_oid -> Order

        self.lock = threading.RLock()

        # ── OMS 层持仓限额（从 config 读取，不再硬编码）──────────
        # 三层梯度：strategy(3000) < oms(5000) < risk(10000)
        self.max_pos_notional: float = (
            config
            .get("risk", {})
            .get("limits", {})
            .get("max_pos_notional_oms", 5000.0)   # ← 读 config
        )

        # [Components]
        self.sequence     = SequenceValidator()
        self.validator    = OrderValidator(config)
        self.exposure     = ExposureManager()
        self.account      = AccountManager(event_engine, self.exposure, config)
        self.order_monitor = OrderManager(event_engine, gateway, self.trigger_reconcile)

    # -----------------------------------------------------------
    # 生命周期
    # -----------------------------------------------------------

    def bootstrap(self):
        """[Phase 1] 启动引导：拉取真值快照，建立初始状态"""
        logger.info("OMS: Bootstrapping State...")
        self._perform_full_reset()

    def halt_system(self, reason: str):
        if self.state == LifecycleState.HALTED:
            return
        self.state = LifecycleState.HALTED
        logger.critical(f"🛑 OMS HALTED: {reason}")
        self.event_engine.put(Event(EVENT_SYSTEM_HEALTH, f"HALT:{reason}"))
        try:
            for s in self.config["symbols"]:
                self.gateway.cancel_all_orders(s)
        except Exception:
            pass

    def stop(self):
        self.order_monitor.stop()

    # -----------------------------------------------------------
    # 自愈逻辑
    # -----------------------------------------------------------

    def trigger_reconcile(self, reason: str, suspicious_oid: str = None):
        if self.state in [LifecycleState.RECONCILING, LifecycleState.HALTED]:
            return
        logger.warning(f"⚠️  OMS Dirty: {reason}. State -> RECONCILING.")
        self.state = LifecycleState.RECONCILING
        threading.Thread(
            target=self._execute_reconcile,
            args=(suspicious_oid,),
            daemon=True,
        ).start()

    def _execute_reconcile(self, suspicious_oid: str):
        logger.info("[Reconcile] Investigating inconsistency...")
        try:
            rem_pos  = self.gateway.get_all_positions()
            rem_ords = self.gateway.get_open_orders()

            if rem_pos is None or rem_ords is None:
                self.halt_system("Reconcile API unreachable")
                return

            # A. 检查持仓
            is_pos_mismatch = False
            with self.lock:
                rem_map = {
                    p["symbol"]: float(p["positionAmt"])
                    for p in rem_pos
                    if float(p["positionAmt"]) != 0
                }
                loc_map = {
                    s: v for s, v in self.exposure.net_positions.items() if v != 0
                }
                for s in set(rem_map) | set(loc_map):
                    if abs(loc_map.get(s, 0) - rem_map.get(s, 0)) > 1e-6:
                        is_pos_mismatch = True
                        logger.error(
                            f"[Reconcile] Position Mismatch {s}: "
                            f"Local={loc_map.get(s,0)}, Exch={rem_map.get(s,0)}"
                        )
                        break

            if is_pos_mismatch:
                logger.warning("[Reconcile] Case C (Pos Mismatch). Resetting.")
                self._perform_full_reset()
                return

            # B. 检查挂单
            is_missing_order = False
            if suspicious_oid:
                found = any(
                    str(o["orderId"]) == suspicious_oid
                    or o["clientOrderId"] == suspicious_oid
                    for o in rem_ords
                )
                if found:
                    is_missing_order = True

            if is_missing_order:
                logger.warning("[Reconcile] Case B (Missing Order). Resetting.")
                self._perform_full_reset()
            else:
                logger.info("[Reconcile] Case A: False alarm. Resuming LIVE.")
                self.state = LifecycleState.LIVE

        except Exception as e:
            self.halt_system(f"Reconcile Critical Error: {e}")

    def _perform_full_reset(self):
        logger.info("[OMS] Performing Full State Reset...")
        try:
            for s in self.config["symbols"]:
                self.gateway.cancel_all_orders(s)
            time.sleep(1.0)

            acc = self.gateway.get_account_info()
            pos = self.gateway.get_all_positions()

            if not acc or not pos:
                raise Exception("API failed during reset")

            with self.lock:
                self.orders.clear()
                self.exchange_id_map.clear()

                self.exposure.net_positions.clear()
                self.exposure.avg_prices.clear()
                self.exposure.open_buy_qty.clear()
                self.exposure.open_sell_qty.clear()

                for p in pos:
                    amt = float(p["positionAmt"])
                    if amt != 0:
                        sym = p["symbol"]
                        self.exposure.net_positions[sym] = amt
                        self.exposure.avg_prices[sym]    = float(p["entryPrice"])

                self.account.force_sync(
                    float(acc["totalWalletBalance"]),
                    float(acc["totalInitialMargin"]),
                )
                self.order_monitor.monitored_orders.clear()

            self.state = LifecycleState.LIVE
            logger.info("OMS: Reset complete. System is CLEAN & LIVE.")

        except Exception as e:
            self.halt_system(f"Reset Failed: {e}")

    # -----------------------------------------------------------
    # 下行指令
    # -----------------------------------------------------------

    def submit_order(self, intent: OrderIntent) -> str:
        if self.state != LifecycleState.LIVE:
            return None

        client_oid = str(uuid.uuid4())

        with self.lock:
            if not self.validator.validate_params(intent):
                return None

            notional = intent.price * intent.volume
            if not self.account.check_margin(notional):
                return None

            # ── 从 config 读取的 OMS 层限额（非硬编码）──────────
            ok, msg = self.exposure.check_risk(
                intent.symbol,
                intent.side,
                intent.volume,
                self.max_pos_notional,          # ← 修复点
            )
            if not ok:
                logger.warning(f"[OMS] Risk rejected: {msg}")
                return None

            order = Order(client_oid, intent)
            self.orders[client_oid] = order
            order.mark_submitting()

            self.exposure.update_open_orders(self.orders)
            self.account.calculate()

        # IO: 发送至 Gateway
        req = OrderRequest(
            symbol=intent.symbol,
            price=intent.price,
            volume=intent.volume,
            side=intent.side.value,
            order_type=intent.order_type,
            time_in_force=intent.time_in_force,
            post_only=intent.is_post_only,
        )

        exchange_oid = self.gateway.send_order(req, client_oid)

        if exchange_oid:
            event_data = OrderSubmitted(req, client_oid, time.time())
            self.order_monitor.on_order_submitted(
                Event(EVENT_ORDER_SUBMITTED, event_data)
            )
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
            if not order or not order.is_active():
                return
            target_id = order.exchange_oid if order.exchange_oid else client_oid
            req = CancelRequest(order.intent.symbol, target_id)
        self.gateway.cancel_order(req)

    def cancel_all_orders(self, symbol: str):
        self.gateway.cancel_all_orders(symbol)

    # -----------------------------------------------------------
    # 上行回报
    # -----------------------------------------------------------

    def on_exchange_update(self, event):
        self._append_and_process(event)

    def _append_and_process(self, event):
        if self.state == LifecycleState.HALTED:
            return

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
                pass

            if event.type == "eExchangeOrderUpdate":
                update: ExchangeOrderUpdate = event.data

                order = self.orders.get(update.client_oid)
                if not order and update.exchange_oid:
                    order = self.exchange_id_map.get(update.exchange_oid)

                if not order:
                    suspicious = update.client_oid or update.exchange_oid
                    threading.Thread(
                        target=self.trigger_reconcile,
                        args=(f"Unknown Order {suspicious}", suspicious),
                    ).start()
                    return

                prev_status = order.status

                if update.status == "NEW":
                    order.mark_new(update.exchange_oid)
                    if update.exchange_oid:
                        self.exchange_id_map[update.exchange_oid] = order

                elif update.status in ["CANCELED", "EXPIRED"]:
                    order.mark_cancelled()

                elif update.status == "REJECTED":
                    order.mark_rejected()

                elif update.status in ["FILLED", "PARTIALLY_FILLED"]:
                    delta = update.cum_filled_qty - order.filled_volume
                    if delta > 1e-9:
                        order.add_fill(delta, update.filled_price)
                        self.exposure.on_fill(
                            order.intent.symbol,
                            order.intent.side,
                            delta,
                            update.filled_price,
                        )
                        fee = delta * update.filled_price * self.config.get(
                            "backtest", {}
                        ).get("taker_fee", 0.0005)
                        self.account.update_balance(0, fee)

                        trade_data = TradeData(
                            symbol=order.intent.symbol,
                            order_id=order.client_oid,
                            trade_id=f"T{int(update.update_time * 1000)}",
                            side=order.intent.side.value,
                            price=update.filled_price,
                            volume=delta,
                            datetime=datetime.now(),
                        )
                        self.event_engine.put(Event(EVENT_TRADE_UPDATE, trade_data))

                self.order_monitor.on_order_update(order.client_oid, order.status)
                self.exposure.update_open_orders(self.orders)
                self.account.calculate()

                if order.status != prev_status or update.status == "PARTIALLY_FILLED":
                    self.event_engine.put(
                        Event(EVENT_ORDER_UPDATE, order.to_snapshot())
                    )
                    if update.status in ["FILLED", "PARTIALLY_FILLED"]:
                        pos_data = self.exposure.get_position_data(
                            order.intent.symbol
                        )
                        self.event_engine.put(
                            Event(EVENT_POSITION_UPDATE, pos_data)
                        )

    def rebuild_from_log(self):
        pass