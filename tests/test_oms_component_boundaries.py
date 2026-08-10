import ast
import importlib
from pathlib import Path

from oms.component import OMSComponent, OMSComponentContext
from oms.component_state import MULTI_WRITER_STATE_OWNERS, build_state_owners
from oms.engine import OMS

OMS_DIR = Path(__file__).resolve().parents[1] / "oms"
MUTATING_CONTAINER_METHODS = frozenset(
    {
        "add",
        "append",
        "clear",
        "discard",
        "extend",
        "pop",
        "popitem",
        "remove",
        "setdefault",
        "update",
    }
)
EXPOSURE_LEDGER_FIELDS = frozenset(
    {
        "net_positions",
        "avg_prices",
        "open_buy_qty",
        "open_sell_qty",
        "reduce_only_buy_qty",
        "reduce_only_sell_qty",
        "strategy_net_positions",
        "strategy_avg_prices",
        "strategy_open_buy_qty",
        "strategy_open_sell_qty",
    }
)


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


def _self_aliases(class_node: ast.ClassDef):
    self_aliases = {"self"}
    for node in ast.walk(class_node):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not isinstance(value, ast.Name) or value.id != "self":
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        self_aliases.update(
            target.id for target in targets if isinstance(target, ast.Name)
        )
    return self_aliases


def _self_attribute_accesses(class_node: ast.ClassDef):
    reads = set()
    writes = set()
    self_aliases = _self_aliases(class_node)
    for node in ast.walk(class_node):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"getattr", "hasattr", "setattr", "delattr"}
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in self_aliases
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
            and node.value.id in self_aliases
        ):
            continue
        target = writes if isinstance(node.ctx, (ast.Store, ast.Del)) else reads
        target.add(node.attr)
    return reads, writes


def _self_container_mutations(class_node: ast.ClassDef):
    mutations = set()
    self_aliases = _self_aliases(class_node)
    for node in ast.walk(class_node):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, (ast.Store, ast.Del))
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id in self_aliases
        ):
            mutations.add(node.value.attr)
            continue
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in MUTATING_CONTAINER_METHODS
            and isinstance(node.func.value, ast.Attribute)
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id in self_aliases
        ):
            continue
        mutations.add(node.func.value.attr)
    return mutations


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


def test_extracted_strategy_guard_fields_cannot_bypass_guard_store():
    extracted_fields = {"strategy_guards", "strategy_symbol_guards"}

    for path, class_node, component_type in _component_classes():
        declared = set(component_type.OWNER_READS) | set(
            component_type.OWNER_WRITES
        )
        mutations = _self_container_mutations(class_node)
        assert extracted_fields.isdisjoint(declared), (
            path.name,
            component_type.__name__,
            sorted(extracted_fields & declared),
        )
        assert extracted_fields.isdisjoint(mutations), (
            path.name,
            component_type.__name__,
            sorted(extracted_fields & mutations),
        )

    assert extracted_fields.isdisjoint(OMS._component_state_field_owners)
    assert extracted_fields.isdisjoint(OMS.__dict__)


def test_exposure_ledgers_are_mutated_only_by_exposure_store():
    violations = []
    for path in sorted(OMS_DIR.glob("*.py")):
        if path.name == "exposure.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            target = None
            if isinstance(node, ast.Subscript) and isinstance(
                node.ctx,
                (ast.Store, ast.Del),
            ):
                target = node.value
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in MUTATING_CONTAINER_METHODS
            ):
                target = node.func.value
            elif isinstance(node, ast.Attribute) and isinstance(
                node.ctx,
                (ast.Store, ast.Del),
            ):
                target = node
            if not (
                isinstance(target, ast.Attribute)
                and target.attr in EXPOSURE_LEDGER_FIELDS
                and isinstance(target.value, ast.Attribute)
                and target.value.attr == "exposure"
            ):
                continue
            violations.append((path.name, node.lineno, target.attr))

    assert violations == []


def test_order_collection_is_mutated_only_by_order_store():
    violations = []
    for path in sorted(OMS_DIR.glob("*.py")):
        if path.name == "order_store.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and isinstance(
                node.ctx,
                (ast.Store, ast.Del),
            ):
                target = node.value
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in MUTATING_CONTAINER_METHODS
            ):
                target = node.func.value
            elif isinstance(node, ast.Attribute) and isinstance(
                node.ctx,
                (ast.Store, ast.Del),
            ):
                target = node
            else:
                continue
            if isinstance(target, ast.Attribute) and target.attr == "orders":
                violations.append((path.name, node.lineno))

    assert violations == []
    assert "orders" not in OMS._component_state_field_owners
    assert "order_store" in OMS._component_state_field_owners
