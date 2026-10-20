"""Run the GRVT maker priced off Binance for one symbol.

Shadow by default: Binance and GRVT market data are live, quotes are
computed and every order the engine would send is logged, but nothing is
sent (orders "rest" until the engine cancels them, and never fill). Live
trading needs both ``"live_enabled": true`` in the config and ``--live``,
plus the secrets file the config names (API key, sub-account id and the
trading key; keep it outside the repository, e.g. ``config/local/``, mode
600).

One process trades one symbol (Binance hot symbols lag when they share a
connection, and a failure stays contained): it reads Binance
``bookTicker`` on its own socket, rebuilds the GRVT book, and in live mode
follows its orders and fills on GRVT's private websocket. Everything runs
through one queue into :class:`strategy.grvt_maker_engine.MakerEngine`;
venue calls run on a small thread pool and answer through the queue.

Safety in live mode, besides the engine's risk limits:

* on start, open orders on the instrument's base are cancelled and the
  position is read from the venue;
* GRVT's cancel-on-disconnect is armed (``cancel_on_disconnect_ms``) and
  renewed every second, so orders die if this process does;
* open orders and the position are reconciled every ``reconcile_sec``
  (a position difference must show twice in a row before it is applied,
  so fills in flight do not reset it) and after a private-feed gap;
* the kill file (``kill_file``) halts every runner that sees it;
* SIGTERM / SIGINT cancel everything before exiting.

Events go to ``<log_dir>/<SYMBOL>_<YYYYMMDD>.jsonl``; a status line is
printed every ``status_sec``.

Example::

    python scripts/run_grvt_maker.py --config config/local/grvt_maker.json --symbol XRPUSDT --minutes 30
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import stat
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.binance.book_ticker_feed import BinanceBookTickerFeed  # noqa: E402
from gateway.grvt.book_feed import GrvtBookFeed  # noqa: E402
from gateway.grvt.rest_api import ENVIRONMENTS, GrvtApiError, GrvtRestClient, client_order_id  # noqa: E402
from strategy.grvt_cross_maker import QuoteParams  # noqa: E402
from strategy.grvt_maker_engine import (  # noqa: E402
    InstrumentRules,
    MakerEngine,
    Mode,
    RiskLimits,
    external_touch,
)

TIMER_SEC = 0.1
HALT_EXIT_CODE = 3  # a risk halt: systemd must not restart it (RestartPreventExitStatus)


# ----------------------------------------------------------------- config
def load_config(path: str, symbol: str) -> dict:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    per_symbol = (config.get("symbols") or {}).get(symbol)
    if per_symbol is None:
        raise SystemExit(f"{symbol} is not configured in {path}")
    defaults = config.get("defaults") or {}
    quote = {**(defaults.get("quote") or {}), **(per_symbol.get("quote") or {})}
    risk = {**(defaults.get("risk") or {}), **(per_symbol.get("risk") or {})}
    known_quote = {f.name for f in fields(QuoteParams)}
    known_risk = {f.name for f in fields(RiskLimits)}
    unknown = sorted(set(quote) - known_quote) + sorted(set(risk) - known_risk)
    if unknown:
        raise SystemExit(f"unknown settings for {symbol}: {', '.join(unknown)}")
    return {**config, "quote": QuoteParams(**quote), "risk": RiskLimits(**risk)}


def load_secrets(path: str) -> dict:
    file = Path(path)
    if not file.exists():
        raise SystemExit(f"{path} is missing: copy deploy/live/grvt-maker/grvt_secrets.example.json there, "
                         f"fill it in and chmod 600 it")
    mode = file.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SystemExit(f"{path} is readable by others; run: chmod 600 {path}")
    secrets = json.loads(file.read_text(encoding="utf-8"))
    missing = [k for k in ("api_key", "sub_account_id", "private_key") if not secrets.get(k)]
    if missing:
        raise SystemExit(f"{path} lacks {', '.join(missing)}")
    return secrets


def fetch_instrument(symbol: str, environment: str) -> dict:
    """GRVT's listing for ``<base><quote>`` (public, no key needed)."""
    url = ENVIRONMENTS[environment]["market_data"] + "/full/v1/all_instruments"
    payload = requests.post(url, json={"kind": ["PERPETUAL"], "is_active": True, "limit": 1000}, timeout=15).json()
    for item in payload.get("result") or []:
        if f"{item['base']}{item['quote']}".upper() == symbol:
            return item
    raise SystemExit(f"{symbol} is not an active GRVT perpetual")


