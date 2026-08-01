"""Order-book synchronization and recovery policy for Binance Paper."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Protocol

from data.orderbook import LocalOrderBook
from event.type import (
    Event,
    OrderBook,
    OrderBookGapError,
    EVENT_ORDERBOOK,
    EVENT_SYSTEM_HEALTH,
)
from infrastructure.logger import logger


class PaperBookSyncOwner(Protocol):
    """Gateway state and callbacks required by the book synchronizer."""

    symbols: list[str]
    orderbooks: dict[str, LocalOrderBook]
    ws_buffer: dict[str, list[dict] | None]
    book_resyncing: set[str]
    book_recovery_generation: dict[str, int]
    book_recovery_tokens: dict[str, int]
    _book_recovery_token: int
    _book_generation: int
    _book_lock: threading.RLock
    _book_recovery_threads: set[threading.Thread]
    _book_recovery_stop: threading.Event
    _last_ws_mark_received_monotonic: dict[str, float]
    publish_depth_levels: int
    emit_full_orderbook_events: bool
    max_orderbook_levels_per_side: int
    max_delta_levels_per_side: int
    max_book_buffer: int
    max_book_recovery_threads: int
    book_recovery_join_timeout_sec: float
    rest: Any
    event_engine: Any

    def _book_generation_matches_locked(self, expected_generation): ...

    def _book_generation_is_current(self, expected_generation): ...

    def _owns_book_recovery_locked(
        self,
        symbol,
        generation,
        recovery_token,
    ): ...

    def _release_book_recovery_locked(
        self,
        symbol,
        generation,
        recovery_token,
    ): ...

    def _begin_book_recovery_locked(
        self,
        symbol,
        freeze_reason="",
        *,
        expected_generation=None,
    ): ...

    def _launch_book_recovery(self, recovery): ...

    def _run_book_recovery(self, symbol, generation, recovery_token): ...

    def _recover_orderbook(self, symbol, generation, recovery_token): ...

    def _resync_book(
        self,
        symbol,
        *,
        expected_generation=None,
        recovery_token=None,
    ): ...

    def _publish_book_update(self, generation, **kwargs): ...

    def _full_matching_book(self, book): ...

    def _submit_worker(self, kind, payload): ...

    def _stamp_market_dispatch(self, data): ...

    def on_market_data(self, event_type, data): ...

    def _fault(self, reason): ...


class PaperBookSynchronizer:
    """Maintain a generation-safe public book and recover sequence gaps."""

    __slots__ = ("_owner",)

    def __init__(self, owner: PaperBookSyncOwner):
        self._owner = owner

    def generation_matches_locked(self, expected_generation):
        owner = self._owner
        return bool(
            expected_generation is None
            or owner._book_generation == expected_generation
        )

    def generation_is_current(self, expected_generation):
        owner = self._owner
        with owner._book_lock:
            return owner._book_generation_matches_locked(expected_generation)

    def invalidate_lifecycle(self):
        owner = self._owner
        with owner._book_lock:
            owner._book_generation += 1
            owner.book_resyncing.clear()
            owner.book_recovery_generation.clear()
            owner.book_recovery_tokens.clear()
            return owner._book_generation

    def reset_books(self):
        owner = self._owner
        with owner._book_lock:
            owner._book_generation += 1
            generation = owner._book_generation
            owner.orderbooks = {
                symbol: LocalOrderBook(
                    symbol,
                    publish_depth_levels=owner.publish_depth_levels,
                    emit_full_book=owner.emit_full_orderbook_events,
                    max_levels_per_side=owner.max_orderbook_levels_per_side,
                    max_delta_levels_per_side=owner.max_delta_levels_per_side,
                )
                for symbol in owner.symbols
            }
            owner.ws_buffer = {symbol: [] for symbol in owner.symbols}
            owner.book_resyncing.clear()
            owner.book_recovery_generation.clear()
            owner.book_recovery_tokens.clear()
            owner._last_ws_mark_received_monotonic.clear()
            return generation

    def resync_book(
        self,
        symbol: str,
        *,
        expected_generation=None,
        recovery_token=None,
    ):
        owner = self._owner
        snapshot = owner.rest.get_depth_snapshot(symbol)
        if not snapshot:
            return False
        try:
            with owner._book_lock:
                if not owner._book_generation_matches_locked(
                    expected_generation
                ):
                    return False
                generation = owner._book_generation
                if (
                    recovery_token is not None
                    and not owner._owns_book_recovery_locked(
                        symbol,
                        generation,
                        recovery_token,
                    )
                ):
                    return False
                book = owner.orderbooks[symbol]
                book.init_snapshot(snapshot)
                buffered = list(owner.ws_buffer.get(symbol) or [])
                for delta in buffered:
                    book.process_delta(delta)
                owner.ws_buffer[symbol] = None
                event_book = book.generate_event_data()
                matching_book = owner._full_matching_book(book)
        except (KeyError, ValueError, OrderBookGapError) as exc:
            logger.error(f"[BINANCE_PAPER] Book sync failed for {symbol}: {exc}")
            return False

        return owner._publish_book_update(
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
        owner = self._owner
        processing_generation = expected_generation
        recovery = None
        gap_failure = None
        other_failure = None
        with owner._book_lock:
            try:
                if not owner._book_generation_matches_locked(
                    expected_generation
                ):
                    return
                processing_generation = owner._book_generation
                buffered = owner.ws_buffer.get(symbol)
                if buffered is not None:
                    owner.orderbooks[symbol].validate_delta_shape(delta)
                    if len(buffered) >= owner.max_book_buffer:
                        raise RuntimeError(
                            f"book buffer overflow for {symbol}"
                        )
                    buffered.append(delta)
                    return
                book = owner.orderbooks[symbol]
                book.process_delta(delta)
                event_book = book.generate_event_data()
                matching_book = owner._full_matching_book(book)
            except OrderBookGapError as exc:
                gap_failure = exc
                # Claim the new recovery owner under the lock that observed
                # the gap so an older worker cannot emit a stale clear.
                recovery = owner._begin_book_recovery_locked(
                    symbol,
                    freeze_reason="FATAL_GAP",
                    expected_generation=processing_generation,
                )
            except Exception as exc:
                other_failure = exc

        if gap_failure is not None:
            if recovery is not None:
                owner._launch_book_recovery(recovery)
            return
        if other_failure is not None:
            if owner._book_generation_is_current(processing_generation):
                owner._fault(
                    f"WS_HANDLER_FAILURE:PUBLIC_BOOK:{symbol}:"
                    f"{type(other_failure).__name__}:{other_failure}"
                )
            return

        owner._publish_book_update(
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
        owner = self._owner
        # Keep validation and publication ordered against lifecycle reset.
        with owner._book_lock:
            if owner._book_generation != generation:
                return False
            if owner.orderbooks.get(symbol) is not expected_book:
                return False
            if (
                expected_recovery_token is not None
                and not owner._owns_book_recovery_locked(
                    symbol,
                    generation,
                    expected_recovery_token,
                )
            ):
                return False
            if matching_book is not None:
                if not owner._submit_worker(
                    "book",
                    (generation, matching_book),
                ):
                    return False
            if event_book is not None:
                owner._stamp_market_dispatch(event_book)
                owner.on_market_data(EVENT_ORDERBOOK, event_book)
            return True

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
            datetime=datetime.fromtimestamp(received_at),
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
        owner = self._owner
        return bool(
            owner._book_generation == generation
            and symbol in owner.book_resyncing
            and owner.book_recovery_generation.get(symbol) == generation
            and owner.book_recovery_tokens.get(symbol) == recovery_token
        )

    def release_recovery_locked(self, symbol, generation, recovery_token):
        owner = self._owner
        if not owner._owns_book_recovery_locked(
            symbol,
            generation,
            recovery_token,
        ):
            return False
        owner.book_recovery_generation.pop(symbol, None)
        owner.book_recovery_tokens.pop(symbol, None)
        owner.book_resyncing.discard(symbol)
        return True

    def schedule_recovery(
        self,
        symbol: str,
        freeze_reason: str = "",
        *,
        expected_generation=None,
    ):
        owner = self._owner
        with owner._book_lock:
            recovery = owner._begin_book_recovery_locked(
                symbol,
                freeze_reason,
                expected_generation=expected_generation,
            )
        if recovery is None:
            return False
        owner._launch_book_recovery(recovery)
        return True

    def begin_recovery_locked(
        self,
        symbol: str,
        freeze_reason: str = "",
        *,
        expected_generation=None,
    ):
        owner = self._owner
        if not owner._book_generation_matches_locked(expected_generation):
            return None
        if symbol in owner.book_resyncing and not freeze_reason:
            return None
        generation = owner._book_generation
        owner._book_recovery_token += 1
        recovery_token = owner._book_recovery_token
        owner.book_resyncing.add(symbol)
        owner.book_recovery_generation[symbol] = generation
        owner.book_recovery_tokens[symbol] = recovery_token
        owner.orderbooks[symbol] = LocalOrderBook(
            symbol,
            publish_depth_levels=owner.publish_depth_levels,
            emit_full_book=owner.emit_full_orderbook_events,
            max_levels_per_side=owner.max_orderbook_levels_per_side,
            max_delta_levels_per_side=owner.max_delta_levels_per_side,
        )
        owner.ws_buffer[symbol] = []
        return symbol, generation, recovery_token, freeze_reason

    def launch_recovery(self, recovery):
        owner = self._owner
        symbol, generation, recovery_token, freeze_reason = recovery
        with owner._book_lock:
            if not owner._owns_book_recovery_locked(
                symbol,
                generation,
                recovery_token,
            ):
                return False
            if freeze_reason:
                owner.event_engine.put(
                    Event(
                        EVENT_SYSTEM_HEALTH,
                        f"FREEZE_SYMBOL:{symbol}:{freeze_reason}:"
                        f"{recovery_token}",
                    )
                )
            threads = owner._book_recovery_threads
            threads.intersection_update(
                thread for thread in threads if thread.is_alive()
            )
            if (
                owner._book_recovery_stop.is_set()
                or len(threads) >= owner.max_book_recovery_threads
            ):
                logger.critical(
                    "[BINANCE_PAPER] OrderBook recovery capacity unavailable: "
                    f"active={len(threads)} "
                    f"limit={owner.max_book_recovery_threads}"
                )
                return False
            thread = threading.Thread(
                target=owner._run_book_recovery,
                args=(symbol, generation, recovery_token),
                daemon=True,
                name=f"PaperBookRecovery-{symbol}",
            )
            threads.add(thread)
        thread.start()
        return True

    def run_recovery(self, symbol, generation, recovery_token):
        owner = self._owner
        try:
            owner._recover_orderbook(symbol, generation, recovery_token)
        finally:
            current = threading.current_thread()
            with owner._book_lock:
                owner._book_recovery_threads.discard(current)

    def recovery_threads_stopped(self) -> bool:
        owner = self._owner
        with owner._book_lock:
            owner._book_recovery_threads.intersection_update(
                thread
                for thread in owner._book_recovery_threads
                if thread.is_alive()
            )
            return not owner._book_recovery_threads

    def join_recovery_threads(self) -> bool:
        owner = self._owner
        with owner._book_lock:
            threads = tuple(
                thread
                for thread in owner._book_recovery_threads
                if thread is not threading.current_thread()
                and thread.is_alive()
            )
        deadline = time.perf_counter() + owner.book_recovery_join_timeout_sec
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.perf_counter()))
        stopped = owner._book_recovery_threads_stopped()
        if not stopped:
            logger.critical(
                "[BINANCE_PAPER] OrderBook recovery workers did not stop "
                "before timeout"
            )
        return stopped

    def recover_orderbook(self, symbol, generation, recovery_token):
        owner = self._owner
        try:
            if owner._book_recovery_stop.is_set():
                return
            ok = owner._resync_book(
                symbol,
                expected_generation=generation,
                recovery_token=recovery_token,
            )
            if ok:
                with owner._book_lock:
                    completed = owner._release_book_recovery_locked(
                        symbol,
                        generation,
                        recovery_token,
                    )
                    if completed:
                        owner.event_engine.put(
                            Event(
                                EVENT_SYSTEM_HEALTH,
                                f"CLEAR_SYMBOL:{symbol}:"
                                f"ORDERBOOK_RESYNCED:{recovery_token}",
                            )
                        )
                return
            with owner._book_lock:
                if owner._owns_book_recovery_locked(
                    symbol,
                    generation,
                    recovery_token,
                ):
                    owner._fault(
                        "WS_HANDLER_FAILURE:PUBLIC_BOOK_RESYNC_FAILED:"
                        f"{symbol}"
                    )
        finally:
            with owner._book_lock:
                owner._release_book_recovery_locked(
                    symbol,
                    generation,
                    recovery_token,
                )
