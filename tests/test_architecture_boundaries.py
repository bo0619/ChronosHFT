import ast
from dataclasses import fields
from pathlib import Path

from infrastructure.runtime_application import RuntimeApplicationServices


ROOT = Path(__file__).resolve().parents[1]
MODULE_LINE_BUDGETS = {
    "main.py": 1250,
    "infrastructure/runtime_application.py": 850,
    "infrastructure/live_config_guard.py": 3631,
    "risk/manager.py": 850,
    "risk/independent_supervisor.py": 150,
    "risk/sidecar_core.py": 850,
    "gateway/binance/gateway.py": 875,
    "gateway/binance/paper_gateway.py": 2641,
    "oms/reconciler.py": 875,
    "oms/lifecycle_controller.py": 550,
    "oms/order_submission.py": 1981,
    "strategy/adaptive_pipeline.py": 124,
    "strategy/avellaneda_stoikov.py": 1701,
    "strategy/glft.py": 3159,
    "strategy/model_readiness.py": 3056,
    "strategy/quote_decision.py": 164,
    "ui/web_dashboard.py": 3422,
}

# These are ratchets, not target sizes.  They freeze today's constructor and
# directly-owned state surface so future extraction work can only lower them.
CONSTRUCTOR_PARAMETER_BUDGETS = {
    ("gateway/binance/paper_gateway.py", "BinancePaperGateway"): 3,
    ("oms/order_submission.py", "OMSOrderSubmission"): 1,
    ("strategy/avellaneda_stoikov.py", "AvellanedaStoikovStrategy"): 8,
    ("strategy/glft.py", "GLFTStrategy"): 8,
    ("ui/web_dashboard.py", "LocalWebDashboard"): 23,
}
OWNED_STATE_BUDGETS = {
    ("gateway/binance/paper_gateway.py", "BinancePaperGateway"): 68,
    ("oms/order_submission.py", "OMSOrderSubmission"): 1,
    ("strategy/adaptive_pipeline.py", "AdaptiveQuotePipeline"): 0,
    ("strategy/avellaneda_stoikov.py", "AvellanedaStoikovStrategy"): 59,
    ("strategy/glft.py", "GLFTStrategy"): 95,
    ("strategy/quote_decision.py", "QuoteDecisionEngine"): 0,
    ("ui/web_dashboard.py", "LocalWebDashboard"): 60,
}
OMS_PORT_OWNED_MODULES = (
    "oms/account_manager.py",
    "oms/account_truth.py",
    "oms/engine.py",
    "oms/exposure.py",
    "oms/order_policy.py",
    "oms/rpi_calibration_runtime.py",
    "oms/validator.py",
)
PROCESS_SINGLETON_MODULES = frozenset(
    {
        "data.cache",
        "data.ref_data",
        "infrastructure.time_service",
    }
)


def _tree(relative_path: str) -> ast.Module:
    return ast.parse(
        (ROOT / relative_path).read_text(encoding="utf-8-sig")
    )


def _function(tree: ast.AST, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == name
    )


def _constructor_parameter_count(class_node: ast.ClassDef) -> int:
    constructor = next(
        node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "__init__"
    )
    assert constructor.args.vararg is None, (
        f"{class_node.name} must not hide constructor dependencies in *args"
    )
    assert constructor.args.kwarg is None, (
        f"{class_node.name} must not hide constructor dependencies in **kwargs"
    )
    parameters = [
        *constructor.args.posonlyargs,
        *constructor.args.args,
        *constructor.args.kwonlyargs,
    ]
    if parameters and parameters[0].arg == "self":
        parameters = parameters[1:]
    return len(parameters)


def _directly_owned_state(class_node: ast.ClassDef) -> set[str]:
    state = {
        node.attr
        for node in ast.walk(class_node)
        if isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Store)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    }
    self_setattr_calls = [
        node
        for node in ast.walk(class_node)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "setattr"
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "self"
    ]
    assert all(
        isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
        for node in self_setattr_calls
    ), f"{class_node.name} must not use dynamic setattr for owned state"
    state.update(
        node.args[1].value
        for node in self_setattr_calls
    )
    return state


def _imported_modules(tree: ast.Module) -> set[str]:
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def test_composition_roots_remain_below_reviewable_size_budgets():
    for relative_path, maximum_lines in MODULE_LINE_BUDGETS.items():
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert len(source.splitlines()) <= maximum_lines, relative_path


def test_large_components_do_not_expand_constructor_dependency_surfaces():
    for (relative_path, class_name), maximum_parameters in (
        CONSTRUCTOR_PARAMETER_BUDGETS.items()
    ):
        actual = _constructor_parameter_count(_class(_tree(relative_path), class_name))
        assert actual <= maximum_parameters, (
            f"{relative_path}:{class_name} has {actual} constructor parameters; "
            f"budget is {maximum_parameters}"
        )


def test_large_components_do_not_expand_direct_state_ownership():
    for (relative_path, class_name), maximum_attributes in OWNED_STATE_BUDGETS.items():
        state = _directly_owned_state(_class(_tree(relative_path), class_name))
        assert len(state) <= maximum_attributes, (
            f"{relative_path}:{class_name} directly owns {len(state)} state "
            f"attributes; budget is {maximum_attributes}: {sorted(state)}"
        )


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


def test_oms_domain_services_do_not_import_process_singletons():
    offenders = {
        relative_path: sorted(
            _imported_modules(_tree(relative_path))
            & PROCESS_SINGLETON_MODULES
        )
        for relative_path in OMS_PORT_OWNED_MODULES
        if _imported_modules(_tree(relative_path))
        & PROCESS_SINGLETON_MODULES
    }

    assert offenders == {}
