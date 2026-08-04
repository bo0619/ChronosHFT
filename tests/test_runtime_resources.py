import pytest

from infrastructure.runtime_resources import RuntimeResources


def test_runtime_resources_share_mutations_with_legacy_mapping():
    legacy = {"config": {"mode": "paper"}}
    runtime = RuntimeResources.coerce(legacy)

    runtime.engine = "engine"
    runtime.risk_supervisor_started = True
    runtime["gateway"] = "gateway"

    assert legacy["engine"] == "engine"
    assert legacy["_risk_supervisor_started"] is True
    assert runtime.gateway == "gateway"
    assert runtime.config == {"mode": "paper"}


def test_empty_runtime_preserves_mapping_truthiness_and_partial_registration():
    runtime = RuntimeResources()

    assert not runtime
    runtime.config = {"execution": {"mode": "paper"}}

    assert runtime
    assert dict(runtime) == {"config": {"execution": {"mode": "paper"}}}


def test_runtime_coercion_is_idempotent_and_rejects_non_mapping():
    runtime = RuntimeResources()

    assert RuntimeResources.coerce(runtime) is runtime
    with pytest.raises(TypeError, match="mutable mapping"):
        RuntimeResources.coerce(object())
