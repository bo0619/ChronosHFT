import sys
import types
import unittest
from datetime import datetime

if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")
    requests_stub.get = lambda *args, **kwargs: None
    sys.modules["requests"] = requests_stub

from event.type import AccountData
from ui.dashboard import TUIDashboard


class DashboardBalanceTests(unittest.TestCase):
    def test_header_shows_usdt_and_usdc_balances(self):
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

        panel = dashboard._render_header()
        content = str(panel.renderable.renderable)

        self.assertIn("USDT: 900.00/850.00", content)
        self.assertIn("USDC: 225.00/130.00", content)


if __name__ == "__main__":
    unittest.main()
