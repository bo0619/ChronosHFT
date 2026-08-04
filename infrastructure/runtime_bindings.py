"""Explicit event bindings for the application runtime."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from data.cache import data_cache
from event.type import (
    EVENT_ACCOUNT_UPDATE,
    EVENT_AGG_TRADE,
    EVENT_ALERT,
    EVENT_API_LIMIT,
    EVENT_EXCHANGE_ACCOUNT_UPDATE,
    EVENT_EXCHANGE_ORDER_UPDATE,
    EVENT_LOG,
    EVENT_MARK_PRICE,
    EVENT_ORDERBOOK,
    EVENT_ORDER_UPDATE,
    EVENT_POSITION_UPDATE,
    EVENT_STRATEGY_UPDATE,
    EVENT_SYSTEM_HEALTH,
    EVENT_TRADE_UPDATE,
)
from infrastructure.system_health import handle_system_health_event


@dataclass(slots=True)
class RuntimeWatchdogState:
    last_tick_time: float = field(default_factory=time.perf_counter)
    stale_watchdog_triggered: bool = False
    event_engine: dict = field(default_factory=dict)
    strategy_runtime: dict = field(default_factory=dict)


class RuntimeEventBindings:
    """Bind runtime collaborators to event lanes without closure state."""

    def __init__(
        self,
        *,
        engine,
        oms,
        strategy_runtime,
        risk_controller,
        risk_supervisor,
        web_dashboard=None,
        external_alerts=None,
        watchdog_state: RuntimeWatchdogState | None = None,
        market_cache=data_cache,
        system_health_handler: Callable = handle_system_health_event,
        perf_counter: Callable[[], float] = time.perf_counter,
    ):
        self.engine = engine
        self.oms = oms
        self.strategy_runtime = strategy_runtime
        self.risk_controller = risk_controller
        self.risk_supervisor = risk_supervisor
        self.web_dashboard = web_dashboard
        self.external_alerts = external_alerts
        self.watchdog_state = watchdog_state or RuntimeWatchdogState()
        self.market_cache = market_cache
        self.system_health_handler = system_health_handler
        self.perf_counter = perf_counter
        self._registration_started = False

    def register_all(self) -> None:
        if self._registration_started:
            raise RuntimeError("runtime event bindings already registered")
        self._registration_started = True

        register_market = self._resolve_registration(
            "register_market",
            "register_hot",
            "register",
        )
        register_execution = self._resolve_registration(
            "register_execution",
            "register_hot",
            "register",
        )
        register_cold = self._resolve_registration("register_cold", "register")

        register_market(EVENT_ORDERBOOK, self.cache_orderbook)
        register_market(EVENT_MARK_PRICE, self.cache_mark_price)
        register_market(EVENT_AGG_TRADE, self.cache_market_trade)
        register_market(EVENT_ORDERBOOK, self.on_hot_tick)

        register_execution(
            EVENT_EXCHANGE_ORDER_UPDATE,
            self.oms.on_exchange_update,
        )
        register_execution(
            EVENT_EXCHANGE_ACCOUNT_UPDATE,
            self.oms.on_exchange_account_update,
        )
        register_execution(EVENT_SYSTEM_HEALTH, self.on_system_health_execution)

        register_cold(EVENT_ORDERBOOK, self.on_orderbook_cold)
        register_cold(EVENT_MARK_PRICE, self.on_mark_price_cold)
        register_cold(EVENT_AGG_TRADE, self.on_market_trade_cold)
        register_cold(
            EVENT_EXCHANGE_ORDER_UPDATE,
            self.on_exchange_order_cold,
        )
        register_cold(EVENT_ORDER_UPDATE, self.on_order_cold)
        register_cold(EVENT_TRADE_UPDATE, self.on_trade_cold)
        register_cold(EVENT_POSITION_UPDATE, self.on_position_cold)
        register_cold(EVENT_ACCOUNT_UPDATE, self.on_account_cold)
        register_cold(EVENT_STRATEGY_UPDATE, self.on_strategy_cold)
        register_cold(EVENT_SYSTEM_HEALTH, self.on_system_health_cold)
        register_cold(EVENT_API_LIMIT, self.on_api_limit_cold)
        register_cold(EVENT_ALERT, self.on_alert_cold)
        register_cold(EVENT_LOG, self.on_log_cold)

    def _resolve_registration(self, *method_names: str) -> Callable:
        for method_name in method_names:
            registration = getattr(self.engine, method_name, None)
            if callable(registration):
                return registration
        alternatives = ", ".join(method_names)
        raise AttributeError(
            f"event engine has no registration method: {alternatives}"
        )

    def cache_orderbook(self, event) -> None:
        self.market_cache.update_book(event.data)

    def cache_mark_price(self, event) -> None:
        self.market_cache.update_mark_price(event.data)

    def cache_market_trade(self, event) -> None:
        self.market_cache.update_trade(event.data)

    def on_hot_tick(self, _event) -> None:
        self.watchdog_state.last_tick_time = self.perf_counter()
        self.watchdog_state.stale_watchdog_triggered = False

    def on_system_health_execution(self, event) -> None:
        self.system_health_handler(
            event,
            self.risk_controller,
            self.oms,
            self.risk_supervisor,
        )

    def record_paper_observation(self, method_name: str, *args) -> None:
        recorder = getattr(self.oms, method_name, None)
        if callable(recorder):
            recorder(*args)

    def on_orderbook_cold(self, event) -> None:
        self.strategy_runtime.on_orderbook(event.data)
        self._update_dashboard("update_market", event.data)

    def on_market_trade_cold(self, event) -> None:
        self.strategy_runtime.on_market_trade(event.data)
        self._update_dashboard("update_market_trade", event.data)

    def on_order_cold(self, event) -> None:
        self.record_paper_observation("record_paper_order_event", event.data)
        self.strategy_runtime.on_order(event.data)
        self._update_dashboard("update_order", event.data)

    def on_trade_cold(self, event) -> None:
        self.strategy_runtime.on_trade(event.data)
        self._update_dashboard("update_trade", event.data)

    def on_position_cold(self, event) -> None:
        self.strategy_runtime.on_position(event.data)
        self._update_dashboard("update_position", event.data)

    def on_account_cold(self, event) -> None:
        self.record_paper_observation(
            "record_paper_account_sample",
            event.data,
        )
        self.strategy_runtime.on_account_update(event.data)
        self._update_dashboard("update_account", event.data)

    def on_strategy_cold(self, event) -> None:
        self.record_paper_observation(
            "record_paper_strategy_sample",
            event.data,
        )
        self._update_dashboard("update_strategy", event.data)

    def on_system_health_cold(self, event) -> None:
        self.record_paper_observation(
            "record_paper_system_event",
            "system_health",
            event.data,
        )
        self.strategy_runtime.on_system_health(event.data)
        self._update_dashboard("update_system_health", event.data)

    def on_alert_cold(self, event) -> None:
        self.record_paper_observation(
            "record_paper_system_event",
            "alert",
            event.data,
        )
        self._update_dashboard("update_alert", event.data)
        if self.external_alerts is not None:
            self.external_alerts.enqueue_event(event.data)

    def on_mark_price_cold(self, event) -> None:
        self.record_paper_observation(
            "record_paper_market_sample",
            event.data,
        )
        self._update_dashboard("update_mark_price", event.data)

    def on_exchange_order_cold(self, event) -> None:
        self._update_dashboard("update_exchange_order", event.data)

    def on_api_limit_cold(self, event) -> None:
        self.record_paper_observation(
            "record_paper_system_event",
            "api_limit",
            event.data,
        )
        self._update_dashboard("update_api_limit", event.data)

    def on_log_cold(self, event) -> None:
        self._update_dashboard("add_log", event.data)

    def _update_dashboard(self, method_name: str, payload) -> None:
        if self.web_dashboard is None:
            return
        getattr(self.web_dashboard, method_name)(payload)
