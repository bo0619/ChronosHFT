"""Record public GRVT perpetual books and trades for offline research.

GRVT counterpart of ``scripts/record_public_book.py``, writing the same
files, so ``scripts/replay_queue_quotes.py`` reads both (keep GRVT in its
own ``--out`` directory: symbol names overlap with Binance):

* ``<out>/<SYMBOL>_depth_<YYYYMMDD>_v2.h5`` key ``depth``: the top five
  levels after every book message that changes them;
* ``<out>/<SYMBOL>_trade_<YYYYMMDD>_v2.h5`` key ``trade``: ``received_ts``,
  ``exchange_ts``, ``trade_id``, ``price``, ``qty``, ``maker_is_buyer``,
  plus GRVT's ``is_rpi`` and ``is_rfq`` (venue other than the order book).

``v1.book.d`` at 50 ms sends a full book on subscribe, then changed levels
(``size == 0`` removes one). The book is rebuilt here: the snapshot sits at
the subscribe acknowledgement's ``latest_sequence_number``, each delta must
continue from the previous sequence, and a gap drops the book and
resubscribes that market (as ``gateway/grvt/market_data.py``). ``v1.trade``
replays recent history on subscribe with sequence number 0; those prints
are skipped. Symbols are GRVT's ``<base><quote>`` names (``ETH_USDT_Perp``
is ``ETHUSDT``).

Example::

    python scripts/record_grvt_book.py --symbols BTCUSDT,ETHUSDT,SOLUSDT --hours 24 --out storage/research/grvt
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

import requests
import websocket

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from record_public_book import LEVELS, Recorder, report_ws_error  # noqa: E402

from gateway.grvt.constants import BOOK_RATE_MS, BOOK_STREAM, TRADE_LIMIT, TRADE_STREAM, WS_URL_MAIN  # noqa: E402
from gateway.grvt.market_data import resubscribe_book_frames, timestamp_ms, trade_id_number  # noqa: E402

INSTRUMENTS_URL = "https://market-data.grvt.io/full/v1/all_instruments"


def instrument_names(symbols: list[str]) -> dict[str, str]:
    """``{"BTCUSDT": "BTC_USDT_Perp", ...}`` from GRVT's instrument list."""
    payload = requests.post(
        INSTRUMENTS_URL, json={"kind": ["PERPETUAL"], "is_active": True, "limit": 1000}, timeout=15
    ).json()
    known = {f"{i['base']}{i['quote']}".upper(): i["instrument"] for i in payload["result"]}
    missing = [s for s in symbols if s not in known]
    if missing:
        raise SystemExit(f"not GRVT perpetuals: {','.join(missing)}")
    return {s: known[s] for s in symbols}


