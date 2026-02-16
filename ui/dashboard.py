# file: ui/dashboard.py

from datetime import datetime
from collections import defaultdict
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.console import Console
from rich.text import Text
from rich.align import Align

from event.type import OrderBook, PositionData, AccountData, SystemHealthData, StrategyData, SystemState
from data.ref_data import ref_data_manager # [NEW] 引入参考数据

class TUIDashboard:
    def __init__(self):
        self.console = Console()
        
        # --- 布局初始化 ---
        self.layout = Layout()
        self.layout.split(
            Layout(name="header", size=3),
            Layout(name="main", ratio=1),
            Layout(name="footer", size=10)
        )
        self.layout["main"].split_row(
            Layout(name="left", ratio=1),
            Layout(name="right", ratio=2)
        )
        self.layout["left"].split(
            Layout(name="risk_monitor", size=8),
            Layout(name="sync_monitor", size=12),
            Layout(name="exec_monitor")
        )
        self.layout["right"].update(Panel("Waiting for Market Data...", title="Market & Strategy"))

        # 数据缓存
        self.health_data = None
        self.account_data = None
        self.market_cache = {}
        self.position_cache = {}
        self.strategy_cache = {}
        self.logs = []
        self.max_logs = 8

    # --- 辅助：价格格式化 ---
    def _fmt_price(self, symbol, price):
        """根据币种精度格式化价格字符串"""
        if price is None: return "-"
        info = ref_data_manager.get_info(symbol)
        if not info:
            return f"{price:.2f}" # 默认
        
        # 动态精度：比如 BTC 是 2, PEPE 是 8
        prec = info.price_precision
        return f"{price:.{prec}f}"

    # --- 数据更新接口 ---
    def update_health(self, data: SystemHealthData): self.health_data = data
    def update_account(self, data: AccountData): self.account_data = data
    def update_market(self, ob: OrderBook): 
        bid, _ = ob.get_best_bid()
        ask, _ = ob.get_best_ask()
        self.market_cache[ob.symbol] = {"bid": bid, "ask": ask}
    def update_position(self, pos: PositionData): self.position_cache[pos.symbol] = pos
    def update_strategy(self, data: StrategyData): self.strategy_cache[data.symbol] = data
    
    def add_log(self, msg: str):
        time_str = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{time_str}] {msg}")
        if len(self.logs) > self.max_logs: self.logs.pop(0)

    # --- 渲染逻辑 ---

    def _render_header(self):
        if not self.account_data:
            return Panel("Loading Account...", title="Account", style="bold white")
        acc = self.account_data
        color = "green" if acc.equity >= acc.balance else "red"
        summary = (
            f"[bold]Equity:[/bold] [{color}]{acc.equity:.2f}[/] | "
            f"[bold]Balance:[/bold] {acc.balance:.2f} | "
            f"[bold]Used Margin:[/bold] {acc.used_margin:.2f} | "
            f"[bold]Available:[/bold] {acc.available:.2f}"
        )
        return Panel(summary, title="ChronosHFT Account", style="bold white")

    def _render_module_1_risk(self):
        """Risk Monitor"""
        if not self.health_data: return Panel("Waiting...", title="🟥 Risk Monitor")
        h = self.health_data
        
        # 阈值变色
        exp_color = "red bold" if h.total_exposure > 10000 else "green"
        
        # [修复] 这里的 margin_ratio 是小数 (0.02)，显示为百分比
        mrg_ratio_pct = h.margin_ratio * 100
        mrg_color = "red bold" if mrg_ratio_pct > 80 else ("yellow" if mrg_ratio_pct > 50 else "green")
        
        grid = Table.grid(expand=True)
        grid.add_column(justify="center", ratio=1)
        grid.add_column(justify="center", ratio=1)
        
        grid.add_row(
            f"[bold]Total Exposure[/]\n[{exp_color}]${h.total_exposure:,.0f}[/]",
            f"[bold]Margin Ratio[/]\n[{mrg_color}]{mrg_ratio_pct:.2f}%[/]"
        )
        return Panel(grid, title="🟥 Risk Monitor", border_style="red")

    def _render_module_2_sync(self):
        """System Integrity"""
        if not self.health_data: return Panel("Waiting...", title="🟧 System State")
        h = self.health_data
        
        state_name = h.state.name
        if h.state.value == "CLEAN":
            status_text = "[bold green]✅ CLEAN[/]"
            border = "green"
        elif h.state.value == "SYNCING":
            status_text = "[bold yellow]🔄 SYNCING[/]"
            border = "yellow"
        else:
            status_text = f"[bold red blink]❌ {state_name}[/]"
            border = "red"

        diff_table = Table(box=None, show_header=True, padding=(0,1), expand=True)
        diff_table.add_column("Item", style="dim")
        diff_table.add_column("Local")
        diff_table.add_column("Exch")
        diff_table.add_column("Diff", style="bold")
        
        o_diff = h.order_count_local - h.order_count_remote
        o_color = "red" if o_diff != 0 else "dim"
        diff_table.add_row("Orders", str(h.order_count_local), str(h.order_count_remote), f"[{o_color}]{o_diff:+}[/]")
        
        has_pos_diff = False
        for sym, (loc, rem, diff) in h.pos_diffs.items():
            diff_table.add_row(f"{sym}", f"{loc:.3f}", f"{rem:.3f}", f"[red]{diff:+.3f}[/]")
            has_pos_diff = True
        if not has_pos_diff:
            diff_table.add_row("Positions", "OK", "OK", "[dim]0[/]")

        content = Table.grid(expand=True)
        content.add_row(Align.center(status_text))
        content.add_row(diff_table)
        return Panel(content, title="🟧 System Integrity", border_style=border)

    def _render_module_3_exec(self):
        """Execution"""
        if not self.health_data: return Panel("Waiting...", title="🟨 Execution")
        h = self.health_data
        c_color = "red blink" if h.cancelling_count > 5 else "white"
        f_color = "green" if h.fill_ratio > 0.2 else "yellow"
        
        grid = Table.grid(expand=True)
        grid.add_column(justify="left"); grid.add_column(justify="right")
        grid.add_row("Pending Cancel:", f"[{c_color}]{h.cancelling_count}[/]")
        grid.add_row("Fill Ratio:", f"[{f_color}]{h.fill_ratio*100:.1f}%[/]")
        return Panel(grid, title="🟨 Execution", border_style="yellow")

    def _render_right_side(self):
        """Market & Alpha Table"""
        table = Table(show_header=True, header_style="bold cyan", expand=True, box=None)
        table.add_column("Sym", width=8)
        table.add_column("Price", justify="right")
        table.add_column("FairVal (Alpha)", justify="center")
        table.add_column("GLFT (γ|k|A)", justify="center", style="dim")
        table.add_column("σ(bp)", justify="right")
        table.add_column("Pos", justify="right")
        table.add_column("PnL", justify="right")
        
        all_syms = set(self.market_cache.keys()) | set(self.position_cache.keys()) | set(self.strategy_cache.keys())
        
        for sym in sorted(list(all_syms)):
            mkt = self.market_cache.get(sym, {"bid": 0, "ask": 0})
            mid = (mkt['bid'] + mkt['ask']) / 2 if (mkt['bid'] and mkt['ask']) else 0
            
            # [修复] 使用动态精度格式化价格
            mid_str = self._fmt_price(sym, mid)
            
            strat = self.strategy_cache.get(sym)
            if strat:
                alpha_c = "green" if strat.alpha_bps > 0.5 else ("red" if strat.alpha_bps < -0.5 else "dim")
                # [修复] Fair Price 也动态格式化
                fair_val_str = self._fmt_price(sym, strat.fair_value)
                fair_str = f"{fair_val_str} ([{alpha_c}]{strat.alpha_bps:+.1f}bp[/])"
                params_str = f"{strat.gamma:.1f}|{strat.k:.1f}|{strat.A:.1f}"
                sigma_str = f"{strat.sigma:.1f}"
            else:
                fair_str, params_str, sigma_str = "-", "-", "-"
                
            pos = self.position_cache.get(sym)
            pos_vol = pos.volume if pos else 0.0
            pos_price = pos.price if pos else 0.0
            
            if pos_vol > 0: pos_str = f"[green]{pos_vol}[/]"
            elif pos_vol < 0: pos_str = f"[red]{pos_vol}[/]"
            else: pos_str = "-"
            
            pnl_str = "-"
            if pos_vol != 0 and mid > 0:
                pnl = (mid - pos_price) * pos_vol
                c = "green" if pnl >= 0 else "red"
                pnl_str = f"[{c}]{pnl:+.2f}[/]"
            
            table.add_row(sym, mid_str, fair_str, params_str, sigma_str, pos_str, pnl_str)
            
        return Panel(table, title="Market & Strategy Status")

    def render(self):
        self.layout["header"].update(self._render_header())
        self.layout["risk_monitor"].update(self._render_module_1_risk())
        self.layout["sync_monitor"].update(self._render_module_2_sync())
        self.layout["exec_monitor"].update(self._render_module_3_exec())
        self.layout["right"].update(self._render_right_side())
        self.layout["footer"].update(Panel("\n".join(self.logs), title="Logs", style="dim"))
        return self.layout