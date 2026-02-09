# file: main.py

import time
import json
import os
import sys
from rich.live import Live

# ... (其他导入保持不变)
from infrastructure.logger import logger
from infrastructure.time_service import time_service
from data.ref_data import ref_data_manager
from data.cache import data_cache
from data.recorder import DataRecorder
from event.engine import EventEngine
from event.type import (
    EVENT_ORDERBOOK, EVENT_AGG_TRADE, EVENT_MARK_PRICE, 
    EVENT_ACCOUNT_UPDATE, EVENT_ORDER_UPDATE, EVENT_POSITION_UPDATE,
    EVENT_STRATEGY_UPDATE, EVENT_LOG, EVENT_EXCHANGE_ORDER_UPDATE, EVENT_ORDER_SUBMITTED
)
from gateway.binance_future import BinanceFutureGateway
from oms.engine import OMS
from risk.manager import RiskManager
from strategy.glft import GLFTStrategy
from ui.dashboard import TUIDashboard
from dashboard.aggregator import DashboardAggregator
from monitor.server import WebMonitor
from ops.alert import TelegramAlerter
# [NEW] 引入对账员
from ops.reconciler import AutoReconciler

def load_config():
    if not os.path.exists("config.json"):
        print("Fatal Error: config.json missing.")
        return None
    with open("config.json", "r") as f:
        return json.load(f)

def main():
    config = load_config()
    if not config: return

    # --- Step 1-3 初始化 (保持不变) ---
    logger.init_logging(config)
    time_service.start(testnet=config["testnet"])
    ref_data_manager.init(testnet=config["testnet"])
    dashboard_ui = TUIDashboard()
    logger.set_ui_callback(dashboard_ui.add_log)
    engine = EventEngine()
    gateway = BinanceFutureGateway(engine, config["api_key"], config["api_secret"], testnet=config["testnet"])
    oms_system = OMS(engine, gateway, config)
    risk = RiskManager(engine, config, oms=oms_system, gateway=gateway)
    strategy = GLFTStrategy(engine, oms_system)
    dash_aggregator = DashboardAggregator(oms_system, gateway, config)
    alerter = TelegramAlerter(engine, config)
    web_monitor = WebMonitor(engine, config)
    recorder = DataRecorder(engine, config["symbols"]) if config.get("record_data", False) else None

    # [NEW] 初始化自动对账员
    reconciler = AutoReconciler(oms_system, dash_aggregator, config)

    # --- Step 4 事件绑定 (保持不变) ---
    engine.register(EVENT_ORDERBOOK, lambda e: data_cache.update_book(e.data))
    engine.register(EVENT_MARK_PRICE, lambda e: data_cache.update_mark_price(e.data))
    engine.register(EVENT_AGG_TRADE, lambda e: data_cache.update_trade(e.data))
    
    last_tick_time = time.time()
    def on_tick_distribution(ob):
        nonlocal last_tick_time
        last_tick_time = time.time()
        strategy.on_orderbook(ob)

    engine.register(EVENT_ORDERBOOK, lambda e: on_tick_distribution(e.data))
    engine.register(EVENT_AGG_TRADE, lambda e: strategy.on_market_trade(e.data))
    engine.register(EVENT_EXCHANGE_ORDER_UPDATE, oms_system.on_exchange_update)
    engine.register(EVENT_ORDER_SUBMITTED, lambda e: oms_system.order_monitor.on_order_submitted(e))
    engine.register(EVENT_ORDER_UPDATE, lambda e: strategy.on_order(e.data))
    engine.register(EVENT_POSITION_UPDATE, lambda e: strategy.on_position(e.data))
    engine.register(EVENT_STRATEGY_UPDATE, lambda e: None)
    engine.register(EVENT_ACCOUNT_UPDATE, lambda e: None)

    # --- Step 5 启动 (保持不变) ---
    engine.start()
    gateway.connect(config["symbols"])
    logger.info("Waiting for Network Warm-up (3s)...")
    time.sleep(3)
    oms_system.sync_with_exchange()
    dash_aggregator.exch_view.cached_positions = oms_system.exposure.net_positions.copy()
    logger.info("ChronosHFT Full Stack Active.")

    # --- Step 6 主循环 ---
    try:
        with Live(dashboard_ui.layout, refresh_per_second=4, screen=True) as live:
            while True:
                # 1. 状态审计
                system_state = dash_aggregator.update()
                
                # 2. [NEW] 自动修复 (如果 Dirty 超过 5秒，自动触发 Sync)
                reconciler.check_and_fix()
                
                # 3. 渲染
                view = dashboard_ui.render(system_state)
                live.update(view)
                
                # 4. 健康检查
                if time.time() - last_tick_time > 30:
                    dashboard_ui.add_log("[CRITICAL] 30s Market Data Timeout!")
                
                time.sleep(0.1)

    except KeyboardInterrupt:
        logger.info("Interrupt received, shutting down...")
        if recorder: recorder.close()
        time_service.stop()
        oms_system.stop()
        engine.stop()
        logger.info("ChronosHFT Shutdown.")
        sys.exit(0)

if __name__ == "__main__":
    main()