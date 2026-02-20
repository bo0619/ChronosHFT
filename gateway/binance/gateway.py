# file: gateway/binance/gateway.py

import json
import threading
import time
import requests
import socket
from datetime import datetime
from requests.adapters import HTTPAdapter

from gateway.base_gateway import BaseGateway
from event.type import (
    Event, EVENT_API_LIMIT, EVENT_AGG_TRADE, EVENT_MARK_PRICE, 
    EVENT_ORDERBOOK, EVENT_EXCHANGE_ORDER_UPDATE, EVENT_SYSTEM_HEALTH,
    OrderRequest, CancelRequest, ApiLimitData, ExchangeOrderUpdate, 
    AggTradeData, MarkPriceData, GatewayState,
    OrderBookGapError, TIF_GTX
)
from data.orderbook import LocalOrderBook
from infrastructure.logger import logger
from infrastructure.time_service import time_service
from .rest_api import BinanceRestApi
from .ws_api import BinanceWsApi

class HFTAdapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs['socket_options'] = [
            (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
            (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
        ]
        super().init_poolmanager(connections, maxsize, block, **pool_kwargs)

class BinanceGateway(BaseGateway):
    def __init__(self, event_engine, api_key, api_secret, testnet=True):
        super().__init__(event_engine, "BINANCE")
        
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        
        self.session = requests.Session()
        adapter = HFTAdapter(pool_connections=20, pool_maxsize=20)
        self.session.mount("https://", adapter)
        self.session.headers.update({"Content-Type": "application/json"})
        
        self.rest = BinanceRestApi(api_key, api_secret, self.session, testnet)
        self.ws = BinanceWsApi(self.on_ws_message, self.on_ws_error, testnet)
        
        self.symbols = []
        self.orderbooks = {}
        self.ws_buffer = {}
        self.active = False
        self.listen_key = ""

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
        
        for s in self.symbols:
            self.orderbooks[s] = LocalOrderBook(s)
            self.ws_buffer[s] = []

        for s in self.symbols:
            self.rest.set_margin_type(s, "CROSSED")

        self.ws.start_market_stream(self.symbols)
        
        listen_key = self.rest.create_listen_key()
        if listen_key:
            self.listen_key = listen_key
            self.ws.start_user_stream(listen_key)
            threading.Thread(target=self._keep_alive_loop, daemon=True).start()
            
        threading.Thread(target=self._init_books, daemon=True).start()
        self.set_state(GatewayState.READY)

    def close(self):
        self.active = False
        self.set_state(GatewayState.DISCONNECTED)
        if self.session: self.session.close()
        logger.info(f"[{self.gateway_name}] Closed.")

    def send_order(self, req: OrderRequest, client_oid: str = None) -> str:
        resp = self.rest.new_order(req, client_oid)
        if resp and resp.status_code == 200:
            data = resp.json()
            logger.info(f"[Gateway] Order Sent. ExchID: {data['orderId']}")
            return str(data["orderId"])
        return None

    def cancel_order(self, req: CancelRequest):
        self.rest.cancel_order(req)

    def cancel_all_orders(self, symbol: str):
        self.rest.cancel_all_orders(symbol)

    def get_account_info(self):
        r = self.rest.get_account()
        return r.json() if r and r.status_code == 200 else None

    def get_all_positions(self):
        r = self.rest.get_positions()
        return r.json() if r and r.status_code == 200 else None
        
    def get_open_orders(self):
        r = self.rest.get_open_orders()
        return r.json() if r and r.status_code == 200 else None
        
    def get_depth_snapshot(self, symbol):
        return self.rest.get_depth_snapshot(symbol)

    def _keep_alive_loop(self):
        while self.active:
            time.sleep(1800)
            self.rest.keep_alive_listen_key()

    def on_ws_message(self, raw_msg):
        try:
            msg = json.loads(raw_msg)
            if "e" in msg and msg["e"] == "ORDER_TRADE_UPDATE":
                self._handle_user_update(msg)
                return
            if "stream" in msg:
                self._handle_market_update(msg)
        except: pass

    def on_ws_error(self, err_msg):
        self.on_log(err_msg, "ERROR")

    def _handle_user_update(self, msg):
        o = msg["o"]
        client_oid = o.get("c", "")
        update = ExchangeOrderUpdate(
            seq=self._next_seq(),
            client_oid=client_oid,
            exchange_oid=str(o["i"]),
            symbol=o["s"],
            status=o["X"],
            filled_qty=float(o["l"]),
            filled_price=float(o["L"]),
            cum_filled_qty=float(o["z"]),
            update_time=float(o["T"])/1000.0
        )
        self.on_order_update(update)

    def _handle_market_update(self, msg):
        stream = msg["stream"]
        data = msg["data"]
        symbol = data.get("s")

        if "@aggTrade" in stream:
            self.on_market_data(EVENT_AGG_TRADE, AggTradeData(symbol, data["a"], float(data["p"]), float(data["q"]), data["m"], datetime.fromtimestamp(data["T"]/1000)))
        elif "@markPrice" in stream:
            self.on_market_data(EVENT_MARK_PRICE, MarkPriceData(symbol, float(data["p"]), float(data["i"]), float(data["r"]), datetime.fromtimestamp(data["T"]/1000), datetime.now()))
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
            if data: self.on_market_data(EVENT_ORDERBOOK, data)
        except OrderBookGapError:
            logger.critical(f"[{symbol}] FATAL: OrderBook Gap! Terminating Gateway.")
            self.event_engine.put(Event(EVENT_SYSTEM_HEALTH, "FATAL_GAP"))
            self.close()

    def _init_books(self):
        time.sleep(2)
        for s in self.symbols: self._resync_book(s)

    def _resync_book(self, symbol):
        snap = self.rest.get_depth_snapshot(symbol)
        if snap:
            self.orderbooks[symbol].init_snapshot(snap)
            if self.ws_buffer[symbol]:
                try:
                    for m in self.ws_buffer[symbol]: self.orderbooks[symbol].process_delta(m)
                    self.ws_buffer[symbol] = None
                except OrderBookGapError:
                    logger.critical(f"[{symbol}] Gap during init. Failed.")
                    self.close()
                    return
        logger.info(f"[{symbol}] Initial Sync Done.")