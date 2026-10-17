"""GRVT trading REST client: API-key session, signed orders, account state.

Endpoints and request shapes follow GRVT's Python SDK (``grvt-pysdk``,
``grvt_raw_sync.py`` and ``grvt_raw_types.py``):

* ``POST https://edge.grvt.io/auth/api_key/login {"api_key": ...}`` sets a
  ``gravity`` session cookie with an expiry and returns the account id in
  the ``X-Grvt-Account-Id`` header; every trading call sends both. The
  session is refreshed shortly before it expires.
* Trading calls are ``POST https://trades.grvt.io/full/v1/<method>`` with
  JSON bodies that name the sub-account: ``create_order``,
  ``cancel_order``, ``cancel_all_orders``, ``order``, ``open_orders``,
  ``positions``, ``account_summary``, ``fill_history`` and
  ``cancel_on_disconnect`` (a dead-man switch: GRVT cancels the
  sub-account's open orders if it is not renewed within ``countdown_time``
  milliseconds; 0 disarms it).

Orders are signed with :mod:`gateway.grvt.signing`. A post-only order is a
maker-only GTT order; GRVT holds orders that may take for a 25 ms
speedbump, which post-only orders skip.

Client order ids are random in ``[2**63, 2**64)``, the range GRVT reserves
for API clients (its UI uses ``[0, 2**63)``); an id must be unique among
the sub-account's open orders.

Responses carry the payload under ``result``; errors carry ``code`` and
``message`` (HTTP 4xx/5xx) and raise :class:`GrvtApiError`.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie

import requests

from .signing import (
    MAINNET_CHAIN_ID,
    TESTNET_CHAIN_ID,
    GrvtInstrumentSpec,
    expiration_after,
    new_nonce,
    order_message,
    sign_order,
)

ENVIRONMENTS = {
    "prod": {"edge": "https://edge.grvt.io", "trades": "https://trades.grvt.io",
             "market_data": "https://market-data.grvt.io", "chain_id": MAINNET_CHAIN_ID},
    "testnet": {"edge": "https://edge.testnet.grvt.io", "trades": "https://trades.testnet.grvt.io",
                "market_data": "https://market-data.testnet.grvt.io", "chain_id": TESTNET_CHAIN_ID},
}
# Refresh the session this long before the cookie expires.
SESSION_REFRESH_MARGIN_SEC = 60.0
DEFAULT_TIMEOUT_SEC = 5.0
DEFAULT_ORDER_TTL_SEC = 24 * 3600.0


class GrvtApiError(RuntimeError):
    def __init__(self, status: int, code, message: str, path: str):
        super().__init__(f"GRVT {path} failed: HTTP {status} code {code}: {message}")
        self.status, self.code, self.message, self.path = status, code, message, path


@dataclass(frozen=True, slots=True)
class GrvtSession:
    cookie: str
    expires_at: float
    account_id: str | None


def client_order_id() -> str:
    """A random id in the range GRVT reserves for API clients."""
    return str(2**63 + secrets.randbelow(2**63))


class GrvtRestClient:
    def __init__(self, *, api_key: str, sub_account_id: str, private_key: str | None = None,
                 environment: str = "prod", session: requests.Session | None = None,
                 timeout_sec: float = DEFAULT_TIMEOUT_SEC, clock=time.time):
        if environment not in ENVIRONMENTS:
            raise ValueError(f"unknown GRVT environment {environment!r}")
        if not api_key or not sub_account_id:
            raise ValueError("GRVT trading needs an API key and a sub-account id")
        self.env = ENVIRONMENTS[environment]
        self.api_key = api_key
        self.sub_account_id = str(sub_account_id)
        self.private_key = private_key
        self.http = session or requests.Session()
        self.timeout_sec = float(timeout_sec)
        self.clock = clock
        self.instruments: dict[str, GrvtInstrumentSpec] = {}
        self._session: GrvtSession | None = None
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- session
    def session(self) -> GrvtSession:
        with self._lock:
            if self._session is None or self._session.expires_at - self.clock() < SESSION_REFRESH_MARGIN_SEC:
                self._session = self._login()
            return self._session

    def _login(self) -> GrvtSession:
        path = self.env["edge"] + "/auth/api_key/login"
        response = self.http.post(path, json={"api_key": self.api_key}, timeout=self.timeout_sec)
        if not response.ok:
            raise GrvtApiError(response.status_code, None, response.text[:200], "/auth/api_key/login")
        cookie = SimpleCookie()
        cookie.load(response.headers.get("Set-Cookie", ""))
        if "gravity" not in cookie:
            raise GrvtApiError(response.status_code, None, "login set no gravity cookie", "/auth/api_key/login")
        morsel = cookie["gravity"]
        expires = morsel["expires"]
        expires_at = parsedate_to_datetime(expires).timestamp() if expires else self.clock() + 3600.0
        return GrvtSession(cookie=morsel.value, expires_at=expires_at,
                           account_id=response.headers.get("X-Grvt-Account-Id"))

    def auth_headers(self) -> dict[str, str]:
        """Headers for an authenticated call (also used by the private websocket)."""
        session = self.session()
        headers = {"Cookie": f"gravity={session.cookie}", "Content-Type": "application/json"}
        if session.account_id:
            headers["X-Grvt-Account-Id"] = session.account_id
        return headers

    # ------------------------------------------------------------- transport
    def _post(self, base: str, method: str, body: Mapping, *, auth: bool = True):
        path = f"/full/v1/{method}"
        headers = self.auth_headers() if auth else {"Content-Type": "application/json"}
        response = self.http.post(self.env[base] + path, json=dict(body), headers=headers,
                                  timeout=self.timeout_sec)
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not response.ok or (isinstance(payload, Mapping) and "code" in payload and "result" not in payload):
            payload = payload if isinstance(payload, Mapping) else {}
            raise GrvtApiError(response.status_code, payload.get("code"),
                               str(payload.get("message") or response.text[:200]), path)
        return payload.get("result") if isinstance(payload, Mapping) else payload

    # ------------------------------------------------------------ reference
    def load_instruments(self) -> dict[str, GrvtInstrumentSpec]:
        result = self._post("market_data", "all_instruments",
                            {"kind": ["PERPETUAL"], "is_active": True, "limit": 1000}, auth=False)
        self.instruments = {
            item["instrument"]: GrvtInstrumentSpec(item["instrument"], item["instrument_hash"],
                                                   int(item["base_decimals"]))
            for item in result or []
        }
        return self.instruments

    # ---------------------------------------------------------------- orders
    def build_limit_order(self, *, instrument: str, is_buy: bool, size: str, price: str,
                          post_only: bool = True, reduce_only: bool = False,
                          time_in_force: str = "GOOD_TILL_TIME", ttl_sec: float = DEFAULT_ORDER_TTL_SEC,
                          client_id: str | None = None, now_ns: int | None = None) -> dict:
        """A signed ``create_order`` body. Size and price are decimal strings."""
        if self.private_key is None:
            raise ValueError("signing GRVT orders needs the trading key")
        if instrument not in self.instruments:
            raise KeyError(f"unknown GRVT instrument {instrument}; call load_instruments()")
        if post_only and time_in_force != "GOOD_TILL_TIME":
            raise ValueError("GRVT post-only orders are GOOD_TILL_TIME")
        legs = [{"instrument": instrument, "size": str(size), "limit_price": str(price),
                 "is_buying_asset": bool(is_buy)}]
        nonce = new_nonce()
        expiration = expiration_after(ttl_sec, now_ns=now_ns)
        message = order_message(sub_account_id=self.sub_account_id, legs=legs, spec=self.instruments,
                                time_in_force=time_in_force, post_only=post_only, reduce_only=reduce_only,
                                is_market=False, nonce=nonce, expiration_ns=expiration)
        signature = sign_order(self.private_key, self.env["chain_id"], message)
        create_ns = time.time_ns() if now_ns is None else now_ns
        return {"order": {
            "sub_account_id": self.sub_account_id,
            "is_market": False,
            "time_in_force": time_in_force,
            "post_only": bool(post_only),
            "reduce_only": bool(reduce_only),
            "legs": legs,
            "signature": signature.as_api(),
            "metadata": {"client_order_id": client_id or client_order_id(), "create_time": str(create_ns)},
        }}

    def create_order(self, body: Mapping) -> dict:
        return self._post("trades", "create_order", body)

    def cancel_order(self, *, order_id: str | None = None, client_id: str | None = None) -> dict:
        if not (order_id or client_id):
            raise ValueError("cancel needs an order id or a client order id")
        body = {"sub_account_id": self.sub_account_id}
        body.update({"order_id": order_id} if order_id else {"client_order_id": client_id})
        return self._post("trades", "cancel_order", body)

    def cancel_all_orders(self, *, base: str | None = None) -> dict:
        body = {"sub_account_id": self.sub_account_id}
        if base:
            body.update({"kind": ["PERPETUAL"], "base": [base]})
        return self._post("trades", "cancel_all_orders", body)

    def get_order(self, *, order_id: str | None = None, client_id: str | None = None) -> dict:
        body = {"sub_account_id": self.sub_account_id}
        body.update({"order_id": order_id} if order_id else {"client_order_id": client_id})
        return self._post("trades", "order", body)

    def open_orders(self) -> list[dict]:
        return self._post("trades", "open_orders", {"sub_account_id": self.sub_account_id}) or []

    # --------------------------------------------------------------- account
    def positions(self) -> list[dict]:
        return self._post("trades", "positions", {"sub_account_id": self.sub_account_id}) or []

    def account_summary(self) -> dict:
        return self._post("trades", "account_summary", {"sub_account_id": self.sub_account_id}) or {}

    def fill_history(self, *, start_ns: int | None = None, limit: int = 500, cursor: str | None = None) -> dict:
        body: dict = {"sub_account_id": self.sub_account_id, "limit": int(limit)}
        if start_ns is not None:
            body["start_time"] = str(int(start_ns))
        if cursor:
            body["cursor"] = cursor
        return self._post("trades", "fill_history", body)

    def cancel_on_disconnect(self, countdown_ms: int) -> dict:
        """Arm (or with 0 disarm) GRVT's dead-man switch for the sub-account."""
        if int(countdown_ms) < 0:
            raise ValueError("countdown must be >= 0 ms")
        return self._post("trades", "cancel_on_disconnect",
                          {"sub_account_id": self.sub_account_id, "countdown_time": str(int(countdown_ms))})


__all__ = ["ENVIRONMENTS", "GrvtApiError", "GrvtRestClient", "GrvtSession", "client_order_id"]
