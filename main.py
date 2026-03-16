import json
import os
import sys
import time

from rich.live import Live

from data.cache import data_cache
from data.recorder import DataRecorder
from data.ref_data import ref_data_manager
from event.engine import EventEngine
from event.type import (
    EVENT_ACCOUNT_UPDATE,
    EVENT_AGG_TRADE,
    EVENT_EXCHANGE_ACCOUNT_UPDATE,
    EVENT_EXCHANGE_ORDER_UPDATE,
    EVENT_MARK_PRICE,
    EVENT_ORDERBOOK,
    EVENT_ORDER_SUBMITTED,
    EVENT_ORDER_UPDATE,
    EVENT_POSITION_UPDATE,
    EVENT_STRATEGY_UPDATE,
    EVENT_SYSTEM_HEALTH,
    EVENT_TRADE_UPDATE,
)
from gateway.binance.gateway import BinanceGateway
from gateway.binance.truth_provider import BinanceTruthSnapshotProvider
from infrastructure.logger import logger
from infrastructure.system_health import handle_system_health_event
from infrastructure.time_service import time_service
from infrastructure.truth_monitor import TruthMonitor
from infrastructure.venue_supervisor import VenueSupervisor
from infrastructure.watchdog import (
    emit_event_engine_backlog_if_needed,
    emit_market_data_stale_if_needed,
    emit_strategy_runtime_backlog_if_needed,
)
from oms.engine import OMS
from risk.manager import RiskManager
from strategy.ml_sniper.ml_sniper import MLSniperStrategy
from strategy.runtime import StrategyRuntime
from ui.dashboard import TUIDashboard


def load_config():
    if not os.path.exists("config.json"):
        print("Error: config.json not found.")
        return None
    with open("config.json", "r", encoding="utf-8") as handle:
        return json.load(handle)


