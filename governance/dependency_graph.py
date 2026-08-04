"""AST-based production dependency graph, including delayed imports."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_EXCLUDED_ROOTS = frozenset(
    {
        ".agents",
        ".codex",
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        ".tmp",
        ".uv-cache",
        ".venv",
        "__pycache__",
        "scripts",
        "tests",
    }
)


@dataclass(frozen=True, order=True)
class ImportEdge:
    source: str
    target: str
    line: int
    delayed: bool = False


def _module_name(relative_path: Path) -> str:
    parts = list(relative_path.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def production_modules(project_root: str | Path) -> dict[str, Path]:
    root = Path(project_root).resolve()
    modules: dict[str, Path] = {}
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        if not relative.parts or relative.parts[0] in _EXCLUDED_ROOTS:
            continue
        module = _module_name(relative)
        if module:
            modules[module] = path
    return modules


def _resolve_from(source: str, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return str(node.module or "")
    package = source.split(".")[:-1]
    keep = len(package) - node.level + 1
    prefix = package[: max(0, keep)]
    if node.module:
        prefix.extend(node.module.split("."))
    return ".".join(prefix)


def _literal_import_target(call: ast.Call, delayed_names: set[str]) -> str | None:
    function = call.func
    is_loader = isinstance(function, ast.Name) and function.id in delayed_names
    if isinstance(function, ast.Attribute):
        is_loader = (
            isinstance(function.value, ast.Name)
            and function.value.id in delayed_names
            and function.attr == "import_module"
        )
    if (
        not is_loader
        or not call.args
        or not isinstance(call.args[0], ast.Constant)
        or not isinstance(call.args[0].value, str)
    ):
        return None
    return call.args[0].value


def import_edges(project_root: str | Path) -> tuple[ImportEdge, ...]:
    edges: set[ImportEdge] = set()
    for source, path in production_modules(project_root).items():
        tree = ast.parse(
            path.read_text(encoding="utf-8-sig"),
            filename=str(path),
        )
        delayed_names = {"__import__", "importlib"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    edges.add(ImportEdge(source, alias.name, node.lineno))
                    if alias.name == "importlib":
                        delayed_names.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom):
                target = _resolve_from(source, node)
                if target:
                    edges.add(ImportEdge(source, target, node.lineno))
                if node.level == 0 and node.module == "importlib":
                    for alias in node.names:
                        if alias.name == "import_module":
                            delayed_names.add(alias.asname or alias.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = _literal_import_target(node, delayed_names)
            if target:
                edges.add(
                    ImportEdge(source, target, node.lineno, delayed=True)
                )
    return tuple(sorted(edges))


def forbidden_edges(edges: Iterable[ImportEdge]) -> tuple[ImportEdge, ...]:
    violations = []
    for edge in edges:
        source_root = edge.source.split(".", 1)[0]
        target_root = edge.target.split(".", 1)[0]
        if target_root in {"scripts", "tests"}:
            violations.append(edge)
        elif source_root == "risk" and target_root == "gateway":
            violations.append(edge)
        elif source_root == "data" and target_root == "strategy":
            violations.append(edge)
    return tuple(violations)


def strongly_connected_components(
    project_root: str | Path,
) -> tuple[tuple[str, ...], ...]:
    modules = production_modules(project_root)
    adjacency = {module: set() for module in modules}
    for edge in import_edges(project_root):
        target = edge.target
        while target and target not in modules:
            target = target.rpartition(".")[0]
        if target in modules and target != edge.source:
            adjacency[edge.source].add(target)

    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(module: str) -> None:
        nonlocal index
        indices[module] = index
        lowlinks[module] = index
        index += 1
        stack.append(module)
        on_stack.add(module)
        for target in adjacency[module]:
            if target not in indices:
                visit(target)
                lowlinks[module] = min(lowlinks[module], lowlinks[target])
            elif target in on_stack:
                lowlinks[module] = min(lowlinks[module], indices[target])
        if lowlinks[module] != indices[module]:
            return
        component = []
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == module:
                break
        if len(component) > 1:
            components.append(tuple(sorted(component)))

    for module in sorted(modules):
        if module not in indices:
            visit(module)
    return tuple(sorted(components))


__all__ = [
    "ImportEdge",
    "forbidden_edges",
    "import_edges",
    "production_modules",
    "strongly_connected_components",
]
