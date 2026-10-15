import argparse

import pandas as pd

from scripts import replay_cross_venue as cv


def grvt_depth_row(exchange, received, bid, ask, size=10.0):
    row = {"received_ts": received, "exchange_ts": exchange}
    for level in range(1, 6):
        row.update({f"bid{level}_p": bid - (level - 1) * 0.01, f"bid{level}_v": size,
                    f"ask{level}_p": ask + (level - 1) * 0.01, f"ask{level}_v": size})
    return row


def args(**overrides):
    values = dict(notional=100.0, anchor="binance", half_spread_bps=2.0, improve=False,
                  basis_half_life=60.0, keep_ticks=1.0, min_rest_sec=1.0, latency_ms=6.0,
                  one_way_samples=None, seed=1, tick=0.01, cross="fill")
    values.update(overrides)
    return argparse.Namespace(**values)


def market(binance_drop_at=1.000):
    """GRVT 100.00/100.02 with 10 queued at each touch; Binance at 100.01.

    Binance drops to 99.80 at ``binance_drop_at`` (seen 2.5 ms later). A
    20-lot GRVT sell takes the whole bid at 1.030, and GRVT reprices at
    1.040.
    """
    depth = pd.DataFrame([
        grvt_depth_row(0.0, 0.010, 100.00, 100.02),
        grvt_depth_row(1.040, 1.050, 99.79, 99.81),
        grvt_depth_row(60.0, 60.010, 99.79, 99.81),
    ])
    trades = pd.DataFrame([dict(received_ts=1.040, exchange_ts=1.030, trade_id=1, price=99.99,
                                qty=20.0, maker_is_buyer=True)])
    binance = pd.DataFrame([
        dict(received_ts=0.005, bid_p=100.00, ask_p=100.02),
        dict(received_ts=binance_drop_at + 0.0025, bid_p=99.79, ask_p=99.81),
        dict(received_ts=60.005, bid_p=99.79, ask_p=99.81),
    ])
    return depth, trades, binance


def triggers(result):
    return [(fill["side"], fill["trigger"]) for fill in result["fills"]]


def test_grvt_only_quote_is_run_over_after_binance_moves():
    depth, trades, binance = market()
    result = cv.replay("X", depth, trades, binance, args(anchor="grvt"))
    assert ("BUY", "through") in triggers(result)


def test_binance_fair_value_pulls_the_bid_before_the_grvt_sell_lands():
    # +/-0.5 bps around fair 100.01 puts the bid on the 100.00 touch, so only
    # pulling it in time keeps it out of the sell.
    depth, trades, binance = market()
    result = cv.replay("X", depth, trades, binance, args(anchor="binance", half_spread_bps=0.5))
    assert not [fill for fill in result["fills"] if fill["side"] == "BUY"]


def test_too_slow_to_pull_when_binance_moves_just_before_the_sell():
    # Seen at 1.0275, the cancel lands at 1.0335, after the 1.030 sell.
    depth, trades, binance = market(binance_drop_at=1.025)
    result = cv.replay("X", depth, trades, binance, args(anchor="binance", half_spread_bps=0.5))
    assert ("BUY", "through") in triggers(result)


def test_cross_skip_pulls_instead_of_filling():
    # Fair 100.02 puts an improving bid at 100.01; the GRVT ask then drops onto it.
    depth = pd.DataFrame([
        grvt_depth_row(0.0, 0.010, 100.00, 100.04),
        grvt_depth_row(5.0, 5.010, 99.99, 100.01),
        grvt_depth_row(60.0, 60.010, 99.99, 100.01),
    ])
    trades = pd.DataFrame(columns=["received_ts", "exchange_ts", "trade_id", "price", "qty", "maker_is_buyer"])
    binance = pd.DataFrame([dict(received_ts=0.005, bid_p=100.02, ask_p=100.04),
                            dict(received_ts=60.005, bid_p=100.02, ask_p=100.04)])
    pessimistic = cv.replay("X", depth, trades, binance, args(improve=True, half_spread_bps=0.5))
    optimistic = cv.replay("X", depth, trades, binance, args(improve=True, half_spread_bps=0.5, cross="skip"))
    assert ("BUY", "cross") in triggers(pessimistic)
    assert triggers(optimistic) == []


def test_rounding_keeps_quotes_on_the_tick_grid_away_from_fair():
    assert cv.round_down(100.019, 0.01) == 100.01
    assert cv.round_up(100.011, 0.01) == 100.02
    assert cv.round_down(100.02, 0.01) == 100.02
