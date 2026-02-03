# file: ui/dashboard.py

from datetime import datetime
from collections import defaultdict
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.console import Console
from rich.text import Text
from event.type import OrderBook, PositionData, AccountData, StrategyData

class TUIDashboard:
    def __init__(self):
        self.console = Console()
        
        # 布局
        self.layout = Layout()
        self.layout.split(
            Layout(name="header", size=3),
            Layout(name="body", ratio=1),
            Layout(name="footer", size=10)
        )
        
        # 数据缓存
        self.market_cache = {}   
        self.position_cache = {} 
        self.strategy_cache = {} # [NEW] {symbol: StrategyData}
        
        self.account_info = {
            "balance": 0.0, "equity": 0.0, "margin": 0.0, "available": 0.0
        }
        
        self.logs = []
        self.max_logs = 8

    def update_market(self, ob: OrderBook):
        bid, _ = ob.get_best_bid()
        ask, _ = ob.get_best_ask()
        self.market_cache[ob.symbol] = {"bid": bid, "ask": ask}

    def update_position(self, pos: PositionData):
        self.position_cache[pos.symbol] = {"vol": pos.volume, "price": pos.price}

    def update_account(self, acc: AccountData):
        self.account_info = {
            "balance": acc.balance, "equity": acc.equity, 
            "margin": acc.used_margin, "available": acc.available
        }

    def update_strategy(self, data: StrategyData):
        """[NEW] 更新策略参数缓存"""
        self.strategy_cache[data.symbol] = data

    def add_log(self, msg: str):
        time_str = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{time_str}] {msg}")
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    def _render_header(self):
        acc = self.account_info
        color = "green" if acc["equity"] >= acc["balance"] else "red"
        summary = (
            f"[bold]Eq:[/bold] [{color}]{acc['equity']:.2f}[/] | "
            f"[bold]Bal:[/bold] {acc['balance']:.2f} | "
            f"[bold]Mrg:[/bold] {acc['margin']:.2f} | "
            f"[bold]Avail:[/bold] {acc['available']:.2f}"
        )
        return Panel(summary, title="GLFT Dashboard", style="bold white")

    def _render_body(self):
        """
        主监控表：融合 行情、策略参数、持仓
        """
        table = Table(expand=True, box=None)
        
        table.add_column("Sym", style="cyan", width=8)
        table.add_column("Mkt (B/A)", justify="center")
        # [NEW] GLFT 核心参数列
        table.add_column("FairVal (Alpha)", justify="center", style="yellow")
        table.add_column("GLFT (γ | k | A)", justify="center", style="magenta")
        table.add_column("σ(bps)", justify="right")
        
        table.add_column("Pos", justify="right")
        table.add_column("PnL", justify="right")

        all_symbols = set(self.market_cache.keys()) | set(self.position_cache.keys()) | set(self.strategy_cache.keys())
        
        for symbol in sorted(list(all_symbols)):
            # 1. Market
            mkt = self.market_cache.get(symbol, {"bid": 0, "ask": 0})
            mid_price = (mkt['bid'] + mkt['ask']) / 2 if (mkt['bid'] and mkt['ask']) else 0
            mkt_str = f"{mkt['bid']:.2f}/{mkt['ask']:.2f}"
            
            # 2. Strategy Params
            st = self.strategy_cache.get(symbol)
            if st:
                # Fair Value (Alpha bps)
                alpha_str = f"{st.alpha_bps:+.1f}bp"
                alpha_color = "green" if st.alpha_bps > 0 else ("red" if st.alpha_bps < 0 else "dim")
                fair_str = f"{st.fair_value:.2f} ([{alpha_color}]{alpha_str}[/])"
                
                # Params (Gamma | k | A)
                params_str = f"{st.gamma:.2f}|{st.k:.2f}|{st.A:.1f}"
                
                # Volatility
                vol_str = f"{st.sigma:.1f}"
            else:
                fair_str = "-"
                params_str = "-"
                vol_str = "-"

            # 3. Position
            pos = self.position_cache.get(symbol, {"vol": 0, "price": 0})
            vol = pos['vol']
            entry = pos['price']
            
            if vol > 0: pos_str = f"[green]{vol}[/]"
            elif vol < 0: pos_str = f"[red]{vol}[/]"
            else: pos_str = "-"
            
            # 4. PnL
            pnl_str = "-"
            if vol != 0 and mid_price > 0:
                pnl = (mid_price - entry) * vol
                c = "green" if pnl >= 0 else "red"
                pnl_str = f"[{c}]{pnl:+.2f}[/]"

            table.add_row(
                symbol,
                mkt_str,
                fair_str,
                params_str,
                vol_str,
                pos_str,
                pnl_str
            )

        return Panel(table, title="Market & GLFT State")

    def _render_footer(self):
        log_text = "\n".join(self.logs)
        return Panel(log_text, title="Logs", style="dim")

    def render(self):
        self.layout["header"].update(self._render_header())
        self.layout["body"].update(self._render_body())
        self.layout["footer"].update(self._render_footer())
        return self.layout