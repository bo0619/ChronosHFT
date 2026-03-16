import hashlib
import hmac
import threading
import time
from urllib.parse import urlencode

import requests

from infrastructure.logger import logger
from infrastructure.time_service import time_service
from event.type import CancelRequest, OrderRequest, TIF_GTX
from .constants import *


class BinanceRestApi:
    def __init__(self, api_key, api_secret, session, testnet=False):
        self.api_key = api_key
        self.api_secret = api_secret
        self.session = session
        self.base_url = REST_URL_TEST if testnet else REST_URL_MAIN

        self.request_lock = threading.Lock()
        self.last_request_ts = 0.0
        self.endpoint_last_request_ts = {}
        self.endpoint_cooldown_until = {}
        self.min_signed_interval_sec = 0.20
        self.min_public_interval_sec = 0.05
        self.endpoint_intervals = {
            EP_ACCOUNT: 1.00,
            EP_POSITION_RISK: 1.50,
            EP_OPEN_ORDERS: 1.00,
            EP_ORDER: 0.10,
            EP_ALL_OPEN_ORDERS: 0.30,
            EP_LEVERAGE: 0.30,
            EP_MARGIN_TYPE: 0.30,
            EP_POSITION_MODE: 0.30,
            EP_LISTEN_KEY: 0.30,
        }
        self.max_retries = 2
        self.retry_backoff_sec = 0.50
        self.failure_backoff_multiplier = 2.0
        self.max_endpoint_cooldown_sec = 10.0
        self.timeout_sec = 3.0

    def _sign(self, params: dict):
        query = urlencode(params)
        signature = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        params["signature"] = signature
        return params

    def _throttle(self, endpoint: str, signed: bool):
        min_interval = self.min_signed_interval_sec if signed else self.min_public_interval_sec
        endpoint_interval = max(min_interval, self.endpoint_intervals.get(endpoint, min_interval))

        with self.request_lock:
            now = time.monotonic()
            global_wait = max(0.0, min_interval - (now - self.last_request_ts))
            endpoint_wait = max(
                0.0,
                endpoint_interval - (now - self.endpoint_last_request_ts.get(endpoint, 0.0)),
            )
            cooldown_wait = max(0.0, self.endpoint_cooldown_until.get(endpoint, 0.0) - now)
            wait_time = max(global_wait, endpoint_wait, cooldown_wait)
            if wait_time > 0:
                time.sleep(wait_time)
            stamp = time.monotonic()
            self.last_request_ts = stamp
            self.endpoint_last_request_ts[endpoint] = stamp

    def _mark_failure_cooldown(self, endpoint: str, attempt: int):
        endpoint_interval = self.endpoint_intervals.get(endpoint, self.min_signed_interval_sec)
        cooldown_sec = min(
            self.max_endpoint_cooldown_sec,
            max(self.retry_backoff_sec * attempt, endpoint_interval * self.failure_backoff_multiplier),
        )
        self.endpoint_cooldown_until[endpoint] = time.monotonic() + cooldown_sec
        return cooldown_sec

    def _extract_error_details(self, response):
        code = ""
        message = ""
        try:
            payload = response.json()
        except Exception:
            payload = {}

        if isinstance(payload, dict):
            raw_code = payload.get("code")
            code = "" if raw_code is None else str(raw_code)
            message = str(payload.get("msg", "") or "")
        return code, message

    def _is_retryable_response(self, status_code: int, error_code: str) -> bool:
        if status_code >= 500 or status_code in {418, 429}:
            return True
        return error_code in {"-1001", "-1003", "-1007", "-1008"}

    def response_succeeded(self, response, accepted_error_codes=None) -> bool:
        accepted_error_codes = {str(code) for code in (accepted_error_codes or set())}
        if response is None:
            return False
        if response.status_code == 200:
            return True
        error_code, _message = self._extract_error_details(response)
        return bool(error_code and error_code in accepted_error_codes)

    def request(self, method, endpoint, params=None, signed=True, suppress_error_codes=None):
        url = self.base_url + endpoint
        base_params = dict(params or {})
        headers = {"X-MBX-APIKEY": self.api_key} if signed else {}
        suppress_error_codes = {str(code) for code in (suppress_error_codes or set())}

        for attempt in range(1, self.max_retries + 1):
            self._throttle(endpoint, signed)
            req_params = dict(base_params)

            if signed:
                req_params["timestamp"] = time_service.now()
                self._sign(req_params)

            try:
                req = requests.Request(method, url, params=req_params, headers=headers)
                prepped = self.session.prepare_request(req)
                response = self.session.send(prepped, timeout=self.timeout_sec)
                self.endpoint_cooldown_until[endpoint] = 0.0
                if response.status_code == 200:
                    return response

                error_code, error_message = self._extract_error_details(response)
                if error_code and error_code in suppress_error_codes:
                    return response
                logger.error(
                    f"REST Error [{endpoint}] status={response.status_code} code={error_code or '-'} "
                    f"msg={error_message or '-'}"
                )

                if signed and error_code == "-1021":
                    sync_ok = time_service._sync()
                    if attempt < self.max_retries and sync_ok:
                        logger.warning(
                            f"REST retry [{endpoint}] attempt {attempt}/{self.max_retries} after timestamp resync"
                        )
                        continue

                if attempt < self.max_retries and self._is_retryable_response(response.status_code, error_code):
                    cooldown_sec = self._mark_failure_cooldown(endpoint, attempt)
                    logger.warning(
                        f"REST retry [{endpoint}] attempt {attempt}/{self.max_retries} after "
                        f"{cooldown_sec:.2f}s: status={response.status_code} code={error_code or '-'}"
                    )
                    continue
                return response
            except Exception as exc:
                cooldown_sec = self._mark_failure_cooldown(endpoint, attempt)
                if attempt >= self.max_retries:
                    logger.error(f"REST Exception [{endpoint}]: {exc}")
                    return None
                logger.warning(
                    f"REST retry [{endpoint}] attempt {attempt}/{self.max_retries} after {cooldown_sec:.2f}s: {exc}"
                )

        return None

    def get_depth_snapshot(self, symbol, limit=1000):
        resp = self.request("GET", EP_DEPTH_SNAPSHOT, {"symbol": symbol, "limit": limit}, signed=False)
        return resp.json() if resp and resp.status_code == 200 else None

    def new_order(self, req: OrderRequest, client_oid: str = None):
        params = {
            "symbol": req.symbol,
            "side": req.side,
            "type": req.order_type,
            "quantity": req.volume,
        }

        if req.reduce_only:
            params["reduceOnly"] = "true"

        if client_oid:
            params["newClientOrderId"] = client_oid

        if req.order_type == "LIMIT":
            params["price"] = req.price
            params["timeInForce"] = TIF_GTX if req.post_only else req.time_in_force

        return self.request("POST", EP_ORDER, params, signed=True)

    def cancel_order(self, req: CancelRequest):
        params = {"symbol": req.symbol}
        if req.order_id.isdigit():
            params["orderId"] = req.order_id
        else:
            params["origClientOrderId"] = req.order_id
        return self.request("DELETE", EP_ORDER, params, signed=True)

    def cancel_all_orders(self, symbol):
        return self.request("DELETE", EP_ALL_OPEN_ORDERS, {"symbol": symbol}, signed=True)

    def create_listen_key(self):
        resp = self.request("POST", EP_LISTEN_KEY, signed=True)
        return resp.json().get("listenKey") if resp and resp.status_code == 200 else None

    def keep_alive_listen_key(self):
        self.request("PUT", EP_LISTEN_KEY, signed=True)

    def set_leverage(self, symbol, leverage):
        params = {"symbol": symbol, "leverage": leverage}
        return self.request("POST", EP_LEVERAGE, params, signed=True)

    def set_margin_type(self, symbol, margin_type="CROSSED"):
        params = {"symbol": symbol, "marginType": margin_type}
        return self.request(
            "POST",
            EP_MARGIN_TYPE,
            params,
            signed=True,
            suppress_error_codes={"-4046"},
        )

    def set_position_mode(self, position_mode="ONE_WAY"):
        normalized = str(position_mode or "ONE_WAY").upper()
        dual_side = "true" if normalized in {"HEDGE", "HEDGE_MODE"} else "false"
        params = {"dualSidePosition": dual_side}
        return self.request(
            "POST",
            EP_POSITION_MODE,
            params,
            signed=True,
            suppress_error_codes={"-4059"},
        )

    def get_account(self):
        return self.request("GET", EP_ACCOUNT, signed=True)

    def get_positions(self):
        return self.request("GET", EP_POSITION_RISK, signed=True)

    def get_open_orders(self):
        return self.request("GET", EP_OPEN_ORDERS, signed=True)
