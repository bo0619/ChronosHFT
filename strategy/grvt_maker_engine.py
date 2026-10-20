"""Live state machine for the GRVT maker priced off Binance.

The engine owns quoting, order state, position and risk for one symbol and
talks to the venue only through an executor (``place``, ``cancel``,
``cancel_all``), whose results come back as calls on the engine. It runs on
one thread: market data, private-feed updates and executor results are
queued by the runner (``scripts/run_grvt_maker.py``) and fed in order.

Quoting is :mod:`strategy.grvt_cross_maker`, as in the replay. Differences
the live side has to handle:

* the GRVT book contains our own quotes; the runner passes the touch with
  them removed (:func:`external_touch`), as the replay saw the market;
* a replace sends the cancel and the new order together, as the replay
  lands both after one latency; a side waits until both are answered;
* the position comes from our fills on the private feed, and is reset to
  the venue's position on reconciliation.

Risk (:class:`RiskLimits`): quotes are pulled when either feed shows no
sign of life (no update and no websocket pong, :meth:`on_feed_alive`, for
``stale_*_sec``; a quiet market keeps its quotes) or Binance lags; the adding side is pulled at ``max_position_usd``; a loss
since the UTC day began beyond ``daily_loss_reduce_usd`` leaves only
reduce-only quotes on the side that cuts the position, and beyond
``daily_loss_halt_usd`` (or on a taker fill, a maker fee above
``max_maker_fee_bps``, repeated venue errors, or :meth:`halt`) everything
is cancelled and the engine stops quoting until restarted. Order actions
are rate limited; cancels never wait for the limit.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

from strategy.grvt_cross_maker import EPS, SIDES, BasisTracker, QuoteParams, VolTracker, needs_replace, quote_targets


class Mode(str, Enum):
    RUNNING = "RUNNING"
    REDUCE_ONLY = "REDUCE_ONLY"
    HALTED = "HALTED"


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_position_usd: float
    daily_loss_reduce_usd: float
    daily_loss_halt_usd: float
    stale_binance_sec: float = 3.0
    stale_grvt_sec: float = 5.0
    max_binance_lag_ms: float = 250.0
    max_maker_fee_bps: float = 0.5
    max_actions_per_sec: float = 20.0
    max_consecutive_errors: int = 5


@dataclass(frozen=True, slots=True)
class InstrumentRules:
    tick: float
    size_step: float
    min_size: float
    min_notional: float = 0.0


class Executor(Protocol):
    def place(self, *, client_id: str, side: str, price: float, size: float, reduce_only: bool) -> None: ...
    def cancel(self, *, client_id: str) -> None: ...
    def cancel_all(self) -> None: ...


@dataclass(slots=True)
class LiveOrder:
    client_id: str
    side: str
    price: float
    size: float
    reduce_only: bool
    created_at: float
    status: str = "SENT"  # SENT -> OPEN -> CANCELING; removed when terminal
    filled: float = 0.0
    order_id: str | None = None

    @property
    def remaining(self) -> float:
        return max(self.size - self.filled, 0.0)


TERMINAL = {"FILLED", "CANCELLED", "REJECTED"}


def floor_to(value: float, step: float) -> float:
    return round(math.floor(value / step + 1e-9) * step, 10)


def external_touch(bids: Mapping[float, float], asks: Mapping[float, float],
                   own: Mapping[str, Mapping[float, float]]) -> tuple[float, float]:
    """Best bid and ask of a book after taking out our own resting sizes."""
    def best(levels, mine, reverse):
        for price in sorted(levels, reverse=reverse):
            if levels[price] - mine.get(price, 0.0) > EPS:
                return price
        return 0.0
    return best(bids, own.get("BUY", {}), True), best(asks, own.get("SELL", {}), False)


@dataclass
class EngineStats:
    placed: int = 0
    cancelled: int = 0
    rejected: int = 0
    fills: int = 0
    maker_volume_usd: float = 0.0
    fees_usd: float = 0.0
    events: dict = field(default_factory=dict)


class MakerEngine:
    def __init__(self, *, symbol: str, rules: InstrumentRules, params: QuoteParams, limits: RiskLimits,
                 executor: Executor, new_client_id: Callable[[], str], clock: Callable[[], float] = time.time,
                 log: Callable[..., None] = lambda event, **fields: None):
        self.symbol, self.rules, self.params, self.limits = symbol, rules, params, limits
        self.executor, self.new_client_id, self.clock, self.log = executor, new_client_id, clock, log
        self.mode, self.reason = Mode.RUNNING, ""
        self.position = 0.0
        self.cash = 0.0
        self.orders: dict[str, LiveOrder] = {}
        self.current: dict[str, str | None] = {"BUY": None, "SELL": None}
        self.inflight: dict[str, int] = {"BUY": 0, "SELL": 0}
        self.requests: dict[tuple[str, str], str] = {}  # (kind, client id) -> side
        self.basis = BasisTracker(params.basis_half_life)
        self.vol = VolTracker(params.vol_window_sec)
        self.binance_mid = 0.0
        self.binance_seen = 0.0
        self.binance_alive = 0.0
        self.binance_lag_ms = 0.0
        self.bid = self.ask = 0.0
        self.grvt_seen = 0.0
        self.grvt_alive = 0.0
        self.day: str | None = None
        self.day_start_equity = 0.0
        self.errors = 0
        self.actions: deque[float] = deque()
        self.trade_ids: deque[str] = deque(maxlen=10_000)
        self.quiet_reason = ""
        self.stats = EngineStats()

    # ----------------------------------------------------------- market data
    def on_binance(self, bid: float, ask: float, event_ms: float | None = None) -> None:
        if bid <= 0 or ask < bid:
            return
        now = self.clock()
        self.binance_mid, self.binance_seen = (bid + ask) / 2, now
        self.vol.update(now, self.binance_mid)
        self.binance_lag_ms = max(now * 1000.0 - event_ms, 0.0) if event_ms else 0.0
        self.decide()

    def on_grvt_book(self, bid: float, ask: float) -> None:
        """The GRVT touch with our own quotes taken out (:func:`external_touch`)."""
        if bid <= 0 or ask <= bid:
            return
        now = self.clock()
        self.bid, self.ask, self.grvt_seen = bid, ask, now
        self.basis.update(now, bid, ask, self.binance_mid)
        self.mark()
        self.decide()

    def on_feed_alive(self, source: str) -> None:
        """A pong on the ``binance`` or ``grvt`` market-data connection: up, even if no price moved."""
        if source == "binance":
            self.binance_alive = self.clock()
        elif source == "grvt":
            self.grvt_alive = self.clock()

    def on_timer(self) -> None:
        self.mark()
        self.decide()

    def own_levels(self) -> dict[str, dict[float, float]]:
        """Our resting size per price, to take out of the public book."""
        levels: dict[str, dict[float, float]] = {"BUY": {}, "SELL": {}}
        for order in self.orders.values():
            if order.status != "SENT":
                side = levels[order.side]
                side[order.price] = side.get(order.price, 0.0) + order.remaining
        return levels

    # -------------------------------------------------------------- decisions
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid > 0 and self.ask > self.bid else 0.0

    def quiet(self, now: float) -> str:
        """Why quotes must be pulled now ('' when they may stand)."""
        if self.mode is Mode.HALTED:
            return f"halted: {self.reason}"
        if not self.binance_seen or now - max(self.binance_seen, self.binance_alive) > self.limits.stale_binance_sec:
            return "binance stale"
        if not self.grvt_seen or now - max(self.grvt_seen, self.grvt_alive) > self.limits.stale_grvt_sec:
            return "grvt stale"
        if self.binance_lag_ms > self.limits.max_binance_lag_ms:
            return f"binance lag {self.binance_lag_ms:.0f} ms"
        return ""

    def targets(self, now: float) -> dict[str, float | None]:
        reason = self.quiet(now)
        if reason != self.quiet_reason:
            self.log("quiet" if reason else "resume", reason=reason or self.quiet_reason)
            self.quiet_reason = reason
        if reason:
            return {"BUY": None, "SELL": None}
        targets = quote_targets(bid=self.bid, ask=self.ask, binance_mid=self.binance_mid,
                                log_basis=self.basis.log_basis, position=self.position,
                                params=self.params, tick=self.rules.tick, vol_bps=self.vol.vol_bps(now))
        exposure = self.position * self.mid()
        if exposure >= self.limits.max_position_usd - EPS:
            targets["BUY"] = None
        if exposure <= -self.limits.max_position_usd + EPS:
            targets["SELL"] = None
        if self.mode is Mode.REDUCE_ONLY:
            if self.position >= 0:
                targets["BUY"] = None
            if self.position <= 0:
                targets["SELL"] = None
        return targets

    def decide(self) -> None:
        now = self.clock()
        targets = self.targets(now)
        for side in SIDES:
            if self.inflight[side]:
                continue
            client_id = self.current[side]
            order = self.orders.get(client_id) if client_id else None
            target = targets[side]
            if target is None:
                if order is not None:
                    self._cancel(order, now)
                continue
            if order is not None:
                if not needs_replace(side=side, resting_price=order.price, target=target,
                                     rested_sec=now - order.created_at, params=self.params, tick=self.rules.tick):
                    continue
                if not self._budget(now, 2):
                    continue
                self._cancel(order, now)
            elif not self._budget(now, 1):
                continue
            self._place(side, target, now)

    def _budget(self, now: float, actions: int) -> bool:
        while self.actions and now - self.actions[0] > 1.0:
            self.actions.popleft()
        return len(self.actions) + actions <= self.limits.max_actions_per_sec

    def _size(self, price: float) -> float:
        size = floor_to(self.params.notional / price, self.rules.size_step)
        if self.mode is Mode.REDUCE_ONLY:
            size = min(size, floor_to(abs(self.position), self.rules.size_step))
        if size < self.rules.min_size - EPS or size * price < self.rules.min_notional - EPS:
            return 0.0
        return size

    def _place(self, side: str, price: float, now: float) -> None:
        size = self._size(price)
        if size <= 0:
            return
        client_id = self.new_client_id()
        reduce_only = self.mode is Mode.REDUCE_ONLY
        self.orders[client_id] = LiveOrder(client_id, side, price, size, reduce_only, now)
        self.current[side] = client_id
        self.inflight[side] += 1
        self.requests[("place", client_id)] = side
        self.actions.append(now)
        self.stats.placed += 1
        self.log("place", client_id=client_id, side=side, price=price, size=size, reduce_only=reduce_only,
                 fair_mid=self.binance_mid, bid=self.bid, ask=self.ask, position=self.position)
        self.executor.place(client_id=client_id, side=side, price=price, size=size, reduce_only=reduce_only)

    def _cancel(self, order: LiveOrder, now: float) -> None:
        if self.current.get(order.side) == order.client_id:
            self.current[order.side] = None
        if order.status == "CANCELING":
            return
        order.status = "CANCELING"
        self.inflight[order.side] += 1
        self.requests[("cancel", order.client_id)] = order.side
        self.actions.append(now)
        self.stats.cancelled += 1
        self.log("cancel", client_id=order.client_id, side=order.side, price=order.price)
        self.executor.cancel(client_id=order.client_id)

    # -------------------------------------------------------- venue responses
    def _answered(self, kind: str, client_id: str) -> None:
        side = self.requests.pop((kind, client_id), None)
        if side is not None:
            self.inflight[side] = max(self.inflight[side] - 1, 0)

    def _error(self, what: str, error: str) -> None:
        self.errors += 1
        self.log("venue_error", what=what, error=error, consecutive=self.errors)
        if self.errors >= self.limits.max_consecutive_errors:
            self.halt(f"{self.errors} venue errors in a row, last: {error}")

    def on_place_result(self, client_id: str, ok: bool, *, order_id: str | None = None, error: str = "",
                        post_only_reject: bool = False) -> None:
        self._answered("place", client_id)
        order = self.orders.get(client_id)
        if ok:
            self.errors = 0
            if order is not None:
                order.order_id = order_id or order.order_id
                if order.status == "SENT":
                    order.status = "OPEN"
        else:
            if order is not None:
                self._drop(order)
            self.stats.rejected += 1
            if post_only_reject:
                self.log("post_only_reject", client_id=client_id)
            else:
                self._error("place", error)
        self.decide()

    def on_cancel_result(self, client_id: str, ok: bool, *, error: str = "") -> None:
        self._answered("cancel", client_id)
        if ok:
            self.errors = 0
        else:
            # Usually the order had already filled or been cancelled; the
            # order feed or the next reconciliation settles it.
            self.log("cancel_failed", client_id=client_id, error=error)
        self.decide()

    def on_order_state(self, client_id: str, status: str, *, order_id: str | None = None,
                       reject_reason: str = "") -> None:
        order = self.orders.get(client_id)
        if order is None:
            return
        if order_id:
            order.order_id = order_id
        if status in TERMINAL:
            if status == "REJECTED":
                self.log("rejected", client_id=client_id, reason=reject_reason)
            self._drop(order)
            self.decide()
        elif status == "OPEN" and order.status == "SENT":
            order.status = "OPEN"

    def _drop(self, order: LiveOrder) -> None:
        self.orders.pop(order.client_id, None)
        if self.current.get(order.side) == order.client_id:
            self.current[order.side] = None

    def on_fill(self, *, trade_id: str, client_id: str, is_buyer: bool, size: float, price: float,
                fee: float, is_taker: bool, fee_rate: str = "") -> None:
        if trade_id in self.trade_ids:
            return
        self.trade_ids.append(trade_id)
        signed = size if is_buyer else -size
        self.position += signed
        self.cash -= signed * price + fee
        notional = size * price
        fee_bps = fee / notional * 1e4 if notional > 0 else 0.0
        self.stats.fills += 1
        self.stats.maker_volume_usd += 0.0 if is_taker else notional
        self.stats.fees_usd += fee
        order = self.orders.get(client_id)
        if order is not None:
            order.filled += size
            if order.remaining <= EPS:
                self._drop(order)
        self.log("fill", trade_id=trade_id, client_id=client_id, side="BUY" if is_buyer else "SELL", size=size,
                 price=price, fee=fee, fee_bps=round(fee_bps, 4), fee_rate=fee_rate, is_taker=is_taker,
                 position=self.position, mid=self.mid(), fair_mid=self.binance_mid)
        if is_taker:
            self.halt(f"taker fill {trade_id}")
        elif fee_bps > self.limits.max_maker_fee_bps:
            self.halt(f"maker fee {fee_bps:.3f} bps on {trade_id}")
        self.mark()
        self.decide()

    def on_position(self, size: float) -> None:
        if abs(size - self.position) > self.rules.size_step / 2:
            self.log("position_reset", engine=self.position, venue=size)
            self.cash -= (size - self.position) * self.mid()  # keep equity continuous
            self.position = size
            self.decide()

    def reconcile(self, open_orders: Mapping[str, dict], position: float | None, *, grace_sec: float = 3.0) -> None:
        """Align with the venue: ``open_orders`` maps client id -> order info."""
        now = self.clock()
        for client_id, order in list(self.orders.items()):
            if client_id not in open_orders and now - order.created_at > grace_sec \
                    and ("place", client_id) not in self.requests:
                self.log("reconcile_drop", client_id=client_id)
                self._drop(order)
        for client_id in open_orders:
            if client_id not in self.orders:
                self.log("reconcile_unknown", client_id=client_id)
                self.executor.cancel(client_id=client_id)
        if position is not None:
            self.on_position(position)
        self.decide()

    # ------------------------------------------------------------------ risk
    def equity(self) -> float:
        return self.cash + self.position * self.mid()

    def mark(self) -> None:
        if self.mid() <= 0:
            return
        day = datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m-%d")
        equity = self.equity()
        if day != self.day:
            self.day, self.day_start_equity = day, equity
            if self.mode is Mode.REDUCE_ONLY and self.reason.startswith("daily loss"):
                self.mode, self.reason = Mode.RUNNING, ""
                self.log("mode", mode=self.mode.value, reason="new day")
        pnl = equity - self.day_start_equity
        if pnl <= -self.limits.daily_loss_halt_usd:
            self.halt(f"daily loss {pnl:.2f} USD")
        elif pnl <= -self.limits.daily_loss_reduce_usd and self.mode is Mode.RUNNING:
            self.mode, self.reason = Mode.REDUCE_ONLY, f"daily loss {pnl:.2f} USD"
            self.log("mode", mode=self.mode.value, reason=self.reason)
            for order in list(self.orders.values()):
                self._cancel(order, self.clock())

    def halt(self, reason: str) -> None:
        if self.mode is Mode.HALTED:
            return
        self.mode, self.reason = Mode.HALTED, reason
        self.log("mode", mode=self.mode.value, reason=reason)
        now = self.clock()
        for order in list(self.orders.values()):
            self._cancel(order, now)
        self.executor.cancel_all()

    def status(self) -> dict:
        return {"symbol": self.symbol, "mode": self.mode.value, "reason": self.reason or self.quiet_reason,
                "position": self.position, "exposure_usd": round(self.position * self.mid(), 2),
                "pnl_today": round(self.equity() - self.day_start_equity, 4) if self.day else 0.0,
                "bid": self.bid, "ask": self.ask, "binance_mid": self.binance_mid,
                "log_basis_bps": round((self.basis.log_basis or 0.0) * 1e4, 3),
                "vol_bps": round(self.vol.vol_bps(self.clock()), 3),
                "quotes": {s: (lambda o: o and [o.price, o.remaining, o.status])(self.orders.get(self.current[s] or ""))
                           for s in SIDES},
                "orders": len(self.orders), "placed": self.stats.placed, "cancelled": self.stats.cancelled,
                "rejected": self.stats.rejected, "fills": self.stats.fills,
                "maker_volume_usd": round(self.stats.maker_volume_usd, 2), "fees_usd": round(self.stats.fees_usd, 6)}


__all__ = ["EngineStats", "Executor", "InstrumentRules", "LiveOrder", "MakerEngine", "Mode", "RiskLimits",
           "external_touch", "floor_to"]
