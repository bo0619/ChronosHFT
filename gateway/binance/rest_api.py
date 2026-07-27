import hashlib
import hmac
import threading
import time
from urllib.parse import urlencode

import requests

from event.type import CancelRequest, OrderRequest
from infrastructure.logger import logger
from infrastructure.time_service import time_service

from .constants import (
    EP_ACCOUNT,
    EP_ALL_OPEN_ORDERS,
    EP_ALL_ORDERS,
    EP_COMMISSION_RATE,
    EP_COUNTDOWN_CANCEL_ALL,
    EP_DEPTH_SNAPSHOT,
    EP_INCOME,
    EP_LEVERAGE,
    EP_LISTEN_KEY,
    EP_MARGIN_TYPE,
    EP_OPEN_ORDERS,
    EP_ORDER,
    EP_POSITION_MODE,
    EP_POSITION_RISK,
    EP_RPI_DEPTH,
    EP_TIME,
    EP_USER_TRADES,
    REST_URL_MAIN,
    REST_URL_TEST,
)
from .rate_limit_budget import BinanceRateLimitBudget
from .rest_metrics import BinanceRestMetrics


class _LocalGuardResponse:
    def __init__(self, code: str, message: str):
        self.status_code = 409
        self._payload = {"code": str(code), "msg": str(message)}

    def json(self):
        return dict(self._payload)


