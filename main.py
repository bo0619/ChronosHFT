# file: main.py

import time
import json
import os
import threading
from rich.live import Live

# 1. 核心事件与引擎
from event.engine import EventEngine
from event.type import (
    EVENT_LOG, EVENT_ORDERBOOK, EVENT_TRADE_UPDATE, 
    EVENT_ORDER_UPDATE, EVENT_POSITION_UPDATE, 
    EVENT_AGG_TRADE, EVENT_MARK_PRICE, EVENT_ACCOUNT_UPDATE,
    EVENT_API_LIMIT, EVENT_ALERT
)

# 2. 网关模块
from gateway.binance_future import BinanceFutureGateway
from gateway.dry_run import DryRunGateway  # [NEW] 引入模拟网关

# 3. 业务逻辑模块
from oms.main_oms import OMS
from risk.manager import RiskManager
from data.recorder import DataRecorder
from strategy.market_maker import MarketMakerStrategy

# 4. 基础设施与数据
from infrastructure.logger import logger
from infrastructure.time_service import time_service
from data.ref_data import ref_data_manager
from data.cache import data_cache

# 5. 监控与运维
from ui.dashboard import TUIDashboard
from ops.alert import TelegramAlerter
from monitor.server import WebMonitor

def load_config():
    if not os.path.exists("config.json"):
        print("Error: config.json not found.")
        return None
    with open("config.json", "r") as f:
        return json.load(f)

def main():
    # --- 1. 加载配置 ---
    config = load_config()
    if not config: return

    # --- 2. 初始化基础设施 (最优先) ---
    # 日志、时间同步、参考数据
    logger.init_logging(config)
    
    # 根据是否是测试网启动时间服务
    is_testnet = config.get("testnet", True)
    time_service.start(testnet=is_testnet)
    
    # 拉取合约规则 (TickSize, LotSize 等) - 这是一个同步阻塞操作
    ref_data_manager.init(testnet=is_testnet)

    # --- 3. 初始化核心引擎与 UI ---
    dashboard = TUIDashboard()
    # 将 Logger 输出挂载到 Dashboard 上
    logger.set_ui_callback(dashboard.add_log)
    
    engine = EventEngine()

    # --- 4. 网关初始化 (Dry Run 核心逻辑) ---
    # A. 必须初始化真实的 Binance Gateway，用于获取实时行情
    real_gateway = BinanceFutureGateway(
        engine, 
        config["api_key"], 
        config["api_secret"], 
        testnet=is_testnet
    )
    
    # B. 根据模式选择“交易网关”
    mode = config.get("mode", "live")
    trade_gateway = None
    
    if mode == "dry_run":
        logger.info("⚠️  SYSTEM MODE: DRY RUN (Virtual Money, Real Data)")
        
        # 初始化虚拟网关
        trade_gateway = DryRunGateway(engine, config)
        
        # 覆写 Config 中的初始资金，以便 OMS 读取虚拟余额
        dry_run_balance = config.get("dry_run", {}).get("initial_balance", 10000.0)
        config["account"]["initial_balance_usdt"] = dry_run_balance
        
    else:
        logger.info("🚨 SYSTEM MODE: LIVE TRADING (Real Money)")
        trade_gateway = real_gateway

    # --- 5. 组装业务组件 (Dependency Injection) ---
    
    # OMS: 负责记账、订单生命周期。使用 trade_gateway 进行撤单操作
    oms_system = OMS(engine, trade_gateway, config)
    
    # Risk: 负责预风控、熔断。需要 trade_gateway 来执行 Cancel All
    risk = RiskManager(engine, config, oms=oms_system, gateway=trade_gateway)
    
    # Recorder: 负责数据录制 (始终记录真实行情)
    recorder = None
    if config.get("record_data", False):
        recorder = DataRecorder(engine, config["symbols"])
    
    # Strategy: 策略大脑。发送指令给 trade_gateway
    strategy = MarketMakerStrategy(engine, trade_gateway, risk)
    
    # Ops: 报警与 Web 监控
    alerter = TelegramAlerter(engine, config)
    monitor = WebMonitor(engine, config)

    # --- 6. 事件绑定 (Wiring) ---
    
    # A. 数据流 -> 缓存层
    engine.register(EVENT_ORDERBOOK, lambda e: data_cache.update_book(e.data))
    engine.register(EVENT_MARK_PRICE, lambda e: data_cache.update_mark_price(e.data))
    engine.register(EVENT_AGG_TRADE, lambda e: data_cache.update_trade(e.data))
    
    # B. 行情 -> 策略 & UI (增加心跳监测 Hook)
    last_tick_time = time.time()
    def on_book_update(event):
        nonlocal last_tick_time
        last_tick_time = time.time()
        
        ob = event.data
        # 驱动策略
        strategy.on_orderbook(ob)
        # 刷新 UI
        dashboard.update_market(ob)

    engine.register(EVENT_ORDERBOOK, on_book_update)
    
    # C. 交易回报 -> 策略
    engine.register(EVENT_TRADE_UPDATE, lambda e: strategy.on_trade(e.data))
    engine.register(EVENT_ORDER_UPDATE, lambda e: strategy.on_order(e.data))
    
    # D. 仓位更新 -> 策略 & UI
    engine.register(EVENT_POSITION_UPDATE, lambda e: [
        strategy.on_position(e.data),
        dashboard.update_position(e.data)
    ])
    
    # E. 账户/API/报警 -> 仅用于 Web/UI 显示或报警
    # (TelegramAlerter 和 WebMonitor 已经在内部注册了监听，这里无需额外绑定)

    # --- 7. 启动系统 ---
    
    # 启动事件分发线程
    engine.start()
    
    # 启动真实网关连接交易所 (开始接收行情)
    # 注意：无论 DryRun 还是 Live，都需要真实行情
    logger.info(f"Connecting to Exchange ({', '.join(config['symbols'])})...")
    real_gateway.connect(config["symbols"])
    
    if mode == "dry_run":
        dashboard.add_log(f"Dry Run Started. Balance: ${config['account']['initial_balance_usdt']}")
    
    web_port = config.get("system", {}).get("web_port", 8000)
    dashboard.add_log(f"Web Monitor: http://localhost:{web_port}")

    # --- 8. 主循环 (UI Render & Watchdog) ---
    try:
        # 刷新率 4fps 足够，太高会闪烁且占 CPU
        with Live(dashboard.render(), refresh_per_second=4) as live:
            while True:
                # 渲染 UI
                live.update(dashboard.render())
                
                # 心跳检测 (每 0.1s 检查一次)
                time.sleep(0.1)
                
                # 如果超过 30 秒没收到行情，且当前不是启动初期
                if time.time() - last_tick_time > 30:
                    dashboard.add_log("[WARNING] 30s No Market Data! Check Network.")
                    # 重置一下，避免疯狂刷屏
                    last_tick_time = time.time() 
                    
    except KeyboardInterrupt:
        logger.info("Shutdown Signal Received.")
        dashboard.add_log("Stopping System...")
        
        # 优雅退出
        if recorder: 
            recorder.close() # 强制刷盘
            
        time_service.stop()
        oms_system.stop()
        engine.stop()
        
        # 等待日志线程写完
        logger.stop()
        print("System Shutdown Complete.")

if __name__ == "__main__":
    main()