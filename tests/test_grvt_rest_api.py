"""GRVT REST client against a scripted HTTP session (no network)."""

import json

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from gateway.grvt.rest_api import GrvtApiError, GrvtRestClient, client_order_id
from gateway.grvt.signing import ORDER_TYPES, GrvtInstrumentSpec, order_message

TEST_KEY = "f7934647276a6e1fa0af3f4467b4b8ddaf45d25a7368fa1a295eef49a446819d"  # GRVT SDK test key, no funds


class Response:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code, self._payload, self.headers = status, payload, headers or {}
        self.ok = 200 <= status < 300
        self.text = json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


class Session:
    def __init__(self, replies, cookie_expiry="Wed, 21 Oct 2026 07:28:00 GMT"):
        self.calls, self.replies, self.cookie_expiry = [], list(replies), cookie_expiry

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers or {}})
        if url.endswith("/auth/api_key/login"):
            return Response(200, {}, {"Set-Cookie": f"gravity=abc123; Path=/; Expires={self.cookie_expiry}",
                                      "X-Grvt-Account-Id": "ACC1"})
        return self.replies.pop(0)


def client(replies, now=1_792_000_000.0, **kwargs):
    session = Session(replies)
    rest = GrvtRestClient(api_key="key", sub_account_id="8289849667772468", private_key=TEST_KEY,
                          session=session, clock=lambda: now, **kwargs)
    rest.instruments = {"XRP_USDT_Perp": GrvtInstrumentSpec("XRP_USDT_Perp", "0x030901", 9)}
    return rest, session


def test_login_once_and_send_cookie_and_account_header():
    rest, session = client([Response(200, {"result": []}), Response(200, {"result": []})])
    rest.open_orders()
    rest.positions()
    logins = [c for c in session.calls if c["url"].endswith("/auth/api_key/login")]
    assert len(logins) == 1 and logins[0]["json"] == {"api_key": "key"}
    trade = session.calls[1]
    assert trade["url"] == "https://trades.grvt.io/full/v1/open_orders"
    assert trade["headers"]["Cookie"] == "gravity=abc123"
    assert trade["headers"]["X-Grvt-Account-Id"] == "ACC1"
    assert trade["json"] == {"sub_account_id": "8289849667772468"}


def test_session_refreshes_before_the_cookie_expires():
    # Cookie expires at 2026-10-21 07:28:00 UTC; 30 s before it the client logs in again.
    expiry = 1_792_567_680.0
    rest, session = client([Response(200, {"result": []})] * 2, now=expiry - 30)
    rest.open_orders()
    rest.open_orders()
    assert sum(c["url"].endswith("/auth/api_key/login") for c in session.calls) == 2


def test_signed_post_only_order_recovers_to_the_trading_key():
    rest, _ = client([])
    body = rest.build_limit_order(instrument="XRP_USDT_Perp", is_buy=True, size="50", price="2.4512",
                                  client_id="9223372036854775809", now_ns=1_792_000_000_000_000_000)
    order = body["order"]
    assert order["post_only"] is True and order["time_in_force"] == "GOOD_TILL_TIME"
    assert order["metadata"]["client_order_id"] == "9223372036854775809"
    signature = order["signature"]
    message = order_message(sub_account_id=order["sub_account_id"], legs=order["legs"], spec=rest.instruments,
                            time_in_force="GOOD_TILL_TIME", post_only=True, reduce_only=False, is_market=False,
                            nonce=signature["nonce"], expiration_ns=int(signature["expiration"]))
    signable = encode_typed_data({"name": "GRVT Exchange", "version": "0", "chainId": 325}, ORDER_TYPES, message)
    recovered = Account.recover_message(signable, vrs=(signature["v"], int(signature["r"], 16),
                                                       int(signature["s"], 16)))
    assert recovered == Account.from_key(TEST_KEY).address == signature["signer"]


def test_post_only_must_be_gtt_and_unknown_instruments_are_refused():
    rest, _ = client([])
    with pytest.raises(ValueError):
        rest.build_limit_order(instrument="XRP_USDT_Perp", is_buy=True, size="1", price="1",
                               time_in_force="IMMEDIATE_OR_CANCEL")
    with pytest.raises(KeyError):
        rest.build_limit_order(instrument="BTC_USDT_Perp", is_buy=True, size="1", price="1")


def test_errors_raise_with_code_and_message():
    rest, _ = client([Response(400, {"code": 2001, "message": "insufficient margin", "status": 400}),
                      Response(200, {"code": 1002, "message": "bad nonce"})])
    with pytest.raises(GrvtApiError) as http_error:
        rest.create_order({"order": {}})
    assert http_error.value.code == 2001 and "insufficient margin" in str(http_error.value)
    with pytest.raises(GrvtApiError):
        rest.create_order({"order": {}})


def test_cancel_and_dead_man_switch_bodies():
    rest, session = client([Response(200, {"result": {"ack": True}})] * 3)
    rest.cancel_order(client_id="9223372036854775809")
    rest.cancel_all_orders(base="XRP")
    rest.cancel_on_disconnect(5_000)
    bodies = [c["json"] for c in session.calls if "/full/v1/" in c["url"]]
    assert bodies[0] == {"sub_account_id": "8289849667772468", "client_order_id": "9223372036854775809"}
    assert bodies[1] == {"sub_account_id": "8289849667772468", "kind": ["PERPETUAL"], "base": ["XRP"]}
    assert bodies[2] == {"sub_account_id": "8289849667772468", "countdown_time": "5000"}
    with pytest.raises(ValueError):
        rest.cancel_order()


def test_client_order_ids_stay_in_the_api_range():
    ids = {int(client_order_id()) for _ in range(200)}
    assert all(2**63 <= value < 2**64 for value in ids) and len(ids) == 200
