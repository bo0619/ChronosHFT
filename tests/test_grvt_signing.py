"""GRVT order signatures against the vectors in GRVT's own SDK tests.

The private key is GRVT's public test key from ``grvt-pysdk``
(``tests/pysdk/test_grvt_raw_signing.py``); it holds no funds.
"""

import pytest

from gateway.grvt.signing import (
    MAX_EXPIRATION_NS,
    TESTNET_CHAIN_ID,
    GrvtInstrumentSpec,
    expiration_after,
    order_message,
    scaled,
    sign_order,
)

SDK_TEST_KEY = "f7934647276a6e1fa0af3f4467b4b8ddaf45d25a7368fa1a295eef49a446819d"
SUB_ACCOUNT = "8289849667772468"
EXPIRY = 1730800479321350000
NONCE = 828700936
SPEC = {"BTC_USDT_Perp": GrvtInstrumentSpec("BTC_USDT_Perp", "0x030501", 9)}


def sdk_order(size: str, price: str) -> dict:
    return order_message(
        sub_account_id=SUB_ACCOUNT,
        legs=[{"instrument": "BTC_USDT_Perp", "size": size, "limit_price": price, "is_buying_asset": False}],
        spec=SPEC, time_in_force="GOOD_TILL_TIME", post_only=False, reduce_only=False, is_market=False,
        nonce=NONCE, expiration_ns=EXPIRY,
    )


@pytest.mark.parametrize("size, price, want_r, want_s", [
    ("1.013", "68900.5",
     "0xb00512d986a718b15136a8ba23de1c1ec84bbdb9958629cbbe4909bae620bb04",
     "0x79f706de61c68cc14d7734594b5d8689df2b2a7b25951f9a3f61d799f4327ffc"),
    ("1.123123123", "68900.777123479",
     "0x365ec79d299c8bcd5f2acff89faf741a90ca02a4b8a6b1b1a5d4f3d16130f9f0",
     "0x465129bca7855f008ea5bc22fe3ee630e4a8e3b9b99c1745631deef29957048a"),
    # Digits past the 9th decimal truncate to the same signature as above.
    ("1.1231231234", "68900.7771234794",
     "0x365ec79d299c8bcd5f2acff89faf741a90ca02a4b8a6b1b1a5d4f3d16130f9f0",
     "0x465129bca7855f008ea5bc22fe3ee630e4a8e3b9b99c1745631deef29957048a"),
    ("1.1231231239", "68900.7771234799",
     "0x365ec79d299c8bcd5f2acff89faf741a90ca02a4b8a6b1b1a5d4f3d16130f9f0",
     "0x465129bca7855f008ea5bc22fe3ee630e4a8e3b9b99c1745631deef29957048a"),
])
def test_order_signature_matches_the_grvt_sdk(size, price, want_r, want_s):
    signature = sign_order(SDK_TEST_KEY, TESTNET_CHAIN_ID, sdk_order(size, price))
    assert (signature.r, signature.s, signature.v) == (want_r, want_s, 28)
    assert signature.nonce == NONCE and signature.expiration == EXPIRY
    assert signature.as_api()["expiration"] == str(EXPIRY)


def test_scaling_is_exact_decimal_truncation():
    assert scaled("1.013", 10**9, "size") == 1_013_000_000  # float math gives 1012999999
    assert scaled("68900.7771234799", 10**9, "price") == 68_900_777_123_479
    with pytest.raises(ValueError):
        scaled("-1", 10**9, "size")


def test_message_rejects_unknown_time_in_force_and_oversized_nonce():
    with pytest.raises(ValueError):
        order_message(sub_account_id=1, legs=[], spec=SPEC, time_in_force="RETAIL_PRICE_IMPROVEMENT",
                      post_only=True, reduce_only=False, is_market=False, nonce=1, expiration_ns=1)
    with pytest.raises(ValueError):
        order_message(sub_account_id=1, legs=[], spec=SPEC, time_in_force="GOOD_TILL_TIME",
                      post_only=True, reduce_only=False, is_market=False, nonce=2**32, expiration_ns=1)


def test_expiration_stays_within_thirty_days():
    assert expiration_after(60, now_ns=10) == 10 + 60 * 10**9
    with pytest.raises(ValueError):
        expiration_after(MAX_EXPIRATION_NS / 1e9 + 1)
