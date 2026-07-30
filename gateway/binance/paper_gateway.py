"""Public-market-data paper gateway for Binance USD-M futures.

The gateway deliberately owns no API credentials and never starts a Binance
user-data stream.  Public market data is combined with a single-writer local
matching/ledger thread which exposes the same private-query shape that the OMS
expects from the live Binance gateway.

An accepted order is first *staged*.  The OMS must call
``commit_order_submission(client_oid)`` only after its command result and
``PENDING_ACK`` state are durable.  No ``NEW`` or fill event can be emitted
before that barrier, which removes the REST-ACK/user-stream race entirely.
"""

from __future__ import annotations

import copy
import json
import math
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import requests

from data.orderbook import LocalOrderBook
from data.ref_data import ref_data_manager
from event.type import (
    AggTradeData,
    CancelRequest,
    CommandOutcome,
    Event,
    ExchangeAccountUpdate,
    ExchangeOrderUpdate,
    GatewayCommandResult,
    GatewayState,
    MarkPriceData,
    OrderBook,
    OrderBookGapError,
    OrderRequest,
    EVENT_AGG_TRADE,
    EVENT_MARK_PRICE,
    EVENT_ORDERBOOK,
    EVENT_SYSTEM_HEALTH,
    TIF_FOK,
    TIF_GTC,
    TIF_GTX,
    TIF_IOC,
    TIF_RPI,
)
from gateway.base_gateway import BaseGateway
from infrastructure.commission_truth import resolve_passive_fee_rate
from infrastructure.logger import logger
from infrastructure.time_service import time_service

from .constants import (
    EP_DEPTH_SNAPSHOT,
    EP_PREMIUM_INDEX,
    EP_RPI_DEPTH,
    REST_URL_MAIN,
)
from .ws_api import BinanceWsApi


_ACTIVE_STATUSES = frozenset({"NEW", "PARTIALLY_FILLED"})
_TERMINAL_STATUSES = frozenset({"FILLED", "CANCELED", "EXPIRED", "REJECTED"})


class _PaperResponse:
    """Small requests.Response-compatible result used by OMS cancel paths."""

    def __init__(self, status_code: int = 200, payload: Any = None):
        self.status_code = int(status_code)
        self._payload = {} if payload is None else payload

    def json(self):
        return copy.deepcopy(self._payload)


class _PublicBinanceRest:
    """Capability-minimal REST client: it can only perform public GETs."""

    def __init__(self, timeout_sec: float = 5.0):
        self.base_url = REST_URL_MAIN
        self.timeout_sec = max(0.5, float(timeout_sec or 5.0))
        self.session = requests.Session()
        self._lock = threading.Lock()
        self._last_request_at = 0.0
        self._last_rpi_request_at = 0.0

    def close(self):
        self.session.close()

    def get_depth_snapshot(self, symbol: str, limit: int = 1000):
        return self._get_json(
            EP_DEPTH_SNAPSHOT,
            {"symbol": str(symbol or "").upper(), "limit": int(limit)},
            minimum_interval=0.05,
        )

    def get_rpi_depth(self, symbol: str, limit: int = 1000):
        if int(limit) != 1000:
            raise ValueError("Binance USD-M rpiDepth only supports limit=1000")
        return self._get_json(
            EP_RPI_DEPTH,
            {"symbol": str(symbol or "").upper(), "limit": 1000},
            minimum_interval=1.0,
            rpi=True,
        )

    def get_premium_index(self, symbol: str, *, timeout_sec: float = None):
        return self._get_json(
            EP_PREMIUM_INDEX,
            {"symbol": str(symbol or "").upper()},
            minimum_interval=0.05,
            timeout_sec=timeout_sec,
            attempts=1,
        )

    def get_all_premium_indexes(self, *, timeout_sec: float = None):
        return self._get_json(
            EP_PREMIUM_INDEX,
            {},
            minimum_interval=0.05,
            timeout_sec=timeout_sec,
            attempts=1,
            response_type=list,
        )

    def _get_json(
        self,
        endpoint: str,
        params: dict,
        *,
        minimum_interval: float,
        rpi: bool = False,
        timeout_sec: float = None,
        attempts: int = 2,
        response_type: type | tuple[type, ...] = dict,
    ):
        attempts = max(1, int(attempts or 1))
        request_timeout = (
            self.timeout_sec
            if timeout_sec is None
            else max(0.05, float(timeout_sec or 0.0))
        )
        for attempt in range(attempts):
            with self._lock:
                now = time.perf_counter()
                reference = self._last_rpi_request_at if rpi else self._last_request_at
                wait_for = max(0.0, float(minimum_interval) - (now - reference))
                if wait_for:
                    time.sleep(wait_for)
                stamp = time.perf_counter()
                self._last_request_at = stamp
                if rpi:
                    self._last_rpi_request_at = stamp
            try:
                response = self.session.get(
                    self.base_url + endpoint,
                    params=params,
                    timeout=request_timeout,
                )
                if response.status_code == 200:
                    payload = response.json()
                    return payload if isinstance(payload, response_type) else None
                logger.error(
                    "[BINANCE_PAPER] Public REST error "
                    f"endpoint={endpoint} status={response.status_code}"
                )
            except Exception as exc:
                logger.error(
                    f"[BINANCE_PAPER] Public REST exception endpoint={endpoint}: {exc}"
                )
            if attempt + 1 < attempts:
                time.sleep(0.25)
        return None


@dataclass(slots=True)
class _PaperPosition:
    quantity: float = 0.0
    entry_price: float = 0.0


@dataclass(slots=True)
class _PaperOrder:
    client_oid: str
    exchange_oid: str
    request: OrderRequest
    accept_seq: int
    created_ms: int
    created_monotonic: float
    update_ms: int
    status: str = "STAGED"
    committed: bool = False
    cum_filled_qty: float = 0.0
    cumulative_cost: float = 0.0
    avg_price: float = 0.0
    queue_ahead: float = 0.0
    fill_model: str = "orderbook"
    cancel_generation_at_stage: int = 0
    pending_cancel_reason: str = ""
    terminal_reason: str = ""
    queue_inserted: bool = False

    @property
    def remaining(self) -> float:
        return max(0.0, float(self.request.volume) - self.cum_filled_qty)

    @property
    def active(self) -> bool:
        return self.status in _ACTIVE_STATUSES


@dataclass(slots=True)
class _EngineCommand:
    kind: str
    payload: Any = None
    wait_for_result: bool = True
    completed: threading.Event = field(default_factory=threading.Event)
    state_lock: threading.RLock = field(default_factory=threading.RLock)
    abandoned: bool = False
    result: Any = None
    error: BaseException | None = None


