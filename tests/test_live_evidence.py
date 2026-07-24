from datetime import datetime

import pytest

from data.live_evidence import (
    LiveEvidenceWriteError,
    account_update_evidence_payload,
    mark_price_evidence_payload,
)
from event.type import ExchangeAccountUpdate, MarkPriceData


def test_mark_price_payload_preserves_exchange_funding_truth():
    mark = MarkPriceData(
        symbol="XAUUSDT",
        mark_price=2400.5,
        index_price=2400.25,
        funding_rate=-0.0001,
        next_funding_time=datetime.fromtimestamp(1_700_028_800.0),
        datetime=datetime.fromtimestamp(1_700_000_000.0),
        exchange_timestamp=1_700_000_000.0,
        received_timestamp=1_700_000_000.01,
        corrected_received_timestamp=1_700_000_000.012,
        clock_offset_ms=2.0,
        next_funding_timestamp=1_700_028_800.0,
    )

    payload = mark_price_evidence_payload(mark)

    assert payload == {
        "symbol": "XAUUSDT",
        "mark_price": 2400.5,
        "index_price": 2400.25,
        "funding_rate": -0.0001,
        "next_funding_timestamp": 1_700_028_800.0,
        "exchange_timestamp": 1_700_000_000.0,
        "received_timestamp": 1_700_000_000.01,
        "corrected_received_timestamp": 1_700_000_000.012,
        "clock_offset_ms": 2.0,
    }


def test_account_payload_preserves_funding_balance_change():
    update = ExchangeAccountUpdate(
        asset="USDT",
        wallet_balance=9_999.75,
        available_balance=9_999.0,
        balances={
            "USDT": {
                "wallet_balance": 9_999.75,
                "available_balance": 9_999.0,
                "balance_change": -0.25,
            }
        },
        positions={
            "XAUUSDT": {
                "volume": 0.0,
                "entry_price": 0.0,
                "unrealized_pnl": 0.0,
            }
        },
        reason="FUNDING_FEE",
        event_time=1_700_000_001.0,
        received_timestamp=1_700_000_001.01,
        corrected_received_timestamp=1_700_000_001.012,
        clock_offset_ms=2.0,
    )

    payload = account_update_evidence_payload(update)

    assert payload["reason"] == "FUNDING_FEE"
    assert payload["balances"]["USDT"]["balance_change"] == -0.25
    assert payload["positions"]["XAUUSDT"]["volume"] == 0.0


def test_non_finite_payload_is_rejected():
    mark = MarkPriceData(
        symbol="XAUUSDT",
        mark_price=float("nan"),
        index_price=2400.0,
        funding_rate=0.0,
        next_funding_time=datetime.fromtimestamp(1_700_028_800.0),
        datetime=datetime.fromtimestamp(1_700_000_000.0),
        exchange_timestamp=1_700_000_000.0,
        received_timestamp=1_700_000_000.01,
        corrected_received_timestamp=1_700_000_000.012,
        next_funding_timestamp=1_700_028_800.0,
    )

    with pytest.raises(LiveEvidenceWriteError, match="mark_price must be finite"):
        mark_price_evidence_payload(mark)
