# file: ui/dashboard.py

from datetime import datetime
from typing import Dict, List, Any
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.align import Align
from rich.box import SIMPLE_HEAD, ROUNDED

from event.type import OrderBook, PositionData, AccountData, StrategyData
from data.ref_data import ref_data_manager

# ----------------------------------------------------------
# 语义化配色方案
# ----------------------------------------------------------
C_BID = "bold fuchsia"     # 买方颜色 (亮粉/紫)
C_ASK = "bold cyan"    # 卖方颜色 (亮青)
C_UP = "bold bright_green" # 上涨/盈利/多头
C_DOWN = "bold bright_red" # 下跌/亏损/空头
C_FAIR = "bold yellow"     # 核心计算值
C_SIGNAL = "bold dark_orange" # 信号/偏移

# ----------------------------------------------------------
# 高级美化工具
# ----------------------------------------------------------

def _fmt_weight(w: float) -> str:
    """权重格式化：使用强对比颜色"""
    if abs(w) < 0.01: return "[dim] 0.000[/]"
    color = "bright_green" if w > 0 else "bright_red"
    return f"[{color}]{w:+.3f}[/]"

def _fmt_value(val: Any) -> str:
    if isinstance(val, list):
        return " ".join([_fmt_weight(w) for w in val])
    if isinstance(val, float):
        return f"{val:.2f}"
    return str(val)

