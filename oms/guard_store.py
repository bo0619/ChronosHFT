"""Encapsulated strategy guard state for the OMS."""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GuardStoreSnapshot:
    """Immutable, deterministic view of strategy guard state."""

    strategy_guards: tuple[tuple[str, str], ...]
    strategy_symbol_guards: tuple[tuple[str, str, str], ...]

    @property
    def strategy_guard_count(self) -> int:
        return len(self.strategy_guards)

    @property
    def strategy_symbol_guard_count(self) -> int:
        return len(self.strategy_symbol_guards)


class GuardStore:
    """Own global and symbol-scoped strategy guards behind a narrow API."""

    __slots__ = (
        "__lock",
        "__strategy_guards",
        "__strategy_symbol_guards",
    )

    def __init__(self) -> None:
        self.__lock = threading.RLock()
        self.__strategy_guards: dict[str, str] = {}
        self.__strategy_symbol_guards: dict[tuple[str, str], str] = {}

    @staticmethod
    def _strategy_id(value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("strategy guard strategy_id must be a string")
        strategy_id = value.strip()
        if not strategy_id:
            raise ValueError("strategy guard strategy_id must not be empty")
        if strategy_id != value or "|" in strategy_id:
            raise ValueError(
                "strategy guard strategy_id must be canonical and must not contain '|'"
            )
        return strategy_id

    @staticmethod
    def _symbol(value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("strategy guard symbol must be a string")
        symbol = value.strip().upper()
        if not symbol:
            raise ValueError("strategy guard symbol must not be empty")
        if symbol != value or "|" in symbol:
            raise ValueError(
                "strategy guard symbol must be canonical and must not contain '|'"
            )
        return symbol

    @staticmethod
    def _reason(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("strategy guard reason must be a non-empty string")
        return value

    def freeze(
        self,
        strategy_id: str,
        reason: str,
        *,
        symbol: str = "",
    ) -> str:
        """Install one guard and return the reason it replaced, if any."""

        strategy_id = self._strategy_id(strategy_id)
        reason = self._reason(reason)
        symbol = self._symbol(symbol) if symbol else ""
        with self.__lock:
            if symbol:
                key = (strategy_id, symbol)
                previous_reason = self.__strategy_symbol_guards.get(key, "")
                self.__strategy_symbol_guards[key] = reason
                return previous_reason
            previous_reason = self.__strategy_guards.get(strategy_id, "")
            self.__strategy_guards[strategy_id] = reason
            return previous_reason

    def clear(
        self,
        strategy_id: str,
        *,
        symbol: str = "",
        expected_reason: str | None = None,
    ) -> str:
        """Clear one guard, optionally only when its reason still matches."""

        strategy_id = self._strategy_id(strategy_id)
        symbol = self._symbol(symbol) if symbol else ""
        if expected_reason is not None:
            expected_reason = self._reason(expected_reason)
        with self.__lock:
            if symbol:
                guards = self.__strategy_symbol_guards
                key: str | tuple[str, str] = (strategy_id, symbol)
            else:
                guards = self.__strategy_guards
                key = strategy_id
            previous_reason = guards.get(key, "")
            if (
                not previous_reason
                or expected_reason is not None
                and previous_reason != expected_reason
            ):
                return ""
            del guards[key]
            return previous_reason

    def reason(self, strategy_id: str, *, symbol: str = "") -> str:
        """Return the scoped reason, falling back to the global guard."""

        strategy_id = self._strategy_id(strategy_id)
        symbol = self._symbol(symbol) if symbol else ""
        with self.__lock:
            if symbol:
                scoped = self.__strategy_symbol_guards.get(
                    (strategy_id, symbol),
                    "",
                )
                if scoped:
                    return scoped
            return self.__strategy_guards.get(strategy_id, "")

    def has_active(self) -> bool:
        with self.__lock:
            return bool(
                self.__strategy_guards or self.__strategy_symbol_guards
            )

    def snapshot(self) -> GuardStoreSnapshot:
        with self.__lock:
            return GuardStoreSnapshot(
                strategy_guards=tuple(sorted(self.__strategy_guards.items())),
                strategy_symbol_guards=tuple(
                    (strategy_id, symbol, reason)
                    for (strategy_id, symbol), reason in sorted(
                        self.__strategy_symbol_guards.items()
                    )
                ),
            )

    def restore(
        self,
        strategy_guards: object,
        strategy_symbol_guards: object,
    ) -> None:
        """Atomically restore a strict canonical checkpoint payload."""

        if not isinstance(strategy_guards, dict):
            raise ValueError("strategy_guards checkpoint field must be an object")
        if not isinstance(strategy_symbol_guards, dict):
            raise ValueError(
                "strategy_symbol_guards checkpoint field must be an object"
            )

        restored_global: dict[str, str] = {}
        for raw_strategy_id, raw_reason in strategy_guards.items():
            strategy_id = self._strategy_id(raw_strategy_id)
            restored_global[strategy_id] = self._reason(raw_reason)

        restored_symbol: dict[tuple[str, str], str] = {}
        for raw_key, raw_reason in strategy_symbol_guards.items():
            if not isinstance(raw_key, str) or raw_key.count("|") != 1:
                raise ValueError(
                    "strategy_symbol_guards keys must use 'strategy_id|SYMBOL'"
                )
            raw_strategy_id, raw_symbol = raw_key.split("|", 1)
            key = (
                self._strategy_id(raw_strategy_id),
                self._symbol(raw_symbol),
            )
            restored_symbol[key] = self._reason(raw_reason)

        with self.__lock:
            self.__strategy_guards = restored_global
            self.__strategy_symbol_guards = restored_symbol

    def checkpoint_payload(self) -> dict[str, dict[str, str]]:
        """Return the established, JSON-safe journal wire representation."""

        snapshot = self.snapshot()
        return {
            "strategy_guards": dict(snapshot.strategy_guards),
            "strategy_symbol_guards": {
                f"{strategy_id}|{symbol}": reason
                for strategy_id, symbol, reason in (
                    snapshot.strategy_symbol_guards
                )
            },
        }
