import json

import pytest

from event.type import OrderIntent, OrderStatus, Side
from oms.exposure import ExposureManager, ExposureStore
from oms.order import Order


def _active_order(
    client_oid: str,
    *,
    strategy_id: str,
    symbol: str,
    side: Side,
    volume: float,
    reduce_only: bool = False,
) -> Order:
    order = Order(
        client_oid,
        OrderIntent(
            strategy_id,
            symbol,
            side,
            100.0,
            volume,
            reduce_only=reduce_only,
        ),
    )
    order.status = OrderStatus.NEW
    return order


def test_exposure_store_replaces_strategy_ledgers_atomically():
    store = ExposureStore()
    store.on_strategy_fill("alpha", "BTCUSDT", Side.BUY, 1.0, 100.0)
    before = store.snapshot()

    with pytest.raises(ValueError, match="values are invalid"):
        store.restore_strategy_ledgers(
            {("alpha", "BTCUSDT"): 2.0},
            {("alpha", "BTCUSDT"): float("nan")},
        )

    assert store.snapshot() == before

    store.update_open_orders(
        {
            "open-buy": _active_order(
                "open-buy",
                strategy_id="alpha",
                symbol="BTCUSDT",
                side=Side.BUY,
                volume=3.0,
            )
        }
    )
    store.restore_strategy_ledgers(
        {
            ("alpha", "BTCUSDT"): 0.5,
            ("beta", "ETHUSDT"): -2.0,
        },
        {
            ("alpha", "BTCUSDT"): 101.0,
            ("beta", "ETHUSDT"): 50.0,
        },
    )

    snapshot = store.snapshot()
    assert dict(snapshot.strategy_net_positions) == {
        ("alpha", "BTCUSDT"): 0.5,
        ("beta", "ETHUSDT"): -2.0,
    }
    assert dict(snapshot.strategy_open_buy_qty) == {}
    assert dict(snapshot.strategy_open_sell_qty) == {}


def test_exposure_snapshot_is_detached_and_immutable():
    store = ExposureStore()
    store.force_sync("BTCUSDT", 1.0, 100.0)
    snapshot = store.snapshot()

    store.force_sync("BTCUSDT", 2.0, 101.0)

    assert snapshot.net_positions["BTCUSDT"] == 1.0
    with pytest.raises(TypeError):
        snapshot.net_positions["BTCUSDT"] = 3.0


def test_account_reset_owns_every_order_reservation_ledger():
    store = ExposureStore()
    store.force_sync("BTCUSDT", 1.0, 100.0)
    store.update_open_orders(
        {
            "open-buy": _active_order(
                "open-buy",
                strategy_id="alpha",
                symbol="BTCUSDT",
                side=Side.BUY,
                volume=2.0,
            ),
            "reduce-only": _active_order(
                "reduce-only",
                strategy_id="alpha",
                symbol="BTCUSDT",
                side=Side.SELL,
                volume=1.0,
                reduce_only=True,
            ),
        }
    )
    store.on_strategy_fill("alpha", "BTCUSDT", Side.BUY, 1.0, 100.0)

    store.reset_account_ledgers({})
    snapshot = store.snapshot()

    assert dict(snapshot.net_positions) == {}
    assert dict(snapshot.avg_prices) == {}
    assert dict(snapshot.open_buy_qty) == {}
    assert dict(snapshot.reduce_only_sell_qty) == {}
    assert dict(snapshot.strategy_open_buy_qty) == {}
    assert snapshot.strategy_net_positions[("alpha", "BTCUSDT")] == 1.0


def test_strategy_checkpoint_rows_are_deterministic_and_json_safe():
    store = ExposureStore()
    store.restore_strategy_ledgers(
        {
            ("beta", "ETHUSDT"): -2.0,
            ("alpha", "BTCUSDT"): 0.5,
        },
        {
            ("beta", "ETHUSDT"): 50.0,
            ("alpha", "BTCUSDT"): 101.0,
        },
    )

    rows = store.strategy_checkpoint_rows()

    assert [row["strategy_id"] for row in rows] == ["alpha", "beta"]
    assert json.loads(json.dumps(rows))[0]["symbol"] == "BTCUSDT"
    assert ExposureManager is ExposureStore


def test_exposure_ledger_views_reject_external_writes():
    store = ExposureStore()
    ledgers = (
        "net_positions",
        "avg_prices",
        "open_buy_qty",
        "open_sell_qty",
        "reduce_only_buy_qty",
        "reduce_only_sell_qty",
        "strategy_net_positions",
        "strategy_avg_prices",
        "strategy_open_buy_qty",
        "strategy_open_sell_qty",
    )

    for field in ledgers:
        view = getattr(store, field)
        with pytest.raises(TypeError):
            view["BTCUSDT"] = 1.0

    with pytest.raises(AttributeError):
        store.net_positions = {"BTCUSDT": 1.0}
