from types import SimpleNamespace

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
from infrastructure.runtime_bindings import (
    RuntimeEventBindings,
    RuntimeWatchdogState,
)


class _Engine:
    def __init__(self):
        self.registrations = []

    def register(self, event_type, handler):
        self.registrations.append(("default", event_type, handler))

    def register_market(self, event_type, handler):
        self.registrations.append(("market", event_type, handler))

    def register_execution(self, event_type, handler):
        self.registrations.append(("execution", event_type, handler))

    def register_cold(self, event_type, handler):
        self.registrations.append(("cold", event_type, handler))


class _LegacyEngine:
    def __init__(self):
        self.registrations = []

    def register_hot(self, event_type, handler):
        self.registrations.append(("hot", event_type, handler))

    def register_cold(self, event_type, handler):
        self.registrations.append(("cold", event_type, handler))


class _OMS:
    def __init__(self, calls):
        self.calls = calls

    def on_exchange_update(self, event):
        self.calls.append(("oms.exchange_order", event))

    def on_exchange_account_update(self, event):
        self.calls.append(("oms.exchange_account", event))

    def record_paper_order_event(self, payload):
        self.calls.append(("oms.paper_order", payload))

    def record_paper_account_sample(self, payload):
        self.calls.append(("oms.paper_account", payload))

    def record_paper_strategy_sample(self, payload):
        self.calls.append(("oms.paper_strategy", payload))

    def record_paper_system_event(self, event_type, payload):
        self.calls.append((f"oms.paper_system.{event_type}", payload))

    def record_paper_market_sample(self, payload):
        self.calls.append(("oms.paper_market", payload))


class _StrategyRuntime:
    def __init__(self, calls):
        self.calls = calls

    def on_orderbook(self, payload):
        self.calls.append(("strategy.orderbook", payload))

    def on_market_trade(self, payload):
        self.calls.append(("strategy.market_trade", payload))

    def on_order(self, payload):
        self.calls.append(("strategy.order", payload))

    def on_trade(self, payload):
        self.calls.append(("strategy.trade", payload))

    def on_position(self, payload):
        self.calls.append(("strategy.position", payload))

    def on_account_update(self, payload):
        self.calls.append(("strategy.account", payload))

    def on_system_health(self, payload):
        self.calls.append(("strategy.system_health", payload))


class _Dashboard:
    def __init__(self, calls):
        self.calls = calls

    def __getattr__(self, name):
        if name.startswith("update_") or name == "add_log":
            return lambda payload: self.calls.append((f"dashboard.{name}", payload))
        raise AttributeError(name)


class _Alerts:
    def __init__(self, calls):
        self.calls = calls

    def enqueue_event(self, payload):
        self.calls.append(("alerts.enqueue_event", payload))


class _MarketCache:
    def __init__(self):
        self.calls = []

    def update_book(self, payload):
        self.calls.append(("book", payload))

    def update_mark_price(self, payload):
        self.calls.append(("mark", payload))

    def update_trade(self, payload):
        self.calls.append(("trade", payload))


def _bindings(*, dashboard=True, alerts=True, perf_counter=lambda: 42.5):
    calls = []
    engine = _Engine()
    cache = _MarketCache()
    watchdog = RuntimeWatchdogState(last_tick_time=1.0, stale_watchdog_triggered=True)
    risk_controller = object()
    risk_supervisor = object()
    health_calls = []
    bindings = RuntimeEventBindings(
        engine=engine,
        oms=_OMS(calls),
        strategy_runtime=_StrategyRuntime(calls),
        risk_controller=risk_controller,
        risk_supervisor=risk_supervisor,
        web_dashboard=_Dashboard(calls) if dashboard else None,
        external_alerts=_Alerts(calls) if alerts else None,
        watchdog_state=watchdog,
        market_cache=cache,
        system_health_handler=lambda *args: health_calls.append(args),
        perf_counter=perf_counter,
    )
    return bindings, engine, cache, watchdog, calls, health_calls


def test_register_all_preserves_lane_and_handler_order():
    bindings, engine, *_ = _bindings()

    bindings.register_all()

    assert [
        (lane, event_type, handler.__name__)
        for lane, event_type, handler in engine.registrations
    ] == [
        ("market", EVENT_ORDERBOOK, "cache_orderbook"),
        ("market", EVENT_MARK_PRICE, "cache_mark_price"),
        ("market", EVENT_AGG_TRADE, "cache_market_trade"),
        ("market", EVENT_ORDERBOOK, "on_hot_tick"),
        ("execution", EVENT_EXCHANGE_ORDER_UPDATE, "on_exchange_update"),
        (
            "execution",
            EVENT_EXCHANGE_ACCOUNT_UPDATE,
            "on_exchange_account_update",
        ),
        ("execution", EVENT_SYSTEM_HEALTH, "on_system_health_execution"),
        ("cold", EVENT_ORDERBOOK, "on_orderbook_cold"),
        ("cold", EVENT_MARK_PRICE, "on_mark_price_cold"),
        ("cold", EVENT_AGG_TRADE, "on_market_trade_cold"),
        ("cold", EVENT_EXCHANGE_ORDER_UPDATE, "on_exchange_order_cold"),
        ("cold", EVENT_ORDER_UPDATE, "on_order_cold"),
        ("cold", EVENT_TRADE_UPDATE, "on_trade_cold"),
        ("cold", EVENT_POSITION_UPDATE, "on_position_cold"),
        ("cold", EVENT_ACCOUNT_UPDATE, "on_account_cold"),
        ("cold", EVENT_STRATEGY_UPDATE, "on_strategy_cold"),
        ("cold", EVENT_SYSTEM_HEALTH, "on_system_health_cold"),
        ("cold", EVENT_API_LIMIT, "on_api_limit_cold"),
        ("cold", EVENT_ALERT, "on_alert_cold"),
        ("cold", EVENT_LOG, "on_log_cold"),
    ]


