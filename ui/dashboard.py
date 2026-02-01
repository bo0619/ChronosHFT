# file: ui/dashboard.py

from datetime import datetime
from collections import defaultdict
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.console import Console
from rich.text import Text
from event.type import OrderBook, PositionData, AccountData

class TUIDashboard:
    def __init__(self):
        self.console = Console()
        
        # 布局初始化: 顶部账户，中部多币种表，底部日志
        self.layout = Layout()
        self.layout.split(
            Layout(name="header", size=3),  # 账户摘要
            Layout(name="body", ratio=1),   # 多币种核心监控
            Layout(name="footer", size=10)  # 日志
        )
        
        # --- 数据缓存 ---
        # Key: Symbol
        self.market_cache = {}   # {symbol: {bid: float, ask: float}}
        self.position_cache = {} # {symbol: {vol: float, price: float}}
        
        # 账户缓存
        self.account_info = {
            "balance": 0.0,
            "equity": 0.0,
            "margin": 0.0,
            "available": 0.0
        }
        
        # 日志缓存
        self.logs = []
        self.max_logs = 8

    # --- 数据更新接口 ---

    def update_market(self, ob: OrderBook):
        """更新行情快照"""
        bid, _ = ob.get_best_bid()
        ask, _ = ob.get_best_ask()
        
        self.market_cache[ob.symbol] = {
            "bid": bid,
            "ask": ask,
            "time": ob.datetime
        }

    def update_position(self, pos: PositionData):
        """更新持仓信息"""
        self.position_cache[pos.symbol] = {
            "vol": pos.volume,
            "price": pos.price
        }

    def update_account(self, acc: AccountData):
        """更新账户资金"""
        self.account_info = {
            "balance": acc.balance,
            "equity": acc.equity,
            "margin": acc.used_margin,
            "available": acc.available
        }

    def add_log(self, msg: str):
        """添加日志"""
        time_str = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{time_str}] {msg}")
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    # --- 渲染逻辑 ---

    def _render_header(self):
        """顶部：账户资金概览"""
        acc = self.account_info
        
        # 动态颜色：权益 > 余额显示绿色，否则红色
        color = "green" if acc["equity"] >= acc["balance"] else "red"
        
        summary = (
            f"[bold]Equity:[/bold] [{color}]{acc['equity']:.2f}[/] | "
            f"[bold]Balance:[/bold] {acc['balance']:.2f} | "
            f"[bold]Used Margin:[/bold] {acc['margin']:.2f} | "
            f"[bold]Available:[/bold] {acc['available']:.2f}"
        )
        return Panel(summary, title="Account Overview", style="bold white")

    def _render_body(self):
        """中部：多币种聚合监控表"""
        table = Table(expand=True, box=None)
        
        table.add_column("Symbol", style="cyan", width=10)
        table.add_column("Market (Bid / Ask)", justify="center")
        table.add_column("Net Position", justify="right")
        table.add_column("Entry Price", justify="right")
        table.add_column("Unrealized PnL", justify="right")

        # 获取所有出现过的 Symbol (并集)
        all_symbols = set(self.market_cache.keys()) | set(self.position_cache.keys())
        
        for symbol in sorted(list(all_symbols)):
            # 1. 获取行情
            mkt = self.market_cache.get(symbol, {"bid": 0, "ask": 0})
            mid_price = (mkt['bid'] + mkt['ask']) / 2 if (mkt['bid'] and mkt['ask']) else 0
            mkt_str = f"[green]{mkt['bid']}[/] / [red]{mkt['ask']}[/]"
            
            # 2. 获取持仓
            pos = self.position_cache.get(symbol, {"vol": 0, "price": 0})
            vol = pos['vol']
            entry = pos['price']
            
            # 格式化持仓显示
            if vol > 0:
                pos_str = f"[green]LONG {vol}[/]"
            elif vol < 0:
                pos_str = f"[red]SHORT {abs(vol)}[/]"
            else:
                pos_str = "[dim]-[/]"
            
            entry_str = f"{entry:.4f}" if vol != 0 else "-"

            # 3. 实时计算 PnL (Mark-to-Market)
            # PnL = (Current - Entry) * Vol
            # 多头: (105 - 100) * 1 = 5
            # 空头: (95 - 100) * -1 = 5
            pnl_str = "-"
            if vol != 0 and mid_price > 0:
                pnl = (mid_price - entry) * vol
                color = "green" if pnl >= 0 else "red"
                pnl_str = f"[{color}]{pnl:+.2f} U[/]"

            table.add_row(
                symbol,
                mkt_str,
                pos_str,
                entry_str,
                pnl_str
            )

        return Panel(table, title="Market & Positions")

    def _render_footer(self):
        """底部：日志流"""
        log_text = "\n".join(self.logs)
        return Panel(log_text, title="System Logs", style="dim")

    def render(self):
        """组合最终视图"""
        self.layout["header"].update(self._render_header())
        self.layout["body"].update(self._render_body())
        self.layout["footer"].update(self._render_footer())
        return self.layout