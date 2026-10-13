import pytest

from strategy.touch_snap import (
    snap_to_touch,
    touch_snap_ticks,
    whole_tick_min_spread_bps,
)

# KORUUSDT-like one-tick market: 0.01 at ~14.375 is ~6.96 bps.
MID = 14.375
BEST_BID = 14.37
BEST_ASK = 14.38
TICK = 0.01


def snap(**overrides):
    arguments = {
        "target_bid": 14.36,
        "target_ask": 14.39,
        "reference_price": MID,
        "bid_depth_bps": 3.6,
        "ask_depth_bps": 4.7,
        "best_bid": BEST_BID,
        "best_ask": BEST_ASK,
        "tick_size": TICK,
        "snap_ticks": 1.0,
        "min_spread_bps": 8.0,
        "passive_fee_bps": 0.0,
    }
    arguments.update(overrides)
    return snap_to_touch(**arguments)


def test_both_sides_one_tick_behind_join_the_touch():
    # Model prices 14.3698 / 14.3818 floor/ceil to 14.36 / 14.39. The 8 bps
    # floor rounds down to one 6.96 bps tick on this symbol, so both join.
    assert snap() == (BEST_BID, BEST_ASK)


def test_floor_of_two_ticks_snaps_only_the_closer_side():
    # 14 bps is two ticks here: a one-tick quote is too tight, and the bid
    # model price (0.02 ticks behind) is closer than the ask (0.18 ticks).
    assert snap(min_spread_bps=14.0) == (BEST_BID, 14.39)


@pytest.mark.parametrize(
    ("tick", "price", "floor_bps", "expected_ticks"),
    [
        (0.01, 14.375, 8.0, 1),   # KORUUSDT: ~6.96 bps tick
        (0.01, 120.0, 8.0, 9),    # SOXLUSDT on GRVT: ~0.83 bps tick
        (0.01, 5.0, 8.0, 1),      # tick (20 bps) wider than the floor
        (0.01, 14.375, 14.0, 2),
    ],
)
def test_whole_tick_floor_is_per_symbol(tick, price, floor_bps, expected_ticks):
    tick_bps = tick / price * 10_000.0
    assert whole_tick_min_spread_bps(floor_bps, tick, price) == pytest.approx(
        expected_ticks * tick_bps
    )


def test_inventory_skew_beyond_the_snap_distance_is_kept():
    # A 12 bps bid depth puts the model bid 1.2 ticks behind the touch.
    assert snap(target_bid=14.35, bid_depth_bps=12.0) == (14.35, BEST_ASK)


def test_side_without_edge_against_fair_value_does_not_snap():
    # Fair value sits on the best bid: joining it earns nothing.
    assert snap(reference_price=BEST_BID, bid_depth_bps=0.5, target_bid=14.36) == (
        14.36,
        BEST_ASK,
    )


def test_passive_fee_must_be_cleared_per_side():
    # 10 bps round trip is 5 bps per side, more than the ~3.5 bps half tick.
    assert snap(passive_fee_bps=10.0) == (14.36, 14.39)


def test_quotes_already_at_the_touch_are_unchanged():
    assert snap(target_bid=BEST_BID, target_ask=BEST_ASK) == (BEST_BID, BEST_ASK)


def test_disabled_snap_returns_the_decision_prices():
    assert snap(snap_ticks=0.0) == (14.36, 14.39)


@pytest.mark.parametrize(
    ("config", "live", "expected"),
    [
        ({"paper_touch_snap_ticks": 1.0}, False, 1.0),
        ({"paper_touch_snap_ticks": 1.0}, True, 0.0),
        ({}, False, 0.0),
        ({"paper_touch_snap_ticks": "bad"}, False, 0.0),
        (None, False, 0.0),
    ],
)
def test_snap_distance_is_paper_only(config, live, expected):
    assert touch_snap_ticks(config, live=live) == expected
