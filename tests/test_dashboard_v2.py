import sys
import types
import unittest
from datetime import datetime
from io import StringIO

if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")
    requests_stub.get = lambda *args, **kwargs: None
    sys.modules["requests"] = requests_stub

from rich.console import Console

from event.type import AccountData, StrategyData
from ui.dashboard_v2 import TUIDashboard


class DashboardV2Tests(unittest.TestCase):
    def render_to_text(self, renderable, width=180):
        buffer = StringIO()
        console = Console(file=buffer, width=width, force_terminal=False, color_system=None)
        console.print(renderable)
        return buffer.getvalue()

    def test_header_shows_balances_and_system_health(self):
        dashboard = TUIDashboard()
        dashboard.update_account(
            AccountData(
                balance=1100.0,
                equity=1125.0,
                available=980.0,
                used_margin=145.0,
                datetime=datetime.utcnow(),
                balances={"USDT": 900.0, "USDC": 225.0},
                available_balances={"USDT": 850.0, "USDC": 130.0},
            )
        )
        dashboard.update_strategy(
            StrategyData(
                symbol="BTCUSDT",
                fair_value=100.0,
                alpha_bps=3.5,
                params={"Health": "RECONCILING"},
            )
        )

        text = self.render_to_text(dashboard._render_header())
        self.assertIn("USDT: 900.00/850.00", text)
        self.assertIn("USDC: 225.00/130.00", text)
        self.assertIn("System: RECONCILING", text)

    def test_signal_board_groups_core_metrics(self):
        dashboard = TUIDashboard()
        dashboard.update_strategy(
            StrategyData(
                symbol="ADAUSDC",
                fair_value=0.2665,
                alpha_bps=4.2,
                params={
                    "State": "ENTERING",
                    "Mode": "GTX",
                    "Conf": "0.64",
                    "10s": "+3.1",
                    "30s": "+2.4",
                    "Vel": "+1.2",
                    "Regime": "OK",
                    "Size": "1.80x",
                    "Blend": {"1s": 0.1, "10s": 0.5, "30s": 0.4},
                    "Weights": {"Imb": 0.4},
                },
            )
        )

        text = self.render_to_text(dashboard._render_signal_board())
        self.assertIn("Signal Board", text)
        self.assertIn("Conf", text)
        self.assertIn("Regime", text)
        self.assertIn("Size", text)
        self.assertNotIn("Weights", text)

    def test_focus_panel_surfaces_health_and_reject(self):
        dashboard = TUIDashboard()
        dashboard.update_strategy(
            StrategyData(
                symbol="DOGEUSDC",
                fair_value=0.0953,
                alpha_bps=-1.8,
                params={
                    "State": "FLAT",
                    "Mode": "BLOCKED:spread",
                    "Conf": "0.11",
                    "1s": "+0.2",
                    "10s": "-0.8",
                    "30s": "-1.6",
                    "MakerCost": "1.2",
                    "TakerCost": "5.6",
                    "MakerReq": "1.8",
                    "TakerReq": "22.0",
                    "MEdge": "+0.0",
                    "TEdge": "-0.6",
                    "ExitEWMA": "+0.0",
                    "Health": "HALT:test_gateway",
                    "Reject": "insufficient_margin",
                    "Blend": {"1s": 0.1, "10s": 0.5, "30s": 0.4},
                    "Weights": {"Imb": 0.42},
                    "Train": {"1s": 3, "10s": 2, "30s": 1},
                },
            )
        )

        text = self.render_to_text(dashboard._render_focus())
        self.assertIn("HALT:test_gateway", text)
        self.assertIn("insufficient_margin", text)
        self.assertIn("Maker 1.2 | Taker 5.6", text)

    def test_model_panel_surfaces_weights_and_blend(self):
        dashboard = TUIDashboard()
        dashboard.update_strategy(
            StrategyData(
                symbol="BTCUSDT",
                fair_value=100.0,
                alpha_bps=2.1,
                params={
                    "State": "HOLDING",
                    "Blend": {"1s": 0.1, "10s": 0.5, "30s": 0.4},
                    "Weights": {"Imb": 0.42, "Mom": 0.21, "dSp": -0.33, "Dep": -0.12},
                    "Train": {"1s": 12, "10s": 8, "30s": 5},
                },
            )
        )

        text = self.render_to_text(dashboard._render_model(), width=140)
        self.assertIn("Blend", text)
        self.assertIn("1s:+0.10", text)
        self.assertIn("Top+", text)
        self.assertIn("Imb +0.42", text)
        self.assertIn("Top-", text)
        self.assertIn("dSp -0.33", text)


if __name__ == "__main__":
    unittest.main()