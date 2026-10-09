"""Paper gateway on Lighter public market data.

The local venue (staging/commit barrier, matching, ledger, private-query
shapes) is the Binance Paper venue unchanged; only the public market
transport differs. Lighter frames are translated into the same depth, trade
and mark records, so book continuity, stale-event faults and recovery run
through the existing Paper book synchronizer.
"""

from __future__ import annotations

import time

from data.ref_data import ref_data_manager
from event.type import TIF_FOK, TIF_GTX, TIF_IOC, TIF_RPI, OrderRequest
from gateway.binance.paper_gateway import BinancePaperGateway
from infrastructure.logger import logger
from infrastructure.time_service import time_service

from .public_ws import LighterPublicWs

# Lighter Standard accounts hold orders in a speed bump before matching.
DEFAULT_MAKER_ORDER_DELAY_MS = 200.0
DEFAULT_TAKER_ORDER_DELAY_MS = 300.0


class _LighterSnapshotSource:
    """Stand-in for the Binance public REST client used by the Paper venue."""

    def __init__(self, gateway: "LighterPaperGateway"):
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


class LighterPaperGateway(BinancePaperGateway):
    """Lighter production-public-data gateway with a local paper venue."""

    def __init__(
        self,
        event_engine,
        config: dict,
        market_data_config: dict | None = None,
    ):
        super().__init__(event_engine, config, market_data_config)
        self.gateway_name = "LIGHTER_PAPER"
        self.rest = _LighterSnapshotSource(self)
        self.maker_order_delay_sec = self._finite_nonnegative(
            self.paper_config.get(
                "maker_order_delay_ms",
                DEFAULT_MAKER_ORDER_DELAY_MS,
            ),
            DEFAULT_MAKER_ORDER_DELAY_MS,
        ) / 1000.0
        self.taker_order_delay_sec = self._finite_nonnegative(
            self.paper_config.get(
                "taker_order_delay_ms",
                DEFAULT_TAKER_ORDER_DELAY_MS,
            ),
            DEFAULT_TAKER_ORDER_DELAY_MS,
        ) / 1000.0
        # client_oid -> perf_counter() at which the order reaches matching.
        self._delayed_commits: dict[str, float] = {}

    # Speed bump --------------------------------------------------------

    def _order_delay_sec(self, request: OrderRequest) -> float:
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

    def _market_ids(self) -> dict[str, int]:
        market_ids = {}
        for symbol in self.symbols:
            info = ref_data_manager.get_info(symbol)
            market_id = getattr(info, "market_id", None)
            if market_id is None:
                raise ValueError(f"No Lighter market id for {symbol}")
            market_ids[symbol] = int(market_id)
        return market_ids

    def _new_public_ws(self, generation: int):
        return LighterPublicWs(
            lambda stream, data: self._on_lighter_record(
                stream,
                data,
                expected_generation=generation,
            ),
            lambda error: self.on_ws_error(
                error,
                expected_generation=generation,
            ),
            market_ids=self._market_ids(),
        )

    def _on_lighter_record(self, stream, data, *, expected_generation=None):
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

    def _wait_for_initial_marks(self, generation: int) -> bool:
        # Lighter has no premium-index REST endpoint; marks arrive on the
        # market_stats channel of the same stream.
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
            "[LIGHTER_PAPER] Initial mark-price readiness timed out; "
            f"missing={sorted(missing)}"
        )
        return False

    def _start_mark_fallback(self, generation: int) -> None:
        # No REST mark source; a silent market_stats channel is caught by
        # the order validator's mark-freshness check instead.
        self._mark_fallback_thread = None


__all__ = ["LighterPaperGateway"]