class GrvtRecorder(Recorder):
    def __init__(self, symbols: list[str], out: Path, hours: float, min_free_gb: float) -> None:
        super().__init__(symbols, out, hours, min_free_gb)
        self.instruments = instrument_names(self.symbols)
        self.symbol_of = {inst: sym for sym, inst in self.instruments.items()}
        self.request_id = 100
        self.reset_books()

    def reset_books(self) -> None:
        self.books: dict[str, tuple[dict, dict] | None] = {s: None for s in self.symbols}
        self.last_seq: dict[str, int | None] = {s: None for s in self.symbols}
        self.snapshot_seq: dict[str, int] = {}
        self.last_top: dict[str, tuple] = {}

    def apps(self) -> list[websocket.WebSocketApp]:
        self.reset_books()
        return [websocket.WebSocketApp(
            WS_URL_MAIN, on_open=self.on_open, on_message=self.on_message, on_error=report_ws_error,
        )]

    def on_open(self, ws) -> None:
        instruments = list(self.instruments.values())
        for request_id, stream, limit in ((1, BOOK_STREAM, BOOK_RATE_MS), (2, TRADE_STREAM, TRADE_LIMIT)):
            ws.send(json.dumps({
                "jsonrpc": "2.0", "method": "subscribe", "id": request_id,
                "params": {"stream": stream, "selectors": [f"{i}@{limit}" for i in instruments]},
            }))

    def on_message(self, ws, raw: str) -> None:
        received = time.time()
        message = json.loads(raw)
        result = message.get("result")
        if isinstance(result, dict) and result.get("stream") == BOOK_STREAM:
            for selector, sequence in zip(result.get("subs") or [], result.get("latest_sequence_number") or []):
                symbol = self.symbol_of.get(selector.partition("@")[0])
                if symbol:
                    self.snapshot_seq[symbol] = int(sequence)
            return
        feed = message.get("feed")
        if not isinstance(feed, dict):
            if message.get("error"):
                print(f"venue error: {message['error']}", file=sys.stderr, flush=True)
            return
        symbol = self.symbol_of.get(str(feed.get("instrument", "")))
        if not symbol:
            return
        sequence = int(message.get("sequence_number", 0) or 0)
        exchange_ts = timestamp_ms(feed.get("event_time")) / 1000.0
        if message.get("stream") == TRADE_STREAM:
            if sequence == 0:
                return  # history replayed on subscribe
            row = {
                "received_ts": received,
                "exchange_ts": exchange_ts,
                "trade_id": trade_id_number(feed.get("trade_id")),
                "price": float(feed["price"]),
                "qty": float(feed["size"]),
                "maker_is_buyer": not bool(feed.get("is_taker_buyer", False)),
                "is_rpi": bool(feed.get("is_rpi", False)),
                "is_rfq": str(feed.get("venue", "ORDERBOOK")) != "ORDERBOOK",
            }
            with self.lock:
                self.trades[symbol].append(row)
            return
        if message.get("stream") != BOOK_STREAM:
            return
        levels = {side: {float(l["price"]): float(l["size"]) for l in feed.get(side) or []}
                  for side in ("bids", "asks")}
        if sequence == 0:
            self.books[symbol] = (levels["bids"], levels["asks"])
            self.last_seq[symbol] = self.snapshot_seq.pop(symbol, None)
        else:
            book, last = self.books[symbol], self.last_seq[symbol]
            if book is None:
                return  # waiting for the snapshot after a resubscribe
            previous = int(message.get("prev_sequence_number", 0) or 0)
            if last is not None and previous != last:
                print(f"{symbol}: book gap {last} -> {previous}; resubscribing", file=sys.stderr, flush=True)
                self.books[symbol] = None
                self.request_id += 2
                for frame in resubscribe_book_frames(self.instruments[symbol], self.request_id):
                    ws.send(json.dumps(frame))
                return
            for side, target in zip(("bids", "asks"), book):
                for price, size in levels[side].items():
                    if size <= 0.0:
                        target.pop(price, None)
                    else:
                        target[price] = size
            self.last_seq[symbol] = sequence
        bids, asks = self.books[symbol]
        top_bids = sorted(bids.items(), reverse=True)[:LEVELS]
        top_asks = sorted(asks.items())[:LEVELS]
        if (top_bids, top_asks) == self.last_top.get(symbol):
            return  # only levels below the top five changed
        self.last_top[symbol] = (top_bids, top_asks)
        row = {"received_ts": received, "exchange_ts": exchange_ts}
        for side, top in (("bid", top_bids), ("ask", top_asks)):
            for index in range(LEVELS):
                price, size = top[index] if index < len(top) else (0.0, 0.0)
                row[f"{side}{index + 1}_p"] = price
                row[f"{side}{index + 1}_v"] = size
        with self.lock:
            self.depth[symbol].append(row)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--symbols", required=True, help="Comma-separated <base><quote> names, e.g. BTCUSDT")
    parser.add_argument("--out", default="storage/research/grvt")
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--min-free-gb", type=float, default=2.0)
    args = parser.parse_args(argv)
    recorder = GrvtRecorder([s.strip().upper() for s in args.symbols.split(",") if s.strip()],
                            Path(args.out), args.hours, args.min_free_gb)
    signal.signal(signal.SIGTERM, lambda *_: recorder.stop.set())
    signal.signal(signal.SIGINT, lambda *_: recorder.stop.set())
    return recorder.run()


if __name__ == "__main__":
    raise SystemExit(main())
