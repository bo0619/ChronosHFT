import gzip
import json

import pandas as pd

from scripts.research_books import (
    BinanceBook,
    GrvtBook,
    SortedSide,
    TableWriter,
    rebuild_binance,
    rebuild_grvt,
)


def diff(first, last, previous, bids=(), asks=(), symbol="KORUUSDT"):
    return {
        "s": symbol, "E": 1_000, "U": first, "u": last, "pu": previous,
        "b": [[str(p), str(q)] for p, q in bids], "a": [[str(p), str(q)] for p, q in asks],
    }


def snapshot(last_update_id, bids, asks):
    return {
        "lastUpdateId": last_update_id,
        "bids": [[str(p), str(q)] for p, q in bids],
        "asks": [[str(p), str(q)] for p, q in asks],
    }


def test_sorted_side_keeps_best_first_and_removes_zero_sizes():
    bids = SortedSide(descending=True)
    for price, size in ((10.0, 1.0), (10.2, 2.0), (10.1, 3.0)):
        bids.set(price, size)
    bids.set(10.2, 0.0)
    assert bids.top(5) == [(10.1, 3.0), (10.0, 1.0)]
    asks = SortedSide(descending=False)
    asks.set(11.0, 1.0)
    asks.set(10.9, 4.0)
    assert asks.top(1) == [(10.9, 4.0)]
    assert asks.best() == 10.9


def test_binance_book_syncs_on_the_diff_spanning_the_snapshot():
    book = BinanceBook()
    book.on_snapshot(0.0, snapshot(100, [(10.0, 5.0)], [(10.1, 5.0)]))
    assert not book.on_diff(0.1, diff(90, 99, 89))  # older than the snapshot
    assert book.on_diff(0.2, diff(95, 105, 99, bids=[(10.0, 4.0)]))
    assert book.on_diff(0.3, diff(106, 110, 105, asks=[(10.1, 0.0), (10.2, 1.0)]))
    assert book.top(1) == ([(10.0, 4.0)], [(10.2, 1.0)])
    assert book.stats.syncs == 1 and book.stats.gaps == 0


def test_binance_book_continues_from_a_snapshot_on_an_earlier_diff_u():
    book = BinanceBook()
    book.on_snapshot(0.0, snapshot(100, [(10.0, 5.0)], [(10.1, 5.0)]))
    # The next diff starts after the snapshot but chains to it through pu.
    assert book.on_diff(0.1, diff(101, 104, 100, bids=[(10.0, 1.0)]))
    assert book.synced and book.stats.gaps == 0 and book.stats.stale_snapshots == 0


def test_binance_book_unsyncs_on_a_broken_chain_and_resyncs_from_buffered_diffs():
    book = BinanceBook()
    book.on_snapshot(0.0, snapshot(100, [(10.0, 5.0)], [(10.1, 5.0)]))
    assert book.on_diff(0.1, diff(95, 105, 99))
    assert not book.on_diff(0.2, diff(120, 130, 115))  # pu 115 != 105: missed diffs
    assert not book.synced and book.stats.gaps == 1
    # A diff spanning the next snapshot arrives before the snapshot itself.
    assert not book.on_diff(0.3, diff(131, 140, 130, bids=[(9.9, 2.0)]))
    replayed = book.on_snapshot(0.4, snapshot(135, [(10.0, 3.0)], [(10.1, 3.0)]))
    assert [event["u"] for event in replayed] == [140]
    assert book.synced and book.last_u == 140
    assert book.top(2)[0] == [(10.0, 3.0), (9.9, 2.0)]


def test_binance_book_counts_a_snapshot_older_than_the_stream_as_stale():
    book = BinanceBook()
    book.on_snapshot(0.0, snapshot(100, [(10.0, 5.0)], [(10.1, 5.0)]))
    assert not book.on_diff(0.1, diff(150, 160, 149))
    assert book.stats.stale_snapshots == 1 and book.stats.gaps == 0


def grvt_book_message(sequence, previous, bids=(), asks=()):
    return {
        "stream": "v1.book.d", "sequence_number": str(sequence), "prev_sequence_number": str(previous),
        "feed": {
            "instrument": "KORU_USDT_Perp", "event_time": "1000000000",
            "bids": [{"price": str(p), "size": str(q)} for p, q in bids],
            "asks": [{"price": str(p), "size": str(q)} for p, q in asks],
        },
    }


