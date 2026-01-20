# file: event/type.py

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict

# --- 事件常量 ---
EVENT_TICK = "eTick"
EVENT_ORDERBOOK = "eOrderBook"
EVENT_AGG_TRADE = "eAggTrade"
EVENT_MARK_PRICE = "eMarkPrice"
EVENT_LOG = "eLog"
EVENT_ORDER_REQUEST = "eOrderRequest"
EVENT_ORDER_UPDATE = "eOrderUpdate"
EVENT_TRADE_UPDATE = "eTradeUpdate"
EVENT_POSITION_UPDATE = "ePositionUpdate"
EVENT_BACKTEST_END = "eBacktestEnd"

# --- 核心枚举 ---
# 在单向持仓模式下，我们只关心买卖方向
Side_BUY = "BUY"
Side_SELL = "SELL"

Status_SUBMITTED = "SUBMITTED"
Status_PARTTRADED = "PARTTRADED"
Status_ALLTRADED = "ALLTRADED"
Status_CANCELLED = "CANCELLED"
Status_REJECTED = "REJECTED"

class OrderBookGapError(Exception):
    pass

@dataclass
class Event:
    type: str
    data: any = None

@dataclass
class OrderRequest:
    symbol: str
    price: float
    volume: float
    side: str       # BUY or SELL
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

@dataclass
class MarkPriceData:
    symbol: str
    mark_price: float
    index_price: float
    funding_rate: float
    next_funding_time: datetime
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
    side: str       # BUY or SELL
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
    side: str       # BUY or SELL
    price: float
    volume: float
    datetime: datetime

@dataclass
class PositionData:
    symbol: str
    volume: float   # 带符号浮点数: >0 多头, <0 空头
    price: float    # 持仓均价
    pnl: float = 0.0 # 浮动盈亏