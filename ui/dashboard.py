# file: ui/dashboard.py

from rich.live import Live
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
        table = Table(title="持仓监控 (Positions)")
        table.add_column("Symbol")
        table.add_column("Dir")
        table.add_column("Vol")
        table.add_column("AvgPrice")
        
        for symbol, pos in self.positions.items():
            color = "green" if pos.direction == "LONG" else "red"
            # 过滤掉 0 持仓
            if pos.volume > 0:
                table.add_row(
                    symbol,
                    f"[{color}]{pos.direction}[/]",
                    f"{pos.volume:.4f}",
                    f"{pos.price:.2f}"
                )
        return Panel(table)

    def _generate_log_panel(self):
        text = "\n".join(self.logs)
        return Panel(text, title="系统日志 (Logs)")

    def render(self):
        """生成当前的视图布局"""
        self.layout["header"].update(Panel("Crypto HFT System - Step 6.5 Optimized", style="bold blue"))
        self.layout["market"].update(self._generate_market_table())
        self.layout["position"].update(self._generate_position_table())
        self.layout["log"].update(self._generate_log_panel())
        return self.layout