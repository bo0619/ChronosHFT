"""Binance transport connection and recovery orchestration."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

from event.type import GatewayState


@dataclass(frozen=True)
class BinanceConnectionDependencies:
    transport_lock: object
    begin_generation_locked: Callable[[list[str]], int]
    generation_matches_locked: Callable[[int | None], bool]
    begin_book_shutdown_locked: Callable[[], None]
    resync_book: Callable[..., bool]
    join_book_recovery_threads: Callable[[], bool]
    book_recovery_threads_stopped: Callable[[], bool]
    apply_account_configuration: Callable[[], bool]
    create_ws: Callable[[int], object]
    start_streams: Callable[[object, list[str], int], bool]
    stop_user_stream: Callable[[], bool]
    invalidate_user_stream: Callable[[], int]
    stream_ready_timeout_sec: Callable[[], float]
    set_state: Callable[[GatewayState], None]
    get_state: Callable[[], GatewayState]
    emit_health: Callable[[str], None]
    close_session: Callable[[], None]
    venue_name: Callable[[], str]
    log_info: Callable[[str], None]
    log_warning: Callable[[str], None]
    log_error: Callable[[str], None]


class BinanceConnectionController:
    """Own transport state and generation-fenced connection transactions."""

    def __init__(self, dependencies: BinanceConnectionDependencies) -> None:
        self.dependencies = dependencies
        self.lifecycle_lock = threading.Lock()
        self.active = False
        self.closing = False
        self.ws = None
        self.symbols: list[str] = []

    def connect(self, symbols: list[str]) -> bool:
        with self.lifecycle_lock:
            with self.dependencies.transport_lock:
                if self.closing:
                    return False
            return self.connect_once(symbols)

    def connect_once(self, symbols: list[str]) -> bool:
        self.dependencies.set_state(GatewayState.CONNECTING)
        self.symbols = [str(symbol).upper() for symbol in symbols]

        with self.dependencies.transport_lock:
            if self.closing:
                return False
            self.active = True
            generation = self.dependencies.begin_generation_locked(
                self.symbols
            )

        if not self.dependencies.apply_account_configuration():
            if self.mark_failure_if_current(generation):
                self.dependencies.emit_health(
                    f"FREEZE_VENUE:{self.dependencies.venue_name()}:"
                    "ACCOUNT_CONFIG_FAILED"
                )
            return False

        candidate_ws = self.dependencies.create_ws(generation)
        with self.dependencies.transport_lock:
            if self.closing or not self._generation_matches_locked(generation):
                candidate_ws.close()
                return False
            old_ws = self.ws
            self.ws = candidate_ws
        if old_ws is not None:
            old_ws.close()

        if not self.dependencies.start_streams(
            candidate_ws,
            self.symbols,
            generation,
        ):
            candidate_ws.close()
            if self.mark_failure_if_current(generation, candidate_ws):
                self.dependencies.emit_health(
                    f"FREEZE_VENUE:{self.dependencies.venue_name()}:"
                    "USER_STREAM_START_FAILED"
                )
            return False

        if not candidate_ws.wait_until_connected(
            timeout_sec=self.dependencies.stream_ready_timeout_sec()
        ):
            candidate_ws.close()
            if self.mark_failure_if_current(generation, candidate_ws):
                self.dependencies.emit_health(
                    f"FREEZE_VENUE:{self.dependencies.venue_name()}:"
                    "STREAM_READY_TIMEOUT"
                )
            return False

        for symbol in self.symbols:
            if not self.dependencies.resync_book(
                symbol,
                expected_generation=generation,
            ):
                candidate_ws.close()
                if self.mark_failure_if_current(generation, candidate_ws):
                    self.dependencies.emit_health(
                        f"FREEZE_SYMBOL:{symbol}:ORDERBOOK_STARTUP_FAILED"
                    )
                return False
        with self.dependencies.transport_lock:
            if not self.owns_lifecycle_locked(generation, candidate_ws):
                candidate_ws.close()
                return False
            self.dependencies.set_state(GatewayState.READY)
        return True

    def begin_shutdown(self) -> bool:
        with self.dependencies.transport_lock:
            self.closing = True
            self.active = False
            self.dependencies.invalidate_user_stream()
            self.dependencies.begin_book_shutdown_locked()
            ws = self.ws
        self.dependencies.set_state(GatewayState.DISCONNECTED)
        ws_stopped = True
        if ws:
            ws_stopped = ws.close() is not False
        keep_alive_stopped = self.dependencies.stop_user_stream()
        book_recovery_stopped = (
            self.dependencies.join_book_recovery_threads()
        )
        return bool(
            ws_stopped
            and keep_alive_stopped
            and book_recovery_stopped
        )

    def close(self) -> bool:
        transport_stopped = self.begin_shutdown()
        with self.lifecycle_lock:
            with self.dependencies.transport_lock:
                ws = self.ws
                self.ws = None
            if ws:
                transport_stopped = bool(
                    ws.close() is not False and transport_stopped
                )
        if not self.dependencies.book_recovery_threads_stopped():
            return False
        self.dependencies.close_session()
        self.dependencies.log_info(
            f"[{self.dependencies.venue_name()}] Closed."
        )
        return transport_stopped

    def recover(self, recovery_context=None) -> bool:
        with self.lifecycle_lock:
            with self.dependencies.transport_lock:
                if self.closing or not self.symbols:
                    return False
                self.active = True
                self.dependencies.invalidate_user_stream()
                generation = self.dependencies.begin_generation_locked(
                    self.symbols
                )

            self.dependencies.log_warning(
                f"[{self.dependencies.venue_name()}] "
                "Recovering venue connectivity..."
            )
            recovery_ws = self.dependencies.create_ws(generation)
            with self.dependencies.transport_lock:
                if not self.owns_lifecycle_locked(generation):
                    recovery_ws.close()
                    return False
                old_ws = self.ws
                self.ws = recovery_ws
                self.dependencies.set_state(GatewayState.CONNECTING)
            if old_ws:
                old_ws.close()

            committed = False
            try:
                if not self.dependencies.start_streams(
                    recovery_ws,
                    self.symbols,
                    generation,
                ):
                    self.dependencies.log_error(
                        f"[{self.dependencies.venue_name()}] Recovery failed: "
                        "listen key unavailable"
                    )
                    self.mark_failure_if_current(generation, recovery_ws)
                    return False

                if not recovery_ws.wait_until_connected(
                    timeout_sec=self.dependencies.stream_ready_timeout_sec(),
                ):
                    self.dependencies.log_error(
                        f"[{self.dependencies.venue_name()}] Recovery failed: "
                        "websocket readiness timeout"
                    )
                    self.mark_failure_if_current(generation, recovery_ws)
                    return False

                for symbol in self.symbols:
                    if not self.dependencies.resync_book(
                        symbol,
                        expected_generation=generation,
                    ):
                        self.dependencies.log_error(
                            f"[{self.dependencies.venue_name()}] Recovery "
                            f"failed during book sync: {symbol}"
                        )
                        self.mark_failure_if_current(generation, recovery_ws)
                        return False

                with self.dependencies.transport_lock:
                    if (
                        not self.owns_lifecycle_locked(
                            generation,
                            recovery_ws,
                        )
                        or self.dependencies.get_state() == GatewayState.ERROR
                    ):
                        self.dependencies.log_error(
                            f"[{self.dependencies.venue_name()}] Recovery "
                            "superseded by a newer transport fault"
                        )
                        return False
                    self.dependencies.set_state(GatewayState.READY)
                    if recovery_context:
                        owner = str(recovery_context.get("owner", "") or "")
                        epoch = int(recovery_context.get("epoch", 0) or 0)
                        verification = (
                            f"VERIFY_VENUE:{self.dependencies.venue_name()}:"
                            f"{epoch}:{owner}"
                        )
                    else:
                        verification = (
                            f"VERIFY_VENUE:{self.dependencies.venue_name()}:"
                            "WS_RECOVERED"
                        )
                    self.dependencies.emit_health(verification)
                    committed = True
                self.dependencies.log_info(
                    f"[{self.dependencies.venue_name()}] Transport recovered; "
                    "awaiting OMS truth verification."
                )
                return True
            finally:
                if not committed:
                    recovery_ws.close()
                    with self.dependencies.transport_lock:
                        if self.ws is recovery_ws:
                            self.ws = None

    def owns_lifecycle_locked(
        self,
        generation: int,
        expected_ws=None,
    ) -> bool:
        return bool(
            not self.closing
            and self.active
            and self._generation_matches_locked(generation)
            and (expected_ws is None or self.ws is expected_ws)
        )

    def mark_failure_if_current(
        self,
        generation: int,
        expected_ws=None,
    ) -> bool:
        with self.dependencies.transport_lock:
            if (
                self.closing
                or not self._generation_matches_locked(generation)
                or (expected_ws is not None and self.ws is not expected_ws)
            ):
                return False
            self.active = False
            self.dependencies.set_state(GatewayState.ERROR)
            return True

    def _generation_matches_locked(self, generation: int | None) -> bool:
        return self.dependencies.generation_matches_locked(generation)
