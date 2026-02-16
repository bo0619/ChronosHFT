# file: gateway/binance/gateway.py

import json
import threading
import time
import requests
import socket
import traceback
from datetime import datetime
from requests.adapters import HTTPAdapter

from gateway.base_gateway import BaseGateway
from event.type import (
    Event, EVENT_API_LIMIT, EVENT_AGG_TRADE, EVENT_MARK_PRICE, 
    EVENT_ORDERBOOK, EVENT_RPI_UPDATE, EVENT_EXCHANGE_ORDER_UPDATE,
    EVENT_SYSTEM_HEALTH, # [NEW] 用于通知致命错误
    OrderRequest, CancelRequest, ApiLimitData, ExchangeOrderUpdate, 
    AggTradeData, MarkPriceData, RpiDepthData, 
    OrderBookGapError, TIF_GTX, TIF_RPI, GatewayState
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
        
        # 1. 建立 Session
        self.session = requests.Session()
        adapter = HFTAdapter(pool_connections=20, pool_maxsize=20)
        self.session.mount("https://", adapter)
        self.session.headers.update({"Content-Type": "application/json"})
        
        # 2. 模块初始化
        self.rest = BinanceRestApi(api_key, api_secret, self.session, testnet)
        self.ws = BinanceWsApi(self.on_ws_message, self.on_ws_error, testnet)
        
        # 3. 状态与缓存
        self.symbols = []
        self.orderbooks = {}
        self.ws_buffer = {}
        self.rpi_books = {}
        self.rpi_buffer = {}
        self.active = False
        self.listen_key = ""

        # 4. [CORE] 本地定序器 (Sequencer)
        # 负责把无序/并发的外部世界，转化为有序的内部事件流
        self.global_sequence_id = 0
        self.seq_lock = threading.Lock()

    def _next_seq(self):
        with self.seq_lock:
            self.global_sequence_id += 1
            return self.global_sequence_id

    # --- 接口实现 ---

    def connect(self, symbols: list):
        self.set_state(GatewayState.CONNECTING)
        self.symbols = [s.upper() for s in symbols]
        self.active = True
        
        for s in self.symbols:
            self.orderbooks[s] = LocalOrderBook(s)
            self.ws_buffer[s] = []
            self.rpi_books[s] = LocalOrderBook(s)
            self.rpi_buffer[s] = []

        # Init settings
        for s in self.symbols:
            self.rest.set_margin_type(s, "CROSSED")

        # Start Streams
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
        """
        [No Compromise] 必须传递 client_oid 以便闭环追踪
        """
        # 我们需要手动构造 params 传给 rest，因为 rest_api.new_order 
        # 可能不支持直接传 client_oid，或者我们需要 hack 一下
        # 这里直接调用底层 _send_request 或者修改 rest_api
        # 为了不修改 rest_api，我们在 req 对象上附带临时属性，
        # 或者直接在这里构造 params 调用 rest 的 request 方法。
        # 最佳方案：修改 rest.new_order 接受 extra_params
        
        # 这里演示直接调用底层逻辑 (为了绝对控制权)
        path = "/fapi/v1/order"
        params = {
            "symbol": req.symbol,
            "side": req.side,
            "type": req.order_type,
            "quantity": req.volume,
        }
        if client_oid:
            params["newClientOrderId"] = client_oid # [Critical] 注入本地ID

        if req.order_type == "LIMIT":
            params["price"] = req.price
            if getattr(req, "is_rpi", False):
                params["timeInForce"] = TIF_RPI
                if not req.post_only: 
                    logger.error("RPI must be PostOnly")
                    return None
            else:
                params["timeInForce"] = (TIF_GTX if req.post_only else req.time_in_force)

        # 调用 rest 实例的 request (复用签名逻辑)
        resp = self.rest.request("POST", path, params, signed=True)
        
        if resp and resp.status_code == 200:
            data = resp.json()
            return str(data["orderId"]) # 返回交易所ID用于日志
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

    # --- Internal ---
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
        """
        [CORE] 生成带序列号的原子事件
        """
        o = msg["o"]
        
        # 严格检查：如果没有 client_oid，这就是脏数据 (可能是手动下的单)
        client_oid = o.get("c", "")
        # 在 No Compromise 模式下，我们也应该允许处理外部订单吗？
        # 顶级 OMS 通常只处理自己发的单。如果不是系统的 UUID 格式，可以标记或忽略。
        # 这里我们全部透传，由 OMS 决定是否丢弃。

        update = ExchangeOrderUpdate(
            seq=self._next_seq(), # [Critical] 分配单调序列号
            client_oid=client_oid,
            exchange_oid=str(o["i"]),
            symbol=o["s"],
            status=o["X"],
            filled_qty=float(o["l"]),
            filled_price=float(o["L"]),
            cum_filled_qty=float(o["z"]),
            update_time=float(o["T"])/1000.0
        )
        # 推送给 OMS (EVENT_EXCHANGE_ORDER_UPDATE)
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
            self._process_book(symbol, data, False)
        elif "@rpiDepth" in stream:
            self._process_book(symbol, data, True)

    def _process_book(self, symbol, raw, is_rpi):
        """
        [No Compromise] 发现 Gap 直接自杀，不尝试修复
        """
        book = self.rpi_books[symbol] if is_rpi else self.orderbooks[symbol]
        buf = self.rpi_buffer[symbol] if is_rpi else self.ws_buffer[symbol]

        if buf is not None:
            buf.append(raw)
            return

        try:
            book.process_delta(raw)
            if is_rpi:
                self.on_market_data(EVENT_RPI_UPDATE, RpiDepthData(symbol, "BINANCE", datetime.now(), book.bids.copy(), book.asks.copy()))
            else:
                data = book.generate_event_data()
                if data: self.on_market_data(EVENT_ORDERBOOK, data)
                
        except OrderBookGapError:
            # [FATAL] 数据不连续，系统不可信，立即熔断
            logger.critical(f"[{symbol}] FATAL: OrderBook Gap Detected! Terminating Gateway.")
            self.event_engine.put(Event(EVENT_SYSTEM_HEALTH, "FATAL_GAP"))
            self.close() # 断开连接
            # 这里的哲学是：与其用修补过的数据继续交易，不如停止交易。

    def _init_books(self):
        time.sleep(2)
        for s in self.symbols: self._resync_book(s)

    def _resync_book(self, symbol):
        # 仅在启动时允许 Sync
        snap = self.rest.get_depth_snapshot(symbol)
        if snap:
            self.orderbooks[symbol].init_snapshot(snap)
            # 处理启动期间积压的数据
            if self.ws_buffer[symbol]:
                try:
                    for m in self.ws_buffer[symbol]: self.orderbooks[symbol].process_delta(m)
                    self.ws_buffer[symbol] = None
                except OrderBookGapError:
                    logger.critical(f"[{symbol}] Gap during init. Failed.")
                    self.close()
                    return

        rpi_snap = self.rest.get_rpi_depth_snapshot(symbol)
        if rpi_snap:
            self.rpi_books[symbol].init_snapshot(rpi_snap)
            if self.rpi_buffer[symbol]:
                try:
                    for m in self.rpi_buffer[symbol]: self.rpi_books[symbol].process_delta(m)
                    self.rpi_buffer[symbol] = None
                except: pass
        
        logger.info(f"[{symbol}] Initial Sync Done.")