class TUIDashboard:
    def __init__(self):
        self.layout = Layout()

        # ── 布局：Header(3) / Body(自由) / Footer(8) ──
        self.layout.split(
            Layout(name="header", size=3),
            Layout(name="body",   ratio=1),
            Layout(name="footer", size=8),
        )

        # ── Body：市场(1.5) / 策略(2.5) ── 
        # 调窄左边，调宽右边以展示复杂的ML参数
        self.layout["body"].split_row(
            Layout(name="market", ratio=3), 
            Layout(name="params", ratio=5),
        )

        self.account_data   = None
        self.market_cache   = {}
        self.position_cache = {}
        self.strategy_cache = {}
        self.logs           = []
        self.max_logs       = 6
        self.dynamic_columns = set()

    # ----------------------------------------------------------
    # 数据更新
    # ----------------------------------------------------------

    def update_account(self, data: AccountData): self.account_data = data
    def update_market(self, ob: OrderBook):
        bid, bv = ob.get_best_bid()
        ask, av = ob.get_best_ask()
        self.market_cache[ob.symbol] = {"bid": bid, "ask": ask}
    def update_position(self, pos: PositionData): self.position_cache[pos.symbol] = pos
    def update_strategy(self, data: StrategyData):
        self.strategy_cache[data.symbol] = data
        if hasattr(data, 'params') and data.params:
            for key in data.params.keys(): self.dynamic_columns.add(key)

    def add_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{ts}] {msg}")
        if len(self.logs) > self.max_logs: self.logs.pop(0)

    # ----------------------------------------------------------
    # 内部渲染辅助
    # ----------------------------------------------------------

    def _fmt_price(self, symbol, price) -> str:
        if not price: return "-"
        info = ref_data_manager.get_info(symbol)
        prec = info.price_precision if info else 4
        return f"{price:.{prec}f}"

    def _render_header(self):
        if not self.account_data: return Panel("ChronosHFT Initializing...", style="bold white")
        acc = self.account_data
        m_ratio = (acc.used_margin / acc.equity * 100) if acc.equity > 0 else 0
        
        # 余额对比色
        equity_color = C_UP if acc.equity >= acc.balance else C_DOWN
        
        summary = Text.assemble(
            ("Equity: ", "bold white"), (f"{acc.equity:.2f}", equity_color), (" | ", "dim"),
            ("Balance: ", "bold white"), (f"{acc.balance:.2f}", "white"), (" | ", "dim"),
            ("Used Margin: ", "bold white"), (f"{acc.used_margin:.2f} ({m_ratio:.1f}%)", "yellow"), (" | ", "dim"),
            ("Avail: ", "bold white"), (f"{acc.available:.2f}", "bright_cyan")
        )
        return Panel(Align.center(summary), title="[bold]Account Overview[/]", border_style="blue")

    def _render_market(self):
        """左侧：行情与持仓，极致压缩与高亮"""
        table = Table(show_header=True, header_style="bold white", expand=True, box=SIMPLE_HEAD)
        table.add_column("Symbol", width=10)
        table.add_column("Market (Bid / Ask)", justify="center")
        table.add_column("Net Position", justify="center")
        table.add_column("Entry Price", justify="right")
        table.add_column("Unrealized PnL", justify="right")

        for sym in sorted(set(self.market_cache.keys()) | set(self.position_cache.keys())):
            m = self.market_cache.get(sym, {"bid": 0, "ask": 0})
            strat = self.strategy_cache.get(sym)
            pos = self.position_cache.get(sym)

            # 盘口合并显示
            bid_str = self._fmt_price(sym, m["bid"])
            ask_str = self._fmt_price(sym, m["ask"])
            mkt_display = Text.assemble((bid_str, C_UP), (" / ", "white"), (ask_str, C_DOWN))

            # 持仓语义化
            pos_display = Text("-", style="dim")
            pnl_display = Text("-", style="dim")
            entry_price = "-"
            
            if pos and abs(pos.volume) > 1e-8:
                side_str = "LONG" if pos.volume > 0 else "SHORT"
                side_col = C_UP if pos.volume > 0 else C_DOWN
                pos_display = Text(f"{side_str} {abs(pos.volume):.1f}", style=side_col)
                entry_price = self._fmt_price(sym, pos.price)
                
                mid = (m["bid"] + m["ask"]) / 2
                if mid > 0:
                    pnl = (mid - pos.price) * pos.volume
                    pnl_col = C_UP if pnl >= 0 else C_DOWN
                    pnl_display = Text(f"{pnl:+.2f} U", style=pnl_col)

            table.add_row(sym, mkt_display, pos_display, entry_price, pnl_display)
            
        return Panel(table, title="[bold green]Market & Positions[/]", border_style="green")

    def _render_params(self):
        """右侧：策略动态参数，针对ML模型进行配色优化"""
        table = Table(show_header=True, header_style="bold white", expand=True, box=SIMPLE_HEAD)
        
        # 定义固定展示顺序
        base_cols = ["Symbol", "Price", "FairVal (Alpha)"]
        # 排除已展示和列表类型的列
        param_cols = sorted([c for c in self.dynamic_columns if c not in ["Clf_Weights", "Reg_Weights", "Mode"]])
        weight_cols = sorted([c for c in self.dynamic_columns if "weight" in c.lower()])

        for col in base_cols: table.add_column(col, justify="center")
        table.add_column("Mode", justify="center") # Mode单独给颜色
        for col in param_cols: table.add_column(col, justify="right")
        for col in weight_cols: table.add_column(col, justify="left")

        for sym in sorted(self.strategy_cache.keys()):
            strat = self.strategy_cache[sym]
            mkt = self.market_cache.get(sym, {"bid": 0, "ask": 0})
            mid = (mkt['bid'] + mkt['ask']) / 2 if mkt['bid'] else 0
            
            # 基础列
            fair_val_str = self._fmt_price(sym, strat.fair_value)
            alpha_c = C_UP if strat.alpha_bps > 0.5 else (C_DOWN if strat.alpha_bps < -0.5 else "white")
            alpha_text = f"({strat.alpha_bps:+.1f}bp)"
            
            # Mode 高亮显示
            mode_val = strat.params.get("Mode", "MM")
            if "BUY" in mode_val: mode_display = Text(mode_val, style="black on green")
            elif "SELL" in mode_val: mode_display = Text(mode_val, style="black on red")
            else: mode_display = Text(mode_val, style="black on cyan")

            row = [
                sym, 
                self._fmt_price(sym, mid),
                Text.assemble((fair_val_str, C_FAIR), (" "), (alpha_text, alpha_c)),
                mode_display
            ]
            
            # 参数列
            for col in param_cols:
                row.append(Text(_fmt_value(strat.params.get(col, "-")), style="bold white"))
            
            # 权重列
            for col in weight_cols:
                row.append(_fmt_value(strat.params.get(col, [])))
            
            table.add_row(*row)

        return Panel(table, title="[bold magenta]Strategy Status[/]", border_style="magenta")

    def render(self):
        self.layout["header"].update(self._render_header())
        self.layout["market"].update(self._render_market())
        self.layout["params"].update(self._render_params())
        self.layout["footer"].update(Panel("\n".join(self.logs), title="System Logs", border_style="dim"))
        return self.layout