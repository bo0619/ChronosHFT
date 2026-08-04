import ast
import importlib
from pathlib import Path

from oms.component import OMSComponent, OMSComponentContext
from oms.component_state import MULTI_WRITER_STATE_OWNERS, build_state_owners
from oms.engine import OMS


OMS_DIR = Path(__file__).resolve().parents[1] / "oms"


def _component_classes():
    for path in sorted(OMS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        module = importlib.import_module(f"oms.{path.stem}")
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            component_type = getattr(module, node.name, None)
            if (
                isinstance(component_type, type)
                and component_type is not OMSComponent
                and issubclass(component_type, OMSComponent)
            ):
                yield path, node, component_type


def _self_attribute_accesses(class_node: ast.ClassDef):
    reads = set()
    writes = set()
    for node in ast.walk(class_node):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"getattr", "hasattr", "setattr", "delattr"}
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "self"
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            target = (
                writes
                if node.func.id in {"setattr", "delattr"}
                else reads
            )
            target.add(node.args[1].value)
        if not (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        ):
            continue
        target = writes if isinstance(node.ctx, (ast.Store, ast.Del)) else reads
        target.add(node.attr)
    return reads, writes


def test_every_component_dependency_is_declared_with_write_access_separated():
    audited = []
    for path, class_node, component_type in _component_classes():
        audited.append(component_type.__name__)
        reads, writes = _self_attribute_accesses(class_node)
        local_attributes = (
            set(dir(component_type))
            | set(component_type.LOCAL_STATE)
            | {"_context"}
        )
        shared_accesses = (reads | writes) - local_attributes
        declared_reads = set(component_type.OWNER_READS)
        declared_writes = set(component_type.OWNER_WRITES)

        assert shared_accesses == declared_reads | declared_writes, (
            path.name,
            component_type.__name__,
            sorted(shared_accesses - declared_reads - declared_writes),
            sorted((declared_reads | declared_writes) - shared_accesses),
        )
        assert writes - local_attributes <= declared_writes, (
            path.name,
            component_type.__name__,
            sorted(writes - local_attributes - declared_writes),
        )

    assert len(audited) >= 18


def test_component_base_has_no_transparent_owner_proxy():
    source = (OMS_DIR / "component.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    component_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "OMSComponent"
    )
    methods = {
        node.name
        for node in component_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    component_attributes = {
        node.attr
        for node in ast.walk(component_class)
        if isinstance(node, ast.Attribute)
    }

    assert "__getattribute__" not in methods
    assert "_owner" not in component_attributes


def test_component_context_contains_bindings_instead_of_the_oms_facade():
    assert "_facade" not in OMSComponentContext.__dataclass_fields__
    source = (OMS_DIR / "component.py").read_text(encoding="utf-8")
    assert "getattr(self._facade" not in source
    assert "setattr(self._facade" not in source


def test_every_shared_state_field_has_one_canonical_storage_owner():
    component_types = [
        component_type
        for _path, _node, component_type in _component_classes()
    ]
    owners = build_state_owners(component_types)

    assert owners
    assert len(owners) == len(set(owners))
    assert set(OMS._component_state_field_owners).issubset(owners)
    for field, owner in MULTI_WRITER_STATE_OWNERS.items():
        assert owners[field] == owner
