from event.type import (
    CancelRequest,
    Event,
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
        if snapshot.status in terminal_statuses:
            self.active_orders.pop(snapshot.client_oid, None)
            self.orders_cancelling.discard(snapshot.client_oid)

    def on_position(self, pos: PositionData):
        self.pos = pos.volume

    def log(self, msg):
        self.engine.put(Event(EVENT_LOG, f"[{self.name}] {msg}"))

    def send_intent(self, intent: OrderIntent):
        intent.price = ref_data_manager.round_price(intent.symbol, intent.price)
        intent.volume = ref_data_manager.round_qty(intent.symbol, intent.volume)

        info = ref_data_manager.get_info(intent.symbol)
        if info:
            notional = intent.price * intent.volume
            min_notional = max(info.min_notional, 5.0)
            if notional < min_notional:
                return None

        client_oid = self.oms.submit_order(intent)
        if client_oid:
            self.active_orders[client_oid] = intent
        return client_oid

    def entry_long(self, symbol, price, volume):
        intent = OrderIntent(self.name, symbol, Side.BUY, price, volume)
        return self.send_intent(intent)

    def exit_long(self, symbol, price, volume):
        intent = OrderIntent(self.name, symbol, Side.SELL, price, volume)
        return self.send_intent(intent)

    def entry_short(self, symbol, price, volume):
        intent = OrderIntent(self.name, symbol, Side.SELL, price, volume)
        return self.send_intent(intent)

    def exit_short(self, symbol, price, volume):
        intent = OrderIntent(self.name, symbol, Side.BUY, price, volume)
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
