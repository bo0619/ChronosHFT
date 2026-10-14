"""Read research recordings and rebuild full order books from raw messages.

``record_public_book.py --all-streams`` and ``record_grvt_book.py
--all-streams`` keep every message in ``raw_<YYYYMMDD>_<HH>.jsonl.gz``. This
module turns those archives into books:

* Binance full depth, from REST ``/fapi/v1/depth`` snapshots and
  ``@depth@100ms`` diffs;
* the Binance RPI book, from REST ``/fapi/v1/rpiDepth`` snapshots and
  ``@rpiDepth@500ms`` diffs. It holds normal and RPI orders together;
  ``RPI-only = RPI book - full book`` at a price. Binance hides crossed RPI
  levels, so they are missing from both;
* GRVT full depth, from the ``v1.book.d`` snapshot and deltas.

Binance diff books follow the venue's rule: drop diffs with ``u`` below the
snapshot's ``lastUpdateId``, the first applied diff must span it
(``U <= lastUpdateId <= u``), and every later diff must continue the chain
(``pu`` equals the previous ``u``). A broken chain leaves the book unsynced
until the next snapshot, which the recorder fetches every minute; diffs from
the last few seconds are kept so a snapshot that lands after the diff
spanning it still syncs. GRVT books follow ``sequence_number`` /
``prev_sequence_number`` the same way, from the snapshot at the subscribe
acknowledgement's ``latest_sequence_number``.

Checks printed per symbol: chain gaps and resyncs, and, for Binance, the
rebuilt full book's top five against every ``@depth5@100ms`` message with
the same ``u`` (an exact comparison), and RPI-book levels below the full
book at the same ``u`` (impossible if both are right).

The CLI writes, per symbol, into ``<dir>/rebuilt/``:

* ``<SYMBOL>_book<N>_<YYYYMMDD>.h5`` key ``book``: ``received_ts``,
  ``exchange_ts``, ``update_id`` and ``bid1_p``, ``bid1_v`` ... ``askN_v``
  after every applied full-depth update;
* ``<SYMBOL>_rpibook<N>_<YYYYMMDD>.h5`` key ``rpibook`` (Binance): the same
  for the RPI book, plus ``bidK_full_v`` / ``askK_full_v``, the latest full
  book's size at each RPI-book price, and ``full_update_id``. The two books
  share update ids but publish on different cadences (500 vs 100 ms), so
  they sit at the same ``u`` only about one row in nine
  (``full_update_id == update_id``, exact); otherwise the full book is up to
  100 ms older and ``RPI-only = v - full_v`` is approximate;
* ``<SYMBOL>_mark_<YYYYMMDD>.h5`` key ``mark``: ``received_ts``,
  ``exchange_ts``, ``mark_price``, ``index_price``, ``funding_rate``
  (a fraction, not percent).

Example::

    python scripts/research_books.py --venue binance --dir storage/research/binance --levels 20
    python scripts/research_books.py --venue grvt --dir storage/research/grvt --levels 20
"""

from __future__ import annotations

import argparse
import bisect
import glob
import gzip
import json
import sys
from collections import defaultdict, deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# Diffs kept per book for a snapshot that arrives after the diff spanning it.
BUFFER_SEC = 5.0
FLUSH_ROWS = 50_000


def iter_raw(directory: str | Path, start: float | None = None,
             end: float | None = None, *, skip: tuple[str, ...] = ()) -> Iterator[dict]:
    """Raw records in file order (hourly files, arrival order within a file).

    ``skip`` drops lines containing any of the given substrings before JSON
    parsing (e.g. ``("@bookTicker",)``), which saves most of the time on
    Binance archives.
    """
    for path in sorted(glob.glob(str(Path(directory) / "raw_*.jsonl.gz"))):
        with gzip.open(path, "rt") as handle:
            for line in handle:
                if skip and any(token in line for token in skip):
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a line cut short by an interrupted write
                received = float(record.get("r", 0.0))
                if start is not None and received < start:
                    continue
                if end is not None and received >= end:
                    return
                yield record


