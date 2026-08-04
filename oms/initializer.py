"""OMS runtime state and component assembly."""

from __future__ import annotations

import math
import threading
from collections import deque

from event.type import LifecycleState, OMSCapabilityMode
from infrastructure.single_writer_fence import SingleWriterFence

from .account_manager import AccountManager
from .account_truth import OMSAccountTruth
from .audit_logger import OMSAuditLogger
from .background_tasks import OMSBackgroundTaskExecutor
from .cancellation_manager import OMSCancellationManager
from .capability_manager import OMSCapabilityManager
from .component import OMSComponent
from .exchange_event_processor import OMSExchangeEventProcessor
from .exposure import ExposureManager
from .full_reset import OMSFullResetCoordinator
from .guard_manager import OMSGuardManager
from .journal import OMSJournal
from .journal_rebuilder import OMSJournalRebuilder
from .lifecycle_controller import OMSLifecycleController
from .order_manager import OrderManager
from .order_policy import OMSOrderPolicy
from .order_submission import OMSOrderSubmission
from .outbound_budget import OutboundMessageBudget
from .outbound_gate import OMSOutboundGate
from .paper_trade_database import PaperTradeDatabase
from .reconciler import OMSReconciler
from .recovery_state import OMSRecoveryStateRestorer
from .rpi_calibration_manager import RpiCalibrationManager
from .rpi_calibration_replay import RpiCalibrationReplay
from .rpi_calibration_runtime import RpiCalibrationRuntime
from .submit_settlement import OMSSubmitSettlement
from .validator import OrderValidator


