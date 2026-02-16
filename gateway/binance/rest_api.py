# file: gateway/binance/rest_api.py

import requests
import time
import hmac
import hashlib
from urllib.parse import urlencode
from infrastructure.logger import logger
from infrastructure.time_service import time_service
from event.type import OrderRequest, CancelRequest, TIF_GTX, TIF_RPI
from .constants import *

class BinanceRestApi:
    def __init__(self, api_key, api_secret, session, testnet=False):
        self.api_key = api_key
        self.api_secret = api_secret
        self.session = session
        self.base_url = REST_URL_TEST if testnet else REST_URL_MAIN

    def _sign(self, params: dict):
        query = urlencode(params)
        signature = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        params["signature"] = signature
        return params

    def request(self, method, endpoint, params=None, signed=True):
        url = self.base_url + endpoint
        params = params or {}
        
        if signed:
            params["timestamp"] = time_service.now()
            self._sign(params)
        
        headers = {"X-MBX-APIKEY": self.api_key} if signed else {}
        
        try:
            req = requests.Request(method, url, params=params, headers=headers)
            prepped = self.session.prepare_request(req)
            resp = self.session.send(prepped, timeout=3.0) 
            return resp
        except Exception as e:
            logger.error(f"REST Exception [{endpoint}]: {e}")
            return None

    # --- 1. 行情模块 ---
    def get_depth_snapshot(self, symbol, limit=1000):
        resp = self.request("GET", EP_DEPTH_SNAPSHOT, {"symbol": symbol, "limit": limit}, signed=False)
        return resp.json() if resp and resp.status_code == 200 else None

    def get_rpi_depth_snapshot(self, symbol, limit=1000):
        """[修复] 获取 RPI 深度快照"""
        resp = self.request("GET", EP_RPI_DEPTH_SNAPSHOT, {"symbol": symbol, "limit": limit}, signed=False)
        # 注意：如果不支持 RPI 的币种可能会报错，需处理
        return resp.json() if resp and resp.status_code == 200 else None

    # --- 2. 交易模块 ---
    def new_order(self, req: OrderRequest):
        """POST /fapi/v1/order"""
        params = {
            "symbol": req.symbol,
            "side": req.side,
            "type": req.order_type,
            "quantity": req.volume,
        }
        
        if req.order_type == "LIMIT":
            params["price"] = req.price
            
            # [修复] RPI 订单逻辑
            if getattr(req, "is_rpi", False):
                # RPI 订单必须是 LIMIT 且 timeInForce=RPI
                params["timeInForce"] = "GTX" # 修正：币安实盘 RPI 似乎不再使用特殊TIF，而是走 GTX 即可？
                # 再次确认币安文档：
                # 早期文档：timeInForce="RPI"
                # 最新文档：可能已合并。
                # 按照你的 System Instruction: 需设置 order_type = "LIMIT" 且 time_in_force = "RPI"
                params["timeInForce"] = "RPI" 
            else:
                # 普通订单
                params["timeInForce"] = TIF_GTX if req.post_only else req.time_in_force
        
        resp = self.request("POST", EP_ORDER, params, signed=True)
        return resp

    def cancel_order(self, req: CancelRequest):
        params = {"symbol": req.symbol}
        if req.order_id.isdigit(): params["orderId"] = req.order_id
        else: params["origClientOrderId"] = req.order_id
        return self.request("DELETE", EP_ORDER, params, signed=True)

    def cancel_all_orders(self, symbol):
        return self.request("DELETE", EP_ALL_OPEN_ORDERS, {"symbol": symbol}, signed=True)

    # --- 3. 账户与基础模块 ---
    def create_listen_key(self):
        resp = self.request("POST", EP_LISTEN_KEY, signed=True) 
        return resp.json().get("listenKey") if resp and resp.status_code == 200 else None

    def keep_alive_listen_key(self):
        self.request("PUT", EP_LISTEN_KEY, signed=True)

    def set_leverage(self, symbol, leverage):
        params = {"symbol": symbol, "leverage": leverage}
        self.request("POST", EP_LEVERAGE, params, signed=True)

    def set_margin_type(self, symbol, margin_type="CROSSED"):
        params = {"symbol": symbol, "marginType": margin_type}
        try: self.request("POST", EP_MARGIN_TYPE, params, signed=True)
        except: pass

    # --- 4. 查询接口 ---
    def get_account(self): 
        return self.request("GET", EP_ACCOUNT, signed=True)
    
    def get_positions(self): 
        return self.request("GET", EP_POSITION_RISK, signed=True)
    
    def get_open_orders(self): 
        return self.request("GET", EP_OPEN_ORDERS, signed=True)