# file: main.py

import time
import json
import os
import sys
from rich.live import Live

# 1. 基础设施层
from infrastructure.logger import logger
from infrastructure.time_service import time_service

# 2. 事件总线与数据类型
from event.engine import EventEngine
from event.type import (
    EVENT_LOG, EVENT_ORDERBOOK, EVENT_TRADE_UPDATE, EVENT_ORDER_UPDATE,
    EVENT_POSITION_UPDATE, EVENT_AGG_TRADE, EVENT_MARK_PRICE, 
    EVENT_ACCOUNT_UPDATE, EVENT_STRATEGY_UPDATE, EVENT_SYSTEM_HEALTH,
    EVENT_ORDER_SUBMITTED,
    EVENT_EXCHANGE_ORDER_UPDATE # [修复] 从 event.type 导入，而不是从 gateway 导入
)

# 3. 核心业务模块
from gateway.binance.gateway import BinanceGateway
from oms.engine import OMS
from risk.manager import RiskManager
from strategy.predictive_glft import PredictiveGLFTStrategy # 使用最新的预测型策略

# 4. 数据管理层
from data.recorder import DataRecorder
from data.ref_data import ref_data_manager
from data.cache import data_cache

# 5. 监控与运维层
from ui.dashboard import TUIDashboard
from monitor.server import WebMonitor
from ops.alert import TelegramAlerter

def load_config():
    if not os.path.exists("config.json"):
        print("Error: config.json not found.")
        return None
    with open("config.json", "r") as f:
        return json.load(f)

def main():
    # --- A. 初始化配置与基础设施 ---
    config = load_config()
    if not config: return

    # 1. 异步日志系统
    config["system"]["log_console"] = False
    logger.init_logging(config)
    
    # 2. 时间同步
    time_service.start(testnet=config["testnet"])
    
    # 3. 参考数据同步
    ref_data_manager.init(testnet=config["testnet"])

    # --- B. 初始化核心引擎与 UI ---
    engine = EventEngine()
    dashboard = TUIDashboard()
    
    # 将日志钩子挂载到 TUI
    logger.set_ui_callback(dashboard.add_log)

    # --- C. 组件组装 ---
    
    # 1. 网关
    gateway = BinanceGateway(engine, config["api_key"], config["api_secret"], testnet=config["testnet"]) # [NEW]
    # 2. OMS
    oms_system = OMS(engine, gateway, config)
    
    # 3. 全局风控
    risk_controller = RiskManager(engine, config, oms=oms_system, gateway=gateway)
    
    # 4. 策略 (Predictive GLFT)
    strategy = PredictiveGLFTStrategy(engine, oms_system)
    
    # 5. 辅助模块
    recorder = DataRecorder(engine, config["symbols"]) if config.get("record_data") else None
    alerter = TelegramAlerter(engine, config)
    web_server = WebMonitor(engine, config)

    # --- D. 核心事件流绑定 ---
    
    # >> 1. 市场数据流
    engine.register(EVENT_ORDERBOOK, lambda e: data_cache.update_book(e.data))
    engine.register(EVENT_MARK_PRICE, lambda e: data_cache.update_mark_price(e.data))
    engine.register(EVENT_AGG_TRADE, lambda e: data_cache.update_trade(e.data))
    
    # >> 2. 策略驱动流
    def on_tick(ob):
        main.last_tick_time = time.time()
        strategy.on_orderbook(ob)
        dashboard.update_market(ob)

    main.last_tick_time = time.time()
    engine.register(EVENT_ORDERBOOK, lambda e: on_tick(e.data))
    
    # 市场成交流驱动模型学习
    engine.register(EVENT_AGG_TRADE, lambda e: strategy.on_market_trade(e.data))
    
    # >> 3. 交易闭环
    
    # 网关原始回报 -> OMS 状态机
    engine.register(EVENT_EXCHANGE_ORDER_UPDATE, oms_system.on_exchange_update)
    
    # 订单提交 -> OMS (掉单检测)
    engine.register(EVENT_ORDER_SUBMITTED, lambda e: oms_system.on_order_submitted(e))
    
    # OMS 结果 -> 策略 & UI
    engine.register(EVENT_ORDER_UPDATE, lambda e: strategy.on_order(e.data))
    engine.register(EVENT_TRADE_UPDATE, lambda e: strategy.on_trade(e.data))
    engine.register(EVENT_POSITION_UPDATE, lambda e: dashboard.update_position(e.data))
    engine.register(EVENT_ACCOUNT_UPDATE, lambda e: dashboard.update_account(e.data))
    
    # 策略内部参数 -> UI
    engine.register(EVENT_STRATEGY_UPDATE, lambda e: dashboard.update_strategy(e.data))
    
    # 系统健康报告 -> UI
    engine.register(EVENT_SYSTEM_HEALTH, lambda e: dashboard.update_health(e.data))

    # --- E. 启动流程 ---
    logger.info("Starting Event Engine...")
    engine.start()
    
    logger.info(f"Connecting Gateway to symbols: {config['symbols']}...")
    gateway.connect(config["symbols"])
    
    # 预留连接时间
    time.sleep(3)
    
    # 强制同步
    oms_system.sync_with_exchange()
    
    logger.info("ChronosHFT Full Stack Ready.")

    # --- F. 主循环 ---
    try:
        with Live(dashboard.render(), refresh_per_second=4, screen=True) as live:
            while True:
                live.update(dashboard.render())
                time.sleep(0.1)
                
                # 看门狗
                if time.time() - main.last_tick_time > 60:
                    logger.warn("SYSTEM WATCHDOG: No market data for 60s!")
                    main.last_tick_time = time.time() 
                    
                # 熔断提示
                if risk_controller.kill_switch_triggered:
                    dashboard.add_log(f"[bold red]!!! EMERGENCY STOP: {risk_controller.kill_reason} !!![/]")

    except KeyboardInterrupt:
        logger.info("Shutdown signal received.")
        if recorder: recorder.close()
        time_service.stop()
        oms_system.stop()
        engine.stop()
        logger.info("Clean shutdown finished.")
        sys.exit(0)

if __name__ == "__main__":
    main()