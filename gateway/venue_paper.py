"""Paper gateways on a non-Binance venue's public market data.

The local venue (staging/commit barrier, matching, ledger, private-query
shapes) is the Binance Paper venue unchanged; only the public market
transport differs. A venue's WebSocket frames are translated into the same
Binance-shaped depth, trade and mark records, so book continuity,
stale-event faults and recovery run through the existing Paper book
synchronizer. Venues that hold orders before matching (a "speed bump")
declare their maker/taker delays and the Paper venue reproduces them.
"""

from __future__ import annotations

import json
import threading
import time

from event.type import TIF_FOK, TIF_GTX, TIF_IOC, TIF_RPI, OrderRequest
from gateway.binance.paper_gateway import BinancePaperGateway
from gateway.binance.ws_api import BinanceWsApi
from infrastructure.logger import logger
from infrastructure.time_service import time_service

STREAM_NAME = "PublicWS"
# Matching worker idle tick when no speed-bumped order is waiting.
WORKER_IDLE_SEC = 0.05


class VenuePublicWs(BinanceWsApi):
    """One public WebSocket that carries book, trades and marks.

    Reuses :class:`BinanceWsApi`'s worker, reconnect and close machinery.
    Book snapshots arrive on the stream itself, so a resync asks for one by
    resubscribing the market's book channel. Subclasses provide the frames
    and the translator; ``_handle_control`` consumes venue control messages.
    """

    def __init__(
        self,
        record_callback,
        error_callback,
        *,
        url: str,
        translator,
        symbols,
    ):
        super().__init__(self._on_raw_message, error_callback, testnet=False)
        self.url = url
        self.record_callback = record_callback
        self.translator = translator
        self.known_symbols = {str(symbol).upper() for symbol in symbols}
        self._snapshot_condition = threading.Condition()
        self._snapshots: dict[str, dict] = {}

    # Venue hooks ------------------------------------------------------

    def _subscribe_frames(self) -> list[dict]:
        raise NotImplementedError

    def _resubscribe_book_frames(self, symbol: str) -> list[dict]:
        raise NotImplementedError

    def _handle_control(self, message: dict) -> bool:
        """Consume a non-market message; return True when handled."""
        return False

    # BinanceWsApi transport hooks -------------------------------------

    def start_market_stream(self, symbols):
        missing = [
            str(symbol).upper()
            for symbol in symbols
            if str(symbol).upper() not in self.known_symbols
        ]
        if missing:
            raise ValueError(f"{type(self).__name__}: unknown markets {missing}")
        return self._start_thread(self.url, STREAM_NAME) is not False

    def wait_until_connected(self, names=(STREAM_NAME,), timeout_sec=10.0):
        # The Paper gateway waits for Binance's two public streams; these
        # venues carry book, trades and marks on one connection.
        return super().wait_until_connected(
            names=(STREAM_NAME,),
            timeout_sec=timeout_sec,
        )

    def _handle_open(self, name, ws):
        with self.lock:
            rejected = not self.active or self.close_requested
        if rejected:
            ws.close()
            return
        self.translator.reset()
        for frame in self._subscribe_frames():
            ws.send(json.dumps(frame))
        super()._handle_open(name, ws)

    # Message handling ---------------------------------------------------

    def _report(self, kind: str, detail: str) -> None:
        self.error_callback(
            {"stream": STREAM_NAME, "kind": kind, "detail": detail[:500]}
        )

    def _on_raw_message(self, raw_message):
        message = json.loads(raw_message)
        if not isinstance(message, dict):
            return
        if self._handle_control(message):
            return
        try:
            snapshot, records = self.translator.translate(message)
        except (KeyError, TypeError, ValueError) as exc:
            self._report("handler_failure", f"{type(exc).__name__}: {exc}")
            return
        if snapshot is not None:
            with self._snapshot_condition:
                self._snapshots[snapshot.symbol] = snapshot.snapshot
                self._snapshot_condition.notify_all()
        for record in records:
            self.record_callback(record.stream, record.data)

    def fetch_snapshot(self, symbol: str, timeout_sec: float = 10.0):
        """Return the next unused book snapshot, resubscribing if needed."""
        symbol = str(symbol or "").upper()
        deadline = time.perf_counter() + max(0.0, float(timeout_sec))
        with self._snapshot_condition:
            snapshot = self._snapshots.pop(symbol, None)
        if snapshot is not None:
            return snapshot
        if symbol not in self.known_symbols:
            return None
        for frame in self._resubscribe_book_frames(symbol):
            if not self._send(frame):
                return None
        with self._snapshot_condition:
            while symbol not in self._snapshots:
                remaining = deadline - time.perf_counter()
                if remaining <= 0.0 or not self._is_active():
                    logger.error(
                        f"[{type(self).__name__}] Snapshot timeout for {symbol}"
                    )
                    return None
                self._snapshot_condition.wait(min(remaining, 0.25))
            return self._snapshots.pop(symbol)

    def _send(self, frame: dict) -> bool:
        with self.lock:
            ws_app = self.stream_apps.get(STREAM_NAME)
        if ws_app is None:
            return False
        try:
            ws_app.send(json.dumps(frame))
        except Exception as exc:
            logger.error(
                f"[{type(self).__name__}] send failed: "
                f"{type(exc).__name__}: {exc}"
            )
            return False
        return True


