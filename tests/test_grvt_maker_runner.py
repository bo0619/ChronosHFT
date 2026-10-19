"""Feeds, private-feed parsing and config checks of the live GRVT maker (no network)."""

import json
import os

import pytest

from gateway.binance.book_ticker_feed import parse_book_ticker
from gateway.grvt.book_feed import GrvtBookBuilder
from scripts import run_grvt_maker as runner

INSTRUMENT = "XRP_USDT_Perp"


def book_message(sequence, previous, bids=(), asks=()):
    return {"stream": "v1.book.d", "sequence_number": str(sequence), "prev_sequence_number": str(previous),
            "feed": {"instrument": INSTRUMENT, "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
                     "asks": [{"price": str(p), "size": str(s)} for p, s in asks]}}


def test_book_builder_applies_snapshot_and_deltas_and_reports_gaps():
    builder = GrvtBookBuilder(INSTRUMENT)
    ack = {"result": {"stream": "v1.book.d", "subs": [f"{INSTRUMENT}@50"], "latest_sequence_number": ["10"]}}
    assert builder.apply(ack) == ""
    assert builder.apply(book_message(11, 10, bids=[(1.30, 5)])) == ""  # before the snapshot: ignored
    assert builder.apply(book_message(0, 0, bids=[(1.30, 5), (1.29, 2)], asks=[(1.31, 4)])) == "book"
    assert builder.apply(book_message(11, 10, bids=[(1.30, 0), (1.295, 1)])) == "book"
    assert builder.top(2) == ({1.295: 1.0, 1.29: 2.0}, {1.31: 4.0})
    assert builder.apply(book_message(13, 12, asks=[(1.31, 1)])) == "gap"
    assert builder.top(5) == ({}, {})


def test_book_builder_ignores_other_instruments():
    builder = GrvtBookBuilder(INSTRUMENT)
    message = book_message(0, 0, bids=[(1.0, 1)])
    message["feed"]["instrument"] = "SUI_USDT_Perp"
    assert builder.apply(message) == ""


def test_book_ticker_parse():
    raw = json.dumps({"e": "bookTicker", "E": 1700000000123, "T": 1700000000120, "s": "XRPUSDT",
                      "b": "1.3068", "B": "100", "a": "1.3069", "A": "50"})
    assert parse_book_ticker(raw) == (1.3068, 1.3069, 1700000000123.0)
    assert parse_book_ticker(json.dumps({"result": None, "id": 1})) is None


def test_private_events_parse_state_order_and_fill():
    state = {"order_id": "0x1", "client_order_id": "9", "order_state": {"status": "CANCELLED", "reject_reason": "CLIENT_CANCEL"}}
    assert runner.private_events("v1.state", state, INSTRUMENT) == [("state", "9", "CANCELLED", "0x1", "CLIENT_CANCEL")]
    order = {"order_id": "0x2", "legs": [{"instrument": INSTRUMENT}], "metadata": {"client_order_id": "8"},
             "state": {"status": "REJECTED", "reject_reason": "FAIL_POST_ONLY"}}
    assert runner.private_events("v1.order", order, INSTRUMENT) == [("state", "8", "REJECTED", "0x2", "FAIL_POST_ONLY")]
    fill = {"instrument": INSTRUMENT, "trade_id": "t1", "client_order_id": "7", "is_buyer": True, "size": "76.5",
            "price": "1.3068", "fee": "-0.000001", "is_taker": False, "fee_rate": "-1"}
    [(kind, call)] = runner.private_events("v1.fill", fill, INSTRUMENT)
    assert kind == "fill" and call["size"] == 76.5 and call["is_buyer"] and not call["is_taker"]
    assert runner.private_events("v1.fill", {**fill, "instrument": "SUI_USDT_Perp"}, INSTRUMENT) == []


def test_config_merges_defaults_and_rejects_unknown_settings(tmp_path):
    config = {"defaults": {"quote": {"notional": 100.0}, "risk": {"max_position_usd": 500.0,
                                                                  "daily_loss_reduce_usd": 10.0,
                                                                  "daily_loss_halt_usd": 20.0}},
              "symbols": {"XRPUSDT": {"quote": {"notional": 500.0, "max_inventory_lots": 1.0}}}}
    path = tmp_path / "maker.json"
    path.write_text(json.dumps(config))
    loaded = runner.load_config(str(path), "XRPUSDT")
    assert loaded["quote"].notional == 500.0 and loaded["quote"].max_inventory_lots == 1.0
    assert loaded["risk"].max_position_usd == 500.0
    with pytest.raises(SystemExit):
        runner.load_config(str(path), "SUIUSDT")
    config["symbols"]["XRPUSDT"]["quote"]["half_sprad_bps"] = 1.0
    path.write_text(json.dumps(config))
    with pytest.raises(SystemExit):
        runner.load_config(str(path), "XRPUSDT")


def test_secrets_must_be_private_and_complete(tmp_path):
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"api_key": "k", "sub_account_id": "1", "private_key": "0xabc"}))
    os.chmod(path, 0o644)
    with pytest.raises(SystemExit):
        runner.load_secrets(str(path))
    os.chmod(path, 0o600)
    assert runner.load_secrets(str(path))["api_key"] == "k"
    path.write_text(json.dumps({"api_key": "k", "sub_account_id": "", "private_key": "0xabc"}))
    with pytest.raises(SystemExit):
        runner.load_secrets(str(path))


def test_decimals_for_price_and_size_strings():
    assert runner.decimals(0.0001) == 4 and runner.decimals(0.1) == 1 and runner.decimals(1.0) == 0
    assert runner.decimals(0.000001) == 6
