import json
from dataclasses import FrozenInstanceError

import pytest

from oms.guard_store import GuardStore


def test_guard_store_owns_global_and_symbol_scoped_strategy_guards():
    store = GuardStore()

    assert store.freeze("alpha", "global-risk") == ""
    assert (
        store.freeze(
            "alpha",
            "symbol-risk",
            symbol="BTCUSDT",
        )
        == ""
    )
    assert store.has_active()
    assert store.reason("alpha") == "global-risk"
    assert store.reason("alpha", symbol="BTCUSDT") == "symbol-risk"
    assert store.reason("alpha", symbol="ETHUSDT") == "global-risk"

    assert (
        store.clear(
            "alpha",
            symbol="BTCUSDT",
            expected_reason="stale-reason",
        )
        == ""
    )
    assert store.reason("alpha", symbol="BTCUSDT") == "symbol-risk"
    assert (
        store.clear(
            "alpha",
            symbol="BTCUSDT",
            expected_reason="symbol-risk",
        )
        == "symbol-risk"
    )
    assert store.clear("alpha") == "global-risk"
    assert not store.has_active()


def test_snapshot_is_immutable_and_does_not_expose_store_mappings():
    store = GuardStore()
    store.freeze("alpha", "global-risk")
    store.freeze("beta", "symbol-risk", symbol="BTCUSDT")

    snapshot = store.snapshot()

    assert snapshot.strategy_guards == (("alpha", "global-risk"),)
    assert snapshot.strategy_symbol_guards == (
        ("beta", "BTCUSDT", "symbol-risk"),
    )
    with pytest.raises(FrozenInstanceError):
        snapshot.strategy_guards = ()
    assert not hasattr(store, "strategy_guards")
    assert not hasattr(store, "strategy_symbol_guards")


def test_checkpoint_payload_is_canonical_json_and_round_trips():
    store = GuardStore()
    store.freeze("beta", "global-beta")
    store.freeze("alpha", "global-alpha")
    store.freeze("alpha", "symbol-alpha", symbol="BTCUSDT")

    payload = store.checkpoint_payload()
    encoded = json.dumps(
        payload,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    assert payload == {
        "strategy_guards": {
            "alpha": "global-alpha",
            "beta": "global-beta",
        },
        "strategy_symbol_guards": {
            "alpha|BTCUSDT": "symbol-alpha",
        },
    }
    assert '"alpha|BTCUSDT":"symbol-alpha"' in encoded

    restored = GuardStore()
    decoded = json.loads(encoded)
    restored.restore(
        decoded["strategy_guards"],
        decoded["strategy_symbol_guards"],
    )
    assert restored.snapshot() == store.snapshot()


@pytest.mark.parametrize(
    ("strategy_guards", "strategy_symbol_guards"),
    [
        (None, {}),
        ([], {}),
        ({" alpha": "risk"}, {}),
        ({"alpha|beta": "risk"}, {}),
        ({"alpha": ""}, {}),
        ({}, None),
        ({}, []),
        ({}, {("alpha", "BTCUSDT"): "risk"}),
        ({}, {"alpha|btcusdt": "risk"}),
        ({}, {"alpha|": "risk"}),
        ({}, {"alpha|BTCUSDT|extra": "risk"}),
        ({}, {"alpha|BTCUSDT": 1}),
    ],
)
def test_restore_rejects_noncanonical_or_ambiguous_state_atomically(
    strategy_guards,
    strategy_symbol_guards,
):
    store = GuardStore()
    store.freeze("existing", "keep-me")
    before = store.snapshot()

    with pytest.raises(ValueError):
        store.restore(strategy_guards, strategy_symbol_guards)

    assert store.snapshot() == before


def test_checkpoint_payload_is_a_detached_copy():
    store = GuardStore()
    store.freeze("alpha", "risk")

    payload = store.checkpoint_payload()
    payload["strategy_guards"].clear()

    assert store.reason("alpha") == "risk"