class VenueSnapshotSource:
    """Stand-in for the Binance public REST client used by the Paper venue."""

    def __init__(self, gateway: "VenuePaperGateway"):
        self._gateway = gateway

    def get_depth_snapshot(self, symbol: str, limit: int = 1000):
        ws = self._gateway.ws
        if ws is None:
            return None
        return ws.fetch_snapshot(
            symbol,
            timeout_sec=self._gateway.stream_ready_timeout_sec,
        )

    @staticmethod
    def get_rpi_depth(symbol: str, limit: int = 1000):
        return None

    @staticmethod
    def get_premium_index(symbol: str, *, timeout_sec: float = None):
        return None

    @staticmethod
    def get_all_premium_indexes(*, timeout_sec: float = None):
        return None

    @staticmethod
    def close():
        return None


class VenuePaperGateway(BinancePaperGateway):
    """Production public data from another venue with a local paper venue."""

    venue_gateway_name = "VENUE_PAPER"
    default_maker_order_delay_ms = 0.0
    default_taker_order_delay_ms = 0.0

    def __init__(
        self,
        event_engine,
        config: dict,
        market_data_config: dict | None = None,
    ):
        super().__init__(event_engine, config, market_data_config)
        self.gateway_name = self.venue_gateway_name
        self.rest = VenueSnapshotSource(self)
        self.maker_order_delay_sec = self._delay_sec(
            "maker_order_delay_ms",
            self.default_maker_order_delay_ms,
        )
        self.taker_order_delay_sec = self._delay_sec(
            "taker_order_delay_ms",
            self.default_taker_order_delay_ms,
        )
        # client_oid -> perf_counter() at which the order reaches matching.
        self._delayed_commits: dict[str, float] = {}

    def _delay_sec(self, key: str, default_ms: float) -> float:
        value = self._finite_nonnegative(
            self.paper_config.get(key, default_ms),
            default_ms,
        )
        return value / 1000.0

    # Speed bump --------------------------------------------------------

    def _order_delay_sec(self, request: OrderRequest) -> float:
        """Maker delay for orders that rest, taker delay for ones that take."""
        if request.order_type == "MARKET" or request.time_in_force in {
            TIF_IOC,
            TIF_FOK,
        }:
            return self.taker_order_delay_sec
        if request.time_in_force in {TIF_GTX, TIF_RPI}:
            return self.maker_order_delay_sec
        book = self._venue_state.books.get(request.symbol)
        if book is not None and book.bids and book.asks and self._would_cross(
            request,
            float(book.get_best_bid()[0]),
            float(book.get_best_ask()[0]),
        ):
            return self.taker_order_delay_sec
        return self.maker_order_delay_sec

    def _commit_staged_order(self, client_oid: str):
        """Hold a committed order until its speed bump has elapsed.

        The order stays STAGED meanwhile, so a cancel issued during the delay
        uses the existing deferred-cancel path, and the parent commit then
        revalidates (post-only would-cross) and matches against the book as
        it is when the order actually reaches the venue.
        """
        client_oid = str(client_oid or "")
        order = self._venue_state.orders.get(client_oid)
        if order is None or order.status != "STAGED":
            return super()._commit_staged_order(client_oid)
        if client_oid in self._delayed_commits:
            return True
        delay_sec = self._order_delay_sec(order.request)
        if delay_sec <= 0.0:
            return super()._commit_staged_order(client_oid)
        self._delayed_commits[client_oid] = time.perf_counter() + delay_sec
        return True

    def _check_dms_deadlines(self):
        self._release_delayed_commits()
        super()._check_dms_deadlines()

    def _worker_idle_timeout_sec(self) -> float:
        if not self._delayed_commits:
            return WORKER_IDLE_SEC
        next_release = min(self._delayed_commits.values())
        return min(
            WORKER_IDLE_SEC,
            max(0.001, next_release - time.perf_counter()),
        )

    def _release_delayed_commits(self) -> None:
        if not self._delayed_commits:
            return
        now = time.perf_counter()
        due = sorted(
            (
                (release_at, client_oid)
                for client_oid, release_at in self._delayed_commits.items()
                if release_at <= now
            ),
        )
        for _release_at, client_oid in due:
            self._delayed_commits.pop(client_oid, None)
            super()._commit_staged_order(client_oid)

    # Public market transport -------------------------------------------

    def _on_venue_record(self, stream, data, *, expected_generation=None):
        if not self._book_generation_is_current(expected_generation):
            return
        (
            received_timestamp,
            received_monotonic,
            corrected_received_timestamp,
            clock_offset_ms,
        ) = time_service.capture_timestamp()
        try:
            self._handle_market_message(
                stream,
                data,
                received_timestamp=received_timestamp,
                received_monotonic=received_monotonic,
                clock_offset_ms=clock_offset_ms,
                corrected_received_timestamp=corrected_received_timestamp,
                expected_generation=expected_generation,
            )
        except Exception as exc:
            if self._book_generation_is_current(expected_generation):
                self._fault(
                    f"WS_HANDLER_FAILURE:PUBLIC:{type(exc).__name__}:{exc}"
                )

    def _record_callback(self, generation: int):
        return lambda stream, data: self._on_venue_record(
            stream,
            data,
            expected_generation=generation,
        )

    def _error_callback(self, generation: int):
        return lambda error: self.on_ws_error(
            error,
            expected_generation=generation,
        )

    def _wait_for_initial_marks(self, generation: int) -> bool:
        # These venues have no premium-index REST endpoint; marks arrive on
        # the same public stream as the book.
        deadline = time.perf_counter() + self.mark_startup_timeout_sec
        missing = set(self.symbols)
        while time.perf_counter() < deadline:
            if not self._book_generation_is_current(generation):
                return False
            with self._book_feed_state.lock:
                seen = set(self._book_feed_state.last_ws_mark_received_monotonic)
            missing = set(self.symbols) - seen
            if not missing:
                return True
            time.sleep(0.05)
        logger.error(
            f"[{self.gateway_name}] Initial mark-price readiness timed out; "
            f"missing={sorted(missing)}"
        )
        return False

    def _start_mark_fallback(self, generation: int) -> None:
        # No REST mark source; a silent mark channel is caught by the order
        # validator's mark-freshness check instead.
        self._mark_fallback_thread = None


__all__ = [
    "STREAM_NAME",
    "VenuePaperGateway",
    "VenuePublicWs",
    "VenueSnapshotSource",
]
