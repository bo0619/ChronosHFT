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
from infrastructure.logger import logger
from infrastructure.system_health import handle_system_health_event
from infrastructure.time_service import time_service
from infrastructure.watchdog import emit_market_data_stale_if_needed
from oms.engine import OMS
from risk.manager import RiskManager
from strategy.ml_sniper.ml_sniper import MLSniperStrategy
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

    engine = EventEngine()
    dashboard = TUIDashboard()
    logger.set_ui_callback(dashboard.add_log)

    gateway = BinanceGateway(engine, config["api_key"], config["api_secret"], testnet=config["testnet"])
    oms_system = OMS(engine, gateway, config)
    risk_controller = RiskManager(engine, config, oms=oms_system, gateway=gateway)
    strategy = MLSniperStrategy(engine, oms_system)
    recorder = DataRecorder(engine, config["symbols"]) if config.get("record_data", False) else None

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

    engine.register(EVENT_ORDERBOOK, lambda e: data_cache.update_book(e.data))
    engine.register(EVENT_MARK_PRICE, lambda e: data_cache.update_mark_price(e.data))
    engine.register(EVENT_AGG_TRADE, lambda e: data_cache.update_trade(e.data))

    main.last_tick_time = time.time()
    main.stale_watchdog_triggered = False

    def on_tick(orderbook):
        main.last_tick_time = time.time()
        main.stale_watchdog_triggered = False
        strategy.on_orderbook(orderbook)
        dashboard.update_market(orderbook)

    engine.register(EVENT_ORDERBOOK, lambda e: on_tick(e.data))
    engine.register(EVENT_AGG_TRADE, lambda e: strategy.on_market_trade(e.data))

    engine.register(EVENT_EXCHANGE_ORDER_UPDATE, oms_system.on_exchange_update)
    engine.register(EVENT_EXCHANGE_ACCOUNT_UPDATE, oms_system.on_exchange_account_update)
    engine.register(EVENT_ORDER_SUBMITTED, lambda e: oms_system.order_monitor.on_order_submitted(e))

    engine.register(EVENT_ORDER_UPDATE, lambda e: strategy.on_order(e.data))
    engine.register(EVENT_TRADE_UPDATE, lambda e: strategy.on_trade(e.data))
    engine.register(EVENT_POSITION_UPDATE, lambda e: [strategy.on_position(e.data), dashboard.update_position(e.data)])
    engine.register(EVENT_ACCOUNT_UPDATE, lambda e: [strategy.on_account_update(e.data), dashboard.update_account(e.data)])
    engine.register(EVENT_STRATEGY_UPDATE, lambda e: dashboard.update_strategy(e.data))
    engine.register(EVENT_SYSTEM_HEALTH, lambda e: [strategy.on_system_health(e.data), handle_system_health_event(e, risk_controller)])

    engine.start()
    gateway.connect(config["symbols"])

    time.sleep(3)
    oms_system.bootstrap()

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
    except KeyboardInterrupt:
        logger.info("Shutdown signal received.")
        if recorder:
            recorder.close()
        time_service.stop()
        oms_system.stop()
        engine.stop()
        gateway.close()
        logger.info("ChronosHFT Shutdown Complete.")
        sys.exit(0)


if __name__ == "__main__":
    main()