class SortedSide:
    """One side of a book: price -> size, with prices kept sorted."""

    def __init__(self, descending: bool) -> None:
        self.descending = descending
        self.sizes: dict[float, float] = {}
        self.prices: list[float] = []  # ascending

    def clear(self) -> None:
        self.sizes.clear()
        self.prices.clear()

    def set(self, price: float, size: float) -> None:
        if size <= 0.0:
            if self.sizes.pop(price, None) is not None:
                index = bisect.bisect_left(self.prices, price)
                del self.prices[index]
            return
        if price not in self.sizes:
            bisect.insort(self.prices, price)
        self.sizes[price] = size

    def top(self, levels: int) -> list[tuple[float, float]]:
        prices = self.prices[-levels:][::-1] if self.descending else self.prices[:levels]
        return [(price, self.sizes[price]) for price in prices]

    def best(self) -> float | None:
        if not self.prices:
            return None
        return self.prices[-1] if self.descending else self.prices[0]


@dataclass
class BookStats:
    snapshots: int = 0
    syncs: int = 0
    gaps: int = 0
    stale_snapshots: int = 0
    applied: int = 0
    skipped_unsynced: int = 0


@dataclass
class DiffBook:
    """A Binance diff-depth book (full or RPI) or a GRVT delta book."""

    bids: SortedSide = field(default_factory=lambda: SortedSide(descending=True))
    asks: SortedSide = field(default_factory=lambda: SortedSide(descending=False))
    last_u: int | None = None
    synced: bool = False
    awaiting_first: bool = False
    stats: BookStats = field(default_factory=BookStats)
    recent: deque = field(default_factory=deque)

    def load(self, bids, asks, last_update_id: int | None) -> None:
        self.bids.clear()
        self.asks.clear()
        for price, size in bids:
            self.bids.set(float(price), float(size))
        for price, size in asks:
            self.asks.set(float(price), float(size))
        self.last_u = last_update_id
        self.synced = True
        self.stats.snapshots += 1

    def apply_levels(self, bids, asks) -> None:
        for price, size in bids:
            self.bids.set(float(price), float(size))
        for price, size in asks:
            self.asks.set(float(price), float(size))

    def top(self, levels: int) -> tuple[list, list]:
        return self.bids.top(levels), self.asks.top(levels)


class BinanceBook(DiffBook):
    """Binance diff book: REST snapshots plus ``U``/``u``/``pu`` diffs."""

    def on_snapshot(self, received: float, body: dict) -> list[dict]:
        """Load a snapshot when unsynced; returns buffered diffs it applied."""
        last_update_id = int(body["lastUpdateId"])
        if self.synced:
            return []
        self.load(body.get("bids") or [], body.get("asks") or [], last_update_id)
        self.awaiting_first = True
        replayed = []
        for event_received, data in list(self.recent):
            if int(data["u"]) >= last_update_id and self._apply(data):
                replayed.append(data)
        return replayed

    def on_diff(self, received: float, data: dict) -> bool:
        self.recent.append((received, data))
        while self.recent and self.recent[0][0] < received - BUFFER_SEC:
            self.recent.popleft()
        if not self.synced:
            self.stats.skipped_unsynced += 1
            return False
        return self._apply(data)

    def _apply(self, data: dict) -> bool:
        first, last, previous = int(data["U"]), int(data["u"]), int(data.get("pu", -1))
        if self.last_u is not None and last <= self.last_u:
            return False  # already covered by the snapshot or an earlier diff
        if self.awaiting_first:
            # The first diff spans the snapshot, or starts right after a
            # snapshot that sits exactly on an earlier diff's ``u``.
            if not (first <= self.last_u <= last or previous == self.last_u):
                self.synced = False
                if first > self.last_u:
                    self.stats.stale_snapshots += 1  # snapshot older than the stream
                else:
                    self.stats.gaps += 1
                return False
            self.awaiting_first = False
            self.stats.syncs += 1
        elif previous != self.last_u:
            self._unsync()
            return False
        self.apply_levels(data.get("b") or [], data.get("a") or [])
        self.last_u = last
        self.stats.applied += 1
        return True

    def _unsync(self) -> None:
        self.synced = False
        self.stats.gaps += 1


