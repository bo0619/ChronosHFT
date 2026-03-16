from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

EVENT_TICK = "eTick"
EVENT_ORDERBOOK = "eOrderBook"
EVENT_AGG_TRADE = "eAggTrade"
EVENT_MARK_PRICE = "eMarkPrice"
EVENT_ACCOUNT_UPDATE = "eAccountUpdate"

EVENT_LOG = "eLog"
EVENT_API_LIMIT = "eApiLimit"
EVENT_ALERT = "eAlert"
EVENT_SYSTEM_HEALTH = "eSystemHealth"

EVENT_ORDER_REQUEST = "eOrderRequest"
EVENT_ORDER_SUBMITTED = "eOrderSubmitted"
EVENT_ORDER_UPDATE = "eOrderUpdate"
EVENT_TRADE_UPDATE = "eTradeUpdate"
EVENT_POSITION_UPDATE = "ePositionUpdate"
EVENT_EXCHANGE_ORDER_UPDATE = "eExchangeOrderUpdate"
EVENT_EXCHANGE_ACCOUNT_UPDATE = "eExchangeAccountUpdate"
EVENT_STRATEGY_UPDATE = "eStrategyUpdate"

EVENT_BACKTEST_END = "eBacktestEnd"


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(Enum):
    CREATED = "CREATED"
    REJECTED_LOCALLY = "REJECTED_LOCALLY"
    SUBMITTING = "SUBMITTING"
    PENDING_ACK = "PENDING_ACK"
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class GatewayState(Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    READY = "READY"
    ERROR = "ERROR"


class GatewayError(Enum):
    NETWORK_ERROR = "NETWORK_ERROR"
    API_ERROR = "API_ERROR"
    RATE_LIMIT = "RATE_LIMIT"
    AUTH_ERROR = "AUTH_ERROR"
    SERVER_OVERLOAD = "SERVER_OVERLOAD"
    UNKNOWN = "UNKNOWN"


class LifecycleState(Enum):
    BOOTSTRAP = "BOOTSTRAP"
    LIVE = "LIVE"
    FROZEN = "FROZEN"
    RECONCILING = "RECONCILING"
    HALTED = "HALTED"


class OMSCapabilityMode(Enum):
    LIVE = "LIVE"
    DEGRADED = "DEGRADED"
    PASSIVE_ONLY = "PASSIVE_ONLY"
    CANCEL_ONLY = "CANCEL_ONLY"
    READ_ONLY = "READ_ONLY"
    LOCKDOWN = "LOCKDOWN"


class SystemState(Enum):
    CLEAN = "CLEAN"
    DIRTY = "DIRTY"
    SYNCING = "SYNCING"
    FROZEN = "FROZEN"


TIF_GTC = "GTC"
TIF_IOC = "IOC"
TIF_FOK = "FOK"
TIF_GTX = "GTX"


class ExecutionPolicy(Enum):
    AGGRESSIVE = "AGGRESSIVE"
    PASSIVE = "PASSIVE"


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
    data: Any = None


@dataclass
class OrderIntent:
    strategy_id: str
    symbol: str
    side: Side
    price: float
    volume: float
    order_type: str = "LIMIT"
    time_in_force: str = TIF_GTC
    is_post_only: bool = False
    policy: ExecutionPolicy = ExecutionPolicy.PASSIVE
    tag: str = ""


@dataclass
class OrderRequest:
    symbol: str
    price: float
    volume: float
    side: str
    order_type: str = "LIMIT"
    time_in_force: str = TIF_GTC
    post_only: bool = False
    reduce_only: bool = False


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
class OrderSubmitResult:
    accepted: bool
    client_oid: str = ""
    reason: str = ""
    state: str = ""


@dataclass
class OrderBook:
    symbol: str
    exchange: str
    datetime: datetime
    asks: Dict[float, float] = field(default_factory=dict)
    bids: Dict[float, float] = field(default_factory=dict)
    exchange_timestamp: float = 0.0
    received_timestamp: float = 0.0

    def get_best_bid(self):
        if not self.bids:
            return 0.0, 0.0
        price = max(self.bids.keys())
        return price, self.bids[price]

    def get_best_ask(self):
        if not self.asks:
            return 0.0, 0.0
        price = min(self.asks.keys())
        return price, self.asks[price]


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
class ExchangeOrderUpdate:
    client_oid: str
    exchange_oid: str
    symbol: str
    status: str
    filled_qty: float
    filled_price: float
    cum_filled_qty: float
    update_time: float
    seq: int = 0
    commission: Optional[float] = None
    commission_asset: str = ""
    realized_pnl: Optional[float] = None
    is_maker: Optional[bool] = None


@dataclass
class OrderStateSnapshot:
    client_oid: str
    exchange_oid: str
    symbol: str
    status: OrderStatus
    price: float
    volume: float
    filled_volume: float
    avg_price: float
    update_time: float
    error_msg: str = ""


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
class ExchangeAccountUpdate:
    asset: str
    wallet_balance: float
    available_balance: Optional[float] = None
    balances: Dict[str, Dict[str, Optional[float]]] = field(default_factory=dict)
    positions: Dict[str, Dict[str, float]] = field(default_factory=dict)
    reason: str = ""
    event_time: float = 0.0


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
    balances: Dict[str, float] = field(default_factory=dict)
    available_balances: Dict[str, float] = field(default_factory=dict)


@dataclass
class ApiLimitData:
    weight_used_1m: int
    timestamp: float


@dataclass
class AlertData:
    level: str
    msg: str
    timestamp: float


@dataclass
class SystemHealthData:
    state: SystemState
    total_exposure: float
    margin_ratio: float
    pos_diffs: Dict[str, tuple]
    order_count_local: int
    order_count_remote: int
    is_sync_error: bool
    cancelling_count: int
    fill_ratio: float
    api_weight: int
    timestamp: float


@dataclass
class StrategyData:
    symbol: str
    fair_value: float
    alpha_bps: float
    params: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())
