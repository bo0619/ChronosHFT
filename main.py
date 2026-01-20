# file: main.py

import time
import json
import os
from rich.live import Live

from event.engine import EventEngine
from event.type import EVENT_LOG, EVENT_ORDERBOOK, EVENT_TRADE_UPDATE, EVENT_ORDER_UPDATE, EVENT_POSITION_UPDATE, EVENT_AGG_TRADE
from gateway.binance_future import BinanceFutureGateway
from oms.position import PositionManager
from risk.manager import RiskManager
from data.recorder import DataRecorder
from strategy.market_maker import MarketMakerStrategy
from ui.dashboard import TUIDashboard
from infrastructure.logger import logger
from infrastructure.time_service import time_service


def load_config():
    if not os.path.exists("config.json"):
        print("错误: 找不到 config.json")
        return None
    with open("config.json", "r") as f:
        return json.load(f)

def main():
    config = load_config()
    if not config: return

    # 1. [NEW] 初始化基础设施
    # 必须最先初始化，后续组件可能会用到
    logger.init_logging(config)
    time_service.start(testnet=config["testnet"])

    # 2. 初始化 UI
    dashboard = TUIDashboard()

    # 3. 初始化核心组件
    # 实盘模式下，EventEngine 开启独立线程
    engine = EventEngine()
    
    # 注册日志推送到 UI
    engine.register(EVENT_LOG, lambda e: dashboard.add_log(e.data))
    
    # 初始化功能模块
    oms = PositionManager(engine)
    risk = RiskManager(engine, config)
    
    # 录制器 (可选)
    recorder = None
    if config.get("record_data", False):
        recorder = DataRecorder(engine, config["symbols"])
    
    # 网关 (连接币安)
    gateway = BinanceFutureGateway(
        engine, 
        config["api_key"], 
        config["api_secret"], 
        testnet=config["testnet"]
    )
    
    # 4. 加载策略
    # 使用最新的做市商策略，支持撤单重挂
    strategy = MarketMakerStrategy(engine, gateway, risk)
    
    # 5. 绑定事件
    # 行情 -> 策略 & UI
    engine.register(EVENT_ORDERBOOK, lambda e: [
        strategy.on_orderbook(e.data),
        dashboard.update_market(e.data)
    ])
    
    # 成交 -> 策略 & UI
    engine.register(EVENT_TRADE_UPDATE, lambda e: strategy.on_trade(e.data))
    
    # 订单状态 -> 策略
    engine.register(EVENT_ORDER_UPDATE, lambda e: strategy.on_order(e.data))
    
    # 仓位 -> 策略 & UI
    engine.register(EVENT_POSITION_UPDATE, lambda e: [
        strategy.on_position(e.data),
        dashboard.update_position(e.data)
    ])
    
    # 逐笔成交 -> 仅录制，策略暂不处理 (可选)
    engine.register(EVENT_AGG_TRADE, lambda e: None)

    # 6. 启动系统
    engine.start() # 启动事件分发线程
    gateway.connect(config["symbols"]) # 启动 WS 线程
    
    dashboard.add_log("实盘交易系统已启动...")

    # 7. UI 主循环
    try:
        with Live(dashboard.render(), refresh_per_second=4) as live:
            while True:
                live.update(dashboard.render())
                time.sleep(0.1)
    except KeyboardInterrupt:
        dashboard.add_log("正在停止系统...")
        if recorder: recorder.close()
        engine.stop()

if __name__ == "__main__":
    main()