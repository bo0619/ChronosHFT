"""Minimal Binance USD-M HTTP client for the independent safety plane."""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from urllib.parse import urlencode

import requests


REST_URL_MAIN = "https://fapi.binance.com"
REST_URL_TEST = "https://testnet.binancefuture.com"


class LocalRiskResponse:
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = int(status_code)
        self._payload = {"code": str(code), "msg": str(message)}
        self.headers = {}

    def json(self):
        return dict(self._payload)


class BinanceRiskHttpClient:
    """Small authenticated client with an explicitly injected timestamp."""

    ENDPOINT_WEIGHTS = {
        "/fapi/v1/time": 1,
        "/fapi/v2/account": 5,
        "/fapi/v2/positionRisk": 5,
        "/fapi/v1/openOrders": 40,
        "/fapi/v1/income": 30,
        "/fapi/v1/premiumIndex": 1,
        "/fapi/v1/countdownCancelAll": 10,
        "/fapi/v1/allOpenOrders": 1,
        "/fapi/v1/order": 1,
    }

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        session,
        *,
        testnet: bool,
        rate_limit_budget=None,
        timestamp_provider=None,
        monotonic=time.perf_counter,
        sleep=time.sleep,
    ) -> None:
        self.api_key = str(api_key)
        self.api_secret = str(api_secret)
        self.session = session
        self.base_url = REST_URL_TEST if testnet else REST_URL_MAIN
        self.rate_limit_budget = rate_limit_budget
        self.timestamp_provider = timestamp_provider
        self.clock_resync_callback = None
        self.timeout_sec = 3.0
        self.recv_window_ms = 5_000
        self.max_retries = 2
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_request_at = 0.0

    def _timestamp_ms(self) -> int:
        provider = self.timestamp_provider
        if not callable(provider):
            raise RuntimeError("risk_exchange_clock_unavailable")
        value = provider()
        if isinstance(value, bool):
            raise RuntimeError("risk_exchange_clock_invalid")
        value = int(value)
        if value <= 0:
            raise RuntimeError("risk_exchange_clock_invalid")
        return value

    def _sign(self, params: dict) -> dict:
        query = urlencode(params)
        params["signature"] = hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return params

    def _acquire_budget(
        self,
        endpoint: str,
        priority: str,
    ) -> LocalRiskResponse | None:
        budget = self.rate_limit_budget
        if budget is None:
            return None
        decision = budget.acquire(
            self.ENDPOINT_WEIGHTS.get(endpoint, 1),
            priority=priority,
            endpoint=endpoint,
            now_epoch=time.time(),
        )
        if decision.allowed:
            return None
        return LocalRiskResponse(
            429,
            "LOCAL_RATE_LIMIT",
            str(decision.reason or "risk_rate_limit_rejected"),
        )

    def _record_budget(self, response) -> None:
        budget = self.rate_limit_budget
        if budget is None or response is None:
            return
        budget.record_response(
            getattr(response, "headers", {}),
            status_code=int(getattr(response, "status_code", 0) or 0),
            now_epoch=time.time(),
        )

    def request(
        self,
        method: str,
        endpoint: str,
        params=None,
        *,
        signed: bool = True,
        rate_limit_priority: str | None = None,
        max_attempts: int | None = None,
    ):
        method = str(method).upper()
        priority = str(
            rate_limit_priority
            or (
                "emergency"
                if method in {"POST", "DELETE"}
                else "background"
            )
        )
        attempts = max(
            1,
            int(
                max_attempts
                if max_attempts is not None
                else self.max_retries
            ),
        )
        for attempt in range(attempts):
            with self._lock:
                now = self._monotonic()
                delay = max(0.0, 0.05 - (now - self._last_request_at))
                if delay:
                    self._sleep(delay)
                self._last_request_at = self._monotonic()
            budget_rejection = self._acquire_budget(endpoint, priority)
            if budget_rejection is not None:
                return budget_rejection
            request_params = dict(params or {})
            headers = {}
            try:
                if signed:
                    request_params["timestamp"] = self._timestamp_ms()
                    request_params["recvWindow"] = self.recv_window_ms
                    self._sign(request_params)
                    headers["X-MBX-APIKEY"] = self.api_key
                request = requests.Request(
                    method,
                    self.base_url + endpoint,
                    params=request_params,
                    headers=headers,
                )
                response = self.session.send(
                    self.session.prepare_request(request),
                    timeout=self.timeout_sec,
                )
            except Exception as exc:
                if attempt + 1 >= attempts:
                    return LocalRiskResponse(
                        503,
                        "RISK_HTTP_EXCEPTION",
                        f"{type(exc).__name__}:{exc}",
                    )
                self._sleep(0.1 * (attempt + 1))
                continue
            self._record_budget(response)
            if response.status_code == 200:
                return response
            try:
                error_code = str(response.json().get("code", "") or "")
            except (AttributeError, TypeError, ValueError):
                error_code = ""
            if signed and error_code == "-1021" and attempt + 1 < attempts:
                callback = self.clock_resync_callback
                if callable(callback) and bool(callback()):
                    continue
            return response
        return LocalRiskResponse(503, "RISK_HTTP_EXHAUSTED", "attempts_exhausted")

    def get_server_time(self, *, emergency: bool = False):
        return self.request(
            "GET",
            "/fapi/v1/time",
            signed=False,
            rate_limit_priority="emergency" if emergency else None,
        )

    def get_account(self):
        return self.request("GET", "/fapi/v2/account")

    def get_positions(self, *, emergency: bool = False):
        return self.request(
            "GET",
            "/fapi/v2/positionRisk",
            rate_limit_priority="emergency" if emergency else None,
        )

    def get_open_orders(self, symbol=None, *, emergency: bool = False):
        params = {}
        if str(symbol or "").strip():
            params["symbol"] = str(symbol).strip().upper()
        return self.request(
            "GET",
            "/fapi/v1/openOrders",
            params,
            rate_limit_priority="emergency" if emergency else None,
        )

    def get_income_history(self, **kwargs):
        params = dict(kwargs)
        for source, destination in (
            ("start_time", "startTime"),
            ("end_time", "endTime"),
            ("income_type", "incomeType"),
        ):
            if source in params:
                params[destination] = params.pop(source)
        return self.request("GET", "/fapi/v1/income", params)

    def set_countdown_cancel_all(self, symbol, countdown_time_ms):
        return self.request(
            "POST",
            "/fapi/v1/countdownCancelAll",
            {
                "symbol": str(symbol or "").upper(),
                "countdownTime": max(0, int(countdown_time_ms)),
            },
            rate_limit_priority="emergency",
        )

    def cancel_all_orders(self, symbol):
        return self.request(
            "DELETE",
            "/fapi/v1/allOpenOrders",
            {"symbol": str(symbol or "").upper()},
            rate_limit_priority="emergency",
        )

    def new_reduce_only_market_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        client_oid: str,
    ):
        return self.request(
            "POST",
            "/fapi/v1/order",
            {
                "symbol": str(symbol or "").upper(),
                "side": str(side or "").upper(),
                "type": "MARKET",
                "quantity": float(quantity),
                "reduceOnly": "true",
                "newClientOrderId": str(client_oid or "")[:36],
            },
            rate_limit_priority="emergency",
            max_attempts=1,
        )

    def request_public(self, endpoint: str, params=None):
        return self.request("GET", endpoint, params, signed=False)

    def close(self) -> None:
        self.session.close()
