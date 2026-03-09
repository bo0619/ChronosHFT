from abc import ABC, abstractmethod

from event.type import Event, GatewayState
from event.type import (
    EVENT_EXCHANGE_ACCOUNT_UPDATE,
    EVENT_EXCHANGE_ORDER_UPDATE,
    EVENT_LOG,
)
from event.type import ExchangeAccountUpdate, ExchangeOrderUpdate


class BaseGateway(ABC):
    """Abstract base class for exchange gateways."""

    def __init__(self, event_engine, gateway_name: str):
        self.event_engine = event_engine
        self.gateway_name = gateway_name
        self.state = GatewayState.DISCONNECTED
        self.latency_stats = {
            "rest_rtt": 0.0,
            "ws_delay": 0.0,
        }

    @abstractmethod
    def connect(self, symbols: list):
        pass

    @abstractmethod
    def close(self):
        pass

    @abstractmethod
    def send_order(self, req) -> str:
        pass

    @abstractmethod
    def cancel_order(self, req):
        pass

    @abstractmethod
    def cancel_all_orders(self, symbol: str):
        pass

    @abstractmethod
    def get_account_info(self):
        pass

    @abstractmethod
    def get_all_positions(self):
        pass

    @abstractmethod
    def get_open_orders(self):
        pass

    @abstractmethod
    def get_depth_snapshot(self, symbol: str):
        pass

    def on_log(self, msg: str, level="INFO"):
        self.event_engine.put(Event(EVENT_LOG, f"[{self.gateway_name}] {msg}"))

    def on_order_update(self, update: ExchangeOrderUpdate):
        self.event_engine.put(Event(EVENT_EXCHANGE_ORDER_UPDATE, update))

    def on_account_update(self, update: ExchangeAccountUpdate):
        self.event_engine.put(Event(EVENT_EXCHANGE_ACCOUNT_UPDATE, update))

    def on_market_data(self, event_type: str, data):
        self.event_engine.put(Event(event_type, data))

    def set_state(self, state: GatewayState):
        self.state = state
        self.on_log(f"State Changed: {state.value}")
