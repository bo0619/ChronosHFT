"""Stable contracts between strategies, execution, and their runtime."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from event.type import (
    AccountData,
    AggTradeData,
    OrderBook,
    OrderIntent,
    OrderStateSnapshot,
    PositionData,
    TradeData,
)


def _enum_value(value: Any) -> str:
    rendered = getattr(value, "value", value)
    return str(rendered or "")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


def _copy_mapping(value: Any) -> dict:
    if not isinstance(value, Mapping):
        return {}
    for _attempt in range(3):
        try:
            return dict(value)
        except RuntimeError:
            continue
    raise RuntimeError("strategy state changed while a snapshot was captured")


@dataclass(frozen=True, slots=True)
class StrategyStateSnapshot:
    """Immutable execution state exposed to a strategy decision cycle."""

    lifecycle_state: str
    capability_mode: str
    capability_reason: str
    positions: Mapping[str, float]
    strategy_positions: Mapping[str, float]
    account_equity: float
    account_used_margin: float
    outbound_gate: Mapping[str, Any]

    def position(self, symbol: str, default: float = 0.0) -> float:
        return float(self.positions.get(str(symbol or "").upper(), default))

    def strategy_position(
        self,
        symbol: str,
        default: float | None = None,
    ) -> float | None:
        value = self.strategy_positions.get(str(symbol or "").upper())
        return default if value is None else float(value)


@runtime_checkable
class StrategyExecutionPort(Protocol):
    """Only execution capabilities available to production strategies."""

    @property
    def supports_strategy_evidence(self) -> bool: ...

    def state_snapshot(self, strategy_id: str) -> StrategyStateSnapshot: ...

    def can_submit(self, strategy_id: str, symbol: str = "") -> bool: ...

    def adapt_intent(self, intent: OrderIntent) -> tuple[OrderIntent, str]: ...

    def submit(self, intent: OrderIntent): ...

    def cancel(self, client_oid: str) -> bool: ...

    def cancel_all(self, symbol: str) -> bool: ...

    def record_strategy_evidence(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        symbol: str,
    ): ...

    def record_paper_markout(self, payload: Mapping[str, Any]): ...

    def enforce_rpi_calibration_runtime_limits(self): ...

    def expire_rpi_calibration_permit(self, reason: str): ...


class OMSStrategyExecutionAdapter:
    """Translate the narrow strategy port to the current OMS facade.

    This is the sole compatibility boundary allowed to inspect current OMS
    state owners. Strategies receive copied primitives and cannot retain a
    mutable account, exposure, gate, or journal object.
    """

    __strategy_execution_port__ = True

    def __init__(self, oms: Any):
        if oms is None:
            raise TypeError("strategy execution requires an OMS or execution port")
        self._oms = oms

    @property
    def supports_strategy_evidence(self) -> bool:
        return callable(getattr(self._oms, "record_strategy_evidence", None))

    def state_snapshot(self, strategy_id: str) -> StrategyStateSnapshot:
        exposure = getattr(self._oms, "exposure", None)
        raw_positions = _copy_mapping(
            getattr(exposure, "net_positions", {})
        )
        raw_strategy_positions = _copy_mapping(
            getattr(exposure, "strategy_net_positions", {})
        )
        strategy_id = str(strategy_id or "")
        positions = {
            str(symbol or "").upper(): float(quantity or 0.0)
            for symbol, quantity in raw_positions.items()
        }
        strategy_positions = {
            str(symbol or "").upper(): float(quantity or 0.0)
            for key, quantity in raw_strategy_positions.items()
            if isinstance(key, tuple)
            and len(key) == 2
            and str(key[0]) == strategy_id
            for symbol in (key[1],)
        }

        account = getattr(self._oms, "account", None)
        gate_reader = getattr(self._oms, "get_outbound_gate_snapshot", None)
        raw_gate = gate_reader() if callable(gate_reader) else {}
        return StrategyStateSnapshot(
            lifecycle_state=_enum_value(getattr(self._oms, "state", "")),
            capability_mode=_enum_value(
                getattr(self._oms, "capability_mode", "")
            ),
            capability_reason=str(
                getattr(self._oms, "capability_reason", "") or ""
            ),
            positions=_freeze(positions),
            strategy_positions=_freeze(strategy_positions),
            account_equity=float(getattr(account, "equity", 0.0) or 0.0),
            account_used_margin=float(
                getattr(account, "used_margin", 0.0) or 0.0
            ),
            outbound_gate=_freeze(_copy_mapping(raw_gate)),
        )

    def can_submit(self, strategy_id: str, symbol: str = "") -> bool:
        guarded = getattr(self._oms, "can_submit_for_strategy", None)
        if callable(guarded):
            return bool(guarded(strategy_id, symbol))
        symbol_guard = getattr(self._oms, "is_symbol_tradeable", None)
        if symbol and callable(symbol_guard):
            return bool(symbol_guard(symbol))
        return _enum_value(getattr(self._oms, "state", "")) == "LIVE"

    def adapt_intent(self, intent: OrderIntent) -> tuple[OrderIntent, str]:
        adapter = getattr(self._oms, "adapt_intent_for_trading_mode", None)
        if not callable(adapter):
            return intent, ""
        adapted, reason = adapter(intent)
        return adapted, str(reason or "")

    def submit(self, intent: OrderIntent):
        return self._oms.submit_order(intent)

    def cancel(self, client_oid: str) -> bool:
        return bool(self._oms.cancel_order(client_oid))

    def cancel_all(self, symbol: str) -> bool:
        return bool(self._oms.cancel_all_orders(symbol))

    def record_strategy_evidence(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        symbol: str,
    ):
        recorder = getattr(self._oms, "record_strategy_evidence", None)
        if not callable(recorder):
            raise RuntimeError("durable strategy evidence is unavailable")
        return recorder(kind, dict(payload), symbol=symbol)

    def record_paper_markout(self, payload: Mapping[str, Any]):
        recorder = getattr(self._oms, "record_paper_markout", None)
        if not callable(recorder):
            return False
        return recorder(dict(payload))

    def enforce_rpi_calibration_runtime_limits(self):
        enforce = getattr(
            self._oms,
            "enforce_rpi_calibration_runtime_limits",
            None,
        )
        if not callable(enforce):
            raise RuntimeError("RPI calibration runtime guard is unavailable")
        return enforce()

    def expire_rpi_calibration_permit(self, reason: str):
        expire = getattr(self._oms, "expire_rpi_calibration_permit", None)
        if not callable(expire):
            raise RuntimeError("RPI calibration permit transition is unavailable")
        return expire(reason)


def coerce_strategy_execution_port(value: Any) -> StrategyExecutionPort:
    if getattr(value, "__strategy_execution_port__", False):
        return value
    if isinstance(value, StrategyExecutionPort):
        return value
    return OMSStrategyExecutionAdapter(value)


@runtime_checkable
class StrategyRuntimeContract(Protocol):
    """Event surface consumed by :class:`strategy.runtime.StrategyRuntime`."""

    name: str

    def on_orderbook(self, orderbook: OrderBook): ...

    def on_market_trade(self, trade: AggTradeData): ...

    def on_order(self, snapshot: OrderStateSnapshot): ...

    def on_trade(self, trade: TradeData): ...

    def on_position(self, position: PositionData): ...

    def on_account_update(self, account: AccountData): ...

    def on_system_health(self, message): ...


__all__ = [
    "OMSStrategyExecutionAdapter",
    "StrategyExecutionPort",
    "StrategyRuntimeContract",
    "StrategyStateSnapshot",
    "coerce_strategy_execution_port",
]
