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

from infrastructure.logger import logger
from infrastructure.time_service import time_service

# 引入核心数据结构
from event.type import Event, EVENT_LOG, EVENT_ORDERBOOK, EVENT_AGG_TRADE, EVENT_MARK_PRICE, EVENT_API_LIMIT
from event.type import OrderRequest, CancelRequest, MarkPriceData, ApiLimitData, AggTradeData, ExchangeOrderUpdate
from event.type import OrderBookGapError, Side, TIF_GTX
from data.orderbook import LocalOrderBook

# [NEW] 定义网关专用的原始回报事件 (Gateway -> OMS)
# OMS 监听此事件来更新内部状态，然后 OMS 再向外推送 OrderUpdate/TradeUpdate
EVENT_EXCHANGE_ORDER_UPDATE = "eExchangeOrderUpdate"

class HFTAdapter(HTTPAdapter):
    """
    针对 HFT 优化的 HTTP 适配器
    1. 禁用 Nagle 算法 (TCP_NODELAY)
    2. 开启 Keep-Alive
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
        
        if self.testnet:
            self.rest_url = "https://testnet.binancefuture.com"
            self.ws_url = "wss://stream.binancefuture.com/ws"
        else:
            self.rest_url = "https://fapi.binance.com"
            self.ws_url = "wss://fstream.binance.com/ws"
            
        self.active = False
        self.symbols = []
        self.orderbooks = {}
        self.ws_buffer = {}
        self.ws_user = None
        self.listen_key = ""

        # 初始化高性能 Session
        self.session = requests.Session()
        adapter = HFTAdapter(pool_connections=10, pool_maxsize=10)
        self.session.mount('https://', adapter)
        self.session.headers.update({
            'Content-Type': 'application/json',
            'User-Agent': 'HFT-Client/1.0'
        })

    def _sign_request(self, params: dict):
        query_string = urlencode(params)
        signature = hmac.new(self.api_secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()
        params['signature'] = signature
        return params

    def _send_request(self, method, path, params=None, signed=True):
        if params is None: params = {}
        if signed:
            params['timestamp'] = time_service.now()
            params = self._sign_request(params)
        
        headers = {'X-MBX-APIKEY': self.api_key} if signed else {}
        url = self.rest_url + path
        
        try:
            req = requests.Request(method, url, headers=headers, params=params)
            prepped = self.session.prepare_request(req)
            response = self.session.send(prepped, timeout=3.0)
            
            # API 权重监控
            if 'x-mbx-used-weight-1m' in response.headers:
                used = int(response.headers['x-mbx-used-weight-1m'])
                self.event_engine.put(Event(EVENT_API_LIMIT, ApiLimitData(used, time.time())))
            
            if response.status_code == 200: return response.json()
            
            # 错误处理
            try:
                res_json = response.json()
                code = res_json.get('code')
                msg = res_json.get('msg')
                if code == -4059: return res_json # No need to change position side
                if code == -2011: # Unknown order sent
                    logger.info(f"Order missing when cancelling: {msg}")
                    return None
            except: pass
            
            logger.error(f"Request Failed [{response.status_code}]: {response.text}")
            return None
        except Exception as e:
            logger.error(f"Request Exception: {e}")
            return None

    def send_order(self, req: OrderRequest):
        """
        发送订单
        """
        path = "/fapi/v1/order"
        
        # 处理 Time In Force
        tif = req.time_in_force
        # 如果是 Post Only，币安使用 GTX
        if req.post_only: # 注意 type.py 定义的是 is_post_only 还是 post_only，此处适配
            tif = TIF_GTX
            
        # 处理 Side 枚举转字符串
        side_str = req.side.value if isinstance(req.side, Side) else req.side
            
        params = {
            "symbol": req.symbol,
            "side": side_str, 
            "type": req.order_type,
            "quantity": req.volume,
        }
        
        if req.order_type == "LIMIT":
            params["price"] = req.price
            params["timeInForce"] = tif
            
        res = self._send_request("POST", path, params)
        if res:
            # 这里的 orderId 是币安生成的 ID
            # 策略层/OMS 通常通过 clientOrderId 来追踪，或者记录这个 mapping
            # 简单起见，我们返回币安 ID
            order_id = str(res.get('orderId'))
            logger.info(f"Order Sent: ID={order_id} {req.symbol} {side_str} {req.price}")
            return order_id
        return None

    def cancel_order(self, req: CancelRequest):
        path = "/fapi/v1/order"
        params = {"symbol": req.symbol, "orderId": req.order_id}
        # logger.info(f"Cancelling Order: {req.order_id}") # 减少日志刷屏
        self._send_request("DELETE", path, params)

    def cancel_all_orders(self, symbol):
        path = "/fapi/v1/allOpenOrders"
        params = {"symbol": symbol}
        logger.info(f"Cancelling All Orders for {symbol}")
        self._send_request("DELETE", path, params)

    def get_depth_snapshot(self, symbol):
        path = "/fapi/v1/depth"
        params = {"symbol": symbol, "limit": 1000}
        return self._send_request("GET", path, params, signed=False)

    def set_one_way_mode(self):
        """强制设置为单向持仓模式"""
        self._send_request("POST", "/fapi/v1/positionSide/dual", {"dualSidePosition": "false"})

    # --- User Stream ---
    def start_user_stream(self):
        data = self._send_request("POST", "/fapi/v1/listenKey")
        if data and "listenKey" in data:
            self.listen_key = data["listenKey"]
            threading.Thread(target=self._run_user_ws, daemon=True).start()
            threading.Thread(target=self._keep_user_stream_alive, daemon=True).start()

    def _keep_user_stream_alive(self):
        while self.active:
            time.sleep(1800)
            try: self._send_request("PUT", "/fapi/v1/listenKey")
            except: pass

    def _run_user_ws(self):
        while self.active:
            ws_url = f"{self.ws_url}/{self.listen_key}"
            self.ws = websocket.WebSocketApp(ws_url, on_message=self._on_user_message)
            self.ws.run_forever(ping_interval=30, ping_timeout=10)
            time.sleep(3)

    def _on_user_message(self, ws, message):
        try:
            raw = json.loads(message)
            if raw.get("e") == "ORDER_TRADE_UPDATE": 
                self._process_order_trade_update(raw)
        except: traceback.print_exc()

    def _process_order_trade_update(self, raw):
        """
        解析交易所回报，标准化为 ExchangeOrderUpdate
        """
        o = raw["o"]
        
        # 提取关键字段
        symbol = o["s"]
        client_oid = o.get("c", "") # clientOrderId
        exchange_oid = str(o["i"])  # orderId
        status = o["X"]             # NEW, CANCELED, FILLED...
        
        filled_qty = float(o["l"])      # 本次成交量 (Last Filled Quantity)
        filled_price = float(o["L"])    # 本次成交价 (Last Filled Price)
        cum_filled_qty = float(o["z"])  # 累计成交量 (Accumulated Filled Quantity)
        update_time = float(o["T"]) / 1000.0
        
        # 构造标准化更新对象
        update = ExchangeOrderUpdate(
            client_oid=client_oid,
            exchange_oid=exchange_oid,
            symbol=symbol,
            status=status,
            filled_qty=filled_qty,
            filled_price=filled_price,
            cum_filled_qty=cum_filled_qty,
            update_time=update_time
        )
        
        # 推送给 OMS (Single Source of Truth)
        # 注意：这里不再直接推送 OrderData 或 TradeData
        # 也不再推送给 Strategy，而是只给 OMS
        self.event_engine.put(Event(EVENT_EXCHANGE_ORDER_UPDATE, update))

    # --- Market Stream ---
    def connect(self, symbols: list):
        self.symbols = [s.upper() for s in symbols]
        self.active = True
        
        for s in self.symbols:
            self.orderbooks[s] = LocalOrderBook(s)
            self.ws_buffer[s] = []
            
        self.set_one_way_mode()
        
        threading.Thread(target=self._run_market_ws, daemon=True).start()
        self.start_user_stream()
        threading.Thread(target=self._init_all_orderbooks, daemon=True).start()

    def _run_market_ws(self):
        while self.active:
            self.ws_market = websocket.WebSocketApp(
                self.ws_url, 
                on_open=self._on_market_open, 
                on_message=self._on_market_message, 
                on_error=lambda ws, err: logger.error(f"Market WS Error: {err}")
            )
            self.ws_market.run_forever(ping_interval=30, ping_timeout=10)
            time.sleep(3)

    def _on_market_open(self, ws):
        logger.info("Market WS Connected")
        params = []
        for s in self.symbols:
            params.append(f"{s.lower()}@depth@100ms")
            params.append(f"{s.lower()}@aggTrade")
            params.append(f"{s.lower()}@markPrice@1s")
        # 发送订阅
        ws.send(json.dumps({"method": "SUBSCRIBE", "params": params, "id": 1}).decode('utf-8'))

    def _on_market_message(self, ws, message):
        try:
            raw = json.loads(message)
            e_type = raw.get("e")
            symbol = raw.get("s")
            
            if e_type == "depthUpdate":
                if self.ws_buffer[symbol] is not None: 
                    self.ws_buffer[symbol].append(raw)
                else:
                    ob = self.orderbooks[symbol]
                    try:
                        ob.process_delta(raw)
                        data = ob.generate_event_data()
                        if data: self.event_engine.put(Event(EVENT_ORDERBOOK, data))
                    except OrderBookGapError:
                        logger.error(f"Gap detected for {symbol}, triggering re-sync...")
                        self.ws_buffer[symbol] = []
                        threading.Thread(target=self._re_sync_symbol, args=(symbol,), daemon=True).start()
                        
            elif e_type == "aggTrade":
                t = AggTradeData(
                    raw["s"], raw["a"], float(raw["p"]), float(raw["q"]), 
                    raw["m"], datetime.fromtimestamp(raw["T"]/1000)
                )
                self.event_engine.put(Event(EVENT_AGG_TRADE, t))
                
            elif e_type == "markPriceUpdate":
                mp = MarkPriceData(
                    raw["s"], float(raw["p"]), float(raw["i"]), float(raw["r"]), 
                    datetime.fromtimestamp(raw["T"]/1000), datetime.now()
                )
                self.event_engine.put(Event(EVENT_MARK_PRICE, mp))
        except: pass

    def _init_all_orderbooks(self):
        time.sleep(2)
        for symbol in self.symbols: self._re_sync_symbol(symbol)

    def _re_sync_symbol(self, symbol):
        logger.info(f"Syncing OrderBook for {symbol}...")
        snapshot = self.get_depth_snapshot(symbol)
        if snapshot:
            ob = self.orderbooks[symbol]
            ob.init_snapshot(snapshot)
            if self.ws_buffer[symbol]:
                for msg in self.ws_buffer[symbol]:
                    try: ob.process_delta(msg)
                    except: pass
            self.ws_buffer[symbol] = None
            logger.info(f"{symbol} Sync Done")
            data = ob.generate_event_data()
            if data: self.event_engine.put(Event(EVENT_ORDERBOOK, data))