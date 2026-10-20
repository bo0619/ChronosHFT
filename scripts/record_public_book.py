"""Record public Binance USD-M books and trades for offline research.

Subscribes to ``<symbol>@depth5@100ms`` (top-5 snapshots),
``<symbol>@bookTicker`` (best bid and ask, pushed on every change rather
than batched every 100 ms) and ``<symbol>@aggTrade`` for any symbols, with
no key, OMS or strategy, and appends them to compressed HDF5 files. Books
and trades use the column layout of ``data/recorder.py``, so the same
tools read both:

* ``<out>/<SYMBOL>_depth_<YYYYMMDD>_v2.h5`` key ``depth``: ``received_ts``,
  ``exchange_ts``, ``bid1_p``, ``bid1_v`` ... ``ask5_p``, ``ask5_v``;
* ``<out>/<SYMBOL>_trade_<YYYYMMDD>_v2.h5`` key ``trade``: ``received_ts``,
  ``exchange_ts``, ``trade_id``, ``price``, ``qty``, ``maker_is_buyer``;
* ``<out>/<SYMBOL>_ticker_<YYYYMMDD>_v2.h5`` key ``ticker``: ``received_ts``,
  ``exchange_ts``, ``update_id``, ``bid_p``, ``bid_v``, ``ask_p``, ``ask_v``.

bookTicker is by far the heaviest stream (BTCUSDT and ETHUSDT push about
1,000 updates a second, mostly size changes at an unchanged price).
``--ticker-price-changes-only`` keeps an update only when the best bid or
ask price changes: the exact timing of price moves, with sizes still in the
100 ms depth rows.

``--ticker-only`` drops depth5 and aggTrade and records bookTicker alone
(what the cross-venue replay and the toxicity study read), and
``--connection-per-symbol`` opens one websocket per symbol inside this one
process: hot symbols sharing a connection were measured to lag by
hundreds of milliseconds, and a process per symbol costs ~130 MB each.

``--all-streams`` records everything public on top: full-depth diffs
(``@depth@100ms``), the RPI book (``@rpiDepth@500ms``, which includes
resting RPI orders but hides crossed RPI levels), and mark price, index and
funding (``@markPrice@1s``), plus REST snapshots of the full book
(``/fapi/v1/depth``, limit 1000) and of the RPI book
(``/fapi/v1/rpiDepth``) every ``--snapshot-sec`` to anchor offline book
rebuilds. With it, every websocket message and REST snapshot is also kept
verbatim in ``<out>/raw_<YYYYMMDD>_<HH>.jsonl.gz``, one JSON object per line:
``{"r": received_ts, "m": <message>}`` or
``{"r": received_ts, "rest": "depth"|"rpiDepth", "s": symbol, "m": <body>}``.

Book and bookTicker streams are served under ``/public`` and trade streams under
``/market`` (as in ``gateway/binance/ws_api.py``); the plain ``/stream``
path no longer carries aggTrade. Recording stops by itself after
``--hours`` or when free disk falls below ``--min-free-gb``. Reconnects
after a dropped connection.

Example::

    python scripts/record_public_book.py --symbols ARCUSDT,ZKUSDT --hours 12
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import signal
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import websocket

WS_ROOT = "wss://fstream.binance.com"
REST_ROOT = "https://fapi.binance.com"
LEVELS = 5
FLUSH_SEC = 10.0


def report_ws_error(_ws, error) -> None:
    print(f"ws error: {error}", file=sys.stderr, flush=True)


class Recorder:
    def __init__(self, symbols: list[str], out: Path, hours: float, min_free_gb: float,
                 *, ticker_price_changes_only: bool = False, all_streams: bool = False,
                 snapshot_sec: float = 60.0, ticker_only: bool = False,
                 connection_per_symbol: bool = False) -> None:
        if ticker_only and all_streams:
            raise ValueError("--ticker-only and --all-streams exclude each other")
        self.ticker_price_changes_only = ticker_price_changes_only
        self.ticker_only = ticker_only
        self.connection_per_symbol = connection_per_symbol
        self.all_streams = all_streams
        self.snapshot_sec = snapshot_sec
        self.raw_lines: list[str] = []
        self.last_touch: dict[str, tuple[float, float]] = {}
        self.symbols = [s.upper() for s in symbols]
        self.out = out
        self.deadline = time.time() + hours * 3600.0
        self.min_free = min_free_gb * 1e9
        self.depth: dict[str, list[dict]] = defaultdict(list)
        self.trades: dict[str, list[dict]] = defaultdict(list)
        self.tickers: dict[str, list[dict]] = defaultdict(list)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.rows = 0

    def urls(self) -> list[str]:
        lower = [symbol.lower() for symbol in self.symbols]
        if self.ticker_only:
            groups = [[s] for s in lower] if self.connection_per_symbol else [lower]
            return [f"{WS_ROOT}/public/stream?streams=" + "/".join(f"{s}@bookTicker" for s in group)
                    for group in groups]
        if self.connection_per_symbol:
            return [url for s in lower for url in self._urls_for([s])]
        return self._urls_for(lower)

    def _urls_for(self, lower: list[str]) -> list[str]:
        return [
            f"{WS_ROOT}/public/stream?streams=" + "/".join(
                f"{s}@depth5@100ms/{s}@bookTicker"
                + (f"/{s}@depth@100ms/{s}@rpiDepth@500ms" if self.all_streams else "")
                for s in lower
            ),
            f"{WS_ROOT}/market/stream?streams=" + "/".join(
                f"{s}@aggTrade" + (f"/{s}@markPrice@1s" if self.all_streams else "")
                for s in lower
            ),
        ]

    def keep_raw(self, received: float, raw: str) -> None:
        if self.all_streams:
            line = f'{{"r":{received:.6f},"m":{raw}}}'
            with self.lock:
                self.raw_lines.append(line)

    def snapshot_loop(self) -> None:
        """REST full-book and RPI-book snapshots (weight 20 each per symbol)."""
        session = requests.Session()
        # The first snapshot must not predate the diff streams, or the first
        # diff cannot be chained to it; let the websockets connect first.
        self.stop.wait(3.0)
        while not self.stop.is_set() and time.time() < self.deadline:
            for symbol in self.symbols:
                for kind, path in (("depth", "/fapi/v1/depth"), ("rpiDepth", "/fapi/v1/rpiDepth")):
                    try:
                        body = session.get(f"{REST_ROOT}{path}", params={"symbol": symbol, "limit": 1000},
                                           timeout=10).text
                        received = time.time()
                        json.loads(body)
                    except Exception as error:
                        print(f"{symbol} {kind} snapshot failed: {error}", file=sys.stderr, flush=True)
                        continue
                    line = f'{{"r":{received:.6f},"rest":"{kind}","s":"{symbol}","m":{body}}}'
                    with self.lock:
                        self.raw_lines.append(line)
            self.stop.wait(self.snapshot_sec)

    def on_message(self, _ws, raw: str) -> None:
        received = time.time()
        self.keep_raw(received, raw)
        message = json.loads(raw)
        data = message.get("data") or {}
        stream = message.get("stream", "")
        symbol = str(data.get("s", "")).upper()
        if stream.endswith("@aggTrade"):
            row = {
                "received_ts": received,
                "exchange_ts": float(data["T"]) / 1000.0,
                "trade_id": int(data["a"]),
                "price": float(data["p"]),
                "qty": float(data["q"]),
                "maker_is_buyer": bool(data["m"]),
            }
            with self.lock:
                self.trades[symbol].append(row)
        elif stream.endswith("@bookTicker"):
            row = {
                "received_ts": received,
                "exchange_ts": float(data["E"]) / 1000.0,
                "update_id": int(data["u"]),
                "bid_p": float(data["b"]),
                "bid_v": float(data["B"]),
                "ask_p": float(data["a"]),
                "ask_v": float(data["A"]),
            }
            if self.ticker_price_changes_only:
                touch = (row["bid_p"], row["ask_p"])
                if self.last_touch.get(symbol) == touch:
                    return
                self.last_touch[symbol] = touch
            with self.lock:
                self.tickers[symbol].append(row)
        elif "@depth5" in stream:
            row = {"received_ts": received, "exchange_ts": float(data["E"]) / 1000.0}
            for side, key in (("bid", "b"), ("ask", "a")):
                levels = data.get(key) or []
                for index in range(LEVELS):
                    price, qty = (levels[index] if index < len(levels) else (0.0, 0.0))
                    row[f"{side}{index + 1}_p"] = float(price)
                    row[f"{side}{index + 1}_v"] = float(qty)
            with self.lock:
                self.depth[symbol].append(row)

    def flush(self) -> None:
        with self.lock:
            depth, self.depth = self.depth, defaultdict(list)
            trades, self.trades = self.trades, defaultdict(list)
            tickers, self.tickers = self.tickers, defaultdict(list)
            raw_lines, self.raw_lines = self.raw_lines, []
        now = datetime.now(timezone.utc)
        day = now.strftime("%Y%m%d")
        if raw_lines:
            path = self.out / f"raw_{now:%Y%m%d_%H}.jsonl.gz"
            try:
                with gzip.open(path, "at", compresslevel=6) as handle:
                    handle.write("\n".join(raw_lines) + "\n")
            except Exception as error:
                print(f"{path.name}: write deferred ({type(error).__name__})", file=sys.stderr, flush=True)
                with self.lock:
                    self.raw_lines[:0] = raw_lines
        for kind, batches in (("depth", depth), ("trade", trades), ("ticker", tickers)):
            for symbol, rows in batches.items():
                if not rows:
                    continue
                path = self.out / f"{symbol}_{kind}_{day}_v2.h5"
                frame = pd.DataFrame(rows)
                try:
                    frame.to_hdf(path, key=kind, mode="a", format="table", append=True,
                                 complevel=5, complib="blosc")
                except Exception as error:  # e.g. a reader holds the HDF5 file lock
                    print(f"{path.name}: write deferred ({type(error).__name__})",
                          file=sys.stderr, flush=True)
                    with self.lock:
                        pending = {"depth": self.depth, "trade": self.trades, "ticker": self.tickers}[kind]
                        pending[symbol][:0] = rows
                    continue
                self.rows += len(rows)

    def apps(self) -> list[websocket.WebSocketApp]:
        """Connections for one session; a venue subclass may override."""
        return [
            websocket.WebSocketApp(url, on_message=self.on_message, on_error=report_ws_error)
            for url in self.urls()
        ]

    def background(self) -> list[threading.Thread]:
        """Threads that run for the whole recording; a venue subclass may override."""
        return [threading.Thread(target=self.snapshot_loop, daemon=True)] if self.all_streams else []

    def run(self) -> int:
        self.out.mkdir(parents=True, exist_ok=True)
        for thread in self.background():
            thread.start()
        while not self.stop.is_set() and time.time() < self.deadline:
            apps = self.apps()
            threads = [
                threading.Thread(target=app.run_forever, kwargs={"ping_interval": 60}, daemon=True)
                for app in apps
            ]
            for thread in threads:
                thread.start()
            while all(t.is_alive() for t in threads) and not self.stop.is_set() and time.time() < self.deadline:
                time.sleep(FLUSH_SEC)
                self.flush()
                free = shutil.disk_usage(self.out).free
                print(f"{datetime.now(timezone.utc):%H:%M:%S} rows={self.rows:,} free={free / 1e9:.1f}GB", flush=True)
                if free < self.min_free:
                    print("free disk below the floor; stopping", file=sys.stderr, flush=True)
                    self.stop.set()
            for app in apps:
                app.close()
            for thread in threads:
                thread.join(timeout=5)
            self.flush()
            if not self.stop.is_set() and time.time() < self.deadline:
                time.sleep(5)  # reconnect
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--symbols", required=True, help="Comma-separated symbols")
    parser.add_argument("--out", default="storage/research")
    parser.add_argument("--hours", type=float, default=12.0)
    parser.add_argument("--min-free-gb", type=float, default=2.0)
    parser.add_argument("--ticker-price-changes-only", action="store_true",
                        help="Keep a bookTicker update only when the best bid or ask price changes")
    parser.add_argument("--all-streams", action="store_true",
                        help="Also record full-depth diffs, the RPI book, mark price, REST snapshots and raw messages")
    parser.add_argument("--snapshot-sec", type=float, default=60.0)
    parser.add_argument("--ticker-only", action="store_true",
                        help="Record only bookTicker (no depth5, no aggTrade)")
    parser.add_argument("--connection-per-symbol", action="store_true",
                        help="One websocket per symbol in this process, so one busy symbol cannot delay another")
    args = parser.parse_args(argv)
    recorder = Recorder(args.symbols.split(","), Path(args.out), args.hours, args.min_free_gb,
                        ticker_price_changes_only=args.ticker_price_changes_only,
                        all_streams=args.all_streams, snapshot_sec=args.snapshot_sec,
                        ticker_only=args.ticker_only, connection_per_symbol=args.connection_per_symbol)
    signal.signal(signal.SIGTERM, lambda *_: recorder.stop.set())
    signal.signal(signal.SIGINT, lambda *_: recorder.stop.set())
    return recorder.run()


if __name__ == "__main__":
    raise SystemExit(main())
