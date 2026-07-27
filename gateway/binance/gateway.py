import json
import math
import socket
import threading
import time
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter

from event.type import (
    AggTradeData,
    CancelRequest,
    CommandOutcome,
    Event,
    ExchangeAccountUpdate,
    ExchangeOrderUpdate,
    GatewayState,
    GatewayCommandResult,
    MarkPriceData,
    OrderBookGapError,
    OrderRequest,
    EVENT_AGG_TRADE,
    EVENT_MARK_PRICE,
    EVENT_ORDERBOOK,
    EVENT_SYSTEM_HEALTH,
)
from gateway.base_gateway import BaseGateway
from infrastructure.binance_account_configuration import (
    AccountConfigurationVerificationError,
    verify_account_configuration,
)
from infrastructure.logger import logger
from infrastructure.time_service import time_service
from data.orderbook import LocalOrderBook

from .rest_api import BinanceRestApi
from .constants import (
    ACCOUNT_CONFIGURATION_MODE_APPLY,
    ACCOUNT_CONFIGURATION_MODE_VERIFY_ONLY,
    ACCOUNT_CONFIGURATION_MODES,
)
from .ws_api import BinanceWsApi


class HFTAdapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["socket_options"] = [
            (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
            (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
        ]
        super().init_poolmanager(connections, maxsize, block, **pool_kwargs)


class BinanceGateway(BaseGateway):
    supports_outbound_send_guard = True
    supports_emergency_query_priority = True

    def __init__(
        self,
        event_engine,
        api_key,
        api_secret,
        testnet=True,
        market_data_config=None,
        rate_limit_budget=None,
    ):
        super().__init__(event_engine, "BINANCE")
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        market_data_config = dict(market_data_config or {})

        self.session = requests.Session()
        adapter = HFTAdapter(pool_connections=20, pool_maxsize=20)
        self.session.mount("https://", adapter)
        self.session.headers.update({"Content-Type": "application/json"})

        self.rest = BinanceRestApi(
            api_key,
            api_secret,
            self.session,
            testnet,
            rate_limit_budget=rate_limit_budget,
        )
        self.require_healthy_clock = True
        self.rest.order_clock_guard = self._clock_health_guard
        # A websocket is bound to the book lifecycle generation at connect
        # time.  Keeping an unversioned client here would allow callbacks from
        # a closed transport to mutate the replacement connection.
        self.ws = None

        self.symbols = []
        self.orderbooks = {}
        self.ws_buffer = {}
        self.book_resyncing = set()
        self.book_recovery_generation = {}
        self.book_recovery_tokens = {}
        self._book_recovery_token = 0
        self._book_generation = 0
        self._book_lock = threading.RLock()
        self.max_book_buffer = max(
            100,
            int(market_data_config.get("max_book_buffer", 50_000) or 50_000),
        )
        self.book_resync_max_attempts = max(
            1,
            int(market_data_config.get("book_resync_max_attempts", 3) or 3),
        )
        self.book_resync_retry_sec = max(
            0.0,
            float(market_data_config.get("book_resync_retry_sec", 0.25) or 0.0),
        )
        self.stream_ready_timeout_sec = max(
            1.0,
            float(market_data_config.get("stream_ready_timeout_sec", 10.0) or 10.0),
        )
        self.publish_depth_levels = max(
            1,
            int(market_data_config.get("publish_depth_levels", 5) or 5),
        )
        self.emit_full_orderbook_events = bool(
            market_data_config.get("emit_full_orderbook_events", False)
        )
        try:
            ingress_age_ms = float(
                market_data_config.get(
                    "max_market_event_ingress_age_ms",
                    1000.0,
                )
                or 1000.0
            )
        except (TypeError, ValueError, OverflowError):
            ingress_age_ms = 1000.0
        self.max_market_event_ingress_age_ms = (
            ingress_age_ms
            if math.isfinite(ingress_age_ms) and ingress_age_ms >= 100.0
            else 1000.0
        )
        self.active = False
        self.listen_key = ""
        self.target_leverage = 0
        self.target_margin_type = "CROSSED"
        self.target_position_mode = "ONE_WAY"
        self.account_configuration_mode = ACCOUNT_CONFIGURATION_MODE_APPLY
        self.recovery_lock = threading.Lock()
        self.keep_alive_generation = 0
        self._closing = False

        self.global_sequence_id = 0
        self.seq_lock = threading.Lock()

    def _next_seq(self):
        with self.seq_lock:
            self.global_sequence_id += 1
            return self.global_sequence_id

    def connect(self, symbols: list):
        with self.recovery_lock:
            with self._book_lock:
                if getattr(self, "_closing", False):
                    return False
            return self._connect_once(symbols)

    def _connect_once(self, symbols: list):
        self.set_state(GatewayState.CONNECTING)
        self.symbols = [s.upper() for s in symbols]

        with self._book_lock:
            if getattr(self, "_closing", False):
                return False
            self.active = True
            self._book_generation += 1
            generation = self._book_generation
            self.book_resyncing.clear()
            self.book_recovery_generation.clear()
            self.book_recovery_tokens.clear()
            for symbol in self.symbols:
                self.orderbooks[symbol] = self._new_local_orderbook(symbol)
                self.ws_buffer[symbol] = []

        if not self._apply_account_trading_configuration():
            if self._mark_transport_failure_if_current(generation):
                self.event_engine.put(
                    Event(
                        EVENT_SYSTEM_HEALTH,
                        f"FREEZE_VENUE:{self.gateway_name}:ACCOUNT_CONFIG_FAILED",
                    )
                )
            return False

        candidate_ws = self._new_ws(generation)
        with self._book_lock:
            if (
                getattr(self, "_closing", False)
                or self._book_generation != generation
            ):
                candidate_ws.close()
                return False
            old_ws = self.ws
            self.ws = candidate_ws
        if old_ws is not None:
            old_ws.close()

        if not self._start_streams(expected_generation=generation):
            candidate_ws.close()
            if self._mark_transport_failure_if_current(
                generation,
                candidate_ws,
            ):
                self.event_engine.put(
                    Event(
                        EVENT_SYSTEM_HEALTH,
                        f"FREEZE_VENUE:{self.gateway_name}:USER_STREAM_START_FAILED",
                    )
                )
            return False

        if not candidate_ws.wait_until_connected(
            timeout_sec=self.stream_ready_timeout_sec
        ):
            candidate_ws.close()
            failure_is_current = self._mark_transport_failure_if_current(
                generation,
                candidate_ws,
            )
            if failure_is_current:
                self.event_engine.put(
                    Event(
                        EVENT_SYSTEM_HEALTH,
                        f"FREEZE_VENUE:{self.gateway_name}:STREAM_READY_TIMEOUT",
                    )
                )
            return False

        for symbol in self.symbols:
            if not self._resync_book(symbol, expected_generation=generation):
                candidate_ws.close()
                failure_is_current = self._mark_transport_failure_if_current(
                    generation,
                    candidate_ws,
                )
                if failure_is_current:
                    self.event_engine.put(
                        Event(
                            EVENT_SYSTEM_HEALTH,
                            f"FREEZE_SYMBOL:{symbol}:ORDERBOOK_STARTUP_FAILED",
                        )
                    )
                return False
        with self._book_lock:
            if not self._owns_transport_lifecycle_locked(
                generation,
                candidate_ws,
            ):
                candidate_ws.close()
                return False
            self.set_state(GatewayState.READY)
        return True

    def begin_shutdown(self):
        with self._book_lock:
            self._closing = True
            self.active = False
            self._book_generation += 1
            self.book_resyncing.clear()
            self.book_recovery_generation.clear()
            self.book_recovery_tokens.clear()
            ws = self.ws
        self.set_state(GatewayState.DISCONNECTED)
        if ws:
            ws.close()
        return True

    def close(self):
        self.begin_shutdown()
        with self.recovery_lock:
            with self._book_lock:
                ws = self.ws
                self.ws = None
            if ws:
                ws.close()
        if self.session:
            self.session.close()
        logger.info(f"[{self.gateway_name}] Closed.")
        return True

    def send_order(
        self,
        req: OrderRequest,
        client_oid: str = None,
        *,
        pre_send_guard=None,
    ) -> GatewayCommandResult:
        if not req.reduce_only:
            symbol = str(req.symbol or "").upper()
            with self._book_lock:
                book_unavailable = bool(
                    not self.active
                    or self.state != GatewayState.READY
                    or symbol in self.book_resyncing
                    or self.ws_buffer.get(symbol) is not None
                )
            if book_unavailable:
                return GatewayCommandResult(
                    CommandOutcome.REJECTED,
                    error_code="ORDERBOOK_NOT_READY",
                    error_message=(
                        f"{symbol} order book is not owned by a READY gateway"
                    ),
                )
        if not req.reduce_only and getattr(self, "require_healthy_clock", True):
            clock_ok, error_code, error_message = self._clock_health_guard()
            if not clock_ok:
                return GatewayCommandResult(
                    CommandOutcome.REJECTED,
                    error_code=error_code,
                    error_message=error_message,
                )
        resp = self.rest.new_order(
            req,
            client_oid,
            pre_send_guard=pre_send_guard,
        )
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                exchange_oid = str(data["orderId"])
            except Exception as exc:
                return GatewayCommandResult(
                    CommandOutcome.UNKNOWN,
                    error_message=f"invalid order acknowledgement: {exc}",
                    response=resp,
                )
            sym = req.symbol.replace("USDC", "").replace("USDT", "").lower()
            side_str = "long" if req.side == "BUY" else "short"
            tif_str = req.time_in_force

            if client_oid and client_oid.startswith("EXIT_"):
                action = "exit "
            elif client_oid and client_oid.startswith("ENTRY_"):
                action = "enter"
            elif client_oid and client_oid.startswith("EMERGENCY_"):
                action = "flatten"
            else:
                action = "order"

            if req.reduce_only:
                action = f"{action} reduce-only"

            logger.info(
                f"{sym} {action} {side_str} @ {req.price:.6g}"
                f"  ({tif_str}, vol={req.volume})"
            )
            return GatewayCommandResult(
                CommandOutcome.ACKNOWLEDGED,
                exchange_oid=exchange_oid,
                response=resp,
            )

        error_code, error_message = self._response_error(resp)
        ambiguous_codes = {
            "-1000",
            "-1001",
            "-1003",
            "-1006",
            "-1007",
            "-1008",
            "-4111",
            "-4116",
        }
        ambiguous = bool(
            resp is None
            or getattr(resp, "status_code", 0) >= 500
            or getattr(resp, "status_code", 0) in {408, 418, 429}
            or error_code in ambiguous_codes
        )
        return GatewayCommandResult(
            CommandOutcome.UNKNOWN if ambiguous else CommandOutcome.REJECTED,
            error_code=error_code,
            error_message=error_message,
            response=resp,
        )

    @staticmethod
    def _clock_health_guard():
        try:
            clock_health = time_service.health_snapshot(
                notify_listeners=False
            )
        except Exception as exc:
            return (
                False,
                "CLOCK_HEALTH_UNAVAILABLE",
                f"{type(exc).__name__}:{exc}",
            )
        if not bool(clock_health.get("ready", False)):
            return (
                False,
                "CLOCK_UNHEALTHY",
                str(clock_health.get("reason", "exchange clock unavailable")),
            )
        return True, "", ""

    def cancel_order(self, req: CancelRequest):
        return self.rest.cancel_order(req)

    def cancel_all_orders(self, symbol: str):
        return self.rest.cancel_all_orders(symbol)

    def set_countdown_cancel_all(self, symbol: str, countdown_time_ms: int):
        return self.rest.set_countdown_cancel_all(symbol, countdown_time_ms)

    def get_account_info(self):
        resp = self.rest.get_account()
        return resp.json() if resp and resp.status_code == 200 else None

    def get_all_positions(self, *, emergency: bool = False):
        resp = self.rest.get_positions(emergency=emergency)
        return resp.json() if resp and resp.status_code == 200 else None

    def get_open_orders(self, *, emergency: bool = False):
        resp = self.rest.get_open_orders(emergency=emergency)
        return resp.json() if resp and resp.status_code == 200 else None

    def get_order(self, symbol: str, order_id: str):
        resp = self.rest.query_order(symbol, order_id)
        if resp and resp.status_code == 200:
            return resp.json()
        error_code, error_message = self._response_error(resp)
        if error_code in {"-2011", "-2013"}:
            return {
                "_query_status": "NOT_FOUND",
                "code": error_code,
                "msg": error_message,
            }
        return None

    def get_all_orders(self, symbol: str, **kwargs):
        resp = self.rest.get_all_orders(symbol, **kwargs)
        return resp.json() if resp and resp.status_code == 200 else None

    def get_user_trades(self, symbol: str, **kwargs):
        resp = self.rest.get_user_trades(symbol, **kwargs)
        return resp.json() if resp and resp.status_code == 200 else None

    def get_income_history(self, **kwargs):
        resp = self.rest.get_income_history(**kwargs)
        return resp.json() if resp and resp.status_code == 200 else None

    def get_depth_snapshot(self, symbol):
        return self.rest.get_depth_snapshot(symbol)

    def get_rpi_depth(self, symbol: str, limit: int = 1000):
        return self.rest.get_rpi_depth(symbol, limit=limit)

    def get_commission_rate(self, symbol: str):
        resp = self.rest.get_commission_rate(symbol)
        return resp.json() if resp and resp.status_code == 200 else None

    def _start_streams(self, *, expected_generation=None):
        if self.ws is None:
            return False
        self.ws.start_market_stream(self.symbols)

        listen_key = self.rest.create_listen_key()
        if not listen_key:
            self.ws.close()
            return False

        self.listen_key = listen_key
        self.ws.start_user_stream(listen_key)
        self.keep_alive_generation += 1
        threading.Thread(
            target=self._keep_alive_loop,
            args=(self.keep_alive_generation, expected_generation),
            daemon=True,
        ).start()
        return True

    def _apply_account_trading_configuration(self):
        target_leverage = int(getattr(self, "target_leverage", 0) or 0)
        target_margin_type = str(getattr(self, "target_margin_type", "CROSSED") or "CROSSED").upper()
        target_position_mode = str(
            getattr(self, "target_position_mode", "ONE_WAY") or "ONE_WAY"
        ).upper()
        if target_position_mode != "ONE_WAY":
            logger.critical(
                f"[{self.gateway_name}] Refusing unsupported position mode "
                f"{target_position_mode}; OMS ledger is ONE_WAY only"
            )
            return False

        configuration_mode = str(
            getattr(
                self,
                "account_configuration_mode",
                ACCOUNT_CONFIGURATION_MODE_APPLY,
            )
            or ACCOUNT_CONFIGURATION_MODE_APPLY
        ).upper()
        if configuration_mode not in ACCOUNT_CONFIGURATION_MODES:
            logger.critical(
                f"[{self.gateway_name}] Refusing unsupported account configuration mode "
                f"{configuration_mode}"
            )
            return False
        if configuration_mode == ACCOUNT_CONFIGURATION_MODE_VERIFY_ONLY:
            return self._verify_account_trading_configuration(
                target_leverage=target_leverage,
                target_margin_type=target_margin_type,
                target_position_mode=target_position_mode,
            )

        response = self.rest.set_position_mode(target_position_mode)
        if not self.rest.response_succeeded(response, accepted_error_codes={"-4059"}):
            logger.error(f"[{self.gateway_name}] Failed to set position mode {target_position_mode}")
            return False

        for symbol in self.symbols:
            response = self.rest.set_margin_type(symbol, target_margin_type)
            if not self.rest.response_succeeded(response, accepted_error_codes={"-4046"}):
                logger.error(
                    f"[{self.gateway_name}] Failed to set margin type {target_margin_type} for {symbol}"
                )
                return False

            if target_leverage > 0:
                response = self.rest.set_leverage(symbol, target_leverage)
                if not self.rest.response_succeeded(response):
                    logger.error(
                        f"[{self.gateway_name}] Failed to set leverage {target_leverage} for {symbol}"
                    )
                    return False
        return True

    def _verify_account_trading_configuration(
        self,
        *,
        target_leverage: int,
        target_margin_type: str,
        target_position_mode: str,
    ) -> bool:
        try:
            position_mode_response = self.rest.get_position_mode()
            if not self.rest.response_succeeded(position_mode_response):
                raise AccountConfigurationVerificationError(
                    "failed to read account position mode"
                )

            position_risk_response = self.rest.get_positions()
            if not self.rest.response_succeeded(position_risk_response):
                raise AccountConfigurationVerificationError(
                    "failed to read account position configuration"
                )

            verify_account_configuration(
                position_mode_payload=position_mode_response.json(),
                position_risk_payload=position_risk_response.json(),
                symbols=self.symbols,
                target_position_mode=target_position_mode,
                target_margin_type=target_margin_type,
                target_leverage=target_leverage,
            )
        except Exception as exc:
            logger.critical(
                f"[{self.gateway_name}] Account configuration verification failed: {exc}"
            )
            return False
        return True

    def _keep_alive_loop(self, generation, transport_generation=None):
        while self.active and generation == self.keep_alive_generation:
            time.sleep(1800)
            if not self.active or generation != self.keep_alive_generation:
                return
            if not self._keep_alive_once(
                generation,
                transport_generation=transport_generation,
            ):
                return

    def _keep_alive_once(
        self,
        generation,
        *,
        transport_generation=None,
    ) -> bool:
        with self._book_lock:
            if (
                getattr(self, "_closing", False)
                or not self.active
                or generation != self.keep_alive_generation
                or not self._book_generation_matches_locked(
                    transport_generation
                )
            ):
                return False

        try:
            response = self.rest.keep_alive_listen_key()
            status_code = getattr(response, "status_code", None)
            if status_code == 200:
                return True
            detail = f"status={status_code or 'unavailable'}"
        except Exception as exc:
            detail = f"{type(exc).__name__}:{exc}"

        self._emit_ws_fault(
            "USER_STREAM_KEEPALIVE_FAILED",
            detail,
            expected_generation=transport_generation,
            expected_keep_alive_generation=generation,
        )
        return False

    def _new_ws(self, generation: int):
        return BinanceWsApi(
            lambda raw_msg: self.on_ws_message(
                raw_msg,
                expected_generation=generation,
            ),
            lambda error: self.on_ws_error(
                error,
                expected_generation=generation,
            ),
            self.testnet,
        )

    def on_ws_message(self, raw_msg, *, expected_generation=None):
        if not self._book_generation_is_current(expected_generation):
            return
        (
            received_timestamp,
            received_monotonic,
            corrected_received_timestamp,
            clock_offset_ms,
        ) = time_service.capture_timestamp()
        try:
            msg = json.loads(raw_msg)
        except Exception as exc:
            self._emit_ws_fault(
                "WS_PARSE_ERROR",
                str(exc),
                raw_msg,
                expected_generation=expected_generation,
            )
            return

        try:
            event_type = msg.get("e")
            if event_type == "ORDER_TRADE_UPDATE":
                self._handle_user_update(
                    msg,
                    received_timestamp=received_timestamp,
                    received_monotonic=received_monotonic,
                    corrected_received_timestamp=corrected_received_timestamp,
                    clock_offset_ms=clock_offset_ms,
                    expected_generation=expected_generation,
                )
                return
            if event_type == "ACCOUNT_UPDATE":
                self._handle_account_update(
                    msg,
                    received_timestamp=received_timestamp,
                    received_monotonic=received_monotonic,
                    corrected_received_timestamp=corrected_received_timestamp,
                    clock_offset_ms=clock_offset_ms,
                    expected_generation=expected_generation,
                )
                return
            if event_type == "listenKeyExpired":
                self._emit_ws_fault(
                    "USER_STREAM_EXPIRED",
                    "listen key expired",
                    msg,
                    expected_generation=expected_generation,
                )
                return
            if "stream" in msg:
                self._handle_market_update(
                    msg,
                    received_timestamp=received_timestamp,
                    received_monotonic=received_monotonic,
                    clock_offset_ms=clock_offset_ms,
                    corrected_received_timestamp=corrected_received_timestamp,
                    expected_generation=expected_generation,
                )
                return
            if self._is_control_message(msg):
                return
            logger.warning(f"[{self.gateway_name}] Ignoring unsupported WS payload: {msg}")
        except Exception as exc:
            self._emit_ws_fault(
                "WS_HANDLER_FAILURE",
                str(exc),
                msg,
                expected_generation=expected_generation,
            )

    def on_ws_error(self, err_msg, *, expected_generation=None):
        if not self._book_generation_is_current(expected_generation):
            return
        if isinstance(err_msg, dict):
            stream = str(err_msg.get("stream", "WS") or "WS")
            kind = str(err_msg.get("kind", "error") or "error").lower()
            detail = str(err_msg.get("detail", "") or "")
            if kind in {"transport_drop", "remote_close"}:
                reason = f"{stream}:{detail}" if detail else stream
                self._emit_ws_fault(
                    "WS_TRANSPORT_DROP",
                    reason,
                    expected_generation=expected_generation,
                )
                return
            rendered = f"[{stream}] {kind}: {detail}" if detail else f"[{stream}] {kind}"
            logger.error(f"[{self.gateway_name}] {rendered}")
            self.on_log(rendered, "ERROR")
            return

        logger.error(f"[{self.gateway_name}] {err_msg}")
        self.on_log(err_msg, "ERROR")

    def _is_control_message(self, msg):
        return isinstance(msg, dict) and "result" in msg and "id" in msg

    def _emit_ws_fault(
        self,
        code: str,
        detail: str = "",
        payload=None,
        *,
        expected_generation=None,
        expected_keep_alive_generation=None,
    ):
        message = f"{code}: {detail}" if detail else code
        if payload is not None:
            payload_preview = str(payload)
            if len(payload_preview) > 240:
                payload_preview = payload_preview[:237] + "..."
            logger.error(f"[{self.gateway_name}] {message} payload={payload_preview}")
        else:
            logger.error(f"[{self.gateway_name}] {message}")

        with self._book_lock:
            if (
                not self._book_generation_matches_locked(expected_generation)
                or (
                    expected_keep_alive_generation is not None
                    and self.keep_alive_generation
                    != expected_keep_alive_generation
                )
            ):
                return False
            self.active = False
            ws_client = getattr(self, "ws", None)
            if self.state != GatewayState.ERROR:
                self.set_state(GatewayState.ERROR)
            # Queue the fault while generation ownership is still held.  A
            # recovery cannot advance the generation before this event.
            self.event_engine.put(
                Event(
                    EVENT_SYSTEM_HEALTH,
                    f"FREEZE_VENUE:{self.gateway_name}:{message}",
                )
            )
        if ws_client:
            ws_client.close()
        return True

    def _handle_user_update(
        self,
        msg,
        *,
        received_timestamp: float = None,
        received_monotonic: float = None,
        corrected_received_timestamp: float = None,
        clock_offset_ms: float = None,
        expected_generation=None,
    ):
        order = msg.get("o", {})
        update_time_ms = order.get("T") or msg.get("T") or msg.get("E") or 0
        received_timestamp = float(received_timestamp or time.time())
        received_monotonic = float(received_monotonic or time.perf_counter())
        update = ExchangeOrderUpdate(
            # USD-M user data events do not expose a globally contiguous
            # sequence. Ordering is validated per order in the OMS instead.
            seq=0,
            client_oid=order.get("c", ""),
            exchange_oid=str(order.get("i", "")),
            symbol=order.get("s", ""),
            status=order.get("X", ""),
            filled_qty=float(order.get("l", 0.0) or 0.0),
            filled_price=float(order.get("L", 0.0) or order.get("ap", 0.0) or 0.0),
            cum_filled_qty=float(order.get("z", 0.0) or 0.0),
            update_time=float(update_time_ms) / 1000.0 if update_time_ms else time.time(),
            commission=self._parse_optional_float(order.get("n")),
            commission_asset=order.get("N") or "",
            realized_pnl=self._parse_optional_float(order.get("rp")),
            is_maker=bool(order.get("m")) if "m" in order else None,
            trade_id=self._parse_optional_int(order.get("t"), default=-1),
            order_type=str(order.get("o", "") or "").upper(),
            time_in_force=str(order.get("f", "") or "").upper(),
            received_timestamp=received_timestamp,
            received_monotonic=received_monotonic,
            dispatch_timestamp=time.time(),
            dispatch_monotonic=time.perf_counter(),
            clock_offset_ms=clock_offset_ms,
            corrected_received_timestamp=float(
                corrected_received_timestamp or received_timestamp
            ),
        )
        self._dispatch_transport_callback(
            expected_generation,
            self.on_order_update,
            update,
        )

    def _handle_account_update(
        self,
        msg,
        *,
        received_timestamp: float = None,
        received_monotonic: float = None,
        corrected_received_timestamp: float = None,
        clock_offset_ms: float = None,
        expected_generation=None,
    ):
        payload = msg.get("a", {})
        balances = payload.get("B", [])
        balance_entry = self._select_balance_entry(balances)

        balance_snapshot = self._extract_balance_snapshot(balances)
        positions = {}
        for raw_position in payload.get("P", []):
            symbol = raw_position.get("s")
            if not symbol:
                continue
            positions[symbol] = {
                "volume": float(raw_position.get("pa", 0.0) or 0.0),
                "entry_price": float(raw_position.get("ep", 0.0) or 0.0),
                "unrealized_pnl": float(raw_position.get("up", 0.0) or 0.0),
            }

        # Transaction time is the ordering boundary shared with order updates.
        # Event time is the delivery time and can be later even when the account
        # state belongs to an older matching-engine transaction.
        event_time_ms = msg.get("T") or msg.get("E") or 0
        received_timestamp = float(received_timestamp or time.time())
        received_monotonic = float(received_monotonic or time.perf_counter())
        update = ExchangeAccountUpdate(
            asset=balance_entry.get("a", "") if balance_entry else "",
            wallet_balance=(
                float(balance_entry.get("wb", 0.0) or 0.0)
                if balance_entry
                else 0.0
            ),
            available_balance=(
                self._parse_optional_float(balance_entry.get("cw"))
                if balance_entry
                else None
            ),
            balances=balance_snapshot,
            positions=positions,
            reason=payload.get("m", ""),
            event_time=float(event_time_ms) / 1000.0 if event_time_ms else time.time(),
            received_timestamp=received_timestamp,
            received_monotonic=received_monotonic,
            dispatch_timestamp=time.time(),
            dispatch_monotonic=time.perf_counter(),
            clock_offset_ms=clock_offset_ms,
            corrected_received_timestamp=float(
                corrected_received_timestamp or received_timestamp
            ),
        )
        self._dispatch_transport_callback(
            expected_generation,
            self.on_account_update,
            update,
        )

    def _handle_market_update(
        self,
        msg,
        *,
        received_timestamp: float = None,
        received_monotonic: float = None,
        clock_offset_ms: float = None,
        corrected_received_timestamp: float = None,
        expected_generation=None,
    ):
        if not self._book_generation_is_current(expected_generation):
            return
        stream = msg["stream"]
        data = dict(msg["data"])
        symbol = data.get("s")
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
        event_time_ms = int(
            data.get("E", 0)
            or (0 if "@markPrice" in stream else data.get("T", 0))
            or 0
        )
        if event_time_ms:
            self.latency_stats["ws_delay"] = (
                corrected_received_timestamp * 1000.0 - event_time_ms
            )

        if "@aggTrade" in stream:
            if self._reject_stale_market_event(
                stream=stream,
                symbol=symbol,
                event_time_ms=event_time_ms,
                corrected_received_timestamp=corrected_received_timestamp,
                expected_generation=expected_generation,
            ):
                return
            exchange_timestamp = float(data.get("T", event_time_ms) or 0.0) / 1000.0
            trade_id = int(data["a"])
            price = float(data["p"])
            quantity = float(data["q"])
            if (
                trade_id < 0
                or not math.isfinite(price)
                or price <= 0.0
                or not math.isfinite(quantity)
                or quantity <= 0.0
                or not math.isfinite(exchange_timestamp)
                or exchange_timestamp <= 0.0
            ):
                raise ValueError(
                    f"invalid aggTrade payload for {symbol}"
                )
            trade = AggTradeData(
                symbol,
                trade_id,
                price,
                quantity,
                data["m"],
                datetime.fromtimestamp(exchange_timestamp or received_timestamp),
                exchange_timestamp=exchange_timestamp,
                received_timestamp=received_timestamp,
                received_monotonic=received_monotonic,
                clock_offset_ms=clock_offset_ms,
                corrected_received_timestamp=corrected_received_timestamp,
            )
            self._dispatch_market_data(
                EVENT_AGG_TRADE,
                trade,
                expected_generation=expected_generation,
            )
        elif "@markPrice" in stream:
            exchange_timestamp = float(data.get("E", event_time_ms) or 0.0) / 1000.0
            next_funding_timestamp = float(data["T"]) / 1000.0
            mark_price = float(data["p"])
            index_price = float(data["i"])
            funding_rate = float(data["r"])
            if (
                not math.isfinite(mark_price)
                or mark_price <= 0.0
                or not math.isfinite(index_price)
                or index_price <= 0.0
                or not math.isfinite(funding_rate)
                or not math.isfinite(exchange_timestamp)
                or exchange_timestamp <= 0.0
                or not math.isfinite(next_funding_timestamp)
                or next_funding_timestamp <= 0.0
            ):
                raise ValueError(
                    f"invalid markPrice payload for {symbol}"
                )
            mark = MarkPriceData(
                symbol,
                mark_price,
                index_price,
                funding_rate,
                datetime.fromtimestamp(next_funding_timestamp),
                datetime.fromtimestamp(exchange_timestamp or received_timestamp),
                exchange_timestamp=exchange_timestamp,
                received_timestamp=received_timestamp,
                received_monotonic=received_monotonic,
                clock_offset_ms=clock_offset_ms,
                corrected_received_timestamp=corrected_received_timestamp,
                next_funding_timestamp=next_funding_timestamp,
            )
            self._dispatch_market_data(
                EVENT_MARK_PRICE,
                mark,
                expected_generation=expected_generation,
            )
        elif "@depth" in stream:
            if self._reject_stale_market_event(
                stream=stream,
                symbol=symbol,
                event_time_ms=event_time_ms,
                corrected_received_timestamp=corrected_received_timestamp,
                expected_generation=expected_generation,
            ):
                return
            data["_local_received_timestamp"] = received_timestamp
            data["_local_received_monotonic"] = received_monotonic
            data["_local_clock_offset_ms"] = clock_offset_ms
            data["_local_corrected_received_timestamp"] = (
                corrected_received_timestamp
            )
            self._process_book(
                symbol,
                data,
                expected_generation=expected_generation,
            )

    def _reject_stale_market_event(
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
            getattr(self, "max_market_event_ingress_age_ms", 1000.0)
        )
        if abs(age_ms) <= max_ingress_age_ms:
            return False
        stream_kind = "PUBLIC_DEPTH" if "@depth" in stream else "PUBLIC_TRADE"
        self._emit_ws_fault(
            "MARKET_DATA_STALE",
            (
                f"{stream_kind}:symbol={symbol}:"
                f"age={age_ms:.1f}ms>"
                f"{max_ingress_age_ms:.1f}ms"
            ),
            expected_generation=expected_generation,
        )
        return True

    def _dispatch_transport_callback(
        self,
        expected_generation,
        callback,
        *args,
    ) -> bool:
        if expected_generation is None:
            callback(*args)
            return True
        with self._book_lock:
            if not self._book_generation_matches_locked(expected_generation):
                return False
            callback(*args)
            return True

    def _dispatch_market_data(
        self,
        event_type: str,
        data,
        *,
        expected_generation=None,
    ) -> bool:
        def _publish():
            data.dispatch_timestamp = time.time()
            data.dispatch_monotonic = time.perf_counter()
            self.on_market_data(event_type, data)

        return self._dispatch_transport_callback(
            expected_generation,
            _publish,
        )

    def _process_book(self, symbol, raw, *, expected_generation=None):
        recovery = None
        failure = None
        with self._book_lock:
            if not self._book_generation_matches_locked(expected_generation):
                return
            try:
                buf = self.ws_buffer[symbol]
                if buf is not None:
                    if len(buf) >= self.max_book_buffer:
                        raise OrderBookGapError(
                            f"book buffer overflow for {symbol}"
                        )
                    buf.append(raw)
                    return

                book = self.orderbooks[symbol]
                book.process_delta(raw)
                data = book.generate_event_data()
                if data:
                    self._dispatch_market_data(
                        EVENT_ORDERBOOK,
                        data,
                        expected_generation=expected_generation,
                    )
            except (KeyError, ValueError, OrderBookGapError) as exc:
                failure = exc
                # Reserve the replacement owner before releasing the same lock
                # that observed the gap.  An about-to-complete old worker can
                # no longer release its token and publish a stale clear first.
                recovery = self._begin_book_recovery_locked(
                    symbol,
                    freeze_reason="FATAL_GAP",
                    expected_generation=expected_generation,
                )

        if failure is not None:
            logger.critical(
                f"[{symbol}] OrderBook integrity failure; freezing and resyncing: {failure}"
            )
            if recovery is not None:
                self._launch_book_recovery(recovery)

    def _init_books(self):
        for symbol in self.symbols:
            self._schedule_book_recovery(symbol)

    def _begin_book_recovery_locked(
        self,
        symbol,
        freeze_reason: str = "",
        *,
        expected_generation=None,
    ):
        if not self._book_generation_matches_locked(expected_generation):
            return None
        # A new integrity failure supersedes an in-flight recovery.  A routine
        # duplicate init request does not.
        if symbol in self.book_resyncing and not freeze_reason:
            return None
        generation = self._book_generation
        self._book_recovery_token += 1
        recovery_token = self._book_recovery_token
        self.book_resyncing.add(symbol)
        self.book_recovery_generation[symbol] = generation
        self.book_recovery_tokens[symbol] = recovery_token
        self.orderbooks[symbol] = self._new_local_orderbook(symbol)
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
            name=f"BinanceBookRecovery-{symbol}",
        ).start()
        return True

    def _schedule_book_recovery(
        self,
        symbol,
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

    def _recover_orderbook(self, symbol, generation, recovery_token):
        try:
            for attempt in range(1, self.book_resync_max_attempts + 1):
                if self._resync_book(
                    symbol,
                    expected_generation=generation,
                    recovery_token=recovery_token,
                ):
                    with self._book_lock:
                        completed = self._release_book_recovery_locked(
                            symbol,
                            generation,
                            recovery_token,
                        )
                        if completed:
                            # Queue CLEAR before releasing ownership ordering.
                            # Any later gap must acquire this lock and reserve a
                            # newer token before it can publish its FREEZE.
                            self.event_engine.put(
                                Event(
                                    EVENT_SYSTEM_HEALTH,
                                    "CLEAR_SYMBOL:"
                                    f"{symbol}:ORDERBOOK_RESYNCED:{recovery_token}",
                                )
                            )
                    return
                with self._book_lock:
                    if not self._owns_book_recovery_locked(
                        symbol,
                        generation,
                        recovery_token,
                    ):
                        return
                    if attempt >= self.book_resync_max_attempts:
                        break
                    self.orderbooks[symbol] = self._new_local_orderbook(symbol)
                    self.ws_buffer[symbol] = []
                time.sleep(self.book_resync_retry_sec * attempt)
            logger.critical(
                f"[{symbol}] OrderBook resync exhausted "
                f"{self.book_resync_max_attempts} attempts; symbol remains frozen."
            )
        finally:
            with self._book_lock:
                self._release_book_recovery_locked(
                    symbol,
                    generation,
                    recovery_token,
                )

    def recover_connectivity(self, recovery_context=None):
        with self.recovery_lock:
            with self._book_lock:
                if (
                    getattr(self, "_closing", False)
                    or not self.symbols
                ):
                    return False
                self.active = True
                self.keep_alive_generation += 1
                self._book_generation += 1
                generation = self._book_generation
                self.book_resyncing.clear()
                self.book_recovery_generation.clear()
                self.book_recovery_tokens.clear()
                for symbol in self.symbols:
                    self.orderbooks[symbol] = self._new_local_orderbook(symbol)
                    self.ws_buffer[symbol] = []

            logger.warning(f"[{self.gateway_name}] Recovering venue connectivity...")
            recovery_ws = self._new_ws(generation)
            with self._book_lock:
                if not self._owns_transport_lifecycle_locked(generation):
                    recovery_ws.close()
                    return False
                old_ws = self.ws
                self.ws = recovery_ws
                self.set_state(GatewayState.CONNECTING)
            if old_ws:
                old_ws.close()

            committed = False
            try:
                if not self._start_streams(expected_generation=generation):
                    logger.error(
                        f"[{self.gateway_name}] Recovery failed: "
                        "listen key unavailable"
                    )
                    self._mark_transport_failure_if_current(
                        generation,
                        recovery_ws,
                    )
                    return False

                if not recovery_ws.wait_until_connected(
                    timeout_sec=self.stream_ready_timeout_sec,
                ):
                    logger.error(
                        f"[{self.gateway_name}] Recovery failed: "
                        "websocket readiness timeout"
                    )
                    self._mark_transport_failure_if_current(
                        generation,
                        recovery_ws,
                    )
                    return False

                for symbol in self.symbols:
                    if not self._resync_book(
                        symbol,
                        expected_generation=generation,
                    ):
                        logger.error(
                            f"[{self.gateway_name}] Recovery failed during "
                            f"book sync: {symbol}"
                        )
                        self._mark_transport_failure_if_current(
                            generation,
                            recovery_ws,
                        )
                        return False

                with self._book_lock:
                    if (
                        not self._owns_transport_lifecycle_locked(
                            generation,
                            recovery_ws,
                        )
                        or self.state == GatewayState.ERROR
                    ):
                        logger.error(
                            f"[{self.gateway_name}] Recovery superseded by a "
                            "newer transport fault"
                        )
                        return False
                    self.set_state(GatewayState.READY)
                    if recovery_context:
                        owner = str(recovery_context.get("owner", "") or "")
                        epoch = int(recovery_context.get("epoch", 0) or 0)
                        verification = (
                            f"VERIFY_VENUE:{self.gateway_name}:{epoch}:{owner}"
                        )
                    else:
                        verification = (
                            f"VERIFY_VENUE:{self.gateway_name}:WS_RECOVERED"
                        )
                    self.event_engine.put(
                        Event(
                            EVENT_SYSTEM_HEALTH,
                            verification,
                        )
                    )
                    committed = True
                logger.info(
                    f"[{self.gateway_name}] Transport recovered; awaiting OMS "
                    "truth verification."
                )
                return True
            finally:
                if not committed:
                    recovery_ws.close()
                    with self._book_lock:
                        if self.ws is recovery_ws:
                            self.ws = None

    def _book_generation_matches_locked(self, expected_generation):
        return bool(
            expected_generation is None
            or self._book_generation == expected_generation
        )

    def _owns_transport_lifecycle_locked(
        self,
        generation,
        expected_ws=None,
    ):
        return bool(
            not getattr(self, "_closing", False)
            and self.active
            and self._book_generation == generation
            and (
                expected_ws is None
                or self.ws is expected_ws
            )
        )

    def _mark_transport_failure_if_current(
        self,
        generation,
        expected_ws=None,
    ):
        with self._book_lock:
            if (
                getattr(self, "_closing", False)
                or self._book_generation != generation
                or (
                    expected_ws is not None
                    and self.ws is not expected_ws
                )
            ):
                return False
            self.active = False
            self.set_state(GatewayState.ERROR)
            return True

    def _book_generation_is_current(self, expected_generation):
        if expected_generation is None:
            return True
        with self._book_lock:
            return self._book_generation_matches_locked(expected_generation)

    def _new_local_orderbook(self, symbol: str):
        return LocalOrderBook(
            symbol,
            publish_depth_levels=getattr(self, "publish_depth_levels", 5),
            emit_full_book=getattr(self, "emit_full_orderbook_events", False),
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

    def _resync_book(
        self,
        symbol,
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
                if recovery_token is not None and not self._owns_book_recovery_locked(
                    symbol,
                    expected_generation,
                    recovery_token,
                ):
                    return False
                book = self.orderbooks[symbol]
                buffered = self.ws_buffer[symbol]
                if buffered is None:
                    return False
                book.init_snapshot(snapshot)
                for message in buffered:
                    book.process_delta(message)
                self.ws_buffer[symbol] = None
                event_book = book.generate_event_data()
                if event_book is not None:
                    self._dispatch_market_data(
                        EVENT_ORDERBOOK,
                        event_book,
                        expected_generation=expected_generation,
                    )
        except (KeyError, ValueError, OrderBookGapError) as exc:
            logger.critical(f"[{symbol}] Gap during init. Resync failed: {exc}")
            if recovery_token is not None:
                with self._book_lock:
                    if self._owns_book_recovery_locked(
                        symbol,
                        expected_generation,
                        recovery_token,
                    ):
                        self.event_engine.put(
                            Event(
                                EVENT_SYSTEM_HEALTH,
                                "FREEZE_SYMBOL:"
                                f"{symbol}:ORDERBOOK_RESYNC_FAILED:{recovery_token}",
                            )
                        )
            return False
        logger.info(f"[{symbol}] Initial Sync Done.")
        return True

    def _parse_optional_float(self, value):
        if value in (None, ""):
            return None
        return float(value)

    def _parse_optional_int(self, value, default=None):
        if value in (None, ""):
            return default
        return int(value)

    def _response_error(self, response):
        if response is None:
            return "", "transport response unavailable"
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            return "", ""
        raw_code = payload.get("code")
        code = "" if raw_code is None else str(raw_code)
        return code, str(payload.get("msg", "") or "")

    def _extract_balance_snapshot(self, balances):
        snapshot = {}
        for entry in balances or []:
            asset = entry.get("a")
            if not asset:
                continue
            snapshot[asset] = {
                "wallet_balance": float(entry.get("wb", 0.0) or 0.0),
                "available_balance": self._parse_optional_float(entry.get("cw")),
                "balance_change": self._parse_optional_float(entry.get("bc")),
            }
        return snapshot

    def _select_balance_entry(self, balances):
        if not balances:
            return None

        tracked_assets = []
        for symbol in self.symbols:
            asset = self._extract_quote_asset(symbol)
            if asset and asset not in tracked_assets:
                tracked_assets.append(asset)

        for asset in tracked_assets:
            for entry in balances:
                if entry.get("a") == asset:
                    return entry

        for entry in balances:
            if entry.get("a") in {"USDT", "USDC", "BUSD", "FDUSD"}:
                return entry

        return balances[0]

    def _extract_quote_asset(self, symbol: str) -> str:
        for suffix in ("USDT", "USDC", "BUSD", "FDUSD", "BTC", "ETH", "BNB"):
            if symbol.endswith(suffix):
                return suffix
        return ""
