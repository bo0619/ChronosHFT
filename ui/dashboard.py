# file: ui/dashboard.py

from datetime import datetime
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.console import Console
from rich.align import Align

from event.type import OrderBook, PositionData, AccountData, StrategyData
from data.ref_data import ref_data_manager

class TUIDashboard:
    def __init__(self):
        self.console = Console()
        
        # --- 布局初始化 (简化版) ---
        self.layout = Layout()
        
        # 垂直分为三部分：头部账户信息、中部策略行情、底部日志
        self.layout.split(
            Layout(name="header", size=3),
            Layout(name="main", ratio=1),
            Layout(name="footer", size=10)
        )

        # 数据缓存
        self.account_data = None
        self.market_cache = {}
        self.position_cache = {}
        self.strategy_cache = {}
        self.logs = []
        self.max_logs = 8

    # --- 辅助：价格格式化 ---
    def _fmt_price(self, symbol, price):
        if price is None: return "-"
        info = ref_data_manager.get_info(symbol)
        if not info: return f"{price:.2f}"
        prec = info.price_precision
        return f"{price:.{prec}f}"

    # --- 数据更新接口 ---
    # 移除了 update_health，因为不再需要显示系统健康包
    
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
        
    def update_health(self, data): 
        # 兼容旧接口调用，但不处理数据
        pass
    
    def add_log(self, msg: str):
        time_str = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{time_str}] {msg}")
        if len(self.logs) > self.max_logs: self.logs.pop(0)

    # --- 渲染逻辑 ---

    def _render_header(self):
        if not self.account_data:
            return Panel("Loading Account...", title="ChronosHFT", style="bold white")
        
        acc = self.account_data
        
        # 计算保证金率
        if acc.equity > 0:
            margin_ratio = (acc.used_margin / acc.equity) * 100
        else:
            margin_ratio = 0.0
            
        color = "green" if acc.equity >= acc.balance else "red"
        m_color = "red" if margin_ratio > 80 else ("yellow" if margin_ratio > 50 else "green")
        
        # 构造头部信息栏
        summary = (
            f"[bold]Equity:[/bold] [{color}]{acc.equity:.2f}[/] | "
            f"[bold]Balance:[/bold] {acc.balance:.2f} | "
            f"[bold]Used Margin:[/bold] {acc.used_margin:.2f} ([{m_color}]{margin_ratio:.1f}%[/]) | "
            f"[bold]Available:[/bold] {acc.available:.2f}"
        )
        return Panel(summary, title="ChronosHFT Account", style="bold white")

    def _render_main_table(self):
        """
        全宽度的市场与策略状态表
        """
        table = Table(
            show_header=True, 
            header_style="bold cyan", 
            expand=True, 
            box=None,
            row_styles=["none", "dim"] # 隔行变色，增加可读性
        )
        
        # 定义列宽和对齐
        table.add_column("Symbol", width=12)
        table.add_column("Price (Bid/Ask)", justify="center")
        table.add_column("FairVal (Alpha)", justify="center")
        table.add_column("GLFT (γ | k | A)", justify="center")
        table.add_column("σ (bp)", justify="right")
        table.add_column("Pos", justify="right")
        table.add_column("Unrealized PnL", justify="right")
        
        # 收集所有涉及的币种
        all_syms = set(self.market_cache.keys()) | set(self.position_cache.keys()) | set(self.strategy_cache.keys())
        
        for sym in sorted(list(all_syms)):
            # 1. 基础行情
            mkt = self.market_cache.get(sym, {"bid": 0, "ask": 0})
            bid, ask = mkt['bid'], mkt['ask']
            
            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2
                # 显示盘口：Bid / Ask
                price_str = f"[green]{self._fmt_price(sym, bid)}[/] / [red]{self._fmt_price(sym, ask)}[/]"
            else:
                mid = 0
                price_str = "-"

            # 2. 策略状态
            strat = self.strategy_cache.get(sym)
            if strat:
                alpha_c = "green" if strat.alpha_bps > 0.5 else ("red" if strat.alpha_bps < -0.5 else "white")
                fair_val_str = self._fmt_price(sym, strat.fair_value)
                
                fair_str = f"{fair_val_str} ([{alpha_c}]{strat.alpha_bps:+.1f}bp[/])"
                params_str = f"{strat.gamma:.1f} | {strat.k:.1f} | {strat.A:.1f}"
                sigma_str = f"{strat.sigma:.1f}"
            else:
                fair_str, params_str, sigma_str = "-", "-", "-"
                
            # 3. 持仓与盈亏
            pos = self.position_cache.get(sym)
            pos_vol = pos.volume if pos else 0.0
            pos_price = pos.price if pos else 0.0
            
            if pos_vol > 0: pos_str = f"[green]{pos_vol}[/]"
            elif pos_vol < 0: pos_str = f"[red]{pos_vol}[/]"
            else: pos_str = "-"
            
            pnl_str = "-"
            if pos_vol != 0 and mid > 0:
                # 简单的未结盈亏计算 UPNL
                pnl = (mid - pos_price) * pos_vol
                c = "green" if pnl >= 0 else "red"
                pnl_str = f"[{c}]{pnl:+.2f} U[/]"
            
            table.add_row(sym, price_str, fair_str, params_str, sigma_str, pos_str, pnl_str)
            
        return Panel(table, title="Live Market & Strategy Dashboard")

    def render(self):
        self.layout["header"].update(self._render_header())
        self.layout["main"].update(self._render_main_table())
        self.layout["footer"].update(Panel("\n".join(self.logs), title="System Logs", style="dim"))
        return self.layout