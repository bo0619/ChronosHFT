# file: event/type.py

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict

EVENT_TICK = "eTick"
EVENT_ORDERBOOK = "eOrderBook"
EVENT_AGG_TRADE = "eAggTrade" # [NEW] 归集成交事件
EVENT_LOG = "eLog"
EVENT_ORDER_REQUEST = "eOrderRequest"
EVENT_ORDER_UPDATE = "eOrderUpdate"
EVENT_TRADE_UPDATE = "eTradeUpdate"
EVENT_POSITION_UPDATE = "ePositionUpdate"
EVENT_BACKTEST_END = "eBacktestEnd"

Direction_LONG = "LONG"
Direction_SHORT = "SHORT"
Action_OPEN = "OPEN"
Action_CLOSE = "CLOSE"

Status_SUBMITTED = "SUBMITTED"
Status_PARTTRADED = "PARTTRADED"
Status_ALLTRADED = "ALLTRADED"
Status_CANCELLED = "CANCELLED"
Status_REJECTED = "REJECTED"

@dataclass
class Event:
    type: str
    data: any = None

@dataclass
class OrderRequest:
    symbol: str
    price: float
    volume: float
    direction: str
    action: str
    order_type: str = "LIMIT"

@dataclass
class CancelRequest:
    symbol: str
    order_id: str

@dataclass
class OrderBook:
    symbol: str
    exchange: str
    datetime: datetime
    # key=price, value=volume
    asks: Dict[float, float] = field(default_factory=dict)
    bids: Dict[float, float] = field(default_factory=dict)

    def get_best_bid(self):
        if not self.bids: return 0.0, 0.0
        p = max(self.bids.keys())
        return p, self.bids[p]

    def get_best_ask(self):
        if not self.asks: return 0.0, 0.0
        p = min(self.asks.keys())
        return p, self.asks[p]

# [NEW] 市场逐笔成交数据
@dataclass
class AggTradeData:
    symbol: str
    trade_id: int
    price: float
    quantity: float
    maker_is_buyer: bool # True=卖方主动吃买单(价格下跌), False=买方主动吃卖单(价格上涨)
    datetime: datetime

@dataclass
class OrderData:
    symbol: str
    order_id: str
    direction: str
    action: str
    price: float
    volume: float
    traded: float
    status: str
    datetime: datetime

@dataclass
class TradeData:
    symbol: str
    order_id: str
    trade_id: str
    direction: str
    action: str
    price: float
    volume: float
    datetime: datetime

@dataclass
class PositionData:
    symbol: str
    direction: str
    volume: float
    price: float
    pnl: float = 0.0