class GrvtBook(DiffBook):
    """GRVT ``v1.book.d`` book: snapshot (sequence 0) plus delta chain."""

    def __init__(self) -> None:
        super().__init__()
        self.snapshot_sequence: int | None = None

    def on_ack(self, sequence: int) -> None:
        self.snapshot_sequence = sequence

    def on_message(self, message: dict) -> bool:
        feed = message["feed"]
        sequence = int(message.get("sequence_number", 0) or 0)
        levels = {
            side: [(level["price"], level["size"]) for level in feed.get(side) or []]
            for side in ("bids", "asks")
        }
        if sequence == 0:
            self.load(levels["bids"], levels["asks"], self.snapshot_sequence)
            self.snapshot_sequence = None
            self.stats.syncs += 1
            return True
        if not self.synced:
            self.stats.skipped_unsynced += 1
            return False
        previous = int(message.get("prev_sequence_number", 0) or 0)
        if self.last_u is not None and previous != self.last_u:
            self.synced = False
            self.stats.gaps += 1
            return False
        self.apply_levels(levels["bids"], levels["asks"])
        self.last_u = sequence
        self.stats.applied += 1
        return True


class TableWriter:
    """Buffered per-symbol HDF5 output, one file per symbol, kind and day."""

    def __init__(self, out: Path) -> None:
        self.out = out
        self.rows: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        self.counts: dict[tuple[str, str], int] = defaultdict(int)

    def add(self, symbol: str, kind: str, key: str, row: dict) -> None:
        day = datetime.fromtimestamp(row["received_ts"], tz=timezone.utc).strftime("%Y%m%d")
        bucket = self.rows[(symbol, kind, key, day)]
        bucket.append(row)
        self.counts[(symbol, kind)] += 1
        if len(bucket) >= FLUSH_ROWS:
            self._write(symbol, kind, key, day)

    def _write(self, symbol: str, kind: str, key: str, day: str) -> None:
        rows = self.rows.pop((symbol, kind, key, day), [])
        if rows:
            self.out.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(rows).to_hdf(self.out / f"{symbol}_{kind}_{day}.h5", key=key, mode="a",
                                      format="table", append=True, complevel=5, complib="blosc")

    def close(self) -> None:
        for symbol, kind, key, day in list(self.rows):
            self._write(symbol, kind, key, day)


def book_row(received: float, exchange: float, update_id: int, top: tuple[list, list],
             levels: int) -> dict:
    row = {"received_ts": received, "exchange_ts": exchange, "update_id": update_id}
    for side, entries in zip(("bid", "ask"), top):
        for index in range(levels):
            price, size = entries[index] if index < len(entries) else (0.0, 0.0)
            row[f"{side}{index + 1}_p"] = price
            row[f"{side}{index + 1}_v"] = size
    return row


@dataclass
class BinanceChecks:
    depth5_compared: int = 0
    depth5_matched: int = 0
    rpi_compared: int = 0
    rpi_levels_below_full: int = 0


