from datetime import datetime
import re
from typing import Any, Dict, List, Optional

from rich.align import Align
from rich.box import SIMPLE_HEAD
from rich.console import Group
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from event.type import AccountData, StrategyData
from data.ref_data import ref_data_manager


class TUIDashboard:
    def __init__(self):
        self.layout = Layout()
        self.layout.split(
            Layout(name="header", size=4),
            Layout(name="body", ratio=1),
            Layout(name="footer", size=9),
        )
        self.layout["body"].split_row(
            Layout(name="left", ratio=4),
            Layout(name="center", ratio=5),
            Layout(name="right", ratio=4),
        )
        self.layout["left"].split_column(
            Layout(name="market", ratio=3),
            Layout(name="execution", ratio=2),
        )
        self.layout["center"].split_column(
            Layout(name="signals", ratio=1),
        )
        self.layout["right"].split_column(
            Layout(name="focus", ratio=3),
            Layout(name="model", ratio=2),
        )

        self.account_data = None
        self.market_cache = {}
        self.position_cache = {}
        self.strategy_cache = {}
        self.runtime_metrics = {}
        self.logs = []
        self.max_logs = 8

    def update_account(self, data: AccountData):
        self.account_data = data

    def update_position(self, pos):
        self.position_cache[pos.symbol] = pos

    def update_market(self, ob):
        bid, _ = ob.get_best_bid()
        ask, _ = ob.get_best_ask()
        self.market_cache[ob.symbol] = {"bid": bid, "ask": ask}

    def update_strategy(self, data: StrategyData):
        self.strategy_cache[data.symbol] = data

    def update_runtime_metrics(self, metrics: Dict[str, Any]):
        self.runtime_metrics = metrics or {}

    def add_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{ts}] {msg}")
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    def _fmt_price(self, symbol: str, price: float) -> str:
        if not price:
            return "-"
        info = ref_data_manager.get_info(symbol)
        prec = info.price_precision if info else 4
        return f"{price:.{prec}f}"

    def _fmt_asset_balance(self, asset: str) -> str:
        if not self.account_data:
            return f"{asset}: -/-/-"
        wallet = self.account_data.balances.get(asset)
        available = self.account_data.available_balances.get(asset)
        budget = self.account_data.trading_budget_by_asset.get(asset)
        wallet_s = "-" if wallet is None else f"{wallet:.2f}"
        available_s = "-" if available is None else f"{available:.2f}"
        budget_s = "-" if budget is None or budget <= 0.0 else f"{budget:.2f}"
        return f"{asset}: {wallet_s}/{available_s}/{budget_s}"

    def _extract_number(self, value: Any) -> Optional[float]:
        if isinstance(value, (int, float)):
            return float(value)
        if not isinstance(value, str):
            return None
        match = re.search(r"[-+]?\d+(?:\.\d+)?", value)
        if not match:
            return None
        try:
            return float(match.group(0))
        except ValueError:
            return None

    def _param(self, strat: StrategyData, key: str, default: Any = "-") -> Any:
        return strat.params.get(key, default)

    def _param_str(self, strat: StrategyData, key: str, default: str = "-") -> str:
        value = self._param(strat, key, default)
        return default if value is None else str(value)

    def _style_state(self, value: str) -> str:
        upper = value.upper()
        if "HOLD" in upper or "ENTER" in upper or "EXIT" in upper:
            return "bold cyan"
        if "WARM" in upper:
            return "yellow"
        return "white"

    def _style_mode(self, value: str) -> str:
        upper = value.upper()
        if "IOC" in upper:
            return "bold red"
        if "GTX" in upper:
            return "bold green"
        if "BLOCKED" in upper or "PAUSED" in upper:
            return "yellow"
        return "white"

    def _style_regime(self, value: str) -> str:
        if value == "OK":
            return "green"
        if value in {"low_conf", "spread", "sigma", "spread_sigma"}:
            return "yellow"
        return "red"

    def _style_health(self, value: str) -> str:
        upper = value.upper()
        if "LIVE" in upper:
            return "green"
        if "RECON" in upper or "DIRTY" in upper or "FROZEN" in upper:
            return "yellow"
        if "HALT" in upper or "ERROR" in upper:
            return "red"
        return "white"

    def _style_signal(self, value: float) -> str:
        if value > 0:
            return "bright_green"
        if value < 0:
            return "bright_red"
        return "white"

    def _display_symbols(self, limit: int = 8) -> List[str]:
        universe = set(self.market_cache.keys()) | set(self.position_cache.keys()) | set(self.strategy_cache.keys())

        def rank(symbol: str):
            pos = abs(getattr(self.position_cache.get(symbol), "volume", 0.0))
            strat = self.strategy_cache.get(symbol)
            if strat is None:
                return (1, 1, 0.0, symbol)
            state = self._param_str(strat, "State", "-")
            active = 0 if state not in {"FLAT", "-"} else 1
            return (0 if pos > 0 else 1, active, -abs(strat.alpha_bps), symbol)

        return sorted(universe, key=rank)[:limit]

    def _focus_symbol(self) -> Optional[str]:
        symbols = self._display_symbols(limit=12)
        if not symbols:
            return None
        for sym in symbols:
            pos = getattr(self.position_cache.get(sym), "volume", 0.0)
            if abs(pos) > 1e-8:
                return sym
        for sym in symbols:
            strat = self.strategy_cache.get(sym)
            if strat and self._param_str(strat, "State", "-") != "FLAT":
                return sym
        return symbols[0]

    def _system_health_summary(self) -> str:
        for sym in self._display_symbols(limit=12):
            strat = self.strategy_cache.get(sym)
            if not strat:
                continue
            health = self._param_str(strat, "Health", "")
            if health and health != "-":
                return health
        return "LIVE" if self.account_data else "-"

    def _system_health_detail(self) -> str:
        for sym in self._display_symbols(limit=12):
            strat = self.strategy_cache.get(sym)
            if not strat:
                continue
            detail = self._param_str(strat, "HealthDetail", "")
            if detail and detail != "-":
                return detail
        return ""

    def _manual_rearm_hint(self) -> str:
        for sym in self._display_symbols(limit=12):
            strat = self.strategy_cache.get(sym)
            if not strat:
                continue
            if self._param_str(strat, "Rearm", "N") == "Y":
                return "python main.py --admin-command rearm --admin-reason operator_ack"
        return ""

    def _runtime_summary(self) -> str:
        engine = self.runtime_metrics.get("event_engine", {})
        strategy = self.runtime_metrics.get("strategy_runtime", {})
        if not engine and not strategy:
            return "-"

        engine_bits = []
        queues = engine.get("queues", {})
        if queues:
            engine_bits.append(
                f"EV M{int(queues.get('market_depth', 0))}/X{int(queues.get('execution_depth', 0))}/C{int(queues.get('cold_depth', 0))}"
            )
        market_lane = engine.get("lanes", {}).get("market", {})
        execution_lane = engine.get("lanes", {}).get("execution", {})
        market_backlog = float(market_lane.get("oldest_queued_ms", 0.0) or 0.0)
        execution_backlog = float(execution_lane.get("oldest_queued_ms", 0.0) or 0.0)
        if market_backlog or execution_backlog:
            engine_bits.append(f"Q {market_backlog:.0f}/{execution_backlog:.0f}ms")

        strategy_bits = []
        if strategy:
            strategy_bits.append(
                f"ST C{int(strategy.get('control_depth', 0))}/M{int(strategy.get('market_depth', 0))}"
            )
            strategy_wait = max(
                float(strategy.get("oldest_market_wait_ms", 0.0) or 0.0),
                float(strategy.get("oldest_control_wait_ms", 0.0) or 0.0),
                float(strategy.get("inflight_wait_ms", 0.0) or 0.0),
            )
            if strategy_wait:
                strategy_bits.append(f"W {strategy_wait:.0f}ms")
            async_worker = strategy.get("async_worker", {})
            if async_worker:
                status = "UP" if async_worker.get("alive") else "DOWN"
                alive_workers = int(async_worker.get("alive_workers", 0))
                worker_count = int(async_worker.get("worker_count", 0))
                strategy_bits.append(
                    f"AP {status} {alive_workers}/{worker_count} D{int(async_worker.get('deferred_depth', 0))}"
                )
                recovering = int(async_worker.get("recovering_symbols", 0))
                if recovering:
                    strategy_bits.append(f"RW {recovering}")
                quarantined = int(async_worker.get("quarantined_symbols", 0))
                if quarantined:
                    strategy_bits.append(f"Q {quarantined}")
                standby_workers = int(async_worker.get("standby_workers", 0))
                if standby_workers:
                    strategy_bits.append(f"SB {standby_workers}")

        return " | ".join(part for part in (" ".join(engine_bits), " ".join(strategy_bits)) if part).strip() or "-"

    def _smart_dict(self, value: Any) -> str:
        if not isinstance(value, dict) or not value:
            return "-"
        parts = []
        for key, item in value.items():
            if isinstance(item, float):
                parts.append(f"{key}:{item:+.2f}")
            elif isinstance(item, int):
                parts.append(f"{key}:{item}")
        return "  ".join(parts) if parts else "-"

    def _numeric_dict(self, value: Any) -> Dict[str, float]:
        if not isinstance(value, dict):
            return {}
        numeric = {}
        for key, item in value.items():
            if isinstance(item, (int, float)):
                numeric[str(key)] = float(item)
        return numeric

    def _top_weights(self, weights: Dict[str, float], limit: int = 4) -> tuple[List[str], List[str]]:
        if not weights:
            return [], []
        positives = [
            f"{key} {value:+.2f}"
            for key, value in sorted(weights.items(), key=lambda item: item[1], reverse=True)
            if value > 0
        ][:limit]
        negatives = [
            f"{key} {value:+.2f}"
            for key, value in sorted(weights.items(), key=lambda item: item[1])
            if value < 0
        ][:limit]
        return positives, negatives

    def _render_header(self):
        if not self.account_data:
            return Panel("Loading...", style="bold white")

        acc = self.account_data
        margin_pct = (acc.used_margin / acc.equity * 100) if acc.equity > 0 else 0.0
        equity_style = "green" if acc.equity >= acc.balance else "red"
        health = self._system_health_summary()
        health_style = self._style_health(health)

        top_line = (
            f"Equity: [{equity_style}]{acc.equity:.2f}[/] | "
            f"Balance: {acc.balance:.2f} | "
            f"Avail: {acc.available:.2f} | "
            f"Budget: {acc.budget_available:.2f}/{acc.budget_balance:.2f} | "
            f"Margin: {acc.used_margin:.2f} ({margin_pct:.1f}%)"
        )
        health_detail = self._system_health_detail()
        bottom_line = (
            f"[{health_style}]System: {health}[/] | "
            f"{self._fmt_asset_balance('USDT')} | {self._fmt_asset_balance('USDC')} | "
            f"Runtime: {self._runtime_summary()}"
        )
        if health_detail:
            bottom_line += f" | Reason: {health_detail[:52]}"
        rearm_hint = self._manual_rearm_hint()
        if rearm_hint:
            bottom_line += f" | RearmCmd: {rearm_hint}"
        return Panel(Align.center(f"{top_line}\n{bottom_line}"), title="Account", border_style="blue")

    def _render_market(self):
        table = Table(show_header=True, header_style="bold cyan", expand=True, box=SIMPLE_HEAD)
        table.add_column("Sym", width=12)
        table.add_column("Bid", justify="right")
        table.add_column("Ask", justify="right")
        table.add_column("Spr", justify="right")
        table.add_column("Pos", justify="right")

        symbols = self._display_symbols(limit=10)
        if not symbols:
            table.add_row("-", "-", "-", "-", "-")
        for sym in symbols:
            market = self.market_cache.get(sym, {"bid": 0.0, "ask": 0.0})
            bid = market.get("bid", 0.0)
            ask = market.get("ask", 0.0)
            mid = (bid + ask) / 2.0 if bid and ask else 0.0
            spread_bps = ((ask - bid) / mid * 10000.0) if mid > 0 else 0.0
            pos = getattr(self.position_cache.get(sym), "volume", 0.0)
            pos_style = "green" if pos > 0 else "red" if pos < 0 else "dim"
            table.add_row(
                sym,
                f"[green]{self._fmt_price(sym, bid)}[/]" if bid else "-",
                f"[red]{self._fmt_price(sym, ask)}[/]" if ask else "-",
                f"{spread_bps:.1f}" if mid > 0 else "-",
                f"[{pos_style}]{pos:.2f}[/]" if abs(pos) > 1e-8 else "-",
            )
        return Panel(table, title="Market & Positions", border_style="cyan")

    def _render_signal_board(self):
        table = Table(show_header=True, header_style="bold magenta", expand=True, box=SIMPLE_HEAD)
        table.add_column("Sym", width=12)
        table.add_column("State", justify="left")
        table.add_column("Mode", justify="left")
        table.add_column("Sig", justify="right")
        table.add_column("Conf", justify="right")
        table.add_column("10s", justify="right")
        table.add_column("30s", justify="right")
        table.add_column("Vel", justify="right")
        table.add_column("Regime", justify="left")
        table.add_column("Size", justify="right")

        symbols = self._display_symbols(limit=8)
        if not symbols:
            table.add_row("-", "-", "-", "-", "-", "-", "-", "-", "-", "-")
        for sym in symbols:
            strat = self.strategy_cache.get(sym)
            if strat is None:
                table.add_row(sym, "-", "-", "-", "-", "-", "-", "-", "-", "-")
                continue
            sig_style = self._style_signal(strat.alpha_bps)
            table.add_row(
                sym,
                f"[{self._style_state(self._param_str(strat, 'State'))}]{self._param_str(strat, 'State')}[/]",
                f"[{self._style_mode(self._param_str(strat, 'Mode'))}]{self._param_str(strat, 'Mode')}[/]",
                f"[{sig_style}]{strat.alpha_bps:+.2f}[/]",
                self._param_str(strat, "Conf"),
                self._param_str(strat, "10s"),
                self._param_str(strat, "30s"),
                self._param_str(strat, "Vel"),
                f"[{self._style_regime(self._param_str(strat, 'Regime'))}]{self._param_str(strat, 'Regime')}[/]",
                self._param_str(strat, "Size"),
            )
        return Panel(table, title="Signal Board", border_style="magenta")

    def _render_execution_board(self):
        table = Table(show_header=True, header_style="bold yellow", expand=True, box=SIMPLE_HEAD)
        table.add_column("Sym", width=12)
        table.add_column("Spread", justify="right")
        table.add_column("Sigma", justify="right")
        table.add_column("MReq", justify="right")
        table.add_column("TReq", justify="right")
        table.add_column("MEdge", justify="right")
        table.add_column("TEdge", justify="right")
        table.add_column("Exit", justify="right")
        table.add_column("Win", justify="right")
        table.add_column("Health", justify="left")

        symbols = self._display_symbols(limit=8)
        if not symbols:
            table.add_row("-", "-", "-", "-", "-", "-", "-", "-", "-", "-")
        for sym in symbols:
            strat = self.strategy_cache.get(sym)
            if strat is None:
                table.add_row(sym, "-", "-", "-", "-", "-", "-", "-", "-", "-")
                continue
            health = self._param_str(strat, "Health")
            table.add_row(
                sym,
                self._param_str(strat, "Spread"),
                self._param_str(strat, "Sigma"),
                self._param_str(strat, "MakerReq"),
                self._param_str(strat, "TakerReq"),
                self._param_str(strat, "MEdge"),
                self._param_str(strat, "TEdge"),
                self._param_str(strat, "ExitEWMA"),
                self._param_str(strat, "WinEWMA"),
                f"[{self._style_health(health)}]{health[:14]}[/]" if health != "-" else "-",
            )
        return Panel(table, title="Execution & Risk", border_style="yellow")

    def _render_focus(self):
        sym = self._focus_symbol()
        if not sym:
            return Panel("Waiting for data...", title="Focus", border_style="green")

        strat = self.strategy_cache.get(sym)
        market = self.market_cache.get(sym, {"bid": 0.0, "ask": 0.0})
        pos = self.position_cache.get(sym)
        if strat is None:
            return Panel(f"{sym}\nNo strategy data yet.", title="Focus", border_style="green")

        bid = self._fmt_price(sym, market.get("bid", 0.0))
        ask = self._fmt_price(sym, market.get("ask", 0.0))
        lines = [
            Text.assemble(("Symbol: ", "bold"), (sym, "bold cyan")),
            Text.assemble(("State: ", "bold"), (self._param_str(strat, "State"), self._style_state(self._param_str(strat, "State")))),
            Text.assemble(("Mode: ", "bold"), (self._param_str(strat, "Mode"), self._style_mode(self._param_str(strat, "Mode")))),
            Text.assemble(("Signal: ", "bold"), (f"{strat.alpha_bps:+.2f}", self._style_signal(strat.alpha_bps)), (" | Conf: ", "bold"), (self._param_str(strat, "Conf"), "white")),
            Text.assemble(("Pred: ", "bold"), (f"1s {self._param_str(strat, '1s')} | 10s {self._param_str(strat, '10s')} | 30s {self._param_str(strat, '30s')}", "white")),
            Text.assemble(("Market: ", "bold"), (f"{bid} / {ask}", "white"), (" | Pos: ", "bold"), (f"{getattr(pos, 'volume', 0.0):.2f}" if pos and abs(pos.volume) > 1e-8 else "-", "white")),
            Text.assemble(("Costs: ", "bold"), (f"Maker {self._param_str(strat, 'MakerCost')} | Taker {self._param_str(strat, 'TakerCost')}", "white")),
            Text.assemble(("Req: ", "bold"), (f"Maker {self._param_str(strat, 'MakerReq')} | Taker {self._param_str(strat, 'TakerReq')}", "white")),
            Text.assemble(("Exec: ", "bold"), (f"MEdge {self._param_str(strat, 'MEdge')} | TEdge {self._param_str(strat, 'TEdge')} | Exit {self._param_str(strat, 'ExitEWMA')}", "white")),
            Text.assemble(
                ("Health: ", "bold"),
                (self._param_str(strat, "Health"), self._style_health(self._param_str(strat, "Health"))),
                (" | Rearm: ", "bold"),
                (self._param_str(strat, "Rearm"), "yellow" if self._param_str(strat, "Rearm") == "Y" else "white"),
            ),
            Text.assemble(("Reason: ", "bold"), (self._param_str(strat, "HealthDetail"), "yellow" if self._param_str(strat, "HealthDetail") != "-" else "dim")),
            Text.assemble(("Block: ", "bold"), (self._param_str(strat, "Block"), "yellow" if self._param_str(strat, "Block") != "-" else "dim")),
            Text.assemble(("Reject: ", "bold"), (self._param_str(strat, "Reject"), "yellow" if self._param_str(strat, "Reject") != "-" else "dim")),
        ]
        return Panel(Group(*lines), title="Focus", border_style="green")

    def _render_model(self):
        sym = self._focus_symbol()
        if not sym:
            return Panel("Waiting for model diagnostics...", title="Model", border_style="bright_blue")

        strat = self.strategy_cache.get(sym)
        if strat is None:
            return Panel(f"{sym}\nNo strategy data yet.", title="Model", border_style="bright_blue")

        blend = self._numeric_dict(self._param(strat, "Blend", {}))
        weights = self._numeric_dict(self._param(strat, "Weights", {}))
        train = self._numeric_dict(self._param(strat, "Train", {}))
        positives, negatives = self._top_weights(weights, limit=4)

        lines = [
            Text.assemble(("Symbol: ", "bold"), (sym, "bold cyan")),
            Text.assemble(("Blend: ", "bold"), (self._smart_dict(blend), "white")),
            Text.assemble(("Train: ", "bold"), (self._smart_dict(train), "white")),
            Text.assemble(("Top+: ", "bold"), (" | ".join(positives) if positives else "-", "green" if positives else "dim")),
            Text.assemble(("Top-: ", "bold"), (" | ".join(negatives) if negatives else "-", "red" if negatives else "dim")),
        ]

        if weights:
            ranked = sorted(weights.items(), key=lambda item: abs(item[1]), reverse=True)[:6]
            lines.append(
                Text.assemble(
                    ("Active: ", "bold"),
                    (" | ".join(f"{key} {value:+.2f}" for key, value in ranked), "white"),
                )
            )

        return Panel(Group(*lines), title="Model", border_style="bright_blue")

    def _render_logs(self):
        if not self.logs:
            return Panel("No logs yet.", title="Logs")

        rendered = []
        for line in self.logs:
            style = "white"
            upper = line.upper()
            if "[ERROR]" in upper or "HALT" in upper:
                style = "bold red"
            elif "[WARNING]" in upper or "RECONCILING" in upper or "FROZEN" in upper:
                style = "yellow"
            elif "[INFO]" in upper or "LIVE" in upper:
                style = "green"
            rendered.append(Text(line, style=style))
        return Panel(Group(*rendered), title="Logs", border_style="white")

    def render(self):
        self.layout["header"].update(self._render_header())
        self.layout["market"].update(self._render_market())
        self.layout["execution"].update(self._render_execution_board())
        self.layout["signals"].update(self._render_signal_board())
        self.layout["focus"].update(self._render_focus())
        self.layout["model"].update(self._render_model())
        self.layout["footer"].update(self._render_logs())
        return self.layout