class BinanceRestApi:
    def __init__(
        self,
        api_key,
        api_secret,
        session,
        testnet=False,
        rate_limit_budget: BinanceRateLimitBudget | None = None,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.session = session
        self.base_url = REST_URL_TEST if testnet else REST_URL_MAIN
        self.rate_limit_budget = rate_limit_budget

        self.request_lock = threading.Lock()
        self.last_request_ts = 0.0
        self.endpoint_last_request_ts = {}
        self.endpoint_cooldown_until = {}
        self.min_signed_interval_sec = 0.20
        self.min_public_interval_sec = 0.05
        self.endpoint_intervals = {
            EP_RPI_DEPTH: 1.00,
            EP_COMMISSION_RATE: 1.00,
            EP_ACCOUNT: 1.00,
            EP_POSITION_RISK: 1.50,
            EP_OPEN_ORDERS: 1.00,
            EP_ALL_ORDERS: 1.00,
            EP_USER_TRADES: 0.50,
            EP_INCOME: 2.00,
            EP_COUNTDOWN_CANCEL_ALL: 1.00,
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
        self.recv_window_ms = 5000
        self.order_clock_guard = None
        self.clock_resync_callback = None
        self.telemetry = BinanceRestMetrics()
        self._last_budget_rejection_log_at = 0.0

    def _sign(self, params: dict):
        query = urlencode(params)
        signature = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        params["signature"] = signature
        return params

    def _throttle(
        self,
        endpoint: str,
        signed: bool,
        params=None,
        *,
        priority: str = "background",
    ):
        min_interval = self.min_signed_interval_sec if signed else self.min_public_interval_sec
        endpoint_interval = max(min_interval, self.endpoint_intervals.get(endpoint, min_interval))
        emergency = str(priority or "").lower() in {
            "emergency",
            "safety",
            "reduce_only",
        }
        throttle_key = endpoint
        if endpoint == EP_OPEN_ORDERS and (params or {}).get("symbol"):
            throttle_key = (
                f"{endpoint}:{str(params['symbol']).strip().upper()}"
            )

        with self.request_lock:
            now = time.perf_counter()
            global_wait = max(0.0, min_interval - (now - self.last_request_ts))
            endpoint_wait = max(
                0.0,
                endpoint_interval
                - (
                    now
                    - self.endpoint_last_request_ts.get(
                        throttle_key,
                        0.0,
                    )
                ),
            )
            # Local transport backoff from normal traffic must not delay
            # cancellation or reduce-only recovery. Exchange Retry-After
            # remains authoritative in the cross-process budget coordinator.
            cooldown_wait = (
                0.0
                if emergency
                else max(
                    0.0,
                    self.endpoint_cooldown_until.get(endpoint, 0.0) - now,
                )
            )
            wait_time = max(global_wait, endpoint_wait, cooldown_wait)
            if wait_time > 0:
                time.sleep(wait_time)
            stamp = time.perf_counter()
            self.last_request_ts = stamp
            self.endpoint_last_request_ts[throttle_key] = stamp

    @staticmethod
    def _request_weight(method: str, endpoint: str, params: dict) -> int:
        method = str(method or "").upper()
        params = params or {}
        if endpoint == EP_DEPTH_SNAPSHOT:
            limit = int(params.get("limit", 100) or 100)
            if limit <= 50:
                return 2
            if limit <= 100:
                return 5
            if limit <= 500:
                return 10
            return 20
        if endpoint == EP_RPI_DEPTH:
            return 20
        if endpoint == EP_ACCOUNT:
            return 5
        if endpoint == EP_POSITION_RISK:
            return 5
        if endpoint == EP_OPEN_ORDERS:
            return 1 if params.get("symbol") else 40
        if endpoint == EP_INCOME:
            return 30
        if endpoint == EP_COMMISSION_RATE:
            return 20
        if endpoint == EP_COUNTDOWN_CANCEL_ALL:
            return 10
        if endpoint in {EP_ALL_ORDERS, EP_USER_TRADES}:
            return 5
        if endpoint == EP_ORDER and method == "POST":
            # Binance currently reports zero IP request weight for new-order
            # placement, but reserving one unit keeps this fail-safe when the
            # exchange changes accounting before the client is upgraded.
            return 1
        return 1

    @staticmethod
    def _request_priority(method: str, endpoint: str, params: dict) -> str:
        method = str(method or "").upper()
        params = params or {}
        if (
            endpoint == EP_ORDER
            and method == "POST"
            and str(params.get("reduceOnly", "")).lower() == "true"
        ):
            return "emergency"
        if endpoint in {EP_ALL_OPEN_ORDERS, EP_COUNTDOWN_CANCEL_ALL}:
            return "emergency"
        if endpoint == EP_ORDER and method == "DELETE":
            return "emergency"
        if endpoint == EP_LISTEN_KEY:
            return "safety"
        if endpoint == EP_ORDER and method == "POST":
            return "trading"
        if (
            (endpoint == EP_ORDER and method == "GET")
            or endpoint in {EP_ALL_ORDERS, EP_USER_TRADES}
        ):
            # These reads resolve ambiguous submit/cancel outcomes and
            # backfill executions after a user-stream gap. Treating them as
            # background work can strand the OMS exactly when the protected
            # trading reserve is needed to recover order truth.
            return "trading"
        if endpoint in {
            EP_LEVERAGE,
            EP_MARGIN_TYPE,
            EP_POSITION_MODE,
        } and method == "POST":
            return "trading"
        return "background"

    def _acquire_rate_limit_budget(
        self,
        method: str,
        endpoint: str,
        params: dict,
        priority: str | None,
    ):
        budget = self.rate_limit_budget
        if budget is None:
            return None
        resolved_priority = priority or self._request_priority(
            method,
            endpoint,
            params,
        )
        decision = budget.acquire(
            self._request_weight(method, endpoint, params),
            priority=resolved_priority,
            endpoint=endpoint,
        )
        if decision.allowed:
            if decision.emergency_bypass:
                logger.critical(
                    "Binance REST emergency request bypassed unavailable "
                    f"local rate coordinator: {decision.reason}"
                )
            return None
        now = time.perf_counter()
        if now - self._last_budget_rejection_log_at >= 5.0:
            self._last_budget_rejection_log_at = now
            logger.error(
                "Binance REST request blocked by host weight budget "
                f"endpoint={endpoint} priority={resolved_priority} "
                f"reason={decision.reason} "
                f"retry_after={decision.retry_after_sec:.2f}s"
            )
        return _LocalGuardResponse(
            "RATE_LIMIT_BUDGET_EXHAUSTED",
            f"{decision.reason};retry_after_sec="
            f"{decision.retry_after_sec:.3f}",
        )

    def _record_rate_limit_response(self, response) -> None:
        budget = self.rate_limit_budget
        if budget is None:
            return
        try:
            budget.record_response(
                getattr(response, "headers", None),
                status_code=getattr(response, "status_code", 0),
            )
        except Exception as exc:
            logger.error(
                "Binance REST rate-limit coordinator could not record "
                f"exchange response: {type(exc).__name__}:{exc}"
            )

    def _mark_failure_cooldown(self, endpoint: str, attempt: int):
        endpoint_interval = self.endpoint_intervals.get(endpoint, self.min_signed_interval_sec)
        cooldown_sec = min(
            self.max_endpoint_cooldown_sec,
            max(self.retry_backoff_sec * attempt, endpoint_interval * self.failure_backoff_multiplier),
        )
        self.endpoint_cooldown_until[endpoint] = time.perf_counter() + cooldown_sec
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

    def get_metrics_snapshot(self) -> dict:
        snapshot = self.telemetry.snapshot()
        budget = self.rate_limit_budget
        if budget is not None:
            try:
                snapshot["host_rate_limit_budget"] = budget.snapshot()
            except Exception as exc:
                snapshot["host_rate_limit_budget"] = {
                    "enabled": True,
                    "error": f"{type(exc).__name__}:{exc}",
                }
        return snapshot

    def _record_response_metrics(
        self,
        *,
        method,
        endpoint,
        response,
        started_monotonic_ns,
        completed_monotonic_ns,
    ):
        try:
            self.telemetry.record_response(
                method=method,
                endpoint=endpoint,
                status_code=response.status_code,
                headers=getattr(response, "headers", None),
                rtt_ns=completed_monotonic_ns - started_monotonic_ns,
                completed_monotonic_ns=completed_monotonic_ns,
            )
        except Exception:
            # Observability must never alter the REST command result.
            return

    def _record_exception_metrics(
        self,
        *,
        method,
        endpoint,
        exc,
        started_monotonic_ns,
        completed_monotonic_ns,
    ):
        try:
            self.telemetry.record_exception(
                method=method,
                endpoint=endpoint,
                exception_type=type(exc).__name__,
                rtt_ns=completed_monotonic_ns - started_monotonic_ns,
                completed_monotonic_ns=completed_monotonic_ns,
            )
        except Exception:
            # Preserve the original transport exception and retry path.
            return

    def _run_pre_send_guard(self, guard):
        guards = (
            tuple(item for item in guard if callable(item))
            if isinstance(guard, (list, tuple))
            else ((guard,) if callable(guard) else ())
        )
        if not guards:
            return None
        for candidate in guards:
            try:
                result = candidate()
            except Exception as exc:
                return _LocalGuardResponse(
                    "PRE_SEND_GUARD_UNAVAILABLE",
                    f"{type(exc).__name__}:{exc}",
                )
            if isinstance(result, tuple):
                allowed = bool(result[0]) if result else False
                code = (
                    str(result[1])
                    if len(result) > 1
                    else "PRE_SEND_GUARD_REJECTED"
                )
                message = str(result[2]) if len(result) > 2 else code
            else:
                allowed = bool(result)
                code = "PRE_SEND_GUARD_REJECTED"
                message = "pre-send guard rejected the request"
            if not allowed:
                return _LocalGuardResponse(code, message)
        return None

    def _resynchronize_exchange_clock(self):
        callback = self.clock_resync_callback
        if callable(callback):
            result = callback()
            if isinstance(result, tuple):
                return bool(result[0]) if result else False
            return bool(result)
        return bool(time_service.synchronize_now())

    def request(
        self,
        method,
        endpoint,
        params=None,
        signed=True,
        suppress_error_codes=None,
        pre_send_guard=None,
        max_attempts=None,
        rate_limit_priority=None,
    ):
        url = self.base_url + endpoint
        base_params = dict(params or {})
        headers = {"X-MBX-APIKEY": self.api_key} if signed else {}
        suppress_error_codes = {str(code) for code in (suppress_error_codes or set())}

        attempt_limit = (
            self.max_retries
            if max_attempts is None
            else max(1, int(max_attempts))
        )
        for attempt in range(1, attempt_limit + 1):
            resolved_priority = (
                rate_limit_priority
                or self._request_priority(method, endpoint, base_params)
            )
            self._throttle(
                endpoint,
                signed,
                base_params,
                priority=resolved_priority,
            )
            req_params = dict(base_params)

            if signed:
                req_params["timestamp"] = time_service.now()
                req_params["recvWindow"] = self.recv_window_ms
                self._sign(req_params)

            try:
                req = requests.Request(method, url, params=req_params, headers=headers)
                prepped = self.session.prepare_request(req)
                guard_rejection = self._run_pre_send_guard(pre_send_guard)
                if guard_rejection is not None:
                    return guard_rejection
                budget_rejection = self._acquire_rate_limit_budget(
                    method,
                    endpoint,
                    base_params,
                    resolved_priority,
                )
                if budget_rejection is not None:
                    return budget_rejection
                started_monotonic_ns = time.perf_counter_ns()
                try:
                    response = self.session.send(prepped, timeout=self.timeout_sec)
                except Exception as exc:
                    completed_monotonic_ns = time.perf_counter_ns()
                    self._record_exception_metrics(
                        method=method,
                        endpoint=endpoint,
                        exc=exc,
                        started_monotonic_ns=started_monotonic_ns,
                        completed_monotonic_ns=completed_monotonic_ns,
                    )
                    raise
                completed_monotonic_ns = time.perf_counter_ns()
                self._record_response_metrics(
                    method=method,
                    endpoint=endpoint,
                    response=response,
                    started_monotonic_ns=started_monotonic_ns,
                    completed_monotonic_ns=completed_monotonic_ns,
                )
                self._record_rate_limit_response(response)
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
                    sync_ok = self._resynchronize_exchange_clock()
                    if attempt < attempt_limit and sync_ok:
                        logger.warning(
                            f"REST retry [{endpoint}] attempt "
                            f"{attempt}/{attempt_limit} after timestamp resync"
                        )
                        continue

                if (
                    attempt < attempt_limit
                    and self._is_retryable_response(
                        response.status_code,
                        error_code,
                    )
                ):
                    cooldown_sec = self._mark_failure_cooldown(endpoint, attempt)
                    logger.warning(
                        f"REST retry [{endpoint}] attempt "
                        f"{attempt}/{attempt_limit} after "
                        f"{cooldown_sec:.2f}s: status={response.status_code} code={error_code or '-'}"
                    )
                    continue
                return response
            except Exception as exc:
                cooldown_sec = self._mark_failure_cooldown(endpoint, attempt)
                if attempt >= attempt_limit:
                    logger.error(f"REST Exception [{endpoint}]: {exc}")
                    return None
                logger.warning(
                    f"REST retry [{endpoint}] attempt "
                    f"{attempt}/{attempt_limit} after {cooldown_sec:.2f}s: {exc}"
                )

        return None

    def get_depth_snapshot(self, symbol, limit=1000):
        resp = self.request("GET", EP_DEPTH_SNAPSHOT, {"symbol": symbol, "limit": limit}, signed=False)
        return resp.json() if resp and resp.status_code == 200 else None

    def get_rpi_depth(self, symbol, limit=1000):
        """Low-frequency diagnostic book with eligible RPI liquidity included."""
        if int(limit) != 1000:
            raise ValueError("Binance USD-M rpiDepth only supports limit=1000")
        resp = self.request(
            "GET",
            EP_RPI_DEPTH,
            {"symbol": str(symbol or "").upper(), "limit": int(limit)},
            signed=False,
        )
        return resp.json() if resp and resp.status_code == 200 else None

    def get_commission_rate(self, symbol):
        """Return account-specific final maker, taker, and RPI rates."""
        return self.request(
            "GET",
            EP_COMMISSION_RATE,
            {"symbol": str(symbol or "").upper()},
            signed=True,
        )

    def new_order(
        self,
        req: OrderRequest,
        client_oid: str = None,
        pre_send_guard=None,
    ):
        if req.is_rpi and req.order_type != "LIMIT":
            raise ValueError("Binance RPI requires a LIMIT order")
        if req.post_only and req.time_in_force not in {"GTX", "RPI"}:
            raise ValueError(
                "Binance post-only LIMIT orders require GTX or RPI timeInForce"
            )

        params = {
            "symbol": req.symbol,
            "side": req.side,
            "type": req.order_type,
            "quantity": req.volume,
        }

        if req.reduce_only:
            params["reduceOnly"] = "true"

        if (
            req.self_trade_prevention_mode
            and req.order_type == "LIMIT"
            and req.time_in_force in {"GTC", "IOC", "GTD"}
        ):
            params["selfTradePreventionMode"] = (
                req.self_trade_prevention_mode
            )

        if client_oid:
            params["newClientOrderId"] = client_oid

        if req.order_type == "LIMIT":
            params["price"] = req.price
            params["timeInForce"] = req.time_in_force

        guards = []
        if not req.reduce_only and self.order_clock_guard is not None:
            guards.append(self.order_clock_guard)
        if pre_send_guard is not None:
            guards.append(pre_send_guard)
        return self.request(
            "POST",
            EP_ORDER,
            params,
            signed=True,
            pre_send_guard=tuple(guards),
            # A client order ID makes recovery possible; blindly replaying a
            # timed-out POST can instead turn an unknown outcome into a false
            # rejection or a second transport race.
            max_attempts=1,
        )

    def cancel_order(self, req: CancelRequest):
        params = {"symbol": req.symbol}
        if req.order_id.isdigit():
            params["orderId"] = req.order_id
        else:
            params["origClientOrderId"] = req.order_id
        return self.request("DELETE", EP_ORDER, params, signed=True)

    def cancel_all_orders(self, symbol):
        return self.request("DELETE", EP_ALL_OPEN_ORDERS, {"symbol": symbol}, signed=True)

    def set_countdown_cancel_all(self, symbol, countdown_time_ms):
        params = {
            "symbol": str(symbol or "").upper(),
            "countdownTime": max(0, int(countdown_time_ms)),
        }
        return self.request(
            "POST",
            EP_COUNTDOWN_CANCEL_ALL,
            params,
            signed=True,
        )

    def create_listen_key(self):
        resp = self.request("POST", EP_LISTEN_KEY, signed=True)
        return resp.json().get("listenKey") if resp and resp.status_code == 200 else None

    def keep_alive_listen_key(self):
        return self.request("PUT", EP_LISTEN_KEY, signed=True)

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

    def get_position_mode(self):
        return self.request("GET", EP_POSITION_MODE, signed=True)

    def get_account(self):
        return self.request("GET", EP_ACCOUNT, signed=True)

    def get_server_time(self, *, emergency=False):
        return self.request(
            "GET",
            EP_TIME,
            signed=False,
            rate_limit_priority="emergency" if emergency else None,
        )

    def get_positions(self, *, emergency=False):
        return self.request(
            "GET",
            EP_POSITION_RISK,
            signed=True,
            rate_limit_priority="emergency" if emergency else None,
        )

    def get_open_orders(self, symbol=None, *, emergency=False):
        params = {}
        if str(symbol or "").strip():
            params["symbol"] = str(symbol).strip().upper()
        if emergency:
            return self.request(
                "GET",
                EP_OPEN_ORDERS,
                params,
                signed=True,
                rate_limit_priority="emergency",
            )
        return self.request("GET", EP_OPEN_ORDERS, params, signed=True)

    def query_order(self, symbol, order_id):
        params = {"symbol": symbol}
        if str(order_id).isdigit():
            params["orderId"] = order_id
        else:
            params["origClientOrderId"] = order_id
        return self.request("GET", EP_ORDER, params, signed=True)

    def get_all_orders(
        self,
        symbol,
        order_id=None,
        start_time=None,
        end_time=None,
        limit=1000,
    ):
        params = {"symbol": symbol, "limit": int(limit)}
        if order_id is not None:
            params["orderId"] = int(order_id)
        if start_time is not None:
            params["startTime"] = int(start_time)
        if end_time is not None:
            params["endTime"] = int(end_time)
        return self.request("GET", EP_ALL_ORDERS, params, signed=True)

    def get_user_trades(
        self,
        symbol,
        from_id=None,
        start_time=None,
        end_time=None,
        limit=1000,
    ):
        params = {"symbol": symbol, "limit": int(limit)}
        if from_id is not None:
            params["fromId"] = int(from_id)
        else:
            if start_time is not None:
                params["startTime"] = int(start_time)
            if end_time is not None:
                params["endTime"] = int(end_time)
        return self.request("GET", EP_USER_TRADES, params, signed=True)

    def get_income_history(self, **kwargs):
        params = dict(kwargs or {})
        if "start_time" in params:
            params["startTime"] = int(params.pop("start_time"))
        if "end_time" in params:
            params["endTime"] = int(params.pop("end_time"))
        if "income_type" in params:
            params["incomeType"] = str(params.pop("income_type"))
        if "limit" in params:
            params["limit"] = int(params["limit"])
        if "page" in params:
            params["page"] = int(params["page"])
        return self.request("GET", EP_INCOME, params, signed=True)
