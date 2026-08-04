"""Binance listen-key and keepalive lifecycle."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class BinanceUserStreamDependencies:
    create_listen_key: Callable[[], str | None]
    keep_alive_listen_key: Callable[[], object]
    emit_fault: Callable[..., bool]
    is_transport_active: Callable[[], bool]
    is_transport_closing: Callable[[], bool]
    transport_generation_matches_locked: Callable[[int | None], bool]
    transport_lock: object
    log_critical: Callable[[str], None]


class BinanceUserStreamController:
    """Own the user-stream lease and its generation-fenced worker."""

    def __init__(
        self,
        dependencies: BinanceUserStreamDependencies,
        *,
        keep_alive_interval_sec: float = 1800.0,
    ) -> None:
        self.dependencies = dependencies
        self.keep_alive_interval_sec = max(
            0.01,
            float(keep_alive_interval_sec),
        )
        self.listen_key = ""
        self.generation = 0
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._state_lock = threading.RLock()

    def start(
        self,
        ws,
        symbols,
        *,
        transport_generation: int | None = None,
    ) -> bool:
        if ws is None:
            return False

        rejected = False
        with self.dependencies.transport_lock:
            if not self._transport_generation_is_current_locked(
                transport_generation
            ):
                rejected = True
            elif ws.start_market_stream(list(symbols)) is False:
                rejected = True
        if rejected:
            ws.close()
            return False

        listen_key = self.dependencies.create_listen_key()
        if not listen_key:
            ws.close()
            return False

        rejected = False
        with self.dependencies.transport_lock:
            if not self._transport_generation_is_current_locked(
                transport_generation
            ):
                rejected = True
            elif ws.start_user_stream(listen_key) is False:
                rejected = True
            else:
                with self._state_lock:
                    self.stop_event.set()
                    stop_event = threading.Event()
                    self.stop_event = stop_event
                    self.generation += 1
                    generation = self.generation
                    self.listen_key = listen_key
                    thread = threading.Thread(
                        target=self.keep_alive_loop,
                        args=(
                            generation,
                            transport_generation,
                            stop_event,
                        ),
                        daemon=True,
                        name="BinanceListenKeyKeepAlive",
                    )
                    self.thread = thread
                thread.start()
        if rejected:
            ws.close()
            return False
        return True

    def _transport_generation_is_current_locked(
        self,
        transport_generation: int | None,
    ) -> bool:
        return bool(
            self.dependencies.is_transport_active()
            and not self.dependencies.is_transport_closing()
            and self.dependencies.transport_generation_matches_locked(
                transport_generation
            )
        )

    def invalidate(self) -> int:
        with self._state_lock:
            self.generation += 1
            self.stop_event.set()
            return self.generation

    def stop(self, timeout_sec: float = 2.0) -> bool:
        with self._state_lock:
            self.stop_event.set()
            thread = self.thread
        if (
            thread is not None
            and thread is not threading.current_thread()
            and thread.is_alive()
        ):
            thread.join(timeout=max(0.0, float(timeout_sec)))
        stopped = thread is None or not thread.is_alive()
        if not stopped:
            self.dependencies.log_critical(
                "[BINANCE] Listen-key keepalive worker did not stop"
            )
        return stopped

    def keep_alive_loop(
        self,
        generation: int,
        transport_generation: int | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        stop_event = stop_event or threading.Event()
        while (
            self.dependencies.is_transport_active()
            and generation == self.generation
        ):
            if stop_event.wait(self.keep_alive_interval_sec):
                return
            if (
                not self.dependencies.is_transport_active()
                or generation != self.generation
            ):
                return
            if not self.keep_alive_once(
                generation,
                transport_generation=transport_generation,
            ):
                return

    def keep_alive_once(
        self,
        generation: int,
        *,
        transport_generation: int | None = None,
    ) -> bool:
        with self.dependencies.transport_lock:
            if (
                self.dependencies.is_transport_closing()
                or not self.dependencies.is_transport_active()
                or generation != self.generation
                or not self.dependencies.transport_generation_matches_locked(
                    transport_generation
                )
            ):
                return False

        try:
            response = self.dependencies.keep_alive_listen_key()
            status_code = getattr(response, "status_code", None)
            if status_code == 200:
                return True
            detail = f"status={status_code or 'unavailable'}"
        except Exception as exc:
            detail = f"{type(exc).__name__}:{exc}"

        self.dependencies.emit_fault(
            "USER_STREAM_KEEPALIVE_FAILED",
            detail,
            expected_generation=transport_generation,
            expected_keep_alive_generation=generation,
        )
        return False
