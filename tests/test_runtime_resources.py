from infrastructure.runtime_resources import RuntimeResources


def test_runtime_resources_expose_typed_and_mapping_access():
    runtime = RuntimeResources()
    runtime.config = {"mode": "paper"}

    runtime.engine = "engine"
    runtime.risk_supervisor_started = True
    runtime["gateway"] = "gateway"

    assert runtime["engine"] == "engine"
    assert runtime["_risk_supervisor_started"] is True
    assert runtime.gateway == "gateway"
    assert runtime.config == {"mode": "paper"}


def test_empty_runtime_preserves_mapping_truthiness_and_partial_registration():
    runtime = RuntimeResources()

    assert not runtime
    runtime.config = {"execution": {"mode": "paper"}}

    assert runtime
    assert dict(runtime) == {"config": {"execution": {"mode": "paper"}}}
