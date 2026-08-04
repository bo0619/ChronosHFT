"""Pre-trade order policy and emergency reduction controls."""

from __future__ import annotations

import math
import time
import uuid

from data.cache import data_cache
from data.ref_data import ref_data_manager
from event.type import (
    ExecutionPolicy,
    OMSCapabilityMode,
    OrderIntent,
    OrderRequest,
    Side,
    TIF_GTX,
    TIF_IOC,
)
from infrastructure.time_service import time_service

from .component import OMSComponent


class OMSOrderPolicy(OMSComponent):
    """Own pre-trade gates, mode adaptation and emergency reduce-only policy."""

    OWNER_READS = frozenset(
        {
            "_audit",
            "_ensure_capability_mode_consistent",
            "_get_capability_block_reason",
            "_submit_internal_order",
            "_venue_dead_man_switch_health_locked",
            "account",
            "can_open_new_risk",
            "capability_mode",
            "degraded_aggressive_to_passive",
            "duplicate_intent_window_sec",
            "emergency_flatten_cooldown_sec",
            "exchange_self_trade_prevention_mode",
            "exposure",
            "get_strategy_freeze_reason",
            "get_symbol_freeze_reason",
            "get_venue_freeze_reason",
            "last_emergency_flatten_ts",
            "last_risk_control_heartbeat_monotonic",
            "local_self_cross_check_enabled",
            "lock",
            "margin_health_enabled",
            "margin_health_require_snapshot",
            "margin_reduce_only_ratio",
            "margin_snapshot_max_age_sec",
            "max_strategy_active_orders",
            "max_strategy_symbol_active_orders",
            "max_symbol_active_orders",
            "max_total_active_orders",
            "orders",
            "paper_trade_database",
            "query_positions",
            "require_explicit_strategy_budget",
            "require_healthy_clock",
            "risk_control_heartbeat_enabled",
            "risk_control_heartbeat_max_age_sec",
            "risk_control_heartbeat_reason",
            "risk_control_heartbeat_status",
            "self_trade_prevention_enabled",
            "strategy_risk_budgets",
            "strategy_risk_budgets_enabled",
            "venue_dead_man_switch_enabled",
        }
    )

    def _get_clock_health_rejection_locked(self, intent: OrderIntent) -> str:
        if intent.reduce_only or not self.require_healthy_clock:
            return ""
        try:
            snapshot = time_service.health_snapshot(notify_listeners=False)
        except Exception as exc:
            return f"clock_health_unavailable:{type(exc).__name__}"
        if bool(snapshot.get("ready", False)):
            return ""
        state = str(snapshot.get("state", "unhealthy") or "unhealthy")
        reason = str(snapshot.get("reason", "") or "unsynchronized")
        return f"clock_health:{state}:{reason}"

    def adapt_intent_for_trading_mode(self, intent: OrderIntent):
        self._ensure_capability_mode_consistent()
        if self.capability_mode == OMSCapabilityMode.REDUCE_ONLY:
            if not intent.reduce_only:
                return None, "oms_mode_reduce_only"
            return intent, ""
        if self.capability_mode == OMSCapabilityMode.PASSIVE_ONLY:
            if not intent.is_post_only:
                return None, "oms_mode_passive_only"
            return intent, ""

        if self.capability_mode == OMSCapabilityMode.DEGRADED and self.degraded_aggressive_to_passive:
            if not intent.is_post_only:
                adapted = OrderIntent(
                    strategy_id=intent.strategy_id,
                    symbol=intent.symbol,
                    side=intent.side,
                    price=intent.price,
                    volume=intent.volume,
                    order_type="LIMIT",
                    time_in_force=TIF_GTX,
                    is_post_only=True,
                    reduce_only=intent.reduce_only,
                    policy=ExecutionPolicy.PASSIVE,
                    tag=f"{intent.tag}|degraded" if intent.tag else "degraded",
                    calibration_permit_id=intent.calibration_permit_id,
                    calibration_depth_bps=intent.calibration_depth_bps,
                    calibration_reference_mid=(
                        intent.calibration_reference_mid
                    ),
                )
                self._audit(
                    "intent_degraded_to_passive",
                    strategy_id=intent.strategy_id,
                    symbol=intent.symbol,
                    side=intent.side.value,
                    original_order_type=intent.order_type,
                    original_tif=intent.time_in_force,
                )
                return adapted, ""
        return intent, ""

    def _estimate_emergency_price(self, symbol: str, side: Side) -> float:
        bid, ask = data_cache.get_best_quote(symbol)
        if side == Side.BUY and ask > 0:
            return ask
        if side == Side.SELL and bid > 0:
            return bid

        mark_price = data_cache.get_mark_price(symbol)
        if mark_price > 0:
            return mark_price

        last_trade = data_cache.get_last_trade_price(symbol)
        if last_trade > 0:
            return last_trade

        local_pos_price = abs(float(self.exposure.avg_prices.get(symbol, 0.0) or 0.0))
        return local_pos_price if local_pos_price > 0 else 1.0

    def emergency_reduce_only_flatten(self, reason: str, symbol: str = "") -> int:
        target_symbols = {symbol.upper()} if symbol else set()
        remote_positions = self.query_positions()
        positions = {}

        if remote_positions:
            for payload in remote_positions:
                remote_symbol = str(payload.get("symbol", "") or "").upper()
                if not remote_symbol:
                    continue
                if target_symbols and remote_symbol not in target_symbols:
                    continue
                remote_volume = float(
                    payload.get("positionAmt", 0.0) or 0.0
                )
                if abs(remote_volume) > 1e-9:
                    positions[remote_symbol] = remote_volume

        # A just-arrived user-stream fill can be newer than the REST position
        # snapshot. A reduce-only order cannot increase exposure, so use local
        # nonzero truth whenever REST has not reported that symbol as nonzero.
        with self.lock:
            for local_symbol, volume in self.exposure.net_positions.items():
                local_symbol = local_symbol.upper()
                if target_symbols and local_symbol not in target_symbols:
                    continue
                if local_symbol not in positions and abs(volume) > 1e-9:
                    positions[local_symbol] = volume

        submitted = 0
        now_monotonic = time.perf_counter()
        self._audit("emergency_flatten_requested", reason=reason, symbols=sorted(positions.keys()))
        for target_symbol, volume in positions.items():
            if abs(volume) <= 1e-9:
                continue

            last_sent = self.last_emergency_flatten_ts.get(target_symbol, 0.0)
            if (
                now_monotonic - last_sent
                < self.emergency_flatten_cooldown_sec
            ):
                self._audit(
                    "emergency_flatten_suppressed",
                    reason=reason,
                    symbol=target_symbol,
                    cooldown_sec=self.emergency_flatten_cooldown_sec,
                )
                continue

            qty = ref_data_manager.round_qty(target_symbol, abs(volume))
            if qty <= 0:
                continue

            side = Side.SELL if volume > 0 else Side.BUY
            estimate_price = self._estimate_emergency_price(target_symbol, side)
            client_oid = f"EMG_{target_symbol[:16]}_{uuid.uuid4().hex[:12]}"
            intent = OrderIntent(
                "system_emergency",
                target_symbol,
                side,
                estimate_price,
                qty,
                order_type="MARKET",
                time_in_force=TIF_IOC,
                is_post_only=False,
                reduce_only=True,
                policy=ExecutionPolicy.AGGRESSIVE,
                tag=f"reduce_only_flatten:{reason}",
            )
            request = OrderRequest(
                symbol=target_symbol,
                price=estimate_price,
                volume=qty,
                side=side.value,
                order_type="MARKET",
                time_in_force=TIF_IOC,
                post_only=False,
                reduce_only=True,
                self_trade_prevention_mode=(
                    self.exchange_self_trade_prevention_mode
                ),
            )
            if self._submit_internal_order(
                intent,
                request,
                client_oid,
                "emergency_flatten",
                "emergency_flatten_submitted",
                reason=reason,
                reduce_only=True,
            ):
                self.last_emergency_flatten_ts[target_symbol] = now_monotonic
                submitted += 1

        return submitted

    def _get_order_block_reason(
        self,
        strategy_id: str = "",
        symbol: str = "",
        reduce_only: bool = False,
    ) -> str:
        if not self.can_open_new_risk():
            if self.capability_mode != OMSCapabilityMode.REDUCE_ONLY:
                return self._get_capability_block_reason("open_risk")
            if not reduce_only:
                return "oms_mode_reduce_only"

        venue_reason = self.get_venue_freeze_reason()
        if venue_reason:
            return f"venue_frozen:{venue_reason}"

        symbol_reason = self.get_symbol_freeze_reason(symbol)
        if symbol_reason:
            return f"symbol_frozen:{symbol_reason}"

        strategy_reason = self.get_strategy_freeze_reason(strategy_id, symbol)
        if strategy_reason:
            return f"strategy_frozen:{strategy_reason}"

        return ""

    def _get_submission_safety_reason_locked(self, intent: OrderIntent) -> str:
        database_rejection = self._get_paper_database_rejection_locked(intent)
        if database_rejection:
            return database_rejection

        clock_rejection = self._get_clock_health_rejection_locked(intent)
        if clock_rejection:
            return clock_rejection

        dead_man_rejection = (
            self._get_venue_dead_man_switch_rejection_locked(intent)
        )
        if dead_man_rejection:
            return dead_man_rejection

        heartbeat_rejection = self._get_risk_control_heartbeat_rejection_locked(
            intent
        )
        if heartbeat_rejection:
            return heartbeat_rejection

        margin_rejection = self._get_margin_health_rejection_locked(intent)
        if margin_rejection:
            return margin_rejection

        self_cross_rejection = self._get_self_trade_prevention_rejection_locked(
            intent
        )
        if self_cross_rejection:
            return self_cross_rejection

        total_active = 0
        symbol_active = 0
        strategy_active = 0
        strategy_symbol_active = 0
        now_monotonic = time.perf_counter()

        for order in self.orders.values():
            if not order.is_active():
                continue

            total_active += 1
            same_symbol = order.intent.symbol == intent.symbol
            same_strategy = order.intent.strategy_id == intent.strategy_id
            if same_symbol:
                symbol_active += 1
            if same_strategy:
                strategy_active += 1
            if same_symbol and same_strategy:
                strategy_symbol_active += 1

            if self.duplicate_intent_window_sec <= 0:
                continue
            created_monotonic = float(
                getattr(order, "created_monotonic", now_monotonic)
            )
            if (
                now_monotonic - created_monotonic
                > self.duplicate_intent_window_sec
            ):
                continue
            if not same_symbol or not same_strategy:
                continue
            if order.intent.side != intent.side:
                continue
            if order.intent.order_type != intent.order_type:
                continue
            if order.intent.time_in_force != intent.time_in_force:
                continue
            if bool(order.intent.is_post_only) != bool(intent.is_post_only):
                continue
            if abs(order.intent.price - intent.price) > 1e-9:
                continue
            if abs(order.intent.volume - intent.volume) > 1e-9:
                continue
            return (
                "duplicate_active_intent:"
                f"{intent.strategy_id}:{intent.symbol}:{intent.side.value}"
            )

        if self.max_total_active_orders > 0 and total_active >= self.max_total_active_orders:
            return f"active_order_limit:total:{total_active}>={self.max_total_active_orders}"
        if self.max_symbol_active_orders > 0 and symbol_active >= self.max_symbol_active_orders:
            return f"active_order_limit:symbol:{symbol_active}>={self.max_symbol_active_orders}"
        if self.max_strategy_active_orders > 0 and strategy_active >= self.max_strategy_active_orders:
            return f"active_order_limit:strategy:{strategy_active}>={self.max_strategy_active_orders}"
        if (
            self.max_strategy_symbol_active_orders > 0
            and strategy_symbol_active >= self.max_strategy_symbol_active_orders
        ):
            return (
                "active_order_limit:strategy_symbol:"
                f"{strategy_symbol_active}>={self.max_strategy_symbol_active_orders}"
            )
        return ""

    def _get_paper_database_rejection_locked(
        self,
        intent: OrderIntent,
    ) -> str:
        if intent.reduce_only:
            return ""
        database = getattr(self, "paper_trade_database", None)
        if database is None:
            return ""
        try:
            snapshot = database.health_snapshot()
        except Exception as exc:
            return f"paper_trade_database_health_unavailable:{type(exc).__name__}"
        if bool(snapshot.get("healthy", False)):
            return ""
        reason = str(snapshot.get("last_error", "") or "unhealthy")
        return f"paper_trade_database_unhealthy:{reason}"

    def _get_self_trade_prevention_rejection_locked(
        self,
        intent: OrderIntent,
    ) -> str:
        if (
            not self.self_trade_prevention_enabled
            or not self.local_self_cross_check_enabled
        ):
            return ""

        incoming_is_market = str(intent.order_type or "").upper() == "MARKET"
        for resting in self.orders.values():
            if not resting.is_active() or resting.intent.symbol != intent.symbol:
                continue
            if resting.intent.side == intent.side:
                continue
            if resting.intent.volume - resting.filled_volume <= 1e-12:
                continue
            # RPI orders only match App/Web retail flow. They cannot execute
            # against this OMS's API orders, even at crossed prices.
            if intent.is_rpi or resting.intent.is_rpi:
                continue

            resting_is_market = (
                str(resting.intent.order_type or "").upper() == "MARKET"
            )
            crosses = incoming_is_market or resting_is_market
            if not crosses and intent.side == Side.BUY:
                crosses = intent.price >= resting.intent.price
            elif not crosses and intent.side == Side.SELL:
                crosses = intent.price <= resting.intent.price
            if not crosses:
                continue

            return (
                "self_trade_prevention:crossing_active_order:"
                f"{resting.client_oid}:{resting.intent.side.value}:"
                f"{resting.intent.price:.12g}"
            )
        return ""

    def _get_risk_control_heartbeat_rejection_locked(
        self,
        intent: OrderIntent,
    ) -> str:
        if intent.reduce_only or not self.risk_control_heartbeat_enabled:
            return ""

        if self.last_risk_control_heartbeat_monotonic <= 0.0:
            return "risk_control_heartbeat_missing"
        if self.risk_control_heartbeat_status != "healthy":
            detail = self.risk_control_heartbeat_reason or "unhealthy"
            return f"risk_control_heartbeat_unhealthy:{detail}"

        age_sec = max(
            0.0,
            time.perf_counter() - self.last_risk_control_heartbeat_monotonic,
        )
        if age_sec > self.risk_control_heartbeat_max_age_sec:
            return (
                f"risk_control_heartbeat_stale:{age_sec:.3f}s>"
                f"{self.risk_control_heartbeat_max_age_sec:.3f}s"
            )
        return ""

    def _get_venue_dead_man_switch_rejection_locked(
        self,
        intent: OrderIntent,
    ) -> str:
        if intent.reduce_only or not self.venue_dead_man_switch_enabled:
            return ""
        healthy, reason = self._venue_dead_man_switch_health_locked()
        if healthy:
            return ""
        return f"venue_dead_man_switch:{reason or 'unhealthy'}"

    def _get_margin_health_rejection_locked(self, intent: OrderIntent) -> str:
        if intent.reduce_only or not self.margin_health_enabled:
            return ""
        if not self.account.margin_snapshot_synced:
            return "margin_health_unavailable" if self.margin_health_require_snapshot else ""

        snapshot_monotonic = float(
            self.account.margin_snapshot_monotonic or 0.0
        )
        snapshot_time = float(self.account.margin_snapshot_time or 0.0)
        if snapshot_monotonic > 0.0:
            age_sec = max(
                0.0,
                time.perf_counter() - snapshot_monotonic,
            )
        else:
            age_sec = (
                max(0.0, time.time() - snapshot_time)
                if snapshot_time
                else float("inf")
            )
        if self.margin_snapshot_max_age_sec > 0.0 and age_sec > self.margin_snapshot_max_age_sec:
            return (
                f"margin_health_stale:{age_sec:.3f}s>"
                f"{self.margin_snapshot_max_age_sec:.3f}s"
            )
        ratio = float(self.account.maintenance_margin_ratio or 0.0)
        if math.isnan(ratio) or ratio < 0.0:
            return f"margin_health_invalid:{ratio!r}"
        if ratio >= self.margin_reduce_only_ratio:
            return (
                f"margin_health_reduce_only:{ratio:.6f}>="
                f"{self.margin_reduce_only_ratio:.6f}"
            )
        return ""

    def _get_strategy_budget_rejection_locked(self, intent: OrderIntent) -> str:
        if intent.reduce_only or not self.strategy_risk_budgets_enabled:
            return ""
        strategy_id = str(intent.strategy_id or "").strip()
        budget = self.strategy_risk_budgets.get(strategy_id)
        if budget is None:
            if self.require_explicit_strategy_budget:
                return f"strategy_budget_unconfigured:{strategy_id or '<empty>'}"
            return ""
        ok, reason = self.exposure.check_strategy_risk(
            strategy_id,
            intent.symbol,
            intent.side,
            intent.volume,
            budget["max_gross_notional"],
            budget["max_symbol_notional"],
            intent.price,
        )
        return "" if ok else f"strategy_budget_limit:{reason}"

    def get_strategy_risk_budget_snapshot(self) -> dict:
        with self.lock:
            return {
                "enabled": self.strategy_risk_budgets_enabled,
                "require_explicit_strategy": self.require_explicit_strategy_budget,
                "budgets": {
                    strategy_id: dict(budget)
                    for strategy_id, budget in self.strategy_risk_budgets.items()
                },
                "ledger": self.exposure.get_strategy_snapshot(),
            }