def decimals(step: float) -> int:
    text = f"{step:.12f}".rstrip("0")
    return len(text.split(".")[1]) if "." in text else 0


# --------------------------------------------------------------- logging
class EventLog:
    def __init__(self, directory: str, symbol: str):
        self.directory, self.symbol = Path(directory), symbol
        self.directory.mkdir(parents=True, exist_ok=True)
        self.day, self.file = None, None

    def __call__(self, event: str, **values) -> None:
        now = time.time()
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%d")
        if day != self.day:
            if self.file:
                self.file.close()
            self.day = day
            self.file = open(self.directory / f"{self.symbol}_{day}.jsonl", "a", encoding="utf-8")
        self.file.write(json.dumps({"ts": round(now, 6), "event": event, **values}, default=str) + "\n")
        if event in {"mode", "venue_error", "quiet", "resume", "position_reset", "reconcile_unknown", "feed"}:
            print(f"{datetime.now(timezone.utc):%H:%M:%S} {event} {values}", flush=True)

    def flush(self) -> None:
        if self.file:
            self.file.flush()


# -------------------------------------------------------------- executors
class ShadowExecutor:
    """Answers every action at once; orders rest until cancelled, never fill."""

    def __init__(self, events: queue.SimpleQueue):
        self.events = events

    def place(self, *, client_id, side, price, size, reduce_only):
        self.events.put(("place_result", client_id, True, f"shadow-{client_id}", "", False))

    def cancel(self, *, client_id):
        self.events.put(("cancel_result", client_id, True, ""))
        self.events.put(("state", client_id, "CANCELLED", None, ""))

    def cancel_all(self):
        pass

    def reconcile(self):
        pass


class LiveExecutor:
    def __init__(self, rest: GrvtRestClient, instrument: str, base: str, rules: InstrumentRules,
                 events: queue.SimpleQueue, log):
        self.rest, self.instrument, self.base, self.events, self.log = rest, instrument, base, events, log
        self.price_decimals, self.size_decimals = decimals(rules.tick), decimals(rules.size_step)
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="grvt-rest")

    def place(self, *, client_id, side, price, size, reduce_only):
        self.pool.submit(self._place, client_id, side, price, size, reduce_only)

    def _place(self, client_id, side, price, size, reduce_only):
        try:
            body = self.rest.build_limit_order(
                instrument=self.instrument, is_buy=side == "BUY", size=f"{size:.{self.size_decimals}f}",
                price=f"{price:.{self.price_decimals}f}", post_only=True, reduce_only=reduce_only,
                client_id=client_id)
            result = self.rest.create_order(body) or {}
            self.events.put(("place_result", client_id, True, result.get("order_id"), "", False))
        except Exception as exc:  # noqa: BLE001 - every failure goes back to the engine
            text = str(exc)
            self.events.put(("place_result", client_id, False, None, text, "POST_ONLY" in text.upper()))

    def cancel(self, *, client_id):
        self.pool.submit(self._cancel, client_id)

    def _cancel(self, client_id):
        try:
            self.rest.cancel_order(client_id=client_id)
            self.events.put(("cancel_result", client_id, True, ""))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("cancel_result", client_id, False, str(exc)))

    def cancel_all(self):
        self.pool.submit(self._cancel_all)

    def _cancel_all(self):
        try:
            self.rest.cancel_all_orders(base=self.base)
        except Exception as exc:  # noqa: BLE001
            self.log("venue_error", what="cancel_all", error=str(exc), consecutive=0)

    def reconcile(self):
        self.pool.submit(self._reconcile)

    def _reconcile(self):
        try:
            orders = {}
            for order in self.rest.open_orders():
                legs = order.get("legs") or [{}]
                if legs[0].get("instrument") == self.instrument:
                    orders[str((order.get("metadata") or {}).get("client_order_id"))] = order
            position = 0.0
            for item in self.rest.positions():
                if item.get("instrument") == self.instrument:
                    position = float(item.get("size") or 0.0)
            self.events.put(("reconcile", orders, position))
        except Exception as exc:  # noqa: BLE001
            self.log("venue_error", what="reconcile", error=str(exc), consecutive=0)


