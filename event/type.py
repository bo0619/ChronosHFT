# file: event/type.py

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, Optional, List, Any


# ==========================================
# 1. 事件类型常量 (Event Types)
# ==========================================

# 行情数据事件
EVENT_TICK = "eTick"
EVENT_ORDERBOOK = "eOrderBook"          # 订单簿快照
EVENT_AGG_TRADE = "eAggTrade"           # 逐笔成交
EVENT_MARK_PRICE = "eMarkPrice"         # 标记价格/资金费率

# 交易流程事件
EVENT_ORDER_REQUEST = "eOrderRequest"       # 策略请求发单
EVENT_ORDER_SUBMITTED = "eOrderSubmitted"   # 订单已提交 (Post-Trade)
EVENT_ORDER_UPDATE = "eOrderUpdate"         # 订单状态更新 (OMS -> Strategy)
EVENT_TRADE_UPDATE = "eTradeUpdate"         # 成交回报
EVENT_POSITION_UPDATE = "ePositionUpdate"   # 持仓更新
EVENT_RPI_UPDATE = "eRpiUpdate"             # RPI 状态更新
EVENT_EXCHANGE_ORDER_UPDATE = "eExchangeOrderUpdate"  # 交易所订单更新 (Gateway -> OMS)

# 账户与策略事件
EVENT_ACCOUNT_UPDATE = "eAccountUpdate"     # 账户资金变动
EVENT_STRATEGY_UPDATE = "eStrategyUpdate"   # 策略决策事件

# 系统事件
EVENT_LOG = "eLog"
EVENT_API_LIMIT = "eApiLimit"               # API 权重监控
EVENT_ALERT = "eAlert"                      # 系统报警
EVENT_SYSTEM_HEALTH = "eSystemHealth"       # 系统健康与对账事件
EVENT_BACKTEST_END = "eBacktestEnd"         # 回测结束信号


# ==========================================
# 2. 核心枚举与常量 (Enums & Constants)
# ==========================================

# 买卖方向
class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


# 订单状态 (OMS 核心状态机)
class OrderStatus(Enum):
    CREATED = "CREATED"                     # 策略意图已生成
    REJECTED_LOCALLY = "REJECTED_LOCALLY"   # 风控或OMS拒绝
    SUBMITTING = "SUBMITTING"               # 正在发往交易所
    PENDING_ACK = "PENDING_ACK"             # 已发送，等待交易所确认
    NEW = "NEW"                             # 交易所已确认挂单
    PARTIALLY_FILLED = "PARTIALLY_FILLED"   # 部分成交
    FILLED = "FILLED"                       # 全部成交
    CANCELLING = "CANCELLING"               # 正在撤单
    CANCELLED = "CANCELLED"                 # 已撤单
    REJECTED = "REJECTED"                   # 交易所拒单
    EXPIRED = "EXPIRED"                     # 订单过期 (FOK/IOC)


# 执行策略枚举
class ExecutionPolicy(Enum):
    AGGRESSIVE = "AGGRESSIVE"   # 激进吃单 (Taker)
    PASSIVE = "PASSIVE"         # 普通挂单 (Maker)
    RPI = "RPI"                 # 零售价格优化 (Hidden Maker)


# Time In Force (有效方式)
TIF_GTC = "GTC"     # Good Till Cancel
TIF_IOC = "IOC"     # Immediate or Cancel
TIF_FOK = "FOK"     # Fill or Kill
TIF_GTX = "GTX"     # Post Only (Maker Only)
TIF_RPI = "RPI"     # RPI 专用


# 系统状态枚举
class SystemState(Enum):
    CLEAN = "CLEAN"         # 健康：本地状态 = 交易所，允许交易
    DIRTY = "DIRTY"         # 脏态：检测到不一致/超时，禁止交易
    SYNCING = "SYNCING"     # 同步中：正在重置状态，禁止交易
    FROZEN = "FROZEN"       # 冻结：多次同步失败，人工介入


