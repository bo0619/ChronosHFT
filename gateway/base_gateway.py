# file: gateway/base.py

from abc import ABC, abstractmethod
from enum import Enum
from datetime import datetime
from event.type import Event, GatewayState, GatewayError
from event.type import (
    EVENT_LOG, EVENT_ORDER_UPDATE, EVENT_TRADE_UPDATE, 
    EVENT_ORDERBOOK, EVENT_AGG_TRADE, EVENT_MARK_PRICE, 
    EVENT_API_LIMIT, EVENT_EXCHANGE_ORDER_UPDATE
)
from event.type import ExchangeOrderUpdate, TradeData, OrderBook

class BaseGateway(ABC):
    """
    交易所网关抽象基类
    """
    def __init__(self, event_engine, gateway_name: str):
        self.event_engine = event_engine
        self.gateway_name = gateway_name
        self.state = GatewayState.DISCONNECTED
        
        # 延迟统计
        self.latency_stats = {
            "rest_rtt": 0.0,
            "ws_delay": 0.0
        }

    # --- 必须实现的抽象接口 ---

    @abstractmethod
    def connect(self):
        """连接 REST 和 WebSocket"""
        pass

    @abstractmethod
    def close(self):
        """断开连接"""
        pass

    @abstractmethod
    def send_order(self, req) -> str:
        """发单，返回 exchange_order_id"""
        pass

    @abstractmethod
    def cancel_order(self, req):
        """撤单"""
        pass

    @abstractmethod
    def cancel_all_orders(self, symbol: str):
        """全撤"""
        pass

    # --- 查询接口 (同步/异步取决于实现，通常建议同步阻塞返回) ---

    @abstractmethod
    def get_account_info(self):
        """查询账户余额"""
        pass

    @abstractmethod
    def get_all_positions(self):
        """查询持仓"""
        pass

    @abstractmethod
    def get_open_orders(self):
        """查询挂单"""
        pass

    @abstractmethod
    def get_depth_snapshot(self, symbol: str):
        """查询深度快照"""
        pass

    # --- 通用辅助方法 (子类直接调用) ---

    def on_log(self, msg: str, level="INFO"):
        self.event_engine.put(Event(EVENT_LOG, f"[{self.gateway_name}] {msg}"))

    def on_order_update(self, update: ExchangeOrderUpdate):
        """推送标准化的订单回报给 OMS"""
        # 可以在这里做一层通用的预处理或日志
        self.event_engine.put(Event(EVENT_EXCHANGE_ORDER_UPDATE, update))

    def on_market_data(self, event_type: str, data: any):
        """推送行情"""
        self.event_engine.put(Event(event_type, data))

    def set_state(self, state: GatewayState):
        self.state = state
        self.on_log(f"State Changed: {state.value}")