# file: event/type.py

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict

# --- 事件常量 ---
EVENT_TICK = "eTick"
EVENT_ORDERBOOK = "eOrderBook"
EVENT_AGG_TRADE = "eAggTrade"
EVENT_MARK_PRICE = "eMarkPrice" # [NEW] 标记价格事件
EVENT_LOG = "eLog"
EVENT_ORDER_REQUEST = "eOrderRequest"
EVENT_ORDER_UPDATE = "eOrderUpdate"
EVENT_TRADE_UPDATE = "eTradeUpdate"
EVENT_POSITION_UPDATE = "ePositionUpdate"
EVENT_BACKTEST_END = "eBacktestEnd"

# --- 方向与状态 ---
Direction_LONG = "LONG"
Direction_SHORT = "SHORT"
Action_OPEN = "OPEN"
Action_CLOSE = "CLOSE"

Status_SUBMITTED = "SUBMITTED"
Status_PARTTRADED = "PARTTRADED"
Status_ALLTRADED = "ALLTRADED"
Status_CANCELLED = "CANCELLED"
Status_REJECTED = "REJECTED"

# --- 异常类 ---
class OrderBookGapError(Exception):
    """当检测到行情丢失时抛出的异常"""
    pass

@dataclass
class Event:
    type: str
    data: any = None # type: ignore

@dataclass
class OrderRequest:
    symbol: str
    price: float
    volume: float
    direction: str
    action: str
    order_type: str = "LIMIT"

@dataclass
class OrderBook:
    symbol: str
    exchange: str
    datetime: datetime
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

# [NEW] 标记价格与资金费率数据
@dataclass
class MarkPriceData:
    symbol: str
    mark_price: float      # 标记价格
    index_price: float     # 指数价格
    funding_rate: float    # 资金费率 (如 0.0001)
    next_funding_time: datetime # 下次结算时间
    datetime: datetime

@dataclass
class AggTradeData:
    symbol: str
    trade_id: int
    price: float
    quantity: float
    maker_is_buyer: bool 
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

@dataclass
class CancelRequest:
    symbol: str
    order_id: str