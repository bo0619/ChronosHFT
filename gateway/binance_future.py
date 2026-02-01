# file: gateway/binance_future.py

import orjson as json
import threading
import time
import hmac
import hashlib
import requests
import socket
import traceback
from urllib.parse import urlencode
from datetime import datetime
import websocket
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager

# 基础设施
from infrastructure.logger import logger
from infrastructure.time_service import time_service

# 事件与数据结构
from event.type import Event, EVENT_LOG, EVENT_ORDERBOOK, EVENT_RPI_UPDATE, EVENT_ORDER_UPDATE, EVENT_TRADE_UPDATE, EVENT_AGG_TRADE, EVENT_MARK_PRICE, EVENT_API_LIMIT, EVENT_EXCHANGE_ORDER_UPDATE
from event.type import OrderRequest, OrderData, TradeData, AggTradeData, CancelRequest, MarkPriceData, ApiLimitData, RpiDepthData, ExchangeOrderUpdate
from event.type import TIF_GTX, TIF_GTC, TIF_IOC, TIF_FOK, TIF_RPI
from data.orderbook import LocalOrderBook

class HFTAdapter(HTTPAdapter):
    """
    [网络优化] 针对 HFT 优化的 HTTP 适配器
    """
    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs['socket_options'] = [
            (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
            (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
        ]
        super().init_poolmanager(connections, maxsize, block, **pool_kwargs)

class BinanceFutureGateway:
    def __init__(self, event_engine, api_key, api_secret, testnet=True):
        self.event_engine = event_engine
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet

        if testnet:
            self.rest_url = "https://testnet.binancefuture.com"
            self.ws_url = "wss://stream.binancefuture.com/ws"
        else:
            self.rest_url = "https://fapi.binance.com"
            self.ws_url = "wss://fstream.binance.com/ws"

        self.active = False
        self.symbols = []

        # 标准 & RPI OrderBook
        self.orderbooks = {}
        self.ws_buffer = {}
        self.rpi_books = {}
        self.rpi_buffer = {}

        self.listen_key = ""
        self.ws_user = None

        # HTTP Session
        self.session = requests.Session()
        adapter = HFTAdapter(pool_connections=20, pool_maxsize=20)
        self.session.mount("https://", adapter)
        self.session.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "HFT-Client/1.0"
        })

    # ------------------------------------------------------------------
    # REST 通用 (统一命名为 _send)
    # ------------------------------------------------------------------
    def _sign(self, params: dict):
        query = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode(),
            query.encode(),
            hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        return params

    def _send(self, method, path, params=None, signed=True):
        params = params or {}

        if signed:
            params["timestamp"] = time_service.now()
            self._sign(params)

        headers = {"X-MBX-APIKEY": self.api_key} if signed else {}
        url = self.rest_url + path

        try:
            req = requests.Request(method, url, params=params, headers=headers)
            prepped = self.session.prepare_request(req)
            resp = self.session.send(prepped, timeout=3.0)

            if "x-mbx-used-weight-1m" in resp.headers:
                used = int(resp.headers["x-mbx-used-weight-1m"])
                self.event_engine.put(Event(
                    EVENT_API_LIMIT,
                    ApiLimitData(used, time.time())
                ))

            if resp.status_code == 200:
                return resp.json()

            # 错误处理
            try:
                data = resp.json()
                code = data.get("code")
                msg = data.get("msg")
                if code == -4059: return None # No need to change position side
                if code == -2011: 
                    logger.info(f"Order missing when cancelling: {msg}")
                    return None
            except:
                pass

            logger.error(f"REST Error {resp.status_code}: {resp.text}")
            return None

        except Exception as e:
            logger.error(f"REST Exception: {e}")
            return None

    # ------------------------------------------------------------------
    # 业务接口
    # ------------------------------------------------------------------
    def send_order(self, req: OrderRequest):
        path = "/fapi/v1/order"

        params = {
            "symbol": req.symbol,
            "side": req.side,
            "type": req.order_type,
            "quantity": req.volume,
        }

        if req.order_type == "LIMIT":
            params["price"] = req.price
            
            # RPI 与 TIF 逻辑
            if getattr(req, "is_rpi", False):
                params["timeInForce"] = TIF_RPI
                if not req.post_only:
                    # RPI 强制 PostOnly，或者策略层保证
                    pass 
            else:
                params["timeInForce"] = (
                    TIF_GTX if req.post_only else req.time_in_force
                )

        res = self._send("POST", path, params)
        if res:
            oid = str(res["orderId"])
            prefix = "[RPI]" if getattr(req, "is_rpi", False) else ""
            logger.info(f"{prefix} Order Sent {oid} {req.symbol} {req.side} {req.price}")
            return oid
        return None

    def cancel_order(self, req: CancelRequest):
        self._send("DELETE", "/fapi/v1/order", {
            "symbol": req.symbol,
            "orderId": req.order_id
        })

    def cancel_all_orders(self, symbol):
        self._send("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})

    def get_depth_snapshot(self, symbol, limit=1000):
        return self._send("GET", "/fapi/v1/depth", {"symbol": symbol, "limit": limit}, signed=False)

    def get_rpi_depth_snapshot(self, symbol, limit=1000):
        return self._send("GET", "/fapi/v1/rpiDepth", {"symbol": symbol, "limit": limit}, signed=False)

    def get_all_positions(self):
        """
        [修复] 获取全仓持仓，使用 _send 方法
        """
        path = "/fapi/v2/positionRisk"
        return self._send("GET", path, signed=True)
    
    def get_account_info(self):
        """
        [NEW] 获取账户余额信息
        GET /fapi/v2/account
        """
        path = "/fapi/v2/account"
        data = self._send("GET", path, signed=True)
        
        return data
        # if data and "assets" in data:
        #     for asset in data["assets"]:
        #         if asset["asset"] == "USDT":
        #             # 返回钱包余额 (包含未结盈亏的 marginBalance 还是 walletBalance? 通常初始用 walletBalance)
        #             return float(asset["walletBalance"])
        # return 0.0

    def set_one_way_mode(self):
        self._send("POST", "/fapi/v1/positionSide/dual", {"dualSidePosition": "false"})

    # ------------------------------------------------------------------
    # User Stream
    # ------------------------------------------------------------------
    def start_user_stream(self):
        data = self._send("POST", "/fapi/v1/listenKey")
        if data and "listenKey" in data:
            self.listen_key = data["listenKey"]
            threading.Thread(target=self._run_user_ws, daemon=True).start()
            threading.Thread(target=self._keep_user_stream_alive, daemon=True).start()

    def _keep_user_stream_alive(self):
        while self.active:
            time.sleep(1800)
            try: self._send("PUT", "/fapi/v1/listenKey")
            except: pass

    def _run_user_ws(self):
        while self.active:
            ws_url = f"{self.ws_url}/{self.listen_key}"
            self.ws_user = websocket.WebSocketApp(ws_url, on_message=self._on_user_message)
            self.ws_user.run_forever(ping_interval=30, ping_timeout=10)
            time.sleep(3)

    def _on_user_message(self, ws, message):
        try:
            raw = json.loads(message)
            if raw.get("e") == "ORDER_TRADE_UPDATE": 
                self._process_order_trade_update(raw)
        except: traceback.print_exc()

    def _process_order_trade_update(self, raw):
        """解析交易所回报 -> ExchangeOrderUpdate"""
        o = raw["o"]
        
        # 构造 ExchangeOrderUpdate
        update = ExchangeOrderUpdate(
            client_oid=o.get("c", ""),
            exchange_oid=str(o["i"]),
            symbol=o["s"],
            status=o["X"],
            filled_qty=float(o["l"]),
            filled_price=float(o["L"]),
            cum_filled_qty=float(o["z"]),
            update_time=float(o["T"]) / 1000.0
        )
        # 推送给 OMS
        self.event_engine.put(Event(EVENT_EXCHANGE_ORDER_UPDATE, update))

    # ------------------------------------------------------------------
    # Market Stream
    # ------------------------------------------------------------------
    def connect(self, symbols):
        self.symbols = [s.upper() for s in symbols]
        self.active = True

        for s in self.symbols:
            self.orderbooks[s] = LocalOrderBook(s)
            self.ws_buffer[s] = []
            self.rpi_books[s] = LocalOrderBook(s)
            self.rpi_buffer[s] = []

        self.set_one_way_mode()
        threading.Thread(target=self._run_market_ws, daemon=True).start()
        self.start_user_stream()
        threading.Thread(target=self._init_all_books, daemon=True).start()

    def _run_market_ws(self):
        streams = []
        for s in self.symbols:
            sl = s.lower()
            streams += [f"{sl}@depth@100ms", f"{sl}@aggTrade", f"{sl}@markPrice@1s", f"{sl}@rpiDepth"]

        url = self.ws_url.replace("/ws", "") + "/stream?streams=" + "/".join(streams)

        while self.active:
            ws = websocket.WebSocketApp(
                url,
                on_message=self._on_ws_message,
                on_error=lambda w, e: logger.error(e)
            )
            ws.run_forever(ping_interval=30)
            time.sleep(3)

    def _on_ws_message(self, ws, message):
        try:
            msg = json.loads(message)
            stream = msg["stream"]
            data = msg["data"]
            symbol = data.get("s")

            if "@rpiDepth" in stream:
                self._process_depth(symbol, data, True)
            elif "@depth" in stream:
                self._process_depth(symbol, data, False)
            elif "@aggTrade" in stream:
                self.event_engine.put(Event(EVENT_AGG_TRADE, AggTradeData(symbol, data["a"], float(data["p"]), float(data["q"]), data["m"], datetime.fromtimestamp(data["T"]/1000))))
            elif "@markPrice" in stream:
                self.event_engine.put(Event(EVENT_MARK_PRICE, MarkPriceData(symbol, float(data["p"]), float(data["i"]), float(data["r"]), datetime.fromtimestamp(data["T"]/1000), datetime.now())))
        except: pass

    def _process_depth(self, symbol, raw, is_rpi):
        book = self.rpi_books[symbol] if is_rpi else self.orderbooks[symbol]
        buf = self.rpi_buffer[symbol] if is_rpi else self.ws_buffer[symbol]

        if buf is not None:
            buf.append(raw)
            return

        try:
            book.process_delta(raw)
            if is_rpi:
                self.event_engine.put(Event(EVENT_RPI_UPDATE, RpiDepthData(symbol, "BINANCE", datetime.now(), book.bids.copy(), book.asks.copy())))
            else:
                data = book.generate_event_data()
                if data: self.event_engine.put(Event(EVENT_ORDERBOOK, data))
        except: # Gap Error
            if is_rpi: self.rpi_buffer[symbol] = []
            else: self.ws_buffer[symbol] = []
            threading.Thread(target=self._resync_symbol, args=(symbol,), daemon=True).start()

    def _init_all_books(self):
        time.sleep(2)
        for s in self.symbols: self._resync_symbol(s)

    def _resync_symbol(self, symbol):
        snap = self.get_depth_snapshot(symbol)
        if snap:
            ob = self.orderbooks[symbol]
            ob.init_snapshot(snap)
            for m in self.ws_buffer[symbol]: 
                try: ob.process_delta(m)
                except: pass
            self.ws_buffer[symbol] = None

        rpi_snap = self.get_rpi_depth_snapshot(symbol)
        if rpi_snap:
            rob = self.rpi_books[symbol]
            rob.init_snapshot(rpi_snap)
            for m in self.rpi_buffer[symbol]: 
                try: rob.process_delta(m)
                except: pass
            self.rpi_buffer[symbol] = None
        
        logger.info(f"{symbol} synced")