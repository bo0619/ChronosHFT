import math

from scripts.rank_tradfi_symbols import rank_rows, tradfi_contracts


def test_tradfi_contracts_keeps_trading_tradfi_only():
    info = {
        "symbols": [
            {
                "symbol": "SNDKUSDT",
                "contractType": "TRADIFI_PERPETUAL",
                "status": "TRADING",
                "permissionSets": [["GRID", "RPI"]],
                "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"}],
            },
            {"symbol": "BTCUSDT", "contractType": "PERPETUAL", "status": "TRADING"},
            {
                "symbol": "OLDUSDT",
                "contractType": "TRADIFI_PERPETUAL",
                "status": "SETTLING",
            },
        ]
    }
    contracts = tradfi_contracts(info)
    assert contracts == {"SNDKUSDT": {"tick_size": 0.01, "rpi": True}}


def test_rank_rows_sorts_by_volume_and_computes_tick_bps():
    contracts = {
        "AUSDT": {"tick_size": 0.01, "rpi": True},
        "BUSDT": {"tick_size": 0.1, "rpi": False},
    }
    tickers = [
        {"symbol": "AUSDT", "lastPrice": "100", "quoteVolume": "5", "count": 3},
        {"symbol": "BUSDT", "lastPrice": "50", "quoteVolume": "9", "count": 7},
        {"symbol": "BTCUSDT", "lastPrice": "1", "quoteVolume": "99", "count": 1},
    ]
    rows = rank_rows(contracts, tickers, {"AUSDT": [1.0, 2.0, 3.0]})
    assert [row["symbol"] for row in rows] == ["BUSDT", "AUSDT"]
    assert math.isclose(rows[1]["tick_bps"], 1.0)
    assert math.isclose(rows[1]["spread_bps_median"], 2.0)
    assert math.isclose(rows[1]["spread_ticks_median"], 2.0)
    assert math.isnan(rows[0]["spread_bps_median"])
