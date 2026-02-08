# file: main.py

import time
import json
import os
import sys
from rich.live import Live

# 1. 基础设施层 (Infrastructure)
from infrastructure.logger import logger
from infrastructure.time_service import time_service

# 2. 事件总线与数据类型 (Events & Types)
from event.engine import EventEngine
from event.type import EVENT_LOG, EVENT_ORDERBOOK, EVENT_TRADE_UPDATE, EVENT_ORDER_UPDATE 
from event.type import EVENT_POSITION_UPDATE, EVENT_AGG_TRADE, EVENT_MARK_PRICE, EVENT_ACCOUNT_UPDATE
from event.type import EVENT_ORDER_SUBMITTED, EVENT_STRATEGY_UPDATE
from gateway.binance_future import EVENT_EXCHANGE_ORDER_UPDATE

# 3. 核心业务模块 (Core Modules)
from gateway.binance_future import BinanceFutureGateway
from oms.engine import OMS # 引入 OMS 核心引擎
from risk.manager import RiskManager
from strategy.glft import GLFTStrategy # 引入 GLFT 策略

# 4. 数据与持久化 (Data Layer)
from data.recorder import DataRecorder
from data.ref_data import ref_data_manager
from data.cache import data_cache

# 5. 监控与运维 (Monitoring & Ops)
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

    # --- B. 基础设施初始化 (同步阻塞) ---
    logger.init_logging(config)
    
    logger.info("Initializing Time Service...")
    time_service.start(testnet=config["testnet"])
    
    logger.info("Initializing Reference Data...")
    ref_data_manager.init(testnet=config["testnet"])

    # --- C. 核心引擎与 UI ---
    dashboard = TUIDashboard()
    logger.set_ui_callback(dashboard.add_log) # 将日志挂载到 TUI
    
    engine = EventEngine() # 事件总线

    # --- D. 组件实例化与依赖注入 (Dependency Injection) ---
    
    # 1. 网关 (Gateway)
    # 此时只建立对象和 Session，尚未连接 WebSocket
    gateway = BinanceFutureGateway(engine, config["api_key"], config["api_secret"], testnet=config["testnet"])
    
    # 2. 订单管理系统 (OMS)
    # OMS 是系统的状态核心，持有 Gateway 用于执行，持有 Config 用于校验
    oms_system = OMS(engine, gateway, config)
    
    # 3. 全局风控 (Risk Manager)
    # 注入 OMS 用于检查资金/仓位，注入 Gateway 用于熔断撤单
    risk = RiskManager(engine, config, oms=oms_system, gateway=gateway)
    
    # 4. 策略 (Strategy)
    # 策略只依赖 OMS 发送意图，依赖 Engine 接收事件
    strategy = GLFTStrategy(engine, oms_system)
    
    # 5. 数据录制器 (Recorder)
    recorder = None
    if config.get("record_data", False):
        recorder = DataRecorder(engine, config["symbols"])

    # 6. 运维组件
    alerter = TelegramAlerter(engine, config)
    monitor = WebMonitor(engine, config)

    # --- E. 事件流绑定 (Event Wiring) ---
    
    # >> 1. 基础数据流 (Data Flow)
    # 更新内存缓存
    engine.register(EVENT_ORDERBOOK, lambda e: data_cache.update_book(e.data))
    engine.register(EVENT_MARK_PRICE, lambda e: data_cache.update_mark_price(e.data))
    engine.register(EVENT_AGG_TRADE, lambda e: data_cache.update_trade(e.data))
    
    # >> 2. 策略驱动流 (Strategy Driver)
    # 行情 -> 策略 & UI (增加心跳监测 Hook)
    last_tick_time = time.time()
    def on_market_data(ob):
        nonlocal last_tick_time
        last_tick_time = time.time()
        strategy.on_orderbook(ob)
        dashboard.update_market(ob)

    engine.register(EVENT_ORDERBOOK, lambda e: on_market_data(e.data))
    
    # [关键] 逐笔成交 -> 策略 (用于 GLFT 校准 A/k 参数)
    engine.register(EVENT_AGG_TRADE, lambda e: strategy.on_market_trade(e.data))
    
    # >> 3. 交易闭环流 (Trading Loop)
    
    # [Gateway -> OMS] 交易所原始回报
    engine.register(EVENT_EXCHANGE_ORDER_UPDATE, oms_system.on_exchange_update)
    
    # [Gateway -> OMS] 订单提交确认 (用于掉单检测)
    # 注意：现在 Strategy.send_intent -> OMS.submit_order -> Gateway.send_order
    # Gateway 发送成功后会触发 internal call 通知 OMS，但也保留事件监听以防万一
    engine.register(EVENT_ORDER_SUBMITTED, lambda e: oms_system.order_monitor.on_order_submitted(e))
    
    # [OMS -> Strategy/UI] 标准化订单状态更新
    engine.register(EVENT_ORDER_UPDATE, lambda e: strategy.on_order(e.data))
    
    # [OMS -> Strategy/UI] 自有成交回报
    engine.register(EVENT_TRADE_UPDATE, lambda e: strategy.on_trade(e.data))
    
    # [OMS -> Strategy/UI] 持仓更新
    engine.register(EVENT_POSITION_UPDATE, lambda e: [
        strategy.on_position(e.data),
        dashboard.update_position(e.data)
    ])
    
    # [OMS -> Web] 资金更新 (WebMonitor 自动监听 EVENT_ACCOUNT_UPDATE)
    
    # [Strategy -> UI] 策略内部参数可视化
    engine.register(EVENT_STRATEGY_UPDATE, lambda e: dashboard.update_strategy(e.data))

    # --- F. 系统启动序列 (Startup Sequence) ---
    logger.info("Starting Event Engine...")
    engine.start()
    
    logger.info(f"Connecting Gateway to {config['symbols']}...")
    gateway.connect(config["symbols"])
    
    # 等待 WebSocket 建立连接 (预留缓冲时间)
    logger.info("Waiting 3s for connection stabilization...")
    time.sleep(3)
    
    # [关键] 启动同步：拉取账户余额与持仓，初始化 OMS 状态
    oms_system.sync_with_exchange()
    
    logger.info("ChronosHFT System Started. Architecture: GLFT->OMS->Gateway")
    dashboard.add_log(f"Web Monitor: http://localhost:{config['system']['web_port']}")

    # --- G. UI 主循环 ---
    try:
        with Live(dashboard.render(), refresh_per_second=4) as live:
            while True:
                live.update(dashboard.render())
                time.sleep(0.1)
                
                # 心跳看门狗
                if time.time() - last_tick_time > 30:
                    dashboard.add_log("[WARNING] 30s No Data! Check Network connection.")
                    # 可以在此加入自动重启 Gateway 的逻辑
                    last_tick_time = time.time() 

    except KeyboardInterrupt:
        logger.info("Stopping System...")
        
        # 优雅退出
        if recorder: recorder.close()
        
        time_service.stop()
        oms_system.stop()
        engine.stop()
        
        logger.info("System Shutdown Complete.")
        sys.exit(0)

if __name__ == "__main__":
    main()