from collections import deque

import pytest

from oms.exchange_snapshot import (
    ExchangeSnapshotNormalizer,
    ExchangeSnapshotQueries,
    StableExchangeSnapshotCollector,
    StableSnapshotPolicy,
)


def _collector(
    *,
    positions,
    open_orders=None,
    account=None,
    max_attempts=4,
    stability_required=2,
):
    normalizer = ExchangeSnapshotNormalizer()
    position_snapshots = deque(positions)
    audit = []
    sleeps = []
    clock = iter(range(1_000, 100_000, 100))
    default_account = {
        "totalWalletBalance": "1000",
        "totalInitialMargin": "0",
    }
    queries = ExchangeSnapshotQueries(
        open_orders=lambda: [] if open_orders is None else open_orders(),
        account=lambda: default_account if account is None else account(),
        positions=lambda: position_snapshots.popleft(),
    )
    collector = StableExchangeSnapshotCollector(
        queries=queries,
        policy=lambda: StableSnapshotPolicy(
            stability_required=stability_required,
            max_attempts=max_attempts,
            settle_interval_sec=0.25,
        ),
        normalize_account=normalizer.account,
        normalize_positions=normalizer.positions,
        normalize_open_orders=normalizer.open_orders,
        snapshot_signature=lambda remote_account, remote_positions, orders: (
            normalizer.signature(
                remote_account,
                remote_positions,
                normalizer.open_orders(orders),
            )
        ),
        audit=lambda event, **fields: audit.append((event, fields)),
        now_ms=lambda: next(clock),
        sleep=sleeps.append,
    )
    return collector, audit, sleeps


def test_normalizer_canonicalizes_complete_snapshot_without_mutating_input():
    normalizer = ExchangeSnapshotNormalizer()
    account = {
        "totalWalletBalance": "1000.5",
        "totalInitialMargin": "12",
        "assets": [
            {
                "asset": "usdt",
                "walletBalance": "1000.5",
                "availableBalance": "988.5",
            }
        ],
    }
    positions = [
        {
            "symbol": "btcusdt",
            "positionSide": "both",
            "positionAmt": "0.25",
            "entryPrice": "50000",
        }
    ]
    orders = [
        {
            "symbol": "btcusdt",
            "orderId": 7,
            "clientOrderId": "oid-7",
            "side": "buy",
        }
    ]

    normalized_account = normalizer.account(
        account,
        require_initial_margin=True,
    )
    normalized_positions = normalizer.positions(positions)
    normalized_orders = normalizer.open_orders(orders)

    assert normalized_account["totalWalletBalance"] == 1000.5
    assert normalizer.account_balances(account) == {
        "USDT": {
            "wallet_balance": 1000.5,
            "available_balance": 988.5,
        }
    }
    assert normalized_positions[0]["symbol"] == "BTCUSDT"
    assert normalized_positions[0]["positionAmt"] == 0.25
    assert normalized_orders == [
        {
            "symbol": "BTCUSDT",
            "identifiers": ("7", "oid-7"),
            "side": "BUY",
        }
    ]
    assert account["totalWalletBalance"] == "1000.5"
    assert positions[0]["positionAmt"] == "0.25"


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (
            lambda normalizer: normalizer.account(
                {"totalWalletBalance": "nan"}
            ),
            "not finite",
        ),
        (
            lambda normalizer: normalizer.account(
                {
                    "totalWalletBalance": "1",
                    "totalInitialMargin": "-1",
                },
                require_initial_margin=True,
            ),
            "negative",
        ),
        (
            lambda normalizer: normalizer.positions(
                [
                    {
                        "symbol": "BTCUSDT",
                        "positionSide": "LONG",
                    }
                ]
            ),
            "not one-way/BOTH",
        ),
        (
            lambda normalizer: normalizer.positions(
                [
                    {"symbol": "BTCUSDT"},
                    {"symbol": "btcusdt"},
                ]
            ),
            "duplicate BTCUSDT",
        ),
        (
            lambda normalizer: normalizer.open_orders(
                [{"orderId": 1}]
            ),
            "missing symbol",
        ),
    ],
)
def test_normalizer_rejects_ambiguous_or_nonfinite_truth(operation, message):
    with pytest.raises(ValueError, match=message):
        operation(ExchangeSnapshotNormalizer())


def test_collector_requires_consecutive_matching_structural_snapshots():
    changed_position = {
        "symbol": "BTCUSDT",
        "positionAmt": "1",
        "entryPrice": "100",
    }
    collector, audit, sleeps = _collector(
        positions=[[], [changed_position], [changed_position]],
    )

    snapshot = collector.capture()

    assert snapshot["attempt"] == 3
    assert snapshot["positions"][0]["positionAmt"] == 1.0
    assert snapshot["account_floor"] < snapshot["positions_floor"]
    assert audit == [
        (
            "stable_snapshot_acquired",
            {
                "attempts": 3,
                "stable_count": 2,
                "end_time_ms": snapshot["end_time_ms"],
            },
        )
    ]
    assert sleeps == [0.25, 0.25]


def test_collector_fails_closed_when_open_orders_never_clear():
    def orders():
        return [
            {
                "symbol": "BTCUSDT",
                "orderId": 9,
                "clientOrderId": "residual-9",
                "side": "SELL",
            }
        ]

    collector, audit, sleeps = _collector(
        positions=[[], []],
        open_orders=orders,
        max_attempts=2,
    )

    with pytest.raises(RuntimeError, match="residual open orders"):
        collector.capture(require_no_open_orders=True)

    assert audit == []
    assert sleeps == [0.25, 0.25]


def test_collector_fails_immediately_on_incomplete_api_snapshot():
    collector, audit, sleeps = _collector(
        positions=[[]],
        account=lambda: None,
    )

    with pytest.raises(RuntimeError, match="API failed"):
        collector.capture()

    assert audit == []
    assert sleeps == []