def rebuild_binance(directory: str | Path, levels: int, writer: TableWriter | None,
                    start: float | None = None, end: float | None = None) -> dict:
    full: dict[str, BinanceBook] = defaultdict(BinanceBook)
    rpi: dict[str, BinanceBook] = defaultdict(BinanceBook)
    checks: dict[str, BinanceChecks] = defaultdict(BinanceChecks)
    pending_depth5: dict[str, dict[int, dict]] = defaultdict(dict)

    def emit_full(symbol: str, received: float, data: dict) -> None:
        book = full[symbol]
        expected = pending_depth5[symbol].pop(book.last_u, None)
        if expected is not None:
            compare_depth5(symbol, book, expected)
        if writer is not None:
            writer.add(symbol, f"book{levels}", "book",
                       book_row(received, float(data.get("E", 0)) / 1000.0, book.last_u,
                                book.top(levels), levels))

    def compare_depth5(symbol: str, book: BinanceBook, data: dict) -> None:
        check = checks[symbol]
        check.depth5_compared += 1
        bids, asks = book.top(5)
        want_bids = [(float(p), float(q)) for p, q in data.get("b") or []][:5]
        want_asks = [(float(p), float(q)) for p, q in data.get("a") or []][:5]
        if bids == want_bids and asks == want_asks:
            check.depth5_matched += 1

    def emit_rpi(symbol: str, received: float, data: dict) -> None:
        book, normal = rpi[symbol], full[symbol]
        same_u = normal.synced and normal.last_u == book.last_u
        top = book.top(levels)
        if same_u:
            check = checks[symbol]
            check.rpi_compared += 1
            for side, entries in zip((normal.bids, normal.asks), top):
                check.rpi_levels_below_full += sum(
                    1 for price, size in entries if size < side.sizes.get(price, 0.0) - 1e-9
                )
        if writer is None:
            return
        row = book_row(received, float(data.get("E", 0)) / 1000.0, book.last_u, top, levels)
        row["full_update_id"] = normal.last_u if normal.last_u is not None else -1
        for side_name, entries, side in zip(("bid", "ask"), top, (normal.bids, normal.asks)):
            for index in range(levels):
                price = entries[index][0] if index < len(entries) else 0.0
                row[f"{side_name}{index + 1}_full_v"] = side.sizes.get(price, 0.0) if normal.synced else float("nan")
        writer.add(symbol, f"rpibook{levels}", "rpibook", row)

    skip = ("@bookTicker", "@aggTrade")
    for record in iter_raw(directory, start, end, skip=skip):
        received = float(record["r"])
        if "rest" in record:
            symbol = record["s"]
            target = full if record["rest"] == "depth" else rpi
            book = target[symbol]
            for data in book.on_snapshot(received, record["m"]):
                (emit_full if target is full else emit_rpi)(symbol, received, data)
            continue
        message = record["m"]
        stream = str(message.get("stream", ""))
        data = message.get("data") or {}
        symbol = str(data.get("s", "")).upper()
        if "@depth5" in stream:
            book = full[symbol]
            update = int(data["u"])
            if book.synced and book.last_u == update:
                compare_depth5(symbol, book, data)
            elif book.last_u is None or update > book.last_u:
                pending_depth5[symbol][update] = data
                if len(pending_depth5[symbol]) > 100:
                    pending_depth5[symbol].pop(next(iter(pending_depth5[symbol])))
        elif "@depth@" in stream:
            if full[symbol].on_diff(received, data):
                emit_full(symbol, received, data)
        elif "@rpiDepth" in stream:
            if rpi[symbol].on_diff(received, data):
                emit_rpi(symbol, received, data)
        elif "@markPrice" in stream and writer is not None:
            writer.add(symbol, "mark", "mark", {
                "received_ts": received,
                "exchange_ts": float(data["E"]) / 1000.0,
                "mark_price": float(data["p"]),
                "index_price": float(data.get("i") or 0.0),
                "funding_rate": float(data.get("r") or 0.0),
                "next_funding_ts": float(data.get("T") or 0) / 1000.0,
            })
    return {"full": full, "rpi": rpi, "checks": checks}


