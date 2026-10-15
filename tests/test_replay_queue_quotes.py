import argparse

import numpy as np
import pandas as pd

from scripts import replay_queue_quotes as rq
from scripts.evaluate_toxicity_factors import auc, fit_logistic


def depth_row(exchange, received, bid, bid_size, ask, ask_size):
    row = {"received_ts": received, "exchange_ts": exchange}
    for level in range(1, 6):
        row.update({
            f"bid{level}_p": bid - (level - 1) * 0.01, f"bid{level}_v": bid_size if level == 1 else 1000.0,
            f"ask{level}_p": ask + (level - 1) * 0.01, f"ask{level}_v": ask_size if level == 1 else 1000.0,
        })
    return row


def ticker_row(exchange, received, bid, bid_size, ask, ask_size, update_id):
    return dict(received_ts=received, exchange_ts=exchange, update_id=update_id,
                bid_p=bid, bid_v=bid_size, ask_p=ask, ask_v=ask_size)


def replay_args(**overrides):
    args = dict(notional=100.0, inside_ticks=0, keep_ticks=2.0, min_rest_sec=5.0, react="depth",
                latency_ms=5.0, thin_notional=0.0, cancel_ahead_model="proportional", tick=0.01,
                event_clock="exchange", seed=1, one_way_samples=None)
    args.update(overrides)
    return argparse.Namespace(**args)


def fills_of(result):
    return [(round(fill["t"], 3), fill["side"], fill["price"], fill["trigger"]) for fill in result["fills"]]


def late_trade_market():
    """A sell trades our bid away at 1.000; the venue publishes it at 1.150.

    The book showing the emptied bid (9.99 / 10.00) is published at once.
    """
    depth = pd.DataFrame([
        depth_row(0.0, 0.002, 10.00, 1.0, 10.01, 1000.0),
        depth_row(1.001, 1.003, 9.99, 1000.0, 10.00, 500.0),
        depth_row(40.0, 40.002, 9.99, 1000.0, 10.00, 500.0),
    ])
    ticker = pd.DataFrame([
        ticker_row(0.0, 0.002, 10.00, 1.0, 10.01, 1000.0, 1),
        ticker_row(1.001, 1.003, 9.99, 1000.0, 10.00, 500.0, 2),
        ticker_row(40.0, 40.002, 9.99, 1000.0, 10.00, 500.0, 3),
    ])
    trades = pd.DataFrame([dict(received_ts=1.150, exchange_ts=1.000, trade_id=1, price=10.00,
                                qty=50.0, maker_is_buyer=True)])
    return depth, trades, ticker


def test_exchange_clock_fills_on_the_trade_not_the_later_book():
    depth, trades, ticker = late_trade_market()
    result = rq.replay("X", depth, trades, ticker, replay_args())
    assert fills_of(result) == [(1.0, "BUY", 10.0, "at_price")]


def test_receipt_clock_misreads_a_late_trade_as_a_cross():
    depth, trades, ticker = late_trade_market()
    result = rq.replay("X", depth, trades, ticker, replay_args(event_clock="received"))
    assert fills_of(result) == [(1.003, "BUY", 10.0, "cross")]


def stale_bid_market():
    """Our bid's level empties at 1.00; the ask reaches our price at 1.05."""
    depth = pd.DataFrame([
        depth_row(0.0, 0.0, 10.00, 1000.0, 10.01, 1000.0),
        depth_row(1.10, 1.10, 9.99, 1000.0, 10.00, 500.0),
        depth_row(40.0, 40.0, 9.99, 1000.0, 10.00, 500.0),
    ])
    ticker = pd.DataFrame([
        ticker_row(0.0, 0.0, 10.00, 1000.0, 10.01, 1000.0, 1),
        ticker_row(1.00, 1.00, 9.99, 1000.0, 10.01, 1000.0, 2),
        ticker_row(1.05, 1.05, 9.99, 1000.0, 10.00, 500.0, 3),
        ticker_row(40.0, 40.0, 9.99, 1000.0, 10.00, 500.0, 4),
    ])
    trades = pd.DataFrame([dict(received_ts=0.5, exchange_ts=0.5, trade_id=1, price=10.01, qty=1.0,
                                maker_is_buyer=False)])
    return depth, trades, ticker


def test_a_quote_pulled_on_bookticker_in_time_escapes_the_cross():
    depth, trades, ticker = stale_bid_market()
    assert fills_of(rq.replay("X", depth, trades, ticker, replay_args(react="depth"))) == [
        (1.05, "BUY", 10.0, "cross")]
    assert fills_of(rq.replay("X", depth, trades, ticker, replay_args(react="ticker"))) == []
    assert fills_of(rq.replay("X", depth, trades, ticker, replay_args(react="ticker", latency_ms=80.0))) == [
        (1.05, "BUY", 10.0, "cross")]


def test_sampled_latency_draws_from_the_given_one_way_samples():
    depth, trades, ticker = stale_bid_market()
    fast = replay_args(react="ticker", one_way_samples=np.array([2.0]))
    slow = replay_args(react="ticker", one_way_samples=np.array([80.0]))
    assert fills_of(rq.replay("X", depth, trades, ticker, fast)) == []
    assert fills_of(rq.replay("X", depth, trades, ticker, slow)) == [(1.05, "BUY", 10.0, "cross")]


def test_auc_ranks_and_ties():
    label = np.array([0, 0, 1, 1])
    assert auc(np.array([0.1, 0.2, 0.8, 0.9]), label) == 1.0
    assert auc(np.array([0.9, 0.8, 0.2, 0.1]), label) == 0.0
    assert auc(np.array([0.5, 0.5, 0.5, 0.5]), label) == 0.5


def test_logistic_regression_recovers_the_sign_of_a_real_effect():
    rng = np.random.default_rng(0)
    x = rng.normal(size=(4000, 2))
    y = (rng.random(4000) < 1 / (1 + np.exp(-(0.5 + 2.0 * x[:, 0])))).astype(float)
    beta = fit_logistic(x, y, l2=0.1)
    assert beta[1] > 1.5 and abs(beta[2]) < 0.2
