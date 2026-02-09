# file: ui/dashboard.py

from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.console import Console
from rich.text import Text
from rich.align import Align
from dashboard.models import SystemStatus, DashboardState

class TUIDashboard:
    def __init__(self):
        self.console = Console()
        self.layout = Layout()
        
        # 布局：上(状态栏)，中(仓位表)，下(日志)
        self.layout.split(
            Layout(name="header", size=3),
            Layout(name="main", ratio=1),
            Layout(name="footer", size=10)
        )
        
        self.logs = []

    def add_log(self, msg):
        self.logs.append(msg)
        if len(self.logs) > 10: self.logs.pop(0)

    def render(self, state: DashboardState):
        """
        纯函数式渲染：输入 State -> 输出 UI
        """
        # 1. Header: 系统状态大字报
        status_color = "green"
        if state.status == SystemStatus.DIRTY: status_color = "yellow"
        if state.status == SystemStatus.DANGER: status_color = "red blink"
        
        header_text = Text(f"SYSTEM STATUS: {state.status.name} | Exposure: ${state.total_exposure:.0f}", style=f"bold {status_color}")
        self.layout["header"].update(Panel(Align.center(header_text), style=status_color))
        
        # 2. Main: 仓位核对表 (真理之表)
        table = Table(expand=True, title="Position Audit (Local vs Exchange)")
        table.add_column("Symbol")
        table.add_column("Local Qty", justify="right")
        table.add_column("Exch Qty", justify="right")
        table.add_column("Delta", justify="right") # 核心列
        table.add_column("Notional", justify="right")
        
        for row in state.positions:
            # Delta 高亮逻辑
            delta_style = "red bold" if row.is_dirty else "dim"
            delta_str = f"{row.delta_qty:.4f}" if row.is_dirty else "-"
            
            # Notional 高亮逻辑
            notional_style = "red blink" if row.is_danger else "green"
            
            table.add_row(
                row.symbol,
                f"{row.local_qty:.4f}",
                f"{row.exch_qty:.4f}",
                Text(delta_str, style=delta_style),
                Text(f"${row.notional:.0f}", style=notional_style)
            )
            
        # 附加：订单健康栏
        health = state.order_health
        health_text = f"Active Orders: {health.local_active} | Cancelling: {health.cancelling_count}"
        if health.cancelling_count > 5:
            health_text = f"[red blink]{health_text}[/]"
            
        self.layout["main"].update(Panel(table, subtitle=health_text))
        
        # 3. Footer: 日志
        self.layout["footer"].update(Panel("\n".join(self.logs), title="System Logs"))
        
        return self.layout
    
    # 兼容旧接口 (update_market 等不再用于渲染，只用于日志或其他用途，或者弃用)
    def update_market(self, *args): pass
    def update_position(self, *args): pass