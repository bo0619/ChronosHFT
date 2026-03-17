from event.type import (
    AccountData,
    CancelRequest,
    Event,
    LifecycleState,
    OrderBook,
    OrderIntent,
    OrderStateSnapshot,
    OrderStatus,
    PositionData,
    Side,
    TradeData,
    EVENT_LOG,
)
from data.ref_data import ref_data_manager


class StrategyTemplate:
    """
    Base strategy that only talks to OMS.
    """

    def __init__(self, engine, oms, name="Strategy"):
        self.engine = engine
        self.oms = oms
        self.name = name

        self.pos = 0.0
        self.active_orders = {}
        self.orders_cancelling = set()
        self.latest_account = None
        self.last_system_health = ""
        self.last_submit_reject_reason = ""
        self.last_submit_reject_oid = ""
        self.last_submit_reject_by_symbol = {}

    def on_orderbook(self, orderbook: OrderBook):
        raise NotImplementedError

    def on_trade(self, trade: TradeData):
        pass

    def on_order(self, snapshot: OrderStateSnapshot):
        terminal_statuses = {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.REJECTED_LOCALLY,
            OrderStatus.EXPIRED,
        }
        if snapshot.status in {OrderStatus.REJECTED, OrderStatus.REJECTED_LOCALLY}:
            reason = snapshot.error_msg or snapshot.status.value.lower()
            self.last_submit_reject_reason = reason
            self.last_submit_reject_oid = snapshot.client_oid
            self.last_submit_reject_by_symbol[snapshot.symbol] = reason
        if snapshot.status in terminal_statuses:
            self.active_orders.pop(snapshot.client_oid, None)
            self.orders_cancelling.discard(snapshot.client_oid)

    def on_position(self, pos: PositionData):
        self.pos = pos.volume

    def on_account_update(self, account: AccountData):
        self.latest_account = account

    def on_system_health(self, message):
        if not isinstance(message, str):
            message = str(message)
        self.last_system_health = message

    def on_submit_rejected(self, intent: OrderIntent, reason: str, client_oid: str = ""):
        self.last_submit_reject_reason = reason
        self.last_submit_reject_oid = client_oid or ""
        self.last_submit_reject_by_symbol[intent.symbol] = reason

    def can_submit_orders(self, symbol: str = "") -> bool:
        if hasattr(self.oms, "can_submit_for_strategy"):
            return bool(self.oms.can_submit_for_strategy(self.name, symbol))
        if symbol and hasattr(self.oms, "is_symbol_tradeable"):
            return bool(self.oms.is_symbol_tradeable(symbol))
        return getattr(self.oms, "state", None) == LifecycleState.LIVE

    def log(self, msg):
        self.engine.put(Event(EVENT_LOG, f"[{self.name}] {msg}"))

    def send_intent(self, intent: OrderIntent):
        intent.price = ref_data_manager.round_price(intent.symbol, intent.price)
        intent.volume = ref_data_manager.round_qty(intent.symbol, intent.volume)

        if hasattr(self.oms, "adapt_intent_for_trading_mode"):
            adapted_intent, reject_reason = self.oms.adapt_intent_for_trading_mode(intent)
            if reject_reason:
                self.on_submit_rejected(intent, reject_reason)
                return None
            intent = adapted_intent

        info = ref_data_manager.get_info(intent.symbol)
        if info:
            notional = intent.price * intent.volume
            min_notional = max(info.min_notional, 5.0)
            if notional < min_notional:
                self.on_submit_rejected(intent, "min_notional")
                return None

        submit_result = self.oms.submit_order(intent)
        if isinstance(submit_result, str):
            client_oid = submit_result
            if client_oid:
                self.active_orders[client_oid] = intent
            return client_oid

        client_oid = getattr(submit_result, "client_oid", "") if submit_result else ""
        accepted = bool(getattr(submit_result, "accepted", False)) if submit_result else False
        if accepted and client_oid:
            self.active_orders[client_oid] = intent
            return client_oid

        reject_reason = getattr(submit_result, "reason", "submit_rejected") if submit_result else "submit_rejected"
        self.on_submit_rejected(intent, reject_reason, client_oid)
        return None

    def entry_long(self, symbol, price, volume):
        intent = OrderIntent(self.name, symbol, Side.BUY, price, volume)
        return self.send_intent(intent)

    def exit_long(self, symbol, price, volume):
        intent = OrderIntent(self.name, symbol, Side.SELL, price, volume, reduce_only=True)
        return self.send_intent(intent)

    def entry_short(self, symbol, price, volume):
        intent = OrderIntent(self.name, symbol, Side.SELL, price, volume)
        return self.send_intent(intent)

    def exit_short(self, symbol, price, volume):
        intent = OrderIntent(self.name, symbol, Side.BUY, price, volume, reduce_only=True)
        return self.send_intent(intent)

    def buy(self, symbol, price, volume):
        return self.entry_long(symbol, price, volume)

    def sell(self, symbol, price, volume):
        return self.entry_short(symbol, price, volume)

    def cancel_order(self, client_oid: str):
        if client_oid not in self.active_orders:
            return
        if client_oid in self.orders_cancelling:
            return

        self.orders_cancelling.add(client_oid)
        self.oms.cancel_order(client_oid)

    def cancel_all(self, symbol: str):
        self.oms.cancel_all_orders(symbol)

        to_remove = [
            oid for oid, intent in self.active_orders.items() if intent.symbol == symbol
        ]
        for oid in to_remove:
            del self.active_orders[oid]
            self.orders_cancelling.discard(oid)
