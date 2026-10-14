"""Record public Binance USD-M books and trades for offline research.

Subscribes to ``<symbol>@depth5@100ms`` (top-5 snapshots) and
``<symbol>@aggTrade`` for any symbols, with no key, OMS or strategy, and
appends them to compressed HDF5 files in the column layout of
``data/recorder.py``, so the same tools read both:

* ``<out>/<SYMBOL>_depth_<YYYYMMDD>_v2.h5`` key ``depth``: ``received_ts``,
  ``exchange_ts``, ``bid1_p``, ``bid1_v`` ... ``ask5_p``, ``ask5_v``;
* ``<out>/<SYMBOL>_trade_<YYYYMMDD>_v2.h5`` key ``trade``: ``received_ts``,
  ``exchange_ts``, ``trade_id``, ``price``, ``qty``, ``maker_is_buyer``.

Book streams are served under ``/public`` and trade streams under
``/market`` (as in ``gateway/binance/ws_api.py``); the plain ``/stream``
path no longer carries aggTrade. Recording stops by itself after
``--hours`` or when free disk falls below ``--min-free-gb``. Reconnects
after a dropped connection.

Example::

    python scripts/record_public_book.py --symbols ARCUSDT,ZKUSDT --hours 12
"""

from __future__ import annotations

import argparse
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
import websocket

WS_ROOT = "wss://fstream.binance.com"
LEVELS = 5
FLUSH_SEC = 10.0


class Recorder:
    def __init__(self, symbols: list[str], out: Path, hours: float, min_free_gb: float) -> None:
        self.symbols = [s.upper() for s in symbols]
        self.out = out
        self.deadline = time.time() + hours * 3600.0
        self.min_free = min_free_gb * 1e9
        self.depth: dict[str, list[dict]] = defaultdict(list)
        self.trades: dict[str, list[dict]] = defaultdict(list)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.rows = 0

    def urls(self) -> list[str]:
        lower = [symbol.lower() for symbol in self.symbols]
        return [
            f"{WS_ROOT}/public/stream?streams=" + "/".join(f"{s}@depth5@100ms" for s in lower),
            f"{WS_ROOT}/market/stream?streams=" + "/".join(f"{s}@aggTrade" for s in lower),
        ]

    def on_message(self, _ws, raw: str) -> None:
        received = time.time()
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
        elif "@depth" in stream:
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
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        for kind, batches in (("depth", depth), ("trade", trades)):
            for symbol, rows in batches.items():
                if not rows:
                    continue
                path = self.out / f"{symbol}_{kind}_{day}_v2.h5"
                frame = pd.DataFrame(rows)
                frame.to_hdf(path, key=kind, mode="a", format="table", append=True,
                             complevel=5, complib="blosc")
                self.rows += len(rows)

    def run(self) -> int:
        self.out.mkdir(parents=True, exist_ok=True)
        while not self.stop.is_set() and time.time() < self.deadline:
            apps = [
                websocket.WebSocketApp(
                    url,
                    on_message=self.on_message,
                    on_error=lambda _ws, error: print(f"ws error: {error}", file=sys.stderr, flush=True),
                )
                for url in self.urls()
            ]
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
    args = parser.parse_args(argv)
    recorder = Recorder(args.symbols.split(","), Path(args.out), args.hours, args.min_free_gb)
    signal.signal(signal.SIGTERM, lambda *_: recorder.stop.set())
    signal.signal(signal.SIGINT, lambda *_: recorder.stop.set())
    return recorder.run()


if __name__ == "__main__":
    raise SystemExit(main())
