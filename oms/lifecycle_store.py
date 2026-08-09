"""Encapsulated operator-visible OMS lifecycle state."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from event.type import LifecycleState


@dataclass(frozen=True, slots=True)
class LifecycleSnapshot:
    """Immutable, internally consistent lifecycle view."""

    state: LifecycleState
    generation: int
    manual_rearm_required: bool
    last_freeze_reason: str
    last_halt_reason: str


_UNCHANGED = object()


class LifecycleStore:
    """Own lifecycle transitions behind a narrow, validated API."""

    __slots__ = (
        "__generation",
        "__last_freeze_reason",
        "__last_halt_reason",
        "__lock",
        "__manual_rearm_required",
        "__state",
    )

    def __init__(self) -> None:
        self.__lock = threading.RLock()
        self.__state = LifecycleState.BOOTSTRAP
        self.__generation = 0
        self.__manual_rearm_required = False
        self.__last_freeze_reason = ""
        self.__last_halt_reason = ""

    @staticmethod
    def _validate_state(value: object) -> LifecycleState:
        if not isinstance(value, LifecycleState):
            raise TypeError("lifecycle state must be a LifecycleState")
        return value

    @staticmethod
    def _validate_generation(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("lifecycle generation must be a non-negative integer")
        return value

    @staticmethod
    def _validate_rearm(value: object) -> bool:
        if not isinstance(value, bool):
            raise TypeError("manual_rearm_required must be a boolean")
        return value

    @staticmethod
    def _validate_reason(value: object, field: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{field} must be a string")
        return value

    def snapshot(self) -> LifecycleSnapshot:
        with self.__lock:
            return LifecycleSnapshot(
                state=self.__state,
                generation=self.__generation,
                manual_rearm_required=self.__manual_rearm_required,
                last_freeze_reason=self.__last_freeze_reason,
                last_halt_reason=self.__last_halt_reason,
            )

    def transition(
        self,
        state: LifecycleState,
        *,
        increment_generation: bool = False,
        manual_rearm_required: bool | object = _UNCHANGED,
        last_freeze_reason: str | object = _UNCHANGED,
        last_halt_reason: str | object = _UNCHANGED,
    ) -> LifecycleSnapshot:
        """Apply one validated transition and return the previous snapshot."""

        state = self._validate_state(state)
        if not isinstance(increment_generation, bool):
            raise TypeError("increment_generation must be a boolean")
        if manual_rearm_required is not _UNCHANGED:
            manual_rearm_required = self._validate_rearm(
                manual_rearm_required
            )
        if last_freeze_reason is not _UNCHANGED:
            last_freeze_reason = self._validate_reason(
                last_freeze_reason,
                "last_freeze_reason",
            )
        if last_halt_reason is not _UNCHANGED:
            last_halt_reason = self._validate_reason(
                last_halt_reason,
                "last_halt_reason",
            )

        with self.__lock:
            previous = self.snapshot()
            self.__state = state
            if increment_generation:
                self.__generation += 1
            if manual_rearm_required is not _UNCHANGED:
                self.__manual_rearm_required = manual_rearm_required
            if last_freeze_reason is not _UNCHANGED:
                self.__last_freeze_reason = last_freeze_reason
            if last_halt_reason is not _UNCHANGED:
                self.__last_halt_reason = last_halt_reason
            return previous

    @property
    def state(self) -> LifecycleState:
        with self.__lock:
            return self.__state

    @state.setter
    def state(self, value: LifecycleState) -> None:
        value = self._validate_state(value)
        with self.__lock:
            self.__state = value

    @property
    def generation(self) -> int:
        with self.__lock:
            return self.__generation

    @generation.setter
    def generation(self, value: int) -> None:
        value = self._validate_generation(value)
        with self.__lock:
            self.__generation = value

    @property
    def manual_rearm_required(self) -> bool:
        with self.__lock:
            return self.__manual_rearm_required

    @manual_rearm_required.setter
    def manual_rearm_required(self, value: bool) -> None:
        value = self._validate_rearm(value)
        with self.__lock:
            self.__manual_rearm_required = value

    @property
    def last_freeze_reason(self) -> str:
        with self.__lock:
            return self.__last_freeze_reason

    @last_freeze_reason.setter
    def last_freeze_reason(self, value: str) -> None:
        value = self._validate_reason(value, "last_freeze_reason")
        with self.__lock:
            self.__last_freeze_reason = value

    @property
    def last_halt_reason(self) -> str:
        with self.__lock:
            return self.__last_halt_reason

    @last_halt_reason.setter
    def last_halt_reason(self, value: str) -> None:
        value = self._validate_reason(value, "last_halt_reason")
        with self.__lock:
            self.__last_halt_reason = value
