"""Market-order execution for quoting strategies (``market`` mode).

The quoting model still computes its inventory-adjusted reservation price.
Instead of resting post-only quotes around it, this mode takes liquidity with
an IOC MARKET order whenever the reservation price clears the touch by the
taker fee plus ``market_execution.min_edge_bps``.
"""

from __future__ import annotations

import math
from collections import defaultdict

from event.type import (
    EVENT_STRATEGY_UPDATE,
    TIF_IOC,
    Event,
    ExecutionPolicy,
    OrderIntent,
    OrderStateSnapshot,
    OrderStatus,
    Side,
    StrategyData,
    TradeData,
)
from strategy.execution_mode import (
    EXECUTION_MODE_MARKET,
    MarketExecutionPolicy,
    decide_market_order,
    resolve_execution_mode,
)

_TERMINAL_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.REJECTED_LOCALLY,
        OrderStatus.EXPIRED,
    }
)
_MAX_REMEMBERED_MARKET_ORDERS = 1024


class MarketExecutionMixin:
    """Per-venue execution mode state and the market-order cycle."""

    def init_execution_mode(
        self,
        strategy_config,
        *,
        live_mode: bool,
    ) -> None:
        self.execution_mode = resolve_execution_mode(
            strategy_config,
            self.execution_venue,
        )
        self.market_execution = MarketExecutionPolicy.from_config(
            strategy_config
        )
        if live_mode and self.market_mode:
            raise ValueError(
                f"Live {self.name} market execution mode is Paper-only "
                "until it has its own approved evidence"
            )
        self.market_order_state = defaultdict(
            lambda: {"oid": None, "last_sent": float("-inf")}
        )
        # Our MARKET client ids, so taker fills stay out of passive markouts.
        self.market_order_oids = {}

    @property
    def market_mode(self) -> bool:
        return self.execution_mode == EXECUTION_MODE_MARKET

    def is_market_fill(self, trade: TradeData) -> bool:
        return trade.order_id in self.market_order_oids

    def on_order(self, snapshot: OrderStateSnapshot):
        super().on_order(snapshot)
        if snapshot.status in _TERMINAL_STATUSES:
            state = self.market_order_state[snapshot.symbol]
            if state["oid"] == snapshot.client_oid:
                state["oid"] = None

    def run_market_execution_cycle(
        self,
        symbol: str,
        mid: float,
        best_bid: float,
        best_ask: float,
        fair_mid: float,
        formula_quote,
        current_pos: float,
        now_monotonic: float,
        alpha_offset_bps: float,
        gamma: float,
        sigma: float,
        feature_engine,
    ) -> None:
        """Send at most one MARKET order, then close the model interval."""
        try:
            self._market_execution_cycle(
                symbol,
                mid,
                best_bid,
                best_ask,
                fair_mid,
                formula_quote.center_offset_bps,
                current_pos,
                now_monotonic,
                alpha_offset_bps,
                {"gamma_per_bps": gamma, "sigma_bps": sigma},
            )
        finally:
            feature_engine.reset_interval(symbol)

    def _market_execution_cycle(
        self,
        symbol: str,
        mid: float,
        best_bid: float,
        best_ask: float,
        fair_mid: float,
        center_offset_bps: float,
        current_pos: float,
        now_monotonic: float,
        alpha_offset_bps: float,
        telemetry: dict,
    ) -> None:
        # Switching from post-only must not leave resting quotes behind.
        for client_oid, intent in list(self.active_orders.items()):
            if intent.symbol == symbol and intent.order_type != "MARKET":
                self.cancel_order(client_oid)
        try:
            reservation_price = fair_mid * math.exp(
                center_offset_bps / 10_000.0
            )
            taker_fee_bps = self.taker_fee_bps(symbol)
            decision = decide_market_order(
                reservation_price=reservation_price,
                best_bid=best_bid,
                best_ask=best_ask,
                taker_fee_bps=taker_fee_bps,
                min_edge_bps=self.market_execution.min_edge_bps,
            )
        except (OverflowError, ValueError):
            return

        state = self.market_order_state[symbol]
        action = "HOLD"
        order_volume = 0.0
        if decision.side is not None:
            price = best_ask if decision.side == Side.BUY else best_bid
            order_volume = self._calculate_safe_vol(
                symbol,
                price,
                side=decision.side,
                current_position=current_pos,
                reference_price=mid,
            )
            action = self._send_market_order(
                symbol,
                decision.side,
                price,
                order_volume,
                now_monotonic,
            )

        params = {
            "schema": "market_making.v1",
            "strategy": self.name,
            "state": "MARKET_TAKING",
            "mode": "MARKET",
            "execution_mode": self.execution_mode,
            "execution_venue": self.execution_venue,
            "time_in_force": TIF_IOC,
            "use_rpi": False,
            "mid_price": mid,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "market_spread_bps": (best_ask - best_bid) / mid * 10_000.0,
            "fair_value": fair_mid,
            "reservation_price": reservation_price,
            "alpha_bps": alpha_offset_bps,
            "position_qty": current_pos,
            "position_notional": current_pos * mid,
            "inventory_center_offset_bps": center_offset_bps,
            "taker_fee_bps": taker_fee_bps,
            "market_min_edge_bps": self.market_execution.min_edge_bps,
            "market_buy_edge_bps": decision.buy_edge_bps,
            "market_sell_edge_bps": decision.sell_edge_bps,
            "market_action": action,
            "market_order_qty": order_volume,
            "market_order_id": state["oid"] or "",
            **telemetry,
            "State": "MARKET_TAKING",
            "Mode": "MARKET",
            "Size": f"{order_volume:.8g}",
        }
        self.engine.put(
            Event(
                EVENT_STRATEGY_UPDATE,
                StrategyData(
                    symbol=symbol,
                    fair_value=fair_mid,
                    alpha_bps=alpha_offset_bps,
                    params=params,
                ),
            )
        )

    def _send_market_order(
        self,
        symbol: str,
        side: Side,
        price: float,
        volume: float,
        now_monotonic: float,
    ) -> str:
        state = self.market_order_state[symbol]
        if state["oid"]:
            return "ORDER_IN_FLIGHT"
        elapsed_ms = (now_monotonic - state["last_sent"]) * 1_000.0
        if elapsed_ms < self.market_execution.cooldown_ms:
            return "COOLDOWN"
        if volume <= 0.0:
            return "POSITION_LIMIT"
        if not self.can_submit_orders(symbol):
            return "SUBMIT_BLOCKED"
        oid = self.send_intent(
            OrderIntent(
                self.name,
                symbol,
                side,
                price,
                volume,
                order_type="MARKET",
                time_in_force=TIF_IOC,
                is_post_only=False,
                policy=ExecutionPolicy.AGGRESSIVE,
                tag="market_mm",
            )
        )
        state["last_sent"] = now_monotonic
        if not oid:
            return "SUBMIT_REJECTED"
        state["oid"] = oid
        self.market_order_oids[oid] = True
        while len(self.market_order_oids) > _MAX_REMEMBERED_MARKET_ORDERS:
            self.market_order_oids.pop(next(iter(self.market_order_oids)))
        return f"MARKET_{side.value}"


__all__ = ["MarketExecutionMixin"]
