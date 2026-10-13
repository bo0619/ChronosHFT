"""Event-driven requote for resting post-only quotes (``fast_requote``).

The quoting model normally reprices on a fixed cycle. Between cycles a
resting quote can go stale: when the mid moves toward it, informed takers
pick it off before the next cycle. With ``fast_requote`` enabled, every book
update compares the mid with the mid the quotes were priced from. Once it
has moved the trigger the quote on the side the market moved toward is
cancelled at once and the model cycle runs immediately. When the cancels
have resolved, one more cycle refills the empty side instead of waiting for
the regular interval. ``min_interval_ms`` bounds how often this can fire.

The trigger is the larger of ``trigger_bps`` and ``min_trigger_ticks`` ticks
in bps. On a large-tick book (KORUUSDT: one 0.01 tick is ~7 bps) the mid
moves a whole tick whenever the touch flickers between two levels; a 1 bps
trigger then cancelled a quote on every flicker and sent it to the back of
the queue. A trigger above one tick lets the quote rest through a one-level
flicker while a two-level move still pulls it.

On venues that hold taker orders in a speed bump but not cancels (GRVT:
25ms for non-post-only orders), this is the window to pull a stale quote
before the taker's order reaches matching.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from event.type import Side

DEFAULT_TRIGGER_BPS = 1.0
DEFAULT_MIN_INTERVAL_MS = 100.0
DEFAULT_MIN_TRIGGER_TICKS = 0.0


@dataclass(frozen=True, slots=True)
class FastRequotePolicy:
    enabled: bool = False
    trigger_bps: float = DEFAULT_TRIGGER_BPS
    min_interval_ms: float = DEFAULT_MIN_INTERVAL_MS
    min_trigger_ticks: float = DEFAULT_MIN_TRIGGER_TICKS

    @classmethod
    def from_config(cls, section: Mapping | None, *, live_mode: bool):
        section = section if isinstance(section, Mapping) else {}
        values = {}
        for field, default in (
            ("trigger_bps", DEFAULT_TRIGGER_BPS),
            ("min_interval_ms", DEFAULT_MIN_INTERVAL_MS),
            ("min_trigger_ticks", DEFAULT_MIN_TRIGGER_TICKS),
        ):
            try:
                value = float(section.get(field, default))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"fast_requote.{field} must be finite") from exc
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(
                    f"fast_requote.{field} must be finite and nonnegative"
                )
            values[field] = value
        if values["trigger_bps"] <= 0.0:
            raise ValueError("fast_requote.trigger_bps must be positive")
        # Paper-only until Live has its own evidence for the extra order rate.
        enabled = bool(section.get("enabled", False)) and not live_mode
        return cls(enabled=enabled, **values)

    def trigger_threshold_bps(self, tick_size: float, mid: float) -> float:
        tick_bps = (
            tick_size / mid * 10_000.0 if tick_size > 0.0 and mid > 0.0 else 0.0
        )
        return max(self.trigger_bps, self.min_trigger_ticks * tick_bps)


class FastRequoteMixin:
    """Decides when the quoting cycle runs: on schedule or on a mid move."""

    def init_fast_requote(self, adaptive_config, *, live_mode: bool) -> None:
        config = adaptive_config if isinstance(adaptive_config, Mapping) else {}
        self.fast_requote_policy = FastRequotePolicy.from_config(
            config.get("fast_requote", {}),
            live_mode=live_mode,
        )
        self.fast_requote_state = defaultdict(
            lambda: {
                "reference_mid": None,
                "last_forced": float("-inf"),
                "refill": False,
                "count": 0,
                "imbalance_pulls": 0,
            }
        )

    def quote_cycle_due(self, symbol: str, mid: float, now: float) -> bool:
        """Return True when the model cycle should run on this book update."""
        due = now - self.last_run_times[symbol] >= self.cycle_interval
        if not due:
            due = self._fast_requote_due(symbol, mid, now)
        if due:
            self.last_run_times[symbol] = now
            self.fast_requote_state[symbol]["reference_mid"] = mid
        return due

    def _fast_requote_due(self, symbol: str, mid: float, now: float) -> bool:
        policy = self.fast_requote_policy
        if not policy.enabled or mid <= 0.0:
            return False
        fast = self.fast_requote_state[symbol]
        if (now - fast["last_forced"]) * 1_000.0 < policy.min_interval_ms:
            return False
        quotes = self.quote_state[symbol]
        if self._pull_against_imbalance(symbol, quotes):
            fast["imbalance_pulls"] += 1
            fast["refill"] = True
            return self._force_quote_cycle(symbol, now)
        reference = fast["reference_mid"]
        move_bps = (
            math.log(mid / reference) * 10_000.0
            if reference is not None and reference > 0.0
            else 0.0
        )
        if abs(move_bps) >= policy.trigger_threshold_bps(
            self._fast_requote_tick_size(symbol),
            mid,
        ):
            # Mid up: our ask is now cheap. Mid down: our bid is now rich.
            client_oid = quotes["ask_oid" if move_bps > 0.0 else "bid_oid"]
            if client_oid:
                self.cancel_order(client_oid)
            fast["refill"] = True
            return self._force_quote_cycle(symbol, now)
        if fast["refill"] and self._quotes_ready_to_refill(quotes):
            fast["refill"] = False
            return self._force_quote_cycle(symbol, now)
        return False

    def _pull_against_imbalance(self, symbol: str, quotes) -> bool:
        """Cancel a quote resting at the touch that the queue imbalance is
        against (strategy.touch_snap re-places it one level back)."""
        calibrator = getattr(self, "calibrators", {}).get(symbol)
        model = getattr(calibrator, "queue_imbalance", None)
        last_bid = getattr(model, "last_bid", None) if model else None
        if not model or last_bid is None:
            return False
        pulled = False
        for side, key, touch in (
            (Side.BUY, "bid", model.last_bid),
            (Side.SELL, "ask", model.last_ask),
        ):
            client_oid = quotes[f"{key}_oid"]
            price = quotes.get(f"{key}_price")
            if (
                not client_oid
                or price is None
                or client_oid in self.orders_cancelling
                or abs(float(price) - touch) > 1e-9 * max(1.0, touch)
                or not model.against(side)
            ):
                continue
            self.cancel_order(client_oid)
            pulled = True
        return pulled

    def _fast_requote_tick_size(self, symbol: str) -> float:
        info = self.reference_data.get_info(symbol)
        try:
            return float(getattr(info, "tick_size", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _quotes_ready_to_refill(self, quotes) -> bool:
        oids = (quotes["bid_oid"], quotes["ask_oid"])
        if any(oid and oid in self.orders_cancelling for oid in oids):
            return False
        return any(oid is None for oid in oids)

    def _force_quote_cycle(self, symbol: str, now: float) -> bool:
        fast = self.fast_requote_state[symbol]
        fast["last_forced"] = now
        fast["count"] += 1
        # min_interval_ms already bounds the order rate; skip the replace
        # cooldown so the forced cycle can actually reprice.
        self.quote_state[symbol]["last_update"] = float("-inf")
        return True


__all__ = ["FastRequoteMixin", "FastRequotePolicy"]
