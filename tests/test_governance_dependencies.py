from pathlib import Path

from governance.dependency_graph import (
    forbidden_edges,
    import_edges,
    strongly_connected_components,
)


ROOT = Path(__file__).resolve().parents[1]


def test_dependency_graph_detects_delayed_imports(tmp_path):
    package = tmp_path / "service"
    package.mkdir()
    (package / "loader.py").write_text(
        "from importlib import import_module as load\n"
        "load('scripts.private_tool')\n",
        encoding="ascii",
    )

    violations = forbidden_edges(import_edges(tmp_path))

    assert len(violations) == 1
    assert violations[0].source == "service.loader"
    assert violations[0].target == "scripts.private_tool"
    assert violations[0].delayed is True


def test_production_dependency_directions_are_enforced():
    violations = forbidden_edges(import_edges(ROOT))

    assert violations == ()


def test_production_dependency_graph_is_acyclic():
    components = strongly_connected_components(ROOT)

    assert components == ()
