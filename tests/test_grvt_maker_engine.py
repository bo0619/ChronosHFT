"""The live GRVT maker engine against a fake executor and clock."""

import itertools
import math

from strategy.grvt_cross_maker import QuoteParams, quote_targets
from strategy.grvt_maker_engine import InstrumentRules, MakerEngine, Mode, RiskLimits, external_touch


class FakeExecutor:
    def __init__(self):
        self.calls = []

    def place(self, *, client_id, side, price, size, reduce_only):
        self.calls.append(("place", client_id, side, price, size, reduce_only))

    def cancel(self, *, client_id):
        self.calls.append(("cancel", client_id))

    def cancel_all(self):
        self.calls.append(("cancel_all",))

    def take(self):
        calls, self.calls = self.calls, []
        return calls


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


RULES = InstrumentRules(tick=0.0001, size_step=0.1, min_size=0.1, min_notional=5.0)
PARAMS = QuoteParams(notional=100.0, half_spread_bps=0.25, skew_bps_per_lot=1.0, max_inventory_lots=5.0)
LIMITS = RiskLimits(max_position_usd=500.0, daily_loss_reduce_usd=5.0, daily_loss_halt_usd=10.0)


def engine(**overrides):
    clock, executor, events = Clock(), FakeExecutor(), []
    ids = (f"c{n}" for n in itertools.count(1))
    eng = MakerEngine(symbol="XRPUSDT", rules=overrides.pop("rules", RULES), params=overrides.pop("params", PARAMS),
                      limits=overrides.pop("limits", LIMITS), executor=executor, new_client_id=lambda: next(ids),
                      clock=clock, log=lambda event, **f: events.append((event, f)))
    return eng, executor, clock, events


def warm(eng, executor, bid=1.2990, ask=1.3010, binance=(1.2995, 1.3005)):
    eng.on_binance(*binance)
    eng.on_grvt_book(bid, ask)
    return executor.take()


def ack_all(eng, calls):
    for call in calls:
        if call[0] == "place":
            eng.on_place_result(call[1], True, order_id="x" + call[1])
        elif call[0] == "cancel":
            eng.on_cancel_result(call[1], True)


def test_quotes_both_sides_at_the_shared_targets():
    eng, ex, _, _ = engine()
    calls = warm(eng, ex)
    want = quote_targets(bid=1.2990, ask=1.3010, binance_mid=1.3, log_basis=eng.basis.log_basis,
                         position=0.0, params=PARAMS, tick=RULES.tick)
    placed = {c[2]: c for c in calls if c[0] == "place"}
    assert placed["BUY"][3] == want["BUY"] and placed["SELL"][3] == want["SELL"]
    assert placed["BUY"][4] == math.floor(100 / want["BUY"] / 0.1) * 0.1 or abs(
        placed["BUY"][4] - math.floor(100 / want["BUY"] / 0.1 + 1e-9) * 0.1) < 1e-9
    assert all(not c[5] for c in placed.values())


def test_adverse_binance_move_cancels_and_replaces_at_once_but_waits_for_answers():
    eng, ex, clock, _ = engine()
    ack_all(eng, warm(eng, ex))
    old_bid = eng.orders[eng.current["BUY"]].price
    clock.t += 0.01
    eng.on_binance(1.2985, 1.2995)  # Binance down 10 bps: our bid is now too high
    calls = ex.take()
    kinds = [(c[0], c[2] if c[0] == "place" else None) for c in calls]
    assert ("cancel", None) in kinds and ("place", "BUY") in kinds
    new_bid = next(c[3] for c in calls if c[0] == "place" and c[2] == "BUY")
    assert new_bid < old_bid
    eng.on_binance(1.2980, 1.2990)  # still in flight: nothing new on that side
    assert not [c for c in ex.take() if c[0] == "place" and c[2] == "BUY"]


def test_passive_quote_waits_for_min_rest_before_improving():
    eng, ex, clock, _ = engine()
    ack_all(eng, warm(eng, ex))
    clock.t += 0.2
    eng.on_binance(1.3005, 1.3015)  # up 10 bps: our bid is behind, not dangerous
    assert not [c for c in ex.take() if c[0] == "place" and c[2] == "BUY"]
    clock.t += 1.0
    eng.on_binance(1.3005, 1.3015)
    assert [c for c in ex.take() if c[0] == "place" and c[2] == "BUY"]


def test_stale_binance_pulls_both_quotes():
    eng, ex, clock, events = engine()
    ack_all(eng, warm(eng, ex))
    clock.t += 3.5
    eng.on_grvt_book(1.2990, 1.3010)
    calls = ex.take()
    assert sorted(c[0] for c in calls) == ["cancel", "cancel"]
    assert ("quiet", {"reason": "binance stale"}) in events


def test_quiet_market_keeps_quotes_while_feeds_answer_pings():
    eng, ex, clock, events = engine()
    ack_all(eng, warm(eng, ex))
    for _ in range(10):  # ten seconds without a price change, a pong each second
        clock.t += 1.0
        eng.on_feed_alive("binance")
        eng.on_feed_alive("grvt")
        eng.on_timer()
    assert not [c for c in ex.take() if c[0] == "cancel"]
    clock.t += 3.5  # pongs stop: the Binance connection is gone
    eng.on_timer()
    assert sorted(c[0] for c in ex.take()) == ["cancel", "cancel"]
    assert ("quiet", {"reason": "binance stale"}) in events


def test_binance_lag_pulls_quotes():
    eng, ex, clock, _ = engine()
    ack_all(eng, warm(eng, ex))
    eng.on_binance(1.2995, 1.3005, event_ms=clock.t * 1000 - 400)
    assert sorted(c[0] for c in ex.take()) == ["cancel", "cancel"]


