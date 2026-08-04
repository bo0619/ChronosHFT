"""Binance REST command and query adaptation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from event.type import (
    CommandOutcome,
    GatewayCommandResult,
    GatewayState,
    OrderRequest,
)


@dataclass(frozen=True)
class BinanceRestGatewayDependencies:
    rest: object
    transport_lock: object
    is_transport_active: Callable[[], bool]
    gateway_state: Callable[[], GatewayState]
    symbol_ready_locked: Callable[[str], bool]
    require_healthy_clock: Callable[[], bool]
    clock_health_snapshot: Callable[..., dict]
    log_info: Callable[[str], None]


class BinanceRestGateway:
    """Translate domain commands and REST responses without transport state."""

    AMBIGUOUS_ORDER_CODES = frozenset(
        {
            "-1000",
            "-1001",
            "-1003",
            "-1006",
            "-1007",
            "-1008",
            "-4111",
            "-4116",
        }
    )

    def __init__(self, dependencies: BinanceRestGatewayDependencies) -> None:
        self.dependencies = dependencies

    def send_order(
        self,
        req: OrderRequest,
        client_oid: str | None = None,
        *,
        pre_send_guard=None,
    ) -> GatewayCommandResult:
        if not req.reduce_only:
            symbol = str(req.symbol or "").upper()
            with self.dependencies.transport_lock:
                book_unavailable = bool(
                    not self.dependencies.is_transport_active()
                    or self.dependencies.gateway_state()
                    != GatewayState.READY
                    or not self.dependencies.symbol_ready_locked(symbol)
                )
            if book_unavailable:
                return GatewayCommandResult(
                    CommandOutcome.REJECTED,
                    error_code="ORDERBOOK_NOT_READY",
                    error_message=(
                        f"{symbol} order book is not owned by a READY gateway"
                    ),
                )
        if (
            not req.reduce_only
            and self.dependencies.require_healthy_clock()
        ):
            clock_ok, error_code, error_message = self.clock_health_guard()
            if not clock_ok:
                return GatewayCommandResult(
                    CommandOutcome.REJECTED,
                    error_code=error_code,
                    error_message=error_message,
                )

        response = self.dependencies.rest.new_order(
            req,
            client_oid,
            pre_send_guard=pre_send_guard,
        )
        if response and response.status_code == 200:
            try:
                data = response.json()
                exchange_oid = str(data["orderId"])
            except Exception as exc:
                return GatewayCommandResult(
                    CommandOutcome.UNKNOWN,
                    error_message=f"invalid order acknowledgement: {exc}",
                    response=response,
                )
            self._log_acknowledged_order(req, client_oid)
            return GatewayCommandResult(
                CommandOutcome.ACKNOWLEDGED,
                exchange_oid=exchange_oid,
                response=response,
            )

        error_code, error_message = self.response_error(response)
        ambiguous = bool(
            response is None
            or getattr(response, "status_code", 0) >= 500
            or getattr(response, "status_code", 0) in {408, 418, 429}
            or error_code in self.AMBIGUOUS_ORDER_CODES
        )
        return GatewayCommandResult(
            CommandOutcome.UNKNOWN if ambiguous else CommandOutcome.REJECTED,
            error_code=error_code,
            error_message=error_message,
            response=response,
        )

    def clock_health_guard(self):
        try:
            clock_health = self.dependencies.clock_health_snapshot(
                notify_listeners=False
            )
        except Exception as exc:
            return (
                False,
                "CLOCK_HEALTH_UNAVAILABLE",
                f"{type(exc).__name__}:{exc}",
            )
        if not bool(clock_health.get("ready", False)):
            return (
                False,
                "CLOCK_UNHEALTHY",
                str(
                    clock_health.get(
                        "reason",
                        "exchange clock unavailable",
                    )
                ),
            )
        return True, "", ""

    def cancel_order(self, request):
        return self.dependencies.rest.cancel_order(request)

    def cancel_all_orders(self, symbol: str):
        return self.dependencies.rest.cancel_all_orders(symbol)

    def set_countdown_cancel_all(
        self,
        symbol: str,
        countdown_time_ms: int,
    ):
        return self.dependencies.rest.set_countdown_cancel_all(
            symbol,
            countdown_time_ms,
        )

    def get_account_info(self):
        response = self.dependencies.rest.get_account()
        return self._successful_json(response)

    def get_all_positions(self, *, emergency: bool = False):
        response = self.dependencies.rest.get_positions(emergency=emergency)
        return self._successful_json(response)

    def get_open_orders(self, *, emergency: bool = False):
        response = self.dependencies.rest.get_open_orders(emergency=emergency)
        return self._successful_json(response)

    def get_order(self, symbol: str, order_id: str):
        response = self.dependencies.rest.query_order(symbol, order_id)
        if response and response.status_code == 200:
            return response.json()
        error_code, error_message = self.response_error(response)
        if error_code in {"-2011", "-2013"}:
            return {
                "_query_status": "NOT_FOUND",
                "code": error_code,
                "msg": error_message,
            }
        return None

    def get_all_orders(self, symbol: str, **kwargs):
        response = self.dependencies.rest.get_all_orders(symbol, **kwargs)
        return self._successful_json(response)

    def get_user_trades(self, symbol: str, **kwargs):
        response = self.dependencies.rest.get_user_trades(symbol, **kwargs)
        return self._successful_json(response)

    def get_income_history(self, **kwargs):
        response = self.dependencies.rest.get_income_history(**kwargs)
        return self._successful_json(response)

    def get_depth_snapshot(self, symbol: str):
        return self.dependencies.rest.get_depth_snapshot(symbol)

    def get_rpi_depth(self, symbol: str, limit: int = 1000):
        return self.dependencies.rest.get_rpi_depth(symbol, limit=limit)

    def get_commission_rate(self, symbol: str):
        response = self.dependencies.rest.get_commission_rate(symbol)
        return self._successful_json(response)

    @staticmethod
    def response_error(response):
        if response is None:
            return "", "transport response unavailable"
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            return "", ""
        raw_code = payload.get("code")
        code = "" if raw_code is None else str(raw_code)
        return code, str(payload.get("msg", "") or "")

    @staticmethod
    def _successful_json(response):
        return (
            response.json()
            if response and response.status_code == 200
            else None
        )

    def _log_acknowledged_order(
        self,
        request: OrderRequest,
        client_oid: str | None,
    ) -> None:
        symbol = request.symbol.replace("USDC", "").replace(
            "USDT",
            "",
        ).lower()
        side = "long" if request.side == "BUY" else "short"
        if client_oid and client_oid.startswith("EXIT_"):
            action = "exit "
        elif client_oid and client_oid.startswith("ENTRY_"):
            action = "enter"
        elif client_oid and client_oid.startswith("EMERGENCY_"):
            action = "flatten"
        else:
            action = "order"
        if request.reduce_only:
            action = f"{action} reduce-only"
        self.dependencies.log_info(
            f"{symbol} {action} {side} @ {request.price:.6g}"
            f"  ({request.time_in_force}, vol={request.volume})"
        )
