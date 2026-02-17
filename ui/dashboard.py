# file: ui/dashboard.py
# ============================================================
# 特征说明（供阅读对照）：
#   F0 OFI  = L1 盘口买卖量失衡 (Orderbook Imbalance)
#   F1 Mom  = 5s 价格动量 tanh 归一化
#   F2 Dep  = L5 深度比 - 1 (Depth Ratio Bias)
#
# 正权重含义：该特征值升高 → 预测方向倾向 UP / bps 值增大
# ============================================================

from datetime import datetime
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from event.type import OrderBook, PositionData, AccountData, StrategyData
from data.ref_data import ref_data_manager

# 特征名称（与 TrendDetector.get_features() 顺序严格对应）
_FEAT_NAMES = ["OFI", "Mom", "Dep"]


def _fmt_weight(w: float) -> str:
    """权重格式化：带颜色的三位小数"""
    color = "green" if w > 0.05 else ("red" if w < -0.05 else "white")
    return f"[{color}]{w:+.3f}[/]"


def _fmt_weights(weights) -> str:
    if not weights or len(weights) < 3:
        return "[dim]—[/]"
    parts = [f"{n}:{_fmt_weight(w)}" for n, w in zip(_FEAT_NAMES, weights)]
    return "  ".join(parts)


class TUIDashboard:
    def __init__(self):
        self.layout = Layout()

        # ── 顶层：header / body / footer ──────────────────────
        self.layout.split(
            Layout(name="header", size=3),
            Layout(name="body",   ratio=1),
            Layout(name="footer", size=10),
        )

        # ── body：左右分栏 ────────────────────────────────────
        self.layout["body"].split_row(
            Layout(name="market", ratio=3),   # 行情 + GLFT
            Layout(name="ml",     ratio=2),   # ML 状态
        )

        # ── 数据缓存 ──────────────────────────────────────────
        self.account_data   = None
        self.market_cache   = {}
        self.position_cache = {}
        self.strategy_cache = {}
        self.logs           = []
        self.max_logs       = 8

    # ----------------------------------------------------------
    # 数据更新接口（由事件回调调用）
    # ----------------------------------------------------------

    def update_account(self, data: AccountData):
        self.account_data = data

    def update_market(self, ob: OrderBook):
        bid, bv = ob.get_best_bid()
        ask, av = ob.get_best_ask()
        self.market_cache[ob.symbol] = {
            "bid": bid, "ask": ask, "bid_v": bv, "ask_v": av
        }

    def update_position(self, pos: PositionData):
        self.position_cache[pos.symbol] = pos

    def update_strategy(self, data: StrategyData):
        self.strategy_cache[data.symbol] = data

    def add_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{ts}] {msg}")
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    # ----------------------------------------------------------
    # 内部辅助
    # ----------------------------------------------------------

    def _fmt_price(self, symbol, price) -> str:
        if price is None or price == 0:
            return "-"
        info = ref_data_manager.get_info(symbol)
        prec = info.price_precision if info else 2
        return f"{price:.{prec}f}"

    # ----------------------------------------------------------
    # 渲染：Header（账户摘要）
    # ----------------------------------------------------------

    def _render_header(self):
        if not self.account_data:
            return Panel("Loading...", title="ChronosHFT")
        acc = self.account_data
        m_ratio = (acc.used_margin / acc.equity * 100) if acc.equity > 0 else 0
        color   = "green" if acc.equity >= acc.balance else "red"
        summary = (
            f"[bold]Equity:[/][{color}]{acc.equity:.2f}[/]  |  "
            f"[bold]Balance:[/]{acc.balance:.2f}  |  "
            f"[bold]Used Margin:[/]{acc.used_margin:.2f} ({m_ratio:.1f}%)  |  "
            f"[bold]Avail:[/]{acc.available:.2f}"
        )
        return Panel(summary, title="Account Summary")

    # ----------------------------------------------------------
    # 渲染：Market & Strategy 表（左栏）
    # ----------------------------------------------------------

    def _render_market(self):
        table = Table(
            show_header=True, header_style="bold cyan",
            expand=True, box=None
        )
        table.add_column("Symbol",          width=12)
        table.add_column("Bid1",            justify="right",  style="green")
        table.add_column("Ask1",            justify="right",  style="red")
        table.add_column("FairVal (Alpha)", justify="center")
        table.add_column("GLFT (γ|k|A)",   justify="center", style="dim")
        table.add_column("σ(bp)",           justify="right",  style="dim")
        table.add_column("Pos",             justify="right")
        table.add_column("PnL",             justify="right")

        all_syms = (
            set(self.market_cache.keys())
            | set(self.position_cache.keys())
            | set(self.strategy_cache.keys())
        )

        for sym in sorted(all_syms):
            m     = self.market_cache.get(sym,  {"bid": 0, "ask": 0})
            strat = self.strategy_cache.get(sym)
            pos   = self.position_cache.get(sym)

            bid_str = self._fmt_price(sym, m["bid"])
            ask_str = self._fmt_price(sym, m["ask"])

            if strat:
                a_c  = ("green" if strat.alpha_bps > 0.3
                         else "red" if strat.alpha_bps < -0.3 else "white")
                fair = (f"{self._fmt_price(sym, strat.fair_value)} "
                        f"([{a_c}]{strat.alpha_bps:+.1f}[/])")
                glft = f"{strat.gamma:.1f}|{strat.k:.1f}|{strat.A:.1f}"
                sig  = f"{strat.sigma:.1f}"
            else:
                fair = glft = sig = "-"

            pos_str = pnl_str = "-"
            if pos and abs(pos.volume) > 1e-8:
                p_color = "green" if pos.volume > 0 else "red"
                pos_str = f"[{p_color}]{pos.volume:+.3f}[/]"
                mid = (m["bid"] + m["ask"]) / 2
                if mid > 0:
                    pnl   = (mid - pos.price) * pos.volume
                    pnl_c = "green" if pnl >= 0 else "red"
                    pnl_str = f"[{pnl_c}]{pnl:+.2f}[/]"

            table.add_row(
                sym, bid_str, ask_str, fair, glft, sig, pos_str, pnl_str
            )

        return Panel(table, title="Market & Strategy Status")

    # ----------------------------------------------------------
    # 渲染：ML 状态面板（右栏）
    # ----------------------------------------------------------

    def _render_ml(self):
        table = Table(
            show_header=True, header_style="bold magenta",
            expand=True, box=None
        )

        # 表头
        table.add_column("Symbol",  width=12)
        table.add_column("Mode",    width=10,  justify="center")
        table.add_column("p_trend", width=7,   justify="right")
        table.add_column("Train",   width=6,   justify="center")
        table.add_column("N",       width=6,   justify="right")   # n_samples
        table.add_column("Buf",     width=5,   justify="right")   # buffer_size
        table.add_column("Clf-W  [OFI | Mom | Dep]",  justify="left")
        table.add_column("Reg-W  [OFI | Mom | Dep]",  justify="left")

        all_syms = sorted(self.strategy_cache.keys())
        if not all_syms:
            # 数据还没来，占位
            table.add_row("[dim]waiting for data...[/]",
                          *["—"] * 7)
            return Panel(table, title="ML Model Status")

        for sym in all_syms:
            s = self.strategy_cache[sym]

            # ── Mode 颜色 ──────────────────────────────────────
            mode_raw = getattr(s, "ml_mode", "MARKET_MAKING")
            if mode_raw == "MOMENTUM_BUY":
                mode_str = "[green]▲ MOM_BUY[/]"
            elif mode_raw == "MOMENTUM_SELL":
                mode_str = "[red]▼ MOM_SELL[/]"
            elif mode_raw == "COLDSTART":
                mode_str = "[dim]❄ COLD[/]"
            else:
                mode_str = "[cyan]◆ MM[/]"

            # ── p_trend 颜色 ───────────────────────────────────
            p = getattr(s, "ml_p_trend", 0.5)
            p_color = ("green" if p > 0.65
                       else "red" if p < 0.35 else "white")
            p_str = f"[{p_color}]{p:.2f}[/]"

            # ── 训练状态 ───────────────────────────────────────
            trained = getattr(s, "ml_trained", False)
            train_str = "[green]✓[/]" if trained else "[red]✗[/]"

            # ── 样本量 / 缓冲区 ────────────────────────────────
            n   = getattr(s, "ml_n_samples",   0)
            buf = getattr(s, "ml_buffer_size", 0)
            n_color   = "green" if n   > 200 else ("yellow" if n   > 50 else "red")
            buf_color = "green" if buf < 100 else "yellow"
            n_str   = f"[{n_color}]{n}[/]"
            buf_str = f"[{buf_color}]{buf}[/]"

            # ── 权重 ───────────────────────────────────────────
            clf_w = getattr(s, "ml_clf_weights", [])
            reg_w = getattr(s, "ml_reg_weights", [])
            clf_str = _fmt_weights(clf_w)
            reg_str = _fmt_weights(reg_w)

            table.add_row(
                sym, mode_str, p_str, train_str,
                n_str, buf_str, clf_str, reg_str
            )

        # ── 图例（固定在面板底部）──────────────────────────────
        legend = (
            "\n[dim]特征说明  "
            "OFI=L1盘口失衡  "
            "Mom=5s动量  "
            "Dep=L5深度比  │  "
            "权重正=该特征升高→看涨/收益正  "
            "N=训练样本数  Buf=等待标注队列[/]"
        )

        return Panel(
            table,
            title="[bold magenta]ML Model Status[/]",
            subtitle=legend,
        )

    # ----------------------------------------------------------
    # 主渲染入口
    # ----------------------------------------------------------

    def render(self):
        self.layout["header"].update(self._render_header())
        self.layout["market"].update(self._render_market())
        self.layout["ml"].update(self._render_ml())
        self.layout["footer"].update(
            Panel("\n".join(self.logs), title="Logs")
        )
        return self.layout