import pytest

from strategy.adaptive_pipeline import (
    AdaptivePipelineInput,
    AdaptiveQuotePipeline,
)


def _inputs(**overrides):
    values = {
        "base_A_per_s": 10.0,
        "base_k_per_bps": 0.5,
        "bid_A_multiplier": 1.2,
        "ask_A_multiplier": 0.8,
        "bid_k_multiplier": 1.1,
        "ask_k_multiplier": 0.9,
        "bid_hawkes_multiplier": 1.5,
        "ask_hawkes_multiplier": 2.0,
        "bid_markout_cost_bps": 0.2,
        "ask_markout_cost_bps": 0.3,
        "bid_queue_cost_bps": 0.4,
        "ask_queue_cost_bps": 0.5,
        "bid_flow_cost_bps": 0.6,
        "ask_flow_cost_bps": 0.7,
    }
    values.update(overrides)
    return AdaptivePipelineInput(**values)


def test_adaptive_pipeline_composes_side_specific_signals():
    context = AdaptiveQuotePipeline().build(_inputs())

    assert context.bid_A_per_s == pytest.approx(18.0)
    assert context.ask_A_per_s == pytest.approx(16.0)
    assert context.bid_k_per_bps == pytest.approx(0.55)
    assert context.ask_k_per_bps == pytest.approx(0.45)
    assert context.bid_adverse_cost_bps == pytest.approx(1.2)
    assert context.ask_adverse_cost_bps == pytest.approx(1.5)
    assert context.as_formula_context() == {
        "bid_A_per_s": pytest.approx(18.0),
        "ask_A_per_s": pytest.approx(16.0),
        "bid_k_per_bps": pytest.approx(0.55),
        "ask_k_per_bps": pytest.approx(0.45),
        "bid_adverse_cost_bps": pytest.approx(1.2),
        "ask_adverse_cost_bps": pytest.approx(1.5),
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("base_A_per_s", 0.0, "base_A_per_s must be positive"),
        ("bid_hawkes_multiplier", float("nan"), "bid_hawkes_multiplier"),
        ("ask_queue_cost_bps", -0.1, "ask_queue_cost_bps must be nonnegative"),
    ),
)
def test_adaptive_pipeline_rejects_invalid_estimator_outputs(
    field,
    value,
    message,
):
    with pytest.raises(ValueError, match=message):
        AdaptiveQuotePipeline().build(_inputs(**{field: value}))


def test_adaptive_pipeline_has_no_mutable_runtime_state():
    pipeline = AdaptiveQuotePipeline()
    first = pipeline.build(_inputs(base_A_per_s=5.0))
    second = pipeline.build(_inputs(base_A_per_s=8.0))

    assert first.bid_A_per_s == pytest.approx(9.0)
    assert second.bid_A_per_s == pytest.approx(14.4)
    assert not hasattr(pipeline, "__dict__")
