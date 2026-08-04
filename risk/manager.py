import math
import time
from collections import deque
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
from infrastructure.oms_risk_port import RiskOMSPort
from infrastructure.time_service import time_service
from risk.account_risk import (
    AccountRiskController,
    AccountRiskField,
    AccountRiskMethod,
)
from risk.deployment_loss import deployment_policy_fingerprint
from risk.funding_guard import (
    FundingGuardDecision,
    FundingGuardPolicy,
    FundingGuardState,
)
from risk.funding_controller import FundingRiskController
from risk.limit_contract import (
    DEFAULT_MAX_ACCOUNT_GROSS_NOTIONAL,
    DEFAULT_MAX_DAILY_LOSS,
    DEFAULT_MAX_DRAWDOWN_PCT,
)
from risk.kill_switch import (
    KillSwitchConfig,
    KillSwitchField,
    KillSwitchMethod,
    RiskKillSwitchController,
)
from risk.market_risk import (
    MarketRiskController,
    MarketRiskField,
    MarketRiskMethod,
)
from risk.scope_guards import (
    RiskScopeGuardController,
    ScopeGuardField,
    ScopeGuardMethod,
)
from risk.state_repository import (
    RESUMABLE_KILL_STATES,
    VALID_KILL_STATES,
    RiskStateField,
    RiskStateRepository,
)
from risk.venue_dms import VenueDMSController


