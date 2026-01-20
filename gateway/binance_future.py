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

from infrastructure.logger import logger
from infrastructure.time_service import time_service

from event.type import Event, EVENT_LOG, EVENT_ORDERBOOK, EVENT_ORDER_UPDATE, EVENT_TRADE_UPDATE, EVENT_AGG_TRADE, EVENT_MARK_PRICE
from event.type import OrderRequest, OrderData, TradeData, AggTradeData, CancelRequest, MarkPriceData
from event.type import Side_BUY, Side_SELL, OrderBookGapError
from data.orderbook import LocalOrderBook

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
        self.listen_key = ""

    # --- REST API ---
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
            if method == "GET": response = requests.get(url, headers=headers, params=params)
            elif method == "POST": response = requests.post(url, headers=headers, params=params)
            elif method == "PUT": response = requests.put(url, headers=headers, params=params)
            elif method == "DELETE": response = requests.delete(url, headers=headers, params=params)
            
            if response.status_code == 200: return response.json()
            res_json = response.json()
            code = res_json.get('code')
            
            # 忽略常见非关键错误
            if code == -4059: return res_json # No need to change position side
            if code == -2011: # Unknown order sent
                logger.info(f"Order missing when cancelling: {res_json.get('msg')}")
                return None
            
            logger.error(f"Request Failed [{response.status_code}]: {response.text}")
            return None
        except Exception as e:
            logger.error(f"Request Exception: {e}")
            return None

    def send_order(self, req: OrderRequest):
        path = "/fapi/v1/order"
        # 单向持仓模式：只需要 side=BUY 或 SELL，不需要 positionSide
        params = {
            "symbol": req.symbol,
            "side": req.side,
            "type": req.order_type,
            "quantity": req.volume,
        }
        if req.order_type == "LIMIT":
            params["price"] = req.price
            params["timeInForce"] = "GTC"
            
        res = self._send_request("POST", path, params)
        if res:
            order_id = str(res.get('orderId'))
            logger.info(f"Order Sent: ID={order_id} {req.symbol} {req.side} {req.price} x {req.volume}")
            return order_id
        return None

    def cancel_order(self, req: CancelRequest):
        path = "/fapi/v1/order"
        params = {"symbol": req.symbol, "orderId": req.order_id}
        logger.info(f"Cancelling Order: {req.order_id}")
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
        """[修改] 强制设置为单向持仓模式"""
        # "true": 双向, "false": 单向
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
            if raw.get("e") == "ORDER_TRADE_UPDATE": self._process_order_trade_update(raw)
        except: traceback.print_exc()

    def _process_order_trade_update(self, raw):
        o = raw["o"]
        symbol, order_id = o["s"], str(o["i"])
        side = o["S"] # BUY / SELL
        status = o["X"]
        last_filled = float(o["l"])
        
        # 推送 OrderData
        order = OrderData(
            symbol=symbol, order_id=order_id, side=side,
            price=float(o["p"]), volume=float(o["q"]), 
            traded=float(o["z"]), status=status, datetime=datetime.now()
        )
        self.event_engine.put(Event(EVENT_ORDER_UPDATE, order))
        
        # 推送 TradeData
        if o["x"] == "TRADE" and last_filled > 0:
            trade = TradeData(
                symbol=symbol, order_id=order_id, trade_id=str(o["t"]),
                side=side, price=float(o["L"]), volume=last_filled,
                datetime=datetime.now()
            )
            self.event_engine.put(Event(EVENT_TRADE_UPDATE, trade))

    # --- Market Stream ---
    def connect(self, symbols: list):
        self.symbols = [s.upper() for s in symbols]
        self.active = True
        
        for s in self.symbols:
            self.orderbooks[s] = LocalOrderBook(s)
            self.ws_buffer[s] = []
            
        # [修改] 设置为单向持仓
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
                    symbol=raw["s"], mark_price=float(raw["p"]), index_price=float(raw["i"]),
                    funding_rate=float(raw["r"]), next_funding_time=datetime.fromtimestamp(raw["T"]/1000),
                    datetime=datetime.now()
                )
                self.event_engine.put(Event(EVENT_MARK_PRICE, mp))
        except Exception: pass

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