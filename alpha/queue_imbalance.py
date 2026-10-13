"""Per-symbol L1 queue imbalance as a next-price-move predictor.

On a large-tick book the next mid change is almost always one tick, and its
direction is well predicted by the L1 queue imbalance
``I = (bid_qty - ask_qty) / (bid_qty + ask_qty)`` (Gould and Bonart, "Queue
imbalance as a one-tick-ahead price predictor", 2016). On 9 hours of
KORUUSDT, ``I < -0.4`` was followed by a down move 75-91% of the time and
``I > 0.2`` by an up move 70-81%. A bid resting at the touch before a down
move is filled by the move itself (an adverse through-fill), so the side
the imbalance points against should step back a level.

The conditional probabilities are learned online per symbol from the public
book, not from our fills, so quoting decisions cannot feed back into them:

* every book update adds its imbalance bin to the current spell;
* when the mid changes, each update in the spell is one observation of the
  move direction for its bin. Quotes are decided at arbitrary updates, so
  the probability is per update (weighting each move once instead
  overstates predictability: the updates just before a move are the most
  extreme). Updates in one spell share an outcome, so the evidence needed
  before a bin is used is counted in moves: the spell's share of each bin;
* all counts decay so that ``half_life_moves`` moves halve old evidence;
* ``against_probability`` is the Laplace-smoothed probability that the next
  move goes against a resting quote on the given side, or ``None`` while
  the bin has fewer than ``min_moves_per_bin`` moves of evidence.

Every setting can be overridden per symbol under ``symbols.<SYMBOL>``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace

from event.type import Side


@dataclass(frozen=True, slots=True)
class QueueImbalanceConfig:
    enabled: bool = False
    against_threshold: float = 0.65
    bins: int = 10
    min_moves_per_bin: float = 30.0
    half_life_moves: float = 2000.0

    @classmethod
    def for_symbol(cls, section: object, symbol: str) -> "QueueImbalanceConfig":
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
        if not 0.5 <= float(config.against_threshold) < 1.0:
            raise ValueError("queue_imbalance.against_threshold must be in [0.5, 1)")
        if int(config.bins) < 2:
            raise ValueError("queue_imbalance.bins must be at least 2")
        for name in ("min_moves_per_bin", "half_life_moves"):
            value = float(getattr(config, name))
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"queue_imbalance.{name} must be positive")
        return config


def queue_imbalance(bid_qty: float, ask_qty: float) -> float | None:
    total = bid_qty + ask_qty
    if not (bid_qty >= 0.0 and ask_qty >= 0.0 and total > 0.0):
        return None
    return (bid_qty - ask_qty) / total


class QueueImbalanceModel:
    """Online P(next mid move up | imbalance bin) for one symbol."""

    def __init__(self, config: QueueImbalanceConfig) -> None:
        self.config = config
        bins = int(config.bins)
        self._up = [0.0] * bins
        self._down = [0.0] * bins
        self._moves_evidence = [0.0] * bins
        self._spell = [0] * bins
        self._decay = 0.5 ** (1.0 / float(config.half_life_moves))
        self._last_mid: float | None = None
        self.last_bid: float | None = None
        self.last_ask: float | None = None
        self.imbalance: float | None = None
        self.moves = 0

    def _bin(self, imbalance: float) -> int:
        bins = len(self._up)
        return min(bins - 1, max(0, int((imbalance + 1.0) / 2.0 * bins)))

    def on_book(self, bid: float, bid_qty: float, ask: float, ask_qty: float) -> None:
        if not (bid > 0.0 and ask > bid):
            return
        mid = (bid + ask) / 2.0
        if self._last_mid is not None and mid != self._last_mid:
            self._record_move(up=mid > self._last_mid)
        self._last_mid = mid
        self.last_bid, self.last_ask = bid, ask
        self.imbalance = queue_imbalance(bid_qty, ask_qty)
        if self.imbalance is not None:
            self._spell[self._bin(self.imbalance)] += 1

    def _record_move(self, *, up: bool) -> None:
        total = sum(self._spell)
        decay = self._decay
        for index, count in enumerate(self._spell):
            self._up[index] *= decay
            self._down[index] *= decay
            self._moves_evidence[index] *= decay
            if count:
                if up:
                    self._up[index] += count
                else:
                    self._down[index] += count
                self._moves_evidence[index] += count / total
            self._spell[index] = 0
        self.moves += 1

    def probability_up(self, imbalance: float) -> float | None:
        index = self._bin(imbalance)
        up, down = self._up[index], self._down[index]
        if self._moves_evidence[index] < self.config.min_moves_per_bin:
            return None
        return (up + 1.0) / (up + down + 2.0)

    def against_probability(self, side: Side, imbalance: float | None = None) -> float | None:
        """P(the next move goes against a resting quote on ``side``)."""
        value = self.imbalance if imbalance is None else imbalance
        if value is None:
            return None
        up = self.probability_up(value)
        if up is None:
            return None
        return 1.0 - up if side == Side.BUY else up

    def against(self, side: Side) -> bool:
        probability = self.against_probability(side)
        return probability is not None and probability > self.config.against_threshold

    def table(self) -> list[dict[str, float]]:
        bins = len(self._up)
        return [
            {
                "imbalance_from": -1.0 + 2.0 * index / bins,
                "imbalance_to": -1.0 + 2.0 * (index + 1) / bins,
                "moves": self._moves_evidence[index],
                "p_up": (self._up[index] + 1.0)
                / (self._up[index] + self._down[index] + 2.0),
            }
            for index in range(bins)
        ]


__all__ = [
    "QueueImbalanceConfig",
    "QueueImbalanceModel",
    "queue_imbalance",
]