def test_grvt_book_chains_deltas_from_the_ack_sequence():
    book = GrvtBook()
    book.on_ack(500)
    assert book.on_message(grvt_book_message(0, 0, bids=[(10.0, 1.0)], asks=[(10.1, 1.0)]))
    assert book.on_message(grvt_book_message(501, 500, bids=[(10.0, 0.0), (9.9, 2.0)]))
    assert book.top(1) == ([(9.9, 2.0)], [(10.1, 1.0)])
    assert not book.on_message(grvt_book_message(510, 505))
    assert book.stats.gaps == 1 and not book.synced


def write_raw(path, records):
    with gzip.open(path, "wt") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def test_rebuild_binance_checks_depth5_and_rpi_and_writes_tables(tmp_path):
    depth5 = {"s": "KORUUSDT", "E": 2_000, "U": 101, "u": 110, "pu": 100,
              "b": [["10.0", "4"]], "a": [["10.1", "5"]]}
    records = [
        {"r": 1.0, "rest": "depth", "s": "KORUUSDT", "m": snapshot(100, [(10.0, 5.0)], [(10.1, 5.0)])},
        {"r": 1.0, "rest": "rpiDepth", "s": "KORUUSDT",
         "m": snapshot(100, [(10.0, 7.0)], [(10.1, 5.0)])},
        {"r": 1.1, "m": {"stream": "koruusdt@depth5@100ms", "data": depth5}},
        {"r": 1.2, "m": {"stream": "koruusdt@depth@100ms", "data": diff(101, 110, 100, bids=[(10.0, 4.0)])}},
        {"r": 1.3, "m": {"stream": "koruusdt@rpiDepth@500ms",
                         "data": diff(101, 110, 100, bids=[(10.0, 6.0)])}},
        {"r": 1.4, "m": {"stream": "koruusdt@bookTicker", "data": {"s": "KORUUSDT"}}},
        {"r": 1.5, "m": {"stream": "koruusdt@markPrice@1s", "data": {
            "s": "KORUUSDT", "E": 1_500, "p": "10.05", "i": "10.04", "r": "0.0001", "T": 3_600_000}}},
    ]
    write_raw(tmp_path / "raw_19700101_00.jsonl.gz", records)
    out = tmp_path / "rebuilt"
    writer = TableWriter(out)
    result = rebuild_binance(tmp_path, 3, writer)
    writer.close()
    check = result["checks"]["KORUUSDT"]
    assert (check.depth5_compared, check.depth5_matched) == (1, 1)
    assert check.rpi_compared == 1 and check.rpi_levels_below_full == 0
    rpi = pd.read_hdf(out / "KORUUSDT_rpibook3_19700101.h5", "rpibook")
    assert rpi.loc[0, "bid1_v"] == 6.0 and rpi.loc[0, "bid1_full_v"] == 4.0
    assert rpi.loc[0, "full_update_id"] == rpi.loc[0, "update_id"] == 110
    book = pd.read_hdf(out / "KORUUSDT_book3_19700101.h5", "book")
    assert list(book[["bid1_p", "bid1_v", "ask1_p"]].iloc[0]) == [10.0, 4.0, 10.1]
    mark = pd.read_hdf(out / "KORUUSDT_mark_19700101.h5", "mark")
    assert mark.loc[0, "funding_rate"] == 0.0001


def test_rebuild_grvt_reads_ack_snapshot_deltas_and_ticker(tmp_path):
    records = [
        {"r": 1.0, "m": {"jsonrpc": "2.0", "method": "subscribe", "result": {
            "stream": "v1.book.d", "subs": ["KORU_USDT_Perp@50"], "latest_sequence_number": ["500"]}}},
        {"r": 1.1, "m": grvt_book_message(0, 0, bids=[(10.0, 1.0)], asks=[(10.1, 1.0)])},
        {"r": 1.2, "m": grvt_book_message(501, 500, bids=[(10.0, 3.0)])},
        {"r": 1.3, "m": {"stream": "v1.ticker.s", "feed": {
            "instrument": "KORU_USDT_Perp", "event_time": "1300000000", "mark_price": "10.05",
            "index_price": "10.04", "funding_rate": "0.01", "next_funding_time": "0"}}},
    ]
    write_raw(tmp_path / "raw_19700101_00.jsonl.gz", records)
    writer = TableWriter(tmp_path / "rebuilt")
    result = rebuild_grvt(tmp_path, 2, writer)
    writer.close()
    assert result["full"]["KORUUSDT"].stats.gaps == 0
    book = pd.read_hdf(tmp_path / "rebuilt" / "KORUUSDT_book2_19700101.h5", "book")
    assert list(book["bid1_v"]) == [1.0, 3.0]
    mark = pd.read_hdf(tmp_path / "rebuilt" / "KORUUSDT_mark_19700101.h5", "mark")
    assert abs(mark.loc[0, "funding_rate"] - 0.0001) < 1e-12  # percentage points to a fraction
