# file: main.py

import time
import json
import os
from rich.live import Live

from event.engine import EventEngine
from event.type import EVENT_LOG, EVENT_ORDERBOOK, EVENT_TRADE_UPDATE, EVENT_ORDER_UPDATE, EVENT_POSITION_UPDATE, EVENT_AGG_TRADE, EVENT_MARK_PRICE, EVENT_ACCOUNT_UPDATE
from gateway.binance_future import BinanceFutureGateway
from oms.main_oms import OMS # [NEW]
from risk.manager import RiskManager
from data.recorder import DataRecorder
from strategy.market_maker import MarketMakerStrategy
from ui.dashboard import TUIDashboard

from infrastructure.logger import logger
from infrastructure.time_service import time_service
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
    ref_data_manager.init(testnet=config["testnet"])

    # 2. UI & Engine
    dashboard = TUIDashboard()
    logger.set_ui_callback(dashboard.add_log)
    engine = EventEngine()
    
    # 3. [组件组装] Dependency Injection
    # 网关
    gateway = BinanceFutureGateway(engine, config["api_key"], config["api_secret"], testnet=config["testnet"])
    
    # OMS (持有 Gateway 用于撤单，持有 Engine 发送 AccountUpdate)
    oms_system = OMS(engine, gateway, config)
    
    # Risk (注入 OMS 用于 Pre-trade Check)
    risk = RiskManager(engine, config, oms=oms_system, gateway=gateway)
    
    # Strategy (注入 Risk，但不知道 OMS)
    strategy = MarketMakerStrategy(engine, gateway, risk)
    
    # 录制器
    recorder = DataRecorder(engine, config["symbols"]) if config.get("record_data") else None
    
    # 4. 事件绑定
    # 数据流
    engine.register(EVENT_ORDERBOOK, lambda e: data_cache.update_book(e.data))
    engine.register(EVENT_MARK_PRICE, lambda e: data_cache.update_mark_price(e.data))
    engine.register(EVENT_AGG_TRADE, lambda e: data_cache.update_trade(e.data))
    
    # 策略与UI
    last_tick_time = time.time()
    def on_book_update(ob):
        nonlocal last_tick_time
        last_tick_time = time.time()
        strategy.on_orderbook(ob)
        dashboard.update_market(ob)

    engine.register(EVENT_ORDERBOOK, lambda e: on_book_update(e.data))
    engine.register(EVENT_TRADE_UPDATE, lambda e: strategy.on_trade(e.data))
    engine.register(EVENT_ORDER_UPDATE, lambda e: strategy.on_order(e.data))
    engine.register(EVENT_POSITION_UPDATE, lambda e: [strategy.on_position(e.data), dashboard.update_position(e.data)])
    engine.register(EVENT_AGG_TRADE, lambda e: None)
    
    # [NEW] OMS 监听事件已在 OMS 内部注册 (构造函数中)，这里无需手动注册
    # 但是要注册 UI 监听 AccountUpdate
    # Dashboard 暂未实现 update_account，先占位
    engine.register(EVENT_ACCOUNT_UPDATE, lambda e: None)

    # 5. 启动
    engine.start()
    gateway.connect(config["symbols"])
    
    logger.info("System Started with Full Decoupled Architecture.")

    try:
        with Live(dashboard.render(), refresh_per_second=4) as live:
            while True:
                live.update(dashboard.render())
                time.sleep(0.1)
                
                if time.time() - last_tick_time > 30:
                    dashboard.add_log("[WARNING] 30s No Data...")
                    last_tick_time = time.time()
                    
    except KeyboardInterrupt:
        logger.info("Stopping...")
        if recorder: recorder.close()
        engine.stop()
        time_service.stop()
        oms_system.stop()
        logger.stop()

if __name__ == "__main__":
    main()