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
from event.type import EVENT_LOG, EVENT_ORDERBOOK, EVENT_TRADE_UPDATE, EVENT_ORDER_UPDATE 
from event.type import EVENT_POSITION_UPDATE, EVENT_AGG_TRADE, EVENT_MARK_PRICE, EVENT_ACCOUNT_UPDATE
from event.type import EVENT_ORDER_SUBMITTED 
from gateway.binance_future import EVENT_EXCHANGE_ORDER_UPDATE # 关键事件

# 3. 核心业务模块
from gateway.binance_future import BinanceFutureGateway
from oms.engine import OMS # [修改] 引用新的 OMS Engine
from risk.manager import RiskManager
from strategy.market_maker import MarketMakerStrategy

# 4. 数据与持久化
from data.recorder import DataRecorder
from data.ref_data import ref_data_manager
from data.cache import data_cache

# 5. 监控与运维
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
    # --- A. 配置加载 ---
    config = load_config()
    if not config: return

    # --- B. 基础设施初始化 ---
    logger.init_logging(config)
    time_service.start(testnet=config["testnet"])
    ref_data_manager.init(testnet=config["testnet"])

    # --- C. 核心引擎与UI ---
    dashboard = TUIDashboard()
    logger.set_ui_callback(dashboard.add_log)
    
    engine = EventEngine() 

    # --- D. 组件实例化与依赖注入 ---
    
    # 1. 网关
    gateway = BinanceFutureGateway(engine, config["api_key"], config["api_secret"], testnet=config["testnet"])
    oms_system = OMS(engine, gateway, config)
    
    # 3. 风控 (注入 OMS 用于预交易资金检查)
    risk = RiskManager(engine, config, oms=oms_system, gateway=gateway)
    
    # [修改] Strategy 只注入 Engine 和 OMS
    strategy = MarketMakerStrategy(engine, oms_system)
    # 如果策略需要直接调用 OMS 的某些高级接口（如 submit_order），也可以注入 oms_system
    # 但根据目前架构，策略通过 Gateway 发送 Request，Gateway 返回 ID，
    # 然后策略通过 EVENT_ORDER_SUBMITTED 通知 OMS 纳入管理。
    
    # 5. 数据录制器
    recorder = None
    if config.get("record_data", False):
        recorder = DataRecorder(engine, config["symbols"])

    # 6. 运维监控
    alerter = TelegramAlerter(engine, config)
    monitor = WebMonitor(engine, config)

    # --- E. 事件流绑定 (The Wiring) ---
    
    # >> 1. 数据流 (Data Flow)
    engine.register(EVENT_ORDERBOOK, lambda e: data_cache.update_book(e.data))
    engine.register(EVENT_MARK_PRICE, lambda e: data_cache.update_mark_price(e.data))
    engine.register(EVENT_AGG_TRADE, lambda e: data_cache.update_trade(e.data))
    
    # 策略与 UI 接收行情
    last_tick_time = time.time()
    def on_market_data(ob):
        nonlocal last_tick_time
        last_tick_time = time.time()
        strategy.on_orderbook(ob)
        dashboard.update_market(ob)

    engine.register(EVENT_ORDERBOOK, lambda e: on_market_data(e.data))
    
    # >> 2. 交易流 (Trade Flow - The Closed Loop)
    
    # [Gateway -> OMS] 
    # 只有 OMS 才有资格处理来自交易所的原始回报
    engine.register(EVENT_EXCHANGE_ORDER_UPDATE, oms_system.on_exchange_update)
    
    # [Gateway/Strategy -> OMS] 
    # 订单发送成功后，通知 OMS 建立档案 (用于掉单检测)
    # 注意：这个事件目前在 Strategy.send_order_safe 中发出
    engine.register(EVENT_ORDER_SUBMITTED, lambda e: oms_system.order_monitor.on_order_submitted(e))
    
    # [OMS -> Strategy/UI] 
    # OMS 处理完状态机后，广播标准化的 OrderUpdate 和 TradeUpdate
    engine.register(EVENT_ORDER_UPDATE, lambda e: strategy.on_order(e.data))
    engine.register(EVENT_TRADE_UPDATE, lambda e: strategy.on_trade(e.data))
    
    engine.register(EVENT_ACCOUNT_UPDATE, lambda e: dashboard.update_account(e.data))
    # [OMS -> Strategy/UI] 持仓更新
    engine.register(EVENT_POSITION_UPDATE, lambda e: [
        strategy.on_position(e.data),
        dashboard.update_position(e.data)
    ])
    
    # [OMS -> UI] 资金更新
    engine.register(EVENT_ACCOUNT_UPDATE, lambda e: dashboard.update_account(e.data))
    # --- F. 启动系统 ---
    engine.start() 
    gateway.connect(config["symbols"]) 
    
    # 等待 WebSocket 链路稳定 (User Data Stream 需要一点时间同步)
    logger.info("Initializing connection, please wait 3s...")
    time.sleep(3) 
    
    # [核心] 执行账户与持仓的深度同步
    # 这会覆盖掉 config.json 里的 initial_balance
    oms_system.sync_with_exchange()
    
    logger.info("ChronosHFT Engine Ready. Starting Strategy...")
    # --- G. 主循环 ---
    try:
        with Live(dashboard.render(), refresh_per_second=4) as live:
            while True:
                live.update(dashboard.render())
                time.sleep(0.1)
                
                # 心跳监控
                if time.time() - last_tick_time > 30:
                    dashboard.add_log("[WARNING] 30s No Data! Check Network.")
                    last_tick_time = time.time() 

    except KeyboardInterrupt:
        logger.info("Stopping System...")
        if recorder: recorder.close()
        
        # 停止所有子系统
        time_service.stop()
        oms_system.stop()
        engine.stop()
        
        logger.info("System Shutdown Complete.")
        sys.exit(0)

if __name__ == "__main__":
    main()