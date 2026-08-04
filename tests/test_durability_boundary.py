import ast
from pathlib import Path

from infrastructure.durability import DurabilityError
from oms.journal import JournalCorruptionError, JournalError, JournalWriteError


def test_oms_journal_errors_implement_shared_durability_contract():
    assert issubclass(JournalError, DurabilityError)
    assert issubclass(JournalCorruptionError, DurabilityError)
    assert issubclass(JournalWriteError, DurabilityError)


def test_risk_manager_does_not_import_oms_internals():
    risk_manager_path = Path(__file__).resolve().parents[1] / "risk" / "manager.py"
    tree = ast.parse(risk_manager_path.read_text(encoding="utf-8"))

    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert not any(
        module == "oms" or module.startswith("oms.")
        for module in imported_modules
    )
