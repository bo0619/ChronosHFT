# file: gateway/binance_future.py

import orjson as json
import threading
import time
import hmac
import hashlib
import requests
import traceback
from urllib.parse import urlencode
from datetime import datetime
import websocket

# [修复] 这里必须导入 CancelRequest
from event.type import Event, EVENT_LOG, EVENT_ORDERBOOK, EVENT_ORDER_UPDATE, EVENT_TRADE_UPDATE, EVENT_AGG_TRADE
from event.type import OrderRequest, OrderData, TradeData, AggTradeData, CancelRequest
from event.type import Direction_LONG, Direction_SHORT, Action_OPEN, Action_CLOSE
from data.orderbook import LocalOrderBook
from infrastructure.logger import logger
from infrastructure.time_service import time_service


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


    # --- REST API ---
    def _sign_request(self, params: dict):
        query_string = urlencode(params)
        signature = hmac.new(self.api_secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()
        params['signature'] = signature
        return params

    def _send_request(self, method, path, params=None, signed=True):
        if params is None: 
            params = {}
        
        if signed:
            # [修改] 使用 time_service 获取精确时间
            params['timestamp'] = time_service.now()
            params = self._sign_request(params)
        headers = {'X-MBX-APIKEY': self.api_key} if signed else {}
        url = self.rest_url + path
        try:
            if method == "GET": response = requests.get(url, headers=headers, params=params)
            elif method == "POST": response = requests.post(url, headers=headers, params=params)
            elif method == "PUT": response = requests.put(url, headers=headers, params=params)
            elif method == "DELETE": response = requests.delete(url, headers=headers, params=params)
            
            if response.status_code == 200: 
                return response.json()
            if response.json().get('code') == -4059: 
                return response.json()

            logger.error(f"请求失败 [{response.status_code}]: {response.text}")
            return None
        except Exception as e:
            logger.error(f"请求异常: {e}")
            return None

    def send_order(self, req: OrderRequest):
        path = "/fapi/v1/order"
        side = "BUY" if (req.direction == Direction_LONG and req.action == Action_OPEN) or \
                        (req.direction == Direction_SHORT and req.action == Action_CLOSE) else "SELL"
        position_side = req.direction
        params = {"symbol": req.symbol, "side": side, "positionSide": position_side, "type": req.order_type, "quantity": req.volume}
        if req.order_type == "LIMIT":
            params["price"] = req.price
            params["timeInForce"] = "GTC"
        res = self._send_request("POST", path, params)
        if res:
            order_id = str(res.get('orderId'))
            logger.info(f"订单已发送, OrderID: {order_id}-{req.symbol}-{req.price}")
            return order_id
        return None

    def cancel_order(self, req: CancelRequest):
        """撤销单个订单"""
        path = "/fapi/v1/order"
        params = {
            "symbol": req.symbol,
            "orderId": req.order_id
        }
        logger.info(f"正在撤单: {req.order_id}")
        self._send_request("DELETE", path, params)

    def cancel_all_orders(self, symbol):
        """撤销某币种所有挂单"""
        path = "/fapi/v1/allOpenOrders"
        params = {"symbol": symbol}
        logger.info(f"正在撤销 {symbol} 全部挂单")
        self._send_request("DELETE", path, params)

    def get_depth_snapshot(self, symbol):
        path = "/fapi/v1/depth"
        params = {"symbol": symbol, "limit": 1000}
        return self._send_request("GET", path, params, signed=False)

    def set_hedge_mode(self):
        self._send_request("POST", "/fapi/v1/positionSide/dual", {"dualSidePosition": "true"})

    # --- User Stream ---
    def start_user_stream(self):
        data = self._send_request("POST", "/fapi/v1/listenKey")
        if data and "listenKey" in data:
            self.listen_key = data["listenKey"]
            threading.Thread(target=self._run_user_ws).start()
            threading.Thread(target=self._keep_user_stream_alive).start()

    def _keep_user_stream_alive(self):
        while self.active:
            time.sleep(1800)
            try: self._send_request("PUT", "/fapi/v1/listenKey")
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
            if raw.get("e") == "ORDER_TRADE_UPDATE": self._process_order_trade_update(raw)
        except: traceback.print_exc()

    def _process_order_trade_update(self, raw):
        o = raw["o"]
        symbol, order_id = o["s"], str(o["i"])
        side, pos_side = o["S"], o["ps"]
        status = o["X"]
        last_filled = float(o["l"])
        
        action = Action_OPEN
        if pos_side == Direction_LONG and side == "SELL": action = Action_CLOSE
        elif pos_side == Direction_SHORT and side == "BUY": action = Action_CLOSE
        
        order = OrderData(symbol, order_id, pos_side, action, float(o["p"]), float(o["q"]), float(o["z"]), status, datetime.now())
        self.event_engine.put(Event(EVENT_ORDER_UPDATE, order))
        if o["x"] == "TRADE" and last_filled > 0:
            trade = TradeData(symbol, order_id, str(o["t"]), pos_side, action, float(o["L"]), last_filled, datetime.now())
            self.event_engine.put(Event(EVENT_TRADE_UPDATE, trade))

    # --- Market Stream ---
    def connect(self, symbols: list):
        self.symbols = [s.upper() for s in symbols]
        self.active = True
        for s in self.symbols:
            self.orderbooks[s] = LocalOrderBook(s)
            self.ws_buffer[s] = []
        self.set_hedge_mode()
        threading.Thread(target=self._run_market_ws).start()
        self.start_user_stream()
        threading.Thread(target=self._init_orderbooks).start()

    def _run_market_ws(self):
        while self.active:
            self.ws_market = websocket.WebSocketApp(self.ws_url, on_open=self._on_market_open, on_message=self._on_market_message)
            self.ws_market.run_forever(ping_interval=30, ping_timeout=10)
            time.sleep(3)

    def _on_market_open(self, ws):
        logger.info("Market WS Connected")
        # 双流订阅
        params = []
        for s in self.symbols:
            params.append(f"{s.lower()}@depth@100ms")
            params.append(f"{s.lower()}@aggTrade")
        ws.send(json.dumps({"method": "SUBSCRIBE", "params": params, "id": 1}).decode('utf-8'))

    def _on_market_message(self, ws, message):
        try:
            raw = json.loads(message)
            e_type = raw.get("e")
            if e_type == "depthUpdate":
                s = raw["s"]
                if self.ws_buffer[s] is not None: self.ws_buffer[s].append(raw)
                else:
                    ob = self.orderbooks[s]
                    ob.process_delta(raw)
                    self.event_engine.put(Event(EVENT_ORDERBOOK, ob.generate_event_data()))
            elif e_type == "aggTrade":
                t = AggTradeData(
                    raw["s"], raw["a"], float(raw["p"]), float(raw["q"]), 
                    raw["m"], datetime.fromtimestamp(raw["T"]/1000)
                )
                self.event_engine.put(Event(EVENT_AGG_TRADE, t))
        except: pass

    def _init_orderbooks(self):
        time.sleep(2)
        for symbol in self.symbols:
            snapshot = self.get_depth_snapshot(symbol)
            if snapshot:
                ob = self.orderbooks[symbol]
                ob.init_snapshot(snapshot)
                if self.ws_buffer[symbol]:
                    for msg in self.ws_buffer[symbol]: ob.process_delta(msg)
                self.ws_buffer[symbol] = None
                logger.info(f"{symbol} Sync Done")
                self.event_engine.put(Event(EVENT_ORDERBOOK, ob.generate_event_data()))