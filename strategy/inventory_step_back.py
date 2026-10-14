"""Paper inventory control in whole ticks for large-tick books.

GLFT skews quotes away from inventory by a few bps per lot. On KORUUSDT one
tick is ~7 bps, so below about four lots the skew rounds away, and touch
snapping (``strategy.touch_snap``) put the adding side straight back on the
touch: at the 2026-10-14 KRX open the bid kept filling at full size as the
price fell, and inventory reached 170 KORU (~4.9 lots) within two minutes.

Here the side that adds inventory steps back one tick per ``lots_per_tick``
lots of inventory, up to ``max_ticks``, after snapping. The side that
reduces inventory is left at the touch. Lots are the strategy's inventory
lot (the quote notional), so the rule scales to any symbol's price and
tick. Every setting can be overridden per symbol under ``symbols.<SYMBOL>``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace

# Tolerance for whole-lot boundaries.
_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class InventoryStepBackConfig:
    enabled: bool = False
    lots_per_tick: float = 1.0
    max_ticks: int = 3

    @classmethod
    def for_symbol(cls, section: object, symbol: str) -> "InventoryStepBackConfig":
        """Global settings, then ``symbols.<SYMBOL>`` overrides."""
        section = section if isinstance(section, Mapping) else {}
        merged = {key: value for key, value in section.items() if key != "symbols"}
        overrides = section.get("symbols")
        if isinstance(overrides, Mapping):
            symbol_section = overrides.get(str(symbol or "").upper())
            if isinstance(symbol_section, Mapping):
                merged.update(symbol_section)
        known = {item.name for item in fields(cls)}
        config = replace(
            cls(),
            **{key: value for key, value in merged.items() if key in known},
        )
        lots = float(config.lots_per_tick)
        if not math.isfinite(lots) or lots <= 0.0:
            raise ValueError("inventory_step_back.lots_per_tick must be positive")
        if int(config.max_ticks) < 1:
            raise ValueError("inventory_step_back.max_ticks must be at least 1")
        return config

    def ticks_back(self, inventory_lots: float) -> int:
        if not self.enabled or not math.isfinite(inventory_lots):
            return 0
        steps = math.floor(abs(inventory_lots) / self.lots_per_tick + _EPSILON)
        return min(int(self.max_ticks), max(0, steps))


def step_back_against_inventory(
    target_bid: float,
    target_ask: float,
    *,
    config: InventoryStepBackConfig,
    inventory_lots: float,
    best_bid: float,
    best_ask: float,
    tick_size: float,
) -> tuple[float, float]:
    """Hold the inventory-adding side ``ticks_back`` levels behind the touch."""
    ticks = config.ticks_back(inventory_lots)
    if ticks == 0 or tick_size <= 0.0:
        return target_bid, target_ask
    if inventory_lots > 0.0:
        target_bid = min(target_bid, best_bid - ticks * tick_size)
    else:
        target_ask = max(target_ask, best_ask + ticks * tick_size)
    return target_bid, target_ask


__all__ = ["InventoryStepBackConfig", "step_back_against_inventory"]
