"""Pure Binance USD-M user-data stream normalization."""

from __future__ import annotations

import time
from collections.abc import Iterable

from event.type import ExchangeAccountUpdate, ExchangeOrderUpdate


class BinanceAccountStreamParser:
    """Convert Binance user-data payloads into transport-neutral events."""

    @staticmethod
    def parse_optional_float(value):
        if value in (None, ""):
            return None
        return float(value)

    @staticmethod
    def parse_optional_int(value, default=None):
        if value in (None, ""):
            return default
        return int(value)

    @classmethod
    def parse_order_update(
        cls,
        msg: dict,
        *,
        received_timestamp: float | None = None,
        received_monotonic: float | None = None,
        corrected_received_timestamp: float | None = None,
        clock_offset_ms: float | None = None,
        now=time.time,
        monotonic=time.perf_counter,
    ) -> ExchangeOrderUpdate:
        order = msg.get("o", {})
        update_time_ms = order.get("T") or msg.get("T") or msg.get("E") or 0
        received_timestamp = float(received_timestamp or now())
        received_monotonic = float(received_monotonic or monotonic())
        return ExchangeOrderUpdate(
            # USD-M user data events do not expose a globally contiguous
            # sequence. Ordering is validated per order in the OMS instead.
            seq=0,
            client_oid=order.get("c", ""),
            exchange_oid=str(order.get("i", "")),
            symbol=order.get("s", ""),
            status=order.get("X", ""),
            filled_qty=float(order.get("l", 0.0) or 0.0),
            filled_price=float(
                order.get("L", 0.0) or order.get("ap", 0.0) or 0.0
            ),
            cum_filled_qty=float(order.get("z", 0.0) or 0.0),
            update_time=(
                float(update_time_ms) / 1000.0 if update_time_ms else now()
            ),
            commission=cls.parse_optional_float(order.get("n")),
            commission_asset=order.get("N") or "",
            realized_pnl=cls.parse_optional_float(order.get("rp")),
            is_maker=bool(order.get("m")) if "m" in order else None,
            trade_id=cls.parse_optional_int(order.get("t"), default=-1),
            order_type=str(order.get("o", "") or "").upper(),
            time_in_force=str(order.get("f", "") or "").upper(),
            received_timestamp=received_timestamp,
            received_monotonic=received_monotonic,
            dispatch_timestamp=now(),
            dispatch_monotonic=monotonic(),
            clock_offset_ms=clock_offset_ms,
            corrected_received_timestamp=float(
                corrected_received_timestamp or received_timestamp
            ),
        )

    @classmethod
    def parse_account_update(
        cls,
        msg: dict,
        *,
        tracked_symbols: Iterable[str] = (),
        received_timestamp: float | None = None,
        received_monotonic: float | None = None,
        corrected_received_timestamp: float | None = None,
        clock_offset_ms: float | None = None,
        now=time.time,
        monotonic=time.perf_counter,
    ) -> ExchangeAccountUpdate:
        payload = msg.get("a", {})
        balances = payload.get("B", [])
        balance_entry = cls.select_balance_entry(balances, tracked_symbols)
        positions = {}
        for raw_position in payload.get("P", []):
            symbol = raw_position.get("s")
            if not symbol:
                continue
            positions[symbol] = {
                "volume": float(raw_position.get("pa", 0.0) or 0.0),
                "entry_price": float(raw_position.get("ep", 0.0) or 0.0),
                "unrealized_pnl": float(raw_position.get("up", 0.0) or 0.0),
            }

        # Transaction time is the ordering boundary shared with order updates.
        event_time_ms = msg.get("T") or msg.get("E") or 0
        received_timestamp = float(received_timestamp or now())
        received_monotonic = float(received_monotonic or monotonic())
        return ExchangeAccountUpdate(
            asset=balance_entry.get("a", "") if balance_entry else "",
            wallet_balance=(
                float(balance_entry.get("wb", 0.0) or 0.0)
                if balance_entry
                else 0.0
            ),
            # ACCOUNT_UPDATE does not publish availableBalance. Its `cw`
            # field is crossWalletBalance and must not replace REST truth.
            available_balance=None,
            balances=cls.extract_balance_snapshot(balances),
            positions=positions,
            reason=payload.get("m", ""),
            event_time=(
                float(event_time_ms) / 1000.0 if event_time_ms else now()
            ),
            received_timestamp=received_timestamp,
            received_monotonic=received_monotonic,
            dispatch_timestamp=now(),
            dispatch_monotonic=monotonic(),
            clock_offset_ms=clock_offset_ms,
            corrected_received_timestamp=float(
                corrected_received_timestamp or received_timestamp
            ),
        )

    @classmethod
    def extract_balance_snapshot(cls, balances) -> dict:
        snapshot = {}
        for entry in balances or []:
            asset = entry.get("a")
            if not asset:
                continue
            snapshot[asset] = {
                "wallet_balance": float(entry.get("wb", 0.0) or 0.0),
                "available_balance": None,
                "cross_wallet_balance": cls.parse_optional_float(
                    entry.get("cw")
                ),
                "balance_change": cls.parse_optional_float(entry.get("bc")),
            }
        return snapshot

    @classmethod
    def select_balance_entry(cls, balances, tracked_symbols=()):
        if not balances:
            return None

        tracked_assets = []
        for symbol in tracked_symbols:
            asset = cls.extract_quote_asset(symbol)
            if asset and asset not in tracked_assets:
                tracked_assets.append(asset)

        for asset in tracked_assets:
            for entry in balances:
                if entry.get("a") == asset:
                    return entry

        for entry in balances:
            if entry.get("a") in {"USDT", "USDC", "BUSD", "FDUSD"}:
                return entry
        return balances[0]

    @staticmethod
    def extract_quote_asset(symbol: str) -> str:
        for suffix in (
            "USDT",
            "USDC",
            "BUSD",
            "FDUSD",
            "BTC",
            "ETH",
            "BNB",
        ):
            if symbol.endswith(suffix):
                return suffix
        return ""
