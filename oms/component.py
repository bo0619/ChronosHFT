"""Explicit ownership boundaries for extracted OMS components."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from .component_state import OMSAttributeBinding


_ComponentT = TypeVar("_ComponentT", bound="OMSComponent")


@dataclass(frozen=True, slots=True)
class OMSComponentContext:
    """Grant one component a declared view of shared facade state.

    Components never receive transparent access to the facade ``__dict__``.
    Every shared read and write is checked against the component's class-level
    manifest, which makes the remaining coupling searchable and reviewable.
    """

    component_name: str
    readable: frozenset[str]
    writable: frozenset[str]
    _bindings: Mapping[str, OMSAttributeBinding]
    _spawn: Callable[[type[_ComponentT]], _ComponentT]

    @classmethod
    def for_facade(
        cls,
        facade: object,
        component_type: type[_ComponentT],
    ) -> "OMSComponentContext":
        """Build narrow bindings for non-OMS facades and isolated tests."""

        readable = frozenset(component_type.OWNER_READS)
        writable = frozenset(component_type.OWNER_WRITES)
        bindings = {
            name: OMSAttributeBinding(
                read=lambda name=name: getattr(facade, name),
                write=(
                    (lambda value, name=name: setattr(facade, name, value))
                    if name in writable
                    else None
                ),
            )
            for name in readable | writable
        }
        return cls(
            component_name=component_type.__name__,
            readable=readable,
            writable=writable,
            _bindings=bindings,
            _spawn=lambda child_type: child_type(facade),
        )

    def read(self, name: str) -> Any:
        if name not in self.readable and name not in self.writable:
            raise AttributeError(
                f"{self.component_name} has no declared OMS dependency {name!r}"
            )
        return self._bindings[name].read()

    def write(self, name: str, value: Any) -> None:
        if name not in self.writable:
            raise AttributeError(
                f"{self.component_name} cannot write OMS field {name!r}"
            )
        writer = self._bindings[name].write
        if writer is None:
            raise AttributeError(
                f"{self.component_name} cannot write OMS field {name!r}"
            )
        writer(value)

    def spawn(self, component_type: type[_ComponentT]) -> _ComponentT:
        """Construct a sibling component through the injected factory."""
        return self._spawn(component_type)


class _ComponentMethod:
    """Bind a facade method to a named owner component."""

    def __init__(self, component_name: str, method_name: str | None = None):
        self.component_name = component_name
        self.method_name = method_name

    def __set_name__(self, _owner, name: str) -> None:
        if self.method_name is None:
            self.method_name = name

    def _component(self, instance, owner):
        try:
            return getattr(instance, self.component_name)
        except AttributeError:
            factories = getattr(owner, "_component_factories", {})
            factory = factories.get(self.component_name)
            if factory is None:
                raise
            component = factory(instance)
            setattr(instance, self.component_name, component)
            return getattr(instance, self.component_name)

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        component = self._component(instance, owner)
        return getattr(component, self.method_name)

    def __set__(self, instance, value) -> None:
        component = self._component(instance, type(instance))
        component._install_method_override(self.method_name, value)

    def __delete__(self, instance) -> None:
        component = self._component(instance, type(instance))
        component._remove_method_override(self.method_name)


def component_method(
    component_name: str,
    method_name: str | None = None,
) -> _ComponentMethod:
    return _ComponentMethod(component_name, method_name)


class OMSComponent:
    """Base class for components with explicit shared-state capabilities.

    ``OWNER_READS`` and ``OWNER_WRITES`` are intentionally separate. An
    assignment that is neither a declared shared write nor ``LOCAL_STATE`` is
    rejected instead of being silently stored on the component or facade.
    Facade method overrides installed through :func:`component_method` also
    live on the target component.
    """

    OWNER_READS: frozenset[str] = frozenset()
    OWNER_WRITES: frozenset[str] = frozenset()
    LOCAL_STATE: frozenset[str] = frozenset()

    __slots__ = ("_context", "__dict__")

    def __init__(self, owner):
        if isinstance(owner, OMSComponentContext):
            context = owner
        else:
            context_factory = getattr(
                owner,
                "_component_context_for",
                None,
            )
            context = (
                context_factory(type(self))
                if callable(context_factory)
                else OMSComponentContext.for_facade(owner, type(self))
            )
        object.__setattr__(self, "_context", context)

    def __getattr__(self, name: str):
        context = object.__getattribute__(self, "_context")
        return context.read(name)

    def __setattr__(self, name: str, value) -> None:
        if name == "_context":
            raise AttributeError("OMS component context is immutable")
        if name in type(self).LOCAL_STATE:
            object.__setattr__(self, name, value)
            return
        context = object.__getattribute__(self, "_context")
        context.write(name, value)

    def _spawn_component(self, component_type: type[_ComponentT]) -> _ComponentT:
        context = object.__getattribute__(self, "_context")
        return context.spawn(component_type)

    def _install_method_override(self, name: str, value) -> None:
        component_method_value = getattr(type(self), name, None)
        if not callable(component_method_value):
            raise AttributeError(
                f"{type(self).__name__} has no component method {name!r}"
            )
        object.__setattr__(self, name, value)

    def _remove_method_override(self, name: str) -> None:
        vars(self).pop(name, None)
