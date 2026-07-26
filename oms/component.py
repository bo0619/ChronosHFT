"""Shared ownership boundary for incrementally extracted OMS components."""

from __future__ import annotations


class _ComponentMethod:
    """Bind a facade method to a named owner component."""

    def __init__(self, component_name: str, method_name: str | None = None):
        self.component_name = component_name
        self.method_name = method_name

    def __set_name__(self, _owner, name: str) -> None:
        if self.method_name is None:
            self.method_name = name

    def __get__(self, instance, _owner=None):
        if instance is None:
            return self
        component = getattr(instance, self.component_name)
        return getattr(component, self.method_name)


def component_method(
    component_name: str,
    method_name: str | None = None,
) -> _ComponentMethod:
    return _ComponentMethod(component_name, method_name)


class OMSComponent:
    """Delegate shared state to the OMS facade during component extraction.

    Instance-level overrides on the facade take precedence. This preserves
    the existing test and operator instrumentation hooks while responsibilities
    move out of the monolithic class.
    """

    __slots__ = ("_owner",)

    def __init__(self, owner):
        object.__setattr__(self, "_owner", owner)

    def __getattribute__(self, name):
        if name not in {"_owner", "__class__", "__slots__"}:
            owner = object.__getattribute__(self, "_owner")
            owner_values = vars(owner)
            if name in owner_values:
                return owner_values[name]
        return object.__getattribute__(self, name)

    def __getattr__(self, name):
        return getattr(self._owner, name)

    def __setattr__(self, name, value):
        setattr(self._owner, name, value)
