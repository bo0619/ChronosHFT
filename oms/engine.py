import json
import math
import threading
import time
from collections import deque
from decimal import Decimal

from infrastructure.commission_truth import resolve_passive_fee_rate
from infrastructure.logger import logger
from infrastructure.single_writer_fence import SingleWriterFence
from infrastructure.time_service import time_service

from event.type import (
    CommandOutcome,
    Event,
    ExchangeOrderUpdate,
    LifecycleState,
    OMSCapabilityMode,
    OrderIntent,
    OrderRequest,
    OrderSubmitResult,
    EVENT_ORDER_UPDATE,
    EVENT_POSITION_UPDATE,
    EVENT_SYSTEM_HEALTH,
)

from .account_manager import AccountManager
from .account_truth import OMSAccountTruth
from .audit_logger import OMSAuditLogger
from .background_tasks import OMSBackgroundTaskExecutor
from .capability_manager import OMSCapabilityManager
from .cancellation_manager import OMSCancellationManager
from .component import component_method
from .exchange_event_processor import OMSExchangeEventProcessor
from .exposure import ExposureManager
from .guard_manager import OMSGuardManager
from .journal import JournalError, OMSJournal
from .journal_rebuilder import OMSJournalRebuilder
from .lifecycle_controller import OMSLifecycleController
from .order import Order
from .order_manager import OrderManager
from .order_policy import OMSOrderPolicy
from .order_submission import OMSOrderSubmission
from .outbound_budget import OutboundMessageBudget
from .reconciler import OMSReconciler
from .rpi_calibration_manager import RpiCalibrationManager
from .rpi_calibration_replay import RpiCalibrationReplay
from .rpi_calibration_runtime import RpiCalibrationRuntime
from .submit_settlement import OMSSubmitSettlement
from .validator import OrderValidator


