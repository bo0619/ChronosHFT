# file: ui/dashboard.py

from datetime import datetime
from collections import defaultdict
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.console import Console
from rich.text import Text
from rich.align import Align

# 引入核心数据结构
from event.type import OrderBook, PositionData, AccountData, SystemHealthData, StrategyData, SystemState

class TUIDashboard:
    def __init__(self):
        self.console = Console()
        
        # --- 布局初始化 ---
        self.layout = Layout()
        
        # 顶层：Header(账户), Main(核心监控), Footer(日志)
        self.layout.split(
            Layout(name="header", size=3),
            Layout(name="main", ratio=1),
            Layout(name="footer", size=10)
        )
        
        # Main 分为左右两栏
        self.layout["main"].split_row(
            Layout(name="left", ratio=1),  # 风控与系统健康
            Layout(name="right", ratio=2)  # 市场与策略详情
        )
        
        # 左侧分为三块：风险、一致性、执行
        self.layout["left"].split(
            Layout(name="risk_monitor", size=8),  # 🟥 模块1: 风险敞口
            Layout(name="sync_monitor", size=12), # 🟧 模块2: 状态机与对账 (System State)
            Layout(name="exec_monitor")           # 🟨 模块3: 执行统计
        )
        
        # 右侧：市场大表
        self.layout["right"].update(Panel("Waiting for Market Data...", title="Market & Strategy"))

        # --- 数据缓存 ---
        self.health_data = None  # SystemHealthData
        self.account_data = None # AccountData
        
        self.market_cache = {}   # {symbol: {bid: 0, ask: 0}}
        self.position_cache = {} # {symbol: PositionData}
        self.strategy_cache = {} # {symbol: StrategyData}
        
        self.logs = []
        self.max_logs = 8

    # --- 数据更新接口 ---

    def update_health(self, data: SystemHealthData):
        self.health_data = data

    def update_account(self, data: AccountData):
        self.account_data = data

    def update_market(self, ob: OrderBook):
        bid, _ = ob.get_best_bid()
        ask, _ = ob.get_best_ask()
        self.market_cache[ob.symbol] = {"bid": bid, "ask": ask}

    def update_position(self, pos: PositionData):
        self.position_cache[pos.symbol] = pos

    def update_strategy(self, data: StrategyData):
        self.strategy_cache[data.symbol] = data

    def add_log(self, msg: str):
        time_str = datetime.now().strftime("%H:%M:%S")
        # 简单过滤颜色代码，防止日志错乱（可选）
        self.logs.append(f"[{time_str}] {msg}")
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    # --- 渲染逻辑 ---

    def _render_header(self):
        """顶部：账户资金概览"""
        if not self.account_data:
            return Panel("Loading Account...", title="Account", style="bold white")
            
        acc = self.account_data
        
        # 动态颜色：权益 > 余额显示绿色，否则红色 (盈利/亏损)
        color = "green" if acc.equity >= acc.balance else "red"
        
        summary = (
            f"[bold]Equity:[/bold] [{color}]{acc.equity:.2f}[/] | "
            f"[bold]Balance:[/bold] {acc.balance:.2f} | "
            f"[bold]Used Margin:[/bold] {acc.used_margin:.2f} | "
            f"[bold]Available:[/bold] {acc.available:.2f}"
        )
        return Panel(summary, title="ChronosHFT Account", style="bold white")

    def _render_module_1_risk(self):
        """🟥 模块 1：风险与仓位 (Risk)"""
        if not self.health_data: return Panel("Waiting...", title="🟥 Risk Monitor")
        
        h = self.health_data
        
        # 阈值变色
        exp_color = "red bold" if h.total_exposure > 10000 else "green"
        mrg_color = "red bold" if h.margin_ratio > 0.8 else "green"
        
        grid = Table.grid(expand=True)
        grid.add_column(justify="center", ratio=1)
        grid.add_column(justify="center", ratio=1)
        
        grid.add_row(
            f"[bold]Total Exposure[/]\n[{exp_color}]${h.total_exposure:,.0f}[/]",
            f"[bold]Margin Ratio[/]\n[{mrg_color}]{h.margin_ratio*100:.1f}%[/]"
        )
        
        return Panel(grid, title="🟥 Risk Monitor", border_style="red")

    def _render_module_2_sync(self):
        """🟧 模块 2：系统状态与对账 (System Integrity)"""
        if not self.health_data: return Panel("Waiting...", title="🟧 System State")
        
        h = self.health_data
        
        # 1. 状态机可视化
        state_name = h.state.name # CLEAN, DIRTY, SYNCING
        
        if h.state == SystemState.CLEAN:
            status_text = "[bold green]✅ CLEAN[/]"
            border = "green"
        elif h.state == SystemState.SYNCING:
            status_text = "[bold yellow]🔄 SYNCING[/]"
            border = "yellow"
        else: # DIRTY / FROZEN
            status_text = f"[bold red blink]❌ {state_name}[/]"
            border = "red"

        # 2. 对账差异表
        diff_table = Table(box=None, show_header=True, padding=(0,1), expand=True)
        diff_table.add_column("Item", style="dim")
        diff_table.add_column("Local")
        diff_table.add_column("Exch")
        diff_table.add_column("Diff", style="bold")
        
        # 订单计数对比
        o_diff = h.order_count_local - h.order_count_remote
        o_color = "red" if o_diff != 0 else "dim"
        diff_table.add_row(
            "Orders", 
            str(h.order_count_local), 
            str(h.order_count_remote), 
            f"[{o_color}]{o_diff:+}[/]"
        )
        
        # 仓位差异 (只显示有问题的)
        has_pos_diff = False
        for sym, (loc, rem, diff) in h.pos_diffs.items():
            diff_table.add_row(
                f"{sym}", 
                f"{loc:.2f}", 
                f"{rem:.2f}", 
                f"[red]{diff:+.2f}[/]"
            )
            has_pos_diff = True
            
        if not has_pos_diff:
            diff_table.add_row("Positions", "OK", "OK", "[dim]0[/]")

        # 组合视图
        content = Table.grid(expand=True)
        content.add_row(Align.center(status_text))
        content.add_row(diff_table)

        return Panel(content, title="🟧 System Integrity", border_style=border)

    def _render_module_3_exec(self):
        """🟨 模块 3：执行健康度 (Execution)"""
        if not self.health_data: return Panel("Waiting...", title="🟨 Execution")
        h = self.health_data
        
        # 卡单警告
        c_color = "red blink" if h.cancelling_count > 5 else "white"
        
        # 成交率
        f_color = "green" if h.fill_ratio > 0.2 else "yellow"
        
        grid = Table.grid(expand=True)
        grid.add_column(justify="left")
        grid.add_column(justify="right")
        
        grid.add_row("Pending Cancel:", f"[{c_color}]{h.cancelling_count}[/]")
        grid.add_row("Fill Ratio:", f"[{f_color}]{h.fill_ratio*100:.1f}%[/]")
        
        # 可以在这里加 API Weight
        # grid.add_row("API Weight:", f"{h.api_weight}")
        
        return Panel(grid, title="🟨 Execution", border_style="yellow")

    def _render_right_side(self):
        """右侧主表：行情、Alpha、持仓"""
        table = Table(show_header=True, header_style="bold cyan", expand=True, box=None)
        table.add_column("Sym", width=8)
        table.add_column("Price", justify="right")
        table.add_column("FairVal (Alpha)", justify="center")
        table.add_column("GLFT (γ|k|A)", justify="center", style="dim")
        table.add_column("σ(bp)", justify="right")
        table.add_column("Pos", justify="right")
        table.add_column("PnL", justify="right")
        
        # 获取所有相关 Symbol
        all_syms = set(self.market_cache.keys()) | set(self.position_cache.keys()) | set(self.strategy_cache.keys())
        
        for sym in sorted(list(all_syms)):
            # 1. Market
            mkt = self.market_cache.get(sym, {"bid": 0, "ask": 0})
            mid = (mkt['bid'] + mkt['ask']) / 2 if (mkt['bid'] and mkt['ask']) else 0
            
            # 2. Strategy
            st = self.strategy_cache.get(sym)
            if st:
                # Alpha 着色
                alpha_c = "green" if st.alpha_bps > 0.5 else ("red" if st.alpha_bps < -0.5 else "dim")
                fair_str = f"{st.fair_value:.2f} ([{alpha_c}]{st.alpha_bps:+.1f}bp[/])"
                params_str = f"{st.gamma:.1f}|{st.k:.1f}|{st.A:.1f}"
                sigma_str = f"{st.sigma:.1f}"
            else:
                fair_str, params_str, sigma_str = "-", "-", "-"
                
            # 3. Position & PnL
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
            
            table.add_row(
                sym,
                f"{mid:.2f}",
                fair_str,
                params_str,
                sigma_str,
                pos_str,
                pnl_str
            )
            
        return Panel(table, title="Market & Strategy Status")

    def render(self):
        """组合最终界面"""
        # 更新 Header
        self.layout["header"].update(self._render_header())
        
        # 更新 Left (Modules)
        self.layout["risk_monitor"].update(self._render_module_1_risk())
        self.layout["sync_monitor"].update(self._render_module_2_sync())
        self.layout["exec_monitor"].update(self._render_module_3_exec())
        
        # 更新 Right (Table)
        self.layout["right"].update(self._render_right_side())
        
        # 更新 Footer
        log_text = "\n".join(self.logs)
        self.layout["footer"].update(Panel(log_text, title="System Logs", style="dim"))
        
        return self.layout