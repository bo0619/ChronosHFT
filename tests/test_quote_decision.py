import math

import pytest

from strategy.quote_decision import (
    QuoteDecisionEngine,
    QuoteDecisionInput,
)
from strategy.quote_math import QuoteOffsets


def _formula_quote(*, half_spread_bps=1.0, center_offset_bps=0.0):
    return QuoteOffsets(
        bid_depth_bps=half_spread_bps - center_offset_bps,
        ask_depth_bps=half_spread_bps + center_offset_bps,
        center_offset_bps=center_offset_bps,
        half_spread_bps=half_spread_bps,
        bid_price=99.0,
        ask_price=101.0,
    )


def test_quote_decision_applies_fee_floor_and_directional_tick_rounding():
    decision = QuoteDecisionEngine().decide(
        QuoteDecisionInput(
            reference_price=100.0,
            best_bid=99.9,
            best_ask=100.1,
            tick_size=0.01,
            configured_min_spread_bps=4.0,
            passive_fee_bps=10.0,
            formula_quote=_formula_quote(),
        )
    )

    assert decision is not None
    assert decision.effective_min_spread_bps == 10.0
    assert decision.effective_half_spread_bps == 5.0
    assert decision.target_bid == 99.95
    assert decision.target_ask == 100.06


def test_quote_decision_enforces_passive_top_of_book_after_skew():
    decision = QuoteDecisionEngine().decide(
        QuoteDecisionInput(
            reference_price=100.0,
            best_bid=99.9,
            best_ask=100.1,
            tick_size=0.01,
            configured_min_spread_bps=0.0,
            passive_fee_bps=0.0,
            formula_quote=_formula_quote(
                half_spread_bps=1.0,
                center_offset_bps=100.0,
            ),
        )
    )

    assert decision is not None
    assert decision.target_bid == pytest.approx(100.09)
    assert decision.target_ask > decision.target_bid
    assert decision.post_only_adjusted is True
    assert decision.quote_center_price > 100.0


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("reference_price", 0.0, "reference_price must be positive"),
        ("best_bid", float("nan"), "best_bid must be finite"),
        ("tick_size", -0.01, "tick_size must be positive"),
        (
            "configured_min_spread_bps",
            -1.0,
            "configured_min_spread_bps must be nonnegative",
        ),
    ),
)
def test_quote_decision_rejects_invalid_market_policy_inputs(
    field,
    value,
    message,
):
    values = {
        "reference_price": 100.0,
        "best_bid": 99.9,
        "best_ask": 100.1,
        "tick_size": 0.01,
        "configured_min_spread_bps": 1.0,
        "passive_fee_bps": 0.0,
        "formula_quote": _formula_quote(),
    }
    values[field] = value

    with pytest.raises(ValueError, match=message):
        QuoteDecisionEngine().decide(QuoteDecisionInput(**values))


def test_quote_decision_rejects_crossed_market_and_nonfinite_formula():
    engine = QuoteDecisionEngine()
    with pytest.raises(ValueError, match="best_bid must be below best_ask"):
        engine.decide(
            QuoteDecisionInput(
                reference_price=100.0,
                best_bid=100.1,
                best_ask=100.0,
                tick_size=0.01,
                configured_min_spread_bps=1.0,
                passive_fee_bps=0.0,
                formula_quote=_formula_quote(),
            )
        )

    with pytest.raises(ValueError, match="center_offset_bps must be finite"):
        engine.decide(
            QuoteDecisionInput(
                reference_price=100.0,
                best_bid=99.9,
                best_ask=100.1,
                tick_size=0.01,
                configured_min_spread_bps=1.0,
                passive_fee_bps=0.0,
                formula_quote=_formula_quote(
                    center_offset_bps=math.inf,
                ),
            )
        )