# 网关连接状态
class GatewayState(Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    READY = "READY"         # 可以交易
    ERROR = "ERROR"


# 网关错误类型
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


# 为了兼容旧代码的字符串状态 (Gateway 原始回报)
Status_SUBMITTED = "SUBMITTED"
Status_PARTTRADED = "PARTTRADED"
Status_ALLTRADED = "ALLTRADED"
Status_CANCELLED = "CANCELLED"
Status_REJECTED = "REJECTED"


# ==========================================
# 3. 异常类 (Exceptions)
# ==========================================

class OrderBookGapError(Exception):
    """行情序列号中断异常"""
    pass


# ==========================================
# 4. 基础事件对象 (Base Event)
# ==========================================

@dataclass
class Event:
    type: str
    data: any = None  # type: ignore


# ==========================================
# 5. 交易指令与意图 (Trading Intent & Request)
# ==========================================

@dataclass
class OrderIntent:
    """
    策略发出的原始意图 (Strategy -> OMS)
    描述"我想做什么"，而不是"怎么做"
    """
    strategy_id: str
    symbol: str
    side: Side
    price: float
    volume: float
    order_type: str = "LIMIT"
    time_in_force: str = TIF_GTC
    is_post_only: bool = False
    is_rpi: bool = False
    policy: ExecutionPolicy = ExecutionPolicy.PASSIVE
    tag: str = ""       # 策略自定义标签，方便追踪


@dataclass
class OrderRequest:
    """
    发送给网关的具体请求
    """
    symbol: str
    price: float
    volume: float
    side: str           # "BUY" or "SELL"
    order_type: str = "LIMIT"
    time_in_force: str = TIF_GTC
    post_only: bool = False
    is_rpi: bool = False    # 是否为 RPI 订单
    client_oid: str = ''    # 客户端订单号


@dataclass
class CancelRequest:
    """撤单请求"""
    symbol: str
    order_id: str


@dataclass
class OrderSubmitted:
    """
    [Internal] 订单已发送通知 (Gateway -> OMS)
    用于触发掉单检测
    """
    req: OrderRequest   # 或 OrderIntent
    order_id: str
    timestamp: float


# ==========================================
# 6. 行情数据结构 (Market Data)
# ==========================================

@dataclass
class OrderBook:
    symbol: str
    exchange: str
    datetime: datetime
    # Key: Price (float), Value: Volume (float)
    asks: Dict[float, float] = field(default_factory=dict)
    bids: Dict[float, float] = field(default_factory=dict)

    def get_best_bid(self):
        if not self.bids:
            return 0.0, 0.0
        p = max(self.bids.keys())
        return p, self.bids[p]

    def get_best_ask(self):
        if not self.asks:
            return 0.0, 0.0
        p = min(self.asks.keys())
        return p, self.asks[p]


@dataclass
class RpiDepthData:
    symbol: str
    exchange: str
    datetime: datetime
    # RPI 的买卖盘通常比较稀疏，但也用 Dict 存储
    asks: Dict[float, float] = field(default_factory=dict)
    bids: Dict[float, float] = field(default_factory=dict)

    def get_best_bid(self):
        if not self.bids:
            return 0.0, 0.0
        p = max(self.bids.keys())
        return p, self.bids[p]

    def get_best_ask(self):
        if not self.asks:
            return 0.0, 0.0
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
# 7. 订单与成交回报 (Updates)
# ==========================================

@dataclass
class ExchangeOrderUpdate:
    """
    来自网关的原始回报
    """
    seq: int            # 严格单调递增序列号 (Gateway分配)
    client_oid: str     # 唯一主键
    exchange_oid: str   # 仅作为属性存储，不作为索引
    symbol: str
    status: str
    filled_qty: float
    filled_price: float
    cum_filled_qty: float
    update_time: float


@dataclass
class OrderStateSnapshot:
    """
    OMS 处理后的标准状态快照 (OMS -> Strategy/UI)
    """
    client_oid: str
    exchange_oid: str
    symbol: str
    status: OrderStatus     # 标准化枚举状态
    price: float
    volume: float
    filled_volume: float
    avg_price: float
    update_time: float
    is_rpi: bool = False


@dataclass
class OrderData:
    """
    订单状态
    """
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
    """成交明细"""
    symbol: str
    order_id: str
    trade_id: str
    side: str           # "BUY" or "SELL"
    price: float
    volume: float
    datetime: datetime


# ==========================================
# 8. 账户与持仓 (Account & Position)
# ==========================================

@dataclass
class PositionData:
    """
    净持仓数据 (单向持仓模式)
    """
    symbol: str
    volume: float       # 正数表示多头，负数表示空头
    price: float        # 持仓均价
    pnl: float = 0.0    # 预估浮动盈亏


@dataclass
class AccountData:
    """账户资产快照"""
    balance: float          # 余额
    equity: float           # 动态权益
    available: float        # 可用资金
    used_margin: float      # 占用保证金
    datetime: datetime


# ==========================================
# 9. 系统监控 (System Monitoring)
# ==========================================

@dataclass
class ApiLimitData:
    weight_used_1m: int
    timestamp: float

@dataclass
class StrategyData:
    symbol: str
    fair_value: float
    alpha_bps: float
    # [核心修改] 使用字典承载不同策略的特有参数
    # 例如: {"gamma": 0.1, "k": 1.5, "mode": "MM"}
    params: Dict[str, Any] = field(default_factory=dict) 
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())


@dataclass
class SystemHealthData:
    state: SystemState              # 当前状态
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


# ==========================================
# 10. 启动与生命周期 (Bootstrap & Lifecycle)
# ==========================================

@dataclass
class BootstrapEvent:
    """
    用于系统启动时注入初始状态
    """
    timestamp: float
    balance: float
    used_margin: float
    # 列表: [(symbol, net_volume, avg_price)]
    positions: List[tuple]