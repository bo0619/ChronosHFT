from types import SimpleNamespace

import pytest

from strategy.inventory_step_back import (
    InventoryStepBackConfig,
    step_back_against_inventory,
)
from strategy.touch_snap import snap_decision_to_touch

TICK = 0.01
ON = InventoryStepBackConfig(enabled=True, lots_per_tick=1.0, max_ticks=3)


def step(lots, config=ON, bid=14.37, ask=14.38):
    return step_back_against_inventory(
        bid, ask, config=config, inventory_lots=lots,
        best_bid=14.37, best_ask=14.38, tick_size=TICK,
    )


@pytest.mark.parametrize(
    ("lots", "expected"),
    [
        (0.0, (14.37, 14.38)),
        (0.99, (14.37, 14.38)),
        (1.0, (14.36, 14.38)),    # long one lot: bid one tick back
        (2.5, (14.35, 14.38)),
        (9.0, (14.34, 14.38)),    # capped at max_ticks
        (-1.0, (14.37, 14.39)),   # short: ask steps back
    ],
)
def test_adding_side_steps_back_one_tick_per_lot(lots, expected):
    assert step(lots) == pytest.approx(expected)


def test_a_target_already_further_back_is_kept_and_disabled_is_a_no_op():
    assert step(2.0, bid=14.30) == (14.30, 14.38)
    assert step(3.0, config=InventoryStepBackConfig()) == (14.37, 14.38)


def test_symbol_overrides_and_validation():
    section = {"enabled": True, "symbols": {"KORUUSDT": {"lots_per_tick": 0.5}}}
    assert InventoryStepBackConfig.for_symbol(section, "KORUUSDT").ticks_back(1.0) == 2
    assert InventoryStepBackConfig.for_symbol(section, "SOXLUSDT").ticks_back(1.0) == 1
    with pytest.raises(ValueError):
        InventoryStepBackConfig.for_symbol({"lots_per_tick": 0}, "KORUUSDT")


def test_snap_cannot_pull_the_inventory_side_back_to_the_touch():
    # GLFT's bps skew puts the model bid 0.9 tick behind, which snapping
    # alone would pull back onto the touch.
    decision = SimpleNamespace(
        target_bid=14.36,
        target_ask=14.38,
        effective_bid_depth_bps=10.0,
        effective_ask_depth_bps=3.0,
        effective_min_spread_bps=8.0,
    )
    strategy = SimpleNamespace(
        live_mode=False,
        strat_conf={},
        glft_conf={
            "execution": {
                "paper_touch_snap_ticks": 1.0,
                "paper_inventory_step_back": {"enabled": True},
            }
        },
    )
    calibrator = SimpleNamespace(queue_imbalance=False)

    def quotes(lots):
        return snap_decision_to_touch(
            decision, strategy, "KORUUSDT", calibrator,
            reference_price=14.375, best_bid=14.37, best_ask=14.38,
            tick_size=TICK, passive_fee_bps=0.0, inventory_lots=lots,
        )

    assert quotes(0.0) == (14.37, 14.38)
    assert quotes(1.2) == pytest.approx((14.36, 14.38))
    assert quotes(3.0) == pytest.approx((14.34, 14.38))
    # Live neither snaps nor applies the Paper step-back.
    strategy.live_mode = True
    assert quotes(3.0) == (14.36, 14.38)
