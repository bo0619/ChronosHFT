"""Order-book ownership, synchronization, and recovery for Binance Paper."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from data.orderbook import LocalOrderBook
from event.type import EVENT_ORDERBOOK, OrderBook, OrderBookGapError
from infrastructure.logger import logger

RecoveryClaim = tuple[str, int, int, str]


@dataclass(frozen=True, slots=True)
class PaperBookFeedConfig:
    """Immutable limits used by the public order-book feed."""

    publish_depth_levels: int
    emit_full_orderbook_events: bool
    max_orderbook_levels_per_side: int
    max_delta_levels_per_side: int
    max_book_buffer: int
    max_book_recovery_threads: int
    book_recovery_join_timeout_sec: float


@dataclass(slots=True)
class PaperBookFeedState:
    """Book state owned exclusively by :class:`PaperBookSynchronizer`."""

    symbols: tuple[str, ...] = ()
    orderbooks: dict[str, LocalOrderBook] = field(default_factory=dict)
    ws_buffer: dict[str, list[dict] | None] = field(default_factory=dict)
    resyncing: set[str] = field(default_factory=set)
    recovery_generation: dict[str, int] = field(default_factory=dict)
    recovery_tokens: dict[str, int] = field(default_factory=dict)
    recovery_token: int = 0
    generation: int = 0
    recovery_threads: set[threading.Thread] = field(default_factory=set)
    recovery_stop: threading.Event = field(default_factory=threading.Event)
    last_ws_mark_received_monotonic: dict[str, float] = field(
        default_factory=dict
    )
    lock: Any = field(default_factory=threading.RLock)
    active_dispatches: int = 0
    dispatch_owners: dict[int, int] = field(default_factory=dict)
    lifecycle_transition: bool = False
    pending_invalidation: bool = False
    condition: Any = field(init=False, repr=False)

    def __post_init__(self):
        self.condition = threading.Condition(self.lock)

    def replace_lock(self, lock: Any) -> None:
        if self.active_dispatches or self.lifecycle_transition:
            raise RuntimeError("cannot replace an active Paper book lock")
        self.lock = lock
        self.condition = threading.Condition(lock)


@dataclass(frozen=True, slots=True)
class PaperBookFeedPort:
    """Explicit external effects available to the book feed."""

    fetch_depth_snapshot: Callable[[str], dict | None]
    submit_worker: Callable[[str, object], bool]
    stamp_market_dispatch: Callable[[object], None]
    publish_market_data: Callable[[str, object], None]
    publish_health: Callable[[str], None]
    report_fault: Callable[[str], None]
    launch_recovery: Callable[[RecoveryClaim], bool]
    run_recovery: Callable[[str, int, int], None]
    resync_book: Callable[..., bool]
    publish_book_update: Callable[..., bool]


class PaperBookSynchronizer:
    """Maintain a generation-safe public book through narrow effect ports."""

    __slots__ = ("_config", "_port", "_state")

    def __init__(
        self,
        state: PaperBookFeedState,
        port: PaperBookFeedPort,
        config: PaperBookFeedConfig,
    ):
        self._state = state
        self._port = port
        self._config = config

    @property
    def state(self) -> PaperBookFeedState:
        return self._state

    def generation_matches_locked(self, expected_generation):
        return bool(
            not self._state.lifecycle_transition
            and (
                expected_generation is None
                or self._state.generation == expected_generation
            )
        )

    def generation_is_current(self, expected_generation):
        with self._state.condition:
            return self.generation_matches_locked(expected_generation)

    def claim_dispatch(self, generation, validator=None) -> bool:
        """Claim a generation lease without retaining the physical lock."""
        state = self._state
        with state.condition:
            if not self.generation_matches_locked(generation):
                return False
            if validator is not None and not validator():
                return False
            ident = threading.get_ident()
            state.active_dispatches += 1
            state.dispatch_owners[ident] = state.dispatch_owners.get(ident, 0) + 1
            return True

    def release_dispatch(self) -> None:
        state = self._state
        ident = threading.get_ident()
        with state.condition:
            owned = state.dispatch_owners.get(ident, 0)
            if owned <= 0:
                raise RuntimeError("Paper book dispatch lease released by non-owner")
            if owned == 1:
                state.dispatch_owners.pop(ident, None)
            else:
                state.dispatch_owners[ident] = owned - 1
            state.active_dispatches -= 1
            state.condition.notify_all()

    def _run_lifecycle_transition(self, mutation: Callable[[], Any]):
        state = self._state
        ident = threading.get_ident()
        with state.condition:
            while state.lifecycle_transition:
                state.condition.wait()
            state.lifecycle_transition = True
            owned_here = state.dispatch_owners.get(ident, 0)
            try:
                while state.active_dispatches > owned_here:
                    state.condition.wait()
                try:
                    return mutation()
                finally:
                    if state.pending_invalidation:
                        self._invalidate_state_locked()
                        state.pending_invalidation = False
            finally:
                state.lifecycle_transition = False
                state.condition.notify_all()

    def _invalidate_state_locked(self) -> int:
        state = self._state
        state.generation += 1
        state.resyncing.clear()
        state.recovery_generation.clear()
        state.recovery_tokens.clear()
        return state.generation

    def invalidate_lifecycle(self):
        state = self._state
        ident = threading.get_ident()
        with state.condition:
            if (
                state.lifecycle_transition
                and state.dispatch_owners.get(ident, 0) > 0
            ):
                state.pending_invalidation = True
                return state.generation + 1
        return self._run_lifecycle_transition(self._invalidate_state_locked)

    def reset_books(self, symbols: list[str] | tuple[str, ...] | None = None):
        state = self._state
        config = self._config

        def reset():
            if symbols is not None:
                state.symbols = tuple(str(item).upper() for item in symbols)
            state.generation += 1
            generation = state.generation
            state.orderbooks = {
                symbol: LocalOrderBook(
                    symbol,
                    publish_depth_levels=config.publish_depth_levels,
                    emit_full_book=config.emit_full_orderbook_events,
                    max_levels_per_side=config.max_orderbook_levels_per_side,
                    max_delta_levels_per_side=config.max_delta_levels_per_side,
                )
                for symbol in state.symbols
            }
            state.ws_buffer = {symbol: [] for symbol in state.symbols}
            state.resyncing.clear()
            state.recovery_generation.clear()
            state.recovery_tokens.clear()
            state.last_ws_mark_received_monotonic.clear()
            return generation

        return self._run_lifecycle_transition(reset)

    def resync_book(
        self,
        symbol: str,
        *,
        expected_generation=None,
        recovery_token=None,
    ):
        state = self._state
        snapshot = self._port.fetch_depth_snapshot(symbol)
        if not snapshot:
            return False
        try:
            with state.lock:
                if not self.generation_matches_locked(expected_generation):
                    return False
                generation = state.generation
                if recovery_token is not None and not self.owns_recovery_locked(
                    symbol,
                    generation,
                    recovery_token,
                ):
                    return False
                book = state.orderbooks[symbol]
                book.init_snapshot(snapshot)
                buffered = list(state.ws_buffer.get(symbol) or [])
                for delta in buffered:
                    book.process_delta(delta)
                state.ws_buffer[symbol] = None
                event_book = book.generate_event_data()
                matching_book = self.full_matching_book(book)
        except (KeyError, ValueError, OrderBookGapError) as exc:
            logger.error(f"[BINANCE_PAPER] Book sync failed for {symbol}: {exc}")
            return False

        return self._port.publish_book_update(
            generation,
            symbol=symbol,
            expected_book=book,
            expected_recovery_token=recovery_token,
            event_book=event_book,
            matching_book=matching_book,
        )

    def process_delta(
        self,
        symbol: str,
        delta: dict,
        *,
        expected_generation=None,
    ):
        state = self._state
        processing_generation = expected_generation
        recovery = None
        gap_failure = None
        other_failure = None
        book = None
        event_book = None
        matching_book = None
        with state.lock:
            try:
                if not self.generation_matches_locked(expected_generation):
                    return
                processing_generation = state.generation
                buffered = state.ws_buffer.get(symbol)
                if buffered is not None:
                    state.orderbooks[symbol].validate_delta_shape(delta)
                    if len(buffered) >= self._config.max_book_buffer:
                        raise RuntimeError(f"book buffer overflow for {symbol}")
                    buffered.append(delta)
                    return
                book = state.orderbooks[symbol]
                book.process_delta(delta)
                event_book = book.generate_event_data()
                matching_book = self.full_matching_book(book)
            except OrderBookGapError as exc:
                gap_failure = exc
            except Exception as exc:
                other_failure = exc

        if gap_failure is not None:
            recovery = self.begin_recovery(
                symbol,
                freeze_reason="FATAL_GAP",
                expected_generation=processing_generation,
            )
            if recovery is not None:
                self._port.launch_recovery(recovery)
            return
        if other_failure is not None:
            if self.claim_dispatch(processing_generation):
                try:
                    self._port.report_fault(
                        f"WS_HANDLER_FAILURE:PUBLIC_BOOK:{symbol}:"
                        f"{type(other_failure).__name__}:{other_failure}"
                    )
                finally:
                    self.release_dispatch()
            return

        self._port.publish_book_update(
            processing_generation,
            symbol=symbol,
            expected_book=book,
            event_book=event_book,
            matching_book=matching_book,
        )

    def publish_update(
        self,
        generation,
        *,
        symbol,
        expected_book,
        expected_recovery_token=None,
        event_book,
        matching_book,
    ):
        state = self._state

        def publication_is_current():
            if state.orderbooks.get(symbol) is not expected_book:
                return False
            return bool(
                expected_recovery_token is None
                or self.owns_recovery_locked(
                    symbol,
                    generation,
                    expected_recovery_token,
                )
            )

        if not self.claim_dispatch(generation, publication_is_current):
            return False
        try:
            if matching_book is not None and not self._port.submit_worker(
                "book",
                (generation, matching_book),
            ):
                return False
            if event_book is not None:
                self._port.stamp_market_dispatch(event_book)
                self._port.publish_market_data(EVENT_ORDERBOOK, event_book)
            return True
        finally:
            self.release_dispatch()

    @staticmethod
    def full_matching_book(book: LocalOrderBook):
        if not book.initialized:
            return None
        received_at = float(book.last_received_ts or time.time())
        received_monotonic = float(
            book.last_received_monotonic or time.perf_counter()
        )
        dispatch_timestamp = time.time()
        dispatch_monotonic = time.perf_counter()
        return OrderBook(
            symbol=book.symbol,
            exchange="BINANCE",
            datetime=datetime.fromtimestamp(received_at, tz=timezone.utc),
            bids=dict(book.bids),
            asks=dict(book.asks),
            top_bids=tuple(book.top_bids),
            top_asks=tuple(book.top_asks),
            exchange_timestamp=float(book.last_exchange_ts or 0.0),
            received_timestamp=received_at,
            received_monotonic=received_monotonic,
            dispatch_timestamp=dispatch_timestamp,
            dispatch_monotonic=dispatch_monotonic,
            clock_offset_ms=book.last_clock_offset_ms,
            corrected_received_timestamp=float(
                book.last_corrected_received_ts or 0.0
            ),
            best_bid_price=float(book.best_bid_price or 0.0),
            best_bid_volume=float(book.best_bid_volume or 0.0),
            best_ask_price=float(book.best_ask_price or 0.0),
            best_ask_volume=float(book.best_ask_volume or 0.0),
            depth_levels=max(len(book.bids), len(book.asks)),
        )

    def owns_recovery_locked(self, symbol, generation, recovery_token):
        state = self._state
        return bool(
            state.generation == generation
            and symbol in state.resyncing
            and state.recovery_generation.get(symbol) == generation
            and state.recovery_tokens.get(symbol) == recovery_token
        )

    def release_recovery_locked(self, symbol, generation, recovery_token):
        state = self._state
        if not self.owns_recovery_locked(symbol, generation, recovery_token):
            return False
        state.recovery_generation.pop(symbol, None)
        state.recovery_tokens.pop(symbol, None)
        state.resyncing.discard(symbol)
        return True

    def schedule_recovery(
        self,
        symbol: str,
        freeze_reason: str = "",
        *,
        expected_generation=None,
    ):
        recovery = self.begin_recovery(
            symbol,
            freeze_reason,
            expected_generation=expected_generation,
        )
        if recovery is None:
            return False
        return self._port.launch_recovery(recovery)

    def begin_recovery(
        self,
        symbol: str,
        freeze_reason: str = "",
        *,
        expected_generation=None,
    ):
        return self._run_lifecycle_transition(
            lambda: self.begin_recovery_locked(
                symbol,
                freeze_reason,
                expected_generation=expected_generation,
            )
        )

    def begin_recovery_locked(
        self,
        symbol: str,
        freeze_reason: str = "",
        *,
        expected_generation=None,
    ):
        state = self._state
        config = self._config
        if (
            expected_generation is not None
            and state.generation != expected_generation
        ):
            return None
        if symbol in state.resyncing and not freeze_reason:
            return None
        generation = state.generation
        state.recovery_token += 1
        recovery_token = state.recovery_token
        state.resyncing.add(symbol)
        state.recovery_generation[symbol] = generation
        state.recovery_tokens[symbol] = recovery_token
        state.orderbooks[symbol] = LocalOrderBook(
            symbol,
            publish_depth_levels=config.publish_depth_levels,
            emit_full_book=config.emit_full_orderbook_events,
            max_levels_per_side=config.max_orderbook_levels_per_side,
            max_delta_levels_per_side=config.max_delta_levels_per_side,
        )
        state.ws_buffer[symbol] = []
        return symbol, generation, recovery_token, freeze_reason

    def launch_recovery(self, recovery: RecoveryClaim):
        state = self._state
        symbol, generation, recovery_token, freeze_reason = recovery
        if not self.claim_dispatch(
            generation,
            lambda: self.owns_recovery_locked(
                symbol,
                generation,
                recovery_token,
            ),
        ):
            return False
        try:
            with state.lock:
                threads = state.recovery_threads
                threads.intersection_update(
                    thread for thread in threads if thread.is_alive()
                )
                if (
                    state.recovery_stop.is_set()
                    or len(threads) >= self._config.max_book_recovery_threads
                ):
                    logger.critical(
                        "[BINANCE_PAPER] OrderBook recovery capacity unavailable: "
                        f"active={len(threads)} "
                        f"limit={self._config.max_book_recovery_threads}"
                    )
                    return False
                thread = threading.Thread(
                    target=self._port.run_recovery,
                    args=(symbol, generation, recovery_token),
                    daemon=True,
                    name=f"PaperBookRecovery-{symbol}",
                )
                threads.add(thread)
            if freeze_reason:
                self._port.publish_health(
                    f"FREEZE_SYMBOL:{symbol}:{freeze_reason}:{recovery_token}"
                )
            try:
                thread.start()
            except BaseException:
                with state.lock:
                    state.recovery_threads.discard(thread)
                raise
            return True
        finally:
            self.release_dispatch()

    def run_recovery(self, symbol, generation, recovery_token):
        try:
            self.recover_orderbook(symbol, generation, recovery_token)
        finally:
            current = threading.current_thread()
            with self._state.lock:
                self._state.recovery_threads.discard(current)

    def recovery_threads_stopped(self) -> bool:
        state = self._state
        with state.lock:
            state.recovery_threads.intersection_update(
                thread for thread in state.recovery_threads if thread.is_alive()
            )
            return not state.recovery_threads

    def join_recovery_threads(self) -> bool:
        state = self._state
        with state.lock:
            threads = tuple(
                thread
                for thread in state.recovery_threads
                if thread is not threading.current_thread() and thread.is_alive()
            )
        deadline = (
            time.perf_counter() + self._config.book_recovery_join_timeout_sec
        )
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.perf_counter()))
        stopped = self.recovery_threads_stopped()
        if not stopped:
            logger.critical(
                "[BINANCE_PAPER] OrderBook recovery workers did not stop "
                "before timeout"
            )
        return stopped

    def recover_orderbook(self, symbol, generation, recovery_token):
        state = self._state
        try:
            if state.recovery_stop.is_set():
                return
            ok = self._port.resync_book(
                symbol,
                expected_generation=generation,
                recovery_token=recovery_token,
            )
            if ok:
                completed = self.claim_dispatch(
                    generation,
                    lambda: self.release_recovery_locked(
                        symbol,
                        generation,
                        recovery_token,
                    ),
                )
                if completed:
                    try:
                        self._port.publish_health(
                            f"CLEAR_SYMBOL:{symbol}:"
                            f"ORDERBOOK_RESYNCED:{recovery_token}"
                        )
                    finally:
                        self.release_dispatch()
                return
            owned = self.claim_dispatch(
                generation,
                lambda: self.owns_recovery_locked(
                    symbol,
                    generation,
                    recovery_token,
                ),
            )
            if owned:
                try:
                    self._port.report_fault(
                        "WS_HANDLER_FAILURE:PUBLIC_BOOK_RESYNC_FAILED:"
                        f"{symbol}"
                    )
                finally:
                    self.release_dispatch()
        finally:
            with state.lock:
                self.release_recovery_locked(
                    symbol,
                    generation,
                    recovery_token,
                )


__all__ = [
    "PaperBookFeedConfig",
    "PaperBookFeedPort",
    "PaperBookFeedState",
    "PaperBookSynchronizer",
    "RecoveryClaim",
]
