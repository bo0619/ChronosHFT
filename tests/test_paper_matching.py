import pytest
from datetime import datetime

from event.type import (
    TIF_GTX,
    TIF_RPI,
    AggTradeData,
    OrderBook,
    OrderRequest,
)
from gateway.binance.paper_matching import (
    MIN_MAKER_FEE_RATE,
    PaperMatchingEngine,
    PaperMatchingPolicy,
    PaperMatchingPort,
    PaperVenueState,
)
from gateway.binance.paper_state import PaperOrder

SYMBOL = "SNDKUSDT"


def make_order(
    client_oid: str,
    *,
    time_in_force: str,
    accept_seq: int,
    volume: float = 0.2,
) -> PaperOrder:
    return PaperOrder(
        client_oid=client_oid,
        exchange_oid=f"exchange-{client_oid}",
        request=OrderRequest(
            symbol=SYMBOL,
            price=100.0,
            volume=volume,
            side="BUY",
            time_in_force=time_in_force,
            post_only=True,
        ),
        accept_seq=accept_seq,
        created_ms=1,
        created_monotonic=1.0,
        update_ms=1,
        status="NEW",
        committed=True,
    )


class MatchingHarness:
    def __init__(
        self,
        *orders: PaperOrder,
        rpi_fill_model="public_trade_proxy",
        maker_fee=0.0002,
        cancel_ahead_model="fraction",
    ):
        self.applied_fills = []
        self.expired_orders = []
        self.state = PaperVenueState(
            orders={order.client_oid: order for order in orders},
            positions={},
            books={
                SYMBOL: OrderBook(
                    symbol=SYMBOL,
                    exchange="BINANCE",
                    datetime=datetime.now(),
                    bids={100.0: 0.0},
                    asks={101.0: 1.0},
                )
            },
            liquidity={
                SYMBOL: {
                    "bids": {100.0: 0.0},
                    "asks": {101.0: 1.0},
                }
            },
        )
        self.matching = PaperMatchingEngine(
            self.state,
            PaperMatchingPort(
                apply_fill=self.apply_fill,
                expire_order=self.expire_order,
            ),
            PaperMatchingPolicy(
                rpi_fill_model=rpi_fill_model,
                cancel_ahead_fraction=0.0,
                market_order_max_slippage_bps=100.0,
                maker_fee=maker_fee,
                taker_fee=0.0005,
                rpi_commission_rate=0.0,
                rpi_commission_rates={},
                cancel_ahead_model=cancel_ahead_model,
            ),
        )

    def apply_fill(
        self,
        order,
        quantity,
        price,
        *,
        is_maker,
        fill_context=None,
    ):
        self.applied_fills.append(
            {
                "client_oid": order.client_oid,
                "quantity": quantity,
                "price": price,
                "is_maker": is_maker,
                "context": dict(fill_context or {}),
            }
        )

    def expire_order(self, order, reason):
        self.expired_orders.append((order.client_oid, reason))


def make_trade(trade_id: int, price: float) -> AggTradeData:
    return AggTradeData(
        symbol=SYMBOL,
        trade_id=trade_id,
        price=price,
        quantity=1.0,
        maker_is_buyer=True,
        datetime=datetime.now(),
    )


def test_paper_order_state_owns_remaining_and_active_semantics():
    order = make_order("state", time_in_force=TIF_GTX, accept_seq=1)

    order.cum_filled_qty = 0.075
    assert order.active
    assert order.remaining == 0.125

    order.status = "FILLED"
    assert not order.active


def test_rpi_through_trade_is_selected_with_explicit_fill_context():
    order = make_order("rpi-through", time_in_force=TIF_RPI, accept_seq=1)
    harness = MatchingHarness(order)

    assert harness.matching.on_market_trade(make_trade(11, 99.0))

    assert len(harness.applied_fills) == 1
    fill = harness.applied_fills[0]
    assert fill["client_oid"] == "rpi-through"
    assert fill["quantity"] == 0.2
    assert fill["price"] == 100.0
    assert fill["is_maker"]
    assert fill["context"]["fill_trigger"] == "through"
    assert fill["context"]["market_trade_id"] == 11


def test_market_trade_ids_are_deduplicated_before_matching():
    order = make_order("dedupe", time_in_force=TIF_GTX, accept_seq=1)
    harness = MatchingHarness(order)
    trade = make_trade(12, 99.0)

    assert harness.matching.on_market_trade(trade)
    assert not harness.matching.on_market_trade(trade)
    assert len(harness.applied_fills) == 1


