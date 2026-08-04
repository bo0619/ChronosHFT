import threading

from event.type import CommandOutcome, GatewayState, OrderRequest
from gateway.binance.rest_gateway import (
    BinanceRestGateway,
    BinanceRestGatewayDependencies,
)


class Response:
    def __init__(self, payload, status_code):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class Rest:
    def __init__(self, order_response=None):
        self.order_response = order_response
        self.order_calls = []
        self.query_response = None

    def new_order(self, request, client_oid, **kwargs):
        self.order_calls.append((request, client_oid, kwargs))
        return self.order_response

    def query_order(self, _symbol, _order_id):
        return self.query_response


def make_gateway(
    rest,
    *,
    active=True,
    state=GatewayState.READY,
    book_ready=True,
    clock_ready=True,
):
    return BinanceRestGateway(
        BinanceRestGatewayDependencies(
            rest=rest,
            transport_lock=threading.RLock(),
            is_transport_active=lambda: active,
            gateway_state=lambda: state,
            symbol_ready_locked=lambda _symbol: book_ready,
            require_healthy_clock=lambda: True,
            clock_health_snapshot=lambda **_kwargs: {
                "ready": clock_ready,
                "reason": "clock unavailable",
            },
            log_info=lambda _message: None,
        )
    )


def order(*, reduce_only=False):
    return OrderRequest(
        symbol="BTCUSDT",
        price=100.0,
        volume=1.0,
        side="BUY",
        reduce_only=reduce_only,
    )


def test_opening_order_requires_owned_ready_book_before_rest():
    rest = Rest(Response({"orderId": 1}, 200))
    gateway = make_gateway(rest, book_ready=False)

    result = gateway.send_order(order(), "oid-1")

    assert result.outcome == CommandOutcome.REJECTED
    assert result.error_code == "ORDERBOOK_NOT_READY"
    assert rest.order_calls == []


def test_reduce_only_order_bypasses_book_and_clock_opening_gates():
    rest = Rest(None)
    gateway = make_gateway(
        rest,
        active=False,
        state=GatewayState.ERROR,
        book_ready=False,
        clock_ready=False,
    )

    result = gateway.send_order(order(reduce_only=True), "EMERGENCY_1")

    assert result.outcome == CommandOutcome.UNKNOWN
    assert len(rest.order_calls) == 1


def test_clock_failure_rejects_before_rest_truth_is_ambiguous():
    rest = Rest(None)
    gateway = make_gateway(rest, clock_ready=False)

    result = gateway.send_order(order(), "oid-clock")

    assert result.outcome == CommandOutcome.REJECTED
    assert result.error_code == "CLOCK_UNHEALTHY"
    assert rest.order_calls == []


def test_ack_and_ambiguous_exchange_error_are_classified():
    acknowledged = Rest(Response({"orderId": 123}, 200))
    result = make_gateway(acknowledged).send_order(order(), "ENTRY_1")
    assert result.outcome == CommandOutcome.ACKNOWLEDGED
    assert result.exchange_oid == "123"

    ambiguous = Rest(
        Response(
            {"code": -1006, "msg": "execution status unknown"},
            400,
        )
    )
    result = make_gateway(ambiguous).send_order(order(), "oid-unknown")
    assert result.outcome == CommandOutcome.UNKNOWN
    assert result.error_code == "-1006"


def test_query_not_found_is_distinct_from_unavailable_truth():
    rest = Rest()
    gateway = make_gateway(rest)
    rest.query_response = Response(
        {"code": -2013, "msg": "Order does not exist"},
        400,
    )

    result = gateway.get_order("BTCUSDT", "123")

    assert result == {
        "_query_status": "NOT_FOUND",
        "code": "-2013",
        "msg": "Order does not exist",
    }


def test_rest_gateway_has_no_gateway_or_owner_back_reference():
    gateway = make_gateway(Rest())

    assert set(gateway.__dict__) == {"dependencies"}