def main():
    config = load_config()
    if not config:
        return

    config["system"]["log_console"] = False
    logger.init_logging(config)

    event_engine_config = config.get("system", {}).get("event_engine", {})
    engine = EventEngine(event_engine_config)
    dashboard = TUIDashboard()
    logger.set_ui_callback(dashboard.add_log)

    market_data_config = config.get("system", {}).get("market_data", {})
    gateway = BinanceGateway(
        engine,
        config["api_key"],
        config["api_secret"],
        testnet=config["testnet"],
        market_data_config=market_data_config,
    )
    truth_provider = BinanceTruthSnapshotProvider(
        config["api_key"],
        config["api_secret"],
        testnet=config["testnet"],
    )
    oms_system = OMS(engine, gateway, config)
    risk_controller = RiskManager(engine, config, oms=oms_system, gateway=gateway)
    alpha_process_config = (
        config.get("system", {})
        .get("strategy_runtime", {})
        .get("alpha_process", {"enabled": True})
    )
    alpha_process_config = dict(alpha_process_config or {})
    alpha_process_config.setdefault("processes", min(4, max(1, len(config.get("symbols", [])))))
    strategy = MLSniperStrategy(engine, oms_system, alpha_process_config=alpha_process_config)
    strategy_runtime = StrategyRuntime(
        strategy,
        config.get("system", {}).get("strategy_runtime", {}),
        start_thread=False,
    )
    recorder = DataRecorder(engine, config["symbols"]) if config.get("record_data", False) else None
    truth_monitor = TruthMonitor(oms_system, truth_provider, config, start_thread=False)
    venue_supervisor = VenueSupervisor(oms_system, gateway, config, start_thread=False)

    def on_time_service_health(severity, reason, details):
        if severity == "freeze":
            oms_system.freeze_system(f"TimeSync: {reason}", cancel_active_orders=True)
            return
        if severity == "halt":
            risk_controller.trigger_kill_switch(f"TimeSync: {reason}")
            return
        if severity == "recovered" and oms_system.state.value == "FROZEN":
            if oms_system.last_freeze_reason.startswith("TimeSync:"):
                oms_system.trigger_reconcile("Time sync recovered")

    time_service.clear_listeners()
    time_service.configure(config.get("system", {}).get("time_sync", {}))
    time_service.register_listener(on_time_service_health)
    time_service.start(testnet=config["testnet"])
    ref_data_manager.init(testnet=config["testnet"])

    register_market = getattr(engine, "register_market", None)
    if not callable(register_market):
        register_market = getattr(engine, "register_hot", engine.register)
    register_execution = getattr(engine, "register_execution", None)
    if not callable(register_execution):
        register_execution = getattr(engine, "register_hot", engine.register)
    register_cold = getattr(engine, "register_cold", engine.register)

    register_market(EVENT_ORDERBOOK, lambda e: data_cache.update_book(e.data))
    register_market(EVENT_MARK_PRICE, lambda e: data_cache.update_mark_price(e.data))
    register_market(EVENT_AGG_TRADE, lambda e: data_cache.update_trade(e.data))

    main.last_tick_time = time.time()
    main.stale_watchdog_triggered = False
    main.event_engine_watchdog_state = {}
    main.strategy_runtime_watchdog_state = {}

    def on_hot_tick(_event):
        main.last_tick_time = time.time()
        main.stale_watchdog_triggered = False

    register_market(EVENT_ORDERBOOK, on_hot_tick)
    register_execution(EVENT_EXCHANGE_ORDER_UPDATE, oms_system.on_exchange_update)
    register_execution(EVENT_EXCHANGE_ACCOUNT_UPDATE, oms_system.on_exchange_account_update)
    register_execution(EVENT_ORDER_SUBMITTED, lambda e: oms_system.order_monitor.on_order_submitted(e))
    register_execution(
        EVENT_SYSTEM_HEALTH,
        lambda e: handle_system_health_event(e, risk_controller, oms_system),
    )

    register_cold(
        EVENT_ORDERBOOK,
        lambda e: [strategy_runtime.on_orderbook(e.data), dashboard.update_market(e.data)],
    )
    register_cold(EVENT_AGG_TRADE, lambda e: strategy_runtime.on_market_trade(e.data))
    register_cold(EVENT_ORDER_UPDATE, lambda e: strategy_runtime.on_order(e.data))
    register_cold(EVENT_TRADE_UPDATE, lambda e: strategy_runtime.on_trade(e.data))
    register_cold(
        EVENT_POSITION_UPDATE,
        lambda e: [strategy_runtime.on_position(e.data), dashboard.update_position(e.data)],
    )
    register_cold(
        EVENT_ACCOUNT_UPDATE,
        lambda e: [strategy_runtime.on_account_update(e.data), dashboard.update_account(e.data)],
    )
    register_cold(EVENT_STRATEGY_UPDATE, lambda e: dashboard.update_strategy(e.data))
    register_cold(EVENT_SYSTEM_HEALTH, lambda e: strategy_runtime.on_system_health(e.data))

    engine.start()
    strategy_runtime.start()
    gateway.connect(config["symbols"])

    time.sleep(3)
    oms_system.bootstrap()
    truth_monitor.start()
    venue_supervisor.start()

    logger.info("ChronosHFT Core Engine LIVE. (Minimalist Mode)")

    try:
        with Live(dashboard.render(), refresh_per_second=4, screen=True) as live:
            while True:
                live.update(dashboard.render())
                time.sleep(0.1)
                main.stale_watchdog_triggered = emit_market_data_stale_if_needed(
                    engine,
                    main.last_tick_time,
                    main.stale_watchdog_triggered,
                )
                main.event_engine_watchdog_state = emit_event_engine_backlog_if_needed(
                    engine,
                    oms_system,
                    getattr(gateway, "gateway_name", "UNKNOWN"),
                    main.event_engine_watchdog_state,
                    event_engine_config,
                )
                main.strategy_runtime_watchdog_state = emit_strategy_runtime_backlog_if_needed(
                    strategy_runtime,
                    oms_system,
                    strategy.name,
                    main.strategy_runtime_watchdog_state,
                    config.get("system", {}).get("strategy_runtime", {}),
                )
                dashboard.update_runtime_metrics(
                    {
                        "event_engine": engine.get_metrics_snapshot(),
                        "strategy_runtime": strategy_runtime.get_metrics_snapshot(),
                    }
                )
    except KeyboardInterrupt:
        logger.info("Shutdown signal received.")
        if recorder:
            recorder.close()
        venue_supervisor.stop()
        truth_monitor.stop()
        strategy_runtime.stop()
        truth_provider.close()
        time_service.stop()
        oms_system.stop()
        engine.stop()
        gateway.close()
        logger.info("ChronosHFT Shutdown Complete.")
        sys.exit(0)


if __name__ == "__main__":
    main()
