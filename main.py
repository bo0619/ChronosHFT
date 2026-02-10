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
    EVENT_ORDER_SUBMITTED
)
from gateway.binance_future import EVENT_EXCHANGE_ORDER_UPDATE

# 3. 核心业务模块
from gateway.binance_future import BinanceFutureGateway
from oms.engine import OMS
from risk.manager import RiskManager
from strategy.glft import GLFTStrategy 

# 4. 数据管理层
from data.recorder import DataRecorder
from data.ref_data import ref_data_manager
from data.cache import data_cache

# 5. 监控层 (TUI Dashboard)
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

    # 1. 异步日志系统 (关闭控制台输出，由 TUI 接管)
    config["system"]["log_console"] = False
    logger.init_logging(config)
    
    # 2. 时间同步 (NTP)
    time_service.start(testnet=config["testnet"])
    
    # 3. 参考数据同步 (获取 TickSize/LotSize 等硬约束)
    ref_data_manager.init(testnet=config["testnet"])

    # --- B. 初始化核心引擎与 UI ---
    engine = EventEngine()
    dashboard = TUIDashboard()
    
    # 将日志钩子挂载到 TUI
    logger.set_ui_callback(dashboard.add_log)

    # --- C. 组件组装 ---
    
    # 1. 网关 (REST + WebSocket)
    gateway = BinanceFutureGateway(engine, config["api_key"], config["api_secret"], testnet=config["testnet"])
    
    # 2. OMS (核心状态机，内部包含 Exposure, Account, OrderManager)
    # OMS 默认初始状态为 SystemState.DIRTY
    oms_system = OMS(engine, gateway, config)
    
    # 3. 全局风险控制器 (Overwatch)
    # 注入 OMS 用于资金校验，注入 Gateway 用于 Kill Switch 执行
    risk_controller = RiskManager(engine, config, oms=oms_system, gateway=gateway)
    
    # 4. 策略层 (GLFT 算法)
    # 策略只通过事件和 OMS 意图接口工作
    strategy = GLFTStrategy(engine, oms_system)
    
    # 5. 辅助支持
    recorder = DataRecorder(engine, config["symbols"]) if config.get("record_data") else None
    alerter = TelegramAlerter(engine, config)
    web_server = WebMonitor(engine, config) # 同时也提供 Web 端的 PnL 监控

    # --- D. 事件流绑定 (Wiring) ---
    
    # >> 1. 市场数据路径
    engine.register(EVENT_ORDERBOOK, lambda e: data_cache.update_book(e.data))
    engine.register(EVENT_MARK_PRICE, lambda e: data_cache.update_mark_price(e.data))
    engine.register(EVENT_AGG_TRADE, lambda e: data_cache.update_trade(e.data))
    
    # >> 2. 策略与 UI 驱动 (Tick-to-Trade)
    def on_tick(ob):
        main.last_tick_time = time.time()
        # 策略根据行情进行报价计算
        strategy.on_orderbook(ob)
        # TUI 行情看板更新
        dashboard.update_market(ob)

    main.last_tick_time = time.time()
    engine.register(EVENT_ORDERBOOK, lambda e: on_tick(e.data))
    
    # 逐笔成交 -> 策略学习 (GLFT 参数在线校准)
    engine.register(EVENT_AGG_TRADE, lambda e: strategy.on_market_trade(e.data))
    
    # >> 3. 交易状态机路径 (The Truth Loop)
    
    # 交易所原始回报 -> 进入 OMS 状态机 (唯一修改真值的入口)
    engine.register(EVENT_EXCHANGE_ORDER_UPDATE, oms_system.on_exchange_update)
    
    # 订单提交 -> OMS (用于 ACK 超时/掉单监测)
    engine.register(EVENT_ORDER_SUBMITTED, lambda e: oms_system.on_order_submitted(e))
    
    # OMS 状态快照 -> 策略 (通知策略订单成交/撤销)
    engine.register(EVENT_ORDER_UPDATE, lambda e: strategy.on_order(e.data))
    
    # OMS 持仓/资金更新 -> TUI 相应面板更新
    engine.register(EVENT_POSITION_UPDATE, lambda e: dashboard.update_position(e.data))
    engine.register(EVENT_ACCOUNT_UPDATE, lambda e: dashboard.update_account(e.data))
    
    # 系统健康报告 (对账结果) -> TUI 核心面板
    engine.register(EVENT_SYSTEM_HEALTH, lambda e: dashboard.update_health(e.data))
    
    # 策略 Alpha 内部参数 -> TUI 策略监控列
    engine.register(EVENT_STRATEGY_UPDATE, lambda e: dashboard.update_strategy(e.data))

    # --- E. 系统启动序列 ---
    
    # 1. 开启事件处理
    engine.start()
    
    # 2. 建立网络连接
    logger.info(f"Connecting Gateway to symbols: {config['symbols']}...")
    gateway.connect(config["symbols"])
    
    # 3. 等待连接稳定 (WebSocket 鉴权及订阅就绪)
    logger.info("Initializing WebSocket streams, please wait 3s...")
    time.sleep(3)
    
    # 4. [强制对齐] 首次全量同步
    # 此步骤会将系统从 DIRTY 变为 CLEAN
    oms_system.sync_with_exchange()
    
    logger.info("System fully operational. Monitoring for mismatches...")

    # --- F. TUI 实时渲染主循环 ---
    try:
        with Live(dashboard.render(), refresh_per_second=4, screen=True) as live:
            while True:
                live.update(dashboard.render())
                time.sleep(0.1)
                
                # 系统存活看门狗 (Heartbeat check)
                if time.time() - main.last_tick_time > 60:
                    logger.error("WATCHDOG: No market data for 60s! System may be stalled.")
                    main.last_tick_time = time.time() # 防止重复报错
                    
                # 熔断提示 (如果 Kill Switch 触发，通过 Dashboard 加强提示)
                if risk_controller.kill_switch_triggered:
                    dashboard.add_log(f"[bold red]!!! EMERGENCY STOP: {risk_controller.kill_reason} !!![/]")

    except KeyboardInterrupt:
        logger.info("Shutdown signal received (Ctrl+C).")
        
        # 优雅停机序列
        if recorder: recorder.close()
        
        oms_system.stop()      # 关闭对账和监控线程
        time_service.stop()    # 关闭 NTP 线程
        engine.stop()          # 关闭事件总线
        
        logger.info("All subsystems stopped. Clean shutdown.")
        sys.exit(0)

if __name__ == "__main__":
    main()