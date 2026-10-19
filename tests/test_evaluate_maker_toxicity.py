"""Factor orientation in the maker toxicity study: larger means worse for the fill."""

import math

import numpy as np
import pandas as pd

from scripts import evaluate_maker_toxicity as tox


def test_move_against_is_positive_when_binance_moves_against_the_maker():
    times = np.array([0.0, 1.0, 2.0])
    log_mid = np.log([100.0, 100.0, 99.0])  # Binance falls 1%
    moves = tox.move_against(log_mid, times, np.array([2.5, 2.5]), 1.0, np.array([True, False]))
    assert moves[0] > 99 and moves[1] < -99  # bad for a resting bid, good for a resting ask


def test_fill_factors_orientation_on_a_tiny_market():
    depth = pd.DataFrame({"received_ts": [0.0, 1.0, 2.0], "bid1_p": [99.9, 99.9, 99.9], "ask1_p": [100.1, 100.1, 100.1],
                          "bid1_v": [1.0, 1.0, 1.0], "ask1_v": [9.0, 9.0, 9.0]})
    trades = pd.DataFrame({"received_ts": [1.5, 1.6], "price": [99.9, 99.9], "qty": [1.0, 1.0],
                           "maker_is_buyer": [True, True]})
    b_times = np.array([0.0, 1.0, 2.9])
    b_logmid = np.log([100.0, 100.0, 99.5])  # Binance drops just before the fill
    fills = pd.DataFrame({"t": [3.0], "side": ["BUY"], "price": [99.9], "qty": [1.0]})
    out = tox.fill_factors(fills, depth, trades, b_times, b_logmid, [], react_sec=0.01, basis_half_life=60.0, tick=0.1)
    row = out.iloc[0]
    assert row.quote_vs_fair > 0  # we bid 99.9 but fair fell to about 99.5
    assert row.grvt_vs_fair > 0  # GRVT mid 100 sits above the new fair: GRVT should fall
    assert row.binance_ret_200ms > 0
    assert row.grvt_imbalance > 0  # thin bid queue (1) against a deep ask (9)
    assert math.isclose(row.grvt_flow_5s, 1.0)  # every taker hit the bids
    assert row.grvt_spread_ticks == 2
