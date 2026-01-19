# file: oms/position.py

from event.type import TradeData, PositionData, Direction_LONG, Direction_SHORT, Action_OPEN, Action_CLOSE
from event.type import EVENT_TRADE_UPDATE, EVENT_POSITION_UPDATE, Event

class PositionManager:
    def __init__(self, engine):
        self.engine = engine
        self.positions = {}
        self.engine.register(EVENT_TRADE_UPDATE, self.on_trade)

    def on_trade(self, event: Event):
        trade: TradeData = event.data
        if trade.symbol not in self.positions:
            self.positions[trade.symbol] = {
                Direction_LONG: PositionData(trade.symbol, Direction_LONG, 0.0, 0.0),
                Direction_SHORT: PositionData(trade.symbol, Direction_SHORT, 0.0, 0.0)
            }
        
        pos_dict = self.positions[trade.symbol]
        direction = trade.direction
        
        if direction == Direction_LONG:
            pos = pos_dict[Direction_LONG]
            if trade.action == Action_OPEN:
                cost = pos.volume * pos.price + trade.volume * trade.price
                pos.volume += trade.volume
                if pos.volume > 0: pos.price = cost / pos.volume
            else:
                pos.volume -= trade.volume
                if pos.volume < 1e-8: pos.volume = 0
        else:
            pos = pos_dict[Direction_SHORT]
            if trade.action == Action_OPEN:
                cost = pos.volume * pos.price + trade.volume * trade.price
                pos.volume += trade.volume
                if pos.volume > 0: pos.price = cost / pos.volume
            else:
                pos.volume -= trade.volume
                if pos.volume < 1e-8: pos.volume = 0

        self.engine.put(Event(EVENT_POSITION_UPDATE, pos))