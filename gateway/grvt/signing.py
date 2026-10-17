"""EIP-712 signatures for GRVT orders.

GRVT matches orders on its own engine but settles them on its chain, so
every order carries the trading key's EIP-712 signature over its economic
fields. The typed data follows GRVT's Python SDK (``grvt-pysdk``,
``grvt_raw_signing.py``):

* domain ``{"name": "GRVT Exchange", "version": "0", "chainId": ...}``,
  chain id 325 on mainnet and 326 on testnet;
* ``Order(subAccountID uint64, isMarket bool, timeInForce uint8,
  postOnly bool, reduceOnly bool, legs OrderLeg[], nonce uint32,
  expiration int64)``;
* ``OrderLeg(assetID uint256, contractSize uint64, limitPrice uint64,
  isBuyingContract bool)``.

``assetID`` is the instrument's ``instrument_hash``, ``contractSize`` the
size in units of ``10**-base_decimals`` and ``limitPrice`` the price in
units of ``1e-9``, both truncated from the decimal strings (never through
floats: ``int(float("1.013") * 1e9)`` is ``1012999999``). ``expiration`` is
in unix nanoseconds and at most 30 days ahead; ``nonce`` is random so the
same instruction can be sent twice.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from eth_account import Account
from eth_account.messages import encode_typed_data

MAINNET_CHAIN_ID = 325
TESTNET_CHAIN_ID = 326
PRICE_MULTIPLIER = Decimal(1_000_000_000)
MAX_EXPIRATION_NS = 30 * 24 * 3600 * 1_000_000_000

# The signed time-in-force codes (the API sends the names).
SIGNED_TIME_IN_FORCE = {
    "GOOD_TILL_TIME": 1,
    "ALL_OR_NONE": 2,
    "IMMEDIATE_OR_CANCEL": 3,
    "FILL_OR_KILL": 4,
}

ORDER_TYPES = {
    "Order": [
        {"name": "subAccountID", "type": "uint64"},
        {"name": "isMarket", "type": "bool"},
        {"name": "timeInForce", "type": "uint8"},
        {"name": "postOnly", "type": "bool"},
        {"name": "reduceOnly", "type": "bool"},
        {"name": "legs", "type": "OrderLeg[]"},
        {"name": "nonce", "type": "uint32"},
        {"name": "expiration", "type": "int64"},
    ],
    "OrderLeg": [
        {"name": "assetID", "type": "uint256"},
        {"name": "contractSize", "type": "uint64"},
        {"name": "limitPrice", "type": "uint64"},
        {"name": "isBuyingContract", "type": "bool"},
    ],
}


@dataclass(frozen=True, slots=True)
class GrvtInstrumentSpec:
    """What signing needs from ``all_instruments``."""

    instrument: str
    instrument_hash: str
    base_decimals: int


@dataclass(frozen=True, slots=True)
class GrvtOrderSignature:
    signer: str
    r: str
    s: str
    v: int
    expiration: int
    nonce: int

    def as_api(self) -> dict:
        return {"signer": self.signer, "r": self.r, "s": self.s, "v": self.v,
                "expiration": str(self.expiration), "nonce": self.nonce}


def scaled(value: str | Decimal, multiplier: Decimal, name: str) -> int:
    """``value * multiplier`` truncated, from a decimal string."""
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError(f"{name} must be a finite non-negative decimal")
    return int((number * multiplier).to_integral_value(rounding=ROUND_DOWN))


def order_message(*, sub_account_id: str | int, legs: list[dict], spec: dict[str, GrvtInstrumentSpec],
                  time_in_force: str, post_only: bool, reduce_only: bool, is_market: bool,
                  nonce: int, expiration_ns: int) -> dict:
    """The EIP-712 message for an order; ``legs`` use the API's field names."""
    if time_in_force not in SIGNED_TIME_IN_FORCE:
        raise ValueError(f"unsupported time in force {time_in_force!r}")
    if not 0 <= int(nonce) < 2**32:
        raise ValueError("nonce must fit uint32")
    signed_legs = []
    for leg in legs:
        instrument = spec[leg["instrument"]]
        signed_legs.append({
            "assetID": int(instrument.instrument_hash, 16),
            "contractSize": scaled(leg["size"], Decimal(10) ** instrument.base_decimals, "size"),
            "limitPrice": scaled(leg.get("limit_price") or "0", PRICE_MULTIPLIER, "limit_price"),
            "isBuyingContract": bool(leg["is_buying_asset"]),
        })
    return {
        "subAccountID": int(sub_account_id),
        "isMarket": bool(is_market),
        "timeInForce": SIGNED_TIME_IN_FORCE[time_in_force],
        "postOnly": bool(post_only),
        "reduceOnly": bool(reduce_only),
        "legs": signed_legs,
        "nonce": int(nonce),
        "expiration": int(expiration_ns),
    }


def sign_order(private_key: str, chain_id: int, message: dict) -> GrvtOrderSignature:
    domain = {"name": "GRVT Exchange", "version": "0", "chainId": int(chain_id)}
    account = Account.from_key(private_key)
    signed = account.sign_message(encode_typed_data(domain, ORDER_TYPES, message))
    return GrvtOrderSignature(
        signer=str(account.address),
        r="0x" + signed.r.to_bytes(32, byteorder="big").hex(),
        s="0x" + signed.s.to_bytes(32, byteorder="big").hex(),
        v=int(signed.v),
        expiration=int(message["expiration"]),
        nonce=int(message["nonce"]),
    )


def new_nonce() -> int:
    return secrets.randbelow(2**32)


def expiration_after(seconds: float, now_ns: int | None = None) -> int:
    """Unix-nanosecond expiration ``seconds`` ahead, within GRVT's 30 days."""
    now_ns = time.time_ns() if now_ns is None else now_ns
    ahead = int(seconds * 1_000_000_000)
    if not 0 < ahead <= MAX_EXPIRATION_NS:
        raise ValueError("order expiration must be within 30 days")
    return now_ns + ahead


__all__ = [
    "GrvtInstrumentSpec",
    "GrvtOrderSignature",
    "MAINNET_CHAIN_ID",
    "TESTNET_CHAIN_ID",
    "expiration_after",
    "new_nonce",
    "order_message",
    "scaled",
    "sign_order",
]
