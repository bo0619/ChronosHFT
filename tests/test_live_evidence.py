from datetime import datetime
import io
from types import SimpleNamespace
import threading
from unittest.mock import patch

import pytest

import data.live_evidence as live_evidence_module
from data.live_evidence import (
    LiveEvidenceRecorder,
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


def test_live_evidence_rejects_batch_before_advancing_hash_chain(tmp_path):
    recorder = object.__new__(LiveEvidenceRecorder)
    recorder.path = tmp_path / "market_evidence.jsonl"
    recorder._handle = io.StringIO()
    recorder._lock = threading.RLock()
    recorder._next_seq = 1
    recorder._last_hash = ""
    recorder._committed_seq = 0
    recorder._last_record_monotonic = 0.0
    recorder._last_fsync_monotonic = 0.0
    recorder._created_new_file = False
    recorder.min_free_bytes = 512 * 1024 * 1024
    recorder._disk_free_bytes = None
    recorder._last_space_check_monotonic = 0.0
    recorder._disk_space_rejections = 0
    recorder._disk_space_check_failures = 0

    with patch.object(
        live_evidence_module.shutil,
        "disk_usage",
        return_value=SimpleNamespace(free=recorder.min_free_bytes),
    ):
        with pytest.raises(
            LiveEvidenceWriteError,
            match="disk_reserve_exhausted",
        ):
            recorder._write_records(
                [("session_start", {"schema": "test"})],
                force_fsync=False,
            )

    assert recorder._handle.getvalue() == ""
    assert recorder._next_seq == 1
    assert recorder._last_hash == ""
    assert recorder._committed_seq == 0
    assert recorder._disk_space_rejections == 1