def test_disabled_rpi_model_does_not_create_proxy_fill():
    order = make_order("rpi-disabled", time_in_force=TIF_RPI, accept_seq=1)
    harness = MatchingHarness(order, rpi_fill_model="disabled")

    assert harness.matching.on_market_trade(make_trade(13, 99.0))
    assert harness.applied_fills == []


def test_non_rpi_order_moves_ahead_of_earlier_rpi_at_same_level():
    rpi = make_order("rpi", time_in_force=TIF_RPI, accept_seq=1)
    gtx = make_order("gtx", time_in_force=TIF_GTX, accept_seq=2)
    harness = MatchingHarness(rpi, gtx)

    harness.matching.insert_into_local_queue(rpi)
    harness.matching.insert_into_local_queue(gtx)

    assert gtx.queue_ahead == 0.0
    assert rpi.queue_ahead == gtx.remaining


def test_matching_component_has_explicit_venue_state_not_gateway_owner():
    harness = MatchingHarness()

    assert harness.matching.state is harness.state
    assert not hasattr(harness.matching, "_owner")


def test_negative_maker_fee_is_a_rebate_and_taker_fee_is_unchanged():
    gtx = make_order("gtx-rebate", time_in_force=TIF_GTX, accept_seq=1)
    harness = MatchingHarness(gtx, maker_fee=-0.0001)

    assert harness.matching.fee_rate(gtx, is_maker=True) == -0.0001
    assert harness.matching.fee_rate(gtx, is_maker=False) == 0.0005


def test_maker_rebate_is_bounded_by_min_maker_fee_rate():
    gtx = make_order("gtx-floor", time_in_force=TIF_GTX, accept_seq=1)
    harness = MatchingHarness(gtx, maker_fee=-0.5)

    assert harness.matching.fee_rate(gtx, is_maker=True) == MIN_MAKER_FEE_RATE


def _bid_book(size: float) -> OrderBook:
    return OrderBook(
        symbol=SYMBOL,
        exchange="BINANCE",
        datetime=datetime.now(),
        bids={100.0: size},
        asks={101.0: 1.0},
    )


def _queued_bid(queue_ahead: float) -> PaperOrder:
    order = make_order("queued", time_in_force=TIF_GTX, accept_seq=1, volume=5.0)
    order.queue_ahead = queue_ahead
    return order


def test_proportional_model_credits_our_share_of_cancels_net_of_trades():
    order = _queued_bid(3000.0)
    harness = MatchingHarness(order, cancel_ahead_model="proportional")
    harness.matching.on_book(_bid_book(8000.0))

    # The level shrinks 1,000 with no trades: all cancels, 3/8 ahead of us.
    harness.matching.on_book(_bid_book(7000.0))
    assert order.queue_ahead == pytest.approx(3000.0 - 1000.0 * 3000.0 / 8000.0)

    # 400 of the next 1,000 reduction traded (and already drained the queue
    # in on_market_trade); only the other 600 are cancels.
    ahead = order.queue_ahead
    trade = AggTradeData(
        symbol=SYMBOL, trade_id=50, price=100.0, quantity=400.0,
        maker_is_buyer=True, datetime=datetime.now(),
    )
    harness.matching.on_market_trade(trade)
    after_trade = order.queue_ahead
    assert after_trade == pytest.approx(ahead - 400.0)
    harness.matching.on_book(_bid_book(6000.0))
    assert order.queue_ahead == pytest.approx(after_trade - 600.0 * after_trade / 7000.0)
    # The trade tally is per book interval.
    assert harness.state.traded_since_book == {}


def test_growing_level_and_default_model_leave_the_queue_alone():
    order = _queued_bid(3000.0)
    harness = MatchingHarness(order, cancel_ahead_model="proportional")
    harness.matching.on_book(_bid_book(8000.0))
    harness.matching.on_book(_bid_book(9000.0))
    assert order.queue_ahead == 3000.0

    default_order = _queued_bid(3000.0)
    default = MatchingHarness(default_order)
    default.matching.on_book(_bid_book(8000.0))
    default.matching.on_book(_bid_book(7000.0))
    assert default_order.queue_ahead == 3000.0
