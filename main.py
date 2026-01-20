# file: main.py

import time
import json
import os
from rich.live import Live

from event.engine import EventEngine
from event.type import EVENT_LOG, EVENT_ORDERBOOK, EVENT_TRADE_UPDATE, EVENT_ORDER_UPDATE, EVENT_POSITION_UPDATE, EVENT_AGG_TRADE, EVENT_MARK_PRICE
from gateway.binance_future import BinanceFutureGateway
from oms.position import PositionManager
from risk.manager import RiskManager
from data.recorder import DataRecorder
from strategy.market_maker import MarketMakerStrategy
from ui.dashboard import TUIDashboard

# 基础设施
from infrastructure.logger import logger
from infrastructure.time_service import time_service

# [NEW] 数据管理层
from data.ref_data import ref_data_manager
from data.cache import data_cache

def load_config():
    if not os.path.exists("config.json"): return None
    with open("config.json", "r") as f: return json.load(f)

def main():
    config = load_config()
    if not config: return

    # 1. 基础设施
    logger.init_logging(config)
    time_service.start(testnet=config["testnet"])
    
    # 2. [NEW] 初始化参考数据 (同步阻塞，必须先完成)
    ref_data_manager.init(testnet=config["testnet"])

    # 3. 核心组件
    dashboard = TUIDashboard()
    logger.set_ui_callback(dashboard.add_log)
    
    engine = EventEngine()
    oms = PositionManager(engine)
    risk = RiskManager(engine, config)
    
    # 录制器 (HDF5版)
    recorder = DataRecorder(engine, config["symbols"]) if config.get("record_data") else None
    
    gateway = BinanceFutureGateway(engine, config["api_key"], config["api_secret"], testnet=config["testnet"])
    strategy = MarketMakerStrategy(engine, gateway, risk)
    
    # 4. 事件绑定
    
    # 绑定 LiveDataCache (缓存层)
    engine.register(EVENT_ORDERBOOK, lambda e: data_cache.update_book(e.data))
    engine.register(EVENT_MARK_PRICE, lambda e: data_cache.update_mark_price(e.data))
    engine.register(EVENT_AGG_TRADE, lambda e: data_cache.update_trade(e.data))
    
    # 绑定策略 & UI
    engine.register(EVENT_ORDERBOOK, lambda e: [strategy.on_orderbook(e.data), dashboard.update_market(e.data)])
    engine.register(EVENT_TRADE_UPDATE, lambda e: strategy.on_trade(e.data))
    engine.register(EVENT_ORDER_UPDATE, lambda e: strategy.on_order(e.data))
    engine.register(EVENT_POSITION_UPDATE, lambda e: [strategy.on_position(e.data), dashboard.update_position(e.data)])
    
    # 5. 启动
    engine.start()
    gateway.connect(config["symbols"])
    
    logger.info("System Started with HDF5 Recorder & In-Memory Cache.")

    try:
        with Live(dashboard.render(), refresh_per_second=4) as live:
            while True:
                live.update(dashboard.render())
                time.sleep(0.1)
    except KeyboardInterrupt:
        logger.info("Stopping...")
        if recorder: recorder.close() # 退出时强制 Flush
        engine.stop()
        time_service.stop()
        logger.stop()

if __name__ == "__main__":
    main()