def rebuild_grvt(directory: str | Path, levels: int, writer: TableWriter | None,
                 start: float | None = None, end: float | None = None) -> dict:
    books: dict[str, GrvtBook] = defaultdict(GrvtBook)

    def symbol_of(instrument: str) -> str:
        parts = instrument.split("_")
        return f"{parts[0]}{parts[1]}".upper() if len(parts) >= 2 else instrument.upper()

    for record in iter_raw(directory, start, end):
        received = float(record["r"])
        message = record.get("m") or {}
        result = message.get("result")
        if isinstance(result, dict) and result.get("stream") == "v1.book.d":
            for selector, sequence in zip(result.get("subs") or [], result.get("latest_sequence_number") or []):
                books[symbol_of(selector.partition("@")[0])].on_ack(int(sequence))
            continue
        feed = message.get("feed")
        if not isinstance(feed, dict):
            continue
        symbol = symbol_of(str(feed.get("instrument", "")))
        stream = message.get("stream")
        exchange = int(feed.get("event_time") or 0) / 1e9
        if stream == "v1.book.d":
            book = books[symbol]
            if book.on_message(message) and writer is not None:
                writer.add(symbol, f"book{levels}", "book",
                           book_row(received, exchange, book.last_u or 0, book.top(levels), levels))
        elif stream == "v1.ticker.s" and writer is not None:
            funding = feed.get("funding_rate")
            if funding in (None, "", {}):
                funding = feed.get("funding_rate_8h_curr")
            writer.add(symbol, "mark", "mark", {
                "received_ts": received,
                "exchange_ts": exchange,
                "mark_price": float(feed.get("mark_price") or 0.0),
                "index_price": float(feed.get("index_price") or 0.0),
                "funding_rate": float(funding or 0.0) / 100.0,  # GRVT sends percentage points
                "next_funding_ts": int(feed.get("next_funding_time") or 0) / 1e9,
            })
    return {"full": books}


def report(venue: str, result: dict, writer: TableWriter | None) -> None:
    for symbol in sorted(result["full"]):
        stats = result["full"][symbol].stats
        line = (f"{symbol:<12} full: snapshots {stats.snapshots} syncs {stats.syncs} gaps {stats.gaps} "
                f"stale-snapshots {stats.stale_snapshots} applied {stats.applied:,} "
                f"skipped-unsynced {stats.skipped_unsynced:,}")
        if venue == "binance":
            rpi = result["rpi"][symbol].stats
            check = result["checks"][symbol]
            matched = (f"{check.depth5_matched}/{check.depth5_compared}"
                       f" ({check.depth5_matched / check.depth5_compared:.1%})" if check.depth5_compared else "n/a")
            line += (f"\n{'':<12} rpi:  snapshots {rpi.snapshots} syncs {rpi.syncs} gaps {rpi.gaps} "
                     f"stale-snapshots {rpi.stale_snapshots} applied {rpi.applied:,} "
                     f"skipped-unsynced {rpi.skipped_unsynced:,}\n{'':<12} check: depth5 top-5 exact match {matched}; "
                     f"RPI vs full at the same u: {check.rpi_compared} books, "
                     f"{check.rpi_levels_below_full} levels below full")
        print(line)
    if writer is not None:
        for (symbol, kind), count in sorted(writer.counts.items()):
            print(f"  wrote {symbol}_{kind}: {count:,} rows")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--venue", required=True, choices=("binance", "grvt"))
    parser.add_argument("--dir", required=True, help="Recording directory holding raw_*.jsonl.gz")
    parser.add_argument("--out", default="", help="Output directory (default: <dir>/rebuilt)")
    parser.add_argument("--levels", type=int, default=20)
    parser.add_argument("--start", default="", help="UTC, e.g. 2026-10-14T16:00")
    parser.add_argument("--end", default="")
    parser.add_argument("--check-only", action="store_true", help="Rebuild and check without writing files")
    args = parser.parse_args(argv)
    start = pd.Timestamp(args.start, tz="UTC").timestamp() if args.start else None
    end = pd.Timestamp(args.end, tz="UTC").timestamp() if args.end else None
    if not glob.glob(str(Path(args.dir) / "raw_*.jsonl.gz")):
        print(f"no raw_*.jsonl.gz in {args.dir}", file=sys.stderr)
        return 1
    writer = None if args.check_only else TableWriter(Path(args.out or Path(args.dir) / "rebuilt"))
    rebuild = rebuild_binance if args.venue == "binance" else rebuild_grvt
    result = rebuild(args.dir, args.levels, writer, start, end)
    if writer is not None:
        writer.close()
    report(args.venue, result, writer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
