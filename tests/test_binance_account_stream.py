from gateway.binance.account_stream import BinanceAccountStreamParser


def test_order_update_normalization_is_deterministic():
    update = BinanceAccountStreamParser.parse_order_update(
        {
            "E": 900,
            "o": {
                "c": "client-1",
                "i": 42,
                "s": "BTCUSDT",
                "X": "PARTIALLY_FILLED",
                "l": "0.25",
                "L": "101.5",
                "z": "0.25",
                "T": 1000,
                "n": "0.01",
                "N": "USDT",
                "rp": "1.2",
                "m": True,
                "t": 7,
            },
        },
        received_timestamp=2.0,
        received_monotonic=3.0,
        corrected_received_timestamp=2.1,
        clock_offset_ms=100.0,
        now=lambda: 4.0,
        monotonic=lambda: 5.0,
    )

    assert update.client_oid == "client-1"
    assert update.exchange_oid == "42"
    assert update.update_time == 1.0
    assert update.realized_pnl == 1.2
    assert update.commission == 0.01
    assert update.trade_id == 7
    assert update.received_timestamp == 2.0
    assert update.dispatch_timestamp == 4.0
    assert update.dispatch_monotonic == 5.0


def test_account_update_prefers_the_quote_asset_for_tracked_symbols():
    update = BinanceAccountStreamParser.parse_account_update(
        {
            "T": 2000,
            "a": {
                "m": "ORDER",
                "B": [
                    {"a": "USDT", "wb": "100", "cw": "90"},
                    {"a": "USDC", "wb": "250", "cw": "240", "bc": "2"},
                ],
                "P": [
                    {"s": "ETHUSDC", "pa": "2", "ep": "10", "up": "1"}
                ],
            },
        },
        tracked_symbols=["ETHUSDC"],
        received_timestamp=3.0,
        received_monotonic=4.0,
        now=lambda: 5.0,
        monotonic=lambda: 6.0,
    )

    assert update.asset == "USDC"
    assert update.wallet_balance == 250.0
    assert update.available_balance is None
    assert update.event_time == 2.0
    assert update.balances["USDC"] == {
        "wallet_balance": 250.0,
        "available_balance": None,
        "cross_wallet_balance": 240.0,
        "balance_change": 2.0,
    }
    assert update.positions["ETHUSDC"]["volume"] == 2.0


def test_position_only_update_preserves_missing_balance_truth():
    update = BinanceAccountStreamParser.parse_account_update(
        {
            "E": 3000,
            "a": {
                "m": "MARGIN_TYPE_CHANGE",
                "B": [],
                "P": [{"s": "BTCUSDT", "pa": "0.5"}],
            },
        },
        tracked_symbols=["BTCUSDT"],
        now=lambda: 4.0,
        monotonic=lambda: 5.0,
    )

    assert update.asset == ""
    assert update.wallet_balance == 0.0
    assert update.balances == {}
    assert update.positions["BTCUSDT"]["volume"] == 0.5
