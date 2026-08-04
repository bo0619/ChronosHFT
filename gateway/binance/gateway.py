import math
import time

import requests

from event.type import (
    CancelRequest,
    Event,
    GatewayState,
    GatewayCommandResult,
    OrderRequest,
    EVENT_ORDERBOOK,
    EVENT_SYSTEM_HEALTH,
)
from gateway.base_gateway import BaseGateway
from infrastructure.logger import logger
from infrastructure.time_service import time_service
from .account_configuration import (
    BinanceAccountConfigurationController,
    BinanceAccountConfigurationDependencies,
)
from .book_controller import (
    BinanceOrderBookConfig,
    BinanceOrderBookController,
    BinanceOrderBookDependencies,
)
from .connection_controller import (
    BinanceConnectionController,
    BinanceConnectionDependencies,
)
from .gateway_facade_fields import BinanceGatewayCompatibilityFields
from .http_adapter import HFTAdapter
from .rest_api import BinanceRestApi
from .rest_gateway import (
    BinanceRestGateway,
    BinanceRestGatewayDependencies,
)
from .ws_api import BinanceWsApi
from .user_stream import (
    BinanceUserStreamController,
    BinanceUserStreamDependencies,
)
from .websocket_dispatcher import (
    BinanceWebSocketDependencies,
    BinanceWebSocketDispatcher,
)