class RiskManager:
    RESUMABLE_KILL_STATES = RESUMABLE_KILL_STATES
    VALID_KILL_STATES = VALID_KILL_STATES

    # Compatibility facade for existing callers. Each field is declared
    # explicitly; the values live only in RiskStateRepository.state.
    risk_day = RiskStateField("risk_day")
    initial_equity = RiskStateField("initial_equity")
    initial_external_cash_flow_total = RiskStateField(
        "initial_external_cash_flow_total"
    )
    peak_equity = RiskStateField("peak_equity")
    last_equity = RiskStateField("last_equity")
    deployment_id = RiskStateField("deployment_id")
    deployment_policy_fingerprint = RiskStateField(
        "deployment_policy_fingerprint"
    )
    deployment_start_equity = RiskStateField("deployment_start_equity")
    deployment_start_external_cash_flow_total = RiskStateField(
        "deployment_start_external_cash_flow_total"
    )
    deployment_adjusted_equity = RiskStateField(
        "deployment_adjusted_equity"
    )
    deployment_loss = RiskStateField("deployment_loss")
    kill_switch_triggered = RiskStateField("kill_switch_triggered")
    kill_state = RiskStateField("kill_state")
    kill_reason = RiskStateField("kill_reason")

    _kill_empty_order_snapshots = KillSwitchField(
        "_kill_empty_order_snapshots"
    )
    _kill_empty_flat_snapshots = KillSwitchField(
        "_kill_empty_flat_snapshots"
    )
    _kill_last_accepted_snapshot_at = KillSwitchField(
        "_kill_last_accepted_snapshot_at"
    )
    _kill_state_lock = KillSwitchField("_kill_state_lock")
    _kill_verification_lock = KillSwitchField("_kill_verification_lock")
    _kill_supervisor_thread = KillSwitchField("_kill_supervisor_thread")
    _kill_supervisor_lock = KillSwitchField("_kill_supervisor_lock")

    _refresh_rearm_state = KillSwitchMethod()
    can_operator_rearm = KillSwitchMethod()
    acknowledge_operator_rearm = KillSwitchMethod()
    resume_kill_switch_supervision = KillSwitchMethod()
    trigger_kill_switch = KillSwitchMethod()
    restart_kill_switch_after_truth_drift = KillSwitchMethod()
    _set_kill_state = KillSwitchMethod()
    _start_kill_supervisor = KillSwitchMethod()
    _supervise_kill_switch = KillSwitchMethod()
    _verify_kill_state_safely = KillSwitchMethod()
    _verify_kill_state_once = KillSwitchMethod()
    _reset_kill_empty_snapshots = KillSwitchMethod()
    _query_local_kill_order_truth = KillSwitchMethod()
    _query_kill_open_orders = KillSwitchMethod()
    _query_kill_positions = KillSwitchMethod()
    _collect_nonzero_kill_positions = KillSwitchMethod()
    _collect_kill_cancel_symbols = KillSwitchMethod()
    _retry_mass_cancel = KillSwitchMethod()

    latency_recovery_by_symbol = ScopeGuardField(
        "latency_recovery_by_symbol"
    )
    divergence_recovery_by_symbol = ScopeGuardField(
        "divergence_recovery_by_symbol"
    )
    frozen_symbols = ScopeGuardField("frozen_symbols")
    symbol_freeze_epochs = ScopeGuardField("symbol_freeze_epochs")
    symbol_freeze_owners = ScopeGuardField("symbol_freeze_owners")
    frozen_venues = ScopeGuardField("frozen_venues")
    venue_freeze_epochs = ScopeGuardField("venue_freeze_epochs")
    venue_recovery_by_venue = ScopeGuardField("venue_recovery_by_venue")

    _risk_symbol_guard_owner = ScopeGuardMethod()
    _refresh_local_symbol_guard = ScopeGuardMethod()
    _owned_symbol_reason = ScopeGuardMethod()
    _freeze_symbol = ScopeGuardMethod()
    _clear_owned_symbol_freeze = ScopeGuardMethod()
    _recover_symbol_if_stable = ScopeGuardMethod()
    _current_venue = ScopeGuardMethod()
    _freeze_venue = ScopeGuardMethod()
    _recover_venue_if_stable = ScopeGuardMethod()
    _maybe_escalate_symbol_freeze = ScopeGuardMethod()

    market_freshness_enabled = MarketRiskField("market_freshness_enabled")
    require_mark_price = MarketRiskField("require_mark_price")
    require_book = MarketRiskField("require_book")
    max_mark_age_ms = MarketRiskField("max_mark_age_ms")
    max_book_age_ms = MarketRiskField("max_book_age_ms")
    freshness_poll_interval_sec = MarketRiskField(
        "freshness_poll_interval_sec"
    )
    freshness_breach_checks = MarketRiskField("freshness_breach_checks")
    freshness_recovery_checks = MarketRiskField(
        "freshness_recovery_checks"
    )
    _last_freshness_poll_at = MarketRiskField(
        "_last_freshness_poll_at"
    )
    freshness_breach_by_symbol = MarketRiskField(
        "freshness_breach_by_symbol"
    )
    freshness_recovery_by_symbol = MarketRiskField(
        "freshness_recovery_by_symbol"
    )
    max_latency_ms = MarketRiskField("max_latency_ms")
    max_processing_lag_ms = MarketRiskField("max_processing_lag_ms")
    consecutive_error_limit = MarketRiskField("consecutive_error_limit")
    degraded_error_limit = MarketRiskField("degraded_error_limit")
    passive_only_error_limit = MarketRiskField("passive_only_error_limit")
    volatility_halt_threshold = MarketRiskField(
        "volatility_halt_threshold"
    )
    latency_breach_count = MarketRiskField("latency_breach_count")
    processing_lag_breach_count = MarketRiskField(
        "processing_lag_breach_count"
    )
    last_market_latency_ms = MarketRiskField("last_market_latency_ms")
    last_processing_lag_ms = MarketRiskField("last_processing_lag_ms")
    last_gateway_dispatch_lag_ms = MarketRiskField(
        "last_gateway_dispatch_lag_ms"
    )
    latency_breach_by_symbol = MarketRiskField(
        "latency_breach_by_symbol"
    )
    divergence_breach_by_symbol = MarketRiskField(
        "divergence_breach_by_symbol"
    )
    processing_mode_recovery_by_venue = MarketRiskField(
        "processing_mode_recovery_by_venue"
    )

    on_mark_price = MarketRiskMethod()
    on_orderbook = MarketRiskMethod()
    check_market_data_freshness = MarketRiskMethod()
    market_data_readiness_failures = MarketRiskMethod()
    _freshness_failure_reason = MarketRiskMethod()
    _poll_external_cash_flow_truth = MarketRiskMethod()

    margin_health_enabled = AccountRiskField("margin_health_enabled")
    margin_degraded_ratio = AccountRiskField("margin_degraded_ratio")
    margin_reduce_only_ratio = AccountRiskField("margin_reduce_only_ratio")
    margin_kill_ratio = AccountRiskField("margin_kill_ratio")
    margin_recovery_ratio = AccountRiskField("margin_recovery_ratio")
    margin_snapshot_max_age_sec = AccountRiskField(
        "margin_snapshot_max_age_sec"
    )
    margin_recovery_checks = AccountRiskField("margin_recovery_checks")
    margin_recovery_count = AccountRiskField("margin_recovery_count")
    cash_flow_truth_enabled = AccountRiskField("cash_flow_truth_enabled")
    cash_flow_truth_require_snapshot = AccountRiskField(
        "cash_flow_truth_require_snapshot"
    )
    cash_flow_snapshot_max_age_sec = AccountRiskField(
        "cash_flow_snapshot_max_age_sec"
    )
    cash_flow_recovery_checks = AccountRiskField(
        "cash_flow_recovery_checks"
    )
    cash_flow_recovery_count = AccountRiskField("cash_flow_recovery_count")

    on_account_update = AccountRiskMethod()
    _check_cash_flow_truth = AccountRiskMethod()
    _check_margin_health = AccountRiskMethod()

    def __init__(
        self,
        engine,
        config: dict,
        oms: RiskOMSPort | None = None,
        gateway=None,
    ):
        self.engine = engine
        self.oms = oms
        self.gateway = gateway
        self.root_config = config
        self.config = config.get("risk", {})

        self.active = self.config.get("active", True)
        self.independent_supervisor_enabled = bool(
            self.config.get("independent_supervisor", {}).get("enabled", False)
        )
        self.venue_dms = VenueDMSController(
            root_config=config,
            oms=oms,
            logger=logger,
        )

        limits = self.config.get("limits", {})
        self.max_order_qty = limits.get("max_order_qty", 1000.0)
        self.max_order_notional = limits.get("max_order_notional", 5000.0)
        self.max_pos_notional = limits.get("max_pos_notional", 20000.0)
        self.max_account_gross_notional = limits.get(
            "max_account_gross_notional",
            DEFAULT_MAX_ACCOUNT_GROSS_NOTIONAL,
        )
        self.max_daily_loss = limits.get(
            "max_daily_loss",
            DEFAULT_MAX_DAILY_LOSS,
        )
        self.max_drawdown_pct = limits.get(
            "max_drawdown_pct",
            DEFAULT_MAX_DRAWDOWN_PCT,
        )
        live_launch = config.get("live_launch", {}) or {}
        deployment_id = str(
            live_launch.get("deployment_id", "") or ""
        ).strip()
        self.declared_account_equity = max(
            0.0,
            float(
                live_launch.get("declared_account_equity_usdt", 0.0)
                or 0.0
            ),
        )
        self.max_deployed_capital = max(
            0.0,
            float(
                live_launch.get("max_deployed_capital_usdt", 0.0)
                or 0.0
            ),
        )
        self.max_deployment_loss = max(
            0.0,
            float(
                live_launch.get("max_deployment_loss_usdt", 0.0)
                or 0.0
            ),
        )
        self.deployment_loss_reduce_only_fraction = min(
            1.0,
            max(
                0.0,
                float(
                    live_launch.get(
                        "deployment_loss_reduce_only_fraction",
                        0.80,
                    )
                    or 0.0
                ),
            ),
        )
        deployment_policy_fingerprint_value = deployment_policy_fingerprint(
            deployment_id=deployment_id,
            symbols=config.get("symbols", []),
            declared_account_equity=self.declared_account_equity,
            max_deployed_capital=self.max_deployed_capital,
            maximum_loss=self.max_deployment_loss,
            reduce_only_fraction=self.deployment_loss_reduce_only_fraction,
        )
        durability_failure_handler = getattr(
            oms,
            "handle_durability_failure",
            None,
        )
        halt_handler = getattr(oms, "halt_system", None)
        self.risk_state_repository = RiskStateRepository(
            journal=getattr(oms, "journal", None),
            deployment_id=deployment_id,
            deployment_policy_fingerprint=(
                deployment_policy_fingerprint_value
            ),
            durability_failure_handler=(
                durability_failure_handler
                if callable(durability_failure_handler)
                else None
            ),
            halt_handler=halt_handler if callable(halt_handler) else None,
            logger=logger,
        )

        sanity = self.config.get("price_sanity", {})
        self.max_deviation_pct = sanity.get("max_deviation_pct", 0.05)

        self.funding_guard = FundingRiskController(
            root_config=config,
            risk_config=self.config,
            oms=oms,
            set_trading_mode=self._set_trading_mode,
            clear_trading_mode=self._clear_trading_mode,
            tracked_symbols=self._tracked_symbols,
            reduce_only_mode=OMSCapabilityMode.REDUCE_ONLY,
        )

        tech = self.config.get("tech_health", {})
        self.max_orders_per_sec = tech.get("max_order_count_per_sec", 20)
        consecutive_error_limit = max(
            1,
            int(tech.get("consecutive_error_limit", 10)),
        )
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
        self.kill_empty_snapshots_required = max(
            2,
            int(kill_config.get("empty_snapshots_required", 2) or 2),
        )

        self.order_history = deque()
        self.symbol_freeze_recovery_updates = max(
            1,
            int(
                tech.get(
                    "symbol_freeze_recovery_updates",
                    consecutive_error_limit,
                )
            ),
        )
        self.venue_freeze_recovery_updates = max(
            1,
            int(
                tech.get(
                    "venue_freeze_recovery_updates",
                    consecutive_error_limit,
                )
            ),
        )
        self.max_frozen_symbols_before_kill = int(tech.get("max_frozen_symbols_before_kill", 0))

        self.scope_guards = RiskScopeGuardController(
            oms=oms,
            gateway=gateway,
            log_warn=self._log_warn,
            trigger_kill_switch=(
                lambda reason: self.trigger_kill_switch(reason)
            ),
            tracked_symbols=self._tracked_symbols,
            symbol_recovery_updates=self.symbol_freeze_recovery_updates,
            venue_recovery_updates=self.venue_freeze_recovery_updates,
            max_frozen_symbols_before_kill=(
                self.max_frozen_symbols_before_kill
            ),
        )
        self.kill_switch = RiskKillSwitchController(
            oms=oms,
            gateway=gateway,
            risk_state_repository=self.risk_state_repository,
            root_config=config,
            frozen_symbols=self.frozen_symbols,
            config=KillSwitchConfig(
                verify_interval_sec=self.kill_verify_interval_sec,
                verify_timeout_sec=self.kill_verify_timeout_sec,
                flatten_retry_sec=self.kill_flatten_retry_sec,
                empty_snapshots_required=self.kill_empty_snapshots_required,
            ),
        )
        self.account_risk = AccountRiskController(
            risk_config=self.config,
            active=bool(self.active),
            oms=oms,
            risk_state_repository=self.risk_state_repository,
            max_daily_loss=self.max_daily_loss,
            max_drawdown_pct=self.max_drawdown_pct,
            max_deployed_capital=self.max_deployed_capital,
            max_deployment_loss=self.max_deployment_loss,
            deployment_loss_reduce_only_fraction=(
                self.deployment_loss_reduce_only_fraction
            ),
            refresh_rearm_state=(lambda: self._refresh_rearm_state()),
            is_killed=(lambda: self.kill_switch_triggered),
            trigger_kill_switch=(
                lambda reason: self.trigger_kill_switch(reason)
            ),
            current_risk_day=self._current_risk_day,
            set_trading_mode=self._set_trading_mode,
            clear_trading_mode=self._clear_trading_mode,
        )
        self.market_risk = MarketRiskController(
            risk_config=self.config,
            active=bool(self.active),
            oms=oms,
            gateway=gateway,
            funding_guard=self.funding_guard,
            refresh_rearm_state=(lambda: self._refresh_rearm_state()),
            is_killed=(lambda: self.kill_switch_triggered),
            trigger_kill_switch=(
                lambda reason: self.trigger_kill_switch(reason)
            ),
            freeze_symbol=self._freeze_symbol,
            recover_symbol_if_stable=self._recover_symbol_if_stable,
            owned_symbol_reason=self._owned_symbol_reason,
            clear_owned_symbol_freeze=self._clear_owned_symbol_freeze,
            current_venue=self._current_venue,
            freeze_venue=self._freeze_venue,
            recover_venue_if_stable=self._recover_venue_if_stable,
            set_trading_mode=self._set_trading_mode,
            clear_trading_mode=self._clear_trading_mode,
            renew_venue_dead_man_switch=self._renew_venue_dead_man_switch,
            publish_risk_control_heartbeat=(
                self._publish_risk_control_heartbeat
            ),
            tracked_symbols=self._tracked_symbols,
            log_warn=self._log_warn,
            latency_recovery_by_symbol=self.latency_recovery_by_symbol,
            divergence_recovery_by_symbol=(
                self.divergence_recovery_by_symbol
            ),
            venue_recovery_by_venue=self.venue_recovery_by_venue,
            venue_freeze_recovery_updates=(
                self.venue_freeze_recovery_updates
            ),
        )

        self.risk_state_repository.restore()

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
        venue_dms_status = self.venue_dms.status_snapshot()
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
            "deployment_id": self.deployment_id,
            "deployment_start_equity": self.deployment_start_equity,
            "deployment_adjusted_equity": self.deployment_adjusted_equity,
            "deployment_loss": self.deployment_loss,
            "max_deployment_loss": self.max_deployment_loss,
            "declared_account_equity": self.declared_account_equity,
            "max_deployed_capital": self.max_deployed_capital,
            "deployment_policy_fingerprint": (
                self.deployment_policy_fingerprint
            ),
            "kill_switch_triggered": bool(self.kill_switch_triggered),
            "kill_state": self.kill_state,
            "kill_reason": self.kill_reason,
            **venue_dms_status,
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
            "frozen_symbol_epochs": dict(self.symbol_freeze_epochs),
            "frozen_symbol_owners": {
                symbol: {
                    owner: dict(record)
                    for owner, record in owners.items()
                }
                for symbol, owners in self.symbol_freeze_owners.items()
            },
            "frozen_venues": dict(self.frozen_venues),
            "frozen_venue_epochs": dict(self.venue_freeze_epochs),
            "market_latency_ms": self.last_market_latency_ms,
            "processing_lag_ms": self.last_processing_lag_ms,
            "gateway_dispatch_lag_ms": self.last_gateway_dispatch_lag_ms,
            "exchange_clock_offset_ms": float(
                getattr(time_service, "offset", 0.0) or 0.0
            ),
            "funding_guard": self.funding_guard.status_snapshot(),
        }

    def set_venue_dms_supervisor_health(self, healthy: bool) -> None:
        self.venue_dms.set_supervisor_health(healthy)

    def _latch_venue_dead_man_switch_failure(self, reason: str) -> bool:
        return self.venue_dms.latch_failure(reason)

    def _renew_venue_dead_man_switch(self) -> bool:
        return self.venue_dms.renew(
            active=self.active,
            kill_switch_triggered=self.kill_switch_triggered,
        )

    # Read-only compatibility for existing operator instrumentation. Runtime
    # state is owned solely by VenueDMSController.
    @property
    def _venue_dms_renewal_authorized(self) -> bool:
        return self.venue_dms.renewal_authorized

    @property
    def _venue_dms_supervisor_healthy(self) -> bool:
        return self.venue_dms.supervisor_healthy

    @property
    def _venue_dms_failure_reason(self) -> str:
        return self.venue_dms.failure_reason

    @property
    def _last_venue_dms_renewal_result(self):
        return self.venue_dms.last_renewal_result

    @property
    def funding_guard_policy(self) -> FundingGuardPolicy:
        return self.funding_guard.policy

    @property
    def funding_guard_lock(self):
        return self.funding_guard.lock

    @property
    def funding_guard_states(self) -> dict[str, FundingGuardState]:
        return self.funding_guard.states

    @property
    def funding_guard_decisions(self) -> dict[str, FundingGuardDecision]:
        return self.funding_guard.decisions

    @staticmethod
    def _current_risk_day() -> str:
        return datetime.now(timezone.utc).date().isoformat()


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

        try:
            price = float(req.price)
            volume = float(req.volume)
        except (TypeError, ValueError):
            self._log_warn("Order price or volume is not numeric")
            return False
        if (
            not math.isfinite(price)
            or price <= 0.0
            or not math.isfinite(volume)
            or volume <= 0.0
        ):
            self._log_warn("Order price or volume is not finite and positive")
            return False

        if volume > self.max_order_qty:
            self._log_warn(f"Order volume {req.volume} > {self.max_order_qty}")
            return False

        notional = price * volume
        if not math.isfinite(notional) or notional > self.max_order_notional:
            self._log_warn(f"Order notional {notional:.2f} > {self.max_order_notional}")
            return False

        mark_price = data_cache.get_mark_price(req.symbol)
        if mark_price > 0:
            deviation = abs(price - mark_price) / mark_price
            if deviation > self.max_deviation_pct:
                self._log_warn(f"Order price deviation {deviation:.2%} > {self.max_deviation_pct:.2%}")
                return False

        if self.oms:
            current_vol = self.oms.exposure.net_positions.get(req.symbol, 0.0)
            if not math.isfinite(float(current_vol)):
                self._log_warn(
                    f"Current position is non-finite for {req.symbol}"
                )
                return False
            new_notional = (abs(current_vol) + volume) * price
            if new_notional > self.max_pos_notional:
                self._log_warn(f"Projected position {new_notional:.2f} > {self.max_pos_notional}")
                return False
            if self.max_account_gross_notional > 0:
                order_side = req.side if isinstance(req.side, Side) else Side(str(req.side).upper())
                gross_notional = self.oms.exposure.estimate_account_gross_notional(
                    symbol=req.symbol,
                    side=order_side,
                    volume=volume,
                    order_price=price,
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


    def check_funding_guard(self, now: float = None) -> bool:
        return self.funding_guard.check(
            active=self.active,
            kill_switch_triggered=self.kill_switch_triggered,
            now=now,
        )



    def on_order_update(self, event: Event):
        return None


    def _set_trading_mode(self, mode: OMSCapabilityMode, reason: str):
        if self.oms and hasattr(self.oms, "set_trading_mode"):
            try:
                self.oms.set_trading_mode(mode, reason)
            except Exception as exc:
                logger.error(f"[Risk] oms.set_trading_mode({mode.value}) failed: {exc}")

    def _clear_trading_mode(
        self,
        reason: str = "",
        prefixes=(),
        *,
        expected_generations=None,
    ) -> bool:
        if self.oms and hasattr(self.oms, "clear_trading_mode"):
            try:
                kwargs = {
                    "reason": reason,
                    "prefixes": prefixes,
                }
                if expected_generations is not None:
                    kwargs["expected_generations"] = expected_generations
                return bool(
                    self.oms.clear_trading_mode(**kwargs)
                )
            except Exception as exc:
                logger.error(f"[Risk] oms.clear_trading_mode failed: {exc}")
        return False

    def _tracked_symbols(self):
        symbols = set(self.frozen_symbols.keys())
        if self.oms:
            symbols.update(self.oms.config.get("symbols", []))
            symbols.update(getattr(self.oms.exposure, "net_positions", {}).keys())
        return {symbol for symbol in symbols if symbol}


    def _log_warn(self, msg: str):
        self.engine.put(Event(EVENT_LOG, f"[Risk] {msg}"))
