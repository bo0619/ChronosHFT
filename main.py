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
    EVENT_ACCOUNT_UPDATE, EVENT_STRATEGY_UPDATE,
    EVENT_ORDER_SUBMITTED, EVENT_EXCHANGE_ORDER_UPDATE
)

# 3. 核心业务模块
from gateway.binance.gateway import BinanceGateway
from oms.engine import OMS
from risk.manager import RiskManager
from strategy.hybrid_glft.hybrid_glft import HybridGLFTStrategy
from strategy.ml_sniper.ml_sniper import MLSniperStrategy

# 4. 数据管理层
from data.recorder import DataRecorder
from data.ref_data import ref_data_manager
from data.cache import data_cache

# 5. UI
from ui.dashboard import TUIDashboard

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

    # 1. 基础设施启动
    config["system"]["log_console"] = False
    logger.init_logging(config)
    time_service.start(testnet=config["testnet"])
    ref_data_manager.init(testnet=config["testnet"])

    # --- B. 初始化核心引擎与 UI ---
    engine = EventEngine()
    dashboard = TUIDashboard()
    logger.set_ui_callback(dashboard.add_log)

    # --- C. 组件组装 ---
    
    # 1. 网关
    gateway = BinanceGateway(engine, config["api_key"], config["api_secret"], testnet=config["testnet"])
    
    # 2. OMS (核心状态机)
    oms_system = OMS(engine, gateway, config)
    
    # 3. 全局风控 (持有 OMS)
    risk_controller = RiskManager(engine, config, oms=oms_system, gateway=gateway)
    
    # 4. 策略 (Hybrid GLFT)
    strategy = MLSniperStrategy(engine, oms_system)
    
    # 5. 数据录制器
    recorder = DataRecorder(engine, config["symbols"]) if config.get("record_data", False) else None

    # --- D. 核心事件流绑定 ---
    
    # >> 1. 市场数据缓存
    engine.register(EVENT_ORDERBOOK, lambda e: data_cache.update_book(e.data))
    engine.register(EVENT_MARK_PRICE, lambda e: data_cache.update_mark_price(e.data))
    engine.register(EVENT_AGG_TRADE, lambda e: data_cache.update_trade(e.data))
    
    # >> 2. 策略与 UI 驱动
    main.last_tick_time = time.time()
    def on_tick(ob):
        main.last_tick_time = time.time()
        strategy.on_orderbook(ob)
        dashboard.update_market(ob)

    engine.register(EVENT_ORDERBOOK, lambda e: on_tick(e.data))
    engine.register(EVENT_AGG_TRADE, lambda e: strategy.on_market_trade(e.data))
    
    # >> 3. 交易流闭环
    # Gateway -> OMS
    engine.register(EVENT_EXCHANGE_ORDER_UPDATE, oms_system.on_exchange_update)
    
    # Strategy -> OMS (提交记录)
    engine.register(EVENT_ORDER_SUBMITTED, lambda e: oms_system.order_monitor.on_order_submitted(e))
    
    # OMS -> Strategy / UI
    engine.register(EVENT_ORDER_UPDATE, lambda e: strategy.on_order(e.data))
    engine.register(EVENT_TRADE_UPDATE, lambda e: strategy.on_trade(e.data))
    engine.register(EVENT_POSITION_UPDATE, lambda e: [
        strategy.on_position(e.data),
        dashboard.update_position(e.data)
    ])
    engine.register(EVENT_ACCOUNT_UPDATE, lambda e: dashboard.update_account(e.data))
    engine.register(EVENT_STRATEGY_UPDATE, lambda e: dashboard.update_strategy(e.data))

    # --- E. 启动流程 ---
    engine.start()
    gateway.connect(config["symbols"])
    
    # 预留连接与同步时间
    time.sleep(3)
    oms_system.bootstrap()
    
    logger.info("ChronosHFT Core Engine LIVE. (Minimalist Mode)")

    # --- F. 主循环 (TUI 渲染) ---
    try:
        with Live(dashboard.render(), refresh_per_second=4, screen=True) as live:
            while True:
                live.update(dashboard.render())
                time.sleep(0.1)
                
                # 系统看门狗
                if time.time() - main.last_tick_time > 60:
                    logger.warning("SYSTEM WATCHDOG: Data stream unresponsive for 60s.")
                    main.last_tick_time = time.time() 

    except KeyboardInterrupt:
        logger.info("Shutdown signal received.")
        if recorder: recorder.close()
        time_service.stop()
        oms_system.stop()
        engine.stop()
        gateway.close()
        logger.info("ChronosHFT Shutdown Complete.")
        sys.exit(0)

if __name__ == "__main__":
    main()