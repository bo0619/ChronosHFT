"""Generation-fenced Binance WebSocket decoding and event routing."""

from __future__ import annotations

import json
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass

from event.type import EVENT_ORDERBOOK

from .account_stream import BinanceAccountStreamParser
from .market_stream import BinanceMarketStreamParser


@dataclass(frozen=True)
class BinanceWebSocketDependencies:
    gateway_name: Callable[[], str]
    capture_timestamp: Callable[[], tuple]
    clock_offset_ms: Callable[[], float]
    generation_is_current: Callable[[int | None], bool]
    emit_fault: Callable[..., bool]
    tracked_symbols: Callable[[], tuple[str, ...] | list[str]]
    latency_stats: Callable[[], MutableMapping[str, float]]
    max_ingress_age_ms: Callable[[], float]
    dispatch_transport: Callable[..., bool]
    dispatch_market_data: Callable[..., bool]
    process_book: Callable[..., object]
    on_order_update: Callable[[object], None]
    on_account_update: Callable[[object], None]
    on_log: Callable[[str, str], None]
    log_warning: Callable[[str], None]
    log_error: Callable[[str], None]
    wall_time: Callable[[], float]
    monotonic: Callable[[], float]


class BinanceWebSocketDispatcher:
    """Parse and route one transport generation without owning transport state."""

    def __init__(self, dependencies: BinanceWebSocketDependencies) -> None:
        self.dependencies = dependencies

    def on_message(self, raw_msg, *, expected_generation=None) -> None:
        dependencies = self.dependencies
        if not dependencies.generation_is_current(expected_generation):
            return
        (
            received_timestamp,
            received_monotonic,
            corrected_received_timestamp,
            clock_offset_ms,
        ) = dependencies.capture_timestamp()
        try:
            msg = json.loads(raw_msg)
        except Exception as exc:
            dependencies.emit_fault(
                "WS_PARSE_ERROR",
                str(exc),
                raw_msg,
                expected_generation=expected_generation,
            )
            return

        try:
            event_type = msg.get("e")
            if event_type == "ORDER_TRADE_UPDATE":
                self.handle_user_update(
                    msg,
                    received_timestamp=received_timestamp,
                    received_monotonic=received_monotonic,
                    corrected_received_timestamp=corrected_received_timestamp,
                    clock_offset_ms=clock_offset_ms,
                    expected_generation=expected_generation,
                )
                return
            if event_type == "ACCOUNT_UPDATE":
                self.handle_account_update(
                    msg,
                    received_timestamp=received_timestamp,
                    received_monotonic=received_monotonic,
                    corrected_received_timestamp=corrected_received_timestamp,
                    clock_offset_ms=clock_offset_ms,
                    expected_generation=expected_generation,
                )
                return
            if event_type == "listenKeyExpired":
                dependencies.emit_fault(
                    "USER_STREAM_EXPIRED",
                    "listen key expired",
                    msg,
                    expected_generation=expected_generation,
                )
                return
            if "stream" in msg:
                self.handle_market_update(
                    msg,
                    received_timestamp=received_timestamp,
                    received_monotonic=received_monotonic,
                    clock_offset_ms=clock_offset_ms,
                    corrected_received_timestamp=corrected_received_timestamp,
                    expected_generation=expected_generation,
                )
                return
            if self.is_control_message(msg):
                return
            dependencies.log_warning(
                f"[{dependencies.gateway_name()}] "
                f"Ignoring unsupported WS payload: {msg}"
            )
        except Exception as exc:
            dependencies.emit_fault(
                "WS_HANDLER_FAILURE",
                str(exc),
                msg,
                expected_generation=expected_generation,
            )

    def on_error(self, err_msg, *, expected_generation=None) -> None:
        dependencies = self.dependencies
        if not dependencies.generation_is_current(expected_generation):
            return
        if isinstance(err_msg, dict):
            stream = str(err_msg.get("stream", "WS") or "WS")
            kind = str(err_msg.get("kind", "error") or "error").lower()
            detail = str(err_msg.get("detail", "") or "")
            if kind in {"transport_drop", "remote_close"}:
                reason = f"{stream}:{detail}" if detail else stream
                dependencies.emit_fault(
                    "WS_TRANSPORT_DROP",
                    reason,
                    expected_generation=expected_generation,
                )
                return
            rendered = (
                f"[{stream}] {kind}: {detail}"
                if detail
                else f"[{stream}] {kind}"
            )
            dependencies.log_error(
                f"[{dependencies.gateway_name()}] {rendered}"
            )
            dependencies.on_log(rendered, "ERROR")
            return

        dependencies.log_error(
            f"[{dependencies.gateway_name()}] {err_msg}"
        )
        dependencies.on_log(err_msg, "ERROR")

    @staticmethod
    def is_control_message(msg) -> bool:
        return isinstance(msg, dict) and "result" in msg and "id" in msg

    def handle_user_update(
        self,
        msg,
        *,
        received_timestamp: float = None,
        received_monotonic: float = None,
        corrected_received_timestamp: float = None,
        clock_offset_ms: float = None,
        expected_generation=None,
    ) -> None:
        dependencies = self.dependencies
        update = BinanceAccountStreamParser.parse_order_update(
            msg,
            received_timestamp=received_timestamp,
            received_monotonic=received_monotonic,
            corrected_received_timestamp=corrected_received_timestamp,
            clock_offset_ms=clock_offset_ms,
            now=dependencies.wall_time,
            monotonic=dependencies.monotonic,
        )
        dependencies.dispatch_transport(
            expected_generation,
            dependencies.on_order_update,
            update,
        )

    def handle_account_update(
        self,
        msg,
        *,
        received_timestamp: float = None,
        received_monotonic: float = None,
        corrected_received_timestamp: float = None,
        clock_offset_ms: float = None,
        expected_generation=None,
    ) -> None:
        dependencies = self.dependencies
        update = BinanceAccountStreamParser.parse_account_update(
            msg,
            tracked_symbols=dependencies.tracked_symbols(),
            received_timestamp=received_timestamp,
            received_monotonic=received_monotonic,
            corrected_received_timestamp=corrected_received_timestamp,
            clock_offset_ms=clock_offset_ms,
            now=dependencies.wall_time,
            monotonic=dependencies.monotonic,
        )
        dependencies.dispatch_transport(
            expected_generation,
            dependencies.on_account_update,
            update,
        )

    def handle_market_update(
        self,
        msg,
        *,
        received_timestamp: float = None,
        received_monotonic: float = None,
        clock_offset_ms: float = None,
        corrected_received_timestamp: float = None,
        expected_generation=None,
    ) -> None:
        dependencies = self.dependencies
        if not dependencies.generation_is_current(expected_generation):
            return
        effective_clock_offset_ms = float(
            dependencies.clock_offset_ms()
            if clock_offset_ms is None
            else clock_offset_ms
        )
        envelope = BinanceMarketStreamParser.envelope(
            msg,
            received_timestamp=received_timestamp,
            received_monotonic=received_monotonic,
            corrected_received_timestamp=corrected_received_timestamp,
            clock_offset_ms=effective_clock_offset_ms,
            now=dependencies.wall_time,
            monotonic=dependencies.monotonic,
        )
        if envelope.event_time_ms:
            dependencies.latency_stats()["ws_delay"] = (
                envelope.corrected_received_timestamp * 1000.0
                - envelope.event_time_ms
            )
        if envelope.requires_ingress_freshness and self.reject_stale_market_event(
            stream=envelope.stream,
            symbol=envelope.symbol,
            event_time_ms=envelope.event_time_ms,
            corrected_received_timestamp=(
                envelope.corrected_received_timestamp
            ),
            expected_generation=expected_generation,
        ):
            return
        normalized = BinanceMarketStreamParser.normalize(envelope)
        if normalized is None:
            return
        if normalized.event_type == EVENT_ORDERBOOK:
            dependencies.process_book(
                envelope.symbol,
                normalized.payload,
                expected_generation=expected_generation,
            )
            return
        dependencies.dispatch_market_data(
            normalized.event_type,
            normalized.payload,
            expected_generation=expected_generation,
        )

    def reject_stale_market_event(
        self,
        *,
        stream: str,
        symbol: str,
        event_time_ms: int,
        corrected_received_timestamp: float,
        expected_generation=None,
    ) -> bool:
        if event_time_ms <= 0:
            return False
        age_ms = corrected_received_timestamp * 1000.0 - event_time_ms
        max_ingress_age_ms = float(
            self.dependencies.max_ingress_age_ms()
        )
        if abs(age_ms) <= max_ingress_age_ms:
            return False
        stream_kind = (
            "PUBLIC_DEPTH" if "@depth" in stream else "PUBLIC_TRADE"
        )
        self.dependencies.emit_fault(
            "MARKET_DATA_STALE",
            (
                f"{stream_kind}:symbol={symbol}:"
                f"age={age_ms:.1f}ms>{max_ingress_age_ms:.1f}ms"
            ),
            expected_generation=expected_generation,
        )
        return True
