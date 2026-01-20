# file: event/type.py

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict

# --- 事件常量 ---
EVENT_TICK = "eTick"
EVENT_ORDERBOOK = "eOrderBook"
EVENT_AGG_TRADE = "eAggTrade"
EVENT_MARK_PRICE = "eMarkPrice"
EVENT_ACCOUNT_UPDATE = "eAccountUpdate"
EVENT_LOG = "eLog"
EVENT_ORDER_REQUEST = "eOrderRequest"
EVENT_ORDER_SUBMITTED = "eOrderSubmitted"
EVENT_ORDER_UPDATE = "eOrderUpdate"
EVENT_TRADE_UPDATE = "eTradeUpdate"
EVENT_POSITION_UPDATE = "ePositionUpdate"
EVENT_BACKTEST_END = "eBacktestEnd"
EVENT_API_LIMIT = "eApiLimit" # [NEW] API 权重事件
EVENT_ALERT = "eAlert"        # [NEW] 报警事件

# --- 核心枚举 ---
Side_BUY = "BUY"
Side_SELL = "SELL"

# [NEW] Time In Force
TIF_GTC = "GTC" # Good Till Cancel
TIF_IOC = "IOC" # Immediate or Cancel
TIF_FOK = "FOK" # Fill or Kill
TIF_GTX = "GTX" # Post Only (Maker Only)

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
    side: str       
    order_type: str = "LIMIT"
    # [NEW] 高级参数
    time_in_force: str = TIF_GTC 
    post_only: bool = False      # 如果为True，强制设为 GTX

@dataclass
class CancelRequest:
    symbol: str
    order_id: str

@dataclass
class OrderSubmitted:
    req: OrderRequest
    order_id: str
    timestamp: float

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
    side: str
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
    side: str
    price: float
    volume: float
    datetime: datetime

@dataclass
class PositionData:
    symbol: str
    volume: float
    price: float
    pnl: float = 0.0

@dataclass
class AccountData:
    balance: float
    equity: float
    available: float
    used_margin: float
    datetime: datetime

@dataclass
class ApiLimitData:
    """API 权重消耗数据"""
    weight_used_1m: int  # 每分钟已用权重
    timestamp: float

@dataclass
class AlertData:
    """报警内容"""
    level: str # INFO, WARNING, ERROR, CRITICAL
    msg: str
    timestamp: float