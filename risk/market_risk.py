"""Market-data health evaluation and scoped recovery state."""

from __future__ import annotations

import math
import time
from collections import defaultdict

from data.cache import data_cache
from event.type import Event, OMSCapabilityMode
from infrastructure.time_service import time_service


class MarketRiskField:
    """Expose one declared market-risk field through RiskManager."""

    def __init__(self, attribute: str):
        self.attribute = attribute

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return getattr(instance.market_risk, self.attribute)

    def __set__(self, instance, value) -> None:
        setattr(instance.market_risk, self.attribute, value)


class MarketRiskMethod:
    """Bind one RiskManager method to its market-risk controller."""

    def __init__(self, method_name: str | None = None):
        self.method_name = method_name

    def __set_name__(self, owner, name: str) -> None:
        if self.method_name is None:
            self.method_name = name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return getattr(instance.market_risk, self.method_name)

    def __set__(self, instance, value) -> None:
        setattr(instance.market_risk, self.method_name, value)


class MarketRiskController:
    """Own market latency, divergence and freshness state machines."""

    def __init__(
        self,
        *,
        risk_config: dict,
        active: bool,
        oms,
        gateway,
        funding_guard,
        refresh_rearm_state,
        is_killed,
        trigger_kill_switch,
        freeze_symbol,
        recover_symbol_if_stable,
        owned_symbol_reason,
        clear_owned_symbol_freeze,
        current_venue,
        freeze_venue,
        recover_venue_if_stable,
        set_trading_mode,
        clear_trading_mode,
        renew_venue_dead_man_switch,
        publish_risk_control_heartbeat,
        tracked_symbols,
        log_warn,
        latency_recovery_by_symbol,
        divergence_recovery_by_symbol,
        venue_recovery_by_venue,
        venue_freeze_recovery_updates: int,
    ) -> None:
        self.active = active
        self.oms = oms
        self.gateway = gateway
        self.funding_guard = funding_guard
        self._refresh_rearm_state = refresh_rearm_state
        self._is_killed = is_killed
        self.trigger_kill_switch = trigger_kill_switch
        self._freeze_symbol = freeze_symbol
        self._recover_symbol_if_stable = recover_symbol_if_stable
        self._owned_symbol_reason = owned_symbol_reason
        self._clear_owned_symbol_freeze = clear_owned_symbol_freeze
        self._current_venue = current_venue
        self._freeze_venue = freeze_venue
        self._recover_venue_if_stable = recover_venue_if_stable
        self._set_trading_mode = set_trading_mode
        self._clear_trading_mode = clear_trading_mode
        self._renew_venue_dead_man_switch = renew_venue_dead_man_switch
        self._publish_risk_control_heartbeat = publish_risk_control_heartbeat
        self._tracked_symbols = tracked_symbols
        self._log_warn = log_warn
        self.latency_recovery_by_symbol = latency_recovery_by_symbol
        self.divergence_recovery_by_symbol = divergence_recovery_by_symbol
        self.venue_recovery_by_venue = venue_recovery_by_venue
        self.venue_freeze_recovery_updates = venue_freeze_recovery_updates

        freshness = risk_config.get("market_data_freshness", {})
        self.market_freshness_enabled = bool(freshness.get("enabled", True))
        self.require_mark_price = bool(
            freshness.get("require_mark_price", True)
        )
        self.require_book = bool(freshness.get("require_book", True))
        self.max_mark_age_ms = max(
            0.0,
            float(freshness.get("max_mark_age_ms", 3000.0) or 0.0),
        )
        self.max_book_age_ms = max(
            0.0,
            float(freshness.get("max_book_age_ms", 1500.0) or 0.0),
        )
        self.freshness_poll_interval_sec = max(
            0.05,
            float(freshness.get("poll_interval_sec", 0.25) or 0.25),
        )
        self.freshness_breach_checks = max(
            1,
            int(freshness.get("breach_checks", 2) or 2),
        )
        self.freshness_recovery_checks = max(
            1,
            int(freshness.get("recovery_checks", 5) or 5),
        )
        self._last_freshness_poll_at = 0.0
        self.freshness_breach_by_symbol = defaultdict(int)
        self.freshness_recovery_by_symbol = defaultdict(int)

        tech = risk_config.get("tech_health", {})
        self.max_latency_ms = tech.get("max_latency_ms", 1000)
        self.max_processing_lag_ms = tech.get(
            "max_processing_lag_ms",
            self.max_latency_ms,
        )
        self.consecutive_error_limit = max(
            1,
            int(tech.get("consecutive_error_limit", 10)),
        )
        self.degraded_error_limit = max(
            1,
            int(tech.get("degraded_error_limit", 1)),
        )
        self.passive_only_error_limit = max(
            self.degraded_error_limit,
            int(
                tech.get(
                    "passive_only_error_limit",
                    max(2, self.degraded_error_limit + 1),
                )
            ),
        )
        black_swan = risk_config.get("black_swan", {})
        self.volatility_halt_threshold = black_swan.get(
            "volatility_halt_threshold",
            0.05,
        )
        self.latency_breach_count = 0
        self.processing_lag_breach_count = 0
        self.last_market_latency_ms = 0.0
        self.last_processing_lag_ms = 0.0
        self.last_gateway_dispatch_lag_ms = 0.0
        self.latency_breach_by_symbol = defaultdict(int)
        self.divergence_breach_by_symbol = defaultdict(int)
        self.processing_mode_recovery_by_venue = defaultdict(int)

    @property
    def kill_switch_triggered(self) -> bool:
        return bool(self._is_killed())

    @property
    def funding_guard_policy(self):
        return self.funding_guard.policy

    def on_mark_price(self, event: Event):
        self._refresh_rearm_state()
        data = event.data
        symbol = str(getattr(data, "symbol", "") or "").upper()
        try:
            mark_price = float(getattr(data, "mark_price", 0.0))
            index_price = float(getattr(data, "index_price", 0.0))
        except (TypeError, ValueError):
            mark_price = 0.0
            index_price = 0.0
        if (
            not symbol
            or not math.isfinite(mark_price)
            or mark_price <= 0.0
            or not math.isfinite(index_price)
            or index_price <= 0.0
        ):
            if self.active and not self.kill_switch_triggered and symbol:
                self._freeze_symbol(
                    symbol,
                    "invalid_mark_price",
                )
            return
        if self.active and self.funding_guard_policy.enabled:
            self.funding_guard.evaluate_mark(data)
        if self.kill_switch_triggered or not self.active:
            return

        if self.volatility_halt_threshold <= 0:
            return

        divergence = abs(mark_price - index_price) / index_price
        if divergence > self.volatility_halt_threshold:
            if symbol:
                self.divergence_breach_by_symbol[symbol] += 1
                self.divergence_recovery_by_symbol[symbol] = 0
            self._log_warn(
                f"Mark/index divergence {divergence:.2%} > {self.volatility_halt_threshold:.2%} "
                f"({self.divergence_breach_by_symbol[symbol]}/{self.consecutive_error_limit}) {data.symbol}"
            )
            if symbol and self.divergence_breach_by_symbol[symbol] >= self.consecutive_error_limit:
                self._freeze_symbol(
                    symbol,
                    f"divergence:{divergence:.2%}>{self.volatility_halt_threshold:.2%}",
                )
            return

        if symbol:
            self.divergence_breach_by_symbol[symbol] = 0
            self._recover_symbol_if_stable(symbol, prefix="divergence:")

    def on_orderbook(self, event: Event):
        self._refresh_rearm_state()
        if self.kill_switch_triggered or not self.active:
            return

        orderbook = event.data
        symbol = getattr(orderbook, "symbol", "").upper()
        venue = self._current_venue()
        now = time.time()
        now_monotonic = time.perf_counter()
        exchange_ts = float(getattr(orderbook, "exchange_timestamp", 0.0) or 0.0)
        received_ts = float(getattr(orderbook, "received_timestamp", 0.0) or 0.0)
        corrected_received_ts = float(
            getattr(orderbook, "corrected_received_timestamp", 0.0) or 0.0
        )
        received_monotonic = float(
            getattr(orderbook, "received_monotonic", 0.0) or 0.0
        )
        dispatch_monotonic = float(
            getattr(orderbook, "dispatch_monotonic", 0.0) or 0.0
        )
        if received_monotonic:
            processing_lag_ms = (now_monotonic - received_monotonic) * 1000.0
        elif received_ts:
            # Backward compatibility for synthetic and persisted events that
            # predate the monotonic ingress timestamp.
            processing_lag_ms = (now - received_ts) * 1000.0
        else:
            processing_lag_ms = 0.0
        self.last_processing_lag_ms = processing_lag_ms
        self.last_gateway_dispatch_lag_ms = (
            (dispatch_monotonic - received_monotonic) * 1000.0
            if dispatch_monotonic and received_monotonic
            else 0.0
        )
        processing_lag_for_limit = abs(processing_lag_ms)
        if processing_lag_for_limit > self.max_processing_lag_ms:
            self.processing_lag_breach_count += 1
            self.venue_recovery_by_venue[venue] = 0
            self.processing_mode_recovery_by_venue[venue] = 0
            if self.processing_lag_breach_count >= self.degraded_error_limit:
                self._set_trading_mode(
                    OMSCapabilityMode.DEGRADED,
                    f"processing_lag:{processing_lag_ms:.1f}ms>{self.max_processing_lag_ms}ms",
                )
            if self.processing_lag_breach_count >= self.passive_only_error_limit:
                self._set_trading_mode(
                    OMSCapabilityMode.PASSIVE_ONLY,
                    f"processing_lag:{processing_lag_ms:.1f}ms>{self.max_processing_lag_ms}ms",
                )
            self._log_warn(
                f"Market data processing lag {processing_lag_ms:.1f}ms > {self.max_processing_lag_ms}ms "
                f"({self.processing_lag_breach_count}/{self.consecutive_error_limit})"
            )
            if self.processing_lag_breach_count >= self.consecutive_error_limit:
                self._freeze_venue(
                    venue,
                    f"processing_lag:{processing_lag_ms:.1f}ms>{self.max_processing_lag_ms}ms",
                )
            return

        self.processing_lag_breach_count = 0
        self.processing_mode_recovery_by_venue[venue] += 1
        if self.processing_mode_recovery_by_venue[venue] >= self.venue_freeze_recovery_updates:
            self._clear_trading_mode(
                reason="processing lag recovered",
                prefixes=("processing_lag:",),
            )
            self.processing_mode_recovery_by_venue[venue] = 0
        self._recover_venue_if_stable(venue, prefix="processing_lag:")

        event_clock_offset_ms = getattr(orderbook, "clock_offset_ms", None)
        if event_clock_offset_ms is None:
            clock_offset_sec = (
                float(getattr(time_service, "offset", 0.0) or 0.0)
                / 1000.0
            )
            # Legacy and synthetic events do not carry an offset snapshot;
            # their default corrected timestamp is therefore not authoritative.
            corrected_received_ts = 0.0
        else:
            clock_offset_sec = float(event_clock_offset_ms) / 1000.0
        if exchange_ts and received_ts:
            corrected_received_ts = corrected_received_ts or (
                received_ts + clock_offset_sec
            )
            latency_ms = (corrected_received_ts - exchange_ts) * 1000.0
        elif exchange_ts:
            latency_ms = (now + clock_offset_sec - exchange_ts) * 1000.0
        elif received_ts:
            latency_ms = (now - received_ts) * 1000.0
        else:
            latency_ms = (now - orderbook.datetime.timestamp()) * 1000.0
        self.last_market_latency_ms = latency_ms
        # Negative exchange latency is a clock-domain anomaly, not zero
        # latency. Preserve its sign for telemetry and use its magnitude for
        # the circuit-breaker threshold so clock skew cannot be hidden.
        latency_for_limit = abs(latency_ms)
        if latency_for_limit > self.max_latency_ms:
            self.latency_breach_count += 1
            if symbol:
                self.latency_breach_by_symbol[symbol] += 1
                self.latency_recovery_by_symbol[symbol] = 0
            self._log_warn(
                f"Market data latency {latency_ms:.1f}ms > {self.max_latency_ms}ms "
                f"({self.latency_breach_count}/{self.consecutive_error_limit})"
            )
            if symbol and self.latency_breach_by_symbol[symbol] >= self.consecutive_error_limit:
                self._freeze_symbol(
                    symbol,
                    f"latency:{latency_ms:.1f}ms>{self.max_latency_ms}ms",
                )
            if self.latency_breach_count >= self.consecutive_error_limit and not symbol:
                self.trigger_kill_switch(
                    f"Market data latency {latency_ms:.1f}ms exceeded {self.max_latency_ms}ms "
                    f"for {self.latency_breach_count} consecutive updates"
                )
        else:
            self.latency_breach_count = 0
            if symbol:
                self.latency_breach_by_symbol[symbol] = 0
                self._recover_symbol_if_stable(symbol, prefix="latency:")

    def check_market_data_freshness(self, now: float = None):
        if not self.active or self.kill_switch_triggered:
            return False
        self._poll_external_cash_flow_truth()
        dms_healthy = self._renew_venue_dead_man_switch()
        if not dms_healthy:
            return False
        if not self.market_freshness_enabled:
            self._publish_risk_control_heartbeat("risk_live_loop")
            return True
        now = time.perf_counter() if now is None else float(now)
        if not math.isfinite(now):
            now = time.perf_counter()
        if now - self._last_freshness_poll_at < self.freshness_poll_interval_sec:
            self._publish_risk_control_heartbeat("risk_live_loop")
            return True
        self._last_freshness_poll_at = now

        oms_state = getattr(getattr(self.oms, "state", None), "value", "")
        if oms_state in {"BOOTSTRAP", "RECONCILING", "HALTED"}:
            self._publish_risk_control_heartbeat("risk_live_loop")
            return True

        for symbol in sorted(self._tracked_symbols()):
            snapshot = data_cache.get_risk_snapshot(symbol, now=now)
            reason = self._freshness_failure_reason(snapshot)
            if reason:
                self.freshness_breach_by_symbol[symbol] += 1
                self.freshness_recovery_by_symbol[symbol] = 0
                if self.freshness_breach_by_symbol[symbol] >= self.freshness_breach_checks:
                    self._freeze_symbol(symbol, reason)
                continue

            self.freshness_breach_by_symbol[symbol] = 0
            frozen_reason = self._owned_symbol_reason(
                symbol,
                prefix="stale_market_data:",
            )
            if not frozen_reason.startswith("stale_market_data:"):
                continue
            self.freshness_recovery_by_symbol[symbol] += 1
            if self.freshness_recovery_by_symbol[symbol] < self.freshness_recovery_checks:
                continue
            if self._clear_owned_symbol_freeze(
                symbol,
                frozen_reason,
                "market data freshness recovered",
            ):
                self.freshness_recovery_by_symbol[symbol] = 0
        self._publish_risk_control_heartbeat("risk_live_loop")
        return True

    def market_data_readiness_failures(self, now: float = None) -> dict[str, str]:
        """Inspect startup market-data readiness without mutating risk state."""
        if not self.market_freshness_enabled:
            return {}
        now = time.perf_counter() if now is None else float(now)
        if not math.isfinite(now):
            now = time.perf_counter()
        failures = {}
        for symbol in sorted(self._tracked_symbols()):
            reason = self._freshness_failure_reason(
                data_cache.get_risk_snapshot(symbol, now=now)
            )
            if reason:
                failures[symbol] = reason
        return failures

    def _freshness_failure_reason(self, snapshot: dict) -> str:
        try:
            mark_price = float(
                snapshot.get("mark_price", 0.0) or 0.0
            )
        except (TypeError, ValueError):
            mark_price = 0.0
        if not math.isfinite(mark_price):
            return "stale_market_data:mark_invalid"
        mark_age_ms = snapshot.get("mark_age_ms")
        if self.require_mark_price and mark_price <= 0:
            return "stale_market_data:mark_unavailable"
        if mark_price > 0 and mark_age_ms is None:
            return "stale_market_data:mark_timestamp_missing"
        if mark_age_ms is not None:
            try:
                mark_age_ms = float(mark_age_ms)
            except (TypeError, ValueError):
                return "stale_market_data:mark_timestamp_invalid"
            if not math.isfinite(mark_age_ms) or mark_age_ms < 0.0:
                return "stale_market_data:mark_timestamp_invalid"
        if (
            mark_price > 0
            and self.max_mark_age_ms > 0
            and mark_age_ms > self.max_mark_age_ms
        ):
            return f"stale_market_data:mark_age={mark_age_ms:.1f}ms"

        try:
            bid = float(snapshot.get("bid_price", 0.0) or 0.0)
            ask = float(snapshot.get("ask_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            return "stale_market_data:book_invalid"
        if not math.isfinite(bid) or not math.isfinite(ask):
            return "stale_market_data:book_invalid"
        book_age_ms = snapshot.get("book_age_ms")
        if self.require_book and (bid <= 0 or ask <= 0):
            return "stale_market_data:book_unavailable"
        if bid > 0 and ask > 0 and bid >= ask:
            return (
                "stale_market_data:book_crossed="
                f"bid={bid:.12g},ask={ask:.12g}"
            )
        if (bid > 0 or ask > 0) and book_age_ms is None:
            return "stale_market_data:book_timestamp_missing"
        if book_age_ms is not None:
            try:
                book_age_ms = float(book_age_ms)
            except (TypeError, ValueError):
                return "stale_market_data:book_timestamp_invalid"
            if not math.isfinite(book_age_ms) or book_age_ms < 0.0:
                return "stale_market_data:book_timestamp_invalid"
        if (
            (bid > 0 or ask > 0)
            and self.max_book_age_ms > 0
            and book_age_ms > self.max_book_age_ms
        ):
            return f"stale_market_data:book_age={book_age_ms:.1f}ms"
        return ""

    def _poll_external_cash_flow_truth(self):
        if not self.oms or not self.gateway:
            return
        poll = getattr(self.oms, "poll_external_cash_flow_truth", None)
        query = getattr(self.gateway, "get_income_history", None)
        if callable(poll) and callable(query):
            poll(query=query)
