import ast
from dataclasses import fields
from pathlib import Path

from infrastructure.runtime_application import RuntimeApplicationServices


ROOT = Path(__file__).resolve().parents[1]
MODULE_LINE_BUDGETS = {
    "main.py": 1250,
    "infrastructure/runtime_application.py": 850,
    "risk/manager.py": 850,
    "risk/independent_supervisor.py": 150,
    "risk/sidecar_core.py": 850,
    "gateway/binance/gateway.py": 875,
    "oms/reconciler.py": 875,
    "oms/lifecycle_controller.py": 550,
}


def _tree(relative_path: str) -> ast.Module:
    return ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))


def _function(tree: ast.AST, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_composition_roots_remain_below_reviewable_size_budgets():
    for relative_path, maximum_lines in MODULE_LINE_BUDGETS.items():
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert len(source.splitlines()) <= maximum_lines, relative_path


def test_main_entrypoint_is_only_an_application_delegate():
    run_main = _function(_tree("main.py"), "_run_main")

    assert len(run_main.body) == 1
    assert isinstance(run_main.body[0], ast.Return)
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for statement in run_main.body
        for node in ast.walk(statement)
    )


def test_runtime_application_has_five_named_capability_groups():
    assert [field.name for field in fields(RuntimeApplicationServices)] == [
        "configuration",
        "platform",
        "factories",
        "safety",
        "loop",
    ]


def test_runtime_application_methods_do_not_hide_nested_closures():
    tree = _tree("infrastructure/runtime_application.py")
    application = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "RuntimeApplication"
    )
    for method in application.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nested = [
            node
            for statement in method.body
            for node in ast.walk(statement)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        assert not nested, method.name
