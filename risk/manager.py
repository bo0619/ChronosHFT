import time
from datetime import datetime, timezone

from event.type import (
    EVENT_ACCOUNT_UPDATE,
    EVENT_LOG,
    EVENT_MARK_PRICE,
    EVENT_ORDERBOOK,
    Event,
    OMSCapabilityMode,
)
from infrastructure.logger import logger
from infrastructure.oms_risk_port import RiskOMSPort
from infrastructure.time_service import time_service
from risk.account_risk import AccountRiskController
from risk.deployment_loss import deployment_policy_fingerprint
from risk.funding_controller import FundingRiskController
from risk.funding_guard import (
    FundingGuardPolicy,
)
from risk.kill_switch import (
    KillSwitchConfig,
    KillSwitchMethod,
    RiskKillSwitchController,
)
from risk.limit_contract import (
    DEFAULT_MAX_DAILY_LOSS,
    DEFAULT_MAX_DRAWDOWN_PCT,
)
from risk.market_risk import (
    MarketRiskController,
    MarketRiskMethod,
)
from risk.scope_guards import RiskScopeGuardController
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

    can_operator_rearm = KillSwitchMethod()
    acknowledge_operator_rearm = KillSwitchMethod()
    resume_kill_switch_supervision = KillSwitchMethod()
    trigger_kill_switch = KillSwitchMethod()
    restart_kill_switch_after_truth_drift = KillSwitchMethod()

    check_market_data_freshness = MarketRiskMethod()
    market_data_readiness_failures = MarketRiskMethod()

    def __init__(
        self,
        engine,
        config: dict,
        oms: RiskOMSPort | None = None,
        gateway=None,
    ):
        self.engine = engine
        self.oms = oms
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
            frozen_symbols=self.scope_guards.frozen_symbols,
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
            refresh_rearm_state=(
                lambda: self.kill_switch._refresh_rearm_state()
            ),
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
            refresh_rearm_state=(
                lambda: self.kill_switch._refresh_rearm_state()
            ),
            is_killed=(lambda: self.kill_switch_triggered),
            trigger_kill_switch=(
                lambda reason: self.trigger_kill_switch(reason)
            ),
            freeze_symbol=self.scope_guards._freeze_symbol,
            recover_symbol_if_stable=(
                self.scope_guards._recover_symbol_if_stable
            ),
            owned_symbol_reason=self.scope_guards._owned_symbol_reason,
            clear_owned_symbol_freeze=(
                self.scope_guards._clear_owned_symbol_freeze
            ),
            current_venue=self.scope_guards._current_venue,
            freeze_venue=self.scope_guards._freeze_venue,
            recover_venue_if_stable=(
                self.scope_guards._recover_venue_if_stable
            ),
            set_trading_mode=self._set_trading_mode,
            clear_trading_mode=self._clear_trading_mode,
            renew_venue_dead_man_switch=self._renew_venue_dead_man_switch,
            publish_risk_control_heartbeat=(
                self._publish_risk_control_heartbeat
            ),
            tracked_symbols=self._tracked_symbols,
            log_warn=self._log_warn,
            latency_recovery_by_symbol=(
                self.scope_guards.latency_recovery_by_symbol
            ),
            divergence_recovery_by_symbol=(
                self.scope_guards.divergence_recovery_by_symbol
            ),
            venue_recovery_by_venue=(
                self.scope_guards.venue_recovery_by_venue
            ),
            venue_freeze_recovery_updates=(
                self.venue_freeze_recovery_updates
            ),
        )

        self.risk_state_repository.restore()

        self._register_handler(
            EVENT_MARK_PRICE,
            self.market_risk.on_mark_price,
        )
        self._register_handler(
            EVENT_ACCOUNT_UPDATE,
            self.account_risk.on_account_update,
        )
        self._register_handler(
            EVENT_ORDERBOOK,
            self.market_risk.on_orderbook,
        )
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
            "frozen_symbols": dict(self.scope_guards.frozen_symbols),
            "frozen_symbol_epochs": dict(
                self.scope_guards.symbol_freeze_epochs
            ),
            "frozen_symbol_owners": {
                symbol: {
                    owner: dict(record)
                    for owner, record in owners.items()
                }
                for symbol, owners in (
                    self.scope_guards.symbol_freeze_owners.items()
                )
            },
            "frozen_venues": dict(self.scope_guards.frozen_venues),
            "frozen_venue_epochs": dict(
                self.scope_guards.venue_freeze_epochs
            ),
            "market_latency_ms": self.market_risk.last_market_latency_ms,
            "processing_lag_ms": self.market_risk.last_processing_lag_ms,
            "gateway_dispatch_lag_ms": (
                self.market_risk.last_gateway_dispatch_lag_ms
            ),
            "exchange_clock_offset_ms": float(
                getattr(time_service, "offset", 0.0) or 0.0
            ),
            "funding_guard": self.funding_guard.status_snapshot(),
        }

    def set_venue_dms_supervisor_health(self, healthy: bool) -> None:
        self.venue_dms.set_supervisor_health(healthy)

    def _renew_venue_dead_man_switch(self) -> bool:
        return self.venue_dms.renew(
            active=self.active,
            kill_switch_triggered=self.kill_switch_triggered,
        )

    @property
    def funding_guard_policy(self) -> FundingGuardPolicy:
        return self.funding_guard.policy

    @staticmethod
    def _current_risk_day() -> str:
        return datetime.now(timezone.utc).date().isoformat()


    def check_funding_guard(self, now: float = None) -> bool:
        return self.funding_guard.check(
            active=self.active,
            kill_switch_triggered=self.kill_switch_triggered,
            now=now,
        )



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
        symbols = set(self.scope_guards.frozen_symbols)
        if self.oms:
            symbols.update(self.oms.config.get("symbols", []))
            symbols.update(getattr(self.oms.exposure, "net_positions", {}).keys())
        return {symbol for symbol in symbols if symbol}


    def _log_warn(self, msg: str):
        self.engine.put(Event(EVENT_LOG, f"[Risk] {msg}"))