def test_registration_is_single_use_and_supports_legacy_lane_api():
    bindings, *_ = _bindings()
    legacy_engine = _LegacyEngine()
    bindings.engine = legacy_engine

    bindings.register_all()

    assert len(legacy_engine.registrations) == 20
    assert [lane for lane, _, _ in legacy_engine.registrations[:7]] == [
        "hot",
        "hot",
        "hot",
        "hot",
        "hot",
        "hot",
        "hot",
    ]
    assert all(
        lane == "cold"
        for lane, _, _ in legacy_engine.registrations[7:]
    )

    try:
        bindings.register_all()
    except RuntimeError as exc:
        assert str(exc) == "runtime event bindings already registered"
    else:
        raise AssertionError("duplicate event registration was accepted")


def test_market_cache_and_hot_tick_state_are_explicit():
    bindings, _, cache, watchdog, *_ = _bindings()
    event = SimpleNamespace(data={"symbol": "BTCUSDT"})

    bindings.cache_orderbook(event)
    bindings.cache_mark_price(event)
    bindings.cache_market_trade(event)
    bindings.on_hot_tick(event)

    assert cache.calls == [
        ("book", event.data),
        ("mark", event.data),
        ("trade", event.data),
    ]
    assert watchdog.last_tick_time == 42.5
    assert watchdog.stale_watchdog_triggered is False


def test_system_health_execution_uses_explicit_collaborators():
    bindings, _, _, _, _, health_calls = _bindings()
    event = SimpleNamespace(data={"state": "degraded"})

    bindings.on_system_health_execution(event)

    assert health_calls == [
        (
            event,
            bindings.risk_controller,
            bindings.oms,
            bindings.risk_supervisor,
        )
    ]


def test_cold_callbacks_preserve_observation_strategy_and_output_order():
    bindings, _, _, _, calls, _ = _bindings()
    payload = {"id": "event-1"}
    event = SimpleNamespace(data=payload)

    bindings.on_orderbook_cold(event)
    bindings.on_market_trade_cold(event)
    bindings.on_order_cold(event)
    bindings.on_trade_cold(event)
    bindings.on_position_cold(event)
    bindings.on_account_cold(event)
    bindings.on_strategy_cold(event)
    bindings.on_system_health_cold(event)
    bindings.on_mark_price_cold(event)
    bindings.on_exchange_order_cold(event)
    bindings.on_api_limit_cold(event)
    bindings.on_alert_cold(event)
    bindings.on_log_cold(event)

    assert calls == [
        ("strategy.orderbook", payload),
        ("dashboard.update_market", payload),
        ("strategy.market_trade", payload),
        ("dashboard.update_market_trade", payload),
        ("oms.paper_order", payload),
        ("strategy.order", payload),
        ("dashboard.update_order", payload),
        ("strategy.trade", payload),
        ("dashboard.update_trade", payload),
        ("strategy.position", payload),
        ("dashboard.update_position", payload),
        ("oms.paper_account", payload),
        ("strategy.account", payload),
        ("dashboard.update_account", payload),
        ("oms.paper_strategy", payload),
        ("dashboard.update_strategy", payload),
        ("oms.paper_system.system_health", payload),
        ("strategy.system_health", payload),
        ("dashboard.update_system_health", payload),
        ("oms.paper_market", payload),
        ("dashboard.update_mark_price", payload),
        ("dashboard.update_exchange_order", payload),
        ("oms.paper_system.api_limit", payload),
        ("dashboard.update_api_limit", payload),
        ("oms.paper_system.alert", payload),
        ("dashboard.update_alert", payload),
        ("alerts.enqueue_event", payload),
        ("dashboard.add_log", payload),
    ]


def test_optional_dashboard_alerts_and_recorders_are_safe_to_omit():
    bindings, *_ = _bindings(dashboard=False, alerts=False)
    bindings.oms = object()
    event = SimpleNamespace(data={"state": "ok"})

    bindings.on_mark_price_cold(event)
    bindings.on_exchange_order_cold(event)
    bindings.on_api_limit_cold(event)
    bindings.on_alert_cold(event)
    bindings.on_log_cold(event)
    bindings.record_paper_observation("missing_method", event.data)
