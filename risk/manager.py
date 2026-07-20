import math
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from data.cache import data_cache
from event.type import (
    Event,
    OMSCapabilityMode,
    OrderRequest,
    Side,
    EVENT_ACCOUNT_UPDATE,
    EVENT_LOG,
    EVENT_MARK_PRICE,
    EVENT_ORDERBOOK,
    EVENT_ORDER_UPDATE,
)
from infrastructure.logger import logger
from oms.journal import JournalError


class RiskManager:
    RESUMABLE_KILL_STATES = frozenset(
        {
            "TRIGGERED",
            "CANCEL_PENDING",
            "CANCEL_VERIFIED",
            "FLATTENING",
            "FAILED",
        }
    )

    def __init__(self, engine, config: dict, oms=None, gateway=None):
        self.engine = engine
        self.oms = oms
        self.gateway = gateway
        self.config = config.get("risk", {})

        self.active = self.config.get("active", True)
        self.independent_supervisor_enabled = bool(
            self.config.get("independent_supervisor", {}).get("enabled", False)
        )
        self.kill_switch_triggered = False
        self.kill_reason = ""

        limits = self.config.get("limits", {})
        self.max_order_qty = limits.get("max_order_qty", 1000.0)
        self.max_order_notional = limits.get("max_order_notional", 5000.0)
        self.max_pos_notional = limits.get("max_pos_notional", 20000.0)
        self.max_account_gross_notional = limits.get("max_account_gross_notional", 0.0)
        self.max_daily_loss = limits.get("max_daily_loss", 500.0)
        self.max_drawdown_pct = limits.get("max_drawdown_pct", 0.0)

        sanity = self.config.get("price_sanity", {})
        self.max_deviation_pct = sanity.get("max_deviation_pct", 0.05)
        freshness = self.config.get("market_data_freshness", {})
        self.market_freshness_enabled = bool(freshness.get("enabled", True))
        self.require_mark_price = bool(freshness.get("require_mark_price", True))
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

        tech = self.config.get("tech_health", {})
        self.max_latency_ms = tech.get("max_latency_ms", 1000)
        self.max_processing_lag_ms = tech.get("max_processing_lag_ms", self.max_latency_ms)
        self.max_orders_per_sec = tech.get("max_order_count_per_sec", 20)
        self.consecutive_error_limit = max(1, int(tech.get("consecutive_error_limit", 10)))
        self.degraded_error_limit = max(1, int(tech.get("degraded_error_limit", 1)))
        self.passive_only_error_limit = max(
            self.degraded_error_limit,
            int(tech.get("passive_only_error_limit", max(2, self.degraded_error_limit + 1))),
        )

        black_swan = self.config.get("black_swan", {})
        self.volatility_halt_threshold = black_swan.get("volatility_halt_threshold", 0.05)
        kill_config = self.config.get("kill_switch", {})
        self.kill_verify_interval_sec = max(
            0.05,
            float(kill_config.get("verify_interval_sec", 1.0) or 1.0),
        )
        self.kill_verify_timeout_sec = max(
            self.kill_verify_interval_sec,
            float(kill_config.get("verify_timeout_sec", 30.0) or 30.0),
        )
        self.kill_flatten_retry_sec = max(
            self.kill_verify_interval_sec,
            float(kill_config.get("flatten_retry_sec", 5.0) or 5.0),
        )
        self._kill_supervisor_thread = None
        self._kill_supervisor_lock = threading.Lock()

        margin_health = self.config.get("margin_health", {})
        self.margin_health_enabled = bool(margin_health.get("enabled", True))
        self.margin_degraded_ratio = max(
            0.0,
            float(margin_health.get("degraded_ratio", 0.50) or 0.0),
        )
        self.margin_reduce_only_ratio = max(
            self.margin_degraded_ratio,
            float(margin_health.get("reduce_only_ratio", 0.70) or 0.0),
        )
        self.margin_kill_ratio = max(
            self.margin_reduce_only_ratio,
            float(margin_health.get("kill_ratio", 0.90) or 0.0),
        )
        self.margin_recovery_ratio = min(
            self.margin_degraded_ratio,
            max(0.0, float(margin_health.get("recovery_ratio", 0.40) or 0.0)),
        )
        self.margin_snapshot_max_age_sec = max(
            0.0,
            float(margin_health.get("max_snapshot_age_sec", 15.0) or 0.0),
        )
        self.margin_recovery_checks = max(
            1,
            int(margin_health.get("recovery_checks", 3) or 3),
        )
        self.margin_recovery_count = 0

        cash_flow_truth = self.config.get("cash_flow_truth", {})
        self.cash_flow_truth_enabled = bool(cash_flow_truth.get("enabled", False))
        self.cash_flow_truth_require_snapshot = bool(
            cash_flow_truth.get("require_snapshot", True)
        )
        self.cash_flow_snapshot_max_age_sec = max(
            0.0,
            float(cash_flow_truth.get("max_snapshot_age_sec", 45.0) or 0.0),
        )
        self.cash_flow_recovery_checks = max(
            1,
            int(cash_flow_truth.get("recovery_checks", 2) or 2),
        )
        self.cash_flow_recovery_count = 0

        self.order_history = deque()
        self.initial_equity = 0.0
        self.initial_external_cash_flow_total = 0.0
        self.peak_equity = 0.0
        self.risk_day = ""
        self.last_equity = 0.0
        self.kill_state = "ARMED"
        self.latency_breach_count = 0
        self.processing_lag_breach_count = 0
        self.latency_breach_by_symbol = defaultdict(int)
        self.latency_recovery_by_symbol = defaultdict(int)
        self.divergence_breach_by_symbol = defaultdict(int)
        self.divergence_recovery_by_symbol = defaultdict(int)
        self.frozen_symbols = {}
        self.frozen_venues = {}
        self.venue_recovery_by_venue = defaultdict(int)
        self.processing_mode_recovery_by_venue = defaultdict(int)
        self.symbol_freeze_recovery_updates = max(
            1,
            int(tech.get("symbol_freeze_recovery_updates", self.consecutive_error_limit)),
        )
        self.venue_freeze_recovery_updates = max(
            1,
            int(tech.get("venue_freeze_recovery_updates", self.consecutive_error_limit)),
        )
        self.max_frozen_symbols_before_kill = int(tech.get("max_frozen_symbols_before_kill", 0))

        self._restore_risk_state()

        self._register_handler(EVENT_ORDER_UPDATE, self.on_order_update)
        self._register_handler(EVENT_MARK_PRICE, self.on_mark_price)
        self._register_handler(EVENT_ACCOUNT_UPDATE, self.on_account_update)
        self._register_handler(EVENT_ORDERBOOK, self.on_orderbook)
        self._publish_risk_control_heartbeat("risk_manager_initialized")

    def _register_handler(self, event_type, handler):
        register_execution = getattr(self.engine, "register_execution", None)
        if callable(register_execution):
            register_execution(event_type, handler)
            return
        register_hot = getattr(self.engine, "register_hot", None)
        if callable(register_hot):
            register_hot(event_type, handler)
            return
        self.engine.register(event_type, handler)

    def _publish_risk_control_heartbeat(self, source: str) -> bool:
        if (
            not self.active
            or self.kill_switch_triggered
            or self.oms is None
            or self.independent_supervisor_enabled
        ):
            return False
        publish = getattr(self.oms, "record_risk_control_heartbeat", None)
        if not callable(publish):
            return False
        # Keep the producer identity stable so the OMS source allow-list does
        # not reject heartbeats merely because the risk loop changed phase.
        # The former call-site label remains available as diagnostic context.
        return bool(
            publish(
                source="risk_manager",
                healthy=True,
                reason=str(source or "risk_live_loop"),
            )
        )

    def get_status_snapshot(self) -> dict:
        account = getattr(self.oms, "account", None)
        account_equity = getattr(account, "equity", None)
        equity = float(
            self.last_equity if account_equity is None else account_equity
        )
        external_cash_flow_total = float(
            getattr(account, "external_cash_flow_total", 0.0) or 0.0
        )
        external_cash_flow_delta = (
            external_cash_flow_total - self.initial_external_cash_flow_total
        )
        adjusted_equity = equity - external_cash_flow_delta
        daily_pnl = (
            adjusted_equity - self.initial_equity
            if self.initial_equity != 0.0
            else 0.0
        )
        peak_drawdown_pct = (
            max(0.0, (self.peak_equity - adjusted_equity) / self.peak_equity)
            if self.peak_equity > 0.0
            else 0.0
        )

        margin_snapshot_time = float(
            getattr(account, "margin_snapshot_time", 0.0) or 0.0
        )
        cash_flow_snapshot_time = float(
            getattr(account, "cash_flow_snapshot_time", 0.0) or 0.0
        )
        now = time.time()
        return {
            "active": bool(self.active),
            "risk_day": self.risk_day,
            "day_start_equity": self.initial_equity,
            "equity": equity,
            "cash_flow_adjusted_equity": adjusted_equity,
            "cash_flow_adjusted_daily_pnl": daily_pnl,
            "peak_adjusted_equity": self.peak_equity,
            "peak_drawdown_pct": peak_drawdown_pct,
            "max_daily_loss": self.max_daily_loss,
            "max_drawdown_pct": self.max_drawdown_pct,
            "kill_switch_triggered": bool(self.kill_switch_triggered),
            "kill_state": self.kill_state,
            "kill_reason": self.kill_reason,
            "maintenance_margin_ratio": float(
                getattr(account, "maintenance_margin_ratio", 0.0) or 0.0
            ),
            "margin_snapshot_synced": bool(
                getattr(account, "margin_snapshot_synced", False)
            ),
            "margin_snapshot_age_sec": (
                max(0.0, now - margin_snapshot_time)
                if margin_snapshot_time > 0.0
                else None
            ),
            "cash_flow_snapshot_synced": bool(
                getattr(account, "cash_flow_snapshot_synced", False)
            ),
            "cash_flow_snapshot_age_sec": (
                max(0.0, now - cash_flow_snapshot_time)
                if cash_flow_snapshot_time > 0.0
                else None
            ),
            "frozen_symbols": dict(self.frozen_symbols),
            "frozen_venues": dict(self.frozen_venues),
        }

    def _renew_venue_dead_man_switch(self) -> bool:
        if self.oms is None:
            return True
        renew = getattr(self.oms, "renew_venue_dead_man_switch", None)
        if not callable(renew):
            return True
        return bool(renew())

    @staticmethod
    def _current_risk_day() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _restore_risk_state(self):
        journal = getattr(self.oms, "journal", None)
        if journal is None:
            return
        try:
            records = journal.load()
        except JournalError as exc:
            logger.critical(f"[Risk] Failed to restore durable risk state: {exc}")
            fail_closed = getattr(self.oms, "_fail_closed_on_journal_error", None)
            if callable(fail_closed):
                fail_closed(exc, "restore_risk_state")
            return

        latest = None
        for record in records:
            if record.get("kind") == "risk_state":
                latest = record.get("payload", {})
        if not latest:
            return

        self.risk_day = str(latest.get("risk_day", "") or "")
        self.initial_equity = float(latest.get("day_start_equity", 0.0) or 0.0)
        self.initial_external_cash_flow_total = float(
            latest.get("day_start_external_cash_flow_total", 0.0) or 0.0
        )
        self.peak_equity = float(latest.get("peak_equity", 0.0) or 0.0)
        self.last_equity = float(latest.get("last_equity", 0.0) or 0.0)
        self.kill_state = str(latest.get("kill_state", "ARMED") or "ARMED")
        self.kill_reason = str(latest.get("kill_reason", "") or "")
        self.kill_switch_triggered = bool(latest.get("kill_switch_triggered", False))

    def _persist_risk_state(self, reason: str):
        journal = getattr(self.oms, "journal", None)
        if journal is None:
            return
        try:
            journal.append(
                "risk_state",
                {
                    "risk_day": self.risk_day,
                    "day_start_equity": self.initial_equity,
                    "day_start_external_cash_flow_total": self.initial_external_cash_flow_total,
                    "peak_equity": self.peak_equity,
                    "last_equity": self.last_equity,
                    "kill_switch_triggered": self.kill_switch_triggered,
                    "kill_state": self.kill_state,
                    "kill_reason": self.kill_reason,
                    "reason": reason,
                },
            )
        except JournalError as exc:
            logger.critical(f"[Risk] Failed to persist risk state: {exc}")
            fail_closed = getattr(self.oms, "_fail_closed_on_journal_error", None)
            if callable(fail_closed):
                fail_closed(exc, "persist_risk_state")

    def _refresh_rearm_state(self):
        if not self.kill_switch_triggered or self.oms is None:
            return
        if self.kill_state != "FLAT_VERIFIED":
            return
        state = getattr(getattr(self.oms, "state", None), "value", "")
        manual_rearm_required = bool(
            getattr(self.oms, "manual_rearm_required", False)
        )
        if state != "LIVE" or manual_rearm_required:
            return
        self.kill_switch_triggered = False
        self.kill_reason = ""
        self.kill_state = "ARMED"
        self._persist_risk_state("oms_manual_rearm_completed")

    def can_operator_rearm(self) -> bool:
        return bool(
            not self.kill_switch_triggered
            or self.kill_state == "FLAT_VERIFIED"
        )

    def acknowledge_operator_rearm(self) -> bool:
        self._refresh_rearm_state()
        return not self.kill_switch_triggered

    def resume_kill_switch_supervision(self) -> bool:
        """Resume an interrupted durable kill sequence after dependencies are ready."""
        if not self.kill_switch_triggered:
            return False
        if self.kill_state in {"ARMED", "FLAT_VERIFIED"}:
            return False
        if self.kill_state not in self.RESUMABLE_KILL_STATES:
            logger.critical(
                f"[KillSwitch] Invalid recovered state {self.kill_state!r}; "
                "resuming fail-closed"
            )
            self._set_kill_state("FAILED", "invalid_recovered_kill_state")

        logger.critical(
            f"[KillSwitch] Resuming interrupted sequence from {self.kill_state}: "
            f"{self.kill_reason or 'recovered kill switch'}"
        )
        self._persist_risk_state("kill_supervision_resumed")
        if not self._verify_kill_state_once(allow_flatten=True):
            self._start_kill_supervisor()
        return True

    def check_order(self, req: OrderRequest) -> bool:
        self._refresh_rearm_state()
        if self.kill_switch_triggered:
            return False
        if not self.active:
            return True

        now = time.time()
        while self.order_history and self.order_history[0] < now - 1.0:
            self.order_history.popleft()
        if len(self.order_history) >= self.max_orders_per_sec:
            self._log_warn("Order rate limit exceeded")
            return False

        if req.volume > self.max_order_qty:
            self._log_warn(f"Order volume {req.volume} > {self.max_order_qty}")
            return False

        notional = req.price * req.volume
        if notional > self.max_order_notional:
            self._log_warn(f"Order notional {notional:.2f} > {self.max_order_notional}")
            return False

        mark_price = data_cache.get_mark_price(req.symbol)
        if mark_price > 0:
            deviation = abs(req.price - mark_price) / mark_price
            if deviation > self.max_deviation_pct:
                self._log_warn(f"Order price deviation {deviation:.2%} > {self.max_deviation_pct:.2%}")
                return False

        if self.oms:
            current_vol = self.oms.exposure.net_positions.get(req.symbol, 0.0)
            new_notional = (abs(current_vol) + req.volume) * req.price
            if new_notional > self.max_pos_notional:
                self._log_warn(f"Projected position {new_notional:.2f} > {self.max_pos_notional}")
                return False
            if self.max_account_gross_notional > 0:
                order_side = req.side if isinstance(req.side, Side) else Side(str(req.side).upper())
                gross_notional = self.oms.exposure.estimate_account_gross_notional(
                    symbol=req.symbol,
                    side=order_side,
                    volume=req.volume,
                    order_price=req.price,
                )
                if gross_notional is None:
                    self._log_warn(f"Account gross exposure unavailable for {req.symbol}")
                    return False
                if gross_notional > self.max_account_gross_notional:
                    self._log_warn(
                        f"Projected account gross exposure {gross_notional:.2f} > "
                        f"{self.max_account_gross_notional}"
                    )
                    return False
            if not self.oms.account.check_margin(notional):
                return False

        self.order_history.append(now)
        return True

    def on_mark_price(self, event: Event):
        self._refresh_rearm_state()
        if self.kill_switch_triggered or not self.active:
            return

        data = event.data
        if data.index_price <= 0 or self.volatility_halt_threshold <= 0:
            return

        symbol = getattr(data, "symbol", "").upper()
        divergence = abs(data.mark_price - data.index_price) / data.index_price
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
        exchange_ts = float(getattr(orderbook, "exchange_timestamp", 0.0) or 0.0)
        received_ts = float(getattr(orderbook, "received_timestamp", 0.0) or 0.0)
        processing_lag_ms = max(0.0, (now - received_ts) * 1000.0) if received_ts else 0.0
        if processing_lag_ms > self.max_processing_lag_ms:
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

        if exchange_ts and received_ts:
            latency_ms = max(0.0, (received_ts - exchange_ts) * 1000.0)
        elif exchange_ts:
            latency_ms = max(0.0, (now - exchange_ts) * 1000.0)
        elif received_ts:
            latency_ms = max(0.0, (now - received_ts) * 1000.0)
        else:
            latency_ms = max(0.0, (now - orderbook.datetime.timestamp()) * 1000.0)
        if latency_ms > self.max_latency_ms:
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

    def on_account_update(self, event: Event):
        self._refresh_rearm_state()
        if self.kill_switch_triggered or not self.active:
            return

        account = event.data
        if self._check_cash_flow_truth(account):
            return
        current_risk_day = self._current_risk_day()
        risk_state_changed = False
        if self.risk_day != current_risk_day:
            self.risk_day = current_risk_day
            self.initial_equity = account.equity
            self.initial_external_cash_flow_total = float(
                getattr(account, "external_cash_flow_total", 0.0) or 0.0
            )
            self.peak_equity = account.equity
            risk_state_changed = True
        elif self.initial_equity == 0:
            self.initial_equity = account.equity
            self.initial_external_cash_flow_total = float(
                getattr(account, "external_cash_flow_total", 0.0) or 0.0
            )
            self.peak_equity = account.equity
            risk_state_changed = True

        current_external_cash_flow_total = float(
            getattr(account, "external_cash_flow_total", 0.0) or 0.0
        )
        external_cash_flow_delta = (
            current_external_cash_flow_total - self.initial_external_cash_flow_total
        )
        adjusted_equity = account.equity - external_cash_flow_delta
        if adjusted_equity > self.peak_equity:
            self.peak_equity = adjusted_equity
            risk_state_changed = True
        self.last_equity = account.equity

        if risk_state_changed:
            self._persist_risk_state("account_baseline_or_peak")

        if self._check_margin_health(account):
            return

        drawdown = self.initial_equity - account.equity + external_cash_flow_delta
        if self.max_daily_loss > 0 and drawdown > self.max_daily_loss:
            self.trigger_kill_switch(f"Daily loss limit breached: -{drawdown:.2f}")
            return

        if self.max_drawdown_pct > 0 and self.peak_equity > 0:
            drawdown_pct = max(
                0.0,
                (self.peak_equity - adjusted_equity) / self.peak_equity,
            )
            if drawdown_pct > self.max_drawdown_pct:
                self.trigger_kill_switch(
                    f"Drawdown {drawdown_pct:.2%} > {self.max_drawdown_pct:.2%}"
                )

    def _check_cash_flow_truth(self, account) -> bool:
        if not self.cash_flow_truth_enabled:
            return False

        synced = bool(getattr(account, "cash_flow_snapshot_synced", False))
        snapshot_time = float(getattr(account, "cash_flow_snapshot_time", 0.0) or 0.0)
        snapshot_age_sec = (
            max(0.0, time.time() - snapshot_time)
            if snapshot_time
            else float("inf")
        )
        stale = bool(
            self.cash_flow_snapshot_max_age_sec > 0.0
            and snapshot_age_sec > self.cash_flow_snapshot_max_age_sec
        )
        if self.cash_flow_truth_require_snapshot and (not synced or stale):
            self.cash_flow_recovery_count = 0
            reason = (
                f"stale_snapshot:{snapshot_age_sec:.1f}s"
                if synced and stale
                else "snapshot_unavailable"
            )
            self._set_trading_mode(
                OMSCapabilityMode.REDUCE_ONLY,
                f"daily_pnl_truth:{reason}",
            )
            return True

        constraint_query = getattr(self.oms, "has_trading_mode_constraint", None)
        if callable(constraint_query):
            has_constraint = bool(constraint_query(("daily_pnl_truth:",)))
        else:
            active_reason = str(getattr(self.oms, "mode_override_reason", "") or "")
            has_constraint = active_reason.startswith("daily_pnl_truth:")
        if not has_constraint:
            self.cash_flow_recovery_count = 0
            return False

        self.cash_flow_recovery_count += 1
        if self.cash_flow_recovery_count < self.cash_flow_recovery_checks:
            return True
        self._clear_trading_mode(
            reason="external cash-flow truth recovered",
            prefixes=("daily_pnl_truth:",),
        )
        self.cash_flow_recovery_count = 0
        return True

    def _check_margin_health(self, account) -> bool:
        if not self.margin_health_enabled or not bool(
            getattr(account, "margin_snapshot_synced", False)
        ):
            return False

        snapshot_time = float(getattr(account, "margin_snapshot_time", 0.0) or 0.0)
        snapshot_age_sec = max(0.0, time.time() - snapshot_time) if snapshot_time else float("inf")
        if (
            self.margin_snapshot_max_age_sec > 0.0
            and snapshot_age_sec > self.margin_snapshot_max_age_sec
        ):
            self.margin_recovery_count = 0
            self._set_trading_mode(
                OMSCapabilityMode.REDUCE_ONLY,
                f"margin_health:stale_snapshot:{snapshot_age_sec:.1f}s",
            )
            return False

        ratio = float(getattr(account, "maintenance_margin_ratio", 0.0) or 0.0)
        if math.isnan(ratio) or ratio < 0.0:
            self.trigger_kill_switch(f"Invalid maintenance margin ratio: {ratio!r}")
            return True
        if ratio >= self.margin_kill_ratio:
            self.trigger_kill_switch(
                f"Maintenance margin ratio {ratio:.2%} >= kill {self.margin_kill_ratio:.2%}"
            )
            return True
        if ratio >= self.margin_reduce_only_ratio:
            self.margin_recovery_count = 0
            self._set_trading_mode(
                OMSCapabilityMode.REDUCE_ONLY,
                f"margin_health:reduce_only:{ratio:.6f}",
            )
            return False
        if ratio >= self.margin_degraded_ratio:
            self.margin_recovery_count = 0
            self._set_trading_mode(
                OMSCapabilityMode.DEGRADED,
                f"margin_health:degraded:{ratio:.6f}",
            )
            return False

        has_margin_constraint = False
        constraint_query = getattr(self.oms, "has_trading_mode_constraint", None)
        if callable(constraint_query):
            has_margin_constraint = bool(constraint_query(("margin_health:",)))
        else:
            active_reason = str(getattr(self.oms, "mode_override_reason", "") or "")
            has_margin_constraint = active_reason.startswith("margin_health:")
        if not has_margin_constraint:
            self.margin_recovery_count = 0
            return False
        if ratio > self.margin_recovery_ratio:
            self.margin_recovery_count = 0
            return False

        self.margin_recovery_count += 1
        if self.margin_recovery_count >= self.margin_recovery_checks:
            self._clear_trading_mode(
                reason=f"margin health recovered at {ratio:.2%}",
                prefixes=("margin_health:",),
            )
            self.margin_recovery_count = 0
        return False

    def check_market_data_freshness(self, now: float = None):
        if not self.active or self.kill_switch_triggered:
            return False
        self._poll_external_cash_flow_truth()
        self._renew_venue_dead_man_switch()
        if not self.market_freshness_enabled:
            self._publish_risk_control_heartbeat("risk_live_loop")
            return True
        now = time.time() if now is None else float(now)
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
            frozen_reason = self.frozen_symbols.get(symbol, "")
            if not frozen_reason.startswith("stale_market_data:"):
                continue
            self.freshness_recovery_by_symbol[symbol] += 1
            if self.freshness_recovery_by_symbol[symbol] < self.freshness_recovery_checks:
                continue
            self.frozen_symbols.pop(symbol, None)
            self.freshness_recovery_by_symbol[symbol] = 0
            if self.oms and hasattr(self.oms, "clear_symbol_freeze"):
                self.oms.clear_symbol_freeze(
                    symbol,
                    reason="market data freshness recovered",
                )
        self._publish_risk_control_heartbeat("risk_live_loop")
        return True

    def _freshness_failure_reason(self, snapshot: dict) -> str:
        mark_price = float(snapshot.get("mark_price", 0.0) or 0.0)
        mark_age_ms = snapshot.get("mark_age_ms")
        if self.require_mark_price and mark_price <= 0:
            return "stale_market_data:mark_unavailable"
        if mark_price > 0 and mark_age_ms is None:
            return "stale_market_data:mark_timestamp_missing"
        if (
            mark_price > 0
            and self.max_mark_age_ms > 0
            and float(mark_age_ms) > self.max_mark_age_ms
        ):
            return f"stale_market_data:mark_age={float(mark_age_ms):.1f}ms"

        bid = float(snapshot.get("bid_price", 0.0) or 0.0)
        ask = float(snapshot.get("ask_price", 0.0) or 0.0)
        book_age_ms = snapshot.get("book_age_ms")
        if self.require_book and (bid <= 0 or ask <= 0):
            return "stale_market_data:book_unavailable"
        if (bid > 0 or ask > 0) and book_age_ms is None:
            return "stale_market_data:book_timestamp_missing"
        if (
            (bid > 0 or ask > 0)
            and self.max_book_age_ms > 0
            and float(book_age_ms) > self.max_book_age_ms
        ):
            return f"stale_market_data:book_age={float(book_age_ms):.1f}ms"
        return ""

    def _poll_external_cash_flow_truth(self):
        if not self.oms or not self.gateway:
            return
        poll = getattr(self.oms, "poll_external_cash_flow_truth", None)
        query = getattr(self.gateway, "get_income_history", None)
        if callable(poll) and callable(query):
            poll(query=query)

    def on_order_update(self, event: Event):
        return None

    def trigger_kill_switch(self, reason: str):
        if self.kill_switch_triggered:
            return

        self.kill_switch_triggered = True
        self.kill_reason = reason
        self.kill_state = "TRIGGERED"
        self._persist_risk_state("kill_triggered")
        logger.critical(f"KILL SWITCH TRIGGERED: {reason}")

        if self.gateway:
            symbols = set()
            if self.oms:
                symbols.update(self.oms.config.get("symbols", []))
                symbols.update(self.oms.exposure.net_positions.keys())
            for symbol in symbols:
                try:
                    self.gateway.cancel_all_orders(symbol)
                except Exception as exc:
                    logger.error(f"[KillSwitch] cancel_all_orders({symbol}) failed: {exc}")
            self._set_kill_state("CANCEL_PENDING", "mass_cancel_requested")

        if self.oms:
            try:
                self.oms.halt_system(f"KillSwitch: {reason}")
            except Exception as exc:
                logger.error(f"[KillSwitch] oms.halt_system failed: {exc}")

        if not self._verify_kill_state_once(allow_flatten=True):
            self._start_kill_supervisor()

    def _set_kill_state(self, state: str, reason: str):
        if self.kill_state == state:
            return
        self.kill_state = state
        self._persist_risk_state(reason)
        logger.warning(f"[KillSwitch] State -> {state}: {reason}")

    def _start_kill_supervisor(self):
        with self._kill_supervisor_lock:
            if self._kill_supervisor_thread and self._kill_supervisor_thread.is_alive():
                return
            self._kill_supervisor_thread = threading.Thread(
                target=self._supervise_kill_switch,
                daemon=True,
                name="RiskKillSupervisor",
            )
            self._kill_supervisor_thread.start()

    def _supervise_kill_switch(self):
        deadline = time.monotonic() + self.kill_verify_timeout_sec
        next_flatten_at = time.monotonic() + self.kill_flatten_retry_sec
        while self.kill_switch_triggered and time.monotonic() < deadline:
            now = time.monotonic()
            allow_flatten = now >= next_flatten_at
            if self._verify_kill_state_once(allow_flatten=allow_flatten):
                return
            if allow_flatten:
                next_flatten_at = now + self.kill_flatten_retry_sec
            time.sleep(self.kill_verify_interval_sec)

        if self.kill_switch_triggered and self.kill_state != "FLAT_VERIFIED":
            self._set_kill_state("FAILED", "kill_verification_timeout")
            logger.critical(
                f"[KillSwitch] Failed to verify flat state within "
                f"{self.kill_verify_timeout_sec:.1f}s"
            )

    def _verify_kill_state_once(self, allow_flatten: bool) -> bool:
        open_orders = self._query_kill_open_orders()
        if open_orders is None:
            return False
        if open_orders:
            self._set_kill_state("CANCEL_PENDING", "open_orders_remain")
            if allow_flatten:
                self._retry_mass_cancel()
            return False

        self._set_kill_state("CANCEL_VERIFIED", "no_open_orders")
        positions = self._query_kill_positions()
        if positions is None:
            return False
        if not positions:
            self._set_kill_state("FLAT_VERIFIED", "orders_cancelled_and_positions_flat")
            return True

        self._set_kill_state("FLATTENING", "nonzero_positions_remain")
        if allow_flatten and self.oms and hasattr(self.oms, "emergency_reduce_only_flatten"):
            try:
                self.oms.emergency_reduce_only_flatten(f"KillSwitch: {self.kill_reason}")
            except Exception as exc:
                logger.error(f"[KillSwitch] emergency flatten failed: {exc}")
        return False

    def _query_kill_open_orders(self):
        query = getattr(self.gateway, "get_open_orders", None)
        if not callable(query):
            return None
        try:
            return query()
        except Exception as exc:
            logger.error(f"[KillSwitch] open-order verification failed: {exc}")
            return None

    def _query_kill_positions(self):
        query = getattr(self.gateway, "get_all_positions", None)
        if not callable(query):
            return None
        try:
            remote_positions = query()
        except Exception as exc:
            logger.error(f"[KillSwitch] position verification failed: {exc}")
            return None
        if remote_positions is None:
            return None

        nonzero = {
            str(payload.get("symbol", "") or "").upper()
            for payload in remote_positions
            if abs(float(payload.get("positionAmt", 0.0) or 0.0)) > 1e-9
        }
        if self.oms:
            nonzero.update(
                str(symbol).upper()
                for symbol, volume in getattr(self.oms.exposure, "net_positions", {}).items()
                if abs(float(volume or 0.0)) > 1e-9
            )
        return nonzero

    def _retry_mass_cancel(self):
        if not self.gateway:
            return
        symbols = set(self._tracked_symbols())
        for symbol in symbols:
            try:
                self.gateway.cancel_all_orders(symbol)
            except Exception as exc:
                logger.error(f"[KillSwitch] cancel retry failed for {symbol}: {exc}")

    def _freeze_symbol(self, symbol: str, reason: str):
        if not symbol:
            return

        symbol = symbol.upper()
        existing_reason = self.frozen_symbols.get(symbol, "")
        self.frozen_symbols[symbol] = reason
        if existing_reason == reason:
            return

        logger.error(f"[Risk] Symbol circuit breaker {symbol}: {reason}")
        self._log_warn(f"Symbol frozen {symbol}: {reason}")
        if self.oms and hasattr(self.oms, "freeze_symbol"):
            try:
                self.oms.freeze_symbol(symbol, reason, cancel_active_orders=True)
            except Exception as exc:
                logger.error(f"[Risk] oms.freeze_symbol({symbol}) failed: {exc}")
        self._maybe_escalate_symbol_freeze(reason)

    def _recover_symbol_if_stable(self, symbol: str, prefix: str):
        frozen_reason = self.frozen_symbols.get(symbol, "")
        if not frozen_reason.startswith(prefix):
            return

        if prefix == "latency:":
            self.latency_recovery_by_symbol[symbol] += 1
            stable_updates = self.latency_recovery_by_symbol[symbol]
        else:
            self.divergence_recovery_by_symbol[symbol] += 1
            stable_updates = self.divergence_recovery_by_symbol[symbol]

        if stable_updates < self.symbol_freeze_recovery_updates:
            return

        self.frozen_symbols.pop(symbol, None)
        logger.info(f"[Risk] Symbol circuit breaker cleared {symbol}: {prefix}recovered")
        self._log_warn(f"Symbol restored {symbol}: {prefix}recovered")
        if self.oms and hasattr(self.oms, "clear_symbol_freeze"):
            try:
                self.oms.clear_symbol_freeze(
                    symbol,
                    reason=f"{prefix.rstrip(':')} recovered after {stable_updates} healthy updates",
                )
            except Exception as exc:
                logger.error(f"[Risk] oms.clear_symbol_freeze({symbol}) failed: {exc}")

        self.latency_recovery_by_symbol[symbol] = 0
        self.divergence_recovery_by_symbol[symbol] = 0

    def _current_venue(self):
        venue = ""
        if self.gateway:
            venue = getattr(self.gateway, "gateway_name", "") or venue
        if not venue and self.oms:
            venue = getattr(getattr(self.oms, "gateway", None), "gateway_name", "") or venue
        return (venue or "UNKNOWN").upper()

    def _freeze_venue(self, venue: str, reason: str):
        if not venue:
            return

        venue = venue.upper()
        existing_reason = self.frozen_venues.get(venue, "")
        self.frozen_venues[venue] = reason
        self.venue_recovery_by_venue[venue] = 0
        if existing_reason == reason:
            return

        logger.error(f"[Risk] Venue circuit breaker {venue}: {reason}")
        self._log_warn(f"Venue frozen {venue}: {reason}")
        if self.oms and hasattr(self.oms, "freeze_venue"):
            try:
                self.oms.freeze_venue(venue, reason, cancel_active_orders=False)
            except Exception as exc:
                logger.error(f"[Risk] oms.freeze_venue({venue}) failed: {exc}")

    def _recover_venue_if_stable(self, venue: str, prefix: str):
        venue = (venue or "UNKNOWN").upper()
        frozen_reason = self.frozen_venues.get(venue, "")
        if not frozen_reason.startswith(prefix):
            return

        self.venue_recovery_by_venue[venue] += 1
        stable_updates = self.venue_recovery_by_venue[venue]
        if stable_updates < self.venue_freeze_recovery_updates:
            return

        self.frozen_venues.pop(venue, None)
        self.venue_recovery_by_venue[venue] = 0
        logger.info(f"[Risk] Venue circuit breaker cleared {venue}: {prefix}recovered")
        self._log_warn(f"Venue restored {venue}: {prefix}recovered")
        if self.oms and hasattr(self.oms, "clear_venue_freeze"):
            try:
                self.oms.clear_venue_freeze(
                    venue,
                    reason=f"{prefix.rstrip(':')} recovered after {stable_updates} healthy updates",
                )
            except Exception as exc:
                logger.error(f"[Risk] oms.clear_venue_freeze({venue}) failed: {exc}")

    def _set_trading_mode(self, mode: OMSCapabilityMode, reason: str):
        if self.oms and hasattr(self.oms, "set_trading_mode"):
            try:
                self.oms.set_trading_mode(mode, reason)
            except Exception as exc:
                logger.error(f"[Risk] oms.set_trading_mode({mode.value}) failed: {exc}")

    def _clear_trading_mode(self, reason: str = "", prefixes=()):
        if self.oms and hasattr(self.oms, "clear_trading_mode"):
            try:
                self.oms.clear_trading_mode(reason=reason, prefixes=prefixes)
            except Exception as exc:
                logger.error(f"[Risk] oms.clear_trading_mode failed: {exc}")

    def _tracked_symbols(self):
        symbols = set(self.frozen_symbols.keys())
        if self.oms:
            symbols.update(self.oms.config.get("symbols", []))
            symbols.update(getattr(self.oms.exposure, "net_positions", {}).keys())
        return {symbol for symbol in symbols if symbol}

    def _maybe_escalate_symbol_freeze(self, trigger_reason: str):
        tracked_symbols = self._tracked_symbols()
        if not tracked_symbols:
            return

        threshold = self.max_frozen_symbols_before_kill
        if threshold <= 0:
            threshold = len(tracked_symbols) if len(tracked_symbols) > 1 else 0
        if threshold <= 0:
            return

        frozen_count = len({symbol for symbol in tracked_symbols if symbol in self.frozen_symbols})
        if frozen_count >= threshold:
            self.trigger_kill_switch(
                f"Symbol circuit breakers exhausted ({frozen_count}/{len(tracked_symbols)}): {trigger_reason}"
            )

    def _log_warn(self, msg: str):
        self.engine.put(Event(EVENT_LOG, f"[Risk] {msg}"))
