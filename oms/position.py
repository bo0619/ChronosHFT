# file: oms/position.py

from event.type import TradeData, PositionData, Side_BUY, Side_SELL
from event.type import EVENT_TRADE_UPDATE, EVENT_POSITION_UPDATE, Event

class PositionManager:
    def __init__(self, engine):
        self.engine = engine
        self.positions = {} # Symbol -> PositionData
        self.engine.register(EVENT_TRADE_UPDATE, self.on_trade)

    def on_trade(self, event: Event):
        trade: TradeData = event.data
        if trade.symbol not in self.positions:
            self.positions[trade.symbol] = PositionData(trade.symbol, 0.0, 0.0)
            
        pos = self.positions[trade.symbol]
        
        # 交易带来的数量变化 (Buy: +, Sell: -)
        qty_change = trade.volume if trade.side == Side_BUY else -trade.volume
        
        # 1. 判断是加仓还是减仓 (或反手)
        if pos.volume == 0:
            # 开仓
            pos.price = trade.price
            pos.volume = qty_change
        elif (pos.volume > 0 and qty_change > 0) or (pos.volume < 0 and qty_change < 0):
            # 加仓 (同向) -> 更新加权均价
            # NewAvg = (OldPos * OldAvg + NewQty * TradePrice) / TotalQty
            total_val = (abs(pos.volume) * pos.price) + (abs(qty_change) * trade.price)
            pos.volume += qty_change
            pos.price = total_val / abs(pos.volume)
        else:
            # 减仓或反手 (反向)
            # 剩余持仓量
            new_volume = pos.volume + qty_change
            
            if (pos.volume > 0 and new_volume >= 0) or (pos.volume < 0 and new_volume <= 0):
                # 纯减仓，不反手 -> 均价不变，计算 Realized PnL (此处OMS只维护状态，PnL由Accountant计算)
                pos.volume = new_volume
                if pos.volume == 0: pos.price = 0
            else:
                # 反手 (Flipped) -> 先平仓，再开新仓
                # 此时 pos.price 应该重置为本次成交价
                pos.volume = new_volume
                pos.price = trade.price

        self.engine.put(Event(EVENT_POSITION_UPDATE, pos))