def test_fill_moves_position_and_skews_both_quotes_down():
    eng, ex, clock, _ = engine()
    calls = warm(eng, ex)
    ack_all(eng, calls)
    bid = eng.orders[eng.current["BUY"]]
    eng.on_fill(trade_id="t1", client_id=bid.client_id, is_buyer=True, size=bid.size, price=bid.price,
                fee=-0.0000001, is_taker=False)
    assert abs(eng.position - bid.size) < 1e-9
    eng.on_fill(trade_id="t1", client_id=bid.client_id, is_buyer=True, size=bid.size, price=bid.price,
                fee=0.0, is_taker=False)  # duplicate ignored
    assert abs(eng.position - bid.size) < 1e-9
    want = quote_targets(bid=eng.bid, ask=eng.ask, binance_mid=eng.binance_mid, log_basis=eng.basis.log_basis,
                         position=eng.position, params=PARAMS, tick=RULES.tick)
    flat = quote_targets(bid=eng.bid, ask=eng.ask, binance_mid=eng.binance_mid, log_basis=eng.basis.log_basis,
                         position=0.0, params=PARAMS, tick=RULES.tick)
    assert want["SELL"] <= flat["SELL"]


def test_position_cap_pulls_the_adding_side():
    eng, ex, _, _ = engine(limits=RiskLimits(max_position_usd=150.0, daily_loss_reduce_usd=50, daily_loss_halt_usd=99))
    ack_all(eng, warm(eng, ex))
    eng.on_position(120.0)  # ~156 USD long
    calls = ex.take()
    assert ("cancel", "c1") in calls  # c1 is the first (BUY) quote
    assert not [c for c in calls if c[0] == "place" and c[2] == "BUY"]


def test_taker_fill_or_fee_surprise_halts_and_cancels_everything():
    for kwargs in ({"is_taker": True, "fee": 0.0}, {"is_taker": False, "fee": 0.02}):
        eng, ex, _, events = engine()
        ack_all(eng, warm(eng, ex))
        order = eng.orders[eng.current["BUY"]]
        eng.on_fill(trade_id="t9", client_id=order.client_id, is_buyer=True, size=1.0, price=order.price, **kwargs)
        assert eng.mode is Mode.HALTED
        calls = ex.take()
        assert ("cancel_all",) in calls and not [c for c in calls if c[0] == "place"]


def test_daily_loss_goes_reduce_only_then_halts():
    eng, ex, clock, events = engine()
    ack_all(eng, warm(eng, ex))
    eng.on_position(300.0)  # long ~390 USD
    ack_all(eng, ex.take())
    eng.on_binance(1.2795, 1.2805)
    eng.on_grvt_book(1.2790, 1.2810)  # -1.5%: about -6 USD
    assert eng.mode is Mode.REDUCE_ONLY
    ack_all(eng, ex.take())
    clock.t += 1.1
    eng.on_timer()
    calls = ex.take()
    places = [c for c in calls if c[0] == "place"]
    assert places and all(c[2] == "SELL" and c[5] for c in places)
    eng.on_binance(1.2645, 1.2655)
    eng.on_grvt_book(1.2640, 1.2660)  # about -10.5 USD
    assert eng.mode is Mode.HALTED and ("cancel_all",) in ex.take()


def test_post_only_reject_is_not_an_error_but_repeated_errors_halt():
    eng, ex, _, _ = engine()
    calls = warm(eng, ex)
    eng.on_place_result(calls[0][1], False, error="FAIL_POST_ONLY", post_only_reject=True)
    assert eng.errors == 0 and eng.mode is Mode.RUNNING
    for n in range(5):
        for call in ex.take():
            if call[0] == "place":
                eng.on_place_result(call[1], False, error="HTTP 500")
        if eng.mode is Mode.HALTED:
            break
        eng.on_timer()
    assert eng.mode is Mode.HALTED


def test_reconcile_drops_missed_orders_and_cancels_unknown_ones():
    eng, ex, clock, _ = engine()
    ack_all(eng, warm(eng, ex))
    buy, sell = eng.current["BUY"], eng.current["SELL"]
    clock.t += 5
    eng.reconcile({sell: {}, "stray": {}}, position=0.0)
    assert buy not in eng.orders and sell in eng.orders
    assert ("cancel", "stray") in ex.calls


def test_external_touch_removes_our_size():
    bids = {1.0: 5.0, 0.99: 3.0}
    asks = {1.01: 2.0, 1.02: 4.0}
    assert external_touch(bids, asks, {"BUY": {1.0: 5.0}, "SELL": {1.01: 1.0}}) == (0.99, 1.01)
    assert external_touch(bids, asks, {}) == (1.0, 1.01)


def test_vol_tracker_decays_and_widens_quotes_when_asked():
    from strategy.grvt_cross_maker import VolTracker
    vol = VolTracker(10.0)
    vol.update(0.0, 100.0)
    vol.update(1.0, 100.1)  # a 10 bps move
    assert 9.0 < vol.vol_bps(1.0) < 10.5
    assert vol.vol_bps(31.0) < vol.vol_bps(1.0) * 0.25  # three windows later most of it is gone
    wide = QuoteParams(notional=100.0, half_spread_bps=0.25, vol_spread_coef=0.2)
    args = dict(bid=1.2990, ask=1.3010, binance_mid=1.3, log_basis=0.0, position=0.0, tick=RULES.tick)
    calm = quote_targets(params=wide, vol_bps=0.0, **args)
    busy = quote_targets(params=wide, vol_bps=10.0, **args)  # +2 bps half spread
    assert busy["BUY"] < calm["BUY"] and busy["SELL"] > calm["SELL"]
    assert quote_targets(params=PARAMS, vol_bps=10.0, **args) == quote_targets(params=PARAMS, **args)
