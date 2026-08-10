from typing import ClassVar

import pytest

from event.type import LifecycleState
from oms.component import OMSComponent, component_method
from oms.engine import OMS
from oms.lifecycle_controller import OMSLifecycleController
from oms.lifecycle_store import LifecycleStore


class _ExampleComponent(OMSComponent):
    OWNER_READS = frozenset({"value"})
    OWNER_WRITES = frozenset({"value"})

    def read_value(self, suffix: str = "") -> str:
        return f"{self.value}{suffix}"

    def replace_value(self, value: str) -> None:
        self.value = value

    def outer(self) -> str:
        return self.inner()

    def inner(self) -> str:
        return "component"


class _ExampleFacade:
    read_value = component_method("component")
    replace_value = component_method("component")
    outer = component_method("component")
    inner = component_method("component")

    def __init__(self):
        self.value = "owner"
        self.component = _ExampleComponent(self)


class _LazyFacade:
    _component_factories: ClassVar[dict[str, object]] = {
        "component": _ExampleComponent
    }
    read_value = component_method("component")

    def __init__(self):
        self.value = "lazy-owner"


class _StatefulComponent(OMSComponent):
    LOCAL_STATE = frozenset({"counter"})

    def __init__(self, owner):
        super().__init__(owner)
        self.counter = 0

    def increment(self):
        self.counter += 1
        return self.counter


def test_component_methods_bind_to_the_facade_instance():
    facade = _ExampleFacade()

    assert facade.read_value("-state") == "owner-state"
    facade.replace_value("updated")
    assert facade.value == "updated"


def test_facade_instance_override_wins_inside_component_calls():
    facade = _ExampleFacade()
    facade.inner = lambda: "override"

    assert facade.outer() == "override"
    assert "inner" not in vars(facade)
    assert "inner" in vars(facade.component)


def test_component_method_lazily_constructs_registered_component():
    facade = _LazyFacade()

    assert "component" not in vars(facade)
    assert facade.read_value() == "lazy-owner"
    assert isinstance(facade.component, _ExampleComponent)


def test_undeclared_reads_and_writes_do_not_escape_to_facade():
    facade = _ExampleFacade()

    with pytest.raises(AttributeError, match="no declared OMS dependency"):
        _ = facade.component.undeclared
    with pytest.raises(AttributeError, match="cannot write OMS field"):
        facade.component.undeclared = "leak"

    assert "undeclared" not in vars(facade)
    assert "undeclared" not in vars(facade.component)


def test_declared_component_state_stays_off_the_facade():
    facade = _ExampleFacade()
    component = _StatefulComponent(facade)

    assert component.increment() == 1
    assert component.counter == 1
    assert "counter" not in vars(facade)


def test_extracted_lifecycle_state_bypasses_shared_state_registry():
    oms = object.__new__(OMS)
    oms.lifecycle_store = LifecycleStore()
    oms.state = LifecycleState.RECONCILING

    registry = vars(oms)["_component_state"]
    assert "state" not in vars(oms)
    assert "state" not in registry.field_owners
    assert registry.owner_of("lifecycle_store") == "OMSInitializer"

    context = oms._component_context_for(OMSLifecycleController)
    assert context.read("state") == LifecycleState.RECONCILING
    with pytest.raises(AttributeError, match="cannot write"):
        context.write("state", LifecycleState.LIVE)
    oms.lifecycle_store.transition(LifecycleState.LIVE)

    assert oms.state == LifecycleState.LIVE