class BinanceGateway(BinanceGatewayCompatibilityFields, BaseGateway):
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
        self._order_book_component = self._build_order_book_controller()
        self.max_book_buffer = max(
            100,
            int(market_data_config.get("max_book_buffer", 2048) or 2048),
        )
        self.book_resync_max_attempts = max(
            1,
            int(market_data_config.get("book_resync_max_attempts", 3) or 3),
        )
        self.book_resync_retry_sec = max(
            0.0,
            float(market_data_config.get("book_resync_retry_sec", 0.25) or 0.0),
        )
        self.max_book_recovery_threads = max(
            1,
            int(
                market_data_config.get("max_book_recovery_threads", 4)
                or 4
            ),
        )
        self.book_recovery_join_timeout_sec = max(
            0.5,
            float(
                market_data_config.get(
                    "book_recovery_join_timeout_sec",
                    3.0,
                )
                or 3.0
            ),
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
        self.max_orderbook_levels_per_side = max(
            self.publish_depth_levels,
            int(
                market_data_config.get(
                    "max_orderbook_levels_per_side",
                    4096,
                )
                or 4096
            ),
        )
        self.max_delta_levels_per_side = max(
            1,
            int(
                market_data_config.get(
                    "max_delta_levels_per_side",
                    2048,
                )
                or 2048
            ),
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
        self._account_configuration_component = (
            self._build_account_configuration_controller()
        )
        self._user_stream_component = self._build_user_stream_controller()
        self._connection_component = self._build_connection_controller()
        self._rest_gateway_component = self._build_rest_gateway()

    def _build_order_book_controller(self):
        dependencies = BinanceOrderBookDependencies(
            create_orderbook=lambda symbol: self._new_local_orderbook(symbol),
            fetch_snapshot=lambda symbol: self.rest.get_depth_snapshot(symbol),
            publish_orderbook=lambda data, generation: self._dispatch_market_data(
                EVENT_ORDERBOOK,
                data,
                expected_generation=generation,
            ),
            emit_health=lambda message: self.event_engine.put(
                Event(EVENT_SYSTEM_HEALTH, message)
            ),
            resync_book=lambda symbol, **kwargs: self._resync_book(
                symbol,
                **kwargs,
            ),
            launch_recovery=lambda recovery: self._launch_book_recovery(
                recovery
            ),
            recover_orderbook=lambda symbol, generation, token: (
                self._recover_orderbook(symbol, generation, token)
            ),
            log_info=logger.info,
            log_critical=logger.critical,
        )
        return BinanceOrderBookController(
            dependencies,
            BinanceOrderBookConfig(),
        )

    def _order_books(self):
        component = self.__dict__.get("_order_book_component")
        if component is None:
            # Supports deliberately partial __new__-constructed gateways in
            # isolation tests while production eagerly constructs the owner.
            component = self._build_order_book_controller()
            self.__dict__["_order_book_component"] = component
        return component

    def _build_user_stream_controller(self):
        return BinanceUserStreamController(
            BinanceUserStreamDependencies(
                create_listen_key=lambda: self.rest.create_listen_key(),
                keep_alive_listen_key=(
                    lambda: self.rest.keep_alive_listen_key()
                ),
                emit_fault=lambda code, detail="", **kwargs: (
                    self._emit_ws_fault(code, detail, **kwargs)
                ),
                is_transport_active=lambda: bool(
                    getattr(self, "active", False)
                ),
                is_transport_closing=lambda: bool(
                    getattr(self, "_closing", False)
                ),
                transport_generation_matches_locked=(
                    lambda generation: self._book_generation_matches_locked(
                        generation
                    )
                ),
                transport_lock=self._book_lock,
                log_critical=logger.critical,
            )
        )

    def _build_account_configuration_controller(self):
        return BinanceAccountConfigurationController(
            BinanceAccountConfigurationDependencies(
                rest=lambda: self.rest,
                symbols=lambda: list(self.symbols),
                venue_name=lambda: self.gateway_name,
                log_error=logger.error,
                log_critical=logger.critical,
            )
        )

    def _account_configuration(self):
        component = self.__dict__.get("_account_configuration_component")
        if component is None:
            component = self._build_account_configuration_controller()
            self.__dict__["_account_configuration_component"] = component
        return component

    def _user_streams(self):
        component = self.__dict__.get("_user_stream_component")
        if component is None:
            component = self._build_user_stream_controller()
            self.__dict__["_user_stream_component"] = component
        return component

    def _build_connection_controller(self):
        books = self._order_books()
        return BinanceConnectionController(
            BinanceConnectionDependencies(
                transport_lock=books.lock,
                begin_generation_locked=books.begin_generation_locked,
                generation_matches_locked=books.generation_matches_locked,
                begin_book_shutdown_locked=books.begin_shutdown_locked,
                resync_book=lambda symbol, **kwargs: self._resync_book(
                    symbol,
                    **kwargs,
                ),
                join_book_recovery_threads=books.join_recovery_threads,
                book_recovery_threads_stopped=books.recovery_threads_stopped,
                apply_account_configuration=(
                    lambda: self._apply_account_trading_configuration()
                ),
                create_ws=lambda generation: self._new_ws(generation),
                start_streams=lambda _ws, _symbols, generation: (
                    self._start_streams(
                        expected_generation=generation,
                    )
                ),
                stop_user_stream=lambda: self._user_streams().stop(
                    timeout_sec=2.0
                ),
                invalidate_user_stream=self._user_streams().invalidate,
                stream_ready_timeout_sec=lambda: float(
                    self.stream_ready_timeout_sec
                ),
                set_state=self.set_state,
                get_state=lambda: self.state,
                emit_health=lambda message: self.event_engine.put(
                    Event(EVENT_SYSTEM_HEALTH, message)
                ),
                close_session=lambda: (
                    self.session.close()
                    if getattr(self, "session", None) is not None
                    else None
                ),
                venue_name=lambda: self.gateway_name,
                log_info=logger.info,
                log_warning=logger.warning,
                log_error=logger.error,
            )
        )

    def _build_rest_gateway(self):
        books = self._order_books()
        return BinanceRestGateway(
            BinanceRestGatewayDependencies(
                rest=self.rest,
                transport_lock=books.lock,
                is_transport_active=lambda: bool(self.active),
                gateway_state=lambda: self.state,
                symbol_ready_locked=books.symbol_ready_locked,
                require_healthy_clock=lambda: bool(
                    getattr(self, "require_healthy_clock", True)
                ),
                clock_health_snapshot=time_service.health_snapshot,
                log_info=logger.info,
            )
        )

    def _rest_gateway(self):
        component = self.__dict__.get("_rest_gateway_component")
        if component is None:
            component = self._build_rest_gateway()
            self.__dict__["_rest_gateway_component"] = component
        return component

    def _connections(self):
        component = self.__dict__.get("_connection_component")
        if component is None:
            component = self._build_connection_controller()
            self.__dict__["_connection_component"] = component
        return component

    def connect(self, symbols: list):
        return self._connections().connect(symbols)

    def _connect_once(self, symbols: list):
        return self._connections().connect_once(symbols)

    def begin_shutdown(self):
        return self._connections().begin_shutdown()

    def close(self):
        return self._connections().close()

    def send_order(
        self,
        req: OrderRequest,
        client_oid: str = None,
        *,
        pre_send_guard=None,
    ) -> GatewayCommandResult:
        return self._rest_gateway().send_order(
            req,
            client_oid,
            pre_send_guard=pre_send_guard,
        )

    def _clock_health_guard(self):
        return self._rest_gateway().clock_health_guard()

    def cancel_order(self, req: CancelRequest):
        return self._rest_gateway().cancel_order(req)

    def cancel_all_orders(self, symbol: str):
        return self._rest_gateway().cancel_all_orders(symbol)

    def set_countdown_cancel_all(self, symbol: str, countdown_time_ms: int):
        return self._rest_gateway().set_countdown_cancel_all(
            symbol,
            countdown_time_ms,
        )

    def get_account_info(self):
        return self._rest_gateway().get_account_info()

    def get_all_positions(self, *, emergency: bool = False):
        return self._rest_gateway().get_all_positions(emergency=emergency)

    def get_open_orders(self, *, emergency: bool = False):
        return self._rest_gateway().get_open_orders(emergency=emergency)

    def get_order(self, symbol: str, order_id: str):
        return self._rest_gateway().get_order(symbol, order_id)

    def get_all_orders(self, symbol: str, **kwargs):
        return self._rest_gateway().get_all_orders(symbol, **kwargs)

    def get_user_trades(self, symbol: str, **kwargs):
        return self._rest_gateway().get_user_trades(symbol, **kwargs)

    def get_income_history(self, **kwargs):
        return self._rest_gateway().get_income_history(**kwargs)

    def get_depth_snapshot(self, symbol):
        return self._rest_gateway().get_depth_snapshot(symbol)

    def get_rpi_depth(self, symbol: str, limit: int = 1000):
        return self._rest_gateway().get_rpi_depth(symbol, limit=limit)

    def get_commission_rate(self, symbol: str):
        return self._rest_gateway().get_commission_rate(symbol)

    def _start_streams(self, *, expected_generation=None):
        return self._user_streams().start(
            self.ws,
            self.symbols,
            transport_generation=expected_generation,
        )

    def _keep_alive_loop(
        self,
        generation,
        transport_generation=None,
        stop_event=None,
    ):
        return self._user_streams().keep_alive_loop(
            generation,
            transport_generation,
            stop_event,
        )

    def _keep_alive_once(
        self,
        generation,
        *,
        transport_generation=None,
    ):
        return self._user_streams().keep_alive_once(
            generation,
            transport_generation=transport_generation,
        )

    def _apply_account_trading_configuration(self):
        return self._account_configuration().apply()

    def _verify_account_trading_configuration(
        self,
        *,
        target_leverage: int,
        target_margin_type: str,
        target_position_mode: str,
    ) -> bool:
        return self._account_configuration().verify(
            target_leverage=target_leverage,
            target_margin_type=target_margin_type,
            target_position_mode=target_position_mode,
        )

    def _build_websocket_dispatcher(self):
        return BinanceWebSocketDispatcher(
            BinanceWebSocketDependencies(
                gateway_name=lambda: str(self.gateway_name),
                capture_timestamp=time_service.capture_timestamp,
                clock_offset_ms=lambda: float(
                    getattr(time_service, "offset", 0.0)
                ),
                generation_is_current=lambda generation: (
                    self._book_generation_is_current(generation)
                ),
                emit_fault=lambda *args, **kwargs: self._emit_ws_fault(
                    *args,
                    **kwargs,
                ),
                tracked_symbols=lambda: tuple(
                    getattr(self, "symbols", ())
                ),
                latency_stats=lambda: self.latency_stats,
                max_ingress_age_ms=lambda: float(
                    getattr(
                        self,
                        "max_market_event_ingress_age_ms",
                        1000.0,
                    )
                ),
                dispatch_transport=lambda *args, **kwargs: (
                    self._dispatch_transport_callback(*args, **kwargs)
                ),
                dispatch_market_data=lambda *args, **kwargs: (
                    self._dispatch_market_data(*args, **kwargs)
                ),
                process_book=lambda *args, **kwargs: self._process_book(
                    *args,
                    **kwargs,
                ),
                on_order_update=lambda update: self.on_order_update(update),
                on_account_update=lambda update: self.on_account_update(
                    update
                ),
                on_log=lambda message, level: self.on_log(message, level),
                log_warning=logger.warning,
                log_error=logger.error,
                wall_time=time.time,
                monotonic=time.perf_counter,
            )
        )

    def _websocket_events(self):
        dispatcher = self.__dict__.get("_websocket_dispatcher")
        if dispatcher is None:
            dispatcher = self._build_websocket_dispatcher()
            self.__dict__["_websocket_dispatcher"] = dispatcher
        return dispatcher

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
        return self._websocket_events().on_message(
            raw_msg,
            expected_generation=expected_generation,
        )

    def on_ws_error(self, err_msg, *, expected_generation=None):
        return self._websocket_events().on_error(
            err_msg,
            expected_generation=expected_generation,
        )

    def _is_control_message(self, msg):
        return self._websocket_events().is_control_message(msg)

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
        return self._websocket_events().handle_user_update(
            msg,
            received_timestamp=received_timestamp,
            received_monotonic=received_monotonic,
            corrected_received_timestamp=corrected_received_timestamp,
            clock_offset_ms=clock_offset_ms,
            expected_generation=expected_generation,
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
        return self._websocket_events().handle_account_update(
            msg,
            received_timestamp=received_timestamp,
            received_monotonic=received_monotonic,
            corrected_received_timestamp=corrected_received_timestamp,
            clock_offset_ms=clock_offset_ms,
            expected_generation=expected_generation,
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
        return self._websocket_events().handle_market_update(
            msg,
            received_timestamp=received_timestamp,
            received_monotonic=received_monotonic,
            corrected_received_timestamp=corrected_received_timestamp,
            clock_offset_ms=clock_offset_ms,
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
        return self._websocket_events().reject_stale_market_event(
            stream=stream,
            symbol=symbol,
            event_time_ms=event_time_ms,
            corrected_received_timestamp=corrected_received_timestamp,
            expected_generation=expected_generation,
        )

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
        return self._order_books().process_delta(
            symbol,
            raw,
            expected_generation=expected_generation,
        )

    def _init_books(self):
        return self._order_books().initialize_books(self.symbols)

    def _begin_book_recovery_locked(
        self,
        symbol,
        freeze_reason: str = "",
        *,
        expected_generation=None,
    ):
        return self._order_books().begin_recovery_locked(
            symbol,
            freeze_reason,
            expected_generation=expected_generation,
        )

    def _launch_book_recovery(self, recovery):
        return self._order_books().launch_recovery(recovery)

    def _run_book_recovery(self, symbol, generation, recovery_token):
        return self._order_books().run_recovery(
            symbol,
            generation,
            recovery_token,
        )

    def _book_recovery_threads_stopped(self) -> bool:
        return self._order_books().recovery_threads_stopped()

    def _join_book_recovery_threads(self) -> bool:
        return self._order_books().join_recovery_threads()

    def _schedule_book_recovery(
        self,
        symbol,
        freeze_reason: str = "",
        *,
        expected_generation=None,
    ):
        return self._order_books().schedule_recovery(
            symbol,
            freeze_reason,
            expected_generation=expected_generation,
        )

    def _recover_orderbook(self, symbol, generation, recovery_token):
        return self._order_books().recover_orderbook(
            symbol,
            generation,
            recovery_token,
        )

    def recover_connectivity(self, recovery_context=None):
        return self._connections().recover(recovery_context)

    def _book_generation_matches_locked(self, expected_generation):
        return self._order_books().generation_matches_locked(
            expected_generation
        )

    def _owns_transport_lifecycle_locked(
        self,
        generation,
        expected_ws=None,
    ):
        return self._connections().owns_lifecycle_locked(
            generation,
            expected_ws,
        )

    def _mark_transport_failure_if_current(
        self,
        generation,
        expected_ws=None,
    ):
        return self._connections().mark_failure_if_current(
            generation,
            expected_ws,
        )

    def _book_generation_is_current(self, expected_generation):
        return self._order_books().generation_is_current(
            expected_generation
        )

    def _new_local_orderbook(self, symbol: str):
        return self._order_books().create_local_orderbook(symbol)

    def _owns_book_recovery_locked(self, symbol, generation, recovery_token):
        return self._order_books().owns_recovery_locked(
            symbol,
            generation,
            recovery_token,
        )

    def _release_book_recovery_locked(self, symbol, generation, recovery_token):
        return self._order_books().release_recovery_locked(
            symbol,
            generation,
            recovery_token,
        )

    def _resync_book(
        self,
        symbol,
        *,
        expected_generation=None,
        recovery_token=None,
    ):
        return self._order_books().resync_book(
            symbol,
            expected_generation=expected_generation,
            recovery_token=recovery_token,
        )
