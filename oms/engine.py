import threading
import time
import uuid
from collections import deque
from datetime import datetime

from infrastructure.logger import logger

from event.type import (
    CancelRequest,
    Event,
    ExchangeAccountUpdate,
    ExchangeOrderUpdate,
    LifecycleState,
    OrderIntent,
    OrderRequest,
    OrderStatus,
    OrderSubmitted,
    Side,
    TradeData,
    EVENT_ORDER_SUBMITTED,
    EVENT_ORDER_UPDATE,
    EVENT_POSITION_UPDATE,
    EVENT_SYSTEM_HEALTH,
    EVENT_TRADE_UPDATE,
)

from .account_manager import AccountManager
from .exposure import ExposureManager
from .journal import OMSJournal
from .order import Order, TERMINAL_STATUSES
from .order_manager import OrderManager
from .sequence import SequenceValidator
from .validator import OrderValidator


class OMS:
    """Deterministic OMS for a single Binance perpetual account."""

    def __init__(self, event_engine, gateway, config):
        self.event_engine = event_engine
        self.gateway = gateway
        self.config = config

        self.state = LifecycleState.BOOTSTRAP

        self.event_log = []
        self.orders = {}
        self.exchange_id_map = {}
        self.lock = threading.RLock()

        self.max_pos_notional = (
            config.get("risk", {})
            .get("limits", {})
            .get("max_pos_notional", 2000.0)   # [RISK-1] 统一字段名，与 config.json 一致
        )

        self.sequence = SequenceValidator()
        self.validator = OrderValidator(config)
        self.exposure = ExposureManager()
        self.account = AccountManager(event_engine, self.exposure, config)
        self.order_monitor = OrderManager(event_engine, gateway, self.trigger_reconcile)

        self.journal = OMSJournal(config)
        self.TOMBSTONE_MAX = config.get("oms", {}).get("tombstone_max", 2000)
        self.terminated_oids = set()
        self.terminated_oid_queue = deque()
        self.rebuild_summary = self.rebuild_from_log()

    # -----------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------

    def bootstrap(self):
        logger.info("OMS: Bootstrapping state...")
        self._audit("bootstrap_requested", recovered=self.rebuild_summary)
        self._perform_full_reset()

    def halt_system(self, reason: str):
        if self.state == LifecycleState.HALTED:
            return
        self.state = LifecycleState.HALTED
        logger.critical(f"OMS HALTED: {reason}")
        self._audit("lifecycle", state=self.state.value, reason=reason)
        self.event_engine.put(Event(EVENT_SYSTEM_HEALTH, f"HALT:{reason}"))
        try:
            for symbol in self.config["symbols"]:
                self.gateway.cancel_all_orders(symbol)
        except Exception:
            pass

    def stop(self):
        self._audit("oms_stopped", state=self.state.value)
        self.order_monitor.stop()

    # -----------------------------------------------------------
    # Recovery and reconcile
    # -----------------------------------------------------------

    def trigger_reconcile(self, reason: str, suspicious_oid: str = None):
        if self.state in [LifecycleState.RECONCILING, LifecycleState.HALTED]:
            return
        logger.warning(f"OMS dirty: {reason}. State -> RECONCILING")
        self.state = LifecycleState.RECONCILING
        self._audit(
            "reconcile_requested",
            state=self.state.value,
            reason=reason,
            suspicious_oid=suspicious_oid,
        )
        threading.Thread(
            target=self._execute_reconcile,
            args=(suspicious_oid,),
            daemon=True,
        ).start()

    def _execute_reconcile(self, suspicious_oid: str):
        self._audit("reconcile_started", suspicious_oid=suspicious_oid)
        try:
            remote_positions = self.gateway.get_all_positions()
            remote_orders = self.gateway.get_open_orders()

            if remote_positions is None or remote_orders is None:
                self.halt_system("Reconcile API unreachable")
                return

            with self.lock:
                remote_map = {
                    pos["symbol"]: float(pos["positionAmt"])
                    for pos in remote_positions
                    if float(pos["positionAmt"]) != 0
                }
                local_map = {
                    symbol: volume
                    for symbol, volume in self.exposure.net_positions.items()
                    if volume != 0
                }
                local_active_orders = self._collect_local_active_orders_locked()

            for symbol in set(remote_map) | set(local_map):
                if abs(local_map.get(symbol, 0.0) - remote_map.get(symbol, 0.0)) > 1e-6:
                    logger.error(
                        f"[Reconcile] Position mismatch {symbol}: "
                        f"Local={local_map.get(symbol, 0.0)}, Exch={remote_map.get(symbol, 0.0)}"
                    )
                    self._audit("reconcile_reset", case="position_mismatch", symbol=symbol)
                    self._perform_full_reset()
                    return

            remote_active_orders = self._normalize_remote_open_orders(remote_orders)
            if local_active_orders != remote_active_orders:
                self._audit(
                    "reconcile_reset",
                    case="open_order_mismatch",
                    local_active_orders=local_active_orders,
                    remote_active_orders=remote_active_orders,
                    suspicious_oid=suspicious_oid,
                )
                self._perform_full_reset()
                return

            remote_has_suspicious = False
            if suspicious_oid:
                remote_has_suspicious = any(
                    suspicious_oid in order["identifiers"]
                    for order in remote_active_orders
                )

            if remote_has_suspicious:
                self._audit(
                    "reconcile_reset",
                    case="missing_local_order",
                    suspicious_oid=suspicious_oid,
                )
                self._perform_full_reset()
            else:
                self.state = LifecycleState.LIVE
                self._audit("reconcile_cleared", state=self.state.value)
                logger.info("[Reconcile] False alarm. Resuming LIVE.")

        except Exception as exc:
            self.halt_system(f"Reconcile critical error: {exc}")

    def _perform_full_reset(self):
        logger.info("[OMS] Performing full state reset...")
        self._audit("full_reset_started", symbols=self.config.get("symbols", []))
        try:
            for symbol in self.config["symbols"]:
                self.gateway.cancel_all_orders(symbol)
            time.sleep(1.0)

            remote_orders = self.gateway.get_open_orders()
            account = self.gateway.get_account_info()
            positions = self.gateway.get_all_positions()
            if remote_orders is None or not account or positions is None:
                raise RuntimeError("API failed during reset")

            residual_orders = self._normalize_remote_open_orders(remote_orders)
            if residual_orders:
                raise RuntimeError(
                    f"remote open orders still present after cancel-all: {residual_orders}"
                )

            with self.lock:
                self.orders.clear()
                self.exchange_id_map.clear()
                self.sequence.reset()

                self.exposure.net_positions.clear()
                self.exposure.avg_prices.clear()
                self.exposure.open_buy_qty.clear()
                self.exposure.open_sell_qty.clear()

                for pos in positions:
                    amount = float(pos["positionAmt"])
                    if amount == 0:
                        continue
                    symbol = pos["symbol"]
                    self.exposure.force_sync(symbol, amount, float(pos["entryPrice"]))

                available_balance = account.get("availableBalance")
                self.account.force_sync(
                    float(account["totalWalletBalance"]),
                    float(account["totalInitialMargin"]),
                    float(available_balance) if available_balance is not None else None,
                )
                self.order_monitor.monitored_orders.clear()

            for symbol in self.config.get("symbols", []):
                self._emit_position_update(symbol)

            self.state = LifecycleState.LIVE
            self._audit(
                "full_reset_completed",
                state=self.state.value,
                balance=self.account.balance,
                equity=self.account.equity,
                positions=dict(self.exposure.net_positions),
            )
            logger.info("OMS: Reset complete. System is CLEAN and LIVE.")

        except Exception as exc:
            self.halt_system(f"Reset failed: {exc}")

    # -----------------------------------------------------------
    # Downstream requests
    # -----------------------------------------------------------

    def submit_order(self, intent: OrderIntent) -> str:
        if self.state != LifecycleState.LIVE:
            self._audit(
                "intent_rejected",
                reason=f"oms_not_live:{self.state.value}",
                intent=self._serialize_intent(intent),
            )
            return None

        valid, validation_reason = self.validator.validate_params(intent)
        if not valid:
            self._audit(
                "intent_rejected",
                reason=validation_reason,
                intent=self._serialize_intent(intent),
            )
            return None

        notional = intent.price * intent.volume
        if not self.account.check_margin(notional):
            self._audit(
                "intent_rejected",
                reason="insufficient_margin",
                intent=self._serialize_intent(intent),
                notional=notional,
                available=self.account.available,
            )
            return None

        ok, risk_reason = self.exposure.check_risk(
            intent.symbol,
            intent.side,
            intent.volume,
            self.max_pos_notional,
        )
        if not ok:
            logger.warning(f"[OMS] Risk rejected: {risk_reason}")
            self._audit(
                "intent_rejected",
                reason=f"exposure_limit:{risk_reason}",
                intent=self._serialize_intent(intent),
            )
            return None

        client_oid = str(uuid.uuid4())
        order = Order(client_oid, intent)

        with self.lock:
            self.orders[client_oid] = order
            order.mark_submitting()
            self.exposure.update_open_orders(self.orders)
            self.account.calculate()
            self._record_order_snapshot(order, "accepted")

        request = OrderRequest(
            symbol=intent.symbol,
            price=intent.price,
            volume=intent.volume,
            side=intent.side.value,
            order_type=intent.order_type,
            time_in_force=intent.time_in_force,
            post_only=intent.is_post_only,
        )

        exchange_oid = self.gateway.send_order(request, client_oid)

        if exchange_oid:
            with self.lock:
                order.mark_pending_ack(exchange_oid)
                self.exchange_id_map[exchange_oid] = order
                self._record_order_snapshot(order, "rest_ack")
                self._emit_order_update(order)

            event_data = OrderSubmitted(request, client_oid, time.time())
            self.event_engine.put(Event(EVENT_ORDER_SUBMITTED, event_data))
            self._audit(
                "order_submitted",
                client_oid=client_oid,
                exchange_oid=exchange_oid,
                symbol=intent.symbol,
                side=intent.side.value,
                price=intent.price,
                volume=intent.volume,
            )
            return client_oid

        with self.lock:
            order.mark_rejected_locally("gateway_send_failed")
            self._record_order_snapshot(order, "send_failed")
            self.orders.pop(client_oid, None)
            self.exposure.update_open_orders(self.orders)
            self.account.calculate()
        self._write_tombstone(order)
        self._audit(
            "order_rejected_locally",
            client_oid=client_oid,
            symbol=intent.symbol,
            reason="gateway_send_failed",
        )
        return None

    def cancel_order(self, client_oid: str):
        with self.lock:
            order = self.orders.get(client_oid)
            if not order or not order.is_active():
                return
            target_id = order.exchange_oid if order.exchange_oid else client_oid
            try:
                order.mark_cancelling()
                self._record_order_snapshot(order, "cancel_requested")
                self._emit_order_update(order)
            except ValueError:
                pass
            request = CancelRequest(order.intent.symbol, target_id)

        self.gateway.cancel_order(request)
        self._audit(
            "cancel_submitted",
            client_oid=client_oid,
            target_id=target_id,
            symbol=request.symbol,
        )

    def cancel_all_orders(self, symbol: str):
        self._audit("cancel_all_submitted", symbol=symbol)
        self.gateway.cancel_all_orders(symbol)

    # -----------------------------------------------------------
    # Upstream exchange updates
    # -----------------------------------------------------------

    def on_exchange_update(self, event):
        self._append_and_process(event)

    def on_exchange_account_update(self, event):
        update: ExchangeAccountUpdate = event.data
        tracked_symbols = set(self.config.get("symbols", []))
        tracked_positions = {
            symbol: payload
            for symbol, payload in update.positions.items()
            if not tracked_symbols or symbol in tracked_symbols
        }

        with self.lock:
            self.account.sync_exchange_balance(
                update.wallet_balance,
                available=update.available_balance,
            )
            position_drift = self._collect_exchange_position_drift_locked(tracked_positions)
            has_active_orders = self._has_active_orders_locked(tracked_positions.keys())

        if position_drift:
            self._audit(
                "exchange_account_position_drift",
                reason=update.reason,
                positions=position_drift,
            )
            if not has_active_orders and self.state != LifecycleState.HALTED:
                logger.warning(f"[OMS] Exchange position drift detected: {position_drift}")

    def _append_and_process(self, event):
        if self.state == LifecycleState.HALTED:
            return

        if event.type == "eExchangeOrderUpdate":
            update: ExchangeOrderUpdate = event.data
            if not self.sequence.check(update.seq):
                self._audit("sequence_gap", seq=update.seq)
                self.trigger_reconcile(f"Seq gap {update.seq}")
                return

        self.event_log.append(event)
        self._apply_event(event)

    def _apply_event(self, event):
        if event.type != "eExchangeOrderUpdate":
            return

        update: ExchangeOrderUpdate = event.data
        with self.lock:
            order = self.orders.get(update.client_oid)
            if not order and update.exchange_oid:
                order = self.exchange_id_map.get(update.exchange_oid)

            if not order:
                suspicious = update.client_oid or update.exchange_oid
                if suspicious in self.terminated_oids:
                    self._audit(
                        "late_duplicate_ignored",
                        suspicious_oid=suspicious,
                        exchange_status=update.status,
                    )
                    return
                self._audit(
                    "unknown_order_update",
                    client_oid=update.client_oid,
                    exchange_oid=update.exchange_oid,
                    status=update.status,
                )
                threading.Thread(
                    target=self.trigger_reconcile,
                    args=(f"Unknown Order {suspicious}", suspicious),
                    daemon=True,
                ).start()
                return

            if update.exchange_oid and order.exchange_oid and order.exchange_oid != update.exchange_oid:
                self._audit(
                    "exchange_oid_mismatch",
                    client_oid=order.client_oid,
                    local_exchange_oid=order.exchange_oid,
                    incoming_exchange_oid=update.exchange_oid,
                )
                threading.Thread(
                    target=self.trigger_reconcile,
                    args=(f"Exchange OID mismatch {order.client_oid}", order.client_oid),
                    daemon=True,
                ).start()
                return

            if update.seq and update.seq <= order.last_update_seq:
                self._audit(
                    "stale_update_ignored",
                    client_oid=order.client_oid,
                    seq=update.seq,
                    last_seq=order.last_update_seq,
                )
                return

            if update.cum_filled_qty + 1e-9 < order.filled_volume:
                self._audit(
                    "cum_fill_regression",
                    client_oid=order.client_oid,
                    incoming_cum=update.cum_filled_qty,
                    local_cum=order.filled_volume,
                )
                threading.Thread(
                    target=self.trigger_reconcile,
                    args=(f"Cum fill regression {order.client_oid}", order.client_oid),
                    daemon=True,
                ).start()
                return

            previous_status = order.status
            had_fill = False

            try:
                if update.status == "NEW":
                    order.mark_new(
                        exchange_oid=update.exchange_oid,
                        update_time=update.update_time,
                        seq=update.seq,
                    )
                    if update.exchange_oid:
                        self.exchange_id_map[update.exchange_oid] = order

                elif update.status == "CANCELED":
                    order.mark_cancelled(
                        update_time=update.update_time,
                        seq=update.seq,
                        exchange_status=update.status,
                    )
                    self._write_tombstone(order)

                elif update.status == "EXPIRED":
                    order.mark_expired(update_time=update.update_time, seq=update.seq)
                    self._write_tombstone(order)

                elif update.status == "REJECTED":
                    order.mark_rejected(
                        reason="exchange_rejected",
                        update_time=update.update_time,
                        seq=update.seq,
                        exchange_status=update.status,
                    )
                    self._write_tombstone(order)

                elif update.status in ["FILLED", "PARTIALLY_FILLED"]:
                    delta = update.cum_filled_qty - order.filled_volume
                    if delta > 1e-9:
                        had_fill = order.add_fill(
                            delta,
                            update.filled_price,
                            update_time=update.update_time,
                            seq=update.seq,
                            exchange_status=update.status,
                        )
                        local_realized_pnl = self.exposure.on_fill(
                            order.intent.symbol,
                            order.intent.side,
                            delta,
                            update.filled_price,
                        )
                        realized_pnl = (
                            update.realized_pnl
                            if update.realized_pnl is not None
                            else local_realized_pnl
                        )
                        fill_notional = delta * update.filled_price
                        fee = self._get_fill_commission(update, order, fill_notional)
                        self.account.update_balance(realized_pnl, fee)

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
                    else:
                        order.note_exchange_update(
                            exchange_status=update.status,
                            update_time=update.update_time,
                            seq=update.seq,
                            exchange_oid=update.exchange_oid,
                        )

                    if update.status == "FILLED":
                        order.mark_filled(update_time=update.update_time, seq=update.seq)
                        self._write_tombstone(order)

                else:
                    self._audit(
                        "unhandled_exchange_status",
                        client_oid=order.client_oid,
                        status=update.status,
                    )
                    order.note_exchange_update(
                        exchange_status=update.status,
                        update_time=update.update_time,
                        seq=update.seq,
                        exchange_oid=update.exchange_oid,
                    )
                    return

            except ValueError as exc:
                self._audit(
                    "invalid_transition",
                    client_oid=order.client_oid,
                    current_status=order.status.value,
                    incoming_status=update.status,
                    error=str(exc),
                )
                threading.Thread(
                    target=self.trigger_reconcile,
                    args=(f"Invalid transition {order.client_oid}", order.client_oid),
                    daemon=True,
                ).start()
                return

            self.order_monitor.on_order_update(order.client_oid, order.status)
            self.exposure.update_open_orders(self.orders)
            self.account.calculate()

            if order.status != previous_status or had_fill:
                self._record_order_snapshot(
                    order,
                    "exchange_update",
                    exchange_status=update.status,
                    seq=update.seq,
                    cum_filled_qty=update.cum_filled_qty,
                )
                self._emit_order_update(order)
                if had_fill:
                    self._emit_position_update(order.intent.symbol)

    def rebuild_from_log(self):
        records = self.journal.load()
        if not records:
            return {
                "records": 0,
                "recovered_orders": 0,
                "recovered_terminal_ids": 0,
            }

        latest_order_records = {}
        last_lifecycle = None
        for record in records:
            payload = record.get("payload", {})
            if record.get("kind") == "order_snapshot":
                client_oid = payload.get("client_oid")
                if client_oid:
                    latest_order_records[client_oid] = payload
            elif record.get("kind") == "lifecycle":
                last_lifecycle = payload.get("state")

        with self.lock:
            self.terminated_oids.clear()
            self.terminated_oid_queue.clear()
            recovered_terminal_ids = 0
            for payload in latest_order_records.values():
                status = payload.get("status")
                if status in {state.value for state in TERMINAL_STATUSES}:
                    client_oid = payload.get("client_oid")
                    exchange_oid = payload.get("exchange_oid")
                    if client_oid:
                        self._remember_terminated_oid(client_oid)
                        recovered_terminal_ids += 1
                    if exchange_oid:
                        self._remember_terminated_oid(exchange_oid)
                        recovered_terminal_ids += 1

        summary = {
            "records": len(records),
            "recovered_orders": len(latest_order_records),
            "recovered_terminal_ids": recovered_terminal_ids,
            "last_lifecycle": last_lifecycle,
        }
        if recovered_terminal_ids:
            logger.info(
                f"[OMS] Recovered {recovered_terminal_ids} terminal IDs from journal"
            )
        return summary

    # -----------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------

    def _normalize_remote_open_orders(self, remote_orders):
        tracked_symbols = set(self.config.get("symbols", []))
        normalized = []
        for order in remote_orders:
            symbol = order.get("symbol")
            if tracked_symbols and symbol not in tracked_symbols:
                continue
            identifiers = tuple(
                sorted(
                    oid
                    for oid in [
                        str(order.get("orderId")) if order.get("orderId") is not None else "",
                        order.get("clientOrderId") or "",
                    ]
                    if oid
                )
            )
            normalized.append(
                {
                    "symbol": symbol,
                    "identifiers": identifiers,
                    "side": order.get("side", ""),
                }
            )
        normalized.sort(key=lambda item: (item["symbol"], item["identifiers"], item["side"]))
        return normalized

    def _collect_local_active_orders_locked(self):
        tracked_symbols = set(self.config.get("symbols", []))
        normalized = []
        for order in self.orders.values():
            if not order.is_active():
                continue
            if tracked_symbols and order.intent.symbol not in tracked_symbols:
                continue
            identifiers = tuple(
                sorted(
                    oid
                    for oid in [order.client_oid, order.exchange_oid]
                    if oid
                )
            )
            normalized.append(
                {
                    "symbol": order.intent.symbol,
                    "identifiers": identifiers,
                    "side": order.intent.side.value,
                }
            )
        normalized.sort(key=lambda item: (item["symbol"], item["identifiers"], item["side"]))
        return normalized

    def _collect_exchange_position_drift_locked(self, exchange_positions):
        drift = {}
        for symbol, payload in exchange_positions.items():
            local_pos = self.exposure.net_positions.get(symbol, 0.0)
            exchange_pos = float(payload.get("volume", 0.0))
            if abs(local_pos - exchange_pos) > 1e-6:
                drift[symbol] = {
                    "local": local_pos,
                    "exchange": exchange_pos,
                    "entry_price": float(payload.get("entry_price", 0.0)),
                }
        return drift

    def _has_active_orders_locked(self, symbols=None):
        tracked_symbols = set(symbols or [])
        for order in self.orders.values():
            if not order.is_active():
                continue
            if tracked_symbols and order.intent.symbol not in tracked_symbols:
                continue
            return True
        return False

    def _extract_quote_asset(self, symbol: str) -> str:
        for suffix in ("USDT", "USDC", "BUSD", "FDUSD", "BTC", "ETH", "BNB"):
            if symbol.endswith(suffix):
                return suffix
        return ""

    def _get_fill_commission(self, update: ExchangeOrderUpdate, order: Order, fill_notional: float) -> float:
        if update.commission is None:
            return fill_notional * self._get_fee_rate(order, is_maker=update.is_maker)

        asset = (update.commission_asset or self._extract_quote_asset(order.intent.symbol)).upper()
        if asset in {"", "USDT", "USDC", "BUSD", "FDUSD"}:
            return update.commission

        logger.warning(
            f"[OMS] Unsupported commission asset {asset}; falling back to configured fee model"
        )
        return fill_notional * self._get_fee_rate(order, is_maker=update.is_maker)

    def _get_fee_rate(self, order: Order, is_maker: bool = None) -> float:
        fee_config = self.config.get("backtest", {})
        if is_maker is True:
            return fee_config.get("maker_fee", 0.0)
        if is_maker is False:
            return fee_config.get("taker_fee", 0.0005)
        if order.intent.is_post_only:
            return fee_config.get("maker_fee", 0.0)
        return fee_config.get("taker_fee", 0.0005)

    def _serialize_intent(self, intent: OrderIntent) -> dict:
        return {
            "strategy_id": intent.strategy_id,
            "symbol": intent.symbol,
            "side": intent.side.value,
            "price": intent.price,
            "volume": intent.volume,
            "order_type": intent.order_type,
            "time_in_force": intent.time_in_force,
            "is_post_only": intent.is_post_only,
            "policy": intent.policy.value,
            "tag": intent.tag,
        }

    def _audit(self, kind: str, **payload):
        payload.setdefault("state", self.state.value)
        self.journal.append(kind, payload)

    def _record_order_snapshot(self, order: Order, source: str, **extra):
        payload = order.to_record()
        payload["source"] = source
        if extra:
            payload["extra"] = extra
        self.journal.append("order_snapshot", payload)

    def _emit_order_update(self, order: Order):
        self.event_engine.put(Event(EVENT_ORDER_UPDATE, order.to_snapshot()))

    def _emit_position_update(self, symbol: str):
        self.event_engine.put(
            Event(EVENT_POSITION_UPDATE, self.exposure.get_position_data(symbol))
        )

    def _remember_terminated_oid(self, oid: str):
        if not oid or oid in self.terminated_oids:
            return
        if len(self.terminated_oid_queue) >= self.TOMBSTONE_MAX:
            stale = self.terminated_oid_queue.popleft()
            self.terminated_oids.discard(stale)
        self.terminated_oid_queue.append(oid)
        self.terminated_oids.add(oid)

    def _write_tombstone(self, order: Order):
        self._remember_terminated_oid(order.client_oid)
        self._remember_terminated_oid(order.exchange_oid)