# ------------------------------------------------------------- the runner
def private_events(stream: str, feed: dict, instrument: str):
    """Engine calls for one private-feed message."""
    if stream == "v1.state":
        state = feed.get("order_state") or {}
        return [("state", str(feed.get("client_order_id")), str(state.get("status", "")), feed.get("order_id"),
                 str(state.get("reject_reason", "")))]
    if stream == "v1.order":
        legs = feed.get("legs") or [{}]
        if legs[0].get("instrument") not in (None, instrument):
            return []
        state = feed.get("state") or {}
        return [("state", str((feed.get("metadata") or {}).get("client_order_id")), str(state.get("status", "")),
                 feed.get("order_id"), str(state.get("reject_reason", "")))]
    if stream == "v1.fill" and feed.get("instrument") == instrument:
        return [("fill", dict(trade_id=str(feed.get("trade_id")), client_id=str(feed.get("client_order_id")),
                              is_buyer=bool(feed.get("is_buyer")), size=float(feed.get("size") or 0),
                              price=float(feed.get("price") or 0), fee=float(feed.get("fee") or 0),
                              is_taker=bool(feed.get("is_taker")), fee_rate=str(feed.get("fee_rate", ""))))]
    return []


def run(args) -> int:
    symbol = args.symbol.upper()
    config = load_config(args.config, symbol)
    environment = config.get("environment", "prod")
    live = bool(args.live)
    if live and not config.get("live_enabled"):
        raise SystemExit('--live needs "live_enabled": true in the config')
    info = fetch_instrument(symbol, environment)
    instrument, base = info["instrument"], info["base"]
    rules = InstrumentRules(tick=float(info["tick_size"]), size_step=float(info["min_size"]),
                            min_size=float(info["min_size"]), min_notional=float(info.get("min_notional") or 0))
    log = EventLog(config.get("log_dir", "storage/live/grvt_maker"), symbol)
    events: queue.SimpleQueue = queue.SimpleQueue()
    kill_file = Path(config.get("kill_file", "storage/live/KILL"))

    rest = private = None
    if live:
        secrets = load_secrets(config["secrets_file"])
        rest = GrvtRestClient(api_key=secrets["api_key"], sub_account_id=secrets["sub_account_id"],
                              private_key=secrets["private_key"], environment=environment)
        rest.load_instruments()
        executor = LiveExecutor(rest, instrument, base, rules, events, log)
    else:
        executor = ShadowExecutor(events)
    engine = MakerEngine(symbol=symbol, rules=rules, params=config["quote"], limits=config["risk"],
                         executor=executor, new_client_id=client_order_id, log=log)
    log("start", mode="live" if live else "shadow", instrument=instrument, rules=str(rules),
        quote=str(config["quote"]), risk=str(config["risk"]))
    print(f"{symbol} {instrument} {'LIVE' if live else 'shadow'}: {rules} {config['quote']} {config['risk']}",
          flush=True)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    if live:
        from gateway.grvt.private_ws import GrvtPrivateWs, wait_until_subscribed
        rest.cancel_all_orders(base=base)
        for item in rest.positions():
            if item.get("instrument") == instrument:
                engine.position = float(item.get("size") or 0.0)
        log("start_position", position=engine.position)
        countdown = int(config.get("cancel_on_disconnect_ms", 5000))

        def heartbeat():
            while not stop.is_set():
                try:
                    rest.cancel_on_disconnect(countdown)
                except (GrvtApiError, requests.RequestException) as exc:
                    log("venue_error", what="cancel_on_disconnect", error=str(exc), consecutive=0)
                stop.wait(1.0)
        threading.Thread(target=heartbeat, name="grvt-cod", daemon=True).start()
        private = GrvtPrivateWs(rest=rest, instrument=instrument, environment=environment,
                                on_message=lambda stream, feed: events.put(("private", stream, feed)),
                                on_gap=lambda *gap: events.put(("gap", gap)),
                                on_status=lambda status: events.put(("feed", "grvt-private", status)))
        private.start()
        if not wait_until_subscribed(private):
            raise SystemExit("GRVT private feed did not subscribe")

    binance = BinanceBookTickerFeed(symbol, lambda t, b, a, e: events.put(("binance", b, a, e)),
                                    on_status=lambda s: events.put(("feed", "binance", s)),
                                    on_alive=lambda t: events.put(("alive", "binance")))
    book = GrvtBookFeed(instrument, lambda t, bids, asks: events.put(("book", bids, asks)),
                        on_status=lambda s: events.put(("feed", "grvt-book", s)),
                        on_alive=lambda t: events.put(("alive", "grvt")))
    binance.start()
    book.start()

    deadline = time.time() + args.minutes * 60 if args.minutes else None
    status_sec = float(config.get("status_sec", 10))
    reconcile_sec = float(config.get("reconcile_sec", 30))
    next_timer = next_status = time.time()
    next_reconcile = time.time() + reconcile_sec
    position_mismatch = None
    stopping_since = None
    while True:
        now = time.time()
        if stopping_since is None and (stop.is_set() or (deadline and now >= deadline) or kill_file.exists()):
            reason = "kill file" if kill_file.exists() else "shutdown"
            engine.halt(reason)
            stopping_since = now
        if stopping_since is not None and (now - stopping_since > 3.0 or (not engine.orders and now - stopping_since > 0.5)):
            break
        try:
            item = events.get(timeout=TIMER_SEC)
        except queue.Empty:
            item = None
        if item is not None:
            kind = item[0]
            if kind == "binance":
                engine.on_binance(item[1], item[2], item[3])
            elif kind == "alive":
                engine.on_feed_alive(item[1])
            elif kind == "book":
                # Shadow quotes are not in the public book, so nothing to take out.
                bid, ask = external_touch(item[1], item[2], engine.own_levels() if live else {})
                engine.on_grvt_book(bid, ask)
            elif kind == "place_result":
                _, cid, ok, order_id, error, post_only = item
                engine.on_place_result(cid, ok, order_id=order_id, error=error, post_only_reject=post_only)
            elif kind == "cancel_result":
                engine.on_cancel_result(item[1], item[2], error=item[3])
            elif kind == "state":
                engine.on_order_state(item[1], item[2], order_id=item[3], reject_reason=item[4])
            elif kind == "private":
                for call in private_events(item[1], item[2], instrument):
                    if call[0] == "fill":
                        engine.on_fill(**call[1])
                    else:
                        engine.on_order_state(call[1], call[2], order_id=call[3], reject_reason=call[4])
            elif kind == "gap":
                log("private_gap", gap=item[1])
                executor.reconcile()
            elif kind == "reconcile":
                _, orders, position = item
                if abs(position - engine.position) > rules.size_step / 2:
                    confirmed = position_mismatch is not None and abs(position_mismatch - position) < rules.size_step / 2
                    position_mismatch = None if confirmed else position
                    engine.reconcile(orders, position if confirmed else None)
                else:
                    position_mismatch = None
                    engine.reconcile(orders, None)
            elif kind == "feed":
                log("feed", source=item[1], status=item[2])
        if now >= next_timer:
            engine.on_timer()
            next_timer = now + TIMER_SEC
        if live and now >= next_reconcile:
            executor.reconcile()
            next_reconcile = now + reconcile_sec
        if now >= next_status:
            status = engine.status()
            log("status", **status)
            log.flush()
            q = status["quotes"]
            print(f"{datetime.now(timezone.utc):%H:%M:%S} {symbol} {status['mode']} pos {status['position']:g} "
                  f"({status['exposure_usd']} USD) pnl {status['pnl_today']} | grvt {status['bid']}/{status['ask']} "
                  f"binance {status['binance_mid']:.6g} basis {status['log_basis_bps']} bps | "
                  f"bid {q['BUY']} ask {q['SELL']} | placed {status['placed']} cancelled {status['cancelled']} "
                  f"fills {status['fills']} {status['reason']}", flush=True)
            next_status = now + status_sec
    for client in (binance, book, private):
        if client is not None:
            client.close()
    if live:
        rest.cancel_all_orders(base=base)
    log("stop", **engine.status())
    log.flush()
    return HALT_EXIT_CODE if engine.mode is Mode.HALTED and engine.reason not in {"shutdown", "kill file"} else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default="config/local/grvt_maker.json")
    parser.add_argument("--symbol", required=True, help="<base><quote>, e.g. XRPUSDT")
    parser.add_argument("--minutes", type=float, default=0.0, help="Stop after this long (0: run until stopped)")
    parser.add_argument("--live", action="store_true", help="Send real orders (needs live_enabled in the config)")
    args = parser.parse_args(argv)
    os.umask(0o077)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
