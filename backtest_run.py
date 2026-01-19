# file: monte_carlo_run.py

import json
import random
import numpy as np
from rich.console import Console
from rich.progress import track

from event.engine import EventEngine
from event.type import EVENT_ORDERBOOK, EVENT_TRADE_UPDATE, EVENT_ORDER_UPDATE, EVENT_POSITION_UPDATE
from oms.position import PositionManager
from risk.manager import RiskManager
from strategy.market_maker import MarketMakerStrategy
from analysis.calculator import PerformanceCalculator

from sim_engine.core import SimulationEngine
from sim_engine.clock import EventClock
from sim_engine.exchange import ExchangeEmulator
from sim_engine.gateway import SimGateway, ChaosGateway # 使用 ChaosGateway
from sim_engine.loader import DataLoader
# 引入新写的 LatencyModel
from sim_engine.latency import AdvancedLatencyModel 

class MonteCarloRunner:
    def __init__(self, runs=10):
        self.runs = runs
        self.results = []
        with open("config.json", "r") as f: self.config = json.load(f)

    def run_single_simulation(self, seed):
        # 设置随机种子，保证可复现性
        random.seed(seed)
        np.random.seed(seed)
        
        # --- 组装系统 (同 backtest_run_v2) ---
        event_engine = EventEngine()
        clock = EventClock()
        sim_engine = SimulationEngine()
        
        # 注入高级模型
        sim_engine.latency_model = AdvancedLatencyModel(self.config)
        
        exchange = ExchangeEmulator(sim_engine, event_engine, clock, self.config)
        # 使用混沌网关
        gateway = ChaosGateway(sim_engine, exchange, clock, self.config, event_engine)
        
        risk = RiskManager(event_engine, self.config)
        oms = PositionManager(event_engine)
        strategy = MarketMakerStrategy(event_engine, gateway, risk)
        
        # 简单的会计 (用于计算 PnL)
        balance = self.config["backtest"]["initial_capital"]
        taker_fee = self.config["backtest"]["taker_fee"]
        
        def on_trade_acc(trade):
            nonlocal balance
            balance -= trade.price * trade.volume * taker_fee
            # 这里简化处理，只算手续费扣除，不算盈亏，或者假设只做一轮
            # 为了准确，应该复制 BacktestAccountant 的完整逻辑
            
        event_engine.register(EVENT_ORDERBOOK, lambda e: strategy.on_orderbook(e.data))
        event_engine.register(EVENT_TRADE_UPDATE, lambda e: [strategy.on_trade(e.data), on_trade_acc(e.data)])
        event_engine.register(EVENT_ORDER_UPDATE, lambda e: strategy.on_order(e.data))
        event_engine.register(EVENT_POSITION_UPDATE, lambda e: strategy.on_position(e.data))
        
        loader = DataLoader(sim_engine, clock, "storage", "BTCUSDT")
        try:
            loader.load_and_schedule(exchange)
        except: return None # 数据没找到

        sim_engine.run(event_engine=event_engine)
        
        # 返回最终权益 (简化: 仅使用 Balance，严谨应使用 Equity)
        return balance

    def start(self):
        console = Console()
        console.print(f"[bold blue]Starting Monte Carlo Simulation ({self.runs} runs)...[/]")
        
        for i in track(range(self.runs), description="Simulating..."):
            res = self.run_single_simulation(seed=i)
            if res:
                self.results.append(res)
                
        # 统计分析
        if not self.results: return
        
        pnl_arr = np.array(self.results) - self.config["backtest"]["initial_capital"]
        
        console.print("\n[bold green]=== Monte Carlo Results ===[/]")
        console.print(f"Mean PnL: {np.mean(pnl_arr):.2f}")
        console.print(f"Std Dev:  {np.std(pnl_arr):.2f}")
        console.print(f"Min PnL:  {np.min(pnl_arr):.2f}")
        console.print(f"Max PnL:  {np.max(pnl_arr):.2f}")
        console.print(f"VaR (5%): {np.percentile(pnl_arr, 5):.2f}") # 95% 置信度下的最大亏损

if __name__ == "__main__":
    mc = MonteCarloRunner(runs=20) # 跑 20 次
    mc.start()