class OMSInitializer(OMSComponent):
    """Build the shared state and focused components behind the OMS facade."""

    OWNER_WRITES = frozenset(
        {
            "TOMBSTONE_MAX",
            "_account_state_event_time",
            "_background_tasks",
            "_deferred_cancel_all_symbols",
            "_deferred_cancel_oids",
            "_exchange_account_event_time",
            "_exchange_position_event_time",
            "_known_account_order_symbols",
            "_lifecycle_generation",
            "_max_pending_reconcile_requests",
            "_order_truth_resolution_inflight",
            "_outbound_all_order_seal_reason",
            "_outbound_budget",
            "_outbound_gate_condition",
            "_outbound_gate_epoch",
            "_outbound_gate_holds",
            "_outbound_gate_open",
            "_outbound_gate_reason",
            "_outbound_order_sends_inflight",
            "_outbound_risk_sends_inflight",
            "_outbound_risk_sends_inflight_by_symbol",
            "_pending_reconcile_requests",
            "_position_state_event_time",
            "_reconcile_thread",
            "_recovered_guard_cleanup_snapshot",
            "_rpi_calibration",
            "_rpi_calibration_budget_exhausted",
            "_rpi_calibration_cumulative_notional_microu",
            "_rpi_calibration_effective_loss_cap_microu",
            "_rpi_calibration_enforcement_inflight",
            "_rpi_calibration_enforcement_thread",
            "_rpi_calibration_expired",
            "_rpi_calibration_expiry_reason",
            "_rpi_calibration_last_reserved_exchange_ns",
            "_rpi_calibration_peak_observed_loss_microu",
            "_rpi_calibration_permit_activated",
            "_rpi_calibration_permit_start_notional_microu",
            "_rpi_calibration_permit_start_order_count",
            "_rpi_calibration_reservation_exchange_ns",
            "_rpi_calibration_reservation_ids",
            "_rpi_calibration_reserved_order_count",
            "_rpi_calibration_restart_rearm_blocked",
            "_rpi_calibration_start_equity_microu",
            "_rpi_calibration_start_external_cash_flow_microu",
            "_rpi_calibration_terminal_cancel_sweep_completed",
            "_rpi_calibration_terminal_empty_snapshots",
            "_rpi_calibration_terminal_generation",
            "_rpi_calibration_terminal_pending_reason",
            "_rpi_calibration_terminal_verified",
            "_rpi_calibration_ttl_cancel_oids",
            "_shutdown_cancel_verified",
            "_shutdown_reason",
            "_shutdown_requested",
            "_stopped",
            "_submit_cancel_requested_oids",
            "_submit_settlement_inflight_oids",
            "_unknown_not_found_counts",
            "_venue_dead_man_renewal_thread",
            "_venue_dead_man_safety_cancel_last_attempt",
            "_venue_dead_man_safety_cancel_thread",
            "account",
            "account_truth",
            "audit_logger",
            "cancellation_manager",
            "capability_manager",
            "capability_mode",
            "capability_reason",
            "command_fence_timeout_sec",
            "config",
            "consecutive_reconcile_api_failures",
            "degraded_aggressive_to_passive",
            "duplicate_intent_window_sec",
            "emergency_flatten_cooldown_sec",
            "event_engine",
            "event_log",
            "event_log_evictions",
            "event_log_max",
            "exchange_event_processor",
            "exchange_id_map",
            "exchange_self_trade_prevention_mode",
            "execution_ids",
            "exposure",
            "external_cash_flow_assets",
            "external_cash_flow_ids",
            "external_cash_flow_max_age_sec",
            "external_cash_flow_max_pages",
            "external_cash_flow_poll_interval_sec",
            "external_cash_flow_recovery_lookback_ms",
            "external_cash_flow_recovery_overlap_ms",
            "external_cash_flow_require_snapshot",
            "external_cash_flow_scan_end_ms",
            "external_cash_flow_truth_enabled",
            "external_income_types",
            "full_reset_coordinator",
            "gateway",
            "guard_manager",
            "journal",
            "journal_rebuilder",
            "last_emergency_flatten_ts",
            "last_external_cash_flow_poll_at",
            "last_freeze_reason",
            "last_halt_reason",
            "last_reconcile_failure_ts",
            "last_reconcile_request_ts",
            "last_risk_control_heartbeat_monotonic",
            "last_risk_control_heartbeat_time",
            "last_venue_dead_man_attempt_monotonic",
            "last_venue_dead_man_success_monotonic",
            "last_venue_dead_man_success_time",
            "lifecycle_controller",
            "local_self_cross_check_enabled",
            "lock",
            "manual_rearm_required",
            "margin_health_enabled",
            "margin_health_require_snapshot",
            "margin_reduce_only_ratio",
            "margin_snapshot_max_age_sec",
            "max_account_gross_notional",
            "max_cancel_messages_per_window",
            "max_concurrent_symbols",
            "max_new_orders_per_window",
            "max_pos_notional",
            "max_reduce_orders_per_window",
            "max_strategy_active_orders",
            "max_strategy_symbol_active_orders",
            "max_symbol_active_orders",
            "max_total_active_orders",
            "max_total_messages_per_window",
            "mode_constraint_generation",
            "mode_constraint_generations",
            "mode_constraints",
            "mode_override",
            "mode_override_reason",
            "order_monitor",
            "order_policy",
            "order_submission",
            "orders",
            "outbound_gate",
            "outbound_gate_drain_timeout_sec",
            "outbound_message_budget_enabled",
            "outbound_message_history",
            "outbound_message_window_sec",
            "paper_trade_database",
            "rebuild_summary",
            "recovery_state_restorer",
            "reconcile_api_cooldown_sec",
            "reconcile_api_failure_threshold",
            "reconcile_min_interval_sec",
            "reconcile_retry_scheduled",
            "reconciler",
            "recovered_guard_cleanup_pending",
            "rejected_risk_control_heartbeat_sources",
            "require_explicit_strategy_budget",
            "require_healthy_clock",
            "reserved_risk_messages_per_window",
            "rest_confirmed_execution_ids",
            "risk_control_heartbeat_enabled",
            "risk_control_heartbeat_max_age_sec",
            "risk_control_heartbeat_reason",
            "risk_control_heartbeat_required_source",
            "risk_control_heartbeat_source",
            "risk_control_heartbeat_status",
            "risk_rejection_log_interval_sec",
            "rpi_calibration_manager",
            "rpi_calibration_replay",
            "rpi_calibration_runtime",
            "self_trade_prevention_enabled",
            "shutdown_cancel_settle_interval_sec",
            "shutdown_cancel_timeout_sec",
            "shutdown_empty_snapshots_required",
            "single_writer_fence",
            "snapshot_max_attempts",
            "snapshot_settle_interval_sec",
            "snapshot_stability_required",
            "state",
            "strategy_guards",
            "strategy_risk_budgets",
            "strategy_risk_budgets_enabled",
            "strategy_symbol_guards",
            "submit_settlement",
            "symbol_guard_epoch_counters",
            "symbol_guard_epochs",
            "symbol_guard_records",
            "symbol_guards",
            "terminated_oid_queue",
            "terminated_oids",
            "trade_cursors",
            "trade_recovery_id_overlap",
            "trade_recovery_lookback_ms",
            "trade_recovery_overlap_ms",
            "trade_scan_end_ms",
            "trade_tail_expected_ids",
            "trade_tail_verification_attempts",
            "trade_tail_verification_delay_sec",
            "trade_tail_verification_inflight",
            "trade_tail_verification_retry_sec",
            "unknown_order_min_not_found",
            "unknown_order_resolution_timeout_sec",
            "validator",
            "venue_dead_man_armed_symbols",
            "venue_dead_man_failure_count",
            "venue_dead_man_last_error",
            "venue_dead_man_recovery_count",
            "venue_dead_man_renewal_inflight",
            "venue_dead_man_safety_cancel_retry_sec",
            "venue_dead_man_safety_cancel_timeout_sec",
            "venue_dead_man_switch_countdown_time_ms",
            "venue_dead_man_switch_enabled",
            "venue_dead_man_switch_max_renewal_age_sec",
            "venue_dead_man_switch_recovery_checks",
            "venue_dead_man_switch_renewal_interval_sec",
            "venue_dead_man_switch_symbols",
            "venue_guard_epoch_counters",
            "venue_guard_epochs",
            "venue_guard_records",
            "venue_guards",
        }
    )
    OWNER_READS = OWNER_WRITES | frozenset(
        {
            "SUPPORTED_POSITION_MODES",
            "_apply_rebuild_summary",
            "_on_background_task_error",
            "_on_order_truth_check",
            "_tracked_quote_assets",
            "freeze_system",
            "rebuild_from_log",
        }
    )

    def initialize(self, event_engine, gateway, config) -> None:
        self.event_engine = event_engine
        self.gateway = gateway
        self.config = config
        oms_cfg = config.get("oms", {})

        target_position_mode = self._configure_order_controls(config, oms_cfg)
        self._initialize_shared_state(config)
        self._configure_account_and_risk(config, oms_cfg, target_position_mode)
        self._initialize_components(event_engine, gateway, config)
        self._configure_recovery_and_outbound(config, oms_cfg)
        self._initialize_paper_trade_database(config)
        try:
            self._initialize_background_tasks(oms_cfg)
        except Exception:
            paper_database = getattr(self, "paper_trade_database", None)
            if paper_database is not None:
                paper_database.close(
                    clean_shutdown=False,
                    reason="oms_initialization_failed",
                )
            raise

    def _initialize_paper_trade_database(self, config) -> None:
        self.paper_trade_database = None
        database_config = config.get("paper_trade_database", {}) or {}
        if not isinstance(database_config, dict):
            raise ValueError("paper_trade_database must be an object")
        if bool(database_config.get("enabled", False)):
            self.paper_trade_database = PaperTradeDatabase(
                config,
                self.journal,
                failure_callback=self._on_paper_database_failure,
            )

    def _on_paper_database_failure(self, reason: str) -> None:
        self.freeze_system(
            f"paper_trade_database_unhealthy:{reason}",
            cancel_active_orders=True,
        )

    def _configure_order_controls(self, config, oms_cfg) -> str:
        target_position_mode = str(
            config.get("account", {}).get("position_mode", "ONE_WAY")
            or "ONE_WAY"
        ).upper()
        if target_position_mode not in self.SUPPORTED_POSITION_MODES:
            raise ValueError(
                "Unsupported position mode "
                f"{target_position_mode!r}: the OMS uses a one-way net-position "
                "ledger and must not configure Binance Hedge Mode"
            )

        self_trade_prevention = oms_cfg.get("self_trade_prevention", {})
        self.self_trade_prevention_enabled = bool(
            self_trade_prevention.get("enabled", False)
        )
        self.local_self_cross_check_enabled = bool(
            self_trade_prevention.get("local_cross_check", True)
        )
        configured_stp_mode = str(
            self_trade_prevention.get("exchange_mode", "EXPIRE_MAKER") or ""
        ).upper()
        allowed_stp_modes = {
            "EXPIRE_TAKER",
            "EXPIRE_MAKER",
            "EXPIRE_BOTH",
        }
        if (
            self.self_trade_prevention_enabled
            and configured_stp_mode not in allowed_stp_modes
        ):
            raise ValueError(
                f"Unsupported Binance STP mode: {configured_stp_mode or '<empty>'}"
            )
        self.exchange_self_trade_prevention_mode = (
            configured_stp_mode if self.self_trade_prevention_enabled else ""
        )

        venue_dead_man_switch = oms_cfg.get("venue_dead_man_switch", {})
        self.venue_dead_man_switch_enabled = bool(
            venue_dead_man_switch.get("enabled", False)
        )
        self.venue_dead_man_switch_countdown_time_ms = int(
            venue_dead_man_switch.get("countdown_time_ms", 120_000) or 0
        )
        self.venue_dead_man_switch_renewal_interval_sec = float(
            venue_dead_man_switch.get("renewal_interval_sec", 30.0) or 0.0
        )
        self.venue_dead_man_switch_max_renewal_age_sec = float(
            venue_dead_man_switch.get("max_renewal_age_sec", 45.0) or 0.0
        )
        self.venue_dead_man_switch_recovery_checks = max(
            1,
            int(venue_dead_man_switch.get("recovery_checks", 2) or 1),
        )
        self.venue_dead_man_safety_cancel_timeout_sec = max(
            1.0,
            float(
                venue_dead_man_switch.get(
                    "safety_cancel_timeout_sec",
                    10.0,
                )
                or 10.0
            ),
        )
        self.venue_dead_man_safety_cancel_retry_sec = max(
            0.25,
            float(
                venue_dead_man_switch.get(
                    "safety_cancel_retry_sec",
                    2.0,
                )
                or 2.0
            ),
        )
        self.venue_dead_man_switch_symbols = frozenset(
            str(symbol or "").upper()
            for symbol in config.get("symbols", [])
            if str(symbol or "").strip()
        )
        if self.venue_dead_man_switch_enabled:
            countdown_sec = self.venue_dead_man_switch_countdown_time_ms / 1000.0
            if self.venue_dead_man_switch_countdown_time_ms <= 0:
                raise ValueError(
                    "venue_dead_man_switch.countdown_time_ms must be positive"
                )
            if self.venue_dead_man_switch_renewal_interval_sec <= 0.0:
                raise ValueError(
                    "venue_dead_man_switch.renewal_interval_sec must be positive"
                )
            if (
                self.venue_dead_man_switch_max_renewal_age_sec
                <= self.venue_dead_man_switch_renewal_interval_sec
            ):
                raise ValueError(
                    "venue_dead_man_switch.max_renewal_age_sec must be greater "
                    "than renewal_interval_sec"
                )
            if self.venue_dead_man_switch_max_renewal_age_sec >= countdown_sec:
                raise ValueError(
                    "venue_dead_man_switch.max_renewal_age_sec must be less "
                    "than the exchange countdown"
                )
            if not self.venue_dead_man_switch_symbols:
                raise ValueError(
                    "venue_dead_man_switch requires at least one configured symbol"
                )
        return target_position_mode

    def _initialize_shared_state(self, config) -> None:
        self.state = LifecycleState.BOOTSTRAP
        self._lifecycle_generation = 0

        oms_cfg = config.get("oms", {})
        oms_cfg = oms_cfg if isinstance(oms_cfg, dict) else {}
        event_log_max = oms_cfg.get("event_log_max", 2048)
        if (
            isinstance(event_log_max, bool)
            or not isinstance(event_log_max, int)
            or event_log_max <= 0
        ):
            raise ValueError("oms.event_log_max must be a positive integer")
        self.event_log_max = event_log_max
        self.event_log = deque(maxlen=event_log_max)
        self.event_log_evictions = 0
        self.orders = {}
        # Cancels received during submit settlement either fence the POST or
        # run once settlement establishes whether the order can be live.
        self._submit_settlement_inflight_oids = set()
        self._submit_cancel_requested_oids = set()
        self.exchange_id_map = {}
        self.symbol_guards = {}
        # Independent guard owners remain until each owner proves recovery.
        self.symbol_guard_records = {}
        self.symbol_guard_epochs = {}
        self.symbol_guard_epoch_counters = {}
        self.venue_guards = {}
        self.venue_guard_records = {}
        self.venue_guard_epochs = {}
        self.venue_guard_epoch_counters = {}
        self.strategy_guards = {}
        self.strategy_symbol_guards = {}
        self.lock = threading.RLock()
        self._outbound_gate_condition = threading.Condition()
        self._outbound_gate_open = False
        self._outbound_gate_epoch = 0
        self._outbound_gate_reason = "startup_bootstrap"
        self._outbound_gate_holds = set()
        self._outbound_all_order_seal_reason = ""
        self._outbound_risk_sends_inflight = 0
        self._outbound_order_sends_inflight = 0
        self._outbound_risk_sends_inflight_by_symbol = {}
        self._shutdown_requested = False
        self._shutdown_reason = ""
        self._shutdown_cancel_verified = False
        self.rpi_calibration_manager = RpiCalibrationManager(config)
        self._rpi_calibration = self.rpi_calibration_manager.runtime_config
        self._rpi_calibration_permit_activated = False
        self._rpi_calibration_expired = False
        self._rpi_calibration_expiry_reason = ""
        self._rpi_calibration_budget_exhausted = False
        self._rpi_calibration_restart_rearm_blocked = False
        self._rpi_calibration_reserved_order_count = 0
        self._rpi_calibration_cumulative_notional_microu = 0
        self._rpi_calibration_last_reserved_exchange_ns = 0
        self._rpi_calibration_permit_start_order_count = 0
        self._rpi_calibration_permit_start_notional_microu = 0
        self._rpi_calibration_start_equity_microu = 0
        self._rpi_calibration_start_external_cash_flow_microu = 0
        self._rpi_calibration_peak_observed_loss_microu = 0
        self._rpi_calibration_effective_loss_cap_microu = int(
            self._rpi_calibration.get(
                "max_calibration_loss_microu",
                0,
            )
            or 0
        )
        self._rpi_calibration_reservation_ids = set()
        self._rpi_calibration_reservation_exchange_ns = {}
        self._rpi_calibration_ttl_cancel_oids = set()
        self._rpi_calibration_enforcement_inflight = False
        self._rpi_calibration_terminal_cancel_sweep_completed = False
        self._rpi_calibration_terminal_empty_snapshots = 0
        self._rpi_calibration_terminal_verified = False
        self._rpi_calibration_terminal_pending_reason = ""
        self._rpi_calibration_terminal_generation = 0
        self._rpi_calibration_enforcement_thread = None
        self._known_account_order_symbols = {
            str(symbol or "").upper()
            for symbol in config.get("symbols", [])
            if str(symbol or "").strip()
        }
        # User-stream account and order updates can arrive in either order.
        self._position_state_event_time = {}
        self._exchange_position_event_time = {}
        self._account_state_event_time = 0.0
        self._exchange_account_event_time = 0.0
        self.capability_mode = OMSCapabilityMode.READ_ONLY
        self.capability_reason = "startup_bootstrap"
        self.mode_override = None
        self.mode_override_reason = ""
        self.mode_constraints = {}
        self.mode_constraint_generations = {}
        self.mode_constraint_generation = 0

    def _configure_account_and_risk(
        self,
        config,
        oms_cfg,
        target_position_mode: str,
    ) -> None:
        account_config = config.get("account", {}) or {}
        target_leverage = int(account_config.get("leverage", 0) or 0)
        if target_leverage > 0:
            self.gateway.target_leverage = target_leverage
        target_margin_type = str(
            account_config.get("margin_type", "CROSSED") or "CROSSED"
        ).upper()
        self.gateway.target_margin_type = target_margin_type
        self.gateway.target_position_mode = target_position_mode
        account_configuration_mode = (
            str(account_config.get("configuration_mode", "") or "")
            .strip()
            .upper()
        )
        live_stage = (
            str((config.get("live_launch", {}) or {}).get("stage", "") or "")
            .strip()
            .lower()
        )
        if live_stage in {"canary", "rpi_calibration_canary"}:
            if account_configuration_mode != "VERIFY_ONLY":
                raise ValueError(
                    "Live canary account.configuration_mode must be VERIFY_ONLY"
                )
        elif not account_configuration_mode:
            account_configuration_mode = "APPLY"
        self.gateway.account_configuration_mode = account_configuration_mode

        risk_limits = config.get("risk", {}).get("limits", {}) or {}
        self.max_pos_notional = risk_limits.get("max_pos_notional", 2000.0)
        self.max_account_gross_notional = risk_limits.get(
            "max_account_gross_notional",
            0.0,
        )
        scaling_config = (
            config.get("strategy", {}).get("capital_scaling", {}) or {}
        )
        self.max_concurrent_symbols = max(
            0,
            int(
                risk_limits.get(
                    "max_concurrent_symbols",
                    scaling_config.get("target_concurrent_symbols", 0),
                )
                or 0
            ),
        )
        strategy_budget_config = (
            config.get("risk", {}).get("strategy_risk_budgets", {}) or {}
        )
        self.strategy_risk_budgets_enabled = bool(
            strategy_budget_config.get("enabled", False)
        )
        self.require_explicit_strategy_budget = bool(
            strategy_budget_config.get("require_explicit_strategy", True)
        )
        self.strategy_risk_budgets = {}
        configured_strategy_budgets = strategy_budget_config.get("budgets", {})
        if not isinstance(configured_strategy_budgets, dict):
            configured_strategy_budgets = {}
        for strategy_id, raw_budget in configured_strategy_budgets.items():
            strategy_id = str(strategy_id or "").strip()
            if not strategy_id or not isinstance(raw_budget, dict):
                continue
            max_gross = float(
                raw_budget.get("max_gross_notional", 0.0) or 0.0
            )
            max_symbol = float(
                raw_budget.get("max_symbol_notional", 0.0) or 0.0
            )
            if (
                not math.isfinite(max_gross)
                or not math.isfinite(max_symbol)
                or max_gross <= 0.0
                or max_symbol <= 0.0
                or max_symbol > max_gross
            ):
                raise ValueError(
                    f"Invalid strategy risk budget for {strategy_id!r}: "
                    "require 0 < max_symbol_notional <= max_gross_notional"
                )
            self.strategy_risk_budgets[strategy_id] = {
                "max_gross_notional": max_gross,
                "max_symbol_notional": max_symbol,
            }
        margin_health = config.get("risk", {}).get("margin_health", {})
        self.margin_health_enabled = bool(
            margin_health.get("enabled", False)
        )
        self.margin_health_require_snapshot = bool(
            margin_health.get("require_snapshot", True)
        )
        self.margin_snapshot_max_age_sec = max(
            0.0,
            float(margin_health.get("max_snapshot_age_sec", 15.0) or 0.0),
        )
        self.margin_reduce_only_ratio = max(
            0.0,
            float(margin_health.get("reduce_only_ratio", 0.70) or 0.0),
        )

        cash_flow_truth = config.get("risk", {}).get("cash_flow_truth", {})
        self.external_cash_flow_truth_enabled = bool(
            cash_flow_truth.get("enabled", False)
        )
        self.external_cash_flow_require_snapshot = bool(
            cash_flow_truth.get("require_snapshot", True)
        )
        self.external_cash_flow_max_age_sec = max(
            0.0,
            float(
                cash_flow_truth.get("max_snapshot_age_sec", 45.0) or 0.0
            ),
        )
        configured_income_types = cash_flow_truth.get(
            "external_income_types",
            ["TRANSFER"],
        )
        if not isinstance(configured_income_types, (list, tuple, set)):
            configured_income_types = ["TRANSFER"]
        self.external_income_types = frozenset(
            str(item or "").upper()
            for item in configured_income_types
            if str(item or "").strip()
        )
        self.external_cash_flow_assets = self._tracked_quote_assets(
            config.get("symbols", [])
        )
        self.external_cash_flow_ids = set()
        self.external_cash_flow_scan_end_ms = 0

        risk_control_heartbeat = config.get("risk", {}).get(
            "risk_control_heartbeat",
            {},
        )
        self.risk_control_heartbeat_enabled = bool(
            risk_control_heartbeat.get("enabled", False)
        )
        self.risk_control_heartbeat_max_age_sec = max(
            0.05,
            float(risk_control_heartbeat.get("max_age_sec", 2.0) or 2.0),
        )
        self.risk_control_heartbeat_required_source = str(
            risk_control_heartbeat.get("required_source", "") or ""
        ).strip()
        time_sync_config = config.get("system", {}).get("time_sync", {}) or {}
        self.require_healthy_clock = bool(
            time_sync_config.get(
                "require_healthy_for_trading",
                bool(time_sync_config),
            )
        )
        self.last_risk_control_heartbeat_monotonic = 0.0
        self.last_risk_control_heartbeat_time = 0.0
        self.risk_control_heartbeat_status = "missing"
        self.risk_control_heartbeat_source = ""
        self.risk_control_heartbeat_reason = ""
        self.rejected_risk_control_heartbeat_sources = set()
        self.last_venue_dead_man_attempt_monotonic = 0.0
        self.last_venue_dead_man_success_monotonic = 0.0
        self.last_venue_dead_man_success_time = 0.0
        self.venue_dead_man_armed_symbols = set()
        self.venue_dead_man_failure_count = 0
        self.venue_dead_man_recovery_count = 0
        self.venue_dead_man_last_error = ""
        self.venue_dead_man_renewal_inflight = False
        self._venue_dead_man_renewal_thread = None
        self._venue_dead_man_safety_cancel_thread = None
        self._venue_dead_man_safety_cancel_last_attempt = 0.0

        single_writer_cfg = oms_cfg.get("single_writer_fence", {})
        self.single_writer_fence = None
        if bool(single_writer_cfg.get("enabled", False)):
            journal_path = str(
                oms_cfg.get(
                    "journal_path",
                    "storage/oms/oms_journal.jsonl",
                )
            )
            fence_path = str(
                single_writer_cfg.get("path", "") or f"{journal_path}.lock"
            )
            self.single_writer_fence = SingleWriterFence(
                fence_path,
                owner_metadata={
                    "component": "ChronosHFT.OMS",
                    "gateway": str(
                        getattr(self.gateway, "gateway_name", "UNKNOWN")
                        or "UNKNOWN"
                    ),
                    "symbols": sorted(
                        str(symbol) for symbol in config.get("symbols", [])
                    ),
                    "journal_path": journal_path,
                },
            )
            self.single_writer_fence.acquire()

    def _initialize_components(
        self,
        event_engine,
        gateway,
        config,
    ) -> None:
        self.validator = OrderValidator(config)
        self.exposure = ExposureManager()
        self.account = AccountManager(event_engine, self.exposure, config)
        self.account_truth = self._spawn_component(OMSAccountTruth)
        self.order_monitor = OrderManager(
            event_engine,
            gateway,
            self._on_order_truth_check,
            config.get("oms", {}),
        )

        self.journal = OMSJournal(config)
        self.audit_logger = OMSAuditLogger(self.journal)
        if self._rpi_calibration["enabled"] and not (
            self.journal.enabled
            and self.journal.replay_on_startup
            and self.journal.fsync_enabled
            and self.journal.integrity_check_enabled
        ):
            raise ValueError(
                "RPI calibration requires an enabled, fsync-backed, "
                "integrity-checked OMS journal with startup replay"
            )
        self.TOMBSTONE_MAX = config.get("oms", {}).get(
            "tombstone_max",
            2000,
        )
        self.terminated_oids = set()
        self.terminated_oid_queue = deque()
        self.reconcile_retry_scheduled = False
        self._order_truth_resolution_inflight = set()
        self._unknown_not_found_counts = {}
        self.trade_cursors = {}
        self.trade_scan_end_ms = {}
        self.execution_ids = set()
        self.rest_confirmed_execution_ids = set()
        self.trade_tail_verification_inflight = set()
        self.trade_tail_expected_ids = {}
        self.manual_rearm_required = False
        self.last_freeze_reason = ""
        self.last_halt_reason = ""
        self.recovered_guard_cleanup_pending = False
        self._recovered_guard_cleanup_snapshot = None
        self.rpi_calibration_replay = self._spawn_component(
            RpiCalibrationReplay
        )
        self.rpi_calibration_runtime = self._spawn_component(
            RpiCalibrationRuntime
        )
        self.guard_manager = self._spawn_component(OMSGuardManager)
        self.capability_manager = self._spawn_component(OMSCapabilityManager)
        self.cancellation_manager = self._spawn_component(
            OMSCancellationManager
        )
        self.outbound_gate = self._spawn_component(OMSOutboundGate)
        self.recovery_state_restorer = self._spawn_component(
            OMSRecoveryStateRestorer
        )
        self.lifecycle_controller = self._spawn_component(
            OMSLifecycleController
        )
        self.exchange_event_processor = self._spawn_component(
            OMSExchangeEventProcessor
        )
        self.journal_rebuilder = self._spawn_component(OMSJournalRebuilder)
        self.full_reset_coordinator = self._spawn_component(
            OMSFullResetCoordinator
        )
        self.reconciler = self._spawn_component(OMSReconciler)
        self.order_policy = self._spawn_component(OMSOrderPolicy)
        self.order_submission = self._spawn_component(OMSOrderSubmission)
        self.submit_settlement = self._spawn_component(OMSSubmitSettlement)
        self.rebuild_summary = self.rebuild_from_log()
        self._apply_rebuild_summary()

    def _configure_recovery_and_outbound(self, config, oms_cfg) -> None:
        self.reconcile_min_interval_sec = float(
            oms_cfg.get("reconcile_min_interval_sec", 5.0)
        )
        self.reconcile_api_failure_threshold = int(
            oms_cfg.get("reconcile_api_failure_threshold", 3)
        )
        self.reconcile_api_cooldown_sec = float(
            oms_cfg.get("reconcile_api_cooldown_sec", 10.0)
        )
        self.unknown_order_min_not_found = max(
            2,
            int(oms_cfg.get("unknown_order_min_not_found", 3)),
        )
        self.unknown_order_resolution_timeout_sec = max(
            1.0,
            float(
                oms_cfg.get("unknown_order_resolution_timeout_sec", 15.0)
            ),
        )
        self.trade_recovery_lookback_ms = max(
            60_000,
            int(oms_cfg.get("trade_recovery_lookback_ms", 86_400_000)),
        )
        self.trade_recovery_overlap_ms = max(
            0,
            int(oms_cfg.get("trade_recovery_overlap_ms", 60_000)),
        )
        self.trade_recovery_id_overlap = max(
            0,
            int(oms_cfg.get("trade_recovery_id_overlap", 100)),
        )
        self.trade_tail_verification_delay_sec = max(
            0.0,
            float(
                oms_cfg.get("trade_tail_verification_delay_sec", 0.25)
            ),
        )
        self.trade_tail_verification_retry_sec = max(
            0.05,
            float(
                oms_cfg.get("trade_tail_verification_retry_sec", 0.50)
            ),
        )
        self.trade_tail_verification_attempts = max(
            1,
            int(oms_cfg.get("trade_tail_verification_attempts", 4)),
        )
        cash_flow_truth = config.get("risk", {}).get("cash_flow_truth", {})
        self.external_cash_flow_recovery_lookback_ms = max(
            86_400_000,
            int(
                cash_flow_truth.get("recovery_lookback_ms", 86_400_000)
                or 86_400_000
            ),
        )
        self.external_cash_flow_recovery_overlap_ms = max(
            0,
            int(cash_flow_truth.get("recovery_overlap_ms", 60_000) or 60_000),
        )
        self.external_cash_flow_max_pages = max(
            1,
            int(cash_flow_truth.get("max_pages", 20) or 20),
        )
        self.external_cash_flow_poll_interval_sec = max(
            1.0,
            float(cash_flow_truth.get("poll_interval_sec", 30.0) or 30.0),
        )
        self.last_external_cash_flow_poll_at = 0.0
        self.snapshot_stability_required = max(
            2,
            int(oms_cfg.get("snapshot_stability_required", 2)),
        )
        self.snapshot_max_attempts = max(
            self.snapshot_stability_required,
            int(oms_cfg.get("snapshot_max_attempts", 6)),
        )
        self.snapshot_settle_interval_sec = max(
            0.01,
            float(oms_cfg.get("snapshot_settle_interval_sec", 0.25)),
        )
        gateway_rest = getattr(self.gateway, "rest", None)
        default_command_fence_sec = (
            float(getattr(gateway_rest, "recv_window_ms", 0) or 0) / 1000.0
            + 0.25
            if gateway_rest is not None
            else 0.0
        )
        self.command_fence_timeout_sec = max(
            0.0,
            float(
                oms_cfg.get(
                    "command_fence_timeout_sec",
                    default_command_fence_sec,
                )
            ),
        )
        self.max_total_active_orders = int(
            oms_cfg.get("max_total_active_orders", 100) or 0
        )
        self.max_symbol_active_orders = int(
            oms_cfg.get("max_symbol_active_orders", 20) or 0
        )
        self.max_strategy_active_orders = int(
            oms_cfg.get("max_strategy_active_orders", 30) or 0
        )
        self.max_strategy_symbol_active_orders = int(
            oms_cfg.get("max_strategy_symbol_active_orders", 10) or 0
        )
        self.duplicate_intent_window_sec = max(
            0.0,
            float(oms_cfg.get("duplicate_intent_window_ms", 250.0) or 0.0)
            / 1000.0,
        )
        self.risk_rejection_log_interval_sec = max(
            0.0,
            float(oms_cfg.get("risk_rejection_log_interval_sec", 5.0) or 0.0),
        )
        outbound_budget = oms_cfg.get("outbound_message_budget", {})
        self._outbound_budget = OutboundMessageBudget(outbound_budget)
        self.outbound_message_budget_enabled = self._outbound_budget.enabled
        self.outbound_message_window_sec = self._outbound_budget.window_sec
        self.max_total_messages_per_window = self._outbound_budget.max_total
        self.max_new_orders_per_window = self._outbound_budget.max_new_orders
        self.max_reduce_orders_per_window = (
            self._outbound_budget.max_reduce_orders
        )
        self.max_cancel_messages_per_window = self._outbound_budget.max_cancels
        self.reserved_risk_messages_per_window = (
            self._outbound_budget.reserved_risk_messages
        )
        self.outbound_message_history = self._outbound_budget.history
        self._deferred_cancel_oids = set()
        self._deferred_cancel_all_symbols = set()
        self._stopped = False
        self.outbound_gate_drain_timeout_sec = max(
            0.1,
            float(
                oms_cfg.get("outbound_gate_drain_timeout_sec", 10.0) or 10.0
            ),
        )
        self.shutdown_cancel_timeout_sec = max(
            1.0,
            float(oms_cfg.get("shutdown_cancel_timeout_sec", 30.0) or 30.0),
        )
        self.shutdown_empty_snapshots_required = max(
            2,
            int(oms_cfg.get("shutdown_empty_snapshots_required", 2) or 2),
        )
        self.shutdown_cancel_settle_interval_sec = max(
            0.01,
            float(
                oms_cfg.get("shutdown_cancel_settle_interval_sec", 0.25)
                or 0.25
            ),
        )
        self.degraded_aggressive_to_passive = bool(
            oms_cfg.get("degraded_aggressive_to_passive", True)
        )
        self.emergency_flatten_cooldown_sec = max(
            0.0,
            float(
                oms_cfg.get("emergency_flatten_cooldown_sec", 5.0) or 0.0
            ),
        )
        self.last_emergency_flatten_ts = {}
        self.last_reconcile_request_ts = 0.0
        self.last_reconcile_failure_ts = 0.0
        self.consecutive_reconcile_api_failures = 0
        self._reconcile_thread = None

    def _initialize_background_tasks(self, oms_cfg) -> None:
        background_cfg = oms_cfg.get("background_tasks", {}) or {}
        background_workers = max(
            2,
            int(background_cfg.get("max_workers", 8) or 8),
        )
        background_safety_workers = min(
            background_workers - 1,
            max(
                1,
                int(background_cfg.get("safety_workers", 2) or 2),
            ),
        )
        self._pending_reconcile_requests = []
        self._max_pending_reconcile_requests = max(
            8,
            int(
                background_cfg.get(
                    "max_pending_reconcile_requests",
                    256,
                )
                or 256
            ),
        )
        self._background_tasks = OMSBackgroundTaskExecutor(
            max_workers=background_workers,
            safety_workers=background_safety_workers,
            queue_capacity=max(
                1,
                int(background_cfg.get("queue_capacity", 64) or 64),
            ),
            safety_queue_capacity=max(
                1,
                int(
                    background_cfg.get("safety_queue_capacity", 16) or 16
                ),
            ),
            max_pending_tasks=max(
                background_workers,
                int(background_cfg.get("max_pending_tasks", 96) or 96),
            ),
            error_handler=self._on_background_task_error,
        )
