import pytest

from event.type import LifecycleState
from oms.component_state import (
    EXTRACTED_STATE_STORE_OWNERS,
    OMSStateRegistry,
)
from oms.engine import OMS, _loaded_oms_component_types
from oms.lifecycle_controller import OMSLifecycleController
from oms.lifecycle_store import LifecycleStore


LIFECYCLE_FIELDS = frozenset(
    {
        "last_freeze_reason",
        "last_halt_reason",
        "manual_rearm_required",
        "state",
    }
)


def test_lifecycle_store_applies_one_atomic_transition():
    store = LifecycleStore()

    previous = store.transition(
        LifecycleState.HALTED,
        increment_generation=True,
        manual_rearm_required=True,
        last_freeze_reason="",
        last_halt_reason="operator halt",
    )

    assert previous.state == LifecycleState.BOOTSTRAP
    assert previous.generation == 0
    assert store.snapshot().state == LifecycleState.HALTED
    assert store.snapshot().generation == 1
    assert store.snapshot().manual_rearm_required is True
    assert store.snapshot().last_halt_reason == "operator halt"


def test_lifecycle_store_rejects_invalid_transition_without_partial_write():
    store = LifecycleStore()
    before = store.snapshot()

    with pytest.raises(TypeError, match="last_halt_reason"):
        store.transition(
            LifecycleState.HALTED,
            increment_generation=True,
            manual_rearm_required=True,
            last_halt_reason=None,
        )

    assert store.snapshot() == before


def test_oms_compatibility_fields_delegate_to_lifecycle_store():
    oms = OMS.__new__(OMS)
    oms.__dict__["_component_state"] = OMSStateRegistry(
        OMS._component_state_field_owners
    )
    oms.lifecycle_store = LifecycleStore()

    oms.state = LifecycleState.FROZEN
    oms.manual_rearm_required = True
    oms.last_freeze_reason = "truth unavailable"

    snapshot = oms.lifecycle_store.snapshot()
    assert snapshot.state == LifecycleState.FROZEN
    assert snapshot.generation == 0
    assert snapshot.manual_rearm_required is True
    assert snapshot.last_freeze_reason == "truth unavailable"
    assert LIFECYCLE_FIELDS.isdisjoint(OMS._component_state_field_owners)
    assert set(EXTRACTED_STATE_STORE_OWNERS) == LIFECYCLE_FIELDS


def test_lifecycle_controller_mutates_core_state_only_through_store():
    assert "lifecycle_store" in OMSLifecycleController.OWNER_READS
    assert LIFECYCLE_FIELDS.isdisjoint(OMSLifecycleController.OWNER_WRITES)


def test_no_oms_component_can_write_lifecycle_compatibility_fields():
    offenders = {
        component_type.__name__: sorted(
            LIFECYCLE_FIELDS.intersection(component_type.OWNER_WRITES)
        )
        for component_type in _loaded_oms_component_types()
        if not LIFECYCLE_FIELDS.isdisjoint(component_type.OWNER_WRITES)
    }

    assert offenders == {}
