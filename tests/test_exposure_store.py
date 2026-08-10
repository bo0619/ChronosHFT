import json

import pytest

from event.type import OrderIntent, OrderStatus, Side
from oms.exposure import ExposureStore
from oms.order import Order


class _MarketCache:
    @staticmethod
    def get_mark_price(_symbol: str) -> float:
        return 0.0

    @staticmethod
    def get_best_quote(_symbol: str) -> tuple[float, float]:
        return 0.0, 0.0


def _store() -> ExposureStore:
    return ExposureStore(market_cache=_MarketCache())


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
    store = _store()
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
    store = _store()
    store.force_sync("BTCUSDT", 1.0, 100.0)
    snapshot = store.snapshot()

    store.force_sync("BTCUSDT", 2.0, 101.0)

    assert snapshot.net_positions["BTCUSDT"] == 1.0
    with pytest.raises(TypeError):
        snapshot.net_positions["BTCUSDT"] = 3.0


def test_account_reset_owns_every_order_reservation_ledger():
    store = _store()
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
    store = _store()
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


def test_exposure_ledger_views_reject_external_writes():
    store = _store()
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


def test_repeated_lot_fills_do_not_accumulate_binary_float_noise():
    store = _store()

    for _ in range(10_000):
        store.on_fill("BTCUSDT", Side.BUY, 0.000001, 100.0)

    # The public contract remains float, while the cumulative result is the
    # exact decimal quantity represented by the repeated exchange lots.
    assert store.net_positions["BTCUSDT"] == 0.01
    assert store.avg_prices["BTCUSDT"] == 100.0


def test_strategy_reconciliation_does_not_create_float_residual():
    store = _store()

    for _ in range(10_000):
        store.on_strategy_fill(
            "alpha",
            "BTCUSDT",
            Side.BUY,
            0.000001,
            100.0,
        )

    assert store.strategy_net_positions[("alpha", "BTCUSDT")] == 0.01
    assert store.reconcile_strategy_position("BTCUSDT", 0.01, 100.0) == 0.0
    assert store.strategy_net_positions[("exchange_recovery", "BTCUSDT")] == 0.0


def test_strategy_reconciliation_uses_stable_attribution_sum():
    store = _store()
    store.restore_strategy_ledgers(
        {
            ("large-long", "BTCUSDT"): 1e16,
            ("small-long", "BTCUSDT"): 1.0,
            ("large-short", "BTCUSDT"): -1e16,
        },
        {
            ("large-long", "BTCUSDT"): 100.0,
            ("small-long", "BTCUSDT"): 100.0,
            ("large-short", "BTCUSDT"): 100.0,
        },
    )

    assert store.reconcile_strategy_position("BTCUSDT", 1.0, 100.0) == 0.0
