"""Generation-safe Binance order-book synchronization and recovery."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from data.orderbook import LocalOrderBook
from event.type import OrderBookGapError


BookRecovery = tuple[str, int, int, str]


@dataclass(slots=True)
class BinanceOrderBookConfig:
    """Runtime limits owned by the order-book controller."""

    max_buffer: int = 2048
    resync_max_attempts: int = 3
    resync_retry_sec: float = 0.25
    max_recovery_threads: int = 4
    recovery_join_timeout_sec: float = 3.0
    publish_depth_levels: int = 5
    emit_full_book: bool = False
    max_levels_per_side: int = 4096
    max_delta_levels_per_side: int = 2048


@dataclass(frozen=True, slots=True)
class BinanceOrderBookDependencies:
    """Callbacks at the controller boundary.

    Callbacks which intentionally pass through the gateway facade are kept
    explicit. This preserves the gateway's patchable private API without
    giving the controller generic access to gateway state.
    """

    create_orderbook: Callable[[str], LocalOrderBook]
    fetch_snapshot: Callable[[str], Any]
    publish_orderbook: Callable[[Any, int | None], Any]
    emit_health: Callable[[str], None]
    resync_book: Callable[..., bool]
    launch_recovery: Callable[[BookRecovery], bool]
    recover_orderbook: Callable[[str, int, int], None]
    log_info: Callable[[str], None]
    log_critical: Callable[[str], None]
    thread_factory: Callable[..., threading.Thread] = threading.Thread
    monotonic: Callable[[], float] = time.perf_counter


class BinanceOrderBookController:
    """Own Binance public-book state, recovery ownership, and workers."""

    def __init__(
        self,
        dependencies: BinanceOrderBookDependencies,
        config: BinanceOrderBookConfig | None = None,
    ) -> None:
        self.dependencies = dependencies
        self.config = config or BinanceOrderBookConfig()
        self.lock = threading.RLock()
        self.orderbooks: dict[str, LocalOrderBook] = {}
        self.buffers: dict[str, list[dict[str, Any]] | None] = {}
        self.resyncing: set[str] = set()
        self.recovery_generations: dict[str, int] = {}
        self.recovery_tokens: dict[str, int] = {}
        self.recovery_token = 0
        self.generation = 0
        self.recovery_threads: set[threading.Thread] = set()
        self.recovery_stop = threading.Event()

    def begin_generation_locked(self, symbols: list[str]) -> int:
        """Install fresh books for a new transport generation.

        The caller holds ``lock`` while coordinating the websocket and
        gateway lifecycle. The lock is re-entrant so direct use is also safe.
        """

        self.recovery_stop.clear()
        self.generation += 1
        generation = self.generation
        self.resyncing.clear()
        self.recovery_generations.clear()
        self.recovery_tokens.clear()
        for symbol in symbols:
            self.orderbooks[symbol] = self.dependencies.create_orderbook(symbol)
            self.buffers[symbol] = []
        return generation

    def begin_shutdown_locked(self) -> int:
        """Invalidate every recovery owner and interrupt retry waits."""

        self.recovery_stop.set()
        self.generation += 1
        self.resyncing.clear()
        self.recovery_generations.clear()
        self.recovery_tokens.clear()
        return self.generation

    def symbol_ready_locked(self, symbol: str) -> bool:
        return bool(
            symbol not in self.resyncing
            and self.buffers.get(symbol) is None
        )

    def generation_matches_locked(self, expected_generation: int | None) -> bool:
        return bool(
            expected_generation is None
            or self.generation == expected_generation
        )

    def generation_is_current(self, expected_generation: int | None) -> bool:
        if expected_generation is None:
            return True
        with self.lock:
            return self.generation_matches_locked(expected_generation)

    def process_delta(
        self,
        symbol: str,
        raw: dict[str, Any],
        *,
        expected_generation: int | None = None,
    ) -> None:
        recovery = None
        failure = None
        with self.lock:
            if not self.generation_matches_locked(expected_generation):
                return
            try:
                buffered = self.buffers[symbol]
                if buffered is not None:
                    self.orderbooks[symbol].validate_delta_shape(raw)
                    if len(buffered) >= self.config.max_buffer:
                        raise OrderBookGapError(
                            f"book buffer overflow for {symbol}"
                        )
                    buffered.append(raw)
                    return

                book = self.orderbooks[symbol]
                book.process_delta(raw)
                data = book.generate_event_data()
                if data:
                    self.dependencies.publish_orderbook(
                        data,
                        expected_generation,
                    )
            except (KeyError, ValueError, OrderBookGapError) as exc:
                failure = exc
                # Claim a new owner under the same lock that observed the gap.
                # An old worker cannot publish a stale clear after this point.
                recovery = self.begin_recovery_locked(
                    symbol,
                    freeze_reason="FATAL_GAP",
                    expected_generation=expected_generation,
                )

        if failure is not None:
            self.dependencies.log_critical(
                f"[{symbol}] OrderBook integrity failure; "
                f"freezing and resyncing: {failure}"
            )
            if recovery is not None:
                self.dependencies.launch_recovery(recovery)

    def initialize_books(self, symbols: list[str]) -> None:
        for symbol in symbols:
            self.schedule_recovery(symbol)

    def begin_recovery_locked(
        self,
        symbol: str,
        freeze_reason: str = "",
        *,
        expected_generation: int | None = None,
    ) -> BookRecovery | None:
        if not self.generation_matches_locked(expected_generation):
            return None
        # Integrity failures supersede an in-flight owner. Routine duplicate
        # initialization requests retain the existing owner.
        if symbol in self.resyncing and not freeze_reason:
            return None
        generation = self.generation
        self.recovery_token += 1
        recovery_token = self.recovery_token
        self.resyncing.add(symbol)
        self.recovery_generations[symbol] = generation
        self.recovery_tokens[symbol] = recovery_token
        self.orderbooks[symbol] = self.dependencies.create_orderbook(symbol)
        self.buffers[symbol] = []
        return symbol, generation, recovery_token, freeze_reason

    def launch_recovery(self, recovery: BookRecovery) -> bool:
        symbol, generation, recovery_token, freeze_reason = recovery
        with self.lock:
            if not self.owns_recovery_locked(
                symbol,
                generation,
                recovery_token,
            ):
                return False
            if freeze_reason:
                self.dependencies.emit_health(
                    f"FREEZE_SYMBOL:{symbol}:{freeze_reason}:{recovery_token}"
                )
            self.recovery_threads.intersection_update(
                thread for thread in self.recovery_threads if thread.is_alive()
            )
            max_threads = max(1, int(self.config.max_recovery_threads or 1))
            if (
                self.recovery_stop.is_set()
                or len(self.recovery_threads) >= max_threads
            ):
                self.dependencies.log_critical(
                    f"[{symbol}] OrderBook recovery capacity unavailable: "
                    f"active={len(self.recovery_threads)} limit={max_threads}"
                )
                return False
            worker = self.dependencies.thread_factory(
                target=self.run_recovery,
                args=(symbol, generation, recovery_token),
                daemon=True,
                name=f"BinanceBookRecovery-{symbol}",
            )
            self.recovery_threads.add(worker)
            # Starting while ownership is locked closes the shutdown race in
            # which an unstarted registered worker could evade join().
            try:
                worker.start()
            except Exception as exc:
                self.recovery_threads.discard(worker)
                self.dependencies.log_critical(
                    f"[{symbol}] OrderBook recovery worker failed to start: {exc}"
                )
                return False
        return True

    def run_recovery(
        self,
        symbol: str,
        generation: int,
        recovery_token: int,
    ) -> None:
        try:
            self.dependencies.recover_orderbook(
                symbol,
                generation,
                recovery_token,
            )
        finally:
            current = threading.current_thread()
            with self.lock:
                self.recovery_threads.discard(current)

    def recovery_threads_stopped(self) -> bool:
        with self.lock:
            self.recovery_threads.intersection_update(
                thread for thread in self.recovery_threads if thread.is_alive()
            )
            return not self.recovery_threads

    def join_recovery_threads(self) -> bool:
        with self.lock:
            threads = tuple(
                thread
                for thread in self.recovery_threads
                if thread is not threading.current_thread()
                and thread.is_alive()
            )
        timeout_sec = max(
            0.0,
            float(self.config.recovery_join_timeout_sec or 0.0),
        )
        deadline = self.dependencies.monotonic() + timeout_sec
        for thread in threads:
            thread.join(
                timeout=max(
                    0.0,
                    deadline - self.dependencies.monotonic(),
                )
            )
        stopped = self.recovery_threads_stopped()
        if not stopped:
            self.dependencies.log_critical(
                "[BINANCE] OrderBook recovery workers did not stop before timeout"
            )
        return stopped

    def schedule_recovery(
        self,
        symbol: str,
        freeze_reason: str = "",
        *,
        expected_generation: int | None = None,
    ) -> bool:
        with self.lock:
            recovery = self.begin_recovery_locked(
                symbol,
                freeze_reason,
                expected_generation=expected_generation,
            )
        if recovery is None:
            return False
        return self.dependencies.launch_recovery(recovery)

    def recover_orderbook(
        self,
        symbol: str,
        generation: int,
        recovery_token: int,
    ) -> None:
        try:
            for attempt in range(1, self.config.resync_max_attempts + 1):
                if self.recovery_stop.is_set():
                    return
                if self.dependencies.resync_book(
                    symbol,
                    expected_generation=generation,
                    recovery_token=recovery_token,
                ):
                    with self.lock:
                        completed = self.release_recovery_locked(
                            symbol,
                            generation,
                            recovery_token,
                        )
                        if completed:
                            # CLEAR is queued while the lock still orders it
                            # before any later owner can publish a new FREEZE.
                            self.dependencies.emit_health(
                                "CLEAR_SYMBOL:"
                                f"{symbol}:ORDERBOOK_RESYNCED:{recovery_token}"
                            )
                    return
                with self.lock:
                    if not self.owns_recovery_locked(
                        symbol,
                        generation,
                        recovery_token,
                    ):
                        return
                    if attempt >= self.config.resync_max_attempts:
                        break
                    self.orderbooks[symbol] = (
                        self.dependencies.create_orderbook(symbol)
                    )
                    self.buffers[symbol] = []
                retry_delay = self.config.resync_retry_sec * attempt
                if self.recovery_stop.wait(retry_delay):
                    return
            self.dependencies.log_critical(
                f"[{symbol}] OrderBook resync exhausted "
                f"{self.config.resync_max_attempts} attempts; "
                "symbol remains frozen."
            )
        finally:
            with self.lock:
                self.release_recovery_locked(
                    symbol,
                    generation,
                    recovery_token,
                )

    def owns_recovery_locked(
        self,
        symbol: str,
        generation: int,
        recovery_token: int,
    ) -> bool:
        return bool(
            self.generation == generation
            and symbol in self.resyncing
            and self.recovery_generations.get(symbol) == generation
            and self.recovery_tokens.get(symbol) == recovery_token
        )

    def release_recovery_locked(
        self,
        symbol: str,
        generation: int,
        recovery_token: int,
    ) -> bool:
        if not self.owns_recovery_locked(
            symbol,
            generation,
            recovery_token,
        ):
            return False
        self.recovery_generations.pop(symbol, None)
        self.recovery_tokens.pop(symbol, None)
        self.resyncing.discard(symbol)
        return True

    def create_local_orderbook(self, symbol: str) -> LocalOrderBook:
        return LocalOrderBook(
            symbol,
            publish_depth_levels=self.config.publish_depth_levels,
            emit_full_book=self.config.emit_full_book,
            max_levels_per_side=self.config.max_levels_per_side,
            max_delta_levels_per_side=self.config.max_delta_levels_per_side,
        )

    def resync_book(
        self,
        symbol: str,
        *,
        expected_generation: int | None = None,
        recovery_token: int | None = None,
    ) -> bool:
        snapshot = self.dependencies.fetch_snapshot(symbol)
        if not snapshot:
            return False
        try:
            with self.lock:
                if not self.generation_matches_locked(expected_generation):
                    return False
                if (
                    recovery_token is not None
                    and not self.owns_recovery_locked(
                        symbol,
                        expected_generation,
                        recovery_token,
                    )
                ):
                    return False
                book = self.orderbooks[symbol]
                buffered = self.buffers[symbol]
                if buffered is None:
                    return False
                book.init_snapshot(snapshot)
                for message in buffered:
                    book.process_delta(message)
                self.buffers[symbol] = None
                event_book = book.generate_event_data()
                if event_book is not None:
                    self.dependencies.publish_orderbook(
                        event_book,
                        expected_generation,
                    )
        except (KeyError, ValueError, OrderBookGapError) as exc:
            self.dependencies.log_critical(
                f"[{symbol}] Gap during init. Resync failed: {exc}"
            )
            if recovery_token is not None:
                with self.lock:
                    if self.owns_recovery_locked(
                        symbol,
                        expected_generation,
                        recovery_token,
                    ):
                        self.dependencies.emit_health(
                            "FREEZE_SYMBOL:"
                            f"{symbol}:ORDERBOOK_RESYNC_FAILED:{recovery_token}"
                        )
            return False
        self.dependencies.log_info(f"[{symbol}] Initial Sync Done.")
        return True
