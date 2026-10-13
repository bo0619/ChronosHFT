from types import SimpleNamespace

import pytest

from alpha.queue_imbalance import (
    QueueImbalanceConfig,
    QueueImbalanceModel,
    queue_imbalance,
)
from event.type import Side
from strategy.fast_requote import FastRequoteMixin
from strategy.touch_snap import step_back_against_imbalance

TICK = 0.01


def model(**overrides):
    settings = {"min_moves_per_bin": 5.0, "half_life_moves": 1e12, **overrides}
    return QueueImbalanceModel(QueueImbalanceConfig(enabled=True, **settings))


def train(qi, moves, *, bid_qty, ask_qty, up, updates=3):
    """Each spell: ``updates`` books at one imbalance, then a one-tick move."""
    bid = 14.37
    for _ in range(moves):
        for _ in range(updates):
            qi.on_book(bid, bid_qty, bid + TICK, ask_qty)
        bid = bid + TICK if up else bid - TICK
        # The move is recorded when the next book arrives at the new mid.
        qi.on_book(bid, bid_qty, bid + TICK, ask_qty)


def test_imbalance_is_signed_queue_share():
    assert queue_imbalance(500.0, 1500.0) == pytest.approx(-0.5)
    assert queue_imbalance(0.0, 0.0) is None


def test_learns_that_a_thin_bid_queue_precedes_a_down_move():
    qi = model()
    # Thin bid queue (I = -0.6) is followed by down moves 9 times in 10.
    train(qi, 9, bid_qty=1000.0, ask_qty=4000.0, up=False)
    train(qi, 1, bid_qty=1000.0, ask_qty=4000.0, up=True)

    # Per update: the first spell has 3 updates, later ones 4 (the book at
    # the new mid opens the next spell): 3 + 8 * 4 = 35 down, 4 up.
    against_bid = qi.against_probability(Side.BUY, -0.6)
    assert against_bid == pytest.approx((35 + 1) / (35 + 4 + 2))
    assert qi.against_probability(Side.SELL, -0.6) == pytest.approx(1 - against_bid)

    qi.on_book(14.37, 1000.0, 14.38, 4000.0)
    assert qi.against(Side.BUY)
    assert not qi.against(Side.SELL)


def test_no_step_back_until_a_bin_has_enough_moves():
    qi = model()
    train(qi, 4, bid_qty=1000.0, ask_qty=4000.0, up=False)
    assert qi.against_probability(Side.BUY, -0.6) is None
    qi.on_book(14.37, 1000.0, 14.38, 4000.0)
    assert not qi.against(Side.BUY)


def test_each_move_counts_once_as_evidence_however_many_updates():
    qi = model()
    train(qi, 5, bid_qty=1000.0, ask_qty=4000.0, up=False, updates=50)
    row = qi.table()[qi._bin(-0.6)]
    assert row["moves"] == pytest.approx(5.0)


def test_old_evidence_decays_by_half_life():
    qi = model(half_life_moves=10.0)
    train(qi, 10, bid_qty=1000.0, ask_qty=4000.0, up=False)
    before = qi.table()[qi._bin(-0.6)]["moves"]
    # Ten more moves in another bin halve the first bin's evidence.
    train(qi, 10, bid_qty=4000.0, ask_qty=1000.0, up=True)
    # The spell open at the switch adds a small share to the first bin.
    assert qi.table()[qi._bin(-0.6)]["moves"] == pytest.approx(before / 2.0, rel=0.1)


def test_symbol_overrides_and_validation():
    section = {"enabled": True, "symbols": {"KORUUSDT": {"against_threshold": 0.7}}}
    assert QueueImbalanceConfig.for_symbol(section, "KORUUSDT").against_threshold == 0.7
    assert QueueImbalanceConfig.for_symbol(section, "SOXLUSDT").against_threshold == 0.65
    with pytest.raises(ValueError):
        QueueImbalanceConfig.for_symbol({"against_threshold": 0.4}, "KORUUSDT")


def test_step_back_moves_only_the_side_the_imbalance_is_against():
    against_bid = SimpleNamespace(against=lambda side: side == Side.BUY)
    assert step_back_against_imbalance(
        14.37, 14.38, model=against_bid, best_bid=14.37, best_ask=14.38, tick_size=TICK
    ) == (pytest.approx(14.36), 14.38)
    # A target already further back is kept.
    assert step_back_against_imbalance(
        14.35, 14.38, model=against_bid, best_bid=14.37, best_ask=14.38, tick_size=TICK
    ) == (14.35, 14.38)
    assert step_back_against_imbalance(
        14.37, 14.38, model=None, best_bid=14.37, best_ask=14.38, tick_size=TICK
    ) == (14.37, 14.38)


class _Quoter(FastRequoteMixin):
    def __init__(self, model):
        self.calibrators = {"KORUUSDT": SimpleNamespace(queue_imbalance=model)}
        self.orders_cancelling = set()
        self.cancelled = []

    def cancel_order(self, client_oid):
        self.cancelled.append(client_oid)


def test_resting_touch_quote_is_pulled_when_the_imbalance_turns_against_it():
    qi = model()
    train(qi, 10, bid_qty=1000.0, ask_qty=4000.0, up=False)
    qi.on_book(14.37, 1000.0, 14.38, 4000.0)
    quoter = _Quoter(qi)
    quotes = {
        "bid_oid": "bid-1", "bid_price": 14.37,
        "ask_oid": "ask-1", "ask_price": 14.38,
    }

    assert quoter._pull_against_imbalance("KORUUSDT", quotes)
    assert quoter.cancelled == ["bid-1"]

    # A bid already one level back is left in its queue.
    quoter.cancelled.clear()
    quotes["bid_price"] = 14.36
    assert not quoter._pull_against_imbalance("KORUUSDT", quotes)
    assert quoter.cancelled == []
