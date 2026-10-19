"""Quoting rules for a GRVT maker priced off Binance.

Shared by the replay (``scripts/replay_cross_venue.py``) and the live
runner (``scripts/run_grvt_maker.py``), so what is traded is what was
tested.

* Fair value is the Binance mid times the GRVT/Binance basis, an
  exponential average (half-life ``basis_half_life`` seconds) of
  ``GRVT mid / Binance mid`` updated on each GRVT book, so a standing price
  difference between the venues does not read as a signal.
* Each side quotes ``half_spread_bps`` from fair, rounded away from fair to
  the tick. Without ``improve`` a quote is capped at the GRVT touch; with
  it a quote may sit inside the spread, up to one tick from the far touch
  (post-only, it never crosses what we last saw).
* Inventory, in lots of ``notional``: fair shifts against the position by
  ``skew_bps_per_lot`` per lot, and at ``max_inventory_lots`` the side that
  would add is pulled.
* Optionally the half spread widens with recent Binance volatility:
  ``half_spread_bps + vol_spread_coef * vol_bps``, where ``vol_bps`` is the
  realized move of the Binance mid over about ``vol_window_sec`` seconds
  (:class:`VolTracker`). ``vol_spread_coef`` 0 (the default) leaves it fixed.
* A resting quote more aggressive than its target is replaced at once; one
  behind it by more than ``keep_ticks`` ticks after ``min_rest_sec``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

EPS = 1e-12
SIDES = ("BUY", "SELL")


@dataclass(frozen=True, slots=True)
class QuoteParams:
    notional: float = 100.0
    half_spread_bps: float = 0.25
    skew_bps_per_lot: float = 1.0
    max_inventory_lots: float = 5.0
    improve: bool = True
    keep_ticks: float = 1.0
    min_rest_sec: float = 1.0
    basis_half_life: float = 60.0
    fair_shrink: float = 1.0
    vol_spread_coef: float = 0.0
    vol_window_sec: float = 10.0


def round_down(price: float, tick: float) -> float:
    return round(math.floor(price / tick + 1e-9) * tick, 10)


def round_up(price: float, tick: float) -> float:
    return round(math.ceil(price / tick - 1e-9) * tick, 10)


class BasisTracker:
    """Exponential average of ``log(GRVT mid / Binance mid)`` in time."""

    def __init__(self, half_life_sec: float):
        self.half_life = float(half_life_sec)
        self.log_basis: float | None = None
        self.updated_at = 0.0

    def update(self, t: float, grvt_bid: float, grvt_ask: float, binance_mid: float) -> float | None:
        if binance_mid <= 0 or grvt_bid + grvt_ask <= 0:
            return self.log_basis
        observed = math.log((grvt_bid + grvt_ask) / 2 / binance_mid)
        if self.log_basis is None:
            self.log_basis = observed
        else:
            weight = 1 - 0.5 ** (max(t - self.updated_at, 0.0) / self.half_life)
            self.log_basis += weight * (observed - self.log_basis)
        self.updated_at = t
        return self.log_basis


class VolTracker:
    """Realized Binance volatility: squared log moves, decayed over ``window_sec``.

    ``vol_bps`` is the square root of the decayed sum, so about the typical
    total move over the last window, in bps.
    """

    def __init__(self, window_sec: float):
        self.window = float(window_sec)
        self.sum_sq = 0.0
        self.last_log_mid: float | None = None
        self.updated_at = 0.0

    def update(self, t: float, mid: float) -> float:
        if mid > 0:
            log_mid = math.log(mid)
            self.sum_sq *= math.exp(-max(t - self.updated_at, 0.0) / self.window) if self.updated_at else 1.0
            if self.last_log_mid is not None:
                self.sum_sq += (log_mid - self.last_log_mid) ** 2
            self.last_log_mid, self.updated_at = log_mid, t
        return self.vol_bps(t)

    def vol_bps(self, t: float) -> float:
        decay = math.exp(-max(t - self.updated_at, 0.0) / self.window) if self.updated_at else 1.0
        return math.sqrt(self.sum_sq * decay) * 1e4


def fair_value(*, bid: float, ask: float, binance_mid: float, log_basis: float | None,
               params: QuoteParams) -> float | None:
    if bid <= 0 or ask <= bid or binance_mid <= 0 or log_basis is None:
        return None
    fair = binance_mid * math.exp(log_basis)
    if params.fair_shrink != 1.0:  # GRVT closes only part of a gap to Binance
        grvt_mid = (bid + ask) / 2
        fair = grvt_mid * (fair / grvt_mid) ** params.fair_shrink
    return fair


def quote_targets(*, bid: float, ask: float, binance_mid: float, log_basis: float | None,
                  position: float, params: QuoteParams, tick: float, vol_bps: float = 0.0) -> dict[str, float | None]:
    """Target price per side (``None``: no quote) for a position in base units."""
    fair = fair_value(bid=bid, ask=ask, binance_mid=binance_mid, log_basis=log_basis, params=params)
    if fair is None:
        return {"BUY": None, "SELL": None}  # no fair value yet: stay out
    lots = position * fair / params.notional
    fair *= 1 - params.skew_bps_per_lot * lots / 1e4
    half = (params.half_spread_bps + params.vol_spread_coef * vol_bps) / 1e4
    want_bid, want_ask = round_down(fair * (1 - half), tick), round_up(fair * (1 + half), tick)
    if params.improve:
        want_bid, want_ask = min(want_bid, round(ask - tick, 10)), max(want_ask, round(bid + tick, 10))
    else:
        want_bid, want_ask = min(want_bid, bid), max(want_ask, ask)
    limit = params.max_inventory_lots
    return {"BUY": None if limit and lots >= limit - EPS else want_bid,
            "SELL": None if limit and lots <= -limit + EPS else want_ask}


def needs_replace(*, side: str, resting_price: float, target: float, rested_sec: float,
                  params: QuoteParams, tick: float) -> bool:
    """Whether a resting quote should move to ``target``."""
    behind = (target - resting_price) if side == "BUY" else (resting_price - target)
    return behind < -EPS or (behind > params.keep_ticks * tick + EPS and rested_sec >= params.min_rest_sec)


__all__ = ["BasisTracker", "EPS", "QuoteParams", "SIDES", "VolTracker", "fair_value", "needs_replace",
           "quote_targets", "round_down", "round_up"]
