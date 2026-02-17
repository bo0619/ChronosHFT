# file: ui/dashboard.py

from datetime import datetime
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.console import Console
from rich.text import Text
from rich.align import Align

from event.type import OrderBook, PositionData, AccountData, StrategyData
from data.ref_data import ref_data_manager

class TUIDashboard:
    def __init__(self):
        self.console = Console()
        self.layout = Layout()
        self.layout.split(
            Layout(name="header", size=3),
            Layout(name="main", ratio=1),
            Layout(name="footer", size=10)
        )
        self.account_data = None
        self.market_cache = {}
        self.position_cache = {}
        self.strategy_cache = {}
        self.logs = []
        self.max_logs = 8

    def _fmt_price(self, symbol, price):
        if price is None or price == 0: return "-"
        info = ref_data_manager.get_info(symbol)
        prec = info.price_precision if info else 2
        return f"{price:.{prec}f}"

    def update_account(self, data: AccountData): self.account_data = data
    def update_market(self, ob: OrderBook): 
        bid, bv = ob.get_best_bid()
        ask, av = ob.get_best_ask()
        self.market_cache[ob.symbol] = {"bid": bid, "ask": ask, "bid_v": bv, "ask_v": av}
    def update_position(self, pos: PositionData): self.position_cache[pos.symbol] = pos
    def update_strategy(self, data: StrategyData): self.strategy_cache[data.symbol] = data
    def add_log(self, msg: str):
        time_str = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{time_str}] {msg}")
        if len(self.logs) > self.max_logs: self.logs.pop(0)

    def _render_header(self):
        if not self.account_data: return Panel("Loading...", title="ChronosHFT")
        acc = self.account_data
        m_ratio = (acc.used_margin / acc.equity * 100) if acc.equity > 0 else 0
        color = "green" if acc.equity >= acc.balance else "red"
        summary = (
            f"[bold]Equity:[/][{color}]{acc.equity:.2f}[/] | [bold]Balance:[/]{acc.balance:.2f} | "
            f"[bold]Used Margin:[/]{acc.used_margin:.2f} ({m_ratio:.1f}%) | [bold]Avail:[/]{acc.available:.2f}"
        )
        return Panel(summary, title="Account Summary")

    def _render_main_table(self):
        table = Table(show_header=True, header_style="bold cyan", expand=True, box=None)
        table.add_column("Symbol", width=10)
        table.add_column("Bid1", justify="right", style="green")  # [NEW] 绿色
        table.add_column("Ask1", justify="right", style="red")    # [NEW] 红色
        table.add_column("FairVal (Alpha)", justify="center")
        table.add_column("GLFT (γ|k|A)", justify="center", style="dim")
        table.add_column("σ(bp)", justify="right", style="dim")
        table.add_column("Pos", justify="right")                  # [NEW] 动态颜色
        table.add_column("PnL", justify="right")

        all_syms = set(self.market_cache.keys()) | set(self.position_cache.keys()) | set(self.strategy_cache.keys())
        for sym in sorted(list(all_syms)):
            m = self.market_cache.get(sym, {"bid":0, "ask":0})
            strat = self.strategy_cache.get(sym)
            pos = self.position_cache.get(sym)
            
            # 价格处理
            bid_str = self._fmt_price(sym, m['bid'])
            ask_str = self._fmt_price(sym, m['ask'])
            
            # 策略处理
            if strat:
                a_c = "green" if strat.alpha_bps > 0.3 else ("red" if strat.alpha_bps < -0.3 else "white")
                fair = f"{self._fmt_price(sym, strat.fair_value)} ([{a_c}]{strat.alpha_bps:+.1f}[/])"
                glft = f"{strat.gamma:.1f}|{strat.k:.1f}|{strat.A:.1f}"
                sig = f"{strat.sigma:.1f}"
            else:
                fair, glft, sig = "-", "-", "-"

            # [修复] 仓位颜色逻辑
            pos_str = "-"
            pnl_str = "-"
            if pos and abs(pos.volume) > 1e-8:
                p_color = "green" if pos.volume > 0 else "red"
                pos_str = f"[{p_color}]{pos.volume:+.3f}[/]"
                
                mid = (m['bid'] + m['ask']) / 2
                if mid > 0:
                    pnl = (mid - pos.price) * pos.volume
                    pnl_c = "green" if pnl >= 0 else "red"
                    pnl_str = f"[{pnl_c}]{pnl:+.2f}[/]"

            table.add_row(sym, bid_str, ask_str, fair, glft, sig, pos_str, pnl_str)
        
        return Panel(table, title="Market & Strategy Status")

    def render(self):
        self.layout["header"].update(self._render_header())
        self.layout["main"].update(self._render_main_table())
        self.layout["footer"].update(Panel("\n".join(self.logs), title="Logs"))
        return self.layout