from event.type import OrderIntent, OrderStatus, Side
from oms.order import Order
from oms.order_store import OrderStore


def _order(client_oid: str, *, terminal: bool = False) -> Order:
    order = Order(
        client_oid,
        OrderIntent("strategy", "BTCUSDT", Side.BUY, 100.0, 1.0),
    )
    order.mark_submitting()
    order.mark_new(exchange_oid=f"exchange-{client_oid}")
    if terminal:
        order.mark_cancelled()
    return order


def test_order_store_exposes_read_only_view_and_active_projection():
    store = OrderStore(terminal_limit=2)
    active = _order("active")
    terminal = _order("terminal", terminal=True)
    store.add(active)
    store.add(terminal)

    assert store.view()["active"] is active
    assert store.view()["terminal"] is terminal
    assert tuple(store.active_view()) == ("active",)
    try:
        store.view()["new"] = active
    except TypeError:
        pass
    else:  # pragma: no cover - mapping proxy must reject writes
        raise AssertionError("order view must be read-only")


def test_submitting_order_is_immediately_in_active_projection():
    store = OrderStore()
    order = Order(
        "submitting",
        OrderIntent("strategy", "BTCUSDT", Side.BUY, 100.0, 1.0),
    )
    order.mark_submitting()

    store.add(order)

    assert store.active_view()[order.client_oid] is order


def test_order_store_reindexes_terminal_orders_and_bounds_retention():
    store = OrderStore(terminal_limit=1)
    first = _order("first")
    second = _order("second")
    store.add(first)
    store.add(second)

    first.mark_cancelled()
    store.reindex(first)
    second.mark_cancelled()
    store.reindex(second)

    assert "first" not in store.view()
    assert store.view()["second"] is second
    snapshot = store.snapshot()
    assert snapshot.active_count == 0
    assert snapshot.terminal_count == 1
    assert snapshot.terminal[0].status == OrderStatus.CANCELLED


def test_order_store_replace_active_is_atomic_and_rejects_terminal_payload():
    store = OrderStore()
    current = _order("current")
    store.add(current)
    terminal = _order("terminal", terminal=True)

    try:
        store.replace_active([terminal])
    except ValueError:
        pass
    else:  # pragma: no cover - invalid replacement must fail
        raise AssertionError("terminal replacement must be rejected")

    assert store.view()["current"] is current
    store.replace_active([_order("replacement")])
    assert tuple(store.view()) == ("replacement",)
