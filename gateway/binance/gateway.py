# file: gateway/binance/gateway.py

import json
import time
import threading
import requests
import socket
import traceback
from datetime import datetime
from requests.adapters import HTTPAdapter

from gateway.base_gateway import BaseGateway
from event.type import (
    Event, EVENT_API_LIMIT, EVENT_AGG_TRADE, EVENT_MARK_PRICE, 
    EVENT_ORDERBOOK, EVENT_RPI_UPDATE, EVENT_EXCHANGE_ORDER_UPDATE,
    EVENT_ORDER_UPDATE, EVENT_TRADE_UPDATE,
    OrderRequest, CancelRequest, ApiLimitData, ExchangeOrderUpdate, 
    AggTradeData, MarkPriceData, RpiDepthData, 
    OrderBookGapError, OrderData, TradeData, GatewayState
)
from data.orderbook import LocalOrderBook
from infrastructure.logger import logger
from .rest_api import BinanceRestApi
from .ws_api import BinanceWsApi

# 针对 HFT 优化的连接适配器
class HFTAdapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs['socket_options'] = [
            (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
            (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
        ]
        super().init_poolmanager(connections, maxsize, block, **pool_kwargs)

class BinanceGateway(BaseGateway):
    def __init__(self, event_engine, api_key, api_secret, testnet=True):
        # 初始化父类，定义网关名称
        super().__init__(event_engine, "BINANCE")
        
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        
        # 1. 建立高性能 HTTP Session
        self.session = requests.Session()
        adapter = HFTAdapter(pool_connections=20, pool_maxsize=20)
        self.session.mount("https://", adapter)
        self.session.headers.update({"Content-Type": "application/json"})
        
        # 2. 初始化功能模块
        self.rest = BinanceRestApi(api_key, api_secret, self.session, testnet)
        self.ws = BinanceWsApi(self.on_ws_message, self.on_ws_error, testnet)
        
        # 3. 内部状态缓存
        self.symbols = []
        self.orderbooks = {}
        self.ws_buffer = {}
        self.rpi_books = {}
        self.rpi_buffer = {}
        self.active = False

    # ==================================================================
    # 实现 BaseGateway 要求的抽象接口
    # ==================================================================

    def connect(self, symbols: list):
        """[Required] 启动网关"""
        self.set_state(GatewayState.CONNECTING)
        self.symbols = [s.upper() for s in symbols]
        self.active = True
        
        # 初始化订单簿容器
        for s in self.symbols:
            self.orderbooks[s] = LocalOrderBook(s)
            self.ws_buffer[s] = []
            self.rpi_books[s] = LocalOrderBook(s)
            self.rpi_buffer[s] = []

        # 1. 设置账户环境 (初始化模块)
        for s in self.symbols:
            self.rest.set_margin_type(s, "CROSSED") # 默认全仓
            # 可以根据需要设置杠杆
            # self.rest.set_leverage(s, 10)
        
        # 2. 启动 WebSocket
        self.ws.start_market_stream(self.symbols)
        
        # 3. 启动 User Data Stream
        listen_key = self.rest.create_listen_key()
        if listen_key:
            self.ws.start_user_stream(listen_key)
            # 开启 ListenKey 保活线程
            threading.Thread(target=self._keep_alive_loop, daemon=True).start()
            
        # 4. 异步同步 OrderBook 快照
        threading.Thread(target=self._init_books, daemon=True).start()
        
        self.set_state(GatewayState.READY)

    def close(self):
        """[Required] [修复] 彻底关闭网关"""
        self.active = False
        self.set_state(GatewayState.DISCONNECTED)
        if self.session:
            self.session.close()
        logger.info(f"[{self.gateway_name}] Gateway closed.")

    def send_order(self, req: OrderRequest) -> str:
        """[Required] 发单"""
        resp = self.rest.new_order(req)
        # 注意：BinanceRestApi.new_order 返回的是 requests.Response 对象
        if resp and resp.status_code == 200:
            data = resp.json()
            return str(data["orderId"])
        return None

    def cancel_order(self, req: CancelRequest):
        """[Required] 撤单"""
        self.rest.cancel_order(req)

    def cancel_all_orders(self, symbol: str):
        """[Required] 全撤单"""
        self.rest.cancel_all_orders(symbol)

    def get_account_info(self):
        """[Required] 查询账户余额"""
        r = self.rest.get_account()
        return r.json() if r and r.status_code == 200 else None

    def get_all_positions(self):
        """[Required] 查询持仓"""
        r = self.rest.get_positions()
        return r.json() if r and r.status_code == 200 else None
        
    def get_open_orders(self):
        """[Required] 查询挂单"""
        r = self.rest.get_open_orders()
        return r.json() if r and r.status_code == 200 else None

    def get_depth_snapshot(self, symbol: str):
        """[Required] [修复] 获取深度快照"""
        return self.rest.get_depth_snapshot(symbol)

    # ==================================================================
    # 内部辅助逻辑
    # ==================================================================

    def _keep_alive_loop(self):
        """ListenKey 每 30 分钟保活"""
        while self.active:
            time.sleep(1800)
            self.rest.keep_alive_listen_key()

    def on_ws_message(self, raw_msg):
        """WS 消息路由"""
        try:
            msg = json.loads(raw_msg)
            # 用户私有流 (账户/订单更新)
            if "e" in msg and msg["e"] == "ORDER_TRADE_UPDATE":
                self._handle_user_update(msg)
                return
            # 市场行情流
            if "stream" in msg:
                self._handle_market_update(msg)
        except:
            pass

    def on_ws_error(self, err_msg):
        self.on_log(err_msg, "ERROR")

    def _handle_user_update(self, msg):
        """解析私有推送并转化为标准事件推送给 OMS"""
        o = msg["o"]
        update = ExchangeOrderUpdate(
            client_oid=o.get("c", ""),
            exchange_oid=str(o["i"]),
            symbol=o["s"],
            status=o["X"],
            filled_qty=float(o["l"]),
            filled_price=float(o["L"]),
            cum_filled_qty=float(o["z"]),
            update_time=float(o["T"])/1000.0
        )
        # 调用父类方法，向事件总线发送 EVENT_EXCHANGE_ORDER_UPDATE
        self.on_order_update(update)

    def _handle_market_update(self, msg):
        """解析行情推送"""
        stream = msg["stream"]
        data = msg["data"]
        symbol = data.get("s")

        if "@aggTrade" in stream:
            trade = AggTradeData(
                symbol, data["a"], float(data["p"]), float(data["q"]), 
                data["m"], datetime.fromtimestamp(data["T"]/1000)
            )
            self.on_market_data(EVENT_AGG_TRADE, trade)
        elif "@markPrice" in stream:
            mp = MarkPriceData(
                symbol, float(data["p"]), float(data["i"]), float(data["r"]), 
                datetime.fromtimestamp(data["T"]/1000), datetime.now()
            )
            self.on_market_data(EVENT_MARK_PRICE, mp)
        elif "@depth" in stream:
            self._process_book(symbol, data, is_rpi=False)
        elif "@rpiDepth" in stream:
            self._process_book(symbol, data, is_rpi=True)

    def _process_book(self, symbol, raw, is_rpi):
        """处理增量深度数据"""
        book = self.rpi_books[symbol] if is_rpi else self.orderbooks[symbol]
        buf = self.rpi_buffer[symbol] if is_rpi else self.ws_buffer[symbol]

        if buf is not None:
            buf.append(raw)
            return

        try:
            book.process_delta(raw)
            if is_rpi:
                rpi = RpiDepthData(symbol, "BINANCE", datetime.now(), book.bids.copy(), book.asks.copy())
                self.on_market_data(EVENT_RPI_UPDATE, rpi)
            else:
                data = book.generate_event_data()
                if data: self.on_market_data(EVENT_ORDERBOOK, data)
        except OrderBookGapError:
            # 丢包处理：重置 buffer 并启动异步同步
            if is_rpi: self.rpi_buffer[symbol] = []
            else: self.ws_buffer[symbol] = []
            threading.Thread(target=self._resync_book, args=(symbol,), daemon=True).start()

    def _init_books(self):
        """启动时初始化快照"""
        time.sleep(2)
        for s in self.symbols: self._resync_book(s)

    def _resync_book(self, symbol):
        """同步单个币种的深度（标准 + RPI）"""
        # 1. 标准深度同步
        snap = self.rest.get_depth_snapshot(symbol)
        if snap:
            self.orderbooks[symbol].init_snapshot(snap)
            for m in (self.ws_buffer[symbol] or []):
                try: self.orderbooks[symbol].process_delta(m)
                except: pass
            self.ws_buffer[symbol] = None
        
        # 2. RPI 深度同步
        rpi_snap = self.rest.get_rpi_depth_snapshot(symbol)
        if rpi_snap:
            self.rpi_books[symbol].init_snapshot(rpi_snap)
            for m in (self.rpi_buffer[symbol] or []):
                try: self.rpi_books[symbol].process_delta(m)
                except: pass
            self.rpi_buffer[symbol] = None
        
        logger.info(f"[{self.gateway_name}] {symbol} fully synced (STD + RPI)")