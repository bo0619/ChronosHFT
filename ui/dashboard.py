# file: ui/dashboard.py

from datetime import datetime
from typing import Any, Dict
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.align import Align
from rich.box import SIMPLE_HEAD

from event.type import OrderBook, PositionData, AccountData, StrategyData
from data.ref_data import ref_data_manager

class TUIDashboard:
    def __init__(self):
        self.layout = Layout()
        self.layout.split(
            Layout(name="header", size=3),
            Layout(name="body",   ratio=1),
            Layout(name="footer", size=8),
        )
        self.layout["body"].split_row(
            Layout(name="market", ratio=1), 
            Layout(name="strategy", ratio=2), 
        )

        self.account_data   = None
        self.market_cache   = {}
        self.position_cache = {}
        self.strategy_cache = {}
        self.logs           = []
        self.max_logs       = 6
        self.dynamic_columns = set()

    def update_account(self, data): self.account_data = data
    def update_position(self, pos): self.position_cache[pos.symbol] = pos
    def update_market(self, ob):
        bid, _ = ob.get_best_bid()
        ask, _ = ob.get_best_ask()
        self.market_cache[ob.symbol] = {"bid": bid, "ask": ask}

    def update_strategy(self, data: StrategyData):
        self.strategy_cache[data.symbol] = data
        if data.params:
            for k in data.params.keys():
                self.dynamic_columns.add(k)

    def add_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{ts}] {msg}")
        if len(self.logs) > self.max_logs: self.logs.pop(0)

    # --- 智能格式化 (含字典权重处理) ---
    def _smart_format(self, key: str, value: Any) -> str:
        # 1. 处理字典类型的权重 (例如 {"Imb": 0.12, "OFI": -0.05})
        if isinstance(value, dict):
            parts = []
            for k, v in value.items():
                if not isinstance(v, (int, float)): continue
                if abs(v) < 0.01: continue # 过滤极小权重
                color = "bright_green" if v > 0 else "bright_red"
                # 格式: 键:值
                parts.append(f"{k}:[{color}]{v:+.2f}[/]")
            return " ".join(parts) if parts else "[dim]-[/]"

        # 2. 处理列表类型的权重 (Fallback)
        if isinstance(value, list):
            parts = [f"[{'green' if x > 0 else 'red'}]{x:+.2f}[/]" for x in value if abs(x) > 0.01]
            return " ".join(parts) if parts else "[dim]-[/]"

        # 3. 浮点数
        if isinstance(value, float): return f"{value:.2f}"

        # 4. 字符串 (Mode/State)
        if isinstance(value, str):
            val_upper = value.upper()
            if "BUY" in val_upper or "LONG" in val_upper: return f"[black on green]{value}[/]"
            if "SELL" in val_upper or "SHORT" in val_upper: return f"[black on red]{value}[/]"
            if "WARM" in val_upper: return f"[yellow]{value}[/]"
            return value

        return str(value)

    def _fmt_price(self, symbol, price) -> str:
        if not price: return "-"
        info = ref_data_manager.get_info(symbol)
        prec = info.price_precision if info else 4
        return f"{price:.{prec}f}"

    def _render_header(self):
        if not self.account_data: return Panel("Loading...", style="bold white")
        acc = self.account_data
        m_ratio = (acc.used_margin / acc.equity * 100) if acc.equity > 0 else 0
        c_eq = "green" if acc.equity >= acc.balance else "red"
        txt = (f"Equity: [{c_eq}]{acc.equity:.2f}[/] | Balance: {acc.balance:.2f} | "
               f"Margin: {acc.used_margin:.2f} ({m_ratio:.1f}%) | Avail: {acc.available:.2f}")
        return Panel(Align.center(txt), title="Account", border_style="blue")

    def _render_market(self):
        table = Table(show_header=True, header_style="bold cyan", expand=True, box=SIMPLE_HEAD)
        table.add_column("Sym", width=10)
        table.add_column("Mkt", justify="center")
        table.add_column("Pos", justify="right")
        table.add_row(*["-" for _ in range(3)]) # 占位
        
        for sym in sorted(set(self.market_cache.keys()) | set(self.position_cache.keys())):
            m = self.market_cache.get(sym, {"bid": 0, "ask": 0})
            pos = self.position_cache.get(sym)
            bid_s, ask_s = self._fmt_price(sym, m["bid"]), self._fmt_price(sym, m["ask"])
            mkt_str = f"[green]{bid_s}[/]/[red]{ask_s}[/]"
            pos_str = "-"
            if pos and abs(pos.volume) > 1e-8:
                c = "green" if pos.volume > 0 else "red"
                pos_str = f"[{c}]{pos.volume:.1f}[/]"
            table.add_row(sym, mkt_str, pos_str)
        return Panel(table, title="Market Status")

    def _render_strategy(self):
        table = Table(show_header=True, header_style="bold magenta", expand=True, box=SIMPLE_HEAD)
        table.add_column("Sym", width=10)
        table.add_column("Fair(α)", justify="center")
        
        cols = sorted(list(self.dynamic_columns))
        short_cols = [c for c in cols if "WEIGHT" not in c.upper()]
        long_cols = [c for c in cols if "WEIGHT" in c.upper()]
        
        for c in short_cols: table.add_column(c, justify="right")
        for c in long_cols: table.add_column(c, justify="left", ratio=1)

        for sym in sorted(self.strategy_cache.keys()):
            strat = self.strategy_cache[sym]
            fair_str = self._fmt_price(sym, strat.fair_value)
            alpha_c = "green" if strat.alpha_bps > 0 else "red"
            row = [sym, f"{fair_str}([{alpha_c}]{strat.alpha_bps:+.1f}[/])"]
            
            for col in short_cols + long_cols:
                val = strat.params.get(col, "-")
                row.append(self._smart_format(col, val))
            table.add_row(*row)
            
        return Panel(table, title="Strategy Dynamics")

    def render(self):
        self.layout["header"].update(self._render_header())
        self.layout["market"].update(self._render_market())
        self.layout["strategy"].update(self._render_strategy())
        self.layout["footer"].update(Panel("\n".join(self.logs), title="Logs"))
        return self.layout