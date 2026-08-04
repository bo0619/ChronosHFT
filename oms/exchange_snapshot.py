"""Validated exchange snapshots for OMS recovery workflows."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ExchangeSnapshotQueries:
    """Read-only exchange truth dependencies used by snapshot capture."""

    open_orders: Callable[[], Any]
    account: Callable[[], Any]
    positions: Callable[[], Any]


@dataclass(frozen=True, slots=True)
class StableSnapshotPolicy:
    """Sampling policy read at the start of one snapshot attempt series."""

    stability_required: int
    max_attempts: int
    settle_interval_sec: float


@dataclass(frozen=True, slots=True)
class ExchangeTruthSnapshot:
    """One structurally stable, watermark-bound exchange truth capture."""

    open_orders: Any
    account: dict[str, Any]
    positions: list[dict[str, Any]]
    signature: tuple
    capture_started_ms: float
    account_floor: float
    positions_floor: float
    end_time_ms: float
    attempt: int

    @property
    def trade_watermark_ms(self) -> float:
        return self.end_time_ms

    def __getitem__(self, name: str) -> Any:
        # Keep the extracted full-reset component source-compatible while the
        # snapshot contract replaces its former anonymous dictionaries.
        return getattr(self, name)


class ExchangeSnapshotNormalizer:
    """Pure validation and canonicalization of remote account truth."""

    @staticmethod
    def finite_float(
        value: Any,
        field: str,
        *,
        nonnegative: bool = False,
    ) -> float:
        try:
            normalized = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"exchange snapshot {field} is not numeric"
            ) from exc
        if not math.isfinite(normalized):
            raise ValueError(f"exchange snapshot {field} is not finite")
        if nonnegative and normalized < 0.0:
            raise ValueError(f"exchange snapshot {field} is negative")
        return normalized

    def account(
        self,
        account: Any,
        *,
        require_initial_margin: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(account, dict):
            raise ValueError("remote account snapshot must be an object")
        normalized = dict(account)
        required_fields = ["totalWalletBalance"]
        if require_initial_margin:
            required_fields.append("totalInitialMargin")
        for field in required_fields:
            if field not in normalized or normalized[field] is None:
                raise ValueError(
                    f"remote account snapshot is missing {field}"
                )
        for field, nonnegative in (
            ("totalWalletBalance", False),
            ("totalInitialMargin", True),
        ):
            if normalized.get(field) is None:
                continue
            normalized[field] = self.finite_float(
                normalized[field],
                f"account.{field}",
                nonnegative=nonnegative,
            )
        for field, nonnegative in (
            ("availableBalance", False),
            ("totalMaintMargin", True),
            ("totalMarginBalance", False),
        ):
            if normalized.get(field) is None:
                continue
            normalized[field] = self.finite_float(
                normalized[field],
                f"account.{field}",
                nonnegative=nonnegative,
            )
        return normalized

    def account_balances(self, account: dict[str, Any]) -> dict[str, dict]:
        raw_assets = account.get("assets", []) or []
        if not isinstance(raw_assets, (list, tuple)):
            raise ValueError("remote account assets snapshot must be a list")

        balances = {}
        for index, entry in enumerate(raw_assets):
            if not isinstance(entry, dict):
                raise ValueError(
                    f"remote account asset {index} must be an object"
                )
            asset = str(entry.get("asset", "") or "").upper().strip()
            if not asset:
                raise ValueError(
                    f"remote account asset {index} is missing asset"
                )
            if asset in balances:
                raise ValueError(
                    "remote account assets snapshot contains duplicate "
                    f"{asset}"
                )
            available_balance = entry.get("availableBalance")
            balances[asset] = {
                "wallet_balance": self.finite_float(
                    entry.get("walletBalance", 0.0),
                    f"assets.{asset}.walletBalance",
                ),
                "available_balance": (
                    self.finite_float(
                        available_balance,
                        f"assets.{asset}.availableBalance",
                    )
                    if available_balance is not None
                    else None
                ),
            }
        return balances

    def positions(self, positions: Any) -> list[dict[str, Any]]:
        if not isinstance(positions, (list, tuple)):
            raise ValueError("remote positions snapshot must be a list")
        normalized = []
        seen_symbols = set()
        for index, position in enumerate(positions):
            if not isinstance(position, dict):
                raise ValueError(
                    f"remote position entry {index} must be an object"
                )
            symbol = str(position.get("symbol", "") or "").upper().strip()
            if not symbol:
                raise ValueError(
                    f"remote position entry {index} is missing symbol"
                )
            position_side = str(
                position.get("positionSide", "BOTH") or "BOTH"
            ).upper()
            if position_side != "BOTH":
                raise ValueError(
                    f"remote position {symbol} is not one-way/BOTH"
                )
            if symbol in seen_symbols:
                raise ValueError(
                    f"remote positions snapshot contains duplicate {symbol}"
                )
            seen_symbols.add(symbol)
            payload = dict(position)
            payload["symbol"] = symbol
            payload["positionAmt"] = self.finite_float(
                payload.get("positionAmt", 0.0),
                f"positions.{symbol}.positionAmt",
            )
            payload["entryPrice"] = self.finite_float(
                payload.get("entryPrice", 0.0),
                f"positions.{symbol}.entryPrice",
                nonnegative=True,
            )
            for field in (
                "unRealizedProfit",
                "isolatedWallet",
                "initialMargin",
                "maintMargin",
            ):
                if payload.get(field) is None:
                    continue
                payload[field] = self.finite_float(
                    payload[field],
                    f"positions.{symbol}.{field}",
                    nonnegative=field
                    in {
                        "isolatedWallet",
                        "initialMargin",
                        "maintMargin",
                    },
                )
            normalized.append(payload)
        return normalized

    @staticmethod
    def open_orders(remote_orders: Any) -> list[dict[str, Any]]:
        if not isinstance(remote_orders, (list, tuple)):
            raise ValueError("remote open-orders snapshot must be a list")
        normalized = []
        for order in remote_orders:
            if not isinstance(order, dict):
                raise ValueError("remote open-orders entry must be an object")
            symbol = str(order.get("symbol", "") or "").upper().strip()
            if not symbol:
                raise ValueError(
                    "remote open-orders entry is missing symbol"
                )
            identifiers = tuple(
                sorted(
                    oid
                    for oid in (
                        (
                            str(order.get("orderId"))
                            if order.get("orderId") is not None
                            else ""
                        ),
                        order.get("clientOrderId") or "",
                    )
                    if oid
                )
            )
            normalized.append(
                {
                    "symbol": symbol,
                    "identifiers": identifiers,
                    "side": str(order.get("side", "") or "").upper(),
                }
            )
        normalized.sort(
            key=lambda item: (
                item["symbol"],
                item["identifiers"],
                item["side"],
            )
        )
        return normalized

    @staticmethod
    def signature(
        account: dict[str, Any],
        positions: list[dict[str, Any]],
        normalized_orders: list[dict[str, Any]],
    ) -> tuple:
        normalized_positions = tuple(
            sorted(
                (
                    str(position.get("symbol", "") or ""),
                    float(position.get("positionAmt", 0.0) or 0.0),
                    float(position.get("entryPrice", 0.0) or 0.0),
                )
                for position in positions
                if position.get("symbol")
            )
        )
        order_signature = tuple(
            (
                item["symbol"],
                item["identifiers"],
                item["side"],
            )
            for item in normalized_orders
        )
        # Mark-to-market fields legitimately move while prices change. The
        # stability barrier covers structural truth and settled wallet value.
        account_signature = float(
            account.get("totalWalletBalance", 0.0) or 0.0
        )
        return normalized_positions, order_signature, account_signature


class StableExchangeSnapshotCollector:
    """Acquire consecutive matching exchange snapshots or fail closed."""

    def __init__(
        self,
        *,
        queries: ExchangeSnapshotQueries,
        policy: Callable[[], StableSnapshotPolicy],
        normalize_account: Callable[..., dict[str, Any]],
        normalize_positions: Callable[[Any], list[dict[str, Any]]],
        normalize_open_orders: Callable[[Any], list[dict[str, Any]]],
        snapshot_signature: Callable[[dict, list, Any], tuple],
        audit: Callable[..., None],
        now_ms: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self._queries = queries
        self._policy = policy
        self._normalize_account = normalize_account
        self._normalize_positions = normalize_positions
        self._normalize_open_orders = normalize_open_orders
        self._snapshot_signature = snapshot_signature
        self._audit = audit
        self._now_ms = now_ms
        self._sleep = sleep

    def capture(
        self,
        *,
        require_no_open_orders: bool = False,
        require_initial_margin: bool = True,
    ) -> ExchangeTruthSnapshot:
        policy = self._policy()
        previous_signature = None
        stable_count = 0
        last_payload = None

        for attempt in range(1, policy.max_attempts + 1):
            capture_started_ms = self._now_ms()
            account_floor = self._now_ms() / 1000.0
            open_orders = self._queries.open_orders()
            account = self._queries.account()
            positions_floor = self._now_ms() / 1000.0
            positions = self._queries.positions()
            snapshot_end_ms = self._now_ms()
            if open_orders is None or not account or positions is None:
                raise RuntimeError(
                    "API failed while acquiring stable exchange snapshot"
                )
            account = self._normalize_account(
                account,
                require_initial_margin=require_initial_margin,
            )
            positions = self._normalize_positions(positions)

            signature = self._snapshot_signature(
                account,
                positions,
                open_orders,
            )
            if signature == previous_signature:
                stable_count += 1
            else:
                stable_count = 1
                previous_signature = signature

            normalized_orders = self._normalize_open_orders(open_orders)
            last_payload = ExchangeTruthSnapshot(
                open_orders=open_orders,
                account=account,
                positions=positions,
                signature=signature,
                capture_started_ms=capture_started_ms,
                account_floor=account_floor,
                positions_floor=positions_floor,
                end_time_ms=snapshot_end_ms,
                attempt=attempt,
            )
            if (
                stable_count >= policy.stability_required
                and (not require_no_open_orders or not normalized_orders)
            ):
                self._audit(
                    "stable_snapshot_acquired",
                    attempts=attempt,
                    stable_count=stable_count,
                    end_time_ms=snapshot_end_ms,
                )
                return last_payload

            self._sleep(policy.settle_interval_sec)

        residual = (
            self._normalize_open_orders(last_payload.open_orders)
            if last_payload
            else []
        )
        raise RuntimeError(
            "exchange snapshot did not stabilize"
            + (f"; residual open orders={residual}" if residual else "")
        )
