# file: event/type.py

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, Optional, List, Any # [修复] 必须导入 Any

# ==========================================
# 1. 事件类型常量 (Event Types)
# ==========================================
EVENT_TICK = "eTick"
EVENT_ORDERBOOK = "eOrderBook"      # 订单簿快照
EVENT_AGG_TRADE = "eAggTrade"       # 逐笔成交
EVENT_MARK_PRICE = "eMarkPrice"     # 标记价格/资金费率
EVENT_ACCOUNT_UPDATE = "eAccountUpdate" # 账户资金变动

EVENT_LOG = "eLog"
EVENT_API_LIMIT = "eApiLimit"       # API 权重监控
EVENT_ALERT = "eAlert"              # 系统报警
EVENT_SYSTEM_HEALTH = "eSystemHealth" # 系统健康状态

# 交易流程事件
EVENT_ORDER_REQUEST = "eOrderRequest"     
EVENT_ORDER_SUBMITTED = "eOrderSubmitted" 
EVENT_ORDER_UPDATE = "eOrderUpdate"       
EVENT_TRADE_UPDATE = "eTradeUpdate"       
EVENT_POSITION_UPDATE = "ePositionUpdate" 
EVENT_EXCHANGE_ORDER_UPDATE = "eExchangeOrderUpdate" 
EVENT_STRATEGY_UPDATE = "eStrategyUpdate"

EVENT_BACKTEST_END = "eBacktestEnd"       

# ==========================================
# 2. 核心枚举与常量
# ==========================================

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

# 顶级系统生命周期
class LifecycleState(Enum):
    BOOTSTRAP = "BOOTSTRAP"     # 启动中 (拉取快照，建立连接)
    LIVE = "LIVE"               # 运行中 (处理单调事件流)
    HALTED = "HALTED"           # 熔断 (序列错误，不可恢复，需重启)
    RECONCILING = "RECONCILING" # 发现异常，正在对账 (暂停交易)

class SystemState(Enum):
    CLEAN = "CLEAN"        
    DIRTY = "DIRTY"        
    SYNCING = "SYNCING"    
    FROZEN = "FROZEN"      

# Time In Force
TIF_GTC = "GTC" 
TIF_IOC = "IOC" 
TIF_FOK = "FOK" 
TIF_GTX = "GTX" # Post Only

# 执行策略 (用于 OMS 区分逻辑)
class ExecutionPolicy(Enum):
    AGGRESSIVE = "AGGRESSIVE"
    PASSIVE = "PASSIVE"
    # RPI = "RPI" # [已删除]

# 兼容旧代码的状态字符串
Status_SUBMITTED = "SUBMITTED"
Status_PARTTRADED = "PARTTRADED"
Status_ALLTRADED = "ALLTRADED"
Status_CANCELLED = "CANCELLED"
Status_REJECTED = "REJECTED"

class OrderBookGapError(Exception):
    pass

# ==========================================
# 4. 基础事件对象
# ==========================================
@dataclass
class Event:
    type: str
    data: Any = None

# ==========================================
# 5. 交易指令与意图
# ==========================================

@dataclass
class OrderIntent:
    """策略发出的原始意图"""
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
    # [Removed] is_rpi

@dataclass
class OrderRequest:
    """发送给网关的具体请求"""
    symbol: str
    price: float
    volume: float
    side: str       
    order_type: str = "LIMIT"
    time_in_force: str = TIF_GTC 
    post_only: bool = False
    # [Removed] is_rpi

@dataclass
class CancelRequest:
    symbol: str
    order_id: str

@dataclass
class OrderSubmitted:
    req: OrderRequest 
    order_id: str
    timestamp: float

# ==========================================
# 6. 行情数据结构
# ==========================================

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

# ==========================================
# 7. 订单与成交回报
# ==========================================

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
    # [Removed] is_rpi

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

# ==========================================
# 8. 账户与持仓
# ==========================================

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

# ==========================================
# 9. 系统监控与策略状态
# ==========================================

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
    """
    策略状态广播 (用于 Dashboard 展示)
    """
    symbol: str
    fair_value: float
    alpha_bps: float
    
    # 兼容旧代码的显式字段 (可选，若策略不发则为默认值)
    gamma: float = 0.0
    k: float = 0.0
    A: float = 0.0
    sigma: float = 0.0
    
    # [关键修复] 动态参数字典 (Dashboard 解耦核心)
    params: Dict[str, Any] = field(default_factory=dict)
    
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())