class BinancePaperGateway(BaseGateway):
    """Binance production-public-data gateway with a local paper venue."""

    supports_outbound_send_guard = True

    def __init__(self, event_engine, config: dict, market_data_config: dict | None = None):
        super().__init__(event_engine, "BINANCE_PAPER")
        self.config = dict(config or {})
        self.paper_config = dict(self.config.get("paper_trade", {}) or {})
        system = self.config.get("system", {}) or {}
        if market_data_config is None:
            market_data_config = system.get("market_data", {}) or {}
        self.market_data_config = dict(market_data_config or {})

        # A paper run always consumes production public data.  This flag exists
        # for UI compatibility only; no testnet or private endpoint is used.
        self.testnet = False
        self.environment = "PAPER_LIVE_DATA"
        self.symbols = [str(value).upper() for value in self.config.get("symbols", [])]
        self.active = False
        self._accepting_orders = False
        time_sync_config = system.get("time_sync", {}) or {}
        self.require_healthy_clock = bool(
            time_sync_config.get("require_healthy_for_trading", False)
        )

        self.publish_depth_levels = max(
            1,
            int(self.market_data_config.get("publish_depth_levels", 5) or 5),
        )
        self.emit_full_orderbook_events = bool(
            self.market_data_config.get("emit_full_orderbook_events", False)
        )
        default_ingress_age_ms = (
            (self.config.get("risk", {}).get("tech_health", {}) or {}).get(
                "max_latency_ms",
                1000.0,
            )
        )
        self.max_market_event_ingress_age_ms = max(
            100.0,
            self._finite_nonnegative(
                self.market_data_config.get(
                    "max_market_event_ingress_age_ms",
                    default_ingress_age_ms,
                ),
                1000.0,
            ),
        )
        self.stream_ready_timeout_sec = max(
            1.0,
            float(
                self.market_data_config.get("stream_ready_timeout_sec", 10.0)
                or 10.0
            ),
        )
        self.max_book_buffer = max(
            100,
            int(self.paper_config.get("max_book_buffer", 50_000) or 50_000),
        )
        self.mark_startup_timeout_sec = max(
            1.0,
            float(
                self.paper_config.get("mark_startup_timeout_sec", 10.0)
                or 10.0
            ),
        )
        self.mark_rest_request_timeout_sec = max(
            0.25,
            float(
                self.paper_config.get("mark_rest_request_timeout_sec", 2.0)
                or 2.0
            ),
        )
        self.mark_rest_poll_interval_sec = max(
            0.25,
            float(
                self.paper_config.get("mark_rest_poll_interval_sec", 1.0)
                or 1.0
            ),
        )
        self.mark_ws_stale_after_sec = max(
            0.5,
            float(
                self.paper_config.get("mark_ws_stale_after_sec", 2.0)
                or 2.0
            ),
        )
        self.mark_rest_max_exchange_age_sec = max(
            0.5,
            float(
                self.paper_config.get(
                    "mark_rest_max_exchange_age_sec",
                    3.0,
                )
                or 3.0
            ),
        )

        account_config = self.config.get("account", {}) or {}
        backtest_config = self.config.get("backtest", {}) or {}
        self.initial_balance = self._finite_nonnegative(
            self.paper_config.get(
                "initial_balance_usdt",
                account_config.get("initial_balance_usdt", 10_000.0),
            ),
            10_000.0,
        )
        self.leverage = max(1, int(account_config.get("leverage", 10) or 10))
        self.maintenance_margin_rate = max(
            0.0,
            self._finite_nonnegative(
                self.paper_config.get("maintenance_margin_rate", 0.005),
                0.005,
            ),
        )
        self.maker_fee = self._finite_nonnegative(
            self.paper_config.get("maker_fee", backtest_config.get("maker_fee", 0.0002)),
            0.0002,
        )
        self.taker_fee = self._finite_nonnegative(
            self.paper_config.get("taker_fee", backtest_config.get("taker_fee", 0.0005)),
            0.0005,
        )
        self.rpi_commission_rate = self._finite_nonnegative(
            self.paper_config.get(
                "rpi_commission_rate",
                backtest_config.get("rpi_commission_rate", 0.0),
            ),
            0.0,
        )
        raw_symbol_rpi_rates = self.paper_config.get(
            "rpi_commission_rates",
            backtest_config.get("rpi_commission_rates", {}),
        )
        self.rpi_commission_rates = {
            str(symbol).upper(): self._finite_nonnegative(rate, 0.0)
            for symbol, rate in (raw_symbol_rpi_rates or {}).items()
        }

        rpi_config = self.paper_config.get("rpi", {}) or {}
        requested_rpi_model = str(
            self.paper_config.get(
                "rpi_fill_model",
                rpi_config.get("fill_model", "disabled"),
            )
            or "disabled"
        ).lower()
        proxy_enabled = bool(
            self.paper_config.get("public_trade_proxy", False)
            or rpi_config.get("public_trade_proxy", False)
        )
        if proxy_enabled:
            requested_rpi_model = "public_trade_proxy"
        if requested_rpi_model not in {"disabled", "public_trade_proxy"}:
            raise ValueError(
                "paper_trade.rpi_fill_model must be disabled or public_trade_proxy"
            )
        self.rpi_fill_model = requested_rpi_model
        self.cancel_ahead_fraction = min(
            1.0,
            self._finite_nonnegative(
                self.paper_config.get("cancel_ahead_fraction", 0.0),
                0.0,
            ),
        )
        self.market_order_max_slippage_bps = self._finite_nonnegative(
            self.paper_config.get("market_order_max_slippage_bps", 100.0),
            100.0,
        )

        self.command_timeout_sec = max(
            0.05,
            float(self.paper_config.get("command_timeout_sec", 2.0) or 2.0),
        )
        queue_size = max(
            100,
            int(self.paper_config.get("command_queue_size", 10_000) or 10_000),
        )
        self.max_order_history = max(
            100,
            int(self.paper_config.get("max_order_history", 100_000) or 100_000),
        )
        self.max_trade_history = max(
            100,
            int(self.paper_config.get("max_trade_history", 100_000) or 100_000),
        )

        configured_balance_asset = str(
            self.paper_config.get("balance_asset", "") or ""
        ).upper()
        self.balance_asset = configured_balance_asset or self._default_balance_asset(
            self.symbols
        )
        self._balances = {self.balance_asset: self.initial_balance}

        self.rest = _PublicBinanceRest(
            timeout_sec=float(self.paper_config.get("public_rest_timeout_sec", 5.0) or 5.0)
        )
        self.ws: BinanceWsApi | None = None
        self.orderbooks: dict[str, LocalOrderBook] = {}
        self.ws_buffer: dict[str, list[dict] | None] = {}
        self.book_resyncing: set[str] = set()
        self.book_recovery_generation: dict[str, int] = {}
        self.book_recovery_tokens: dict[str, int] = {}
        self._book_recovery_token = 0
        self._book_generation = 0
        self._book_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._fault_lock = threading.Lock()
        self._fault_epoch = 0
        self._closing = False
        self._last_ws_mark_received_monotonic: dict[str, float] = {}
        self._mark_fallback_stop = threading.Event()
        self._mark_fallback_thread: threading.Thread | None = None

        self._commands: queue.Queue[_EngineCommand] = queue.Queue(maxsize=queue_size)
        self._worker: threading.Thread | None = None
        self._worker_running = False
        self._worker_stop_requested = False

        # The following state is owned exclusively by the matching thread.
        self._orders: dict[str, _PaperOrder] = {}
        self._exchange_to_client: dict[str, str] = {}
        self._positions: dict[str, _PaperPosition] = {}
        self._books: dict[str, OrderBook] = {}
        self._liquidity: dict[str, dict[str, dict[float, float]]] = {}
        self._marks: dict[str, float] = {}
        self._last_market_trade_id: dict[str, int] = {}
        self._trades: deque[dict] = deque(maxlen=self.max_trade_history)
        self._dms_deadlines: dict[str, float] = {}
        # A symbol-wide cancel is a venue barrier.  Orders staged before the
        # barrier must not become active after the OMS durability commit.
        self._cancel_generations: dict[str, int] = {}
        self._accept_sequence = 0
        self._exchange_sequence = 0
        self._event_sequence = 0
        self._paper_trade_sequence = 0

        # OMS writes these attributes during construction.
        self.target_leverage = self.leverage
        self.target_margin_type = str(
            account_config.get("margin_type", "CROSSED") or "CROSSED"
        ).upper()
        self.target_position_mode = str(
            account_config.get("position_mode", "ONE_WAY") or "ONE_WAY"
        ).upper()

    # ------------------------------------------------------------------
    # BaseGateway lifecycle and public market transport
    # ------------------------------------------------------------------

    def connect(self, symbols: list):
        with self._lifecycle_lock:
            if self._closing:
                return False
            if self.active and self.state == GatewayState.READY:
                return True
            with self._fault_lock:
                fault_epoch = self._fault_epoch
            self.symbols = [str(symbol).upper() for symbol in (symbols or self.symbols)]
            if not self.symbols:
                self.set_state(GatewayState.ERROR)
                return False
            if not self.balance_asset:
                self.balance_asset = self._default_balance_asset(self.symbols)
                self._balances.setdefault(self.balance_asset, self.initial_balance)

            self.set_state(GatewayState.CONNECTING)
            self.active = True
            self._accepting_orders = False
            self._start_worker()
            generation = self._reset_public_books()
            ws = self._new_public_ws(generation)
            self.ws = ws
            ws.start_market_stream(self.symbols)

        if not ws.wait_until_connected(
            names=("PublicWS", "MarketWS"),
            timeout_sec=self.stream_ready_timeout_sec,
        ):
            if self._book_generation_is_current(generation):
                self._fault("PUBLIC_STREAM_READY_TIMEOUT")
            ws.close()
            return False

        all_synced = all(
            self._resync_book(symbol, expected_generation=generation)
            for symbol in self.symbols
        )
        if not all_synced:
            if self._book_generation_is_current(generation):
                self._fault("WS_HANDLER_FAILURE:PUBLIC_BOOK_INITIALIZATION_FAILED")
            ws.close()
            return False

        if not self._wait_for_initial_marks(generation):
            if self._book_generation_is_current(generation):
                self._fault("PUBLIC_MARK_INITIALIZATION_FAILED")
            ws.close()
            return False

        with self._lifecycle_lock:
            if not self._emit_full_account_update(reason="PAPER_BOOTSTRAP"):
                ws.close()
                return False
            if not self._commit_ready_if_current(
                ws,
                generation,
                fault_epoch,
            ):
                ws.close()
                return False
            self._start_mark_fallback(generation)
        logger.info(
            "[BINANCE_PAPER] Ready on production public data; "
            f"RPI fill model={self.rpi_fill_model}"
        )
        return True

    def begin_shutdown(self):
        with self._lifecycle_lock:
            self._closing = True
            self._accepting_orders = False
            self.active = False
            self._mark_fallback_stop.set()
            self._invalidate_public_book_lifecycle()
            ws = self.ws
            self.ws = None
            with self._fault_lock:
                self.set_state(GatewayState.DISCONNECTED)
        if ws is not None:
            ws.close()
        return True

    def close(self):
        self.begin_shutdown()
        mark_fallback_stopped = True
        mark_thread = self._mark_fallback_thread
        if (
            mark_thread is not None
            and mark_thread is not threading.current_thread()
            and mark_thread.is_alive()
        ):
            mark_thread.join(timeout=self.mark_rest_request_timeout_sec + 0.5)
            mark_fallback_stopped = not mark_thread.is_alive()
            if not mark_fallback_stopped:
                logger.critical(
                    "[BINANCE_PAPER] Mark-price fallback thread did not stop cleanly"
                )
        with self._lifecycle_lock:
            if self.state == GatewayState.DISCONNECTED and not self._worker_running:
                self.rest.close()
                return mark_fallback_stopped

        worker_stopped = True
        if self._worker_running:
            try:
                self._call_worker("shutdown", None, allow_closing=True)
            except Exception as exc:
                logger.error(f"[BINANCE_PAPER] Matching shutdown failed: {exc}")
                self._worker_stop_requested = True
            worker = self._worker
            if worker is not None and worker.is_alive():
                worker.join(timeout=max(2.0, self.command_timeout_sec * 2.0))
                if worker.is_alive():
                    worker_stopped = False
                    logger.critical("[BINANCE_PAPER] Matching thread did not stop cleanly")

        self.rest.close()
        self.set_state(GatewayState.DISCONNECTED)
        return bool(worker_stopped and mark_fallback_stopped)

    def recover_connectivity(self, recovery_context=None):
        with self._lifecycle_lock:
            if (
                self._closing
                or not self.symbols
                or not self._worker_running
            ):
                return False
            with self._fault_lock:
                fault_epoch = self._fault_epoch
            self._accepting_orders = False
            self.active = True
            self.set_state(GatewayState.CONNECTING)
            if self.ws is not None:
                self.ws.close()
            generation = self._reset_public_books()
            ws = self._new_public_ws(generation)
            self.ws = ws
            ws.start_market_stream(self.symbols)

        if not ws.wait_until_connected(
            names=("PublicWS", "MarketWS"),
            timeout_sec=self.stream_ready_timeout_sec,
        ):
            if self._book_generation_is_current(generation):
                self._fault("PUBLIC_STREAM_RECOVERY_TIMEOUT")
            ws.close()
            return False

        all_synced = all(
            self._resync_book(symbol, expected_generation=generation)
            for symbol in self.symbols
        )
        if not all_synced:
            if self._book_generation_is_current(generation):
                self._fault("WS_HANDLER_FAILURE:PUBLIC_BOOK_RECOVERY_FAILED")
            ws.close()
            return False
        if not self._wait_for_initial_marks(generation):
            if self._book_generation_is_current(generation):
                self._fault("PUBLIC_MARK_RECOVERY_FAILED")
            ws.close()
            return False
        with self._lifecycle_lock:
            if not self._commit_ready_if_current(
                ws,
                generation,
                fault_epoch,
            ):
                ws.close()
                return False
            self._start_mark_fallback(generation)
            if recovery_context:
                owner = str(recovery_context.get("owner", "") or "")
                epoch = int(recovery_context.get("epoch", 0) or 0)
                recovery_event = (
                    f"VERIFY_VENUE:{self.gateway_name}:{epoch}:{owner}"
                )
            else:
                recovery_event = (
                    f"CLEAR_VENUE:{self.gateway_name}:PUBLIC_MARKET_RECOVERED"
                )
            self.event_engine.put(
                Event(
                    EVENT_SYSTEM_HEALTH,
                    recovery_event,
                )
            )
        return True

    def _new_public_ws(self, generation: int):
        return BinanceWsApi(
            lambda raw_message: self.on_ws_message(
                raw_message,
                expected_generation=generation,
            ),
            lambda error: self.on_ws_error(
                error,
                expected_generation=generation,
            ),
            testnet=False,
        )

    def on_ws_message(self, raw_message, *, expected_generation=None):
        if not self._book_generation_is_current(expected_generation):
            return
        (
            received_timestamp,
            received_monotonic,
            corrected_received_timestamp,
            clock_offset_ms,
        ) = time_service.capture_timestamp()
        try:
            message = json.loads(raw_message)
            if not isinstance(message, dict) or "stream" not in message:
                return
            self._handle_market_message(
                message["stream"],
                message.get("data", {}),
                received_timestamp=received_timestamp,
                received_monotonic=received_monotonic,
                clock_offset_ms=clock_offset_ms,
                corrected_received_timestamp=corrected_received_timestamp,
                expected_generation=expected_generation,
            )
        except Exception as exc:
            if self._book_generation_is_current(expected_generation):
                self._fault(f"WS_HANDLER_FAILURE:PUBLIC:{type(exc).__name__}:{exc}")

    def on_ws_error(self, error, *, expected_generation=None):
        if not self._book_generation_is_current(expected_generation):
            return
        if isinstance(error, dict):
            stream = str(error.get("stream", "MarketWS") or "MarketWS")
            kind = str(error.get("kind", "error") or "error")
            detail = str(error.get("detail", "") or "")
            rendered = f"{stream}:{kind}:{detail}"
        else:
            rendered = str(error)
        self._fault(f"WS_TRANSPORT_DROP:PUBLIC:{rendered}")

    def _handle_market_message(
        self,
        stream: str,
        data: dict,
        *,
        received_timestamp: float = None,
        received_monotonic: float = None,
        clock_offset_ms: float = None,
        corrected_received_timestamp: float = None,
        expected_generation=None,
    ):
        data = dict(data)
        symbol = str(data.get("s", "") or "").upper()
        with self._book_lock:
            if not self._book_generation_matches_locked(expected_generation):
                return
            if not symbol or symbol not in self.orderbooks:
                return
            message_generation = self._book_generation

        received_timestamp = float(received_timestamp or time.time())
        received_monotonic = float(received_monotonic or time.perf_counter())
        clock_offset_ms = float(
            getattr(time_service, "offset", 0.0)
            if clock_offset_ms is None
            else clock_offset_ms
        )
        corrected_received_timestamp = float(
            corrected_received_timestamp
            or (received_timestamp + clock_offset_ms / 1000.0)
        )
        normalized_stream = str(stream or "").lower()
        event_ms = int(
            data.get("E", 0)
            or (0 if "@markprice" in normalized_stream else data.get("T", 0))
            or 0
        )
        if event_ms:
            self.latency_stats["ws_delay"] = (
                corrected_received_timestamp * 1000.0 - event_ms
            )

        if "@aggtrade" in normalized_stream:
            if self._reject_stale_market_event(
                stream=normalized_stream,
                symbol=symbol,
                event_time_ms=event_ms,
                corrected_received_timestamp=corrected_received_timestamp,
            ):
                return
            trade = AggTradeData(
                symbol=symbol,
                trade_id=int(data.get("a", -1)),
                price=float(data.get("p", 0.0) or 0.0),
                quantity=float(data.get("q", 0.0) or 0.0),
                maker_is_buyer=bool(data.get("m", False)),
                datetime=datetime.fromtimestamp(
                    float(data.get("T", event_ms) or int(received_timestamp * 1000))
                    / 1000.0
                ),
                exchange_timestamp=(
                    float(data.get("T", event_ms) or 0.0) / 1000.0
                ),
                received_timestamp=received_timestamp,
                received_monotonic=received_monotonic,
                clock_offset_ms=clock_offset_ms,
                corrected_received_timestamp=corrected_received_timestamp,
            )
            self._publish_public_market_update(
                message_generation,
                EVENT_AGG_TRADE,
                trade,
                worker_kind="market_trade",
            )
            return

        if "@markprice" in normalized_stream:
            next_funding_ms = int(data.get("T", 0) or 0)
            event_time_ms = int(data.get("E", 0) or 0)
            next_funding_timestamp = (
                next_funding_ms / 1000.0 if next_funding_ms else 0.0
            )
            mark = MarkPriceData(
                symbol=symbol,
                mark_price=float(data.get("p", 0.0) or 0.0),
                index_price=float(data.get("i", 0.0) or 0.0),
                funding_rate=float(data.get("r", 0.0) or 0.0),
                next_funding_time=datetime.fromtimestamp(
                    next_funding_timestamp or time.time()
                ),
                datetime=datetime.fromtimestamp(
                    event_time_ms / 1000.0 if event_time_ms else received_timestamp
                ),
                exchange_timestamp=(event_time_ms / 1000.0 if event_time_ms else 0.0),
                received_timestamp=received_timestamp,
                received_monotonic=received_monotonic,
                clock_offset_ms=clock_offset_ms,
                corrected_received_timestamp=corrected_received_timestamp,
                next_funding_timestamp=next_funding_timestamp,
            )
            with self._book_lock:
                if not self._book_generation_matches_locked(message_generation):
                    return
                self._last_ws_mark_received_monotonic[symbol] = received_monotonic
            self._publish_public_market_update(
                message_generation,
                EVENT_MARK_PRICE,
                mark,
                worker_kind="mark",
            )
            return

        if "@depth" in normalized_stream:
            if self._reject_stale_market_event(
                stream=normalized_stream,
                symbol=symbol,
                event_time_ms=event_ms,
                corrected_received_timestamp=corrected_received_timestamp,
            ):
                return
            data["_local_received_timestamp"] = received_timestamp
            data["_local_received_monotonic"] = received_monotonic
            data["_local_clock_offset_ms"] = clock_offset_ms
            data["_local_corrected_received_timestamp"] = (
                corrected_received_timestamp
            )
            self._process_book_delta(
                symbol,
                data,
                expected_generation=message_generation,
            )

    def _reject_stale_market_event(
        self,
        *,
        stream: str,
        symbol: str,
        event_time_ms: int,
        corrected_received_timestamp: float,
    ) -> bool:
        if event_time_ms <= 0:
            return False
        age_ms = corrected_received_timestamp * 1000.0 - event_time_ms
        if abs(age_ms) <= self.max_market_event_ingress_age_ms:
            return False
        stream_kind = "PUBLIC_DEPTH" if "@depth" in stream else "PUBLIC_TRADE"
        self._fault(
            "MARKET_DATA_STALE:"
            f"{stream_kind}:symbol={symbol}:"
            f"age={age_ms:.1f}ms>"
            f"{self.max_market_event_ingress_age_ms:.1f}ms"
        )
        return True

    def _wait_for_initial_marks(self, generation: int) -> bool:
        deadline = time.perf_counter() + self.mark_startup_timeout_sec
        missing = set(self.symbols)
        while missing and time.perf_counter() < deadline:
            for symbol in sorted(missing):
                remaining = deadline - time.perf_counter()
                if remaining <= 0.0:
                    break
                payload = self.rest.get_premium_index(
                    symbol,
                    timeout_sec=min(
                        self.mark_rest_request_timeout_sec,
                        remaining,
                    ),
                )
                if not payload:
                    break
                if str(payload.get("symbol", "") or "").upper() != symbol:
                    break
                if self._publish_rest_mark(
                    payload,
                    expected_generation=generation,
                ):
                    missing.discard(symbol)
                elif not self._book_generation_is_current(generation):
                    return False
            if missing and time.perf_counter() < deadline:
                time.sleep(min(0.25, max(0.0, deadline - time.perf_counter())))
        if missing:
            logger.error(
                "[BINANCE_PAPER] Initial mark-price readiness timed out; "
                f"missing={sorted(missing)}"
            )
            return False
        return True

    def _publish_rest_mark(self, payload: dict, *, expected_generation: int) -> bool:
        if not isinstance(payload, dict):
            return False
        symbol = str(payload.get("symbol", "") or "").upper()
        try:
            mark_price = float(payload.get("markPrice", 0.0) or 0.0)
            index_price = float(payload.get("indexPrice", 0.0) or 0.0)
            funding_rate = float(payload.get("lastFundingRate", 0.0) or 0.0)
            event_time_ms = int(payload.get("time", 0) or 0)
            next_funding_ms = int(payload.get("nextFundingTime", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return False
        if (
            symbol not in self.symbols
            or not math.isfinite(mark_price)
            or not math.isfinite(index_price)
            or not math.isfinite(funding_rate)
            or mark_price <= 0.0
            or index_price <= 0.0
            or event_time_ms <= 0
        ):
            return False
        (
            received_timestamp,
            received_monotonic,
            corrected_received_timestamp,
            clock_offset_ms,
        ) = time_service.capture_timestamp()
        exchange_timestamp = (
            event_time_ms / 1000.0 if event_time_ms else received_timestamp
        )
        exchange_age_sec = abs(
            corrected_received_timestamp - exchange_timestamp
        )
        if exchange_age_sec > self.mark_rest_max_exchange_age_sec:
            return False
        next_funding_timestamp = (
            next_funding_ms / 1000.0 if next_funding_ms else 0.0
        )
        mark = MarkPriceData(
            symbol=symbol,
            mark_price=mark_price,
            index_price=index_price,
            funding_rate=funding_rate,
            next_funding_time=datetime.fromtimestamp(
                next_funding_timestamp or received_timestamp
            ),
            datetime=datetime.fromtimestamp(exchange_timestamp),
            exchange_timestamp=exchange_timestamp,
            received_timestamp=received_timestamp,
            received_monotonic=received_monotonic,
            clock_offset_ms=clock_offset_ms,
            corrected_received_timestamp=corrected_received_timestamp,
            next_funding_timestamp=next_funding_timestamp,
        )
        return self._publish_public_market_update(
            expected_generation,
            EVENT_MARK_PRICE,
            mark,
            worker_kind="mark",
        )

    def _start_mark_fallback(self, generation: int) -> None:
        self._mark_fallback_stop.set()
        stop_event = threading.Event()
        self._mark_fallback_stop = stop_event
        thread = threading.Thread(
            target=self._mark_fallback_loop,
            args=(generation, stop_event),
            daemon=True,
            name="BinancePaperMarkFallback",
        )
        self._mark_fallback_thread = thread
        thread.start()

    def _mark_fallback_loop(
        self,
        generation: int,
        stop_event: threading.Event,
    ) -> None:
        fallback_logged = False
        while not stop_event.wait(self.mark_rest_poll_interval_sec):
            with self._book_lock:
                if (
                    self._closing
                    or not self.active
                    or self._book_generation != generation
                ):
                    return
                now = time.perf_counter()
                stale_symbols = [
                    symbol
                    for symbol in self.symbols
                    if now
                    - float(
                        self._last_ws_mark_received_monotonic.get(symbol, 0.0)
                        or 0.0
                    )
                    > self.mark_ws_stale_after_sec
                ]
            if not stale_symbols:
                fallback_logged = False
                continue
            if not fallback_logged:
                logger.warning(
                    "[BINANCE_PAPER] WebSocket mark-price stream is stale; "
                    "using public premiumIndex fallback"
                )
                fallback_logged = True
            payloads = self.rest.get_all_premium_indexes(
                timeout_sec=self.mark_rest_request_timeout_sec,
            )
            if not payloads:
                continue
            payload_by_symbol = {
                str(payload.get("symbol", "") or "").upper(): payload
                for payload in payloads
                if isinstance(payload, dict)
            }
            for symbol in stale_symbols:
                if stop_event.is_set():
                    return
                payload = payload_by_symbol.get(symbol)
                if payload is None:
                    continue
                if self._publish_rest_mark(
                    payload,
                    expected_generation=generation,
                ):
                    continue
                if not self._book_generation_is_current(generation):
                    return

    @staticmethod
    def _stamp_market_dispatch(data):
        data.dispatch_timestamp = time.time()
        data.dispatch_monotonic = time.perf_counter()

    def _publish_public_market_update(
        self,
        generation,
        event_type,
        data,
        *,
        worker_kind: str,
    ) -> bool:
        # Serialize the final generation check, matching enqueue, and public
        # emission against lifecycle reset.  The worker validates the same
        # generation again when it dequeues the command.
        with self._book_lock:
            if self._book_generation != generation:
                return False
            self._stamp_market_dispatch(data)
            if not self._submit_worker(
                worker_kind,
                (generation, data),
            ):
                return False
            self.on_market_data(event_type, data)
            return True

    def _book_generation_matches_locked(self, expected_generation):
        return bool(
            expected_generation is None
            or self._book_generation == expected_generation
        )

    def _book_generation_is_current(self, expected_generation):
        with self._book_lock:
            return self._book_generation_matches_locked(expected_generation)

    def _commit_ready_if_current(
        self,
        expected_ws,
        expected_generation,
        expected_fault_epoch,
    ) -> bool:
        with self._book_lock:
            if not self._book_generation_matches_locked(expected_generation):
                return False
        with self._fault_lock:
            if (
                self._closing
                or self.ws is not expected_ws
                or not self.active
                or self.state == GatewayState.ERROR
                or self._fault_epoch != expected_fault_epoch
                or self._book_generation != expected_generation
            ):
                return False
            self._accepting_orders = True
            self.set_state(GatewayState.READY)
            return True

    def _invalidate_public_book_lifecycle(self):
        with self._book_lock:
            self._book_generation += 1
            self.book_resyncing.clear()
            self.book_recovery_generation.clear()
            self.book_recovery_tokens.clear()
            return self._book_generation

    def _reset_public_books(self):
        with self._book_lock:
            self._book_generation += 1
            generation = self._book_generation
            self.orderbooks = {
                symbol: LocalOrderBook(
                    symbol,
                    publish_depth_levels=self.publish_depth_levels,
                    emit_full_book=self.emit_full_orderbook_events,
                )
                for symbol in self.symbols
            }
            self.ws_buffer = {symbol: [] for symbol in self.symbols}
            self.book_resyncing.clear()
            self.book_recovery_generation.clear()
            self.book_recovery_tokens.clear()
            self._last_ws_mark_received_monotonic.clear()
            return generation

    def _resync_book(
        self,
        symbol: str,
        *,
        expected_generation=None,
        recovery_token=None,
    ):
        snapshot = self.rest.get_depth_snapshot(symbol)
        if not snapshot:
            return False
        try:
            with self._book_lock:
                if not self._book_generation_matches_locked(expected_generation):
                    return False
                generation = self._book_generation
                if recovery_token is not None and not self._owns_book_recovery_locked(
                    symbol,
                    generation,
                    recovery_token,
                ):
                    return False
                book = self.orderbooks[symbol]
                book.init_snapshot(snapshot)
                buffered = list(self.ws_buffer.get(symbol) or [])
                for delta in buffered:
                    book.process_delta(delta)
                self.ws_buffer[symbol] = None
                event_book = book.generate_event_data()
                matching_book = self._full_matching_book(book)
        except (KeyError, ValueError, OrderBookGapError) as exc:
            logger.error(f"[BINANCE_PAPER] Book sync failed for {symbol}: {exc}")
            return False

        return self._publish_book_update(
            generation,
            symbol=symbol,
            expected_book=book,
            expected_recovery_token=recovery_token,
            event_book=event_book,
            matching_book=matching_book,
        )

    def _process_book_delta(
        self,
        symbol: str,
        delta: dict,
        *,
        expected_generation=None,
    ):
        processing_generation = expected_generation
        recovery = None
        gap_failure = None
        other_failure = None
        with self._book_lock:
            try:
                if not self._book_generation_matches_locked(expected_generation):
                    return
                processing_generation = self._book_generation
                buffered = self.ws_buffer.get(symbol)
                if buffered is not None:
                    if len(buffered) >= self.max_book_buffer:
                        raise RuntimeError(f"book buffer overflow for {symbol}")
                    buffered.append(delta)
                    return
                book = self.orderbooks[symbol]
                book.process_delta(delta)
                event_book = book.generate_event_data()
                matching_book = self._full_matching_book(book)
            except OrderBookGapError as exc:
                gap_failure = exc
                # The new owner is installed under the exact lock that saw
                # the broken sequence.  The old recovery worker can no longer
                # win the detection-to-schedule window and emit a stale clear.
                recovery = self._begin_book_recovery_locked(
                    symbol,
                    freeze_reason="FATAL_GAP",
                    expected_generation=processing_generation,
                )
            except Exception as exc:
                other_failure = exc

        if gap_failure is not None:
            if recovery is not None:
                self._launch_book_recovery(recovery)
            return
        if other_failure is not None:
            if self._book_generation_is_current(processing_generation):
                self._fault(
                    f"WS_HANDLER_FAILURE:PUBLIC_BOOK:{symbol}:"
                    f"{type(other_failure).__name__}:{other_failure}"
                )
            return

        self._publish_book_update(
            processing_generation,
            symbol=symbol,
            expected_book=book,
            event_book=event_book,
            matching_book=matching_book,
        )

    def _publish_book_update(
        self,
        generation,
        *,
        symbol,
        expected_book,
        expected_recovery_token=None,
        event_book,
        matching_book,
    ):
        # Keep the generation check and publication ordered against reset.
        # The matching worker validates the generation again when dequeuing.
        with self._book_lock:
            if self._book_generation != generation:
                return False
            if self.orderbooks.get(symbol) is not expected_book:
                return False
            if (
                expected_recovery_token is not None
                and not self._owns_book_recovery_locked(
                    symbol,
                    generation,
                    expected_recovery_token,
                )
            ):
                return False
            if matching_book is not None:
                if not self._submit_worker(
                    "book",
                    (generation, matching_book),
                ):
                    return False
            if event_book is not None:
                self._stamp_market_dispatch(event_book)
                self.on_market_data(EVENT_ORDERBOOK, event_book)
            return True

    def _full_matching_book(self, book: LocalOrderBook):
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

    def _owns_book_recovery_locked(self, symbol, generation, recovery_token):
        return bool(
            self._book_generation == generation
            and symbol in self.book_resyncing
            and self.book_recovery_generation.get(symbol) == generation
            and self.book_recovery_tokens.get(symbol) == recovery_token
        )

    def _release_book_recovery_locked(self, symbol, generation, recovery_token):
        if not self._owns_book_recovery_locked(
            symbol,
            generation,
            recovery_token,
        ):
            return False
        self.book_recovery_generation.pop(symbol, None)
        self.book_recovery_tokens.pop(symbol, None)
        self.book_resyncing.discard(symbol)
        return True

    def _schedule_book_recovery(
        self,
        symbol: str,
        freeze_reason: str = "",
        *,
        expected_generation=None,
    ):
        with self._book_lock:
            recovery = self._begin_book_recovery_locked(
                symbol,
                freeze_reason,
                expected_generation=expected_generation,
            )
        if recovery is None:
            return False
        self._launch_book_recovery(recovery)
        return True

    def _begin_book_recovery_locked(
        self,
        symbol: str,
        freeze_reason: str = "",
        *,
        expected_generation=None,
    ):
        if not self._book_generation_matches_locked(expected_generation):
            return None
        if symbol in self.book_resyncing and not freeze_reason:
            return None
        generation = self._book_generation
        self._book_recovery_token += 1
        recovery_token = self._book_recovery_token
        self.book_resyncing.add(symbol)
        self.book_recovery_generation[symbol] = generation
        self.book_recovery_tokens[symbol] = recovery_token
        self.orderbooks[symbol] = LocalOrderBook(
            symbol,
            publish_depth_levels=self.publish_depth_levels,
            emit_full_book=self.emit_full_orderbook_events,
        )
        self.ws_buffer[symbol] = []
        return symbol, generation, recovery_token, freeze_reason

    def _launch_book_recovery(self, recovery):
        symbol, generation, recovery_token, freeze_reason = recovery
        with self._book_lock:
            if not self._owns_book_recovery_locked(
                symbol,
                generation,
                recovery_token,
            ):
                return False
            if freeze_reason:
                self.event_engine.put(
                    Event(
                        EVENT_SYSTEM_HEALTH,
                        f"FREEZE_SYMBOL:{symbol}:{freeze_reason}:{recovery_token}",
                    )
                )

        threading.Thread(
            target=self._recover_orderbook,
            args=(symbol, generation, recovery_token),
            daemon=True,
            name=f"PaperBookRecovery-{symbol}",
        ).start()
        return True

    def _recover_orderbook(self, symbol, generation, recovery_token):
        try:
            ok = self._resync_book(
                symbol,
                expected_generation=generation,
                recovery_token=recovery_token,
            )
            if ok:
                with self._book_lock:
                    completed = self._release_book_recovery_locked(
                        symbol,
                        generation,
                        recovery_token,
                    )
                    if completed:
                        self.event_engine.put(
                            Event(
                                EVENT_SYSTEM_HEALTH,
                                f"CLEAR_SYMBOL:{symbol}:ORDERBOOK_RESYNCED:{recovery_token}",
                            )
                        )
                return
            with self._book_lock:
                if self._owns_book_recovery_locked(
                    symbol,
                    generation,
                    recovery_token,
                ):
                    self._fault(
                        f"WS_HANDLER_FAILURE:PUBLIC_BOOK_RESYNC_FAILED:{symbol}"
                    )
        finally:
            with self._book_lock:
                self._release_book_recovery_locked(
                    symbol,
                    generation,
                    recovery_token,
                )

    # ------------------------------------------------------------------
    # OMS command/query surface
    # ------------------------------------------------------------------

    def _clock_health_rejection(self, req: OrderRequest):
        if req.reduce_only or not self.require_healthy_clock:
            return "", ""
        try:
            clock_health = time_service.health_snapshot(
                notify_listeners=False
            )
        except Exception as exc:
            return "CLOCK_HEALTH_UNAVAILABLE", f"{type(exc).__name__}:{exc}"
        if not bool(clock_health.get("ready", False)):
            return (
                "CLOCK_UNHEALTHY",
                str(clock_health.get("reason", "exchange clock unavailable")),
            )
        return "", ""

    def send_order(
        self,
        req: OrderRequest,
        client_oid: str = None,
        *,
        pre_send_guard=None,
    ) -> GatewayCommandResult:
        if not self._accepting_orders or self.state != GatewayState.READY:
            return GatewayCommandResult(
                CommandOutcome.REJECTED,
                error_code="PAPER_NOT_READY",
                error_message="paper gateway is not ready for new orders",
            )
        if not req.reduce_only:
            symbol = str(req.symbol or "").upper()
            with self._book_lock:
                if (
                    symbol in self.book_resyncing
                    or self.ws_buffer.get(symbol) is not None
                ):
                    return GatewayCommandResult(
                        CommandOutcome.REJECTED,
                        error_code="PAPER_ORDERBOOK_NOT_READY",
                        error_message=(
                            f"{symbol} paper order book is resynchronizing"
                        ),
                    )
        clock_error, clock_message = self._clock_health_rejection(req)
        if clock_error:
            return GatewayCommandResult(
                CommandOutcome.REJECTED,
                error_code=clock_error,
                error_message=clock_message,
            )
        if not client_oid:
            client_oid = f"PAPER_CLIENT_{time.time_ns()}"
        if callable(pre_send_guard):
            try:
                guard_result = pre_send_guard()
            except Exception as exc:
                return GatewayCommandResult(
                    CommandOutcome.REJECTED,
                    error_code="PRE_SEND_GUARD_UNAVAILABLE",
                    error_message=f"{type(exc).__name__}:{exc}",
                )
            if isinstance(guard_result, tuple):
                allowed = bool(guard_result[0]) if guard_result else False
                error_code = (
                    str(guard_result[1])
                    if len(guard_result) > 1
                    else "PRE_SEND_GUARD_REJECTED"
                )
                error_message = (
                    str(guard_result[2])
                    if len(guard_result) > 2
                    else error_code
                )
            else:
                allowed = bool(guard_result)
                error_code = "PRE_SEND_GUARD_REJECTED"
                error_message = "pre-send guard rejected the paper order"
            if not allowed:
                return GatewayCommandResult(
                    CommandOutcome.REJECTED,
                    error_code=error_code,
                    error_message=error_message,
                )
        try:
            result = self._call_worker(
                "stage_order",
                (copy.deepcopy(req), str(client_oid)),
            )
        except TimeoutError as exc:
            return GatewayCommandResult(
                CommandOutcome.UNKNOWN,
                error_code="PAPER_COMMAND_TIMEOUT",
                error_message=str(exc),
            )
        except Exception as exc:
            return GatewayCommandResult(
                CommandOutcome.UNKNOWN,
                error_code="PAPER_COMMAND_FAILURE",
                error_message=f"{type(exc).__name__}:{exc}",
            )
        return result

    def commit_order_submission(self, client_oid: str) -> bool:
        """Release a staged order after the OMS has durably committed its ACK."""
        return bool(
            self._call_worker(
                "commit_order",
                str(client_oid or ""),
            )
        )

    # Alias for adapters which prefer a shorter hook name.
    commit_order = commit_order_submission

    def cancel_order(self, req: CancelRequest):
        try:
            payload = self._call_worker(
                "cancel_order",
                copy.deepcopy(req),
                allow_closing=True,
            )
        except TimeoutError as exc:
            return _PaperResponse(
                504,
                {"code": "PAPER_CANCEL_TIMEOUT", "msg": str(exc)},
            )
        except Exception as exc:
            return _PaperResponse(
                500,
                {
                    "code": "PAPER_CANCEL_FAILURE",
                    "msg": f"{type(exc).__name__}:{exc}",
                },
            )
        if payload is None:
            return _PaperResponse(
                404,
                {"code": "-2011", "msg": "Unknown paper order"},
            )
        return _PaperResponse(200, payload)

    def cancel_all_orders(self, symbol: str):
        try:
            payload = self._call_worker(
                "cancel_all",
                str(symbol or "").upper(),
                allow_closing=True,
            )
            return _PaperResponse(200, payload)
        except TimeoutError as exc:
            return _PaperResponse(
                504,
                {"code": "PAPER_CANCEL_ALL_TIMEOUT", "msg": str(exc)},
            )
        except Exception as exc:
            return _PaperResponse(
                500,
                {
                    "code": "PAPER_CANCEL_ALL_FAILURE",
                    "msg": f"{type(exc).__name__}:{exc}",
                },
            )

    def set_countdown_cancel_all(self, symbol: str, countdown_time_ms: int):
        try:
            payload = self._call_worker(
                "set_dms",
                (str(symbol or "").upper(), int(countdown_time_ms)),
                allow_closing=True,
            )
            return _PaperResponse(200, payload)
        except Exception as exc:
            return _PaperResponse(
                500,
                {"code": "PAPER_DMS_FAILURE", "msg": f"{type(exc).__name__}:{exc}"},
            )

    def get_account_info(self):
        return self._safe_query("account", default=None)

    def get_all_positions(self):
        return self._safe_query("positions", default=None)

    def get_open_orders(self):
        return self._safe_query("open_orders", default=None)

    def get_order(self, symbol: str, order_id: str):
        payload = self._safe_query(
            "order",
            args=(str(symbol or "").upper(), str(order_id or "")),
            default=None,
        )
        if payload is None:
            return {
                "_query_status": "NOT_FOUND",
                "code": "-2013",
                "msg": "Paper order does not exist",
            }
        return payload

    def get_all_orders(self, symbol: str, **kwargs):
        return self._safe_query(
            "all_orders",
            args=(str(symbol or "").upper(), dict(kwargs)),
            default=None,
        )

    def get_user_trades(self, symbol: str, **kwargs):
        return self._safe_query(
            "user_trades",
            args=(str(symbol or "").upper(), dict(kwargs)),
            default=None,
        )

    def get_income_history(self, **_kwargs):
        # There are no external cash flows inside the isolated paper venue.
        return []

    def get_depth_snapshot(self, symbol: str):
        return self.rest.get_depth_snapshot(symbol)

    def get_rpi_depth(self, symbol: str, limit: int = 1000):
        return self.rest.get_rpi_depth(symbol, limit=limit)

    def get_commission_rate(self, symbol: str):
        symbol = str(symbol or "").upper()
        return {
            "symbol": symbol,
            "makerCommissionRate": f"{self.maker_fee:.12g}",
            "takerCommissionRate": f"{self.taker_fee:.12g}",
            "rpiCommissionRate": f"{self._rpi_fee_rate(symbol):.12g}",
            "_simulated": True,
        }

    def _safe_query(self, query_name: str, args=None, default=None):
        try:
            return self._call_worker(
                "query",
                (query_name, args),
                allow_closing=True,
            )
        except Exception as exc:
            logger.error(f"[BINANCE_PAPER] Query {query_name} failed: {exc}")
            return default

    # ------------------------------------------------------------------
    # Single-writer matching-engine command loop
    # ------------------------------------------------------------------

    def _start_worker(self):
        if self._worker_running and self._worker and self._worker.is_alive():
            return
        self._worker_stop_requested = False
        self._worker_running = True
        self._worker = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name="BinancePaperMatchingEngine",
        )
        self._worker.start()

    def _worker_loop(self):
        try:
            while not self._worker_stop_requested:
                command = None
                try:
                    command = self._commands.get(timeout=0.05)
                except queue.Empty:
                    self._check_dms_deadlines()
                    continue

                try:
                    # Honor an already-expired dead-man deadline before any
                    # later command (especially a staged-order commit).
                    self._check_dms_deadlines()
                    result = self._dispatch_command(command.kind, command.payload)
                    with command.state_lock:
                        if command.abandoned:
                            self._rollback_abandoned_command(command)
                        else:
                            command.result = result
                        # Result publication, abandonment resolution, rollback,
                        # and completion notification are one atomic handoff.
                        # Otherwise a caller can time out in the tiny gap after
                        # result publication and leave a ghost STAGED order.
                        command.completed.set()
                except BaseException as exc:
                    with command.state_lock:
                        if command.abandoned:
                            self._rollback_abandoned_command(command)
                        else:
                            command.error = exc
                        command.completed.set()
                    logger.error(
                        "[BINANCE_PAPER] Matching command failed "
                        f"kind={command.kind}: {type(exc).__name__}:{exc}"
                    )
                finally:
                    self._commands.task_done()
                self._check_dms_deadlines()
        finally:
            self._worker_running = False

    def _dispatch_command(self, kind: str, payload):
        if kind == "stage_order":
            request, client_oid = payload
            return self._stage_order(request, client_oid)
        if kind == "commit_order":
            return self._commit_staged_order(payload)
        if kind == "cancel_order":
            return self._cancel_by_request(payload)
        if kind == "cancel_all":
            return self._cancel_all_internal(payload, reason="PAPER_CANCEL_ALL")
        if kind == "set_dms":
            symbol, countdown_ms = payload
            return self._set_dms_internal(symbol, countdown_ms)
        if kind == "book":
            generation, book = payload
            with self._book_lock:
                if generation != self._book_generation:
                    return False
                return self._on_book(book)
        if kind == "market_trade":
            generation, trade = payload
            with self._book_lock:
                if generation != self._book_generation:
                    return False
                return self._on_market_trade(trade)
        if kind == "mark":
            generation, mark = payload
            with self._book_lock:
                if generation != self._book_generation:
                    return False
                self._marks[mark.symbol] = float(mark.mark_price or 0.0)
                return True
        if kind == "query":
            query_name, args = payload
            return self._query_internal(query_name, args)
        if kind == "emit_full_account":
            return self._emit_full_account_update_internal(str(payload or "PAPER_UPDATE"))
        if kind == "shutdown":
            for symbol in sorted(set(self.symbols)):
                self._cancel_all_internal(symbol, reason="PAPER_SHUTDOWN")
            self._worker_stop_requested = True
            return True
        raise ValueError(f"unsupported paper command: {kind}")

    def _call_worker(self, kind: str, payload, *, allow_closing: bool = False):
        if not self._worker_running:
            raise RuntimeError("paper matching engine is not running")
        if not allow_closing and not self.active:
            raise RuntimeError("paper gateway is closed")
        command = _EngineCommand(kind=kind, payload=payload)
        try:
            self._commands.put(command, timeout=self.command_timeout_sec)
        except queue.Full as exc:
            self._fault("PAPER_COMMAND_QUEUE_FULL")
            raise TimeoutError("paper command queue is full") from exc
        if not command.completed.wait(self.command_timeout_sec):
            with command.state_lock:
                if not command.completed.is_set():
                    command.abandoned = True
                    raise TimeoutError(f"paper command timed out: {kind}")
        if command.error is not None:
            raise command.error
        return command.result

    def _rollback_abandoned_command(self, command: _EngineCommand):
        """Undo a timed-out staged submit before any venue state is exposed."""
        if command.kind != "stage_order":
            return
        _request, client_oid = command.payload
        order = self._orders.get(str(client_oid or ""))
        if order is None or order.status != "STAGED" or order.committed:
            return
        self._orders.pop(order.client_oid, None)
        self._exchange_to_client.pop(order.exchange_oid, None)

    def _submit_worker(self, kind: str, payload) -> bool:
        if not self._worker_running:
            return False
        command = _EngineCommand(
            kind=kind,
            payload=payload,
            wait_for_result=False,
        )
        try:
            self._commands.put_nowait(command)
            return True
        except queue.Full:
            self._fault("PAPER_COMMAND_QUEUE_FULL")
            return False

    # ------------------------------------------------------------------
    # Order admission, matching, fills, and cancellation
    # ------------------------------------------------------------------

    def _stage_order(
        self,
        request: OrderRequest,
        client_oid: str,
    ) -> GatewayCommandResult:
        existing = self._orders.get(client_oid)
        if existing is not None:
            if self._same_request(existing.request, request):
                return GatewayCommandResult(
                    CommandOutcome.ACKNOWLEDGED,
                    exchange_oid=existing.exchange_oid,
                    response=_PaperResponse(200, self._order_payload(existing)),
                )
            return GatewayCommandResult(
                CommandOutcome.REJECTED,
                error_code="PAPER_DUPLICATE_CLIENT_ID",
                error_message="client order ID already belongs to another request",
            )

        rejection = self._validate_request(request)
        if rejection:
            return GatewayCommandResult(
                CommandOutcome.REJECTED,
                error_code="PAPER_ORDER_REJECTED",
                error_message=rejection,
                response=_PaperResponse(
                    400,
                    {"code": "PAPER_ORDER_REJECTED", "msg": rejection},
                ),
            )

        self._accept_sequence += 1
        self._exchange_sequence += 1
        exchange_oid = f"PAPER-{self._exchange_sequence:020d}"
        now_ms = int(time.time() * 1000)
        now_monotonic = time.perf_counter()
        if request.time_in_force == TIF_RPI:
            fill_model = (
                "rpi_public_trade_proxy"
                if self.rpi_fill_model == "public_trade_proxy"
                else "rpi_disabled"
            )
        else:
            fill_model = "orderbook"
        order = _PaperOrder(
            client_oid=client_oid,
            exchange_oid=exchange_oid,
            request=request,
            accept_seq=self._accept_sequence,
            created_ms=now_ms,
            created_monotonic=now_monotonic,
            update_ms=now_ms,
            fill_model=fill_model,
            cancel_generation_at_stage=self._cancel_generations.get(
                request.symbol,
                0,
            ),
        )
        self._orders[client_oid] = order
        self._exchange_to_client[exchange_oid] = client_oid
        self._prune_terminal_orders()
        return GatewayCommandResult(
            CommandOutcome.ACKNOWLEDGED,
            exchange_oid=exchange_oid,
            response=_PaperResponse(200, self._order_payload(order)),
        )

    def _commit_staged_order(self, client_oid: str):
        order = self._orders.get(str(client_oid or ""))
        if order is None:
            return False
        if order.status != "STAGED":
            return order.committed or order.status in _TERMINAL_STATUSES

        cancel_generation = self._cancel_generations.get(order.request.symbol, 0)
        if (
            order.pending_cancel_reason
            or cancel_generation != order.cancel_generation_at_stage
        ):
            reason = order.pending_cancel_reason or "PAPER_CANCEL_ALL_BARRIER"
            return self._cancel_staged_order(order, reason)

        clock_error, clock_message = self._clock_health_rejection(
            order.request
        )
        if clock_error:
            return self._reject_staged_order(
                order,
                f"{clock_error}:{clock_message}",
            )

        # Admission and commit are separated by the OMS durability barrier.
        # Revalidate against the latest book/position state so a GTX/RPI order
        # cannot become marketable, and reduce-only capacity cannot shrink,
        # while the order is staged.
        rejection = self._validate_request(order.request)
        if rejection:
            return self._reject_staged_order(order, rejection)

        order.committed = True
        order.status = "NEW"
        transaction_time = time.time()
        order.update_ms = int(transaction_time * 1000)
        self._emit_order_event(
            order,
            status="NEW",
            transaction_time=transaction_time,
        )

        request = order.request
        if request.order_type == "MARKET" or request.time_in_force in {
            TIF_IOC,
            TIF_FOK,
        }:
            self._match_immediate(order)
        elif request.time_in_force not in {TIF_GTX, TIF_RPI}:
            self._match_immediate(order)
        else:
            self._insert_into_local_queue(order)
        return True

    def _cancel_staged_order(self, order: _PaperOrder, reason: str) -> bool:
        """Publish a deferred cancel only after the OMS commit barrier."""
        order.committed = True
        order.status = "CANCELED"
        order.terminal_reason = str(reason or "PAPER_CANCEL_BEFORE_COMMIT")
        order.pending_cancel_reason = ""
        transaction_time = time.time()
        order.update_ms = int(transaction_time * 1000)
        self._emit_order_event(
            order,
            status="CANCELED",
            transaction_time=transaction_time,
        )
        logger.info(
            f"[BINANCE_PAPER] Canceled on commit {order.client_oid}: "
            f"{order.terminal_reason}"
        )
        return True

    def _reject_staged_order(self, order: _PaperOrder, reason: str) -> bool:
        """Publish one terminal rejection after the OMS commit barrier."""
        order.committed = True
        order.status = "REJECTED"
        order.terminal_reason = str(reason or "PAPER_COMMIT_REJECTED")
        transaction_time = time.time()
        order.update_ms = int(transaction_time * 1000)
        self._emit_order_event(
            order,
            status="REJECTED",
            transaction_time=transaction_time,
        )
        logger.info(
            f"[BINANCE_PAPER] Rejected on commit {order.client_oid}: "
            f"{order.terminal_reason}"
        )
        return True

    def _validate_request(self, request: OrderRequest) -> str:
        request.symbol = str(request.symbol or "").upper()
        request.side = str(request.side or "").upper()
        request.order_type = str(request.order_type or "LIMIT").upper()
        request.time_in_force = str(request.time_in_force or TIF_GTC).upper()

        if request.symbol not in self.symbols:
            return f"unknown_symbol:{request.symbol}"
        if request.side not in {"BUY", "SELL"}:
            return f"invalid_side:{request.side}"
        if request.order_type not in {"LIMIT", "MARKET"}:
            return f"unsupported_order_type:{request.order_type}"
        if request.order_type == "LIMIT" and request.time_in_force not in {
            TIF_GTC,
            TIF_IOC,
            TIF_FOK,
            TIF_GTX,
            TIF_RPI,
        }:
            return f"unsupported_time_in_force:{request.time_in_force}"
        if request.order_type == "MARKET" and request.time_in_force not in {
            TIF_GTC,
            TIF_IOC,
            TIF_FOK,
        }:
            return f"market_order_incompatible_tif:{request.time_in_force}"
        if request.time_in_force == TIF_RPI and request.order_type != "LIMIT":
            return "rpi_requires_limit_order"
        if request.post_only and request.time_in_force not in {TIF_GTX, TIF_RPI}:
            return "post_only_requires_gtx_or_rpi"
        if not math.isfinite(float(request.volume)) or float(request.volume) <= 0.0:
            return "invalid_quantity"
        if request.order_type == "LIMIT":
            if not math.isfinite(float(request.price)) or float(request.price) <= 0.0:
                return "invalid_price"

        info = ref_data_manager.get_info(request.symbol)
        if request.time_in_force == TIF_RPI and not (
            info is not None and info.supports_rpi
        ):
            return f"rpi_unsupported_symbol:{request.symbol}"
        if info is not None:
            if float(request.volume) + 1e-12 < float(info.min_qty):
                return f"quantity_below_min:{request.volume}<{info.min_qty}"
            if not self._is_step_aligned(float(request.volume), float(info.step_size)):
                return f"quantity_not_step_aligned:{request.volume}:{info.step_size}"
            if request.order_type == "LIMIT" and not self._is_step_aligned(
                float(request.price),
                float(info.tick_size),
            ):
                return f"price_not_tick_aligned:{request.price}:{info.tick_size}"

        book = self._books.get(request.symbol)
        if book is None or not book.bids or not book.asks:
            return f"market_data_unavailable:{request.symbol}"
        best_bid, _ = book.get_best_bid()
        best_ask, _ = book.get_best_ask()
        reference_price = (
            float(request.price)
            if request.order_type == "LIMIT"
            else (best_ask if request.side == "BUY" else best_bid)
        )
        if reference_price <= 0.0:
            return f"market_data_unavailable:{request.symbol}"
        if info is not None:
            min_notional = max(0.0, float(info.min_notional or 0.0))
            if reference_price * float(request.volume) + 1e-12 < min_notional:
                return (
                    f"notional_below_min:{reference_price * float(request.volume)}"
                    f"<{min_notional}"
                )

        crosses = self._would_cross(request, best_bid, best_ask)
        if request.time_in_force in {TIF_GTX, TIF_RPI} and crosses:
            return f"post_only_would_cross:{request.symbol}"

        if request.reduce_only:
            position = self._positions.get(request.symbol, _PaperPosition())
            if abs(position.quantity) <= 1e-12:
                return f"reduce_only_without_position:{request.symbol}"
            if position.quantity > 0.0 and request.side != "SELL":
                return f"reduce_only_wrong_side:{request.symbol}"
            if position.quantity < 0.0 and request.side != "BUY":
                return f"reduce_only_wrong_side:{request.symbol}"
            reserved = sum(
                candidate.remaining
                for candidate in self._orders.values()
                if candidate.active
                and candidate.request.symbol == request.symbol
                and candidate.request.reduce_only
                and candidate.request.side == request.side
            )
            reducible = max(0.0, abs(position.quantity) - reserved)
            if float(request.volume) > reducible + 1e-9:
                return (
                    f"reduce_only_exceeds_position:{request.symbol}:"
                    f"requested={request.volume}>available={reducible}"
                )
        return ""

    def _match_immediate(self, order: _PaperOrder):
        if not order.active or not order.committed:
            return
        request = order.request
        liquidity = self._liquidity.get(request.symbol)
        if not liquidity:
            if request.order_type == "MARKET" or request.time_in_force in {
                TIF_IOC,
                TIF_FOK,
            }:
                self._expire_order(order, "PAPER_NO_LIQUIDITY")
            else:
                self._insert_into_local_queue(order)
            return

        side_key = "asks" if request.side == "BUY" else "bids"
        levels = liquidity[side_key]
        prices = sorted(levels) if request.side == "BUY" else sorted(levels, reverse=True)
        eligible_prices = [price for price in prices if self._price_is_executable(order, price)]

        if request.time_in_force == TIF_FOK:
            known_quantity = sum(max(0.0, levels.get(price, 0.0)) for price in eligible_prices)
            fill_cap = self._reduce_only_fill_cap(order)
            required = min(order.remaining, fill_cap)
            if required <= 1e-12 or known_quantity + 1e-12 < required:
                self._expire_order(order, "PAPER_FOK_UNFILLED")
                return

        for price in eligible_prices:
            if not order.active or order.remaining <= 1e-12:
                break
            available = max(0.0, float(levels.get(price, 0.0) or 0.0))
            if available <= 1e-12:
                continue
            quantity = min(order.remaining, available, self._reduce_only_fill_cap(order))
            if quantity <= 1e-12:
                break
            levels[price] = max(0.0, available - quantity)
            self._apply_fill(order, quantity, price, is_maker=False)

        if order.active and order.remaining > 1e-12:
            if request.order_type == "MARKET" or request.time_in_force in {
                TIF_IOC,
                TIF_FOK,
            }:
                self._expire_order(order, "PAPER_IMMEDIATE_REMAINDER")
            else:
                self._insert_into_local_queue(order)

    def _on_market_trade(self, trade: AggTradeData):
        if trade.price <= 0.0 or trade.quantity <= 0.0:
            return False
        last_id = int(self._last_market_trade_id.get(trade.symbol, -1))
        if int(trade.trade_id) >= 0 and int(trade.trade_id) <= last_id:
            return False
        if int(trade.trade_id) >= 0:
            self._last_market_trade_id[trade.symbol] = int(trade.trade_id)

        maker_side = "BUY" if trade.maker_is_buyer else "SELL"
        candidates = sorted(
            (
                order
                for order in self._orders.values()
                if order.active
                and order.committed
                and order.request.symbol == trade.symbol
                and order.request.side == maker_side
                and order.request.order_type == "LIMIT"
                and order.request.time_in_force not in {TIF_IOC, TIF_FOK}
            ),
            key=self._passive_match_priority,
        )
        for order in candidates:
            if order.request.time_in_force == TIF_RPI:
                if self.rpi_fill_model != "public_trade_proxy":
                    continue
            price_relation = self._passive_trade_relation(order, float(trade.price))
            if price_relation == "not_reached":
                continue
            if self._reduce_only_fill_cap(order) <= 1e-12:
                self._expire_order(order, "PAPER_REDUCE_ONLY_EXHAUSTED")
                continue
            ahead_before = max(0.0, order.queue_ahead)
            if price_relation == "through":
                quantity = min(order.remaining, self._reduce_only_fill_cap(order))
            else:
                order.queue_ahead = max(0.0, ahead_before - float(trade.quantity))
                eligible_quantity = max(0.0, float(trade.quantity) - ahead_before)
                quantity = min(
                    order.remaining,
                    eligible_quantity,
                    self._reduce_only_fill_cap(order),
                )
            if quantity > 1e-12:
                # A passive limit executes at its resting price.  A trade
                # through that price proves the order would already have filled.
                self._apply_fill(
                    order,
                    quantity,
                    float(order.request.price),
                    is_maker=True,
                    fill_context={
                        "fill_trigger": price_relation,
                        "market_trade_id": int(trade.trade_id),
                        "market_trade_price": float(trade.price),
                        "market_trade_qty": float(trade.quantity),
                        "market_trade_exchange_time": float(
                            trade.exchange_timestamp or 0.0
                        ),
                        "market_trade_received_time": float(
                            trade.received_timestamp or 0.0
                        ),
                        "market_trade_clock_offset_ms": (
                            float(trade.clock_offset_ms)
                            if trade.clock_offset_ms is not None
                            else None
                        ),
                        "market_trade_transport_latency_ms": (
                            (
                                float(trade.corrected_received_timestamp)
                                - float(trade.exchange_timestamp)
                            )
                            * 1000.0
                            if trade.corrected_received_timestamp > 0.0
                            and trade.exchange_timestamp > 0.0
                            else None
                        ),
                        "market_trade_local_age_ms": (
                            max(
                                0.0,
                                (
                                    time.perf_counter()
                                    - float(trade.received_monotonic)
                                )
                                * 1000.0,
                            )
                            if trade.received_monotonic > 0.0
                            else None
                        ),
                        "queue_ahead_before": ahead_before,
                    },
                )
        return True

    def _on_book(self, book: OrderBook):
        previous = self._books.get(book.symbol)
        if previous is not None and self.cancel_ahead_fraction > 0.0:
            self._apply_conservative_cancel_ahead(previous, book)
        self._books[book.symbol] = book
        self._liquidity[book.symbol] = {
            "bids": {float(price): float(qty) for price, qty in book.bids.items()},
            "asks": {float(price): float(qty) for price, qty in book.asks.items()},
        }
        return True

    def _apply_fill(
        self,
        order: _PaperOrder,
        quantity: float,
        price: float,
        *,
        is_maker: bool,
        fill_context: dict | None = None,
    ):
        quantity = min(float(quantity), order.remaining, self._reduce_only_fill_cap(order))
        if quantity <= 1e-12:
            return False
        transaction_time = time.time()
        context = dict(fill_context) if isinstance(fill_context, dict) else {}
        book = self._books.get(order.request.symbol)
        best_bid_at_fill = None
        best_ask_at_fill = None
        if book is not None:
            best_bid_at_fill = float(book.get_best_bid()[0] or 0.0) or None
            best_ask_at_fill = float(book.get_best_ask()[0] or 0.0) or None
        mid_at_fill = (
            (best_bid_at_fill + best_ask_at_fill) / 2.0
            if best_bid_at_fill is not None and best_ask_at_fill is not None
            else None
        )
        quote_age_ms = max(
            0.0,
            (time.perf_counter() - order.created_monotonic) * 1000.0,
        )
        realized_pnl = self._apply_position_fill(
            order.request.symbol,
            order.request.side,
            quantity,
            float(price),
        )
        fee_rate = self._fee_rate(order, is_maker)
        commission = quantity * float(price) * fee_rate
        quote_asset = self._quote_asset(order.request.symbol)
        self._balances.setdefault(quote_asset, 0.0)
        self._balances[quote_asset] += realized_pnl - commission

        order.cum_filled_qty += quantity
        order.cumulative_cost += quantity * float(price)
        order.avg_price = order.cumulative_cost / order.cum_filled_qty
        order.update_ms = int(transaction_time * 1000)
        order.status = "FILLED" if order.remaining <= 1e-9 else "PARTIALLY_FILLED"

        self._paper_trade_sequence += 1
        paper_trade_id = self._paper_trade_sequence
        trade_payload = {
            "symbol": order.request.symbol,
            "id": paper_trade_id,
            "orderId": order.exchange_oid,
            "side": order.request.side,
            "price": f"{float(price):.12g}",
            "qty": f"{quantity:.12g}",
            "realizedPnl": f"{realized_pnl:.12g}",
            "commission": f"{commission:.12g}",
            "commissionAsset": quote_asset,
            "time": order.update_ms,
            "maker": bool(is_maker),
            "buyer": order.request.side == "BUY",
            "_simulated": True,
            "_fillModel": order.fill_model,
        }
        self._trades.append(trade_payload)
        self._emit_order_event(
            order,
            status=order.status,
            filled_qty=quantity,
            filled_price=float(price),
            transaction_time=transaction_time,
            commission=commission,
            commission_asset=quote_asset,
            realized_pnl=realized_pnl,
            is_maker=is_maker,
            trade_id=paper_trade_id,
            fill_trigger=str(
                context.get(
                    "fill_trigger",
                    "orderbook" if not is_maker else "",
                )
                or ""
            ),
            market_trade_id=int(context.get("market_trade_id", -1) or -1),
            market_trade_price=context.get("market_trade_price"),
            market_trade_qty=context.get("market_trade_qty"),
            market_trade_exchange_time=context.get(
                "market_trade_exchange_time"
            ),
            market_trade_received_time=context.get(
                "market_trade_received_time"
            ),
            market_trade_clock_offset_ms=context.get(
                "market_trade_clock_offset_ms"
            ),
            market_trade_transport_latency_ms=context.get(
                "market_trade_transport_latency_ms"
            ),
            market_trade_local_age_ms=context.get(
                "market_trade_local_age_ms"
            ),
            queue_ahead_before=context.get("queue_ahead_before"),
            best_bid_at_fill=best_bid_at_fill,
            best_ask_at_fill=best_ask_at_fill,
            mid_at_fill=mid_at_fill,
            quote_age_ms=quote_age_ms,
        )
        self._emit_account_update(
            order.request.symbol,
            transaction_time=transaction_time,
            reason="ORDER",
        )
        return True

    def _apply_position_fill(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
    ) -> float:
        position = self._positions.setdefault(symbol, _PaperPosition())
        current = float(position.quantity)
        average = float(position.entry_price)
        signed_quantity = quantity if side == "BUY" else -quantity
        next_quantity = current + signed_quantity
        realized = 0.0

        increasing = (
            abs(current) <= 1e-12
            or (current > 0.0 and signed_quantity > 0.0)
            or (current < 0.0 and signed_quantity < 0.0)
        )
        if increasing:
            total_quantity = abs(current) + quantity
            position.entry_price = (
                (abs(current) * average + quantity * price) / total_quantity
                if total_quantity > 0.0
                else 0.0
            )
        else:
            closing_quantity = min(abs(current), quantity)
            if current > 0.0:
                realized = (price - average) * closing_quantity
            else:
                realized = (average - price) * closing_quantity

        position.quantity = next_quantity
        if abs(next_quantity) <= 1e-9:
            position.quantity = 0.0
            position.entry_price = 0.0
        elif current > 0.0 > next_quantity or current < 0.0 < next_quantity:
            position.entry_price = price
        return realized

    def _expire_order(self, order: _PaperOrder, reason: str):
        if not order.active:
            return False
        removed = order.remaining
        order.status = "EXPIRED"
        order.update_ms = int(time.time() * 1000)
        self._remove_from_later_local_queue(order, removed)
        self._emit_order_event(
            order,
            status="EXPIRED",
            transaction_time=order.update_ms / 1000.0,
        )
        logger.info(f"[BINANCE_PAPER] Expired {order.client_oid}: {reason}")
        return True

    def _cancel_order_internal(self, order: _PaperOrder, reason: str):
        if not order.active:
            return False
        removed = order.remaining
        order.status = "CANCELED"
        order.update_ms = int(time.time() * 1000)
        self._remove_from_later_local_queue(order, removed)
        self._emit_order_event(
            order,
            status="CANCELED",
            transaction_time=order.update_ms / 1000.0,
        )
        logger.info(f"[BINANCE_PAPER] Canceled {order.client_oid}: {reason}")
        return True

    def _cancel_by_request(self, request: CancelRequest):
        symbol = str(request.symbol or "").upper()
        identifier = str(request.order_id or "")
        order = self._find_order(identifier)
        if order is None or (symbol and order.request.symbol != symbol):
            return None
        if order.status == "STAGED":
            # The OMS has durably persisted the ACK before it can issue this
            # cancel. Keep the venue event deferred until submit commit, while
            # acknowledging the cancel immediately to the concurrent caller.
            order.pending_cancel_reason = "PAPER_CANCEL_BEFORE_COMMIT"
            order.update_ms = int(time.time() * 1000)
            payload = self._order_payload(order)
            payload["status"] = "CANCELED"
            payload["_paperPendingCancel"] = True
            return payload
        if not order.active:
            return None
        self._cancel_order_internal(order, "PAPER_CANCEL")
        return self._order_payload(order)

    def _cancel_all_internal(self, symbol: str, reason: str):
        symbol = str(symbol or "").upper()
        self._cancel_generations[symbol] = (
            self._cancel_generations.get(symbol, 0) + 1
        )
        canceled = []
        for order in sorted(self._orders.values(), key=lambda item: item.accept_seq):
            if order.request.symbol != symbol or not order.active:
                continue
            if self._cancel_order_internal(order, reason):
                canceled.append(self._order_payload(order))
        return canceled

    def _set_dms_internal(self, symbol: str, countdown_ms: int):
        if symbol not in self.symbols:
            raise ValueError(f"unknown paper DMS symbol: {symbol}")
        if countdown_ms < 0:
            raise ValueError("countdown_time_ms cannot be negative")
        if countdown_ms == 0:
            self._dms_deadlines.pop(symbol, None)
        else:
            self._dms_deadlines[symbol] = time.perf_counter() + countdown_ms / 1000.0
        return {
            "symbol": symbol,
            "countdownTime": countdown_ms,
            "_simulated": True,
        }

    def _check_dms_deadlines(self):
        if not self._dms_deadlines:
            return
        now = time.perf_counter()
        expired = [
            symbol
            for symbol, deadline in self._dms_deadlines.items()
            if deadline <= now
        ]
        for symbol in expired:
            self._dms_deadlines.pop(symbol, None)
            self._cancel_all_internal(symbol, reason="PAPER_DMS_EXPIRED")
            self.event_engine.put(
                Event(
                    EVENT_SYSTEM_HEALTH,
                    f"PAPER_DMS_TRIGGERED:{symbol}",
                )
            )

    # ------------------------------------------------------------------
    # Queue model and price/fee helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _local_queue_priority(order: _PaperOrder):
        # Binance gives every non-RPI order priority over every RPI order at
        # the same price level, regardless of arrival order. FIFO applies
        # within each class.
        is_rpi = order.request.time_in_force == TIF_RPI
        return (1 if is_rpi else 0, order.accept_seq)

    @classmethod
    def _passive_match_priority(cls, order: _PaperOrder):
        price = float(order.request.price)
        price_priority = -price if order.request.side == "BUY" else price
        return (price_priority, *cls._local_queue_priority(order))

    @staticmethod
    def _same_local_level(left: _PaperOrder, right: _PaperOrder) -> bool:
        return (
            left.request.symbol == right.request.symbol
            and left.request.side == right.request.side
            and abs(float(left.request.price) - float(right.request.price)) <= 1e-12
        )

    def _insert_into_local_queue(self, order: _PaperOrder):
        if order.queue_inserted or not order.active or not order.committed:
            return
        self._set_initial_queue_ahead(order)
        order.queue_inserted = True
        order_priority = self._local_queue_priority(order)
        for candidate in self._orders.values():
            if (
                candidate.client_oid != order.client_oid
                and candidate.active
                and candidate.committed
                and candidate.queue_inserted
                and self._same_local_level(candidate, order)
                and order_priority < self._local_queue_priority(candidate)
            ):
                candidate.queue_ahead += order.remaining

    def _set_initial_queue_ahead(self, order: _PaperOrder):
        if not order.active or order.request.order_type != "LIMIT":
            return
        book = self._books.get(order.request.symbol)
        if book is None:
            order.queue_ahead = 0.0
            return
        own_side = book.bids if order.request.side == "BUY" else book.asks
        external_ahead = max(
            0.0,
            float(own_side.get(float(order.request.price), 0.0) or 0.0),
        )
        local_ahead = sum(
            candidate.remaining
            for candidate in self._orders.values()
            if candidate.client_oid != order.client_oid
            and candidate.active
            and candidate.committed
            and candidate.queue_inserted
            and self._same_local_level(candidate, order)
            and self._local_queue_priority(candidate)
            < self._local_queue_priority(order)
        )
        order.queue_ahead = external_ahead + local_ahead

    def _remove_from_later_local_queue(self, order: _PaperOrder, removed_quantity: float):
        if removed_quantity <= 1e-12 or not order.queue_inserted:
            return
        removed_priority = self._local_queue_priority(order)
        for candidate in self._orders.values():
            if (
                candidate.active
                and candidate.committed
                and candidate.queue_inserted
                and self._same_local_level(candidate, order)
                and removed_priority < self._local_queue_priority(candidate)
            ):
                candidate.queue_ahead = max(
                    0.0,
                    candidate.queue_ahead - removed_quantity,
                )
        order.queue_inserted = False

    def _apply_conservative_cancel_ahead(self, previous: OrderBook, current: OrderBook):
        for order in self._orders.values():
            if not order.active or not order.committed or order.request.symbol != current.symbol:
                continue
            previous_side = previous.bids if order.request.side == "BUY" else previous.asks
            current_side = current.bids if order.request.side == "BUY" else current.asks
            price = float(order.request.price)
            # If a level falls outside the published book, absence is not proof
            # that it was canceled.  Only compare levels present in both views.
            if price not in previous_side or price not in current_side:
                continue
            reduction = max(
                0.0,
                float(previous_side[price]) - float(current_side[price]),
            )
            order.queue_ahead = max(
                0.0,
                order.queue_ahead - reduction * self.cancel_ahead_fraction,
            )

    def _would_cross(self, request: OrderRequest, best_bid: float, best_ask: float):
        if request.order_type == "MARKET":
            return True
        if request.side == "BUY":
            return best_ask > 0.0 and float(request.price) >= best_ask - 1e-12
        return best_bid > 0.0 and float(request.price) <= best_bid + 1e-12

    def _price_is_executable(self, order: _PaperOrder, external_price: float):
        request = order.request
        if request.order_type == "LIMIT":
            if request.side == "BUY":
                return external_price <= float(request.price) + 1e-12
            return external_price >= float(request.price) - 1e-12

        book = self._books.get(request.symbol)
        if book is None or self.market_order_max_slippage_bps <= 0.0:
            return True
        best_price = (
            float(book.get_best_ask()[0])
            if request.side == "BUY"
            else float(book.get_best_bid()[0])
        )
        if best_price <= 0.0:
            return False
        distance_bps = abs(external_price - best_price) / best_price * 10_000.0
        return distance_bps <= self.market_order_max_slippage_bps + 1e-9

    def _passive_trade_relation(self, order: _PaperOrder, trade_price: float):
        order_price = float(order.request.price)
        tolerance = max(1e-12, abs(order_price) * 1e-12)
        if abs(trade_price - order_price) <= tolerance:
            return "at_price"
        if order.request.side == "BUY":
            return "through" if trade_price < order_price else "not_reached"
        return "through" if trade_price > order_price else "not_reached"

    def _reduce_only_fill_cap(self, order: _PaperOrder):
        if not order.request.reduce_only:
            return order.remaining
        position = self._positions.get(order.request.symbol, _PaperPosition())
        if position.quantity > 1e-12 and order.request.side == "SELL":
            return min(order.remaining, position.quantity)
        if position.quantity < -1e-12 and order.request.side == "BUY":
            return min(order.remaining, abs(position.quantity))
        return 0.0

    def _fee_rate(self, order: _PaperOrder, is_maker: bool):
        if order.request.time_in_force == TIF_RPI:
            return self._rpi_fee_rate(order.request.symbol)
        return max(0.0, self.maker_fee if is_maker else self.taker_fee)

    def _rpi_fee_rate(self, symbol: str):
        return max(
            0.0,
            resolve_passive_fee_rate(
                maker_rate=self.maker_fee,
                symbol=symbol,
                is_rpi=True,
                rpi_commission_rates=self.rpi_commission_rates,
                default_rpi_commission_rate=self.rpi_commission_rate,
            ),
        )

    # ------------------------------------------------------------------
    # Exchange-shaped snapshots and event publication
    # ------------------------------------------------------------------

    def _query_internal(self, query_name: str, args):
        if query_name == "account":
            return self._account_payload()
        if query_name == "positions":
            return [self._position_payload(symbol) for symbol in self.symbols]
        if query_name == "open_orders":
            return [
                self._order_payload(order)
                for order in sorted(self._orders.values(), key=lambda item: item.accept_seq)
                if order.active
            ]
        if query_name == "order":
            symbol, identifier = args
            order = self._find_order(identifier)
            if order is None or (symbol and order.request.symbol != symbol):
                return None
            return self._order_payload(order)
        if query_name == "all_orders":
            symbol, kwargs = args
            return self._all_orders_payload(symbol, kwargs)
        if query_name == "user_trades":
            symbol, kwargs = args
            return self._user_trades_payload(symbol, kwargs)
        raise ValueError(f"unsupported paper query: {query_name}")

    def _order_payload(self, order: _PaperOrder):
        return {
            "symbol": order.request.symbol,
            "orderId": order.exchange_oid,
            "clientOrderId": order.client_oid,
            "price": f"{float(order.request.price):.12g}",
            "avgPrice": f"{order.avg_price:.12g}",
            "origQty": f"{float(order.request.volume):.12g}",
            "executedQty": f"{order.cum_filled_qty:.12g}",
            "cumQuote": f"{order.cumulative_cost:.12g}",
            "status": order.status,
            "timeInForce": order.request.time_in_force,
            "type": order.request.order_type,
            "reduceOnly": bool(order.request.reduce_only),
            "side": order.request.side,
            "positionSide": "BOTH",
            "time": order.created_ms,
            "updateTime": order.update_ms,
            "_simulated": True,
            "_paperCommitted": bool(order.committed),
            "_paperFillModel": order.fill_model,
            "_paperQueueAhead": order.queue_ahead,
            "_paperPendingCancel": bool(order.pending_cancel_reason),
            "_paperTerminalReason": order.terminal_reason,
        }

    def _position_payload(self, symbol: str):
        position = self._positions.get(symbol, _PaperPosition())
        mark = self._mark_price(symbol)
        unrealized = (
            (mark - position.entry_price) * position.quantity
            if mark > 0.0 and abs(position.quantity) > 1e-12
            else 0.0
        )
        return {
            "symbol": symbol,
            "positionAmt": f"{position.quantity:.12g}",
            "entryPrice": f"{position.entry_price:.12g}",
            "markPrice": f"{mark:.12g}",
            "unRealizedProfit": f"{unrealized:.12g}",
            "liquidationPrice": "0",
            "leverage": str(self.leverage),
            "marginType": self.target_margin_type.lower(),
            "positionSide": "BOTH",
            "updateTime": int(time.time() * 1000),
            "_simulated": True,
        }

    def _account_payload(self):
        metrics = self._account_metrics()
        assets = []
        for asset in sorted(self._balances):
            wallet = float(self._balances.get(asset, 0.0) or 0.0)
            asset_available = float(metrics["available_by_asset"].get(asset, wallet))
            assets.append(
                {
                    "asset": asset,
                    "walletBalance": f"{wallet:.12g}",
                    "availableBalance": f"{max(0.0, asset_available):.12g}",
                    "crossWalletBalance": f"{wallet:.12g}",
                    "unrealizedProfit": f"{metrics['unrealized_by_asset'].get(asset, 0.0):.12g}",
                    "marginBalance": f"{metrics['margin_by_asset'].get(asset, wallet):.12g}",
                    "_simulated": True,
                }
            )
        return {
            "totalWalletBalance": f"{metrics['wallet_balance']:.12g}",
            "totalUnrealizedProfit": f"{metrics['unrealized_pnl']:.12g}",
            "totalMarginBalance": f"{metrics['margin_balance']:.12g}",
            "totalInitialMargin": f"{metrics['initial_margin']:.12g}",
            "totalMaintMargin": f"{metrics['maintenance_margin']:.12g}",
            "availableBalance": f"{metrics['available_balance']:.12g}",
            "assets": assets,
            "canTrade": self.state == GatewayState.READY,
            "updateTime": int(time.time() * 1000),
            "_simulated": True,
            "_environment": self.environment,
        }

    def _account_metrics(self):
        unrealized_by_asset: dict[str, float] = {}
        initial_by_asset: dict[str, float] = {}
        maintenance_by_asset: dict[str, float] = {}

        for symbol in set(self.symbols) | set(self._positions):
            asset = self._quote_asset(symbol)
            position = self._positions.get(symbol, _PaperPosition())
            mark = self._mark_price(symbol)
            if mark <= 0.0:
                mark = position.entry_price
            unrealized_by_asset[asset] = unrealized_by_asset.get(asset, 0.0) + (
                (mark - position.entry_price) * position.quantity
                if mark > 0.0 and abs(position.quantity) > 1e-12
                else 0.0
            )
            position_notional = abs(position.quantity) * max(0.0, mark)
            initial_by_asset[asset] = initial_by_asset.get(asset, 0.0) + (
                position_notional / self.leverage
            )
            maintenance_by_asset[asset] = maintenance_by_asset.get(asset, 0.0) + (
                position_notional * self.maintenance_margin_rate
            )

        for order in self._orders.values():
            if not order.active or order.request.reduce_only:
                continue
            asset = self._quote_asset(order.request.symbol)
            reference = self._mark_price(order.request.symbol) or float(order.request.price)
            initial_by_asset[asset] = initial_by_asset.get(asset, 0.0) + (
                order.remaining * max(0.0, reference) / self.leverage
            )

        wallet_balance = sum(float(value) for value in self._balances.values())
        unrealized_pnl = sum(unrealized_by_asset.values())
        initial_margin = sum(initial_by_asset.values())
        maintenance_margin = sum(maintenance_by_asset.values())
        margin_balance = wallet_balance + unrealized_pnl
        available_balance = max(0.0, margin_balance - initial_margin)
        available_by_asset = {}
        margin_by_asset = {}
        for asset in set(self._balances) | set(unrealized_by_asset) | set(initial_by_asset):
            asset_margin = float(self._balances.get(asset, 0.0)) + float(
                unrealized_by_asset.get(asset, 0.0)
            )
            margin_by_asset[asset] = asset_margin
            available_by_asset[asset] = max(
                0.0,
                asset_margin - float(initial_by_asset.get(asset, 0.0)),
            )
        return {
            "wallet_balance": wallet_balance,
            "unrealized_pnl": unrealized_pnl,
            "margin_balance": margin_balance,
            "initial_margin": initial_margin,
            "maintenance_margin": maintenance_margin,
            "available_balance": available_balance,
            "unrealized_by_asset": unrealized_by_asset,
            "available_by_asset": available_by_asset,
            "margin_by_asset": margin_by_asset,
        }

    def _emit_order_event(
        self,
        order: _PaperOrder,
        *,
        status: str,
        transaction_time: float,
        filled_qty: float = 0.0,
        filled_price: float = 0.0,
        commission: float | None = None,
        commission_asset: str = "",
        realized_pnl: float | None = None,
        is_maker: bool | None = None,
        trade_id: int = -1,
        fill_trigger: str = "",
        market_trade_id: int = -1,
        market_trade_price: float | None = None,
        market_trade_qty: float | None = None,
        market_trade_exchange_time: float | None = None,
        market_trade_received_time: float | None = None,
        market_trade_clock_offset_ms: float | None = None,
        market_trade_transport_latency_ms: float | None = None,
        market_trade_local_age_ms: float | None = None,
        queue_ahead_before: float | None = None,
        best_bid_at_fill: float | None = None,
        best_ask_at_fill: float | None = None,
        mid_at_fill: float | None = None,
        quote_age_ms: float | None = None,
    ):
        self._event_sequence += 1
        self.on_order_update(
            ExchangeOrderUpdate(
                client_oid=order.client_oid,
                exchange_oid=order.exchange_oid,
                symbol=order.request.symbol,
                status=status,
                filled_qty=float(filled_qty),
                filled_price=float(filled_price),
                cum_filled_qty=float(order.cum_filled_qty),
                update_time=float(transaction_time),
                seq=self._event_sequence,
                commission=commission,
                commission_asset=commission_asset,
                realized_pnl=realized_pnl,
                is_maker=is_maker,
                trade_id=int(trade_id),
                order_type=order.request.order_type,
                time_in_force=order.request.time_in_force,
                fill_model=order.fill_model,
                fill_trigger=fill_trigger,
                market_trade_id=market_trade_id,
                market_trade_price=market_trade_price,
                market_trade_qty=market_trade_qty,
                market_trade_exchange_time=market_trade_exchange_time,
                market_trade_received_time=market_trade_received_time,
                market_trade_clock_offset_ms=market_trade_clock_offset_ms,
                market_trade_transport_latency_ms=(
                    market_trade_transport_latency_ms
                ),
                market_trade_local_age_ms=market_trade_local_age_ms,
                queue_ahead_before=queue_ahead_before,
                best_bid_at_fill=best_bid_at_fill,
                best_ask_at_fill=best_ask_at_fill,
                mid_at_fill=mid_at_fill,
                quote_age_ms=quote_age_ms,
            )
        )

    def _emit_account_update(
        self,
        symbol: str,
        *,
        transaction_time: float,
        reason: str,
    ):
        metrics = self._account_metrics()
        balances = {
            asset: {
                "wallet_balance": float(wallet),
                "available_balance": float(
                    metrics["available_by_asset"].get(asset, wallet)
                ),
            }
            for asset, wallet in self._balances.items()
        }
        position = self._positions.get(symbol, _PaperPosition())
        mark = self._mark_price(symbol)
        unrealized = (
            (mark - position.entry_price) * position.quantity
            if mark > 0.0 and abs(position.quantity) > 1e-12
            else 0.0
        )
        self.on_account_update(
            ExchangeAccountUpdate(
                asset=self.balance_asset,
                wallet_balance=float(metrics["wallet_balance"]),
                available_balance=float(metrics["available_balance"]),
                balances=balances,
                positions={
                    symbol: {
                        "volume": float(position.quantity),
                        "entry_price": float(position.entry_price),
                        "unrealized_pnl": float(unrealized),
                    }
                },
                reason=reason,
                event_time=float(transaction_time),
            )
        )

    def _emit_full_account_update(self, reason: str):
        if not self._worker_running:
            return False
        return self._submit_worker("emit_full_account", reason)

    def _emit_full_account_update_internal(self, reason: str):
        metrics = self._account_metrics()
        balances = {
            asset: {
                "wallet_balance": float(wallet),
                "available_balance": float(
                    metrics["available_by_asset"].get(asset, wallet)
                ),
            }
            for asset, wallet in self._balances.items()
        }
        positions = {}
        for symbol in self.symbols:
            position = self._positions.get(symbol, _PaperPosition())
            mark = self._mark_price(symbol)
            positions[symbol] = {
                "volume": float(position.quantity),
                "entry_price": float(position.entry_price),
                "unrealized_pnl": float(
                    (mark - position.entry_price) * position.quantity
                    if mark > 0.0 and abs(position.quantity) > 1e-12
                    else 0.0
                ),
            }
        self.on_account_update(
            ExchangeAccountUpdate(
                asset=self.balance_asset,
                wallet_balance=float(metrics["wallet_balance"]),
                available_balance=float(metrics["available_balance"]),
                balances=balances,
                positions=positions,
                reason=reason,
                event_time=time.time(),
            )
        )
        return True

    def _all_orders_payload(self, symbol: str, kwargs: dict):
        orders = [
            self._order_payload(order)
            for order in sorted(self._orders.values(), key=lambda item: item.accept_seq)
            if not symbol or order.request.symbol == symbol
        ]
        start_time = self._kwarg_int(kwargs, "start_time", "startTime")
        end_time = self._kwarg_int(kwargs, "end_time", "endTime")
        if start_time is not None:
            orders = [order for order in orders if int(order["time"]) >= start_time]
        if end_time is not None:
            orders = [order for order in orders if int(order["time"]) <= end_time]
        limit = self._kwarg_int(kwargs, "limit") or 500
        return orders[-max(1, min(1000, limit)) :]

    def _user_trades_payload(self, symbol: str, kwargs: dict):
        trades = [
            copy.deepcopy(trade)
            for trade in self._trades
            if not symbol or trade.get("symbol") == symbol
        ]
        from_id = self._kwarg_int(kwargs, "from_id", "fromId")
        start_time = self._kwarg_int(kwargs, "start_time", "startTime")
        end_time = self._kwarg_int(kwargs, "end_time", "endTime")
        if from_id is not None:
            trades = [trade for trade in trades if int(trade.get("id", -1)) >= from_id]
        if start_time is not None:
            trades = [trade for trade in trades if int(trade.get("time", 0)) >= start_time]
        if end_time is not None:
            trades = [trade for trade in trades if int(trade.get("time", 0)) <= end_time]
        limit = self._kwarg_int(kwargs, "limit") or 500
        page_size = max(1, min(1000, limit))
        if from_id is not None or start_time is not None:
            return trades[:page_size]
        return trades[-page_size:]

    def _find_order(self, identifier: str):
        identifier = str(identifier or "")
        order = self._orders.get(identifier)
        if order is not None:
            return order
        client_oid = self._exchange_to_client.get(identifier)
        return self._orders.get(client_oid) if client_oid else None

    def _mark_price(self, symbol: str):
        mark = float(self._marks.get(symbol, 0.0) or 0.0)
        if mark > 0.0:
            return mark
        book = self._books.get(symbol)
        if book is not None:
            bid = float(book.get_best_bid()[0] or 0.0)
            ask = float(book.get_best_ask()[0] or 0.0)
            if bid > 0.0 and ask > 0.0:
                return (bid + ask) / 2.0
            return max(bid, ask)
        position = self._positions.get(symbol)
        return float(position.entry_price) if position is not None else 0.0

    def _prune_terminal_orders(self):
        excess = len(self._orders) - self.max_order_history
        if excess <= 0:
            return
        removable = sorted(
            (
                order
                for order in self._orders.values()
                if order.status in _TERMINAL_STATUSES
            ),
            key=lambda order: (order.update_ms, order.accept_seq),
        )
        for order in removable[:excess]:
            self._orders.pop(order.client_oid, None)
            self._exchange_to_client.pop(order.exchange_oid, None)

    def _fault(self, reason: str):
        reason = str(reason or "PAPER_GATEWAY_FAILURE")
        with self._fault_lock:
            self._accepting_orders = False
            self.active = False
            self._mark_fallback_stop.set()
            if self._closing:
                return
            self._fault_epoch += 1
            with self._book_lock:
                self._book_generation += 1
                self.book_resyncing.clear()
                self.book_recovery_generation.clear()
                self.book_recovery_tokens.clear()
            self.set_state(GatewayState.ERROR)
            self.event_engine.put(
                Event(
                    EVENT_SYSTEM_HEALTH,
                    f"FREEZE_VENUE:{self.gateway_name}:{reason}",
                )
            )
        logger.error(f"[BINANCE_PAPER] {reason}")

    @staticmethod
    def _same_request(left: OrderRequest, right: OrderRequest):
        return (
            str(left.symbol).upper() == str(right.symbol).upper()
            and str(left.side).upper() == str(right.side).upper()
            and str(left.order_type).upper() == str(right.order_type).upper()
            and str(left.time_in_force).upper() == str(right.time_in_force).upper()
            and abs(float(left.price) - float(right.price)) <= 1e-12
            and abs(float(left.volume) - float(right.volume)) <= 1e-12
            and bool(left.post_only) == bool(right.post_only)
            and bool(left.reduce_only) == bool(right.reduce_only)
        )

    @staticmethod
    def _is_step_aligned(value: float, step: float):
        if step <= 0.0:
            return True
        units = value / step
        return abs(units - round(units)) <= max(1e-8, abs(units) * 1e-10)

    @staticmethod
    def _kwarg_int(kwargs: dict, *names: str):
        for name in names:
            value = kwargs.get(name)
            if value is not None:
                return int(value)
        return None

    @staticmethod
    def _finite_nonnegative(value, default: float):
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return float(default)
        if not math.isfinite(parsed) or parsed < 0.0:
            return float(default)
        return parsed

    @staticmethod
    def _quote_asset(symbol: str):
        symbol = str(symbol or "").upper()
        for suffix in ("USDT", "USDC", "BUSD", "FDUSD"):
            if symbol.endswith(suffix):
                return suffix
        return "USDT"

    @classmethod
    def _default_balance_asset(cls, symbols: list[str]):
        return cls._quote_asset(symbols[0]) if symbols else "USDT"


class PaperTruthSnapshotProvider:
    """Independent truth-plane facade over the local paper venue ledger."""

    def __init__(self, gateway: BinancePaperGateway):
        if not isinstance(gateway, BinancePaperGateway):
            raise TypeError("PaperTruthSnapshotProvider requires BinancePaperGateway")
        self.gateway = gateway
        self.gateway_name = gateway.gateway_name

    def get_account_info(self):
        return self.gateway.get_account_info()

    def get_all_positions(self):
        return self.gateway.get_all_positions()

    def get_open_orders(self):
        return self.gateway.get_open_orders()

    def get_order(self, symbol: str, order_id: str):
        return self.gateway.get_order(symbol, order_id)

    def get_all_orders(self, symbol: str, **kwargs):
        return self.gateway.get_all_orders(symbol, **kwargs)

    def get_user_trades(self, symbol: str, **kwargs):
        return self.gateway.get_user_trades(symbol, **kwargs)

    def get_income_history(self, **kwargs):
        return self.gateway.get_income_history(**kwargs)

    def get_commission_rate(self, symbol: str):
        return self.gateway.get_commission_rate(symbol)

    def close(self):
        return True
