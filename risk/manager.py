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
from infrastructure.time_service import time_service
from oms.journal import JournalError
from risk.deployment_loss import (
    MAX_CANARY_DEPLOYED_EQUITY_FRACTION,
    deployed_capital_within_equity_limit,
    deployment_policy_fingerprint,
    deployment_loss_action,
    update_deployment_loss,
)
from risk.funding_guard import (
    FundingGuardDecision,
    FundingGuardPolicy,
    FundingGuardState,
    FundingObservation,
    evaluate_funding_guard,
)


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
    VALID_KILL_STATES = RESUMABLE_KILL_STATES | {
        "ARMED",
        "FLAT_VERIFIED",
    }

    def __init__(self, engine, config: dict, oms=None, gateway=None):
        self.engine = engine
        self.oms = oms
        self.gateway = gateway
        self.root_config = config
        self.config = config.get("risk", {})

        self.active = self.config.get("active", True)
        self.independent_supervisor_enabled = bool(
            self.config.get("independent_supervisor", {}).get("enabled", False)
        )
        self._venue_dms_renewal_authorized = True
        self._venue_dms_supervisor_healthy = True
        self._venue_dms_failure_reason = ""
        self._last_venue_dms_renewal_result = None
        self.kill_switch_triggered = False
        self.kill_reason = ""

        limits = self.config.get("limits", {})
        self.max_order_qty = limits.get("max_order_qty", 1000.0)
        self.max_order_notional = limits.get("max_order_notional", 5000.0)
        self.max_pos_notional = limits.get("max_pos_notional", 20000.0)
        self.max_account_gross_notional = limits.get("max_account_gross_notional", 0.0)
        self.max_daily_loss = limits.get("max_daily_loss", 500.0)
        self.max_drawdown_pct = limits.get("max_drawdown_pct", 0.0)
        live_launch = config.get("live_launch", {}) or {}
        self.deployment_id = str(
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
        self.deployment_policy_fingerprint = deployment_policy_fingerprint(
            deployment_id=self.deployment_id,
            symbols=config.get("symbols", []),
            declared_account_equity=self.declared_account_equity,
            max_deployed_capital=self.max_deployed_capital,
            maximum_loss=self.max_deployment_loss,
            reduce_only_fraction=self.deployment_loss_reduce_only_fraction,
        )
        self.deployment_start_equity = 0.0
        self.deployment_start_external_cash_flow_total = 0.0
        self.deployment_adjusted_equity = 0.0
        self.deployment_loss = 0.0

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

        funding = self.config.get("funding_guard", {})
        funding = funding if isinstance(funding, dict) else {}
        self.funding_guard_policy = FundingGuardPolicy(
            enabled=bool(funding.get("enabled", False)),
            require_snapshot=bool(
                funding.get(
                    "require_snapshot",
                    funding.get("enabled", False),
                )
            ),
            max_snapshot_age_ms=float(
                funding.get("max_snapshot_age_ms", 3000.0) or 3000.0
            ),
            pre_funding_reduce_only_sec=float(
                funding.get(
                    "pre_funding_reduce_only_sec",
                    600.0,
                )
                or 600.0
            ),
            post_funding_hold_sec=float(
                funding.get("post_funding_hold_sec", 120.0) or 120.0
            ),
            max_abs_funding_rate=float(
                funding.get("max_abs_funding_rate", 0.0005) or 0.0005
            ),
            max_next_funding_horizon_sec=float(
                funding.get(
                    "max_next_funding_horizon_sec",
                    32_400.0,
                )
                or 32_400.0
            ),
            recovery_updates=int(
                funding.get("recovery_updates", 5) or 5
            ),
        )
        self.funding_guard_lock = threading.RLock()
        self.funding_guard_states: dict[str, FundingGuardState] = {}
        self.funding_guard_decisions: dict[str, FundingGuardDecision] = {}

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
        self.kill_empty_snapshots_required = max(
            2,
            int(kill_config.get("empty_snapshots_required", 2) or 2),
        )
        self._kill_empty_order_snapshots = 0
        self._kill_empty_flat_snapshots = 0
        self._kill_last_accepted_snapshot_at = 0.0
        self._kill_state_lock = threading.RLock()
        self._kill_verification_lock = threading.Lock()
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
        self.last_market_latency_ms = 0.0
        self.last_processing_lag_ms = 0.0
        self.last_gateway_dispatch_lag_ms = 0.0
        self.latency_breach_by_symbol = defaultdict(int)
        self.latency_recovery_by_symbol = defaultdict(int)
        self.divergence_breach_by_symbol = defaultdict(int)
        self.divergence_recovery_by_symbol = defaultdict(int)
        self.frozen_symbols = {}
        self.symbol_freeze_epochs = {}
        self.symbol_freeze_owners = {}
        self.frozen_venues = {}
        self.venue_freeze_epochs = {}
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
        with self.funding_guard_lock:
            funding_guard_decisions = dict(self.funding_guard_decisions)
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
            "venue_dms_renewal_authorized": bool(
                self._venue_dms_renewal_authorized
            ),
            "venue_dms_supervisor_healthy": bool(
                self._venue_dms_supervisor_healthy
            ),
            "venue_dms_failure_reason": self._venue_dms_failure_reason,
            "last_venue_dms_renewal_result": (
                self._last_venue_dms_renewal_result
            ),
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
            "funding_guard": {
                "enabled": bool(self.funding_guard_policy.enabled),
                "healthy": bool(
                    not self.funding_guard_policy.enabled
                    or (
                        funding_guard_decisions
                        and all(
                            decision.healthy
                            for decision in funding_guard_decisions.values()
                        )
                    )
                ),
                "symbols": {
                    symbol: {
                        "action": decision.action,
                        "reason": decision.reason,
                        "funding_rate": decision.funding_rate,
                        "seconds_to_funding": decision.seconds_to_funding,
                        "snapshot_age_ms": decision.snapshot_age_ms,
                        "post_hold_remaining_sec": (
                            decision.post_hold_remaining_sec
                        ),
                        "healthy_updates": (
                            decision.consecutive_healthy_updates
                        ),
                        "required_recovery_updates": (
                            decision.required_recovery_updates
                        ),
                    }
                    for symbol, decision in funding_guard_decisions.items()
                },
            },
        }

    def set_venue_dms_supervisor_health(self, healthy: bool) -> None:
        previous = bool(self._venue_dms_supervisor_healthy)
        self._venue_dms_supervisor_healthy = bool(healthy)
        if self._venue_dms_supervisor_healthy or not previous:
            return
        dms_enabled = bool(
            (
                self.root_config.get("oms", {}).get(
                    "venue_dead_man_switch",
                    {},
                )
                or {}
            ).get("enabled", False)
        )
        oms_state = str(
            getattr(getattr(self.oms, "state", None), "value", "") or ""
        ).upper()
        if dms_enabled and oms_state == "LIVE":
            self._latch_venue_dead_man_switch_failure(
                "independent_supervisor_unhealthy"
            )

    def _latch_venue_dead_man_switch_failure(self, reason: str) -> bool:
        reason = str(reason or "renewal_health_invalid")
        self._venue_dms_renewal_authorized = False
        self._venue_dms_failure_reason = reason
        self._last_venue_dms_renewal_result = False
        latch_reason = f"venue_dead_man_switch:{reason}"

        logger.critical(
            "[Risk] Venue dead-man switch safety latch engaged: "
            f"{reason}"
        )
        handle_unhealthy = getattr(
            self.oms,
            "handle_venue_dead_man_switch_unhealthy",
            None,
        )
        if callable(handle_unhealthy):
            try:
                if handle_unhealthy(reason) is False:
                    return False
            except Exception as exc:
                logger.critical(
                    "[Risk] DMS safety handler failed: "
                    f"{type(exc).__name__}:{exc}"
                )

        freeze = getattr(self.oms, "freeze_system", None)
        if callable(freeze):
            try:
                freeze(latch_reason, cancel_active_orders=True)
                return False
            except Exception as exc:
                logger.critical(
                    "[Risk] DMS freeze/cancel request failed: "
                    f"{type(exc).__name__}:{exc}"
                )

        halt = getattr(self.oms, "halt_system", None)
        if callable(halt):
            try:
                halt(latch_reason)
            except Exception as exc:
                logger.critical(
                    "[Risk] DMS fallback halt/cancel request failed: "
                    f"{type(exc).__name__}:{exc}"
                )
        return False

    def _renew_venue_dead_man_switch(self) -> bool:
        dms_config = (
            self.root_config.get("oms", {}).get(
                "venue_dead_man_switch",
                {},
            )
            or {}
        )
        if not bool(dms_config.get("enabled", False)):
            return True
        if self.oms is None:
            return False
        if (
            not bool(self._venue_dms_renewal_authorized)
            or not bool(self._venue_dms_supervisor_healthy)
        ):
            return False

        oms_state = str(
            getattr(getattr(self.oms, "state", None), "value", "") or ""
        ).upper()
        if oms_state != "LIVE":
            return True

        snapshot_reader = getattr(
            self.oms,
            "get_venue_dead_man_switch_snapshot",
            None,
        )
        if not callable(snapshot_reader):
            return self._latch_venue_dead_man_switch_failure(
                "health_snapshot_unavailable"
            )
        try:
            snapshot = snapshot_reader()
        except Exception as exc:
            return self._latch_venue_dead_man_switch_failure(
                f"health_snapshot_failed:{type(exc).__name__}:{exc}"
            )
        if not isinstance(snapshot, dict):
            return self._latch_venue_dead_man_switch_failure(
                "health_snapshot_invalid"
            )
        if not bool(snapshot.get("enabled", False)):
            return self._latch_venue_dead_man_switch_failure(
                "unexpectedly_disabled"
            )
        if not bool(snapshot.get("valid", False)):
            return self._latch_venue_dead_man_switch_failure(
                str(snapshot.get("reason", "") or "renewal_health_invalid")
            )

        has_guards = any(
            bool(getattr(self.oms, name, {}))
            for name in (
                "symbol_guards",
                "venue_guards",
                "strategy_guards",
                "strategy_symbol_guards",
            )
        )
        mode_constraints = bool(
            getattr(self.oms, "mode_constraints", {}) or {}
        )
        can_open_new_risk = getattr(self.oms, "can_open_new_risk", None)
        if (
            not self.active
            or self.kill_switch_triggered
            or bool(getattr(self.oms, "_shutdown_requested", False))
            or bool(getattr(self.oms, "_stopped", False))
        ):
            return False
        if (
            has_guards
            or mode_constraints
            or not callable(can_open_new_risk)
            or not bool(can_open_new_risk())
        ):
            # Withhold renewal while new risk is blocked, but keep the risk
            # cycle running so transient guards can accumulate recovery checks.
            # A guard that persists past max_renewal_age still fails closed on
            # the health check above.
            return True

        renewal_allowed = getattr(
            self.oms,
            "can_renew_venue_dead_man_switch",
            None,
        )
        if not callable(renewal_allowed) or not bool(renewal_allowed()):
            return self._latch_venue_dead_man_switch_failure(
                "renewal_not_permitted"
            )

        renew = getattr(
            self.oms,
            "request_venue_dead_man_switch_renewal",
            None,
        )
        if not callable(renew):
            renew = getattr(self.oms, "renew_venue_dead_man_switch", None)
        if not callable(renew):
            return self._latch_venue_dead_man_switch_failure(
                "renewal_method_unavailable"
            )
        try:
            renewed = bool(renew())
        except Exception as exc:
            return self._latch_venue_dead_man_switch_failure(
                f"renewal_request_failed:{type(exc).__name__}:{exc}"
            )

        self._last_venue_dms_renewal_result = renewed
        if renewed:
            return True
        try:
            failed_snapshot = snapshot_reader()
        except Exception:
            failed_snapshot = {}
        failure_reason = (
            str(failed_snapshot.get("reason", "") or "")
            if isinstance(failed_snapshot, dict)
            else ""
        )
        return self._latch_venue_dead_man_switch_failure(
            failure_reason or "renewal_request_rejected"
        )

    @staticmethod
    def _current_risk_day() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _restore_risk_state(self):
        journal = getattr(self.oms, "journal", None)
        if journal is None:
            return
        try:
            latest = None
            stream_records = getattr(journal, "iter_records", None)
            records = (
                stream_records(respect_replay_policy=True)
                if callable(stream_records)
                else iter(journal.load())
            )
            for record in records:
                if record.get("kind") == "risk_state":
                    latest = record.get("payload", {})
        except JournalError as exc:
            logger.critical(f"[Risk] Failed to restore durable risk state: {exc}")
            fail_closed = getattr(self.oms, "_fail_closed_on_journal_error", None)
            if callable(fail_closed):
                fail_closed(exc, "restore_risk_state")
            return

        if not latest:
            return

        try:
            restored_numbers = {
                "initial_equity": float(
                    latest.get("day_start_equity", 0.0) or 0.0
                ),
                "initial_external_cash_flow_total": float(
                    latest.get(
                        "day_start_external_cash_flow_total",
                        0.0,
                    )
                    or 0.0
                ),
                "peak_equity": float(
                    latest.get("peak_equity", 0.0) or 0.0
                ),
                "last_equity": float(
                    latest.get("last_equity", 0.0) or 0.0
                ),
                "deployment_start_equity": float(
                    latest.get("deployment_start_equity", 0.0) or 0.0
                ),
                "deployment_start_external_cash_flow_total": float(
                    latest.get(
                        "deployment_start_external_cash_flow_total",
                        0.0,
                    )
                    or 0.0
                ),
                "deployment_adjusted_equity": float(
                    latest.get("deployment_adjusted_equity", 0.0) or 0.0
                ),
                "deployment_loss": float(
                    latest.get("deployment_loss", 0.0) or 0.0
                ),
            }
        except (TypeError, ValueError) as exc:
            self._fail_closed_on_restored_risk_state(
                f"non_numeric:{type(exc).__name__}"
            )
            return
        if not all(
            math.isfinite(value)
            for value in restored_numbers.values()
        ):
            self._fail_closed_on_restored_risk_state("non_finite")
            return
        if restored_numbers["deployment_loss"] < 0.0:
            self._fail_closed_on_restored_risk_state(
                "negative_deployment_loss"
            )
            return

        restored_kill_state = str(
            latest.get("kill_state", "ARMED") or "ARMED"
        )
        restored_kill_latch = latest.get(
            "kill_switch_triggered",
            False,
        )
        if not isinstance(restored_kill_latch, bool):
            self._fail_closed_on_restored_risk_state(
                "kill_latch_not_boolean"
            )
            return
        if restored_kill_state not in self.VALID_KILL_STATES:
            self._fail_closed_on_restored_risk_state(
                f"invalid_kill_state:{restored_kill_state}"
            )
            return
        if (
            restored_kill_latch
            and restored_kill_state == "ARMED"
        ) or (
            not restored_kill_latch
            and restored_kill_state != "ARMED"
        ):
            self._fail_closed_on_restored_risk_state(
                "inconsistent_kill_latch"
            )
            return

        self.risk_day = str(latest.get("risk_day", "") or "")
        self.initial_equity = restored_numbers["initial_equity"]
        self.initial_external_cash_flow_total = restored_numbers[
            "initial_external_cash_flow_total"
        ]
        self.peak_equity = restored_numbers["peak_equity"]
        self.last_equity = restored_numbers["last_equity"]
        self.kill_state = restored_kill_state
        self.kill_reason = str(latest.get("kill_reason", "") or "")
        self.kill_switch_triggered = restored_kill_latch
        stored_deployment_id = str(
            latest.get("deployment_id", "") or ""
        ).strip()
        if self.deployment_id and not stored_deployment_id:
            self.kill_switch_triggered = True
            self.kill_state = "FAILED"
            self.kill_reason = "deployment_identity_missing_from_journal"
            return
        if (
            stored_deployment_id
            and self.deployment_id
            and stored_deployment_id != self.deployment_id
        ):
            self.kill_switch_triggered = True
            self.kill_state = "FAILED"
            self.kill_reason = (
                "deployment_identity_mismatch:"
                f"{stored_deployment_id}!={self.deployment_id}"
            )
            return
        if not self.deployment_id:
            self.deployment_id = stored_deployment_id
        stored_policy_fingerprint = str(
            latest.get("deployment_policy_fingerprint", "") or ""
        ).strip()
        if (
            self.deployment_policy_fingerprint
            and not stored_policy_fingerprint
        ):
            self.kill_switch_triggered = True
            self.kill_state = "FAILED"
            self.kill_reason = "deployment_policy_missing_from_journal"
            return
        if (
            stored_policy_fingerprint
            and self.deployment_policy_fingerprint
            and stored_policy_fingerprint
            != self.deployment_policy_fingerprint
        ):
            self.kill_switch_triggered = True
            self.kill_state = "FAILED"
            self.kill_reason = "deployment_policy_mismatch"
            return
        self.deployment_start_equity = restored_numbers[
            "deployment_start_equity"
        ]
        self.deployment_start_external_cash_flow_total = (
            restored_numbers[
                "deployment_start_external_cash_flow_total"
            ]
        )
        self.deployment_adjusted_equity = restored_numbers[
            "deployment_adjusted_equity"
        ]
        self.deployment_loss = restored_numbers["deployment_loss"]

    def _fail_closed_on_restored_risk_state(self, detail: str) -> None:
        reason = f"risk_state_corrupt:{detail}"
        self.kill_switch_triggered = True
        self.kill_state = "FAILED"
        self.kill_reason = reason
        logger.critical(f"[Risk] {reason}")
        halt = getattr(self.oms, "halt_system", None)
        if callable(halt):
            try:
                halt(f"RiskManager: {reason}")
            except Exception as exc:
                logger.critical(
                    "[Risk] Could not halt OMS after risk-state "
                    f"corruption: {type(exc).__name__}:{exc}"
                )

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
                    "deployment_id": self.deployment_id,
                    "deployment_policy_fingerprint": (
                        self.deployment_policy_fingerprint
                    ),
                    "deployment_start_equity": (
                        self.deployment_start_equity
                    ),
                    "deployment_start_external_cash_flow_total": (
                        self.deployment_start_external_cash_flow_total
                    ),
                    "deployment_adjusted_equity": (
                        self.deployment_adjusted_equity
                    ),
                    "deployment_loss": self.deployment_loss,
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
        self._reset_kill_empty_snapshots()
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

        recovered_state = self.kill_state
        if recovered_state == "FAILED":
            with self._kill_state_lock:
                self._reset_kill_empty_snapshots()
                self.kill_state = "TRIGGERED"
            self._persist_risk_state("kill_supervision_retry_after_failure")

        logger.critical(
            f"[KillSwitch] Resuming interrupted sequence from {recovered_state}: "
            f"{self.kill_reason or 'recovered kill switch'}"
        )
        self._persist_risk_state("kill_supervision_resumed")
        if not self._verify_kill_state_safely(allow_flatten=True):
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
            self._evaluate_funding_mark(data)
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

    @staticmethod
    def _next_funding_epoch(data) -> float:
        raw_timestamp = getattr(data, "next_funding_timestamp", 0.0)
        try:
            parsed = float(raw_timestamp or 0.0)
        except (TypeError, ValueError):
            parsed = 0.0
        if parsed > 0.0:
            return parsed
        next_funding_time = getattr(data, "next_funding_time", None)
        timestamp = getattr(next_funding_time, "timestamp", None)
        if not callable(timestamp):
            return 0.0
        try:
            return float(timestamp())
        except (OSError, OverflowError, TypeError, ValueError):
            return 0.0

    @staticmethod
    def _funding_observation_id(
        symbol: str,
        exchange_timestamp,
    ) -> str:
        try:
            exchange_timestamp = float(exchange_timestamp or 0.0)
        except (TypeError, ValueError):
            return ""
        if math.isfinite(exchange_timestamp) and exchange_timestamp > 0.0:
            return f"{symbol}:{exchange_timestamp:.6f}"
        return ""

    def _evaluate_funding_observation(
        self,
        symbol: str,
        observation: FundingObservation | None,
        *,
        now_monotonic: float,
    ) -> bool:
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            return False
        with self.funding_guard_lock:
            previous = self.funding_guard_states.get(symbol)
            if previous is None:
                previous = FundingGuardState(
                    reason="funding_guard:startup_hold",
                    post_hold_until_monotonic=(
                        now_monotonic
                        + self.funding_guard_policy.post_funding_hold_sec
                    ),
                )
            decision, next_state = evaluate_funding_guard(
                self.funding_guard_policy,
                observation,
                previous,
                now_monotonic=now_monotonic,
            )
            self.funding_guard_states[symbol] = next_state
            self.funding_guard_decisions[symbol] = decision
            if decision.blocks_open_risk:
                constraint_reason = decision.reason
                if constraint_reason.startswith(
                    "funding_guard:recovery_pending:"
                ):
                    constraint_reason = "funding_guard:recovery_pending"
                self._set_trading_mode(
                    OMSCapabilityMode.REDUCE_ONLY,
                    constraint_reason,
                )
                return False
            return True

    def _evaluate_funding_mark(self, data) -> bool:
        symbol = str(getattr(data, "symbol", "") or "").strip().upper()
        now_monotonic = time.perf_counter()
        received_monotonic = getattr(data, "received_monotonic", 0.0)
        observation = FundingObservation(
            observation_id=self._funding_observation_id(
                symbol,
                getattr(data, "exchange_timestamp", 0.0),
            ),
            funding_rate=getattr(data, "funding_rate", None),
            next_funding_epoch=self._next_funding_epoch(data),
            corrected_received_epoch=getattr(
                data,
                "corrected_received_timestamp",
                0.0,
            ),
            received_monotonic=received_monotonic,
            clock_healthy=bool(time_service.is_ready()),
        )
        healthy = self._evaluate_funding_observation(
            symbol,
            observation,
            now_monotonic=now_monotonic,
        )
        if healthy:
            self._clear_funding_constraint_if_healthy()
        return healthy

    def _funding_observation_from_snapshot(
        self,
        symbol: str,
        snapshot: dict,
    ) -> FundingObservation | None:
        if not isinstance(snapshot, dict):
            return None
        exchange_timestamp = snapshot.get("mark_exchange_timestamp")
        received_monotonic = snapshot.get("mark_received_monotonic")
        observation_id = self._funding_observation_id(
            symbol,
            exchange_timestamp,
        )
        if not observation_id:
            return None
        return FundingObservation(
            observation_id=observation_id,
            funding_rate=snapshot.get("funding_rate"),
            next_funding_epoch=snapshot.get("next_funding_epoch"),
            corrected_received_epoch=snapshot.get(
                "mark_corrected_received_timestamp"
            ),
            received_monotonic=received_monotonic,
            clock_healthy=bool(time_service.is_ready()),
        )

    def _clear_funding_constraint_if_healthy(self) -> bool:
        configured_symbols = {
            str(symbol or "").strip().upper()
            for symbol in self.root_config.get("symbols", ())
            if str(symbol or "").strip()
        }
        with self.funding_guard_lock:
            if not configured_symbols or any(
                not self.funding_guard_decisions.get(symbol)
                or not self.funding_guard_decisions[symbol].healthy
                for symbol in configured_symbols
            ):
                return False
            constraint_query = getattr(
                self.oms,
                "has_trading_mode_constraint",
                None,
            )
            if callable(constraint_query) and not constraint_query(
                ("funding_guard:",)
            ):
                return True
            capability_snapshot = getattr(
                self.oms,
                "get_capability_snapshot",
                None,
            )
            if not callable(capability_snapshot):
                return False
            snapshot = capability_snapshot()
            constraints = (
                snapshot.get("mode_constraints", {})
                if isinstance(snapshot, dict)
                else {}
            )
            record = (
                constraints.get("funding_guard:", {})
                if isinstance(constraints, dict)
                else {}
            )
            generation = (
                int(record.get("generation", 0) or 0)
                if isinstance(record, dict)
                else 0
            )
            reason = (
                str(record.get("reason", "") or "")
                if isinstance(record, dict)
                else ""
            )
            if generation <= 0 or not reason.startswith("funding_guard:"):
                return False
            return self._clear_trading_mode(
                reason="funding guard recovered",
                prefixes=("funding_guard:",),
                expected_generations={
                    "funding_guard:": generation,
                },
            )

    def check_funding_guard(self, now: float = None) -> bool:
        if not self.active or self.kill_switch_triggered:
            return False
        if not self.funding_guard_policy.enabled:
            return True

        now_monotonic = time.perf_counter() if now is None else float(now)
        symbols = sorted(self._tracked_symbols())
        if not symbols:
            return False
        healthy = True
        for symbol in symbols:
            snapshot = data_cache.get_risk_snapshot(
                symbol,
                now=now_monotonic,
            )
            observation = self._funding_observation_from_snapshot(
                symbol,
                snapshot,
            )
            if not self._evaluate_funding_observation(
                symbol,
                observation,
                now_monotonic=now_monotonic,
            ):
                healthy = False
        if healthy:
            return self._clear_funding_constraint_if_healthy()
        return False

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

    def on_account_update(self, event: Event):
        self._refresh_rearm_state()
        if self.kill_switch_triggered or not self.active:
            return

        account = event.data
        try:
            account_numbers = {
                "balance": float(getattr(account, "balance", 0.0)),
                "equity": float(getattr(account, "equity", 0.0)),
                "available": float(getattr(account, "available", 0.0)),
                "used_margin": float(
                    getattr(account, "used_margin", 0.0)
                ),
                "external_cash_flow_total": float(
                    getattr(
                        account,
                        "external_cash_flow_total",
                        0.0,
                    )
                    or 0.0
                ),
            }
        except (TypeError, ValueError):
            self.trigger_kill_switch(
                "Account update contains non-numeric risk values"
            )
            return
        if (
            not all(
                math.isfinite(value)
                for value in account_numbers.values()
            )
            or account_numbers["equity"] <= 0.0
            or account_numbers["used_margin"] < 0.0
        ):
            self.trigger_kill_switch(
                "Account update contains invalid or non-finite risk values"
            )
            return
        account_equity = account_numbers["equity"]
        current_external_cash_flow_total = account_numbers[
            "external_cash_flow_total"
        ]
        if self._check_cash_flow_truth(account):
            return
        if self.max_deployed_capital > 0.0:
            try:
                capital_envelope_safe = (
                    deployed_capital_within_equity_limit(
                        equity=account_equity,
                        max_deployed_capital=self.max_deployed_capital,
                        maximum_fraction=(
                            MAX_CANARY_DEPLOYED_EQUITY_FRACTION
                        ),
                    )
                )
            except ValueError:
                capital_envelope_safe = False
            if not capital_envelope_safe:
                self.trigger_kill_switch(
                    "Canary deployed capital exceeds 2% of current "
                    "account equity"
                )
                return
        current_risk_day = self._current_risk_day()
        risk_state_changed = False
        if self.risk_day != current_risk_day:
            self.risk_day = current_risk_day
            self.initial_equity = account_equity
            self.initial_external_cash_flow_total = (
                current_external_cash_flow_total
            )
            self.peak_equity = account_equity
            risk_state_changed = True
        elif self.initial_equity == 0:
            self.initial_equity = account_equity
            self.initial_external_cash_flow_total = (
                current_external_cash_flow_total
            )
            self.peak_equity = account_equity
            risk_state_changed = True

        external_cash_flow_delta = (
            current_external_cash_flow_total - self.initial_external_cash_flow_total
        )
        adjusted_equity = account_equity - external_cash_flow_delta
        if self.max_deployment_loss > 0.0:
            baseline_missing = self.deployment_start_equity <= 0.0
            (
                self.deployment_start_equity,
                self.deployment_start_external_cash_flow_total,
                self.deployment_adjusted_equity,
                self.deployment_loss,
            ) = update_deployment_loss(
                equity=account_equity,
                external_cash_flow_total=current_external_cash_flow_total,
                start_equity=self.deployment_start_equity,
                start_external_cash_flow_total=(
                    self.deployment_start_external_cash_flow_total
                ),
            )
            risk_state_changed = risk_state_changed or baseline_missing
        if adjusted_equity > self.peak_equity:
            self.peak_equity = adjusted_equity
            risk_state_changed = True
        self.last_equity = account_equity

        if risk_state_changed:
            self._persist_risk_state("account_baseline_or_peak")

        if self._check_margin_health(account):
            return

        deployment_action = deployment_loss_action(
            loss=self.deployment_loss,
            maximum_loss=self.max_deployment_loss,
            reduce_only_fraction=(
                self.deployment_loss_reduce_only_fraction
            ),
        )
        if deployment_action == "KILL":
            self.trigger_kill_switch(
                "Deployment loss limit breached: "
                f"-{self.deployment_loss:.2f} "
                f">= {self.max_deployment_loss:.2f}"
            )
            return

        if deployment_action == "REDUCE_ONLY":
            self._set_trading_mode(
                OMSCapabilityMode.REDUCE_ONLY,
                "deployment_loss_reduce_only:"
                f"{self.deployment_loss:.6f}",
            )
        else:
            self._clear_trading_mode(
                reason="deployment loss recovered",
                prefixes=("deployment_loss_reduce_only:",),
            )

        drawdown = (
            self.initial_equity
            - account_equity
            + external_cash_flow_delta
        )
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
        try:
            snapshot_time = float(
                getattr(account, "cash_flow_snapshot_time", 0.0)
                or 0.0
            )
        except (TypeError, ValueError):
            snapshot_time = 0.0
        if not math.isfinite(snapshot_time) or snapshot_time < 0.0:
            snapshot_time = 0.0
            synced = False
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

        try:
            snapshot_time = float(
                getattr(account, "margin_snapshot_time", 0.0) or 0.0
            )
        except (TypeError, ValueError):
            snapshot_time = 0.0
        if not math.isfinite(snapshot_time) or snapshot_time < 0.0:
            self.margin_recovery_count = 0
            self._set_trading_mode(
                OMSCapabilityMode.REDUCE_ONLY,
                "margin_health:invalid_snapshot_time",
            )
            return False
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

        try:
            ratio = float(
                getattr(account, "maintenance_margin_ratio", 0.0)
                or 0.0
            )
        except (TypeError, ValueError):
            ratio = float("nan")
        if not math.isfinite(ratio) or ratio < 0.0:
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

    def on_order_update(self, event: Event):
        return None

    def trigger_kill_switch(self, reason: str):
        with self._kill_state_lock:
            if self.kill_switch_triggered:
                return
            self._reset_kill_empty_snapshots()
            self.kill_switch_triggered = True
            self.kill_reason = reason
            self.kill_state = "TRIGGERED"
        self._persist_risk_state("kill_triggered")
        logger.critical(f"KILL SWITCH TRIGGERED: {reason}")

        if self.oms:
            try:
                self.oms.halt_system(f"KillSwitch: {reason}")
            except Exception as exc:
                logger.error(f"[KillSwitch] oms.halt_system failed: {exc}")

        try:
            self._retry_mass_cancel()
            self._set_kill_state("CANCEL_PENDING", "mass_cancel_requested")
        except Exception as exc:
            logger.error(
                "[KillSwitch] initial mass cancel failed unexpectedly: "
                f"{type(exc).__name__}:{exc}"
            )
        finally:
            if not self._verify_kill_state_safely(allow_flatten=True):
                self._start_kill_supervisor()

    def restart_kill_switch_after_truth_drift(self, reason: str) -> bool:
        """Re-arm kill supervision when a supposedly flat account drifts."""
        reason = str(reason or "account truth drift after flat verification")
        with self._kill_state_lock:
            self._reset_kill_empty_snapshots()
            self.kill_switch_triggered = True
            self.kill_reason = reason
            self.kill_state = "TRIGGERED"
        self._persist_risk_state("kill_restarted_after_truth_drift")
        logger.critical(
            "KILL SWITCH RESTARTED AFTER ACCOUNT TRUTH DRIFT: "
            f"{reason}"
        )

        if self.oms:
            try:
                self.oms.halt_system(f"KillSwitchRestart: {reason}")
            except Exception as exc:
                logger.error(
                    "[KillSwitch] OMS halt failed during truth-drift restart: "
                    f"{exc}"
                )

        try:
            self._retry_mass_cancel()
            self._set_kill_state(
                "CANCEL_PENDING",
                "truth_drift_mass_cancel_requested",
            )
        except Exception as exc:
            logger.error(
                "[KillSwitch] truth-drift mass cancel failed unexpectedly: "
                f"{type(exc).__name__}:{exc}"
            )
        finally:
            if not self._verify_kill_state_safely(allow_flatten=True):
                self._start_kill_supervisor()
        return True

    def _set_kill_state(self, state: str, reason: str):
        with self._kill_state_lock:
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
        deadline = time.perf_counter() + self.kill_verify_timeout_sec
        next_flatten_at = time.perf_counter() + self.kill_flatten_retry_sec
        timeout_reported = False
        while self.kill_switch_triggered:
            now = time.perf_counter()
            allow_flatten = now >= next_flatten_at
            if self._verify_kill_state_safely(allow_flatten=allow_flatten):
                return
            if allow_flatten:
                next_flatten_at = now + self.kill_flatten_retry_sec
            if now >= deadline and not timeout_reported:
                self._set_kill_state(
                    "FAILED",
                    "kill_verification_timeout_continuing",
                )
                logger.critical(
                    "[KillSwitch] Initial flat-state verification timed "
                    f"out after {self.kill_verify_timeout_sec:.1f}s; "
                    "cancellation and flatten supervision will continue"
                )
                timeout_reported = True
            time.sleep(self.kill_verify_interval_sec)

    def _verify_kill_state_safely(self, allow_flatten: bool) -> bool:
        with self._kill_verification_lock:
            try:
                return bool(
                    self._verify_kill_state_once(allow_flatten=allow_flatten)
                )
            except Exception as exc:
                self._reset_kill_empty_snapshots()
                logger.error(
                    "[KillSwitch] verification attempt failed; "
                    "supervision remains active: "
                    f"{type(exc).__name__}:{exc}"
                )
                return False

    def _verify_kill_state_once(self, allow_flatten: bool) -> bool:
        open_orders = self._query_kill_open_orders()
        if open_orders is None:
            self._reset_kill_empty_snapshots()
            if allow_flatten:
                self._retry_mass_cancel()
            return False

        local_order_truth = self._query_local_kill_order_truth()
        if local_order_truth is None:
            self._reset_kill_empty_snapshots()
            self._set_kill_state("CANCEL_PENDING", "local_order_truth_unknown")
            if allow_flatten:
                self._retry_mass_cancel(remote_orders=open_orders)
            return False

        local_active_orders = local_order_truth["active_orders"]
        order_sends_inflight = local_order_truth["order_sends_inflight"]
        if open_orders:
            self._reset_kill_empty_snapshots()
            self._set_kill_state("CANCEL_PENDING", "open_orders_remain")
            if allow_flatten:
                self._retry_mass_cancel(
                    remote_orders=open_orders,
                    local_orders=local_active_orders,
                )
            return False
        if local_active_orders or order_sends_inflight:
            self._reset_kill_empty_snapshots()
            self._set_kill_state(
                "CANCEL_PENDING",
                "local_orders_or_sends_remain",
            )
            if allow_flatten:
                self._retry_mass_cancel(local_orders=local_active_orders)
            return False

        now = time.perf_counter()
        if (
            self._kill_last_accepted_snapshot_at > 0.0
            and now - self._kill_last_accepted_snapshot_at
            < self.kill_verify_interval_sec
        ):
            self._set_kill_state(
                "CANCEL_PENDING",
                "empty_snapshot_interval_pending",
            )
            return False
        self._kill_last_accepted_snapshot_at = now
        self._kill_empty_order_snapshots = min(
            self.kill_empty_snapshots_required,
            self._kill_empty_order_snapshots + 1,
        )
        positions = self._query_kill_positions()
        if positions is None:
            self._kill_empty_flat_snapshots = 0
            return False
        if positions:
            self._kill_empty_flat_snapshots = 0
            self._set_kill_state("FLATTENING", "nonzero_positions_remain")
            if (
                allow_flatten
                and self.oms
                and hasattr(self.oms, "emergency_reduce_only_flatten")
            ):
                try:
                    self.oms.emergency_reduce_only_flatten(
                        f"KillSwitch: {self.kill_reason}"
                    )
                except Exception as exc:
                    logger.error(f"[KillSwitch] emergency flatten failed: {exc}")
            return False

        self._kill_empty_flat_snapshots = min(
            self.kill_empty_snapshots_required,
            self._kill_empty_flat_snapshots + 1,
        )
        required = self.kill_empty_snapshots_required
        if self._kill_empty_order_snapshots < required:
            self._set_kill_state(
                "CANCEL_PENDING",
                "empty_order_snapshot_confirmation_pending",
            )
            return False
        self._set_kill_state("CANCEL_VERIFIED", "orders_cancelled_confirmed")
        if self._kill_empty_flat_snapshots < required:
            return False

        self._set_kill_state(
            "FLAT_VERIFIED",
            "orders_cancelled_and_positions_flat_confirmed",
        )
        return True

    def _reset_kill_empty_snapshots(self):
        self._kill_empty_order_snapshots = 0
        self._kill_empty_flat_snapshots = 0
        self._kill_last_accepted_snapshot_at = 0.0

    def _query_local_kill_order_truth(self):
        if self.oms is None:
            return {
                "active_orders": [],
                "order_sends_inflight": 0,
            }
        query = getattr(self.oms, "get_local_order_truth_snapshot", None)
        if not callable(query):
            logger.error("[KillSwitch] OMS local order truth snapshot is unavailable")
            return None
        try:
            snapshot = query()
        except Exception as exc:
            logger.error(
                "[KillSwitch] OMS local order truth snapshot failed: "
                f"{type(exc).__name__}:{exc}"
            )
            return None
        if not isinstance(snapshot, dict):
            logger.error(
                "[KillSwitch] OMS local order truth snapshot returned invalid "
                f"container: {type(snapshot).__name__}"
            )
            return None

        active_orders = snapshot.get("active_orders")
        order_sends_inflight = snapshot.get("order_sends_inflight")
        if not isinstance(active_orders, (list, tuple)):
            logger.error(
                "[KillSwitch] OMS local active-order truth is invalid: "
                f"{type(active_orders).__name__}"
            )
            return None
        if (
            isinstance(order_sends_inflight, bool)
            or not isinstance(order_sends_inflight, int)
            or order_sends_inflight < 0
        ):
            logger.error(
                "[KillSwitch] OMS outbound send truth is invalid: "
                f"{order_sends_inflight!r}"
            )
            return None
        for index, payload in enumerate(active_orders):
            if not isinstance(payload, dict):
                logger.error(
                    "[KillSwitch] OMS local active-order row is invalid at "
                    f"index {index}: {type(payload).__name__}"
                )
                return None
            symbol = str(payload.get("symbol", "") or "").upper().strip()
            if not symbol:
                logger.error(
                    "[KillSwitch] OMS local active-order row has no symbol at "
                    f"index {index}"
                )
                return None
        return {
            "active_orders": list(active_orders),
            "order_sends_inflight": order_sends_inflight,
        }

    def _query_kill_open_orders(self):
        query = getattr(self.gateway, "get_open_orders", None)
        if not callable(query):
            return None
        try:
            remote_orders = (
                query(emergency=True)
                if bool(
                    getattr(
                        self.gateway,
                        "supports_emergency_query_priority",
                        False,
                    )
                )
                else query()
            )
        except Exception as exc:
            logger.error(f"[KillSwitch] open-order verification failed: {exc}")
            return None
        if not isinstance(remote_orders, (list, tuple)):
            logger.error(
                "[KillSwitch] open-order verification returned invalid "
                f"container: {type(remote_orders).__name__}"
            )
            return None
        try:
            for index, payload in enumerate(remote_orders):
                if not isinstance(payload, dict):
                    logger.error(
                        "[KillSwitch] open-order verification returned "
                        "invalid row at index "
                        f"{index}: {type(payload).__name__}"
                    )
                    return None
                if "symbol" not in payload:
                    logger.error(
                        "[KillSwitch] open-order verification row is missing "
                        f"symbol at index {index}"
                    )
                    return None
                symbol = str(payload["symbol"] or "").upper().strip()
                if not symbol:
                    logger.error(
                        "[KillSwitch] open-order verification row contains "
                        f"an empty symbol at index {index}"
                    )
                    return None
        except Exception as exc:
            logger.error(
                "[KillSwitch] open-order verification payload could not be "
                f"validated: {type(exc).__name__}:{exc}"
            )
            return None
        return remote_orders

    def _query_kill_positions(self):
        query = getattr(self.gateway, "get_all_positions", None)
        if not callable(query):
            return None
        try:
            remote_positions = (
                query(emergency=True)
                if bool(
                    getattr(
                        self.gateway,
                        "supports_emergency_query_priority",
                        False,
                    )
                )
                else query()
            )
        except Exception as exc:
            logger.error(f"[KillSwitch] position verification failed: {exc}")
            return None
        if remote_positions is None:
            return None

        try:
            return self._collect_nonzero_kill_positions(remote_positions)
        except Exception as exc:
            logger.error(
                "[KillSwitch] position verification payload could not be "
                f"validated: {type(exc).__name__}:{exc}"
            )
            return None

    def _collect_nonzero_kill_positions(self, remote_positions):
        if not isinstance(remote_positions, (list, tuple)):
            logger.error(
                "[KillSwitch] position verification returned invalid "
                f"container: {type(remote_positions).__name__}"
            )
            return None

        nonzero = set()
        for index, payload in enumerate(remote_positions):
            if not isinstance(payload, dict):
                logger.error(
                    "[KillSwitch] position verification returned invalid "
                    f"row at index {index}: {type(payload).__name__}"
                )
                return None
            if "symbol" not in payload or "positionAmt" not in payload:
                logger.error(
                    "[KillSwitch] position verification row is missing "
                    f"required fields at index {index}"
                )
                return None
            try:
                symbol = str(payload["symbol"] or "").upper().strip()
                amount = float(payload["positionAmt"])
            except (TypeError, ValueError, OverflowError) as exc:
                logger.error(
                    "[KillSwitch] position verification row is invalid "
                    f"at index {index}: {type(exc).__name__}"
                )
                return None
            except Exception as exc:
                logger.error(
                    "[KillSwitch] position verification row could not be "
                    f"decoded at index {index}: {type(exc).__name__}"
                )
                return None
            if not symbol or not math.isfinite(amount):
                logger.error(
                    "[KillSwitch] position verification row contains "
                    f"non-finite or empty values at index {index}"
                )
                return None
            if amount != 0.0:
                nonzero.add(symbol)

        if self.oms:
            try:
                exposure = getattr(self.oms, "exposure", None)
                local_positions = getattr(exposure, "net_positions", None)
            except Exception as exc:
                logger.error(
                    "[KillSwitch] local position ledger is unavailable: "
                    f"{type(exc).__name__}:{exc}"
                )
                return None
            if not isinstance(local_positions, dict):
                logger.error(
                    "[KillSwitch] local position ledger is invalid: "
                    f"{type(local_positions).__name__}"
                )
                return None
            for raw_symbol, raw_volume in local_positions.items():
                try:
                    symbol = str(raw_symbol or "").upper().strip()
                    volume = float(raw_volume)
                except (TypeError, ValueError, OverflowError) as exc:
                    logger.error(
                        "[KillSwitch] local position ledger entry is invalid: "
                        f"{type(exc).__name__}"
                    )
                    return None
                except Exception as exc:
                    logger.error(
                        "[KillSwitch] local position ledger entry could not "
                        f"be decoded: {type(exc).__name__}"
                    )
                    return None
                if not symbol or not math.isfinite(volume):
                    logger.error(
                        "[KillSwitch] local position ledger contains "
                        "non-finite or empty values"
                    )
                    return None
                if volume != 0.0:
                    nonzero.add(symbol)
        return nonzero

    def _collect_kill_cancel_symbols(self, remote_orders=None, local_orders=None):
        symbols = set()

        try:
            root_config = getattr(self, "root_config", None)
            configured = (
                root_config.get("symbols")
                if isinstance(root_config, dict)
                else None
            )
            if not isinstance(configured, (list, tuple, set)):
                raise TypeError(
                    f"invalid root symbols: {type(configured).__name__}"
                )
            symbols.update(configured)
        except Exception as exc:
            logger.error(
                "[KillSwitch] root symbol discovery failed: "
                f"{type(exc).__name__}:{exc}"
            )

        try:
            frozen_symbols = getattr(self, "frozen_symbols", {})
            if isinstance(frozen_symbols, dict):
                symbols.update(frozen_symbols.keys())
            else:
                logger.error(
                    "[KillSwitch] frozen symbol ledger is invalid: "
                    f"{type(frozen_symbols).__name__}"
                )
        except Exception as exc:
            logger.error(
                "[KillSwitch] frozen symbol ledger is unavailable: "
                f"{type(exc).__name__}:{exc}"
            )

        if self.oms:
            try:
                oms_config = getattr(self.oms, "config", None)
                configured = (
                    oms_config.get("symbols")
                    if isinstance(oms_config, dict)
                    else None
                )
                if not isinstance(configured, (list, tuple, set)):
                    raise TypeError(
                        f"invalid configured symbols: {type(configured).__name__}"
                    )
                symbols.update(configured)
            except Exception as exc:
                logger.error(
                    "[KillSwitch] configured symbol discovery failed: "
                    f"{type(exc).__name__}:{exc}"
                )
            try:
                exposure = getattr(self.oms, "exposure", None)
                positions = getattr(exposure, "net_positions", None)
                if not isinstance(positions, dict):
                    raise TypeError(
                        f"invalid position ledger: {type(positions).__name__}"
                    )
                symbols.update(positions.keys())
            except Exception as exc:
                logger.error(
                    "[KillSwitch] position symbol discovery failed: "
                    f"{type(exc).__name__}:{exc}"
                )
            try:
                get_known_symbols = getattr(
                    self.oms,
                    "get_known_account_order_symbols",
                    None,
                )
                if not callable(get_known_symbols):
                    raise TypeError("known account symbol snapshot unavailable")
                known_symbols = get_known_symbols()
                if not isinstance(known_symbols, (list, tuple, set)):
                    raise TypeError(
                        "invalid known account symbols: "
                        f"{type(known_symbols).__name__}"
                    )
                symbols.update(known_symbols)
            except Exception as exc:
                logger.error(
                    "[KillSwitch] known account symbol discovery failed: "
                    f"{type(exc).__name__}:{exc}"
                )

        for source_name, orders in (
            ("remote", remote_orders),
            ("local", local_orders),
        ):
            if orders is None:
                continue
            if not isinstance(orders, (list, tuple)):
                logger.error(
                    f"[KillSwitch] {source_name} cancel symbol source is invalid: "
                    f"{type(orders).__name__}"
                )
                continue
            for index, payload in enumerate(orders):
                if not isinstance(payload, dict):
                    logger.error(
                        f"[KillSwitch] {source_name} cancel symbol row is invalid "
                        f"at index {index}: {type(payload).__name__}"
                    )
                    continue
                symbols.add(payload.get("symbol", ""))

        normalized = set()
        for raw_symbol in symbols:
            try:
                symbol = str(raw_symbol or "").upper().strip()
            except Exception as exc:
                logger.error(
                    "[KillSwitch] cancel symbol could not be decoded: "
                    f"{type(exc).__name__}:{exc}"
                )
                continue
            if symbol:
                normalized.add(symbol)
        return normalized

    def _retry_mass_cancel(self, remote_orders=None, local_orders=None):
        if not self.gateway:
            return
        symbols = self._collect_kill_cancel_symbols(
            remote_orders=remote_orders,
            local_orders=local_orders,
        )
        for symbol in symbols:
            try:
                self.gateway.cancel_all_orders(symbol)
            except Exception as exc:
                logger.error(f"[KillSwitch] cancel retry failed for {symbol}: {exc}")

    @staticmethod
    def _risk_symbol_guard_owner(reason: str) -> str:
        return str(reason or "").split(":", 1)[0]

    def _refresh_local_symbol_guard(self, symbol: str) -> None:
        owners = self.symbol_freeze_owners.get(symbol, {})
        if not owners:
            self.symbol_freeze_owners.pop(symbol, None)
            self.frozen_symbols.pop(symbol, None)
            self.symbol_freeze_epochs.pop(symbol, None)
            return
        newest = max(
            owners.values(),
            key=lambda record: int(record.get("epoch", 0) or 0),
        )
        self.frozen_symbols[symbol] = str(newest.get("reason", "") or "")
        self.symbol_freeze_epochs[symbol] = int(newest.get("epoch", 0) or 0)

    def _owned_symbol_reason(self, symbol: str, prefix: str) -> str:
        symbol = str(symbol or "").upper()
        owner = self._risk_symbol_guard_owner(prefix)
        record = self.symbol_freeze_owners.get(symbol, {}).get(owner, {})
        return str(record.get("reason", "") or "")

    def _freeze_symbol(self, symbol: str, reason: str):
        if not symbol:
            return

        symbol = symbol.upper()
        owner = self._risk_symbol_guard_owner(reason)
        owners = self.symbol_freeze_owners.setdefault(symbol, {})
        existing_reason = str(
            (owners.get(owner) or {}).get("reason", "") or ""
        )
        # A live latency figure changes on every update. Re-freezing the same
        # owner for each numeric variation repeats cancellation, journaling,
        # logging and event publication, amplifying an already stale queue.
        # Keep the first durable guard until its guarded recovery clears it.
        if existing_reason:
            return

        logger.error(f"[Risk] Symbol circuit breaker {symbol}: {reason}")
        self._log_warn(f"Symbol frozen {symbol}: {reason}")
        if self.oms and hasattr(self.oms, "freeze_symbol"):
            try:
                epoch = self.oms.freeze_symbol(
                    symbol,
                    reason,
                    cancel_active_orders=True,
                )
                if epoch is None:
                    get_owners = getattr(
                        self.oms,
                        "get_symbol_freeze_owners",
                        None,
                    )
                    if callable(get_owners):
                        remote_owner = (get_owners(symbol) or {}).get(owner, {})
                        if remote_owner.get("reason") == reason:
                            epoch = remote_owner.get("epoch")
                if epoch is None:
                    get_epoch = getattr(self.oms, "get_symbol_freeze_epoch", None)
                    get_reason = getattr(self.oms, "get_symbol_freeze_reason", None)
                    if callable(get_epoch) and (
                        not callable(get_reason) or get_reason(symbol) == reason
                    ):
                        epoch = get_epoch(symbol)
            except Exception as exc:
                logger.error(f"[Risk] oms.freeze_symbol({symbol}) failed: {exc}")
                epoch = None
        else:
            epoch = max(
                (int(record.get("epoch", 0) or 0) for record in owners.values()),
                default=0,
            ) + 1
        owners[owner] = {
            "reason": reason,
            "epoch": int(epoch or 0),
        }
        self._refresh_local_symbol_guard(symbol)
        self._maybe_escalate_symbol_freeze(reason)

    def _clear_owned_symbol_freeze(
        self,
        symbol: str,
        expected_reason: str,
        recovery_reason: str,
    ) -> bool:
        symbol = str(symbol or "").upper()
        if not symbol:
            return False
        owner = self._risk_symbol_guard_owner(expected_reason)
        owners = self.symbol_freeze_owners.setdefault(symbol, {})
        owner_record = owners.get(owner)
        if owner_record is None:
            legacy_reason = self.frozen_symbols.get(symbol, "")
            if legacy_reason == expected_reason:
                owner_record = {
                    "reason": expected_reason,
                    "epoch": int(self.symbol_freeze_epochs.get(symbol, 0) or 0),
                }
                owners[owner] = owner_record
        if not self.oms or not hasattr(self.oms, "clear_symbol_freeze"):
            owners.pop(owner, None)
            self._refresh_local_symbol_guard(symbol)
            return True

        expected_epoch = (
            int(owner_record.get("epoch", 0) or 0)
            if owner_record is not None
            else None
        )
        get_reason = getattr(self.oms, "get_symbol_freeze_reason", None)
        get_epoch = getattr(self.oms, "get_symbol_freeze_epoch", None)
        get_owners = getattr(self.oms, "get_symbol_freeze_owners", None)
        current_reason = get_reason(symbol) if callable(get_reason) else expected_reason
        if (expected_epoch is None or expected_epoch <= 0) and callable(get_owners):
            remote_record = (get_owners(symbol) or {}).get(owner, {})
            if remote_record.get("reason") == expected_reason:
                expected_epoch = int(remote_record.get("epoch", 0) or 0)
                owners[owner] = {
                    "reason": expected_reason,
                    "epoch": expected_epoch,
                }
        if (
            (expected_epoch is None or expected_epoch <= 0)
            and current_reason == expected_reason
            and callable(get_epoch)
        ):
            expected_epoch = int(get_epoch(symbol))
            owners[owner] = {
                "reason": expected_reason,
                "epoch": expected_epoch,
            }
        if expected_epoch is None or expected_epoch <= 0:
            logger.error(
                "[Risk] Refusing unguarded symbol recovery: "
                f"symbol={symbol} reason={expected_reason} epoch unavailable"
            )
            return False

        try:
            cleared = bool(
                self.oms.clear_symbol_freeze(
                    symbol,
                    reason=recovery_reason,
                    expected_epoch=int(expected_epoch),
                    expected_reason=expected_reason,
                )
            )
        except Exception as exc:
            logger.error(f"[Risk] oms.clear_symbol_freeze({symbol}) failed: {exc}")
            return False

        if not cleared:
            if callable(get_owners):
                remote_record = (get_owners(symbol) or {}).get(owner)
                if (
                    not remote_record
                    or remote_record.get("reason") != expected_reason
                    or int(remote_record.get("epoch", 0) or 0)
                    != int(expected_epoch)
                ):
                    owners.pop(owner, None)
                    self._refresh_local_symbol_guard(symbol)
                    return True
            current_reason = get_reason(symbol) if callable(get_reason) else expected_reason
            if current_reason and current_reason != expected_reason:
                logger.warning(
                    "[Risk] Symbol recovery ownership superseded; stale clear ignored: "
                    f"symbol={symbol} expected={expected_reason} current={current_reason}"
                )
                owners.pop(owner, None)
                self._refresh_local_symbol_guard(symbol)
                return True
            if not current_reason:
                owners.pop(owner, None)
                self._refresh_local_symbol_guard(symbol)
                return True
            return False

        owners.pop(owner, None)
        self._refresh_local_symbol_guard(symbol)
        return True

    def _recover_symbol_if_stable(self, symbol: str, prefix: str):
        frozen_reason = self._owned_symbol_reason(symbol, prefix)
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

        recovery_reason = (
            f"{prefix.rstrip(':')} recovered after "
            f"{stable_updates} healthy updates"
        )
        if not self._clear_owned_symbol_freeze(
            symbol,
            frozen_reason,
            recovery_reason,
        ):
            return
        logger.info(f"[Risk] Symbol circuit breaker cleared {symbol}: {prefix}recovered")
        self._log_warn(f"Symbol restored {symbol}: {prefix}recovered")

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
        existing_owner = self._risk_symbol_guard_owner(existing_reason)
        next_owner = self._risk_symbol_guard_owner(reason)
        if existing_reason and existing_owner == next_owner:
            return
        self.frozen_venues[venue] = reason
        self.venue_recovery_by_venue[venue] = 0

        logger.error(f"[Risk] Venue circuit breaker {venue}: {reason}")
        self._log_warn(f"Venue frozen {venue}: {reason}")
        if self.oms and hasattr(self.oms, "freeze_venue"):
            try:
                freeze_epoch = self.oms.freeze_venue(
                    venue,
                    reason,
                    cancel_active_orders=False,
                )
                if freeze_epoch is None:
                    current_reason = getattr(
                        self.oms,
                        "get_venue_freeze_reason",
                        lambda *_args, **_kwargs: "",
                    )(venue)
                    get_epoch = getattr(
                        self.oms,
                        "get_venue_freeze_epoch",
                        None,
                    )
                    if current_reason == reason and callable(get_epoch):
                        freeze_epoch = get_epoch(venue)
                if freeze_epoch is not None:
                    self.venue_freeze_epochs[venue] = int(freeze_epoch)
                else:
                    self.venue_freeze_epochs.pop(venue, None)
                    logger.error(
                        f"[Risk] Venue freeze epoch unavailable for {venue}; "
                        "recovery will remain fail-closed"
                    )
            except Exception as exc:
                self.venue_freeze_epochs.pop(venue, None)
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

        expected_epoch = self.venue_freeze_epochs.pop(venue, None)
        self.frozen_venues.pop(venue, None)
        self.venue_recovery_by_venue[venue] = 0
        cleared = False
        if self.oms and hasattr(self.oms, "clear_venue_freeze"):
            if expected_epoch is None:
                logger.error(
                    f"[Risk] Refusing unguarded venue recovery for {venue}: "
                    "freeze epoch unavailable"
                )
            else:
                try:
                    cleared = bool(
                        self.oms.clear_venue_freeze(
                            venue,
                            reason=(
                                f"{prefix.rstrip(':')} recovered after "
                                f"{stable_updates} healthy updates"
                            ),
                            expected_epoch=expected_epoch,
                            expected_reason=frozen_reason,
                        )
                    )
                except Exception as exc:
                    logger.error(
                        f"[Risk] oms.clear_venue_freeze({venue}) failed: {exc}"
                    )

        if cleared:
            logger.info(
                f"[Risk] Venue circuit breaker cleared {venue}: {prefix}recovered"
            )
            self._log_warn(f"Venue restored {venue}: {prefix}recovered")
        else:
            logger.warning(
                f"[Risk] Local venue breaker recovered for {venue}, but the "
                "OMS guard was retained or already replaced"
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
