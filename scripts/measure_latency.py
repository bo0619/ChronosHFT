"""Measure network and venue latency from this host, for queue simulation.

The Paper engine and ``replay_queue_quotes.py`` delay orders and cancels by
a fixed number of milliseconds; this samples the real distribution instead.
Probes run concurrently for ``--minutes`` and need no API key:

* ``binance_ws_api_rtt``: round trip of ``ping`` on the Binance futures
  WebSocket API (``ws-fapi``), the connection orders and cancels use;
* ``binance_rest_rtt``: round trip of ``GET /fapi/v1/time`` on a kept-alive
  HTTPS session;
* ``binance_book_oneway`` / ``binance_trade_oneway``: local receipt minus
  the matching-engine time ``T`` of ``@bookTicker`` and ``@aggTrade``
  pushes (one way, venue to us), and ``..._publish_delay``: the push's
  event time ``E`` minus ``T``, the part the venue holds before sending.
  aggTrade is often held ~150 ms, so trades can reach us well after the
  book changes they caused;
* ``grvt_rest_rtt``: round trip of ``POST /full/v1/ticker`` on GRVT market
  data (served through Cloudflare);
* ``grvt_book_oneway`` / ``grvt_trade_oneway``: local receipt minus
  ``event_time`` of ``v1.book.d`` and ``v1.trade``;
* ``tcp_<host>``: TCP handshake time, the bare network round trip.

One-way numbers compare our clock with the venue's. The host should be on
a tight time source (on AWS, chrony against 169.254.169.123 keeps it within
microseconds); the clock offset reported by ``chronyc tracking`` is printed
with the results. Not measured without a key: the venue's own order
handling (send to acknowledgement of a real order), which adds to the
round trips here.

Samples go to ``<out>/latency_<YYYYMMDD_HHMMSS>.csv.gz`` (``ts``, ``probe``,
``ms``) and a percentile summary to ``<out>/latency_<...>_summary.json``.

Example::

    python scripts/measure_latency.py --minutes 10 --symbols KORUUSDT,XAUUSDT
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import websocket

BINANCE_WS_API = "wss://ws-fapi.binance.com/ws-fapi/v1"
BINANCE_WS = "wss://fstream.binance.com"
BINANCE_REST = "https://fapi.binance.com"
GRVT_WS = "wss://market-data.grvt.io/ws/full"
GRVT_REST = "https://market-data.grvt.io/full/v1"
TCP_HOSTS = ("fapi.binance.com", "ws-fapi.binance.com", "fstream.binance.com",
             "market-data.grvt.io", "trades.grvt.io")
PERCENTILES = (50, 90, 99, 99.9)


class Collector:
    def __init__(self) -> None:
        self.samples: list[tuple[float, str, float]] = []
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def add(self, probe: str, ms: float) -> None:
        if math.isfinite(ms):
            with self.lock:
                self.samples.append((time.time(), probe, ms))


def every(collector: Collector, interval: float, step) -> None:
    """Run ``step`` every ``interval`` seconds until stopped; log failures."""
    while not collector.stop.is_set():
        started = time.monotonic()
        try:
            step()
        except Exception as error:  # a probe failure must not end the others
            print(f"{getattr(step, '__name__', 'probe')}: {type(error).__name__}: {error}",
                  file=sys.stderr, flush=True)
            collector.stop.wait(1.0)
        collector.stop.wait(max(0.0, interval - (time.monotonic() - started)))


def binance_ws_api(collector: Collector, interval: float) -> None:
    ws = websocket.create_connection(BINANCE_WS_API, timeout=5)
    request_id = 0

    def ping():
        nonlocal request_id
        request_id += 1
        sent = time.perf_counter()
        ws.send(json.dumps({"id": request_id, "method": "ping"}))
        while json.loads(ws.recv()).get("id") != request_id:
            pass
        collector.add("binance_ws_api_rtt", (time.perf_counter() - sent) * 1000)

    try:
        every(collector, interval, ping)
    finally:
        ws.close()


def binance_rest(collector: Collector, interval: float) -> None:
    session = requests.Session()

    def get_time():
        sent = time.perf_counter()
        session.get(f"{BINANCE_REST}/fapi/v1/time", timeout=5).raise_for_status()
        collector.add("binance_rest_rtt", (time.perf_counter() - sent) * 1000)

    every(collector, interval, get_time)


def grvt_rest(collector: Collector, interval: float, instrument: str) -> None:
    session = requests.Session()

    def ticker():
        sent = time.perf_counter()
        session.post(f"{GRVT_REST}/ticker", json={"instrument": instrument}, timeout=5).raise_for_status()
        collector.add("grvt_rest_rtt", (time.perf_counter() - sent) * 1000)

    every(collector, interval, ticker)


def tcp(collector: Collector, interval: float) -> None:
    addresses = {host: socket.gethostbyname(host) for host in TCP_HOSTS}

    def handshake():
        for host, address in addresses.items():
            sock = socket.socket()
            sock.settimeout(3)
            sent = time.perf_counter()
            try:
                sock.connect((address, 443))
                collector.add(f"tcp_{host}", (time.perf_counter() - sent) * 1000)
            finally:
                sock.close()

    every(collector, interval, handshake)


def stream(collector: Collector, url: str, on_open, on_message) -> None:
    """Keep a websocket open until stopped, reconnecting after drops."""
    while not collector.stop.is_set():
        app = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message)
        thread = threading.Thread(target=app.run_forever, kwargs={"ping_interval": 60}, daemon=True)
        thread.start()
        while thread.is_alive() and not collector.stop.is_set():
            collector.stop.wait(1.0)
        app.close()
        thread.join(timeout=5)


def binance_streams(collector: Collector, symbols: list[str]) -> list[threading.Thread]:
    lower = [s.lower() for s in symbols]

    def on_message(_ws, raw):
        received = time.time()
        message = json.loads(raw)
        data, name = message.get("data") or {}, message.get("stream", "")
        if "T" in data:
            kind = "book" if name.endswith("@bookTicker") else "trade"
            collector.add(f"binance_{kind}_oneway", received * 1000 - float(data["T"]))
            if "E" in data:  # how long the venue held the event before publishing it
                collector.add(f"binance_{kind}_publish_delay", float(data["E"]) - float(data["T"]))

    urls = [
        f"{BINANCE_WS}/public/stream?streams=" + "/".join(f"{s}@bookTicker" for s in lower),
        f"{BINANCE_WS}/market/stream?streams=" + "/".join(f"{s}@aggTrade" for s in lower),
    ]
    return [threading.Thread(target=stream, args=(collector, url, None, on_message), daemon=True) for url in urls]


def grvt_stream(collector: Collector, instruments: list[str]) -> threading.Thread:
    def on_open(ws):
        for request_id, (name, limit) in enumerate((("v1.book.d", 50), ("v1.trade", 50)), 1):
            ws.send(json.dumps({"jsonrpc": "2.0", "method": "subscribe", "id": request_id,
                                "params": {"stream": name, "selectors": [f"{i}@{limit}" for i in instruments]}}))

    def on_message(_ws, raw):
        received = time.time()
        message = json.loads(raw)
        feed = message.get("feed")
        if not isinstance(feed, dict) or not int(message.get("sequence_number", 0) or 0):
            return  # acks, and snapshots that replay old state
        kind = "book" if message.get("stream") == "v1.book.d" else "trade"
        collector.add(f"grvt_{kind}_oneway", received * 1000 - int(feed["event_time"]) / 1e6)

    return threading.Thread(target=stream, args=(collector, GRVT_WS, on_open, on_message), daemon=True)


def grvt_instruments(symbols: list[str]) -> list[str]:
    payload = requests.post(f"{GRVT_REST}/all_instruments",
                            json={"kind": ["PERPETUAL"], "is_active": True, "limit": 1000}, timeout=15).json()
    known = {f"{i['base']}{i['quote']}".upper(): i["instrument"] for i in payload["result"]}
    return [known[s] for s in symbols if s in known]


def clock_offset() -> str:
    try:
        out = subprocess.run(["chronyc", "tracking"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return "chronyc unavailable"
    lines = [line.strip() for line in out.splitlines()
             if line.startswith(("Reference ID", "System time", "RMS offset"))]
    return "; ".join(lines) or "chronyc reported nothing"


def summarize(frame: pd.DataFrame) -> dict:
    summary = {}
    for probe, group in frame.groupby("probe"):
        values = group["ms"]
        summary[probe] = {"n": int(len(values)), "mean": round(float(values.mean()), 3),
                          "max": round(float(values.max()), 3)}
        summary[probe].update({f"p{p:g}": round(float(values.quantile(p / 100)), 3) for p in PERCENTILES})
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--minutes", type=float, default=10.0)
    parser.add_argument("--symbols", default="KORUUSDT,XAUUSDT", help="Symbols for the market-data probes")
    parser.add_argument("--rtt-interval", type=float, default=0.2, help="Seconds between round-trip probes")
    parser.add_argument("--out", default="storage/research/latency")
    args = parser.parse_args(argv)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    clock_before = clock_offset()

    collector = Collector()
    instruments = grvt_instruments(symbols)
    threads = [
        threading.Thread(target=binance_ws_api, args=(collector, args.rtt_interval), daemon=True),
        threading.Thread(target=binance_rest, args=(collector, max(0.5, args.rtt_interval)), daemon=True),
        threading.Thread(target=tcp, args=(collector, 2.0), daemon=True),
        *binance_streams(collector, symbols),
    ]
    if instruments:
        threads.append(threading.Thread(target=grvt_rest, args=(collector, max(0.5, args.rtt_interval),
                                                                 instruments[0]), daemon=True))
        threads.append(grvt_stream(collector, instruments))
    for thread in threads:
        thread.start()
    deadline = time.time() + args.minutes * 60
    while time.time() < deadline:
        time.sleep(min(30.0, max(0.0, deadline - time.time())))
        print(f"{datetime.now(timezone.utc):%H:%M:%S} samples={len(collector.samples):,}", flush=True)
    collector.stop.set()
    for thread in threads:
        thread.join(timeout=10)

    frame = pd.DataFrame(collector.samples, columns=["ts", "probe", "ms"])
    stem = f"latency_{started:%Y%m%d_%H%M%S}"
    frame.to_csv(out / f"{stem}.csv.gz", index=False)
    summary = {
        "started_utc": started.isoformat(), "minutes": args.minutes, "symbols": symbols,
        "clock_before": clock_before, "clock_after": clock_offset(), "probes": summarize(frame),
    }
    (out / f"{stem}_summary.json").write_text(json.dumps(summary, indent=2))
    print(f"clock: {summary['clock_after']}")
    print(f"{'probe':<32}{'n':>7}{'p50':>9}{'p90':>9}{'p99':>9}{'p99.9':>9}{'max':>9}  (ms)")
    for probe, stats in summary["probes"].items():
        print(f"{probe:<32}{stats['n']:>7}{stats['p50']:>9.2f}{stats['p90']:>9.2f}{stats['p99']:>9.2f}"
              f"{stats['p99.9']:>9.2f}{stats['max']:>9.2f}")
    print(f"wrote {out / stem}.csv.gz and _summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
