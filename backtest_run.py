# file: backtest_run.py

import json
import os
import time
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from event.engine import EventEngine
from event.type import EVENT_ORDERBOOK, EVENT_TRADE_UPDATE, EVENT_ORDER_UPDATE, EVENT_POSITION_UPDATE
from event.type import EVENT_ORDER_SUBMITTED
from gateway.binance_future import EVENT_EXCHANGE_ORDER_UPDATE

from oms.engine import OMS
from risk.manager import RiskManager
from strategy.avellaneda_stoikov import AvellanedaStoikovStrategy # 使用最新策略
from analysis.calculator import PerformanceCalculator

from sim_engine.core import SimulationEngine
from sim_engine.clock import EventClock
from sim_engine.exchange import ExchangeEmulator
from sim_engine.gateway import ChaosGateway
from sim_engine.loader import DataLoader
from sim_engine.latency import AdvancedLatencyModel

def load_config():
    with open("config.json", "r") as f: return json.load(f)

class BacktestAccountant:
    def __init__(self, initial, fee):
        self.balance = initial
        self.fee = fee
        self.equity_record = []
    
    def on_trade(self, trade):
        # 简单记账用于最后出图
        cost = trade.price * trade.volume * self.fee
        self.balance -= cost
        # 实际盈亏由 OMS PnL 计算，这里仅做粗略记录
        self.equity_record.append({"datetime": trade.datetime, "balance": self.balance})

def main():
    config = load_config()
    console = Console()
    console.print("[bold green]>>> HFT Backtest (OMS Core) Starting...[/]")

    # 1. 基础
    event_engine = EventEngine()
    clock = EventClock()
    sim_engine = SimulationEngine()
    sim_engine.latency_model = AdvancedLatencyModel(config)
    
    # 2. 仿真层
    exchange = ExchangeEmulator(sim_engine, event_engine, clock, config)
    gateway = ChaosGateway(sim_engine, exchange, clock, config, event_engine)
    
    # 3. 业务层 (OMS & Risk & Strategy)
    oms_system = OMS(event_engine, gateway, config)
    risk = RiskManager(event_engine, config, oms=oms_system, gateway=gateway)
    strategy = AvellanedaStoikovStrategy(event_engine, oms_system, risk)
    
    # 4. 会计 (用于统计)
    accountant = BacktestAccountant(config["backtest"]["initial_capital"], config["backtest"]["taker_fee"])

    # 5. 事件绑定 (The Wiring)
    # 仿真回传 -> OMS
    event_engine.register(EVENT_EXCHANGE_ORDER_UPDATE, oms_system.on_exchange_update)
    
    # 策略/网关 -> OMS
    event_engine.register(EVENT_ORDER_SUBMITTED, lambda e: oms_system.order_monitor.on_order_submitted(e))
    
    # OMS -> 策略
    event_engine.register(EVENT_ORDER_UPDATE, lambda e: strategy.on_order(e.data))
    event_engine.register(EVENT_TRADE_UPDATE, lambda e: [strategy.on_trade(e.data), accountant.on_trade(e.data)])
    event_engine.register(EVENT_POSITION_UPDATE, lambda e: strategy.on_position(e.data))
    
    # Market Data -> Strategy & Exchange
    event_engine.register(EVENT_ORDERBOOK, lambda e: strategy.on_orderbook(e.data))

    # 6. 加载数据 & 运行
    loader = DataLoader(sim_engine, clock, "storage", "SOLUSDT")
    try:
        loader.load_and_schedule(exchange)
    except FileNotFoundError:
        console.print("[red]Data not found. Run main.py to record data first.[/]")
        return

    start_t = time.time()
    sim_engine.run(event_engine=event_engine)
    
    # 7. 报告
    console.print(f"\nTime Cost: {time.time() - start_t:.2f}s")
    
    if accountant.equity_record:
        calc = PerformanceCalculator([], config["backtest"]["initial_capital"])
        res = calc.calculate_from_equity(accountant.equity_record)
        t = Table(title="Backtest Result")
        t.add_column("Metric"); t.add_column("Value")
        t.add_row("Total PnL", f"{res.total_pnl:.2f}")
        t.add_row("Return", f"{(res.total_pnl/10000)*100:.2f}%")
        t.add_row("Sharpe", f"{res.sharpe_ratio:.2f}")
        t.add_row("Trades", str(res.total_trades))
        console.print(Panel(t))
    else:
        console.print("[yellow]No trades executed.[/]")

if __name__ == "__main__":
    main()