class OMS:
    """Coordinate deterministic OMS components for one perpetual account.

    The facade retains shared lifecycle, locks and account-level ledgers.
    Domain workflows such as submission, replay, guards and reconciliation
    are delegated to focused components in this package.
    """

    SUPPORTED_POSITION_MODES = frozenset({"ONE_WAY"})
    RPI_CALIBRATION_STAGE = RpiCalibrationManager.RPI_CALIBRATION_STAGE
    RPI_CALIBRATION_VENUE = RpiCalibrationManager.RPI_CALIBRATION_VENUE
    RPI_CALIBRATION_MODEL = RpiCalibrationManager.RPI_CALIBRATION_MODEL
    RPI_CALIBRATION_STRATEGY_ID = "GLFT_MultiScale"
    RPI_CALIBRATION_JOURNAL_SCHEMA = "chronoshft.oms_rpi_calibration_quota.v1"
    RPI_CALIBRATION_PERMIT_KEYS = (
        RpiCalibrationManager.RPI_CALIBRATION_PERMIT_KEYS
    )
    RPI_CALIBRATION_POLICY_KEYS = (
        RpiCalibrationManager.RPI_CALIBRATION_POLICY_KEYS
    )
    RPI_CALIBRATION_SIGNATURE_KEYS = (
        RpiCalibrationManager.RPI_CALIBRATION_SIGNATURE_KEYS
    )
    RPI_CALIBRATION_ACTIVATION_PAYLOAD_KEYS = frozenset(
        {
            "schema",
            "signed_permit",
            "permit_id",
            "permit_sha256",
            "deployment_id",
            "stage",
            "venue",
            "symbol",
            "model",
            "calibration_config_sha256",
            "target_deployment_config_sha256",
            "strategy_policy_sha256",
            "implementation_sha256",
            "activated_at_exchange_ns",
            "not_before_exchange_ns",
            "expires_at_exchange_ns",
            "fixed_depths_bps",
            "order_ttl_ns",
            "min_order_interval_ns",
            "max_active_orders",
            "max_order_count",
            "min_order_notional_microu",
            "max_order_notional_microu",
            "max_cumulative_submitted_notional_microu",
            "max_calibration_loss_microu",
            "effective_deployment_loss_cap_microu",
            "deployment_start_equity_microu",
            "deployment_start_external_cash_flow_microu",
            "peak_observed_loss_microu",
            "starting_reserved_order_count",
            "starting_cumulative_submitted_notional_microu",
        }
    )
    RPI_CALIBRATION_RESERVATION_PAYLOAD_KEYS = frozenset(
        {
            "schema",
            "reservation_seq",
            "permit_reservation_seq",
            "reservation_id",
            "client_oid",
            "permit_id",
            "permit_sha256",
            "deployment_id",
            "calibration_config_sha256",
            "target_deployment_config_sha256",
            "strategy_policy_sha256",
            "implementation_sha256",
            "reserved_at_exchange_ns",
            "symbol",
            "strategy_id",
            "side",
            "price",
            "quantity",
            "declared_depth_bps",
            "calibration_reference_mid",
            "order_type",
            "time_in_force",
            "post_only",
            "reduce_only",
            "submitted_notional_microu",
            "cumulative_submitted_notional_microu",
            "permit_cumulative_submitted_notional_microu",
            "loss_before_send_microu",
            "effective_deployment_loss_cap_microu",
        }
    )
    RPI_CALIBRATION_EXPIRY_PAYLOAD_KEYS = frozenset(
        {
            "schema",
            "signed_permit",
            "permit_id",
            "permit_sha256",
            "deployment_id",
            "symbol",
            "calibration_config_sha256",
            "target_deployment_config_sha256",
            "strategy_policy_sha256",
            "implementation_sha256",
            "reason",
            "budget_exhausted",
            "expired_at_exchange_ns",
            "reserved_order_count",
            "cumulative_submitted_notional_microu",
            "deployment_start_equity_microu",
            "deployment_start_external_cash_flow_microu",
            "peak_observed_loss_microu",
            "effective_deployment_loss_cap_microu",
        }
    )
    RPI_CALIBRATION_BYPASS_PAYLOAD_KEYS = frozenset(
        {
            "schema",
            "bypass_id",
            "client_oid",
            "permit_id",
            "permit_sha256",
            "deployment_id",
            "recorded_at_exchange_ns",
            "symbol",
            "side",
            "price",
            "quantity",
            "estimated_notional_microu",
            "reduce_only",
            "reason",
        }
    )
    USDT_MICRO_SCALE = RpiCalibrationManager.USDT_MICRO_SCALE
    OUTBOUND_NEW_ORDER = "NEW_ORDER"
    OUTBOUND_REDUCE_ORDER = "REDUCE_ORDER"
    OUTBOUND_CANCEL = "CANCEL"

    _component_factories = {
        "account_truth": OMSAccountTruth,
        "capability_manager": OMSCapabilityManager,
        "cancellation_manager": OMSCancellationManager,
        "lifecycle_controller": OMSLifecycleController,
        "order_policy": OMSOrderPolicy,
        "submit_settlement": OMSSubmitSettlement,
    }

    CASH_FLOW_DIRTY_REASONS = frozenset(
        {
            "TRANSFER",
            "DEPOSIT",
            "WITHDRAW",
            "MARGIN_TRANSFER",
            "CROSS_COLLATERAL_TRANSFER",
            "ASSET_TRANSFER",
        }
    )

    @property
    def rpi_calibration_runtime(self) -> RpiCalibrationRuntime:
        runtime = self.__dict__.get("_rpi_calibration_runtime")
        if runtime is None:
            runtime = RpiCalibrationRuntime(self)
            self.__dict__["_rpi_calibration_runtime"] = runtime
        return runtime

    @rpi_calibration_runtime.setter
    def rpi_calibration_runtime(self, runtime: RpiCalibrationRuntime) -> None:
        self.__dict__["_rpi_calibration_runtime"] = runtime

    def __init__(self, event_engine, gateway, config):
        self.event_engine = event_engine
        self.gateway = gateway
        self.config = config
        oms_cfg = config.get("oms", {})

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

        self.state = LifecycleState.BOOTSTRAP
        self._lifecycle_generation = 0

        self.event_log = []
        self.orders = {}
        # A submit remains in-flight from its first local reservation until
        # the transport outcome has been durably settled.  Cancels received
        # during that interval are coalesced and either fence the POST or run
        # once settlement has established whether the order can be live.
        self._submit_settlement_inflight_oids = set()
        self._submit_cancel_requested_oids = set()
        self.exchange_id_map = {}
        self.symbol_guards = {}
        # A symbol can be unsafe for several independent reasons at once
        # (for example an order-book gap and a latency breaker).  Keep every
        # owner until that exact owner proves recovery; ``symbol_guards`` is
        # only the newest active reason retained for compatibility/UI use.
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
        # Exchange account updates and order updates for the same fill may arrive
        # in either order. These watermarks make position/balance application
        # idempotent regardless of that delivery order.
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

        account_config = config.get("account", {}) or {}
        target_leverage = int(account_config.get("leverage", 0) or 0)
        if target_leverage > 0:
            self.gateway.target_leverage = target_leverage
        target_margin_type = str(
            account_config.get("margin_type", "CROSSED") or "CROSSED"
        ).upper()
        self.gateway.target_margin_type = target_margin_type
        self.gateway.target_position_mode = target_position_mode
        account_configuration_mode = str(
            account_config.get("configuration_mode", "") or ""
        ).strip().upper()
        live_stage = str(
            (config.get("live_launch", {}) or {}).get("stage", "") or ""
        ).strip().lower()
        if live_stage in {"canary", "rpi_calibration_canary"}:
            if account_configuration_mode != "VERIFY_ONLY":
                raise ValueError(
                    "Live canary account.configuration_mode must be VERIFY_ONLY"
                )
        elif not account_configuration_mode:
            account_configuration_mode = "APPLY"
        self.gateway.account_configuration_mode = account_configuration_mode

        risk_limits = config.get("risk", {}).get("limits", {}) or {}
        self.max_pos_notional = risk_limits.get(
            "max_pos_notional",
            2000.0,
        )
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
            max_gross = float(raw_budget.get("max_gross_notional", 0.0) or 0.0)
            max_symbol = float(raw_budget.get("max_symbol_notional", 0.0) or 0.0)
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
        self.margin_health_enabled = bool(margin_health.get("enabled", False))
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
            float(cash_flow_truth.get("max_snapshot_age_sec", 45.0) or 0.0),
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

        risk_control_heartbeat = (
            config.get("risk", {}).get("risk_control_heartbeat", {})
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
        time_sync_config = (
            config.get("system", {}).get("time_sync", {}) or {}
        )
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
                    "symbols": sorted(str(symbol) for symbol in config.get("symbols", [])),
                    "journal_path": journal_path,
                },
            )
            self.single_writer_fence.acquire()

        self.validator = OrderValidator(config)
        self.exposure = ExposureManager()
        self.account = AccountManager(event_engine, self.exposure, config)
        self.account_truth = OMSAccountTruth(self)
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
        self.TOMBSTONE_MAX = config.get("oms", {}).get("tombstone_max", 2000)
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
        self.rpi_calibration_replay = RpiCalibrationReplay(self)
        self.rpi_calibration_runtime = RpiCalibrationRuntime(self)
        self.guard_manager = OMSGuardManager(self)
        self.capability_manager = OMSCapabilityManager(self)
        self.cancellation_manager = OMSCancellationManager(self)
        self.lifecycle_controller = OMSLifecycleController(self)
        self.exchange_event_processor = OMSExchangeEventProcessor(self)
        self.journal_rebuilder = OMSJournalRebuilder(self)
        self.reconciler = OMSReconciler(self)
        self.order_policy = OMSOrderPolicy(self)
        self.order_submission = OMSOrderSubmission(self)
        self.submit_settlement = OMSSubmitSettlement(self)
        self.rebuild_summary = self.rebuild_from_log()
        self._apply_rebuild_summary()

        self.reconcile_min_interval_sec = float(oms_cfg.get("reconcile_min_interval_sec", 5.0))
        self.reconcile_api_failure_threshold = int(oms_cfg.get("reconcile_api_failure_threshold", 3))
        self.reconcile_api_cooldown_sec = float(oms_cfg.get("reconcile_api_cooldown_sec", 10.0))
        self.unknown_order_min_not_found = max(
            2,
            int(oms_cfg.get("unknown_order_min_not_found", 3)),
        )
        self.unknown_order_resolution_timeout_sec = max(
            1.0,
            float(oms_cfg.get("unknown_order_resolution_timeout_sec", 15.0)),
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
            float(oms_cfg.get("trade_tail_verification_delay_sec", 0.25)),
        )
        self.trade_tail_verification_retry_sec = max(
            0.05,
            float(oms_cfg.get("trade_tail_verification_retry_sec", 0.50)),
        )
        self.trade_tail_verification_attempts = max(
            1,
            int(oms_cfg.get("trade_tail_verification_attempts", 4)),
        )
        cash_flow_truth = config.get("risk", {}).get("cash_flow_truth", {})
        self.external_cash_flow_recovery_lookback_ms = max(
            86_400_000,
            int(cash_flow_truth.get("recovery_lookback_ms", 86_400_000) or 86_400_000),
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
            float(getattr(gateway_rest, "recv_window_ms", 0) or 0) / 1000.0 + 0.25
            if gateway_rest is not None
            else 0.0
        )
        self.command_fence_timeout_sec = max(
            0.0,
            float(oms_cfg.get("command_fence_timeout_sec", default_command_fence_sec)),
        )
        self.max_total_active_orders = int(oms_cfg.get("max_total_active_orders", 100) or 0)
        self.max_symbol_active_orders = int(oms_cfg.get("max_symbol_active_orders", 20) or 0)
        self.max_strategy_active_orders = int(oms_cfg.get("max_strategy_active_orders", 30) or 0)
        self.max_strategy_symbol_active_orders = int(
            oms_cfg.get("max_strategy_symbol_active_orders", 10) or 0
        )
        self.duplicate_intent_window_sec = max(
            0.0,
            float(oms_cfg.get("duplicate_intent_window_ms", 250.0) or 0.0) / 1000.0,
        )
        outbound_budget = oms_cfg.get("outbound_message_budget", {})
        self._outbound_budget = OutboundMessageBudget(outbound_budget)
        # Compatibility aliases remain read-only from the OMS perspective.
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
            float(oms_cfg.get("outbound_gate_drain_timeout_sec", 10.0) or 10.0),
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
            float(oms_cfg.get("emergency_flatten_cooldown_sec", 5.0) or 0.0),
        )
        self.last_emergency_flatten_ts = {}
        self.last_reconcile_request_ts = 0.0
        self.last_reconcile_failure_ts = 0.0
        self.consecutive_reconcile_api_failures = 0
        self._reconcile_thread = None
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
        self._background_task_rejection_count = 0
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

    _canonical_json = staticmethod(RpiCalibrationManager._canonical_json)
    _require_exact_mapping_keys = staticmethod(
        RpiCalibrationManager._require_exact_mapping_keys
    )
    _require_sha256 = staticmethod(RpiCalibrationManager._require_sha256)
    _finite_decimal = staticmethod(RpiCalibrationManager._finite_decimal)
    _positive_decimal = staticmethod(RpiCalibrationManager._positive_decimal)
    _decimal_text = staticmethod(RpiCalibrationManager._decimal_text)
    _usdt_to_microu = staticmethod(RpiCalibrationManager._usdt_to_microu)
    _parse_utc_exchange_ns = staticmethod(
        RpiCalibrationManager._parse_utc_exchange_ns
    )
    _verify_rpi_calibration_permit_signature = staticmethod(
        RpiCalibrationManager._verify_rpi_calibration_permit_signature
    )

    bootstrap = component_method("lifecycle_controller")
    _refresh_read_only_account_snapshot = component_method("lifecycle_controller")
    _apply_rebuild_summary = component_method("lifecycle_controller")
    _has_active_guards = component_method("lifecycle_controller")
    _outbound_gate_should_open_locked = component_method("lifecycle_controller")
    _close_outbound_gate_locked = component_method("lifecycle_controller")
    _refresh_outbound_gate_locked = component_method("lifecycle_controller")
    _acquire_outbound_order_send_permit_locked = component_method("lifecycle_controller")
    _acquire_outbound_risk_send_permit_locked = component_method("lifecycle_controller")
    _release_outbound_order_send_permit = component_method("lifecycle_controller")
    _release_outbound_risk_send_permit = component_method("lifecycle_controller")
    _submit_settlement_count_locked = component_method("lifecycle_controller")
    _wait_for_outbound_risk_sends = component_method("lifecycle_controller")
    _wait_for_outbound_order_sends = component_method("lifecycle_controller")
    close_outbound_gate = component_method("lifecycle_controller")
    begin_shutdown = component_method("lifecycle_controller")
    verify_preconnect_shutdown_no_order_path = component_method("lifecycle_controller")
    get_outbound_gate_snapshot = component_method("lifecycle_controller")
    freeze_system = component_method("lifecycle_controller")
    halt_system = component_method("lifecycle_controller")
    rearm_system = component_method("lifecycle_controller")
    stop = component_method("lifecycle_controller")


















    def _rpi_calibration_active_orders_locked(self) -> list:
        return self.rpi_calibration_runtime._rpi_calibration_active_orders_locked()

    @staticmethod
    def _exchange_ns_to_iso(value: int) -> str:
        return RpiCalibrationRuntime._exchange_ns_to_iso(value)

    def _rpi_calibration_snapshot_locked(self) -> dict:
        return self.rpi_calibration_runtime._rpi_calibration_snapshot_locked()


    @classmethod
    def _signed_usdt_to_microu(
        cls,
        value,
        *,
        rounding,
        context: str,
    ) -> int:
        return RpiCalibrationRuntime._signed_usdt_to_microu(
            value,
            rounding=rounding,
            context=context,
        )

    def _rpi_calibration_equity_truth_locked(self) -> tuple[str, int, int]:
        return self.rpi_calibration_runtime._rpi_calibration_equity_truth_locked()

    def _observe_rpi_calibration_loss_locked(
        self,
        *,
        initialize_baseline: bool,
    ) -> tuple[str, int]:
        return self.rpi_calibration_runtime._observe_rpi_calibration_loss_locked(
            initialize_baseline=initialize_baseline,
        )

    def _rpi_calibration_activation_payload_locked(
        self,
        activated_at_exchange_ns: int,
    ) -> dict:
        return self.rpi_calibration_runtime._rpi_calibration_activation_payload_locked(
            activated_at_exchange_ns
        )

    def _activate_rpi_calibration_permit_locked(
        self,
        activated_at_exchange_ns: int,
    ) -> None:
        self.rpi_calibration_runtime._activate_rpi_calibration_permit_locked(
            activated_at_exchange_ns
        )

    def _expire_rpi_calibration_permit_locked(
        self,
        reason: str,
        *,
        budget_exhausted: bool = False,
    ) -> bool:
        return self.rpi_calibration_runtime._expire_rpi_calibration_permit_locked(
            reason,
            budget_exhausted=budget_exhausted,
        )

    def expire_rpi_calibration_permit(
        self,
        reason: str = "operator_expired",
    ) -> bool:
        return self.rpi_calibration_runtime.expire_rpi_calibration_permit(
            reason
        )

    def _validate_rpi_calibration_sample_locked(
        self,
        intent: OrderIntent,
        request: OrderRequest,
    ) -> tuple[str, int, str, str]:
        return self.rpi_calibration_runtime._validate_rpi_calibration_sample_locked(
            intent,
            request,
        )

    def _reserve_rpi_calibration_sample_locked(
        self,
        intent: OrderIntent,
        request: OrderRequest,
        client_oid: str,
    ) -> tuple[str, str]:
        return self.rpi_calibration_runtime._reserve_rpi_calibration_sample_locked(
            intent,
            request,
            client_oid,
        )

    def _audit_rpi_calibration_emergency_bypass_locked(
        self,
        intent: OrderIntent,
        request: OrderRequest,
        client_oid: str,
    ) -> None:
        self.rpi_calibration_runtime._audit_rpi_calibration_emergency_bypass_locked(
            intent,
            request,
            client_oid,
        )

    def _mark_rpi_calibration_terminal_pending(
        self,
        reason: str,
        **details,
    ) -> None:
        self.rpi_calibration_runtime._mark_rpi_calibration_terminal_pending(
            reason,
            **details,
        )

    def _enforce_rpi_calibration_terminal_once(self) -> bool:
        return self.rpi_calibration_runtime._enforce_rpi_calibration_terminal_once()

    def enforce_rpi_calibration_runtime_limits(self) -> dict:
        return self.rpi_calibration_runtime.enforce_rpi_calibration_runtime_limits()

    def _schedule_rpi_calibration_runtime_enforcement(
        self,
        *,
        terminal_truth_changed: bool = False,
    ) -> bool:
        return self.rpi_calibration_runtime._schedule_rpi_calibration_runtime_enforcement(
            terminal_truth_changed=terminal_truth_changed,
        )

    def get_local_order_truth_snapshot(self) -> dict:
        """Return one locked view of active local orders and outbound sends."""
        with self.lock:
            active_orders = self._collect_local_active_orders_locked()
            with self._outbound_gate_condition:
                order_sends_inflight = self._outbound_order_sends_inflight
            return {
                "active_orders": active_orders,
                "order_sends_inflight": order_sends_inflight,
            }

    get_known_account_order_symbols = component_method("capability_manager")
    _sync_capability_mode = component_method("capability_manager")
    _mode_rank = component_method("capability_manager")
    _mode_constraint_key = component_method("capability_manager")
    _refresh_selected_mode_constraint = component_method("capability_manager")
    _capability_mode_for_state = component_method("capability_manager")
    _ensure_capability_mode_consistent = component_method("capability_manager")
    set_trading_mode = component_method("capability_manager")
    clear_trading_mode = component_method("capability_manager")
    has_trading_mode_constraint = component_method("capability_manager")
    can_query_exchange = component_method("capability_manager")
    can_cancel_orders = component_method("capability_manager")
    can_open_new_risk = component_method("capability_manager")
    record_risk_control_heartbeat = component_method("capability_manager")
    get_risk_control_heartbeat_snapshot = component_method("capability_manager")
    _venue_dead_man_switch_health_locked = component_method("capability_manager")
    get_venue_dead_man_switch_snapshot = component_method("capability_manager")
    _venue_dead_man_switch_renewal_allowed_locked = component_method("capability_manager")
    can_renew_venue_dead_man_switch = component_method("capability_manager")
    _venue_dead_man_constraint_reason = component_method("capability_manager")
    _start_venue_dead_man_safety_cancel = component_method("capability_manager")
    handle_venue_dead_man_switch_unhealthy = component_method("capability_manager")
    request_venue_dead_man_switch_renewal = component_method("capability_manager")
    renew_venue_dead_man_switch = component_method("capability_manager")
    _ensure_venue_dead_man_switch_armed = component_method("capability_manager")
    get_capability_snapshot = component_method("capability_manager")
    _get_capability_block_reason = component_method("capability_manager")
    query_account_info = component_method("capability_manager")
    sync_account_margin_health = component_method("capability_manager")
    query_positions = component_method("capability_manager")
    query_open_orders = component_method("capability_manager")
    query_order = component_method("capability_manager")
    query_user_trades = component_method("capability_manager")
    query_income_history = component_method("capability_manager")



































    _normalize_submit_command = component_method("submit_settlement")
    _get_final_outbound_send_rejection_locked = component_method("submit_settlement")
    _dispatch_gateway_order_with_final_fence = component_method("submit_settlement")
    _bind_submit_exchange_oid_locked = component_method("submit_settlement")
    _handle_submit_transport_conflict = component_method("submit_settlement")
    _commit_gateway_submission = component_method("submit_settlement")
    _notify_order_state_safely = component_method("submit_settlement")
    _finish_submit_settlement = component_method("submit_settlement")
    _publish_order_submitted_safely = component_method("submit_settlement")
    _audit_post_submit_safely = component_method("submit_settlement")
    _latch_submit_ambiguity_locked = component_method("submit_settlement")
    _close_gate_after_submit_settlement_failure = component_method("submit_settlement")
    _cleanup_pre_dispatch_submit_exception = component_method("submit_settlement")
    _settle_post_dispatch_submit_exception = component_method("submit_settlement")














    def _record_command_prepared(
        self,
        command_id: str,
        command_type: str,
        order: Order,
        request,
    ):
        return self.audit_logger.record_command_prepared(
            command_id,
            command_type,
            order,
            request,
        )

    @staticmethod
    def _command_prepared_payload(
        command_id: str,
        command_type: str,
        order: Order,
        request,
    ) -> dict:
        return OMSAuditLogger.command_prepared_payload(
            command_id,
            command_type,
            order,
            request,
        )

    def _build_submit_prepared_records(
        self,
        command_id: str,
        order: Order,
        request: OrderRequest,
        snapshot_source: str,
        **snapshot_extra,
    ) -> tuple[tuple[str, dict], tuple[str, dict]]:
        return self.audit_logger.build_submit_prepared_records(
            command_id,
            order,
            request,
            snapshot_source,
            **snapshot_extra,
        )

    def _record_submit_prepared_batch(
        self,
        records: tuple[tuple[str, dict], tuple[str, dict]],
    ) -> list[int]:
        return self.audit_logger.record_submit_prepared_batch(records)

    def _record_command_result(
        self,
        command_id: str,
        command_type: str,
        order: Order,
        outcome: CommandOutcome,
        exchange_oid: str = "",
        error_code: str = "",
        error_message: str = "",
    ):
        return self.audit_logger.record_command_result(
            command_id,
            command_type,
            order,
            outcome.value,
            exchange_oid=exchange_oid,
            error_code=error_code,
            error_message=error_message,
        )

    def record_strategy_evidence(
        self,
        kind: str,
        payload: dict,
        *,
        symbol: str = "",
    ) -> int:
        """Durably commit strategy evidence or close the live send gate."""
        try:
            committed_seq = self.audit_logger.audit(kind, payload)
            if not committed_seq:
                raise JournalError(
                    f"OMS journal did not commit strategy evidence {kind}"
                )
        except Exception as exc:
            self._fail_closed_on_journal_error(
                exc,
                f"strategy_evidence:{kind}",
                symbol,
            )
            raise
        return committed_seq

    _execution_id = component_method("account_truth")
    _record_execution = component_method("account_truth")
    _on_order_truth_check = component_method("account_truth")
    _resolve_order_truth = component_method("account_truth")
    _clear_order_truth_guard = component_method("account_truth")
    _create_recovered_order = component_method("account_truth")
    _apply_exchange_order_snapshot = component_method("account_truth")
    _advance_trade_cursor = component_method("account_truth")
    _apply_exchange_trade = component_method("account_truth")
    _backfill_trade_history = component_method("account_truth")
    _schedule_trade_tail_verification = component_method("account_truth")
    _prime_trade_history_baseline = component_method("account_truth")
    _utc_day_start_ms = component_method("account_truth")
    _external_cash_flow_income_id = component_method("account_truth")
    _apply_external_cash_flow_rows = component_method("account_truth")
    mark_external_cash_flow_truth_unavailable = component_method("account_truth")
    backfill_external_cash_flow_history = component_method("account_truth")
    poll_external_cash_flow_truth = component_method("account_truth")
    _refresh_missing_local_order_terminals = component_method("account_truth")



    def _latch_journal_failure_locked(
        self,
        exc: Exception,
        context: str,
        symbol: str = "",
    ) -> str:
        """Atomically seal every order send before an outbound lease is released."""
        reason = f"durable_journal_unavailable:{context}:{exc}"
        already_latched = "journal_failure" in self._outbound_gate_holds
        if already_latched and self._outbound_all_order_seal_reason:
            reason = self._outbound_all_order_seal_reason
        else:
            self._outbound_all_order_seal_reason = reason
        self._close_outbound_gate_locked(
            reason,
            hold="journal_failure",
        )
        self.state = LifecycleState.HALTED
        if not already_latched:
            self._lifecycle_generation += 1
        self.manual_rearm_required = True
        self.last_halt_reason = reason
        self.last_freeze_reason = ""
        self.capability_mode = OMSCapabilityMode.CANCEL_ONLY
        self.capability_reason = reason
        if symbol:
            target_symbol = symbol.upper()
            records = self._ensure_symbol_guard_records_locked(target_symbol)
            owner = self._symbol_guard_owner(reason)
            if owner not in records:
                epoch = max(
                    [
                        int(
                            self.symbol_guard_epoch_counters.get(
                                target_symbol,
                                0,
                            )
                            or 0
                        ),
                        *(
                            int(record.get("epoch", 0) or 0)
                            for record in records.values()
                        ),
                    ]
                ) + 1
                records[owner] = {"reason": reason, "epoch": epoch}
                self.symbol_guard_epoch_counters[target_symbol] = epoch
                self._refresh_symbol_guard_effective_locked(target_symbol)
        return reason

    def _latch_journal_failure(
        self,
        exc: Exception,
        context: str,
        symbol: str = "",
    ) -> str:
        with self.lock:
            return self._latch_journal_failure_locked(
                exc,
                context,
                symbol,
            )

    def _fail_closed_on_journal_error(
        self,
        exc: Exception,
        context: str,
        symbol: str = "",
    ):
        """Enter cancel-only mode without depending on the failed journal."""
        reason = self._latch_journal_failure(exc, context, symbol)
        logger.critical(f"[OMS] {reason}")

        try:
            self.event_engine.put(
                Event(EVENT_SYSTEM_HEALTH, f"HALT:{reason}")
            )
        except Exception as event_exc:
            logger.critical(
                "[OMS] Failed to publish journal-failure HALT event: "
                f"{type(event_exc).__name__}:{event_exc}"
            )
        sends_drained = self._wait_for_outbound_order_sends(
            f"journal_failure:{context}"
        )
        if not sends_drained:
            logger.critical(
                "[OMS] Journal failure order-send drain did not complete "
                f"for {context}"
            )
        for target_symbol in self._account_cancel_symbols():
            try:
                self._cancel_all_orders_unchecked(
                    target_symbol,
                    source="journal_failure",
                    audit=False,
                    bypass_message_budget=True,
                )
            except Exception as cancel_exc:
                logger.critical(
                    "[OMS] Failed to cancel orders after journal failure "
                    f"for {target_symbol}: {type(cancel_exc).__name__}:"
                    f"{cancel_exc}"
                )


















    adapt_intent_for_trading_mode = component_method("order_policy")
    _estimate_emergency_price = component_method("order_policy")
    emergency_reduce_only_flatten = component_method("order_policy")
    _get_order_block_reason = component_method("order_policy")
    _get_submission_safety_reason_locked = component_method("order_policy")

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

    _get_self_trade_prevention_rejection_locked = component_method("order_policy")
    _get_risk_control_heartbeat_rejection_locked = component_method("order_policy")
    _get_venue_dead_man_switch_rejection_locked = component_method("order_policy")
    _get_margin_health_rejection_locked = component_method("order_policy")
    _get_strategy_budget_rejection_locked = component_method("order_policy")
    get_strategy_risk_budget_snapshot = component_method("order_policy")



    def _submit_internal_order(
        self,
        intent: OrderIntent,
        request: OrderRequest,
        client_oid: str,
        snapshot_source: str,
        audit_kind: str,
        **audit_extra,
    ) -> bool:
        return self.order_submission._submit_internal_order(
            intent,
            request,
            client_oid,
            snapshot_source,
            audit_kind,
            **audit_extra,
        )


    _symbol_guard_owner = staticmethod(OMSGuardManager._symbol_guard_owner)
    _ensure_symbol_guard_records_locked = component_method("guard_manager")
    _refresh_symbol_guard_effective_locked = component_method("guard_manager")
    _install_symbol_guard_locked = component_method("guard_manager")
    _enforce_symbol_guard = component_method("guard_manager")
    freeze_symbol = component_method("guard_manager")
    clear_symbol_freeze = component_method("guard_manager")
    get_symbol_freeze_reason = component_method("guard_manager")
    get_symbol_freeze_epoch = component_method("guard_manager")
    get_symbol_freeze_owners = component_method("guard_manager")
    _venue_guard_owner = staticmethod(OMSGuardManager._venue_guard_owner)
    clear_orderbook_freeze = component_method("guard_manager")
    _ensure_venue_guard_records_locked = component_method("guard_manager")
    _refresh_venue_guard_effective_locked = component_method("guard_manager")
    freeze_venue = component_method("guard_manager")
    clear_venue_freeze = component_method("guard_manager")
    get_venue_freeze_reason = component_method("guard_manager")
    get_venue_freeze_epoch = component_method("guard_manager")
    get_venue_freeze_owners = component_method("guard_manager")
    request_venue_recovery_verification = component_method("guard_manager")
    freeze_strategy = component_method("guard_manager")
    clear_strategy_freeze = component_method("guard_manager")
    get_strategy_freeze_reason = component_method("guard_manager")
    _capture_guard_cleanup_snapshot_locked = component_method("guard_manager")
    _clear_guard_cleanup_snapshot = component_method("guard_manager")
    clear_transient_guards = component_method("guard_manager")
    _clear_recovered_guards_if_pending = component_method("guard_manager")
    is_symbol_tradeable = component_method("guard_manager")
    can_submit_for_strategy = component_method("guard_manager")
    get_order_block_reason = component_method("guard_manager")
    _cancel_orders_matching = component_method("guard_manager")

    def _prune_outbound_message_history_locked(self, now: float = None) -> float:
        observed_at = time.perf_counter() if now is None else float(now)
        self._outbound_budget.snapshot(observed_at)
        return observed_at

    def _outbound_message_counts_locked(self) -> dict:
        return dict(self._outbound_budget.snapshot()["counts"])

    def _reserve_outbound_message_locked(
        self,
        message_kind: str,
        now: float = None,
    ) -> str:
        return self._outbound_budget.reserve(message_kind, now)

    def get_outbound_message_budget_snapshot(self) -> dict:
        return self._outbound_budget.snapshot()

    def _latch_background_task_failure(
        self,
        key: str,
        reason: str,
    ) -> None:
        failure_reason = (
            f"background_task_unavailable:{str(key or 'unknown')}:"
            f"{str(reason or 'rejected')}"
        )
        with self.lock:
            self._background_task_rejection_count += 1
            self._close_outbound_gate_locked(
                failure_reason,
                hold="background_task_failure",
            )
            if self.state != LifecycleState.HALTED:
                self._lifecycle_generation += 1
            self.state = LifecycleState.HALTED
            self.manual_rearm_required = True
            self.last_halt_reason = failure_reason
            self.last_freeze_reason = ""
            self._sync_capability_mode(failure_reason)
        logger.critical(f"[OMS] {failure_reason}")
        try:
            self._audit(
                "background_task_failure",
                task_key=key,
                reason=reason,
                rejection_count=self._background_task_rejection_count,
            )
        except Exception as exc:
            logger.critical(
                "[OMS] Could not persist background task failure: "
                f"{type(exc).__name__}:{exc}"
            )

    def _on_background_task_error(
        self,
        key: str,
        name: str,
        exc: BaseException,
    ) -> None:
        self._latch_background_task_failure(
            key,
            f"{name}:{type(exc).__name__}:{exc}",
        )

    def _submit_background_task(
        self,
        key: str,
        callback,
        *args,
        name: str = "",
        safety: bool = False,
        delay_sec: float = 0.0,
        resubmit_after_current: bool = False,
        fail_closed: bool = True,
        **kwargs,
    ):
        lane = (
            OMSBackgroundTaskExecutor.SAFETY_LANE
            if safety
            else OMSBackgroundTaskExecutor.DEFAULT_LANE
        )
        submission = self._background_tasks.submit(
            key,
            callback,
            *args,
            name=name,
            lane=lane,
            delay_sec=delay_sec,
            resubmit_after_current=resubmit_after_current,
            **kwargs,
        )
        if submission.accepted:
            return submission.handle
        if fail_closed:
            self._latch_background_task_failure(key, submission.reason)
        else:
            logger.error(
                f"[OMS] Background task rejected key={key}: "
                f"{submission.reason}"
            )
        return None

    def get_background_task_snapshot(self) -> dict:
        snapshot = self._background_tasks.snapshot()
        snapshot["rejection_count"] = self._background_task_rejection_count
        return snapshot

    def _schedule_cancel_order_retry(self, client_oid: str) -> bool:
        with self.lock:
            if self._stopped or client_oid in self._deferred_cancel_oids:
                return False
            self._deferred_cancel_oids.add(client_oid)

        def retry():
            with self.lock:
                self._deferred_cancel_oids.discard(client_oid)
                stopped = self._stopped
            if not stopped:
                self.cancel_order(client_oid)

        handle = self._submit_background_task(
            f"cancel-retry:{client_oid}",
            retry,
            name=f"CancelRetry-{client_oid}",
            safety=True,
            delay_sec=self.outbound_message_window_sec + 0.01,
        )
        if handle is None:
            with self.lock:
                self._deferred_cancel_oids.discard(client_oid)
            return False
        return True

    def _schedule_cancel_all_retry(self, symbol: str, source: str) -> bool:
        symbol = str(symbol or "").upper()
        with self.lock:
            if self._stopped or symbol in self._deferred_cancel_all_symbols:
                return False
            self._deferred_cancel_all_symbols.add(symbol)

        def retry():
            with self.lock:
                self._deferred_cancel_all_symbols.discard(symbol)
                stopped = self._stopped
            if stopped:
                return
            retry_source = (
                source
                if source.startswith("deferred:")
                else f"deferred:{source}"
            )
            self._cancel_all_orders_unchecked(
                symbol,
                source=retry_source,
            )

        handle = self._submit_background_task(
            f"cancel-all-retry:{symbol}",
            retry,
            name=f"CancelAllRetry-{symbol}",
            safety=True,
            delay_sec=self.outbound_message_window_sec + 0.01,
        )
        if handle is None:
            with self.lock:
                self._deferred_cancel_all_symbols.discard(symbol)
            return False
        return True














    _schedule_pending_reconcile_requests = component_method("reconciler")
    _queue_reconcile_request_locked = component_method("reconciler")
    _drain_pending_reconcile_requests = component_method("reconciler")
    trigger_reconcile = component_method("reconciler")
    _schedule_reconcile_retry = component_method("reconciler")
    _execute_reconcile = component_method("reconciler")
    _complete_venue_recovery_verification = component_method("reconciler")
    _exchange_snapshot_signature = component_method("reconciler")
    _capture_stable_exchange_snapshot = component_method("reconciler")
    _perform_full_reset = component_method("reconciler")

    def submit_order(self, intent: OrderIntent) -> OrderSubmitResult:
        return self.order_submission.submit_order(intent)

    def _reject_intent_locally(self, intent: OrderIntent, client_oid: str, reason: str, **extra):
        order = Order(client_oid, intent)
        order.mark_rejected_locally(reason)
        with self.lock:
            self._record_order_snapshot(order, "intent_rejected", **extra)
            self._emit_order_update(order)
        self._write_tombstone(order)
        audit_payload = {
            "reason": reason,
            "intent": self._serialize_intent(intent),
            "client_oid": client_oid,
        }
        audit_payload.update(extra)
        self._audit("intent_rejected", **audit_payload)
        return OrderSubmitResult(
            accepted=False,
            client_oid=client_oid,
            reason=reason,
            state=self.state.value,
        )

    cancel_order = component_method("cancellation_manager")
    _cancel_all_orders_unchecked = component_method("cancellation_manager")
    _account_cancel_symbols = component_method("cancellation_manager")
    cancel_all_account_orders_verified = component_method("cancellation_manager")
    cancel_all_orders = component_method("cancellation_manager")






    on_exchange_update = component_method("exchange_event_processor")
    on_exchange_account_update = component_method("exchange_event_processor")
    _append_and_process = component_method("exchange_event_processor")
    _quarantine_execution_gap_locked = component_method("exchange_event_processor")
    _apply_event = component_method("exchange_event_processor")
    _apply_recovered_execution = component_method("exchange_event_processor")
    _apply_recovered_command_result = component_method("exchange_event_processor")

    _journal_int = staticmethod(RpiCalibrationReplay._journal_int)

    def _journal_decimal(self, payload: dict, field: str) -> Decimal:
        return self.rpi_calibration_replay._journal_decimal(payload, field)

    _new_rpi_calibration_replay_state = staticmethod(
        RpiCalibrationReplay._new_rpi_calibration_replay_state
    )

    def _verify_replayed_rpi_calibration_permit(
        self,
        signed_permit,
        expected_sha256: str,
        record_index: int,
    ) -> dict:
        return self.rpi_calibration_replay._verify_replayed_rpi_calibration_permit(
            signed_permit,
            expected_sha256,
            record_index,
        )

    def _replay_rpi_calibration_activation(
        self,
        payload: dict,
        state: dict,
        record_index: int,
    ) -> None:
        self.rpi_calibration_replay._replay_rpi_calibration_activation(
            payload,
            state,
            record_index,
        )

    def _replay_rpi_calibration_reservation(
        self,
        payload: dict,
        state: dict,
        record_index: int,
    ) -> None:
        self.rpi_calibration_replay._replay_rpi_calibration_reservation(
            payload,
            state,
            record_index,
        )

    def _replay_rpi_calibration_expiry(
        self,
        payload: dict,
        state: dict,
        record_index: int,
    ) -> None:
        self.rpi_calibration_replay._replay_rpi_calibration_expiry(
            payload,
            state,
            record_index,
        )

    def _replay_rpi_calibration_bypass(
        self,
        payload: dict,
        state: dict,
        record_index: int,
    ) -> None:
        self.rpi_calibration_replay._replay_rpi_calibration_bypass(
            payload,
            state,
            record_index,
        )

    def _replay_rpi_calibration_record(
        self,
        kind: str,
        payload: dict,
        state: dict,
        record_index: int,
    ) -> bool:
        return self.rpi_calibration_replay._replay_rpi_calibration_record(
            kind,
            payload,
            state,
            record_index,
        )

    def _finalize_rpi_calibration_replay(
        self,
        state: dict,
        *,
        dirty_shutdown: bool = False,
    ) -> dict:
        return self.rpi_calibration_replay._finalize_rpi_calibration_replay(
            state,
            dirty_shutdown=dirty_shutdown,
        )

    def rebuild_from_log(self):
        return self.journal_rebuilder.rebuild_from_log()

    def _normalize_remote_open_orders(self, remote_orders):
        if not isinstance(remote_orders, (list, tuple)):
            raise ValueError("remote open-orders snapshot must be a list")
        normalized = []
        for order in remote_orders:
            if not isinstance(order, dict):
                raise ValueError("remote open-orders entry must be an object")
            symbol = str(order.get("symbol", "") or "").upper().strip()
            if not symbol:
                raise ValueError("remote open-orders entry is missing symbol")
            identifiers = tuple(
                sorted(
                    oid
                    for oid in [
                        str(order.get("orderId")) if order.get("orderId") is not None else "",
                        order.get("clientOrderId") or "",
                    ]
                    if oid
                )
            )
            normalized.append(
                {
                    "symbol": symbol,
                    "identifiers": identifiers,
                    "side": str(order.get("side", "") or "").upper(),
                }
            )
        with self.lock:
            self._known_account_order_symbols.update(
                item["symbol"] for item in normalized
            )
        normalized.sort(key=lambda item: (item["symbol"], item["identifiers"], item["side"]))
        return normalized

    def _collect_local_active_orders_locked(self):
        normalized = []
        for order in self.orders.values():
            if not order.is_active():
                continue
            identifiers = tuple(
                sorted(
                    oid
                    for oid in [order.client_oid, order.exchange_oid]
                    if oid
                )
            )
            normalized.append(
                {
                    "symbol": str(order.intent.symbol or "").upper(),
                    "identifiers": identifiers,
                    "side": order.intent.side.value,
                }
            )
        normalized.sort(key=lambda item: (item["symbol"], item["identifiers"], item["side"]))
        return normalized

    def _collect_exchange_position_drift_locked(self, exchange_positions, tracked_symbols=None):
        drift = {}
        symbols = set(tracked_symbols or [])
        symbols.update(exchange_positions.keys())
        symbols.update(
            symbol
            for symbol, volume in self.exposure.net_positions.items()
            if abs(volume) > 1e-6 and (not symbols or symbol in symbols)
        )

        for symbol in symbols:
            local_pos = self.exposure.net_positions.get(symbol, 0.0)
            payload = exchange_positions.get(symbol, {})
            exchange_pos = float(payload.get("volume", 0.0))
            if abs(local_pos - exchange_pos) > 1e-6:
                drift[symbol] = {
                    "local": local_pos,
                    "exchange": exchange_pos,
                    "entry_price": float(payload.get("entry_price", 0.0)),
                }
        return drift

    def _has_active_orders_locked(self, symbols=None):
        tracked_symbols = set(symbols or [])
        for order in self.orders.values():
            if not order.is_active():
                continue
            if tracked_symbols and order.intent.symbol not in tracked_symbols:
                continue
            return True
        return False

    def _extract_quote_asset(self, symbol: str) -> str:
        symbol = str(symbol or "").upper()
        for suffix in ("USDT", "USDC", "BUSD", "FDUSD", "BTC", "ETH", "BNB"):
            if symbol.endswith(suffix):
                return suffix
        return ""

    def _tracked_quote_assets(self, symbols) -> set[str]:
        assets = {
            self._extract_quote_asset(symbol)
            for symbol in symbols or []
        }
        assets.discard("")
        return assets or {"USDT", "USDC", "BUSD", "FDUSD"}

    def _get_fill_commission(self, update: ExchangeOrderUpdate, order: Order, fill_notional: float) -> float:
        if update.commission is None:
            return fill_notional * self._get_fee_rate(order, is_maker=update.is_maker)

        asset = (update.commission_asset or self._extract_quote_asset(order.intent.symbol)).upper()
        if asset in {"", "USDT", "USDC", "BUSD", "FDUSD"}:
            return update.commission

        logger.warning(
            f"[OMS] Unsupported commission asset {asset}; falling back to configured fee model"
        )
        return fill_notional * self._get_fee_rate(order, is_maker=update.is_maker)

    def _get_fee_rate(self, order: Order, is_maker: bool = None) -> float:
        fee_config = self.config.get("backtest", {})
        if order.intent.is_rpi:
            return resolve_passive_fee_rate(
                maker_rate=fee_config.get("maker_fee", 0.0),
                symbol=order.intent.symbol,
                is_rpi=True,
                rpi_commission_rates=fee_config.get(
                    "rpi_commission_rates",
                    {},
                ),
                default_rpi_commission_rate=fee_config.get(
                    "rpi_commission_rate",
                    0.0,
                ),
            )
        maker_fee = float(fee_config.get("maker_fee", 0.0))
        if is_maker is True:
            return maker_fee
        if is_maker is False:
            return fee_config.get("taker_fee", 0.0005)
        if order.intent.is_post_only:
            return maker_fee
        return fee_config.get("taker_fee", 0.0005)

    def _serialize_intent(self, intent: OrderIntent) -> dict:
        return {
            "strategy_id": intent.strategy_id,
            "symbol": intent.symbol,
            "side": intent.side.value,
            "price": intent.price,
            "volume": intent.volume,
            "order_type": intent.order_type,
            "time_in_force": intent.time_in_force,
            "is_post_only": intent.is_post_only,
            "reduce_only": intent.reduce_only,
            "policy": intent.policy.value,
            "tag": intent.tag,
            "calibration_permit_id": intent.calibration_permit_id,
            "calibration_depth_bps": intent.calibration_depth_bps,
            "calibration_reference_mid": intent.calibration_reference_mid,
        }

    def _audit(self, kind: str, **payload):
        payload.setdefault("state", self.state.value)
        payload.setdefault("capability_mode", self.capability_mode.value)
        payload.setdefault("capability_reason", self.capability_reason)
        payload.setdefault("mode_override", self.mode_override.value if self.mode_override else "")
        payload.setdefault("mode_override_reason", self.mode_override_reason)
        self.audit_logger.audit(kind, payload)

    def record_rpi_commission_truth(
        self,
        rates_by_symbol: dict,
        *,
        accepted: bool,
        reason: str,
        source: str,
    ) -> bool:
        """Persist one independent runtime commission-truth observation."""
        if not isinstance(rates_by_symbol, dict):
            raise TypeError("RPI commission truth must be a mapping")
        canonical_rates = json.loads(
            json.dumps(
                rates_by_symbol,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        with self.lock:
            self._audit(
                "rpi_commission_truth",
                rates_by_symbol=canonical_rates,
                accepted=bool(accepted),
                reason=str(reason or ""),
                source=str(source or ""),
            )
        return True

    def _record_order_snapshot(self, order: Order, source: str, **extra):
        self.audit_logger.record_order_snapshot(
            order,
            source,
            **extra,
        )

    def _emit_order_update(self, order: Order):
        self.event_engine.put(Event(EVENT_ORDER_UPDATE, order.to_snapshot()))

    def _emit_position_update(self, symbol: str):
        self.event_engine.put(
            Event(EVENT_POSITION_UPDATE, self.exposure.get_position_data(symbol))
        )

    def _remember_terminated_oid(self, oid: str):
        if not oid or oid in self.terminated_oids:
            return
        if len(self.terminated_oid_queue) >= self.TOMBSTONE_MAX:
            stale = self.terminated_oid_queue.popleft()
            self.terminated_oids.discard(stale)
        self.terminated_oid_queue.append(oid)
        self.terminated_oids.add(oid)

    def _write_tombstone(self, order: Order):
        self._remember_terminated_oid(order.client_oid)
        self._remember_terminated_oid(order.exchange_oid)
