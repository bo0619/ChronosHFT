import json
import socket
import threading
import time
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter

from event.type import (
    AggTradeData,
    CancelRequest,
    Event,
    ExchangeAccountUpdate,
    ExchangeOrderUpdate,
    GatewayState,
    MarkPriceData,
    OrderBookGapError,
    OrderRequest,
    EVENT_AGG_TRADE,
    EVENT_MARK_PRICE,
    EVENT_ORDERBOOK,
    EVENT_SYSTEM_HEALTH,
)
from gateway.base_gateway import BaseGateway
from infrastructure.logger import logger
from data.orderbook import LocalOrderBook

from .rest_api import BinanceRestApi
from .ws_api import BinanceWsApi


class HFTAdapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["socket_options"] = [
            (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
            (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
        ]
        super().init_poolmanager(connections, maxsize, block, **pool_kwargs)


class BinanceGateway(BaseGateway):
    def __init__(self, event_engine, api_key, api_secret, testnet=True, market_data_config=None):
        super().__init__(event_engine, "BINANCE")
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        market_data_config = dict(market_data_config or {})

        self.session = requests.Session()
        adapter = HFTAdapter(pool_connections=20, pool_maxsize=20)
        self.session.mount("https://", adapter)
        self.session.headers.update({"Content-Type": "application/json"})

        self.rest = BinanceRestApi(api_key, api_secret, self.session, testnet)
        self.ws = BinanceWsApi(self.on_ws_message, self.on_ws_error, testnet)

        self.symbols = []
        self.orderbooks = {}
        self.ws_buffer = {}
        self.book_resyncing = set()
        self.publish_depth_levels = max(
            1,
            int(market_data_config.get("publish_depth_levels", 5) or 5),
        )
        self.emit_full_orderbook_events = bool(
            market_data_config.get("emit_full_orderbook_events", False)
        )
        self.active = False
        self.listen_key = ""
        self.target_leverage = 0
        self.target_margin_type = "CROSSED"
        self.target_position_mode = "ONE_WAY"
        self.recovery_lock = threading.Lock()
        self.keep_alive_generation = 0

        self.global_sequence_id = 0
        self.seq_lock = threading.Lock()

    def _next_seq(self):
        with self.seq_lock:
            self.global_sequence_id += 1
            return self.global_sequence_id

    def connect(self, symbols: list):
        self.set_state(GatewayState.CONNECTING)
        self.symbols = [s.upper() for s in symbols]
        self.active = True

        for symbol in self.symbols:
            self.orderbooks[symbol] = self._new_local_orderbook(symbol)
            self.ws_buffer[symbol] = []

        if not self._apply_account_trading_configuration():
            self.active = False
            self.set_state(GatewayState.ERROR)
            self.event_engine.put(
                Event(
                    EVENT_SYSTEM_HEALTH,
                    f"FREEZE_VENUE:{self.gateway_name}:ACCOUNT_CONFIG_FAILED",
                )
            )
            return

        if not self._start_streams():
            self.active = False
            self.set_state(GatewayState.ERROR)
            self.event_engine.put(
                Event(
                    EVENT_SYSTEM_HEALTH,
                    f"FREEZE_VENUE:{self.gateway_name}:USER_STREAM_START_FAILED",
                )
            )
            return

        threading.Thread(target=self._init_books, daemon=True).start()
        self.set_state(GatewayState.READY)

    def close(self):
        self.active = False
        self.set_state(GatewayState.DISCONNECTED)
        if self.ws:
            self.ws.close()
        if self.session:
            self.session.close()
        logger.info(f"[{self.gateway_name}] Closed.")

    def send_order(self, req: OrderRequest, client_oid: str = None) -> str:
        resp = self.rest.new_order(req, client_oid)
        if resp and resp.status_code == 200:
            data = resp.json()
            sym = req.symbol.replace("USDC", "").replace("USDT", "").lower()
            side_str = "long" if req.side == "BUY" else "short"
            tif_str = "GTX" if req.post_only else "IOC"

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
            return str(data["orderId"])
        return None

    def cancel_order(self, req: CancelRequest):
        self.rest.cancel_order(req)

    def cancel_all_orders(self, symbol: str):
        self.rest.cancel_all_orders(symbol)

    def get_account_info(self):
        resp = self.rest.get_account()
        return resp.json() if resp and resp.status_code == 200 else None

    def get_all_positions(self):
        resp = self.rest.get_positions()
        return resp.json() if resp and resp.status_code == 200 else None

    def get_open_orders(self):
        resp = self.rest.get_open_orders()
        return resp.json() if resp and resp.status_code == 200 else None

    def get_depth_snapshot(self, symbol):
        return self.rest.get_depth_snapshot(symbol)

    def _start_streams(self):
        self.ws.start_market_stream(self.symbols)

        listen_key = self.rest.create_listen_key()
        if not listen_key:
            return False

        self.listen_key = listen_key
        self.ws.start_user_stream(listen_key)
        self.keep_alive_generation += 1
        threading.Thread(
            target=self._keep_alive_loop,
            args=(self.keep_alive_generation,),
            daemon=True,
        ).start()
        return True

    def _apply_account_trading_configuration(self):
        target_leverage = int(getattr(self, "target_leverage", 0) or 0)
        target_margin_type = str(getattr(self, "target_margin_type", "CROSSED") or "CROSSED").upper()
        target_position_mode = str(
            getattr(self, "target_position_mode", "ONE_WAY") or "ONE_WAY"
        ).upper()

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

    def _keep_alive_loop(self, generation):
        while self.active and generation == self.keep_alive_generation:
            time.sleep(1800)
            if not self.active or generation != self.keep_alive_generation:
                return
            self.rest.keep_alive_listen_key()

    def on_ws_message(self, raw_msg):
        try:
            msg = json.loads(raw_msg)
        except Exception as exc:
            self._emit_ws_fault("WS_PARSE_ERROR", str(exc), raw_msg)
            return

        try:
            event_type = msg.get("e")
            if event_type == "ORDER_TRADE_UPDATE":
                self._handle_user_update(msg)
                return
            if event_type == "ACCOUNT_UPDATE":
                self._handle_account_update(msg)
                return
            if event_type == "listenKeyExpired":
                self._emit_ws_fault("USER_STREAM_EXPIRED", "listen key expired", msg)
                return
            if "stream" in msg:
                self._handle_market_update(msg)
                return
            if self._is_control_message(msg):
                return
            logger.warning(f"[{self.gateway_name}] Ignoring unsupported WS payload: {msg}")
        except Exception as exc:
            self._emit_ws_fault("WS_HANDLER_FAILURE", str(exc), msg)

    def on_ws_error(self, err_msg):
        if isinstance(err_msg, dict):
            stream = str(err_msg.get("stream", "WS") or "WS")
            kind = str(err_msg.get("kind", "error") or "error").lower()
            detail = str(err_msg.get("detail", "") or "")
            if kind in {"transport_drop", "remote_close"}:
                reason = f"{stream}:{detail}" if detail else stream
                self._emit_ws_fault("WS_TRANSPORT_DROP", reason)
                return
            rendered = f"[{stream}] {kind}: {detail}" if detail else f"[{stream}] {kind}"
            logger.error(f"[{self.gateway_name}] {rendered}")
            self.on_log(rendered, "ERROR")
            return

        logger.error(f"[{self.gateway_name}] {err_msg}")
        self.on_log(err_msg, "ERROR")

    def _is_control_message(self, msg):
        return isinstance(msg, dict) and "result" in msg and "id" in msg

    def _emit_ws_fault(self, code: str, detail: str = "", payload=None):
        message = f"{code}: {detail}" if detail else code
        if payload is not None:
            payload_preview = str(payload)
            if len(payload_preview) > 240:
                payload_preview = payload_preview[:237] + "..."
            logger.error(f"[{self.gateway_name}] {message} payload={payload_preview}")
        else:
            logger.error(f"[{self.gateway_name}] {message}")

        self.active = False
        ws_client = getattr(self, "ws", None)
        if ws_client:
            ws_client.close()
        if self.state != GatewayState.ERROR:
            self.set_state(GatewayState.ERROR)
        self.event_engine.put(
            Event(
                EVENT_SYSTEM_HEALTH,
                f"FREEZE_VENUE:{self.gateway_name}:{message}",
            )
        )

    def _handle_user_update(self, msg):
        order = msg.get("o", {})
        update_time_ms = order.get("T") or msg.get("T") or msg.get("E") or 0
        update = ExchangeOrderUpdate(
            seq=self._next_seq(),
            client_oid=order.get("c", ""),
            exchange_oid=str(order.get("i", "")),
            symbol=order.get("s", ""),
            status=order.get("X", ""),
            filled_qty=float(order.get("l", 0.0) or 0.0),
            filled_price=float(order.get("L", 0.0) or 0.0),
            cum_filled_qty=float(order.get("z", 0.0) or 0.0),
            update_time=float(update_time_ms) / 1000.0 if update_time_ms else time.time(),
            commission=self._parse_optional_float(order.get("n")),
            commission_asset=order.get("N") or "",
            realized_pnl=self._parse_optional_float(order.get("rp")),
            is_maker=bool(order.get("m")) if "m" in order else None,
        )
        self.on_order_update(update)

    def _handle_account_update(self, msg):
        payload = msg.get("a", {})
        balances = payload.get("B", [])
        balance_entry = self._select_balance_entry(balances)
        if not balance_entry:
            return

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

        event_time_ms = msg.get("E") or msg.get("T") or 0
        update = ExchangeAccountUpdate(
            asset=balance_entry.get("a", ""),
            wallet_balance=float(balance_entry.get("wb", 0.0) or 0.0),
            available_balance=self._parse_optional_float(balance_entry.get("cw")),
            balances=balance_snapshot,
            positions=positions,
            reason=payload.get("m", ""),
            event_time=float(event_time_ms) / 1000.0 if event_time_ms else time.time(),
        )
        self.on_account_update(update)

    def _handle_market_update(self, msg):
        stream = msg["stream"]
        data = msg["data"]
        symbol = data.get("s")

        if "@aggTrade" in stream:
            self.on_market_data(
                EVENT_AGG_TRADE,
                AggTradeData(
                    symbol,
                    data["a"],
                    float(data["p"]),
                    float(data["q"]),
                    data["m"],
                    datetime.fromtimestamp(data["T"] / 1000),
                ),
            )
        elif "@markPrice" in stream:
            self.on_market_data(
                EVENT_MARK_PRICE,
                MarkPriceData(
                    symbol,
                    float(data["p"]),
                    float(data["i"]),
                    float(data["r"]),
                    datetime.fromtimestamp(data["T"] / 1000),
                    datetime.now(),
                ),
            )
        elif "@depth" in stream:
            self._process_book(symbol, data)

    def _process_book(self, symbol, raw):
        book = self.orderbooks[symbol]
        buf = self.ws_buffer[symbol]

        if buf is not None:
            buf.append(raw)
            return

        try:
            book.process_delta(raw)
            data = book.generate_event_data()
            if data:
                self.on_market_data(EVENT_ORDERBOOK, data)
        except OrderBookGapError:
            logger.critical(f"[{symbol}] OrderBook gap detected. Freezing symbol and resyncing.")
            self.event_engine.put(
                Event(
                    EVENT_SYSTEM_HEALTH,
                    f"FREEZE_SYMBOL:{symbol}:FATAL_GAP",
                )
            )
            if symbol not in self.book_resyncing:
                self.book_resyncing.add(symbol)
                threading.Thread(
                    target=self._recover_orderbook,
                    args=(symbol,),
                    daemon=True,
                ).start()

    def _init_books(self):
        time.sleep(2)
        for symbol in self.symbols:
            self._resync_book(symbol)

    def _recover_orderbook(self, symbol):
        self.orderbooks[symbol] = self._new_local_orderbook(symbol)
        self.ws_buffer[symbol] = []
        ok = self._resync_book(symbol)
        self.book_resyncing.discard(symbol)
        if ok:
            self.event_engine.put(
                Event(
                    EVENT_SYSTEM_HEALTH,
                    f"CLEAR_SYMBOL:{symbol}:ORDERBOOK_RESYNCED",
                )
            )

    def recover_connectivity(self):
        with self.recovery_lock:
            if not self.symbols:
                return False

            logger.warning(f"[{self.gateway_name}] Recovering venue connectivity...")
            self.active = True
            self.book_resyncing.clear()
            self.keep_alive_generation += 1

            if self.ws:
                self.ws.close()
            self.ws = BinanceWsApi(self.on_ws_message, self.on_ws_error, self.testnet)

            for symbol in self.symbols:
                self.orderbooks[symbol] = self._new_local_orderbook(symbol)
                self.ws_buffer[symbol] = []

            self.set_state(GatewayState.CONNECTING)
            if not self._start_streams():
                logger.error(f"[{self.gateway_name}] Recovery failed: listen key unavailable")
                self.set_state(GatewayState.ERROR)
                return False

            time.sleep(1.0)
            for symbol in self.symbols:
                if not self._resync_book(symbol):
                    logger.error(f"[{self.gateway_name}] Recovery failed during book sync: {symbol}")
                    self.set_state(GatewayState.ERROR)
                    return False

            self.active = True
            self.set_state(GatewayState.READY)
            self.event_engine.put(
                Event(
                    EVENT_SYSTEM_HEALTH,
                    f"CLEAR_VENUE:{self.gateway_name}:WS_RECOVERED",
                )
            )
            logger.info(f"[{self.gateway_name}] Venue recovery complete.")
            return True

    def _new_local_orderbook(self, symbol: str):
        return LocalOrderBook(
            symbol,
            publish_depth_levels=getattr(self, "publish_depth_levels", 5),
            emit_full_book=getattr(self, "emit_full_orderbook_events", False),
        )

    def _resync_book(self, symbol):
        snapshot = self.rest.get_depth_snapshot(symbol)
        if snapshot:
            self.orderbooks[symbol].init_snapshot(snapshot)
            if self.ws_buffer[symbol]:
                try:
                    for message in self.ws_buffer[symbol]:
                        self.orderbooks[symbol].process_delta(message)
                    self.ws_buffer[symbol] = None
                except OrderBookGapError:
                    logger.critical(f"[{symbol}] Gap during init. Resync failed.")
                    self.event_engine.put(
                        Event(
                            EVENT_SYSTEM_HEALTH,
                            f"FREEZE_SYMBOL:{symbol}:ORDERBOOK_RESYNC_FAILED",
                        )
                    )
                    return False
        logger.info(f"[{symbol}] Initial Sync Done.")
        return snapshot is not None

    def _parse_optional_float(self, value):
        if value in (None, ""):
            return None
        return float(value)

    def _extract_balance_snapshot(self, balances):
        snapshot = {}
        for entry in balances or []:
            asset = entry.get("a")
            if not asset:
                continue
            snapshot[asset] = {
                "wallet_balance": float(entry.get("wb", 0.0) or 0.0),
                "available_balance": self._parse_optional_float(entry.get("cw")),
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
