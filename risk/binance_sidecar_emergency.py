"""Emergency exchange actions for the independent Binance risk sidecar."""

from __future__ import annotations

import os
import time
from typing import Protocol

from event.type import OrderRequest


class BinanceSidecarEmergencyOwner(Protocol):
    rest: object

    def _ensure_exchange_clock(self, force: bool = False): ...

    def _response_payload(self, response, expected_type, label: str): ...


class BinanceSidecarEmergencyActions:
    """Cancel and flatten through the sidecar's independent REST channel."""

    __slots__ = ("_owner",)

    def __init__(self, owner: BinanceSidecarEmergencyOwner):
        self._owner = owner

    def cancel(self, symbols, countdown_time_ms: int):
        owner = self._owner
        failures = []
        clock_ok, clock_reason = owner._ensure_exchange_clock(force=True)
        if not clock_ok:
            failures.append(f"clock:{clock_reason}")
        target_symbols = {
            str(symbol or "").upper()
            for symbol in symbols
            if str(symbol or "").strip()
        }
        try:
            response = owner.rest.get_open_orders(emergency=True)
            status_code = getattr(response, "status_code", None)
            if status_code != 200:
                failures.append(
                    f"account_open_orders_status={status_code}"
                )
            else:
                open_orders = response.json()
                if not isinstance(open_orders, list):
                    failures.append("account_open_orders_payload_invalid")
                else:
                    for index, order in enumerate(open_orders):
                        if not isinstance(order, dict):
                            failures.append(
                                "account_open_order_row_invalid:"
                                f"{index}"
                            )
                            continue
                        symbol = str(
                            order.get("symbol", "") or ""
                        ).upper()
                        if not symbol:
                            failures.append(
                                "account_open_order_symbol_missing:"
                                f"{index}"
                            )
                            continue
                        target_symbols.add(symbol)
        except Exception as exc:
            failures.append(
                "account_open_orders_exception:"
                f"{type(exc).__name__}:{exc}"
            )

        for symbol in sorted(target_symbols):
            try:
                countdown_response = owner.rest.set_countdown_cancel_all(
                    symbol,
                    countdown_time_ms,
                )
                if getattr(countdown_response, "status_code", None) != 200:
                    failures.append(
                        f"{symbol}:countdown_status="
                        f"{getattr(countdown_response, 'status_code', None)}"
                    )
            except Exception as exc:
                failures.append(
                    f"{symbol}:countdown_exception:"
                    f"{type(exc).__name__}:{exc}"
                )

            try:
                cancel_response = owner.rest.cancel_all_orders(symbol)
                if getattr(cancel_response, "status_code", None) != 200:
                    failures.append(
                        f"{symbol}:cancel_status="
                        f"{getattr(cancel_response, 'status_code', None)}"
                    )
            except Exception as exc:
                failures.append(
                    f"{symbol}:cancel_exception:{type(exc).__name__}:{exc}"
                )
        return not failures, ";".join(failures)

    def flatten(self):
        owner = self._owner
        clock_ok, clock_reason = owner._ensure_exchange_clock(force=True)
        ok, positions, reason = owner._response_payload(
            owner.rest.get_positions(emergency=True),
            list,
            "positions",
        )
        if not ok:
            return False, 0, reason

        failures = [] if clock_ok else [f"clock:{clock_reason}"]
        submitted = 0
        timestamp_fragment = int(time.time() * 1000) % 1_000_000_000
        for index, position in enumerate(positions):
            symbol = str(position.get("symbol", "") or "").upper()
            try:
                amount = float(position.get("positionAmt", 0.0) or 0.0)
            except (TypeError, ValueError):
                failures.append(f"{symbol or 'unknown'}:invalid_position")
                continue
            if not symbol or abs(amount) <= 1e-9:
                continue
            side = "SELL" if amount > 0.0 else "BUY"
            request = OrderRequest(
                symbol=symbol,
                price=0.0,
                volume=abs(amount),
                side=side,
                order_type="MARKET",
                time_in_force="IOC",
                reduce_only=True,
            )
            client_oid = (
                f"crsk-{os.getpid()}-{timestamp_fragment}-{index}"
            )[:36]
            try:
                response = owner.rest.new_order(request, client_oid)
                if getattr(response, "status_code", None) != 200:
                    failures.append(
                        f"{symbol}:flatten_status="
                        f"{getattr(response, 'status_code', None)}"
                    )
                    continue
                submitted += 1
            except Exception as exc:
                failures.append(
                    f"{symbol}:flatten_exception:"
                    f"{type(exc).__name__}:{exc}"
                )
        return not failures, submitted, ";".join(failures)
