import ast
from pathlib import Path

from infrastructure.oms_risk_port import RISK_OMS_PORT_MEMBERS, RiskOMSPort
from oms.engine import OMS


ROOT = Path(__file__).resolve().parents[1]


class _Engine:
    def put(self, _event):
        return None

    def register(self, _event_type, _handler):
        return None


class _Gateway:
    gateway_name = "BINANCE"

    def get_account_info(self):
        return {
            "totalWalletBalance": "1000",
            "totalInitialMargin": "0",
        }

    def get_all_positions(self):
        return []

    def get_open_orders(self):
        return []

    def cancel_all_orders(self, _symbol):
        return type("Response", (), {"status_code": 200})()

    def set_countdown_cancel_all(self, _symbol, _countdown_time_ms):
        return type("Response", (), {"status_code": 200})()


def _risk_oms_members(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    members = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "self"
            and node.value.attr == "oms"
        ):
            members.add(node.attr)
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"getattr", "hasattr"}
            and len(node.args) >= 2
        ):
            continue
        target, raw_name = node.args[:2]
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and target.attr == "oms"
            and isinstance(raw_name, ast.Constant)
            and isinstance(raw_name.value, str)
        ):
            members.add(raw_name.value)
    return members


def test_risk_only_uses_the_neutral_oms_port_surface():
    used = set()
    for relative_path in ("risk/manager.py", "risk/venue_dms.py"):
        used.update(_risk_oms_members(ROOT / relative_path))
    assert used <= RISK_OMS_PORT_MEMBERS, sorted(
        used - RISK_OMS_PORT_MEMBERS
    )


def test_risk_port_exposes_no_oms_private_members():
    assert not [
        name for name in RISK_OMS_PORT_MEMBERS if name.startswith("_")
    ]


def test_risk_package_does_not_import_oms_implementation_modules():
    violations = []
    for path in sorted((ROOT / "risk").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            for module in modules:
                if module == "oms" or module.startswith("oms."):
                    violations.append((path.name, node.lineno, module))

    assert not violations


def test_oms_facade_declares_every_risk_port_method():
    dynamic_fields = set(RiskOMSPort.__annotations__)
    methods = RISK_OMS_PORT_MEMBERS - dynamic_fields

    assert methods <= set(dir(OMS)), sorted(methods - set(dir(OMS)))


def test_initialized_oms_satisfies_the_runtime_risk_port():
    config = {
        "symbols": ["BTCUSDT"],
        "account": {"initial_balance_usdt": 1000.0, "leverage": 10},
        "backtest": {"taker_fee": 0.0, "maker_fee": 0.0},
        "oms": {
            "journal_enabled": False,
            "replay_journal_on_startup": False,
        },
        "risk": {"limits": {"max_pos_notional": 5000.0}},
    }
    oms = OMS(_Engine(), _Gateway(), config)
    try:
        assert isinstance(oms, RiskOMSPort)
    finally:
        assert oms.stop()["stopped"] is True
