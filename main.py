# file: main.py

import time
import json
import os
import sys
from rich.live import Live

# 1. 基础设施与数据层
from infrastructure.logger import logger
from infrastructure.time_service import time_service
from data.ref_data import ref_data_manager
from data.cache import data_cache
from data.recorder import DataRecorder

# 2. 事件总线与核心定义
from event.engine import EventEngine
from event.type import (
    EVENT_ORDERBOOK, EVENT_AGG_TRADE, EVENT_MARK_PRICE, 
    EVENT_ACCOUNT_UPDATE, EVENT_ORDER_UPDATE, EVENT_POSITION_UPDATE,
    EVENT_STRATEGY_UPDATE, EVENT_LOG
)
from gateway.binance_future import EVENT_EXCHANGE_ORDER_UPDATE

# 3. 核心业务层 (Strategy -> OMS -> Gateway)
from gateway.binance_future import BinanceFutureGateway
from oms.engine import OMS
from risk.manager import RiskManager
from strategy.glft import GLFTStrategy

# 4. 状态审计 UI 层 (State Auditor)
from ui.dashboard import TUIDashboard
from dashboard.aggregator import DashboardAggregator
from monitor.server import WebMonitor
from ops.alert import TelegramAlerter

def load_config():
    if not os.path.exists("config.json"):
        print("Fatal Error: config.json missing.")
        return None
    with open("config.json", "r") as f:
        return json.load(f)

def main():
    # --- [Step 1] 环境准备 ---
    config = load_config()
    if not config: return

    # 初始化最底层基础设施
    logger.init_logging(config)
    time_service.start(testnet=config["testnet"])
    ref_data_manager.init(testnet=config["testnet"])

    # --- [Step 2] 组件组装 (遵循单向架构链) ---
    engine = EventEngine()
    
    # 1. Gateway (连接外部 Exchange)
    gateway = BinanceFutureGateway(engine, config["api_key"], config["api_secret"], testnet=config["testnet"])
    
    # 2. OMS (核心真理源 - 注入 Gateway)
    oms_system = OMS(engine, gateway, config)
    
    # 3. Risk (注入 OMS 用于资金校验)
    risk = RiskManager(engine, config, oms=oms_system, gateway=gateway)
    
    # 4. Strategy (注入 OMS - 遵循 Strategy -> OMS 架构)
    strategy = GLFTStrategy(engine, oms_system)
    
    # --- [Step 3] 监控与审计系统组装 ---
    
    # 初始化状态审计器 (Dashboard Aggregator)
    # 它持有 OMS 和 Gateway，以便核对两者的状态差异
    dash_aggregator = DashboardAggregator(oms_system, gateway, config)
    
    # 初始化 TUI 渲染层
    dashboard_ui = TUIDashboard()
    logger.set_ui_callback(dashboard_ui.add_log)
    
    # 启动运维报警与 Web 控制台
    alerter = TelegramAlerter(engine, config)
    web_monitor = WebMonitor(engine, config)
    
    # 数据录制器
    recorder = None
    if config.get("record_data", False):
        recorder = DataRecorder(engine, config["symbols"])

    # --- [Step 4] 事件总线连线 (Wiring) ---
    
    # A. 市场数据流
    engine.register(EVENT_ORDERBOOK, lambda e: data_cache.update_book(e.data))
    engine.register(EVENT_MARK_PRICE, lambda e: data_cache.update_mark_price(e.data))
    engine.register(EVENT_AGG_TRADE, lambda e: data_cache.update_trade(e.data))
    
    # B. 行情驱动策略与 UI (策略不持有状态，只对行情做反应)
    last_tick_time = time.time()
    def on_tick_distribution(ob):
        nonlocal last_tick_time
        last_tick_time = time.time()
        strategy.on_orderbook(ob) # 策略计算 -> 发送 OrderIntent 给 OMS
        # UI 更新已由 dashboard_aggregator 统一处理，此处不再分散更新

    engine.register(EVENT_ORDERBOOK, lambda e: on_tick_distribution(e.data))
    engine.register(EVENT_AGG_TRADE, lambda e: strategy.on_market_trade(e.data))
    
    # C. 交易回报流 (Closed Loop: Gateway -> OMS -> Strategy)
    # 网关原始更新 -> OMS 状态机更新
    engine.register(EVENT_EXCHANGE_ORDER_UPDATE, oms_system.on_exchange_update)
    
    # OMS 审计后快照 -> 策略 & UI
    engine.register(EVENT_ORDER_UPDATE, lambda e: strategy.on_order(e.data))
    engine.register(EVENT_POSITION_UPDATE, lambda e: strategy.on_position(e.data))
    
    # D. 策略内部参数流 -> UI
    engine.register(EVENT_STRATEGY_UPDATE, lambda e: None) # 由 WebMonitor 使用

    # --- [Step 5] 启动与同步 ---
    engine.start()
    gateway.connect(config["symbols"])
    
    logger.info("Waiting for Network Warm-up (3s)...")
    time.sleep(3)
    
    # 执行启动同步 (强制对齐本地与交易所状态)
    oms_system.sync_with_exchange()
    # 手动更新一次审计器的初始状态
    dash_aggregator.exch_view.cached_positions = oms_system.exposure.net_positions.copy()

    logger.info("ChronosHFT Full Stack Active. Strategy: GLFT_Adaptive")

    # --- [Step 6] 主循环: 状态审计与 UI 渲染 ---
    try:
        # 使用 Rich Live 模式渲染审计面板
        with Live(dashboard_ui.layout, refresh_per_second=4, screen=True) as live:
            while True:
                # 1. 运行状态审计 (核对 Local vs Exchange)
                # 这步是满足 "Stateful OrderBook" 和 "Position Audit" 的关键
                system_state = dash_aggregator.update()
                
                # 2. 渲染 UI 面板
                view = dashboard_ui.render(system_state)
                
                # 3. 刷新屏幕
                live.update(view)
                
                # 4. 健康检查
                if time.time() - last_tick_time > 30:
                    dashboard_ui.add_log("[CRITICAL] 30s Market Data Timeout!")
                    # 这里可以触发 risk.trigger_kill_switch()
                
                time.sleep(0.1)

    except KeyboardInterrupt:
        logger.info("Interrupt received, shutting down...")
        if recorder: recorder.close()
        
        # 释放所有资源
        time_service.stop()
        oms_system.stop()
        engine.stop()
        
        logger.info("ChronosHFT Shutdown. Alpha be with you.")
        sys.exit(0)

if __name__ == "__main__":
    main()