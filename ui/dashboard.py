# file: ui/dashboard.py

from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.console import Console
from datetime import datetime
from event.type import OrderBook, PositionData

class TUIDashboard:
    def __init__(self):
        self.console = Console()
        self.layout = Layout()
        self.layout.split(
            Layout(name="header", size=3),
            Layout(name="main", ratio=1),
            Layout(name="log", size=10)
        )
        self.layout["main"].split_row(
            Layout(name="market"),
            Layout(name="position")
        )
        
        # 数据缓存
        self.last_tick = None
        self.positions = {}
        self.logs = []
        self.max_logs = 8

    def update_market(self, tick: OrderBook):
        self.last_tick = tick

    def update_position(self, pos: PositionData):
        self.positions[pos.symbol] = pos

    def add_log(self, msg: str):
        # 简单的去重或者截断
        time_str = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{time_str}] {msg}")
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    def _generate_market_table(self):
        table = Table(title="实时行情 (Market)")
        table.add_column("Symbol")
        table.add_column("Bid Price", style="green")
        table.add_column("Ask Price", style="red")
        table.add_column("Time")
        
        if self.last_tick:
            bid, _ = self.last_tick.get_best_bid()
            ask, _ = self.last_tick.get_best_ask()
            table.add_row(
                self.last_tick.symbol,
                f"{bid:.2f}",
                f"{ask:.2f}",
                self.last_tick.datetime.strftime("%H:%M:%S")
            )
        return Panel(table)

    def _generate_position_table(self):
        table = Table(title="持仓监控 (Net Positions)")
        table.add_column("Symbol")
        table.add_column("Dir")
        table.add_column("Vol")
        table.add_column("AvgPrice")
        
        for symbol, pos in self.positions.items():
            # [修复] 根据 volume 正负判断方向
            if pos.volume > 0:
                direction = "LONG"
                color = "green"
            elif pos.volume < 0:
                direction = "SHORT"
                color = "red"
            else:
                continue # 不显示空仓位

            # 显示时使用绝对值 abs(pos.volume)
            table.add_row(
                symbol,
                f"[{color}]{direction}[/]",
                f"{abs(pos.volume):.4f}",
                f"{pos.price:.4f}"
            )
        return Panel(table)

    def _generate_log_panel(self):
        text = "\n".join(self.logs)
        return Panel(text, title="系统日志 (Logs)")

    def render(self):
        """生成当前的视图布局"""
        self.layout["header"].update(Panel("Crypto HFT System - Step 9 (One-Way Mode)", style="bold blue"))
        self.layout["market"].update(self._generate_market_table())
        self.layout["position"].update(self._generate_position_table())
        self.layout["log"].update(self._generate_log_panel())
        return self.layout