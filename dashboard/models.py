# file: dashboard/models.py

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional
from datetime import datetime

class SystemStatus(Enum):
    CLEAN = "CLEAN"     # 数据一致，风险可控
    DIRTY = "DIRTY"     # 数据不一致 (Local != Exchange)
    DANGER = "DANGER"   # 风险超限 (Exposure > Limit)
    STALE = "STALE"     # 数据过期

@dataclass
class PositionRow:
    symbol: str
    # 仓位数量
    local_qty: float
    exch_qty: float
    delta_qty: float
    # 名义价值
    notional: float
    # 状态
    is_dirty: bool      # delta != 0
    is_danger: bool     # notional > limit

@dataclass
class OrderHealth:
    # 活跃订单数
    local_active: int
    exch_active: int
    # 异常状态
    cancelling_count: int # 卡在 Cancelling 的数量
    stuck_orders: int     # 长期无回报的订单数
    # 指标
    is_sync: bool         # local == exch

@dataclass
class DashboardState:
    """Dashboard 的唯一真理状态快照"""
    status: SystemStatus
    update_time: datetime
    
    # 模块 1: 仓位与风险
    positions: List[PositionRow] = field(default_factory=list)
    total_exposure: float = 0.0
    margin_usage: float = 0.0
    
    # 模块 2: 订单健康
    order_health: OrderHealth = field(default_factory=lambda: OrderHealth(0,0,0,0,True))
    
    # 模块 3: 执行质量 (可选)
    fill_ratio_1m: float = 0.0
    
    # 系统日志
    recent_logs: List[str] = field(default_factory=list)