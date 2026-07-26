import hashlib
import json
import math
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal

from data.cache import data_cache
from data.ref_data import ref_data_manager
from infrastructure.commission_truth import resolve_passive_fee_rate
from infrastructure.logger import logger
from infrastructure.single_writer_fence import SingleWriterFence
from infrastructure.time_service import time_service

from event.type import (
    CancelRequest,
    CommandOutcome,
    ExecutionPolicy,
    Event,
    ExchangeOrderUpdate,
    GatewayCommandResult,
    LifecycleState,
    OMSCapabilityMode,
    OrderIntent,
    OrderRequest,
    OrderStatus,
    OrderSubmitResult,
    OrderSubmitted,
    Side,
    EVENT_ORDER_SUBMITTED,
    EVENT_EXCHANGE_ORDER_UPDATE,
    EVENT_ORDER_UPDATE,
    EVENT_POSITION_UPDATE,
    EVENT_SYSTEM_HEALTH,
    TIF_GTC,
    TIF_GTX,
    TIF_IOC,
    TIF_RPI,
)

from .account_manager import AccountManager
from .audit_logger import OMSAuditLogger
from .background_tasks import OMSBackgroundTaskExecutor
from .component import component_method
from .exchange_event_processor import OMSExchangeEventProcessor
from .exposure import ExposureManager
from .guard_manager import OMSGuardManager
from .journal import JournalError, OMSJournal
from .journal_rebuilder import OMSJournalRebuilder
from .order import Order
from .order_manager import OrderManager
from .order_submission import OMSOrderSubmission
from .outbound_budget import OutboundMessageBudget
from .reconciler import OMSReconciler
from .rpi_calibration_manager import RpiCalibrationManager
from .rpi_calibration_replay import RpiCalibrationReplay
from .rpi_calibration_runtime import RpiCalibrationRuntime
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
        self.exchange_event_processor = OMSExchangeEventProcessor(self)
        self.journal_rebuilder = OMSJournalRebuilder(self)
        self.reconciler = OMSReconciler(self)
        self.order_submission = OMSOrderSubmission(self)
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

    def bootstrap(self):
        logger.info("OMS: Bootstrapping state...")
        self._audit("bootstrap_requested", recovered=self.rebuild_summary)
        if self.manual_rearm_required or self.state == LifecycleState.HALTED:
            if not self._ensure_venue_dead_man_switch_armed(
                "bootstrap_read_only"
            ):
                return False
            self._sync_capability_mode("manual_rearm_required")
            self._refresh_read_only_account_snapshot()
            logger.error("[OMS] Bootstrap blocked: manual rearm required after recovered HALT")
            self._audit(
                "bootstrap_blocked",
                reason="manual_rearm_required",
                recovered=self.rebuild_summary,
            )
            return False

        if self.state == LifecycleState.FROZEN or self._has_active_guards():
            if not self._ensure_venue_dead_man_switch_armed(
                "bootstrap_guarded"
            ):
                return False
            logger.warning("[OMS] Bootstrapping into guarded reconcile mode")
            self.state = LifecycleState.FROZEN
            self._sync_capability_mode("bootstrap_guarded")
            self.recovered_guard_cleanup_pending = True
            if not self.last_freeze_reason:
                self.last_freeze_reason = "Recovered guarded state"
            self._audit(
                "bootstrap_guarded",
                reason=self.last_freeze_reason,
                recovered=self.rebuild_summary,
            )
            self.trigger_reconcile("Recovered guarded state")
            return True

        self._perform_full_reset()
        return self.state == LifecycleState.LIVE

    def _refresh_read_only_account_snapshot(self):
        if not self.can_query_exchange():
            return False

        try:
            account = self.gateway.get_account_info()
        except Exception as exc:
            logger.warning(f"[OMS] Read-only account sync failed: {exc}")
            return False

        if not isinstance(account, dict) or not account:
            return False

        balances = {}
        for entry in account.get("assets", []) or []:
            asset = str(entry.get("asset", "") or "").upper()
            if not asset:
                continue
            available_balance = entry.get("availableBalance")
            balances[asset] = {
                "wallet_balance": float(entry.get("walletBalance", 0.0) or 0.0),
                "available_balance": (
                    float(available_balance or 0.0)
                    if available_balance is not None
                    else None
                ),
            }

        available_balance = account.get("availableBalance")
        self.account.force_sync(
            float(account.get("totalWalletBalance", self.account.balance) or self.account.balance),
            float(account.get("totalInitialMargin", self.account.used_margin) or 0.0),
            float(available_balance) if available_balance is not None else None,
            balances=balances or None,
            maintenance_margin=account.get("totalMaintMargin"),
            margin_balance=account.get("totalMarginBalance"),
            margin_snapshot_time=time.time(),
            margin_snapshot_monotonic=time.perf_counter(),
        )
        self._audit(
            "read_only_account_sync",
            balance=self.account.balance,
            available=self.account.available,
            budget_available=self.account.budget_available,
            assets=sorted(balances.keys()),
        )
        return True

    def _apply_rebuild_summary(self):
        summary = self.rebuild_summary or {}
        calibration_summary = summary.get("rpi_calibration", {}) or {}
        self._rpi_calibration_permit_activated = bool(
            calibration_summary.get("permit_activated", False)
        )
        self._rpi_calibration_expired = bool(
            calibration_summary.get("expired", False)
        )
        self._rpi_calibration_expiry_reason = str(
            calibration_summary.get("expiry_reason", "") or ""
        )
        self._rpi_calibration_budget_exhausted = bool(
            calibration_summary.get("budget_exhausted", False)
        )
        self._rpi_calibration_restart_rearm_blocked = bool(
            calibration_summary.get("restart_rearm_blocked", False)
        )
        self._rpi_calibration_reserved_order_count = max(
            0,
            int(calibration_summary.get("reserved_order_count", 0) or 0),
        )
        self._rpi_calibration_cumulative_notional_microu = max(
            0,
            int(
                calibration_summary.get(
                    "cumulative_submitted_notional_microu",
                    0,
                )
                or 0
            ),
        )
        self._rpi_calibration_last_reserved_exchange_ns = max(
            0,
            int(
                calibration_summary.get(
                    "last_reserved_exchange_ns",
                    0,
                )
                or 0
            ),
        )
        self._rpi_calibration_permit_start_order_count = max(
            0,
            int(
                calibration_summary.get(
                    "permit_start_order_count",
                    self._rpi_calibration_reserved_order_count,
                )
                or 0
            ),
        )
        self._rpi_calibration_permit_start_notional_microu = max(
            0,
            int(
                calibration_summary.get(
                    "permit_start_notional_microu",
                    self._rpi_calibration_cumulative_notional_microu,
                )
                or 0
            ),
        )
        self._rpi_calibration_start_equity_microu = max(
            0,
            int(
                calibration_summary.get(
                    "deployment_start_equity_microu",
                    0,
                )
                or 0
            ),
        )
        self._rpi_calibration_start_external_cash_flow_microu = int(
            calibration_summary.get(
                "deployment_start_external_cash_flow_microu",
                0,
            )
            or 0
        )
        self._rpi_calibration_peak_observed_loss_microu = max(
            0,
            int(
                calibration_summary.get(
                    "peak_observed_loss_microu",
                    0,
                )
                or 0
            ),
        )
        replayed_loss_cap_microu = int(
            calibration_summary.get(
                "effective_loss_cap_microu",
                0,
            )
            or 0
        )
        configured_loss_cap_microu = int(
            self._rpi_calibration.get(
                "max_calibration_loss_microu",
                0,
            )
            or 0
        )
        self._rpi_calibration_effective_loss_cap_microu = (
            min(
                replayed_loss_cap_microu,
                configured_loss_cap_microu,
            )
            if replayed_loss_cap_microu > 0
            and configured_loss_cap_microu > 0
            else max(
                replayed_loss_cap_microu,
                configured_loss_cap_microu,
            )
        )
        self._rpi_calibration_reservation_ids = set(
            str(item)
            for item in calibration_summary.get("reservation_ids", [])
            if str(item or "")
        )
        self._rpi_calibration_reservation_exchange_ns = {
            str(client_oid): max(0, int(reserved_at_ns or 0))
            for client_oid, reserved_at_ns in (
                calibration_summary.get("reservation_exchange_ns", {}) or {}
            ).items()
            if str(client_oid or "") and int(reserved_at_ns or 0) > 0
        }
        if self._rpi_calibration_expired:
            self._outbound_gate_holds.add("rpi_calibration_expired")
        if self._rpi_calibration_restart_rearm_blocked:
            self._outbound_gate_holds.add(
                "rpi_calibration_restart_rearm_blocked"
            )
        self.trade_cursors = {
            str(symbol).upper(): int(trade_id)
            for symbol, trade_id in summary.get("trade_cursors", {}).items()
        }
        self.trade_scan_end_ms = {
            str(symbol).upper(): int(end_time_ms)
            for symbol, end_time_ms in summary.get("trade_scan_end_ms", {}).items()
        }
        self.external_cash_flow_ids = set(
            str(income_id)
            for income_id in summary.get("external_cash_flow_ids", [])
            if str(income_id or "")
        )
        self.external_cash_flow_scan_end_ms = int(
            summary.get("external_cash_flow_scan_end_ms", 0) or 0
        )
        self.account.external_cash_flow_total = float(
            summary.get("external_cash_flow_total", 0.0) or 0.0
        )
        self.account.cash_flow_snapshot_synced = False
        self.account.cash_flow_snapshot_time = 0.0
        self.account.cash_flow_snapshot_monotonic = 0.0
        legacy_symbol_guards = {
            str(symbol or "").upper(): str(reason or "")
            for symbol, reason in (summary.get("symbol_guards", {}) or {}).items()
            if str(symbol or "").strip() and str(reason or "")
        }
        self.symbol_guards = {}
        self.symbol_guard_records = {}
        self.symbol_guard_epochs = {}
        self.symbol_guard_epoch_counters = {}
        raw_guard_records = summary.get("symbol_guard_records", {}) or {}
        for raw_symbol, raw_records in raw_guard_records.items():
            symbol = str(raw_symbol or "").upper()
            if not symbol or not isinstance(raw_records, dict):
                continue
            records = {}
            for raw_owner, raw_record in raw_records.items():
                if not isinstance(raw_record, dict):
                    continue
                guard_reason = str(raw_record.get("reason", "") or "")
                if not guard_reason:
                    continue
                guard_epoch = max(1, int(raw_record.get("epoch", 1) or 1))
                records[str(raw_owner or self._symbol_guard_owner(guard_reason))] = {
                    "reason": guard_reason,
                    "epoch": guard_epoch,
                }
            if records:
                self.symbol_guard_records[symbol] = records
                self.symbol_guard_epoch_counters[symbol] = max(
                    int(record["epoch"]) for record in records.values()
                )
                self._refresh_symbol_guard_effective_locked(symbol)
        for symbol, guard_reason in legacy_symbol_guards.items():
            if symbol in self.symbol_guard_records:
                continue
            self.symbol_guard_records[symbol] = {
                self._symbol_guard_owner(guard_reason): {
                    "reason": guard_reason,
                    "epoch": 1,
                }
            }
            self.symbol_guard_epoch_counters[symbol] = 1
            self._refresh_symbol_guard_effective_locked(symbol)
        legacy_venue_guards = {
            str(venue or "").upper(): str(reason or "")
            for venue, reason in (summary.get("venue_guards", {}) or {}).items()
            if str(venue or "").strip() and str(reason or "")
        }
        self.venue_guards = {}
        self.venue_guard_records = {}
        self.venue_guard_epochs = {}
        self.venue_guard_epoch_counters = {}
        raw_venue_guard_records = summary.get("venue_guard_records", {}) or {}
        for raw_venue, raw_records in raw_venue_guard_records.items():
            venue = str(raw_venue or "").upper()
            if not venue or not isinstance(raw_records, dict):
                continue
            records = {}
            for raw_owner, raw_record in raw_records.items():
                if not isinstance(raw_record, dict):
                    continue
                guard_reason = str(raw_record.get("reason", "") or "")
                if not guard_reason:
                    continue
                guard_epoch = max(1, int(raw_record.get("epoch", 1) or 1))
                records[str(raw_owner or self._venue_guard_owner(guard_reason))] = {
                    "reason": guard_reason,
                    "epoch": guard_epoch,
                }
            if records:
                self.venue_guard_records[venue] = records
                self.venue_guard_epoch_counters[venue] = max(
                    int(record["epoch"]) for record in records.values()
                )
                self._refresh_venue_guard_effective_locked(venue)
        for venue, guard_reason in legacy_venue_guards.items():
            if venue in self.venue_guard_records:
                continue
            self.venue_guard_records[venue] = {
                self._venue_guard_owner(guard_reason): {
                    "reason": guard_reason,
                    "epoch": 1,
                }
            }
            self.venue_guard_epoch_counters[venue] = 1
            self._refresh_venue_guard_effective_locked(venue)
        self.strategy_guards = dict(summary.get("strategy_guards", {}))
        self.strategy_symbol_guards = {
            tuple(key.split("|", 1)): value
            for key, value in summary.get("strategy_symbol_guards", {}).items()
            if "|" in key
        }
        self._recovered_guard_cleanup_snapshot = (
            self._capture_guard_cleanup_snapshot_locked()
        )
        self.mode_constraints = {}
        self.mode_constraint_generations = {}
        self.mode_constraint_generation = max(
            0,
            int(summary.get("mode_constraint_generation", 0) or 0),
        )
        for constraint_key, payload in (summary.get("mode_constraints", {}) or {}).items():
            mode_value = str((payload or {}).get("mode", "") or "")
            reason = str((payload or {}).get("reason", "") or "")
            if not mode_value or not reason:
                continue
            try:
                mode = OMSCapabilityMode(mode_value)
            except ValueError:
                continue
            constraint_key = str(constraint_key)
            generation = max(
                1,
                int(
                    (payload or {}).get(
                        "generation",
                        self.mode_constraint_generation + 1,
                    )
                    or 1
                ),
            )
            self.mode_constraints[constraint_key] = (mode, reason)
            self.mode_constraint_generations[constraint_key] = generation
            self.mode_constraint_generation = max(
                self.mode_constraint_generation,
                generation,
            )

        override_mode = str(summary.get("mode_override", "") or "")
        override_reason = str(summary.get("mode_override_reason", "") or "")
        if not self.mode_constraints and override_mode and override_reason:
            try:
                legacy_mode = OMSCapabilityMode(override_mode)
            except ValueError:
                legacy_mode = None
            if legacy_mode is not None:
                constraint_key = self._mode_constraint_key(override_reason)
                self.mode_constraint_generation += 1
                self.mode_constraints[constraint_key] = (
                    legacy_mode,
                    override_reason,
                )
                self.mode_constraint_generations[constraint_key] = (
                    self.mode_constraint_generation
                )
        self._refresh_selected_mode_constraint()

        self.last_freeze_reason = str(summary.get("last_freeze_reason", "") or "")
        self.last_halt_reason = str(summary.get("last_halt_reason", "") or "")
        self.manual_rearm_required = bool(summary.get("manual_rearm_required", False))
        unsafe_trade_symbols = sorted(
            {
                str(symbol or "").upper()
                for symbol in (
                    list(summary.get("untrusted_trade_cursor_symbols", []) or [])
                    + list(summary.get("unverified_execution_symbols", []) or [])
                )
                if str(symbol or "").strip()
            }
        )
        if unsafe_trade_symbols:
            for symbol in unsafe_trade_symbols:
                self.trade_cursors.pop(symbol, None)
                self.trade_scan_end_ms.pop(symbol, None)
            self.manual_rearm_required = True
            self.last_halt_reason = (
                "Legacy execution truth requires operator rearm: "
                + ",".join(unsafe_trade_symbols)
            )

        last_lifecycle = summary.get("last_lifecycle")
        dirty_shutdown = bool(summary.get("dirty_shutdown", False))
        recovered_active_orders = int(summary.get("recovered_active_orders", 0) or 0)
        pending_commands = int(summary.get("pending_commands", 0) or 0)
        if self.manual_rearm_required or last_lifecycle == LifecycleState.HALTED.value:
            self.state = LifecycleState.HALTED
            self.manual_rearm_required = True
            if not self.last_halt_reason:
                self.last_halt_reason = "Recovered halted state"
            self._sync_capability_mode("recovered_halted_state")
            return

        if recovered_active_orders or pending_commands:
            self.state = LifecycleState.FROZEN
            self.recovered_guard_cleanup_pending = True
            self.last_freeze_reason = (
                "Recovered orders require exchange truth: "
                f"active={recovered_active_orders}, pending_commands={pending_commands}"
            )
            self._sync_capability_mode("recovered_inflight_commands")
            return

        if dirty_shutdown:
            self.state = LifecycleState.FROZEN
            self.recovered_guard_cleanup_pending = self._has_active_guards()
            if not self.last_freeze_reason:
                self.last_freeze_reason = "Recovered unclean shutdown"
            self._sync_capability_mode("recovered_unclean_shutdown")
            return

        if self._has_active_guards() or last_lifecycle in {
            LifecycleState.FROZEN.value,
            LifecycleState.RECONCILING.value,
        }:
            self.state = LifecycleState.FROZEN
            self.recovered_guard_cleanup_pending = True
            if not self.last_freeze_reason:
                self.last_freeze_reason = "Recovered guarded state"
            self._sync_capability_mode("recovered_guarded_state")
            return

        self.state = LifecycleState.BOOTSTRAP
        self._sync_capability_mode("bootstrap")

    def _has_active_guards(self):
        return bool(
            self.symbol_guards
            or self.venue_guards
            or self.strategy_guards
            or self.strategy_symbol_guards
        )

    def _outbound_gate_should_open_locked(self) -> bool:
        return bool(
            not getattr(self, "_stopped", False)
            and not self._shutdown_requested
            and self.state == LifecycleState.LIVE
            and not self.venue_guards
            and not self._outbound_gate_holds
            and self.capability_mode
            in {
                OMSCapabilityMode.LIVE,
                OMSCapabilityMode.DEGRADED,
                OMSCapabilityMode.PASSIVE_ONLY,
            }
        )

    def _close_outbound_gate_locked(self, reason: str, hold: str = "") -> None:
        if hold:
            self._outbound_gate_holds.add(str(hold))
        with self._outbound_gate_condition:
            if self._outbound_gate_open:
                self._outbound_gate_epoch += 1
            self._outbound_gate_open = False
            self._outbound_gate_reason = str(reason or "risk_send_gate_closed")
            self._outbound_gate_condition.notify_all()

    def _refresh_outbound_gate_locked(self, reason: str = "") -> None:
        should_open = self._outbound_gate_should_open_locked()
        with self._outbound_gate_condition:
            if should_open != self._outbound_gate_open:
                self._outbound_gate_epoch += 1
            self._outbound_gate_open = should_open
            if should_open:
                self._outbound_gate_reason = ""
            elif reason:
                self._outbound_gate_reason = str(reason)
            self._outbound_gate_condition.notify_all()

    def _acquire_outbound_order_send_permit_locked(
        self,
        *,
        risk_increasing: bool,
        symbol: str = "",
        allow_shutdown_emergency: bool = False,
    ) -> tuple[int | None, str]:
        symbol = str(symbol or "").upper()
        with self._outbound_gate_condition:
            if getattr(self, "_stopped", False):
                reason = self._shutdown_reason or "oms_stopping"
                return None, f"shutdown_requested:{reason}"
            if self._outbound_all_order_seal_reason:
                return (
                    None,
                    "outbound_order_gate_closed:"
                    f"{self._outbound_all_order_seal_reason}",
                )
            if self._shutdown_requested and not allow_shutdown_emergency:
                reason = self._shutdown_reason or "oms_stopping"
                return None, f"shutdown_requested:{reason}"
            if risk_increasing and not self._outbound_gate_open:
                reason = self._outbound_gate_reason or "closed"
                return None, f"outbound_gate_closed:{reason}"
            if risk_increasing and symbol in self.symbol_guards:
                return (
                    None,
                    f"symbol_guarded:{symbol}:{self.symbol_guards[symbol]}",
                )
            self._outbound_order_sends_inflight += 1
            if risk_increasing:
                self._outbound_risk_sends_inflight += 1
                if symbol:
                    self._outbound_risk_sends_inflight_by_symbol[symbol] = (
                        self._outbound_risk_sends_inflight_by_symbol.get(
                            symbol,
                            0,
                        )
                        + 1
                    )
            return self._outbound_gate_epoch, ""

    def _acquire_outbound_risk_send_permit_locked(
        self,
        symbol: str = "",
    ) -> tuple[int | None, str]:
        return self._acquire_outbound_order_send_permit_locked(
            risk_increasing=True,
            symbol=symbol,
        )

    def _release_outbound_order_send_permit(
        self,
        *,
        risk_increasing: bool,
        symbol: str = "",
    ) -> None:
        symbol = str(symbol or "").upper()
        with self._outbound_gate_condition:
            if self._outbound_order_sends_inflight <= 0:
                logger.critical("[OMS] Outbound order-send permit underflow")
                return
            self._outbound_order_sends_inflight -= 1
            if risk_increasing:
                if self._outbound_risk_sends_inflight <= 0:
                    logger.critical("[OMS] Outbound risk-send permit underflow")
                else:
                    self._outbound_risk_sends_inflight -= 1
                if symbol:
                    symbol_inflight = (
                        self._outbound_risk_sends_inflight_by_symbol.get(
                            symbol,
                            0,
                        )
                    )
                    if symbol_inflight <= 0:
                        logger.critical(
                            "[OMS] Outbound symbol risk-send permit underflow "
                            f"for {symbol}"
                        )
                    elif symbol_inflight == 1:
                        self._outbound_risk_sends_inflight_by_symbol.pop(
                            symbol,
                            None,
                        )
                    else:
                        self._outbound_risk_sends_inflight_by_symbol[symbol] = (
                            symbol_inflight - 1
                        )
            self._outbound_gate_condition.notify_all()

    def _release_outbound_risk_send_permit(self, symbol: str = "") -> None:
        self._release_outbound_order_send_permit(
            risk_increasing=True,
            symbol=symbol,
        )

    def _submit_settlement_count_locked(
        self,
        *,
        risk_increasing_only: bool = False,
        symbol: str = "",
    ) -> int:
        symbol = str(symbol or "").upper()
        count = 0
        for client_oid in self._submit_settlement_inflight_oids:
            order = self.orders.get(client_oid)
            if order is None:
                if not risk_increasing_only and not symbol:
                    count += 1
                continue
            if risk_increasing_only and order.intent.reduce_only:
                continue
            if symbol and str(order.intent.symbol or "").upper() != symbol:
                continue
            count += 1
        return count

    def _wait_for_outbound_risk_sends(
        self,
        context: str,
        symbol: str = "",
    ) -> bool:
        symbol = str(symbol or "").upper()
        timeout_sec = self.outbound_gate_drain_timeout_sec
        deadline = time.perf_counter() + timeout_sec
        while True:
            with self._outbound_gate_condition:
                inflight = (
                    self._outbound_risk_sends_inflight_by_symbol.get(
                        symbol,
                        0,
                    )
                    if symbol
                    else self._outbound_risk_sends_inflight
                )
            with self.lock:
                settling = self._submit_settlement_count_locked(
                    risk_increasing_only=True,
                    symbol=symbol,
                )
            if inflight <= 0 and settling <= 0:
                return True
            remaining = deadline - time.perf_counter()
            if remaining <= 0.0:
                break
            with self._outbound_gate_condition:
                self._outbound_gate_condition.wait(
                    timeout=min(remaining, 0.05)
                )

        logger.critical(
            "[OMS] Outbound risk-send drain timed out "
            f"context={context} symbol={symbol or '*'} inflight={inflight} "
            f"settling={settling} "
            f"timeout={timeout_sec:.3f}s"
        )
        try:
            self._audit(
                "outbound_gate_drain_timeout",
                context=context,
                symbol=symbol,
                inflight=inflight,
                settling=settling,
                timeout_sec=timeout_sec,
            )
        except JournalError as exc:
            logger.critical(
                "[OMS] Could not persist outbound gate timeout: "
                f"{type(exc).__name__}:{exc}"
            )
        return False

    def _wait_for_outbound_order_sends(self, context: str) -> bool:
        timeout_sec = self.outbound_gate_drain_timeout_sec
        deadline = time.perf_counter() + timeout_sec
        while True:
            with self._outbound_gate_condition:
                inflight = self._outbound_order_sends_inflight
            with self.lock:
                settling = self._submit_settlement_count_locked()
            if inflight <= 0 and settling <= 0:
                return True
            remaining = deadline - time.perf_counter()
            if remaining <= 0.0:
                break
            with self._outbound_gate_condition:
                self._outbound_gate_condition.wait(
                    timeout=min(remaining, 0.05)
                )

        logger.critical(
            "[OMS] Outbound order-send drain timed out "
            f"context={context} inflight={inflight} settling={settling} "
            f"timeout={timeout_sec:.3f}s"
        )
        try:
            self._audit(
                "outbound_order_gate_drain_timeout",
                context=context,
                inflight=inflight,
                settling=settling,
                timeout_sec=timeout_sec,
            )
        except JournalError as exc:
            logger.critical(
                "[OMS] Could not persist outbound order gate timeout: "
                f"{type(exc).__name__}:{exc}"
            )
        return False

    def close_outbound_gate(self, reason: str, wait: bool = True) -> bool:
        """Seal new risk sends while leaving cancels and reductions available."""
        with self.lock:
            self._close_outbound_gate_locked(reason, hold="operator")
        if not wait:
            return True
        return self._wait_for_outbound_risk_sends(reason or "operator")

    def begin_shutdown(self, reason: str = "operator_shutdown") -> bool:
        """Latch shutdown and wait until every order-send call has returned."""
        reason = str(reason or "operator_shutdown")
        first_request = False
        with self.lock:
            if not self._shutdown_requested:
                first_request = True
                self._shutdown_requested = True
                self._shutdown_reason = reason
                self._shutdown_cancel_verified = False
            self._close_outbound_gate_locked(reason, hold="shutdown")
            self._lifecycle_generation += 1
            if self.state != LifecycleState.HALTED:
                self.state = LifecycleState.FROZEN
                self.last_freeze_reason = f"Shutdown: {reason}"
                self._sync_capability_mode(f"shutdown:{reason}")

        if first_request:
            try:
                self._audit("shutdown_started", reason=reason)
            except JournalError as exc:
                logger.critical(
                    "[OMS] Could not persist shutdown latch: "
                    f"{type(exc).__name__}:{exc}"
                )
                return False
        sends_drained = self._wait_for_outbound_order_sends(f"shutdown:{reason}")
        with self.lock:
            reconcile_thread = self._reconcile_thread
            calibration_enforcement_thread = (
                self._rpi_calibration_enforcement_thread
            )
        reconcile_stopped = True
        if (
            reconcile_thread is not None
            and reconcile_thread.is_alive()
            and not reconcile_thread.is_current()
        ):
            reconcile_thread.join(timeout=self.outbound_gate_drain_timeout_sec)
            reconcile_stopped = not reconcile_thread.is_alive()
            if not reconcile_stopped:
                logger.critical("[OMS] Reconcile worker did not stop before shutdown")
        calibration_enforcement_stopped = True
        if (
            calibration_enforcement_thread is not None
            and calibration_enforcement_thread.is_alive()
            and not calibration_enforcement_thread.is_current()
        ):
            calibration_enforcement_thread.join(
                timeout=self.outbound_gate_drain_timeout_sec
            )
            calibration_enforcement_stopped = (
                not calibration_enforcement_thread.is_alive()
            )
            if not calibration_enforcement_stopped:
                logger.critical(
                    "[OMS] RPI calibration enforcement worker did not stop "
                    "before shutdown"
                )
        return bool(
            sends_drained
            and reconcile_stopped
            and calibration_enforcement_stopped
        )

    def verify_preconnect_shutdown_no_order_path(
        self,
        source: str = "preconnect_shutdown",
    ) -> bool:
        """Durably prove that an unconnected shutdown had no local send path."""
        source = str(source or "preconnect_shutdown")
        failure_reason = ""
        journal_error = None
        with self.lock:
            active_orders = self._collect_local_active_orders_locked()
            with self._outbound_gate_condition:
                gate_open = self._outbound_gate_open
                gate_holds = sorted(self._outbound_gate_holds)
                order_sends = self._outbound_order_sends_inflight
                risk_sends = self._outbound_risk_sends_inflight
                shutdown_requested = self._shutdown_requested
                if not shutdown_requested:
                    failure_reason = "shutdown_not_latched"
                elif gate_open:
                    failure_reason = "outbound_gate_open"
                elif active_orders:
                    failure_reason = "local_active_or_unknown_orders"
                elif order_sends != 0 or risk_sends != 0:
                    failure_reason = "outbound_sends_inflight"

                payload = {
                    "source": source,
                    "shutdown_requested": shutdown_requested,
                    "outbound_gate_open": gate_open,
                    "outbound_gate_holds": gate_holds,
                    "local_active_or_unknown_order_count": len(active_orders),
                    "order_sends_inflight": order_sends,
                    "risk_sends_inflight": risk_sends,
                    "state": self.state.value,
                }
                try:
                    if failure_reason:
                        payload["reason"] = failure_reason
                        committed_seq = self.audit_logger.audit(
                            "preconnect_shutdown_no_order_path_rejected",
                            payload,
                        )
                    else:
                        committed_seq = self.audit_logger.audit(
                            "preconnect_shutdown_no_order_path_verified",
                            payload,
                        )
                    if not committed_seq:
                        raise JournalError(
                            "Pre-connect shutdown proof was not committed"
                        )
                    if not failure_reason:
                        self._shutdown_cancel_verified = True
                        return True
                except Exception as exc:
                    self._shutdown_cancel_verified = False
                    journal_error = exc
            if failure_reason or journal_error is not None:
                self._shutdown_cancel_verified = False
                self._close_outbound_gate_locked(
                    "preconnect_shutdown_no_order_path_unverified",
                    hold="shutdown",
                )

        if journal_error is not None:
            self._fail_closed_on_journal_error(
                journal_error,
                "preconnect_shutdown_no_order_path",
            )
        return False

    def _rpi_calibration_active_orders_locked(self) -> list:
        return self.rpi_calibration_runtime._rpi_calibration_active_orders_locked()

    @staticmethod
    def _exchange_ns_to_iso(value: int) -> str:
        return RpiCalibrationRuntime._exchange_ns_to_iso(value)

    def _rpi_calibration_snapshot_locked(self) -> dict:
        return self.rpi_calibration_runtime._rpi_calibration_snapshot_locked()

    def get_outbound_gate_snapshot(self) -> dict:
        with self.lock:
            calibration_snapshot = self._rpi_calibration_snapshot_locked()
            with self._outbound_gate_condition:
                return {
                    "open": self._outbound_gate_open,
                    "epoch": self._outbound_gate_epoch,
                    "reason": self._outbound_gate_reason,
                    "all_order_seal_reason": (
                        self._outbound_all_order_seal_reason
                    ),
                    "holds": sorted(self._outbound_gate_holds),
                    "risk_sends_inflight": self._outbound_risk_sends_inflight,
                    "risk_sends_inflight_by_symbol": dict(
                        self._outbound_risk_sends_inflight_by_symbol
                    ),
                    "order_sends_inflight": self._outbound_order_sends_inflight,
                    "submit_settlements_inflight": len(
                        self._submit_settlement_inflight_oids
                    ),
                    "queued_submit_cancels": len(
                        self._submit_cancel_requested_oids
                    ),
                    "shutdown_requested": self._shutdown_requested,
                    "shutdown_cancel_verified": (
                        self._shutdown_cancel_verified
                    ),
                    "drain_timeout_sec": self.outbound_gate_drain_timeout_sec,
                    "rpi_calibration": calibration_snapshot,
                }

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

    def get_known_account_order_symbols(self) -> list[str]:
        with self.lock:
            return sorted(
                str(symbol or "").upper()
                for symbol in self._known_account_order_symbols
                if str(symbol or "").strip()
            )

    def _sync_capability_mode(self, reason: str = ""):
        with self.lock:
            previous_mode = getattr(self, "capability_mode", None)
            previous_reason = getattr(self, "capability_reason", "")
            base_mode = self._capability_mode_for_state()
            override_mode = self.mode_override
            next_mode = base_mode
            next_reason = reason or self.state.value.lower()
            if override_mode and self._mode_rank(override_mode) > self._mode_rank(base_mode):
                next_mode = override_mode
                next_reason = self.mode_override_reason or next_reason

            changed = previous_mode != next_mode or previous_reason != next_reason
            self.capability_mode = next_mode
            self.capability_reason = next_reason
            self._refresh_outbound_gate_locked(next_reason)
            if changed:
                self._audit(
                    "capability_mode_changed",
                    mode=next_mode.value,
                    reason=next_reason,
                    previous_mode=previous_mode.value if previous_mode else "",
                    previous_reason=previous_reason,
                )

    def _mode_rank(self, mode: OMSCapabilityMode) -> int:
        ranks = {
            OMSCapabilityMode.LIVE: 0,
            OMSCapabilityMode.DEGRADED: 1,
            OMSCapabilityMode.PASSIVE_ONLY: 2,
            OMSCapabilityMode.REDUCE_ONLY: 3,
            OMSCapabilityMode.CANCEL_ONLY: 4,
            OMSCapabilityMode.READ_ONLY: 5,
            OMSCapabilityMode.LOCKDOWN: 6,
        }
        return ranks.get(mode, 99)

    @staticmethod
    def _mode_constraint_key(reason: str) -> str:
        reason = str(reason or "").strip()
        if not reason:
            return "unspecified"
        prefix, separator, _detail = reason.partition(":")
        return f"{prefix}:" if separator else reason

    def _refresh_selected_mode_constraint(self):
        if not self.mode_constraints:
            self.mode_override = None
            self.mode_override_reason = ""
            return
        _key, (mode, reason) = max(
            self.mode_constraints.items(),
            key=lambda item: (self._mode_rank(item[1][0]), item[0]),
        )
        self.mode_override = mode
        self.mode_override_reason = reason

    def _capability_mode_for_state(self) -> OMSCapabilityMode:
        if self.state == LifecycleState.LIVE:
            return OMSCapabilityMode.LIVE
        if self.state in {LifecycleState.BOOTSTRAP, LifecycleState.RECONCILING}:
            return OMSCapabilityMode.READ_ONLY
        if self.state in {LifecycleState.FROZEN, LifecycleState.HALTED}:
            return OMSCapabilityMode.CANCEL_ONLY
        return OMSCapabilityMode.LOCKDOWN

    def _ensure_capability_mode_consistent(self):
        expected_mode = self._capability_mode_for_state()
        if self.mode_override and self._mode_rank(self.mode_override) > self._mode_rank(expected_mode):
            expected_mode = self.mode_override
        if self.capability_mode != expected_mode:
            self._sync_capability_mode(f"state_sync:{self.state.value}")

    def set_trading_mode(self, mode, reason: str):
        if isinstance(mode, str):
            mode = OMSCapabilityMode(mode)
        if mode not in {
            OMSCapabilityMode.DEGRADED,
            OMSCapabilityMode.PASSIVE_ONLY,
            OMSCapabilityMode.REDUCE_ONLY,
        }:
            raise ValueError(f"Unsupported trading mode override: {mode}")
        constraint_key = self._mode_constraint_key(reason)
        force_generation = constraint_key == "venue_dead_man_switch:"
        with self.lock:
            previous_mode = (
                self.mode_override.value if self.mode_override else ""
            )
            previous_reason = self.mode_override_reason
            previous_constraint = self.mode_constraints.get(constraint_key)
            if (
                not force_generation
                and previous_constraint == (mode, reason)
            ):
                return (
                    self.mode_override == mode
                    and self.mode_override_reason == reason
                )

            self.mode_constraint_generation += 1
            constraint_generation = self.mode_constraint_generation
            self.mode_constraints[constraint_key] = (mode, reason)
            self.mode_constraint_generations[constraint_key] = (
                constraint_generation
            )
            self._refresh_selected_mode_constraint()
            self._sync_capability_mode(self.mode_override_reason or reason)
            selected = (
                self.mode_override == mode
                and self.mode_override_reason == reason
            )
            reduce_only_selected = (
                self.mode_override == OMSCapabilityMode.REDUCE_ONLY
                and previous_constraint != (mode, reason)
            )
            self._audit(
                "trading_mode_override_set",
                mode=mode.value,
                reason=reason,
                constraint_key=constraint_key,
                constraint_generation=constraint_generation,
                selected=selected,
                previous_mode=previous_mode,
                previous_reason=previous_reason,
            )
        if reduce_only_selected:
            self._wait_for_outbound_risk_sends(
                f"trading_mode_reduce_only:{reason}"
            )
            self._cancel_orders_matching(lambda order: not order.intent.reduce_only)
        return selected

    def clear_trading_mode(
        self,
        reason: str = "",
        prefixes=(),
        *,
        expected_generations=None,
    ):
        journal_error = None
        with self.lock:
            if not self.mode_constraints:
                return False

            expected = (
                {
                    str(key): int(generation)
                    for key, generation in expected_generations.items()
                }
                if expected_generations is not None
                else None
            )
            candidate_keys = [
                key
                for key, (
                    _mode,
                    constraint_reason,
                ) in self.mode_constraints.items()
                if not prefixes
                or any(
                    constraint_reason.startswith(prefix)
                    for prefix in prefixes
                )
            ]
            matching_keys = [
                key
                for key in candidate_keys
                if expected is None
                or (
                    key in expected
                    and self.mode_constraint_generations.get(key)
                    == expected[key]
                )
            ]
            if not matching_keys:
                return False

            previous_mode = (
                self.mode_override.value if self.mode_override else ""
            )
            previous_reason = self.mode_override_reason
            previous_capability_mode = self.capability_mode
            previous_capability_reason = self.capability_reason
            cleared_generations = {
                key: self.mode_constraint_generations.get(key, 0)
                for key in matching_keys
            }
            cleared_constraints = {
                key: self.mode_constraints[key]
                for key in matching_keys
            }
            for key in matching_keys:
                self.mode_constraints.pop(key, None)
                self.mode_constraint_generations.pop(key, None)
            self._refresh_selected_mode_constraint()
            try:
                self._sync_capability_mode(
                    self.mode_override_reason
                    or reason
                    or "trading_mode_cleared"
                )
                self._audit(
                    "trading_mode_override_cleared",
                    reason=reason or previous_reason,
                    cleared_constraint_keys=matching_keys,
                    cleared_constraint_generations=cleared_generations,
                    previous_mode=previous_mode,
                    previous_reason=previous_reason,
                )
            except JournalError as exc:
                self.mode_constraints.update(cleared_constraints)
                self.mode_constraint_generations.update(
                    cleared_generations
                )
                self._refresh_selected_mode_constraint()
                self.capability_mode = previous_capability_mode
                self.capability_reason = previous_capability_reason
                self._close_outbound_gate_locked(
                    "durable_journal_unavailable:"
                    "clear_trading_mode",
                    hold="journal_failure",
                )
                journal_error = exc

        if journal_error is not None:
            self._fail_closed_on_journal_error(
                journal_error,
                "clear_trading_mode",
            )
            return False
        return True

    def has_trading_mode_constraint(self, prefixes=()) -> bool:
        with self.lock:
            if not prefixes:
                return bool(self.mode_constraints)
            return any(
                any(
                    constraint_reason.startswith(prefix)
                    for prefix in prefixes
                )
                for _mode, constraint_reason in self.mode_constraints.values()
            )

    def can_query_exchange(self) -> bool:
        self._ensure_capability_mode_consistent()
        return self.capability_mode != OMSCapabilityMode.LOCKDOWN

    def can_cancel_orders(self) -> bool:
        self._ensure_capability_mode_consistent()
        return self.capability_mode in {
            OMSCapabilityMode.LIVE,
            OMSCapabilityMode.DEGRADED,
            OMSCapabilityMode.PASSIVE_ONLY,
            OMSCapabilityMode.REDUCE_ONLY,
            OMSCapabilityMode.CANCEL_ONLY,
        }

    def can_open_new_risk(self) -> bool:
        self._ensure_capability_mode_consistent()
        return self.capability_mode in {
            OMSCapabilityMode.LIVE,
            OMSCapabilityMode.DEGRADED,
            OMSCapabilityMode.PASSIVE_ONLY,
        }

    def record_risk_control_heartbeat(
        self,
        source: str = "risk_manager",
        healthy: bool = True,
        reason: str = "",
    ) -> bool:
        if not self.risk_control_heartbeat_enabled:
            return False

        status = "healthy" if healthy else "unhealthy"
        source = str(source or "risk_manager")
        reason = str(reason or "")
        if (
            self.risk_control_heartbeat_required_source
            and source != self.risk_control_heartbeat_required_source
        ):
            should_audit = False
            with self.lock:
                if source not in self.rejected_risk_control_heartbeat_sources:
                    self.rejected_risk_control_heartbeat_sources.add(source)
                    should_audit = True
            if should_audit:
                self._audit(
                    "risk_control_heartbeat_source_rejected",
                    source=source,
                    required_source=self.risk_control_heartbeat_required_source,
                )
            return False
        with self.lock:
            previous_status = self.risk_control_heartbeat_status
            previous_reason = self.risk_control_heartbeat_reason
            self.last_risk_control_heartbeat_monotonic = time.perf_counter()
            self.last_risk_control_heartbeat_time = time.time()
            self.risk_control_heartbeat_status = status
            self.risk_control_heartbeat_source = source
            self.risk_control_heartbeat_reason = reason

        if status != previous_status or reason != previous_reason:
            self._audit(
                "risk_control_heartbeat_status",
                status=status,
                source=source,
                reason=reason,
            )
        return healthy

    def get_risk_control_heartbeat_snapshot(self) -> dict:
        with self.lock:
            monotonic_timestamp = self.last_risk_control_heartbeat_monotonic
            status = self.risk_control_heartbeat_status
            source = self.risk_control_heartbeat_source
            reason = self.risk_control_heartbeat_reason
            wall_time = self.last_risk_control_heartbeat_time
        age_sec = (
            max(0.0, time.perf_counter() - monotonic_timestamp)
            if monotonic_timestamp > 0.0
            else None
        )
        return {
            "enabled": self.risk_control_heartbeat_enabled,
            "status": status,
            "source": source,
            "reason": reason,
            "last_heartbeat_time": wall_time,
            "age_sec": age_sec,
            "max_age_sec": self.risk_control_heartbeat_max_age_sec,
            "required_source": self.risk_control_heartbeat_required_source,
            "valid": bool(
                not self.risk_control_heartbeat_enabled
                or (
                    status == "healthy"
                    and age_sec is not None
                    and age_sec <= self.risk_control_heartbeat_max_age_sec
                )
            ),
        }

    def _venue_dead_man_switch_health_locked(self, now: float = None):
        if not self.venue_dead_man_switch_enabled:
            return True, ""
        missing_symbols = sorted(
            self.venue_dead_man_switch_symbols
            - self.venue_dead_man_armed_symbols
        )
        if missing_symbols:
            return False, f"unarmed_symbols:{','.join(missing_symbols)}"
        if self.last_venue_dead_man_success_monotonic <= 0.0:
            return False, "renewal_missing"
        if self.venue_dead_man_last_error:
            return False, self.venue_dead_man_last_error

        now = time.perf_counter() if now is None else float(now)
        age_sec = max(
            0.0,
            now - self.last_venue_dead_man_success_monotonic,
        )
        if age_sec > self.venue_dead_man_switch_max_renewal_age_sec:
            return (
                False,
                f"renewal_stale:{age_sec:.3f}s>"
                f"{self.venue_dead_man_switch_max_renewal_age_sec:.3f}s",
            )
        return True, ""

    def get_venue_dead_man_switch_snapshot(self) -> dict:
        with self.lock:
            now = time.perf_counter()
            healthy, reason = self._venue_dead_man_switch_health_locked(now)
            success_monotonic = self.last_venue_dead_man_success_monotonic
            return {
                "enabled": self.venue_dead_man_switch_enabled,
                "valid": healthy,
                "reason": reason,
                "countdown_time_ms": self.venue_dead_man_switch_countdown_time_ms,
                "renewal_interval_sec": (
                    self.venue_dead_man_switch_renewal_interval_sec
                ),
                "max_renewal_age_sec": (
                    self.venue_dead_man_switch_max_renewal_age_sec
                ),
                "last_success_time": self.last_venue_dead_man_success_time,
                "age_sec": (
                    max(0.0, now - success_monotonic)
                    if success_monotonic > 0.0
                    else None
                ),
                "armed_symbols": sorted(self.venue_dead_man_armed_symbols),
                "required_symbols": sorted(self.venue_dead_man_switch_symbols),
                "failure_count": self.venue_dead_man_failure_count,
                "recovery_count": self.venue_dead_man_recovery_count,
                "recovery_checks": self.venue_dead_man_switch_recovery_checks,
                "last_error": self.venue_dead_man_last_error,
                "renewal_inflight": self.venue_dead_man_renewal_inflight,
                "safety_cancel_inflight": bool(
                    self._venue_dead_man_safety_cancel_thread is not None
                    and self._venue_dead_man_safety_cancel_thread.is_alive()
                ),
            }

    def _venue_dead_man_switch_renewal_allowed_locked(
        self,
        *,
        force: bool = False,
    ) -> tuple[bool, str]:
        if not self.venue_dead_man_switch_enabled:
            return True, ""
        if self._stopped:
            return False, "oms_stopped"
        if self._shutdown_requested:
            return False, "shutdown_requested"
        if force:
            return True, ""
        if self.state != LifecycleState.LIVE:
            return False, f"lifecycle_not_live:{self.state.value}"
        if self._has_active_guards():
            return False, "oms_guard_active"
        if self.mode_constraints:
            return False, "trading_mode_constraint_active"
        if self.capability_mode != OMSCapabilityMode.LIVE:
            return False, f"capability_not_live:{self.capability_mode.value}"
        healthy, reason = self._venue_dead_man_switch_health_locked()
        if not healthy:
            return False, reason or "renewal_health_invalid"
        return True, ""

    def can_renew_venue_dead_man_switch(self) -> bool:
        """Return whether a routine renewal may extend venue order lifetimes."""
        with self.lock:
            allowed, _reason = (
                self._venue_dead_man_switch_renewal_allowed_locked()
            )
            return allowed

    @staticmethod
    def _venue_dead_man_constraint_reason(reason: str) -> str:
        category = str(reason or "unhealthy").partition(":")[0]
        return f"venue_dead_man_switch:{category or 'unhealthy'}"

    def _start_venue_dead_man_safety_cancel(self, reason: str) -> bool:
        now = time.perf_counter()
        with self.lock:
            thread = self._venue_dead_man_safety_cancel_thread
            if thread is not None and thread.is_alive():
                return False
            if (
                self._venue_dead_man_safety_cancel_last_attempt > 0.0
                and now - self._venue_dead_man_safety_cancel_last_attempt
                < self.venue_dead_man_safety_cancel_retry_sec
            ):
                return False

        def cancel_and_verify():
            published.wait()
            try:
                verified = self.cancel_all_account_orders_verified(
                    self.gateway,
                    source="venue_dead_man_switch",
                    timeout_sec=self.venue_dead_man_safety_cancel_timeout_sec,
                )
                self._audit(
                    "venue_dead_man_switch_safety_cancel_completed",
                    reason=reason,
                    verified=bool(verified),
                )
            finally:
                with self.lock:
                    handle = self._venue_dead_man_safety_cancel_thread
                    if handle is not None and handle.is_current():
                        self._venue_dead_man_safety_cancel_thread = None

        published = threading.Event()
        thread = self._submit_background_task(
            "dms:safety-cancel",
            cancel_and_verify,
            name="VenueDeadManSafetyCancel",
            safety=True,
        )
        with self.lock:
            if thread is None:
                published.set()
                return False
            self._venue_dead_man_safety_cancel_thread = thread
            self._venue_dead_man_safety_cancel_last_attempt = now
        published.set()
        return True

    def handle_venue_dead_man_switch_unhealthy(
        self,
        reason: str = "",
    ) -> bool:
        if not self.venue_dead_man_switch_enabled:
            return True
        with self.lock:
            healthy, detected_reason = self._venue_dead_man_switch_health_locked()
        if healthy:
            return True

        detail = str(reason or detected_reason or "unhealthy")
        constraint_reason = self._venue_dead_man_constraint_reason(detail)
        with self.lock:
            if not self.venue_dead_man_last_error:
                self.venue_dead_man_last_error = detail
                self.venue_dead_man_failure_count += 1
                self.venue_dead_man_recovery_count = 0
        self.set_trading_mode(
            OMSCapabilityMode.REDUCE_ONLY,
            constraint_reason,
        )
        self._start_venue_dead_man_safety_cancel(detail)
        self._audit(
            "venue_dead_man_switch_unhealthy_latched",
            reason=detail,
            constraint=constraint_reason,
        )
        return False

    def request_venue_dead_man_switch_renewal(self) -> bool:
        """Schedule a due DMS renewal without blocking the risk heartbeat."""
        if not self.venue_dead_man_switch_enabled:
            return True

        now = time.perf_counter()
        with self.lock:
            allowed, authorization_reason = (
                self._venue_dead_man_switch_renewal_allowed_locked()
            )
            healthy, health_reason = self._venue_dead_man_switch_health_locked(now)
            if not allowed:
                schedule_renewal = False
            else:
                since_attempt = now - self.last_venue_dead_man_attempt_monotonic
                schedule_renewal = (
                    not self.venue_dead_man_renewal_inflight
                    and not (
                        self.last_venue_dead_man_attempt_monotonic > 0.0
                        and since_attempt
                        < self.venue_dead_man_switch_renewal_interval_sec
                    )
                )
                if schedule_renewal:
                    self.venue_dead_man_renewal_inflight = True

        if not healthy:
            self.handle_venue_dead_man_switch_unhealthy(
                health_reason or authorization_reason
            )
        if not allowed:
            return False
        if not schedule_renewal:
            return healthy

        def renew_in_background():
            published.wait()
            try:
                self.renew_venue_dead_man_switch()
            finally:
                with self.lock:
                    self.venue_dead_man_renewal_inflight = False
                    handle = self._venue_dead_man_renewal_thread
                    if handle is not None and handle.is_current():
                        self._venue_dead_man_renewal_thread = None

        published = threading.Event()
        thread = self._submit_background_task(
            "dms:renewal",
            renew_in_background,
            name="VenueDeadManRenewal",
            safety=True,
        )
        with self.lock:
            if thread is None:
                self.venue_dead_man_renewal_inflight = False
                published.set()
                return False
            self._venue_dead_man_renewal_thread = thread
        published.set()
        return healthy

    def renew_venue_dead_man_switch(self, force: bool = False) -> bool:
        if not self.venue_dead_man_switch_enabled:
            return True

        now = time.perf_counter()
        with self.lock:
            allowed, authorization_reason = (
                self._venue_dead_man_switch_renewal_allowed_locked(
                    force=force,
                )
            )
            if not allowed:
                healthy, health_reason = (
                    self._venue_dead_man_switch_health_locked(now)
                )
            else:
                since_attempt = (
                    now - self.last_venue_dead_man_attempt_monotonic
                )
                if (
                    not force
                    and self.last_venue_dead_man_attempt_monotonic > 0.0
                    and since_attempt
                    < self.venue_dead_man_switch_renewal_interval_sec
                ):
                    healthy, _reason = (
                        self._venue_dead_man_switch_health_locked(now)
                    )
                    return healthy
                self.last_venue_dead_man_attempt_monotonic = now

        if not allowed:
            if not force and not healthy:
                self.handle_venue_dead_man_switch_unhealthy(
                    health_reason or authorization_reason
                )
            return False

        renew = getattr(self.gateway, "set_countdown_cancel_all", None)
        renewed_symbols = set()
        failures = []
        if not callable(renew):
            failures.append("gateway_method_unavailable")
        else:
            for symbol in sorted(self.venue_dead_man_switch_symbols):
                try:
                    response = renew(
                        symbol,
                        self.venue_dead_man_switch_countdown_time_ms,
                    )
                    status_code = getattr(response, "status_code", None)
                    if response is True or status_code == 200:
                        renewed_symbols.add(symbol)
                        continue
                    failures.append(
                        f"{symbol}:status={status_code if status_code is not None else 'unknown'}"
                    )
                except Exception as exc:
                    failures.append(f"{symbol}:{type(exc).__name__}:{exc}")

        if failures:
            failure_reason = "renewal_failed:" + ";".join(failures)
            with self.lock:
                self.venue_dead_man_armed_symbols = renewed_symbols
                self.venue_dead_man_failure_count += 1
                self.venue_dead_man_recovery_count = 0
                self.venue_dead_man_last_error = failure_reason
            self.handle_venue_dead_man_switch_unhealthy(failure_reason)
            self._audit(
                "venue_dead_man_switch_renewal_failed",
                reason=failure_reason,
                renewed_symbols=sorted(renewed_symbols),
                required_symbols=sorted(self.venue_dead_man_switch_symbols),
            )
            return False

        constraint_key = "venue_dead_man_switch:"
        with self.lock:
            observed_constraint = self.mode_constraints.get(constraint_key)
            had_constraint = bool(
                observed_constraint
                and observed_constraint[1].startswith(constraint_key)
            )
            observed_generation = (
                self.mode_constraint_generations.get(constraint_key, 0)
                if had_constraint
                else 0
            )
            first_success = self.last_venue_dead_man_success_monotonic <= 0.0
            recovered_from_error = bool(self.venue_dead_man_last_error)
            self.venue_dead_man_armed_symbols = renewed_symbols
            self.last_venue_dead_man_success_monotonic = now
            self.last_venue_dead_man_success_time = time.time()
            self.venue_dead_man_failure_count = 0
            self.venue_dead_man_last_error = ""
            if had_constraint:
                self.venue_dead_man_recovery_count += 1
            else:
                self.venue_dead_man_recovery_count = 0
            recovery_count = self.venue_dead_man_recovery_count

        cleared = False
        if (
            had_constraint
            and recovery_count >= self.venue_dead_man_switch_recovery_checks
        ):
            cleared = self.clear_trading_mode(
                reason="venue dead-man switch renewal recovered",
                prefixes=("venue_dead_man_switch:",),
                expected_generations={
                    constraint_key: observed_generation,
                },
            )
            with self.lock:
                self.venue_dead_man_recovery_count = 0

        if first_success or recovered_from_error or cleared:
            self._audit(
                "venue_dead_man_switch_renewed",
                armed_symbols=sorted(renewed_symbols),
                recovered=bool(cleared),
                recovery_count=recovery_count,
                observed_constraint_generation=observed_generation,
            )
        return True

    def _ensure_venue_dead_man_switch_armed(self, context: str) -> bool:
        with self.lock:
            renewal_thread = self._venue_dead_man_renewal_thread
        if (
            renewal_thread is not None
            and renewal_thread.is_alive()
            and not renewal_thread.is_current()
        ):
            renewal_thread.join(
                timeout=max(
                    1.0,
                    self.venue_dead_man_switch_max_renewal_age_sec,
                )
            )
            if renewal_thread.is_alive():
                reason = f"venue_dead_man_switch_renewal_stuck:{context}"
                logger.critical(f"[OMS] {reason}")
                self.halt_system(reason)
                return False
        if self.renew_venue_dead_man_switch(force=True):
            return True
        reason = f"venue_dead_man_switch_unavailable:{context}"
        logger.critical(f"[OMS] {reason}")
        self.halt_system(reason)
        return False

    def get_capability_snapshot(self) -> dict:
        with self.lock:
            self._ensure_capability_mode_consistent()
            capability_mode = self.capability_mode
            capability_reason = self.capability_reason
            mode_override = self.mode_override
            mode_override_reason = self.mode_override_reason
            mode_constraint_generation = (
                self.mode_constraint_generation
            )
            mode_constraints = {
                key: {
                    "mode": mode.value,
                    "reason": reason,
                    "generation": self.mode_constraint_generations.get(
                        key,
                        0,
                    ),
                }
                for key, (mode, reason) in self.mode_constraints.items()
            }
        return {
            "mode": capability_mode.value,
            "reason": capability_reason,
            "override_mode": (
                mode_override.value if mode_override else ""
            ),
            "override_reason": mode_override_reason,
            "mode_constraint_generation": (
                mode_constraint_generation
            ),
            "mode_constraints": mode_constraints,
            "can_query": capability_mode != OMSCapabilityMode.LOCKDOWN,
            "can_cancel": capability_mode
            in {
                OMSCapabilityMode.LIVE,
                OMSCapabilityMode.DEGRADED,
                OMSCapabilityMode.PASSIVE_ONLY,
                OMSCapabilityMode.REDUCE_ONLY,
                OMSCapabilityMode.CANCEL_ONLY,
            },
            "can_open_risk": capability_mode
            in {
                OMSCapabilityMode.LIVE,
                OMSCapabilityMode.DEGRADED,
                OMSCapabilityMode.PASSIVE_ONLY,
            },
            "risk_control_heartbeat": self.get_risk_control_heartbeat_snapshot(),
            "venue_dead_man_switch": self.get_venue_dead_man_switch_snapshot(),
            "outbound_message_budget": self.get_outbound_message_budget_snapshot(),
            "background_tasks": self.get_background_task_snapshot(),
            "outbound_gate": self.get_outbound_gate_snapshot(),
            "strategy_risk_budgets": self.get_strategy_risk_budget_snapshot(),
            "single_writer_fence": (
                self.single_writer_fence.health_snapshot()
                if self.single_writer_fence is not None
                else {"held": False, "enabled": False}
            ),
        }

    def _get_capability_block_reason(self, action: str) -> str:
        return (
            f"{action}_blocked:"
            f"{self.capability_mode.value}:{self.capability_reason or self.state.value}"
        )

    def query_account_info(self):
        if not self.can_query_exchange():
            self._audit("query_rejected", query="account", reason=self._get_capability_block_reason("query"))
            return None
        return self.gateway.get_account_info()

    def sync_account_margin_health(
        self,
        account: dict,
        snapshot_time: float = None,
        snapshot_monotonic: float = None,
    ) -> bool:
        if not isinstance(account, dict):
            return False
        maintenance_margin = account.get("totalMaintMargin")
        margin_balance = account.get("totalMarginBalance")
        if maintenance_margin is None or margin_balance is None:
            return False
        try:
            maintenance_margin = float(maintenance_margin)
            margin_balance = float(margin_balance)
        except (TypeError, ValueError):
            self._audit(
                "account_margin_health_invalid",
                maintenance_margin=maintenance_margin,
                margin_balance=margin_balance,
            )
            return False
        if not math.isfinite(maintenance_margin) or not math.isfinite(margin_balance):
            self._audit(
                "account_margin_health_invalid",
                maintenance_margin=maintenance_margin,
                margin_balance=margin_balance,
                reason="non_finite",
            )
            return False
        if maintenance_margin < 0.0:
            self._audit(
                "account_margin_health_invalid",
                maintenance_margin=maintenance_margin,
                margin_balance=margin_balance,
                reason="negative_maintenance_margin",
            )
            return False

        snapshot_time = float(snapshot_time or time.time())
        snapshot_monotonic = float(
            snapshot_monotonic or time.perf_counter()
        )
        with self.lock:
            synced = self.account.sync_margin_health(
                maintenance_margin,
                margin_balance,
                snapshot_time=snapshot_time,
                snapshot_monotonic=snapshot_monotonic,
            )
        if synced:
            self._audit(
                "account_margin_health_synced",
                maintenance_margin=maintenance_margin,
                margin_balance=margin_balance,
                ratio=self.account.maintenance_margin_ratio,
                snapshot_time=snapshot_time,
                snapshot_monotonic=snapshot_monotonic,
            )
        return synced

    def query_positions(self):
        if not self.can_query_exchange():
            self._audit("query_rejected", query="positions", reason=self._get_capability_block_reason("query"))
            return None
        return self.gateway.get_all_positions()

    def query_open_orders(self):
        if not self.can_query_exchange():
            self._audit("query_rejected", query="open_orders", reason=self._get_capability_block_reason("query"))
            return None
        return self.gateway.get_open_orders()

    def query_order(self, symbol: str, order_id: str):
        if not self.can_query_exchange():
            return None
        query = getattr(self.gateway, "get_order", None)
        if not callable(query):
            return None
        return query(symbol, order_id)

    def query_user_trades(self, symbol: str, **kwargs):
        if not self.can_query_exchange():
            return None
        query = getattr(self.gateway, "get_user_trades", None)
        if not callable(query):
            return None
        return query(symbol, **kwargs)

    def query_income_history(self, **kwargs):
        if not self.can_query_exchange():
            self._audit(
                "query_rejected",
                query="income_history",
                reason=self._get_capability_block_reason("query"),
            )
            return None
        query = getattr(self.gateway, "get_income_history", None)
        if not callable(query):
            return None
        return query(**kwargs)

    def _normalize_submit_command(self, raw_result) -> GatewayCommandResult:
        if isinstance(raw_result, GatewayCommandResult):
            return raw_result
        if isinstance(raw_result, str) and raw_result:
            return GatewayCommandResult(
                CommandOutcome.ACKNOWLEDGED,
                exchange_oid=raw_result,
            )
        # Compatibility for simple/custom gateways which predate the explicit
        # command outcome contract. The Binance gateway never returns bare None.
        return GatewayCommandResult(
            CommandOutcome.REJECTED,
            error_message="gateway_send_failed",
        )

    def _get_final_outbound_send_rejection_locked(
        self,
        *,
        permit_epoch: int | None,
        intent: OrderIntent,
        client_oid: str,
        risk_increasing: bool,
        allow_shutdown_emergency: bool,
    ) -> tuple[str, str]:
        """Revalidate a prepared order immediately before transport dispatch."""
        with self._outbound_gate_condition:
            if permit_epoch is None:
                return "outbound_send_permit_missing", ""
            if self._stopped:
                reason = self._shutdown_reason or "oms_stopping"
                return f"shutdown_requested:{reason}", ""
            if self._outbound_all_order_seal_reason:
                return (
                    "outbound_order_gate_closed:"
                    f"{self._outbound_all_order_seal_reason}",
                    "",
                )
            if self._shutdown_requested and not allow_shutdown_emergency:
                reason = self._shutdown_reason or "oms_stopping"
                return f"shutdown_requested:{reason}", ""
            if risk_increasing and permit_epoch != self._outbound_gate_epoch:
                return (
                    "outbound_gate_epoch_changed:"
                    f"{permit_epoch}!={self._outbound_gate_epoch}",
                    "",
                )
            if risk_increasing and not self._outbound_gate_open:
                reason = self._outbound_gate_reason or "closed"
                return f"outbound_gate_closed:{reason}", ""

        if client_oid in self._submit_cancel_requested_oids:
            return "cancel_requested_before_transport", ""

        if not risk_increasing:
            return "", ""

        block_reason = self._get_order_block_reason(
            intent.strategy_id,
            intent.symbol,
            reduce_only=intent.reduce_only,
        )
        if block_reason:
            return block_reason, ""
        if (
            self.capability_mode == OMSCapabilityMode.PASSIVE_ONLY
            and not intent.is_post_only
        ):
            return "oms_mode_passive_only_changed_before_dispatch", ""
        if (
            self.capability_mode == OMSCapabilityMode.DEGRADED
            and self.degraded_aggressive_to_passive
            and not intent.is_post_only
        ):
            return "oms_mode_degraded_changed_before_dispatch", ""

        for check in (
            self._get_clock_health_rejection_locked,
            self._get_venue_dead_man_switch_rejection_locked,
            self._get_risk_control_heartbeat_rejection_locked,
            self._get_margin_health_rejection_locked,
            self._get_self_trade_prevention_rejection_locked,
        ):
            rejection = check(intent)
            if rejection:
                return rejection, ""

        if not self._rpi_calibration["enabled"]:
            return "", ""
        if self._rpi_calibration_expired:
            return (
                "rpi_calibration_permit_expired_at_dispatch",
                self._rpi_calibration_expiry_reason or "permit_expired",
            )
        if self._rpi_calibration_restart_rearm_blocked:
            return (
                "rpi_calibration_restart_blocked_at_dispatch",
                "unclean_restart_requires_new_permit",
            )
        if client_oid not in self._rpi_calibration_reservation_ids:
            return (
                "rpi_calibration_reservation_missing_at_dispatch",
                "reservation_missing_at_dispatch",
            )
        if not self._rpi_calibration_permit_activated:
            return (
                "rpi_calibration_permit_inactive_at_dispatch",
                "permit_inactive_at_dispatch",
            )

        now_ns = time_service.now_ns()
        if now_ns < self._rpi_calibration["not_before_ns"]:
            return (
                "rpi_calibration_permit_not_yet_valid_at_dispatch",
                "permit_time_regressed_before_not_before",
            )
        if now_ns >= self._rpi_calibration["expires_at_ns"]:
            return (
                "rpi_calibration_permit_expired_at_dispatch",
                "permit_expired",
            )

        loss_truth_reason, _ = self._observe_rpi_calibration_loss_locked(
            initialize_baseline=False,
        )
        if loss_truth_reason:
            return (
                "rpi_calibration_loss_truth_unavailable_at_dispatch",
                f"calibration_loss_truth_unavailable:{loss_truth_reason}",
            )
        if (
            self._rpi_calibration_peak_observed_loss_microu
            >= self._rpi_calibration_effective_loss_cap_microu
        ):
            return (
                "rpi_calibration_loss_cap_exhausted_at_dispatch",
                "max_calibration_loss_exhausted",
            )
        return "", ""

    def _dispatch_gateway_order_with_final_fence(
        self,
        request: OrderRequest,
        client_oid: str,
        intent: OrderIntent,
        *,
        permit_epoch: int | None,
        risk_increasing: bool,
        allow_shutdown_emergency: bool = False,
    ) -> tuple[GatewayCommandResult, str]:
        terminal_reason = ""
        outbound_reservation = None
        message_kind = (
            self.OUTBOUND_REDUCE_ORDER
            if request.reduce_only
            else self.OUTBOUND_NEW_ORDER
        )

        def reserve_transport_message() -> str:
            nonlocal outbound_reservation
            if outbound_reservation is not None:
                return ""
            reservation, budget_rejection = (
                self._outbound_budget.reserve_token(message_kind)
            )
            if not budget_rejection:
                outbound_reservation = reservation
            return budget_rejection

        try:
            with self.lock:
                rejection, terminal_reason = (
                    self._get_final_outbound_send_rejection_locked(
                        permit_epoch=permit_epoch,
                        intent=intent,
                        client_oid=client_oid,
                        risk_increasing=risk_increasing,
                        allow_shutdown_emergency=allow_shutdown_emergency,
                    )
                )
                if terminal_reason:
                    self._close_outbound_gate_locked(
                        f"rpi_calibration:{terminal_reason}",
                        hold="rpi_calibration_dispatch_revoked",
                    )
        except Exception as exc:
            rejection = (
                "outbound_send_fence_unavailable:"
                f"{type(exc).__name__}:{exc}"
            )
            if self._rpi_calibration["enabled"] and risk_increasing:
                terminal_reason = "dispatch_fence_unavailable"
            with self.lock:
                self._close_outbound_gate_locked(
                    rejection,
                    hold="outbound_dispatch_fence_failure",
                )

        if rejection:
            try:
                self._audit(
                    "outbound_send_fence_rejected",
                    client_oid=client_oid,
                    symbol=intent.symbol,
                    strategy_id=intent.strategy_id,
                    permit_epoch=permit_epoch,
                    current_gate_epoch=self._outbound_gate_epoch,
                    risk_increasing=risk_increasing,
                    allow_shutdown_emergency=allow_shutdown_emergency,
                    reason=rejection,
                    calibration_terminal_reason=terminal_reason,
                )
            except Exception as exc:
                logger.critical(
                    "[OMS] Could not audit outbound send-fence rejection: "
                    f"{type(exc).__name__}:{exc}"
                )
            return (
                GatewayCommandResult(
                    CommandOutcome.REJECTED,
                    error_code="OUTBOUND_SEND_FENCE_REVOKED",
                    error_message=rejection,
                ),
                terminal_reason,
            )

        def transport_pre_send_guard():
            nonlocal terminal_reason
            try:
                with self.lock:
                    transport_rejection, transport_terminal_reason = (
                        self._get_final_outbound_send_rejection_locked(
                            permit_epoch=permit_epoch,
                            intent=intent,
                            client_oid=client_oid,
                            risk_increasing=risk_increasing,
                            allow_shutdown_emergency=(
                                allow_shutdown_emergency
                            ),
                        )
                    )
                    if transport_terminal_reason:
                        terminal_reason = transport_terminal_reason
                        self._close_outbound_gate_locked(
                            f"rpi_calibration:{terminal_reason}",
                            hold="rpi_calibration_dispatch_revoked",
                        )
            except Exception as exc:
                transport_rejection = (
                    "outbound_transport_guard_unavailable:"
                    f"{type(exc).__name__}:{exc}"
                )
                if self._rpi_calibration["enabled"] and risk_increasing:
                    terminal_reason = "transport_guard_unavailable"
                with self.lock:
                    self._close_outbound_gate_locked(
                        transport_rejection,
                        hold="outbound_transport_guard_failure",
                    )
            if transport_rejection:
                return (
                    False,
                    "OUTBOUND_SEND_FENCE_REVOKED",
                    transport_rejection,
                )
            budget_rejection = reserve_transport_message()
            if budget_rejection:
                return (
                    False,
                    "OUTBOUND_MESSAGE_BUDGET",
                    budget_rejection,
                )
            return True, "", ""

        supports_transport_guard = bool(
            getattr(
                self.gateway,
                "supports_outbound_send_guard",
                False,
            )
        )
        if (
            not supports_transport_guard
            and self._rpi_calibration["enabled"]
            and risk_increasing
        ):
            terminal_reason = "transport_guard_unavailable"
            with self.lock:
                self._close_outbound_gate_locked(
                    "rpi_calibration:transport_guard_unavailable",
                    hold="outbound_transport_guard_failure",
                )
            return (
                GatewayCommandResult(
                    CommandOutcome.REJECTED,
                    error_code="OUTBOUND_SEND_FENCE_REVOKED",
                    error_message="outbound_transport_guard_unavailable",
                ),
                terminal_reason,
            )

        try:
            if supports_transport_guard:
                raw_result = self.gateway.send_order(
                    request,
                    client_oid,
                    pre_send_guard=transport_pre_send_guard,
                )
            else:
                budget_rejection = reserve_transport_message()
                if budget_rejection:
                    return (
                        GatewayCommandResult(
                            CommandOutcome.REJECTED,
                            error_code="OUTBOUND_MESSAGE_BUDGET",
                            error_message=budget_rejection,
                        ),
                        terminal_reason,
                    )
                raw_result = self.gateway.send_order(request, client_oid)
            command = self._normalize_submit_command(raw_result)
        except Exception as exc:
            command = GatewayCommandResult(
                CommandOutcome.UNKNOWN,
                error_message=f"gateway_send_exception:{exc}",
            )
        return command, terminal_reason

    def _bind_submit_exchange_oid_locked(
        self,
        order: Order,
        exchange_oid: str,
        *,
        source: str,
    ) -> str:
        """Bind a transport ACK without overwriting stronger exchange truth."""
        exchange_oid = str(exchange_oid or "")
        if not exchange_oid:
            return ""
        if order.exchange_oid and order.exchange_oid != exchange_oid:
            reason = (
                f"submit_exchange_oid_mismatch:{order.client_oid}:"
                f"{order.exchange_oid}!={exchange_oid}"
            )
            self._audit(
                "submit_exchange_oid_mismatch",
                client_oid=order.client_oid,
                local_exchange_oid=order.exchange_oid,
                transport_exchange_oid=exchange_oid,
                source=source,
            )
            return reason
        mapped_order = self.exchange_id_map.get(exchange_oid)
        if mapped_order is not None and mapped_order is not order:
            reason = (
                f"submit_exchange_oid_collision:{order.client_oid}:"
                f"{exchange_oid}"
            )
            self._audit(
                "submit_exchange_oid_collision",
                client_oid=order.client_oid,
                exchange_oid=exchange_oid,
                mapped_client_oid=mapped_order.client_oid,
                source=source,
            )
            return reason
        order.exchange_oid = exchange_oid
        self.exchange_id_map[exchange_oid] = order
        return ""

    def _handle_submit_transport_conflict(
        self,
        order: Order,
        reason: str,
    ) -> None:
        if not reason:
            return
        context = f"submit_transport_conflict:{order.client_oid}"
        try:
            self.freeze_symbol(
                order.intent.symbol,
                f"order_truth:{reason}",
                cancel_active_orders=False,
            )
            self.trigger_reconcile(
                reason,
                suspicious_oid=order.client_oid,
            )
        except JournalError as exc:
            try:
                self._fail_closed_on_journal_error(
                    exc,
                    context,
                    order.intent.symbol,
                )
            except BaseException as fail_closed_exc:
                logger.critical(
                    "[OMS] Submit transport conflict could not complete "
                    f"fail-closed client_oid={order.client_oid}: "
                    f"{type(fail_closed_exc).__name__}:{fail_closed_exc}"
                )
        except BaseException as exc:
            self._close_gate_after_submit_settlement_failure(
                order,
                context,
                exc,
            )
        finally:
            try:
                self._on_order_truth_check(
                    f"Submit transport conflict: {reason}",
                    suspicious_oid=order.client_oid,
                )
            except BaseException as truth_exc:
                logger.critical(
                    "[OMS] Submit transport conflict could not start "
                    f"truth resolution client_oid={order.client_oid}: "
                    f"{type(truth_exc).__name__}:{truth_exc}"
                )

    def _commit_gateway_submission(self, client_oid: str) -> None:
        """Release gateways which stage exchange events until the OMS ACK is durable.

        Live gateways do not implement this hook.  A local paper venue does: its
        ``send_order`` call returns an acknowledgement without publishing NEW or
        fill events, then this hook releases those events only after the OMS has
        persisted the ACK and installed the exchange-order mapping.
        """
        commit = getattr(self.gateway, "commit_order_submission", None)
        if not callable(commit):
            return
        committed = commit(client_oid)
        if committed is False:
            raise RuntimeError("gateway rejected the submit commit barrier")

    def _notify_order_state_safely(self, order: Order, context: str) -> None:
        """Notify local observers without changing the transport outcome."""
        try:
            self.order_monitor.on_order_update(order.client_oid, order.status)
        except BaseException as exc:
            logger.critical(
                "[OMS] Order monitor notification failed after submit "
                f"context={context} client_oid={order.client_oid}: "
                f"{type(exc).__name__}:{exc}"
            )
        try:
            self._emit_order_update(order)
        except BaseException as exc:
            logger.critical(
                "[OMS] Order update publication failed after submit "
                f"context={context} client_oid={order.client_oid}: "
                f"{type(exc).__name__}:{exc}"
            )
        if context not in {"submit_ack", "internal_submit_ack"}:
            self._finish_submit_settlement(order, context)

    def _finish_submit_settlement(
        self,
        order: Order,
        context: str,
    ) -> None:
        """Release submit coordination and honor one queued cancel."""
        client_oid = order.client_oid
        with self.lock:
            if client_oid not in self._submit_settlement_inflight_oids:
                return
            self._submit_settlement_inflight_oids.discard(client_oid)
            with self._outbound_gate_condition:
                self._outbound_gate_condition.notify_all()
            cancel_requested = (
                client_oid in self._submit_cancel_requested_oids
            )
            current = self.orders.get(client_oid)
            journal_failed = "journal_failure" in self._outbound_gate_holds
            should_cancel = bool(
                cancel_requested
                and current is not None
                and current.is_active()
                and not self._stopped
                and not journal_failed
            )
            if not should_cancel:
                self._submit_cancel_requested_oids.discard(client_oid)

        if not should_cancel:
            return

        def cancel_after_settlement():
            with self.lock:
                self._submit_cancel_requested_oids.discard(client_oid)
                current_order = self.orders.get(client_oid)
                stopped = self._stopped
            if stopped or current_order is None or not current_order.is_active():
                return
            try:
                cancel_admitted = bool(self.cancel_order(client_oid))
            except BaseException as exc:
                cancel_admitted = False
                logger.critical(
                    "[OMS] Queued post-submit cancel raised "
                    f"client_oid={client_oid}: "
                    f"{type(exc).__name__}:{exc}"
                )
            if cancel_admitted:
                return
            with self.lock:
                unresolved = self.orders.get(client_oid)
                still_active = bool(
                    unresolved is not None and unresolved.is_active()
                )
            if still_active:
                self._handle_submit_transport_conflict(
                    unresolved,
                    f"queued_cancel_not_admitted:{context}",
                )

        task_key = f"post-submit-cancel:{client_oid}"
        try:
            handle = self._submit_background_task(
                task_key,
                cancel_after_settlement,
                name=f"PostSubmitCancel-{client_oid}",
                safety=True,
            )
        except BaseException as exc:
            handle = None
            logger.critical(
                "[OMS] Could not enqueue queued post-submit cancel "
                f"client_oid={client_oid}: "
                f"{type(exc).__name__}:{exc}"
            )
        if handle is None:
            with self.lock:
                self._submit_cancel_requested_oids.discard(client_oid)
                unresolved = self.orders.get(client_oid)
                still_active = bool(
                    unresolved is not None and unresolved.is_active()
                )
            cancel_admitted = False
            if still_active:
                try:
                    cancel_admitted = bool(self.cancel_order(client_oid))
                except BaseException as exc:
                    logger.critical(
                        "[OMS] Synchronous queued-cancel fallback failed "
                        f"client_oid={client_oid}: "
                        f"{type(exc).__name__}:{exc}"
                    )
            if cancel_admitted:
                return
            if still_active:
                self._handle_submit_transport_conflict(
                    unresolved,
                    f"queued_cancel_enqueue_failed:{context}",
                )

    def _publish_order_submitted_safely(
        self,
        request: OrderRequest,
        order: Order,
        submitted_status: OrderStatus,
        context: str,
    ) -> None:
        """Publish a derived event without turning a durable ACK into failure."""
        try:
            self.event_engine.put(
                Event(
                    EVENT_ORDER_SUBMITTED,
                    OrderSubmitted(
                        request,
                        order.client_oid,
                        time.time(),
                        submitted_status,
                        monotonic_timestamp=time.perf_counter(),
                    ),
                )
            )
        except BaseException as exc:
            logger.critical(
                "[OMS] Order-submitted publication failed "
                f"context={context} client_oid={order.client_oid}: "
                f"{type(exc).__name__}:{exc}"
            )

    def _audit_post_submit_safely(
        self,
        kind: str,
        order: Order,
        **payload,
    ) -> None:
        """Fail closed on durable-audit loss while preserving submit truth."""
        try:
            self._audit(kind, **payload)
        except JournalError as exc:
            try:
                self._fail_closed_on_journal_error(
                    exc,
                    f"post_submit_audit:{kind}",
                    order.intent.symbol,
                )
            except BaseException as fail_closed_exc:
                logger.critical(
                    "[OMS] Post-submit audit failure could not complete "
                    f"fail-closed context={kind} "
                    f"client_oid={order.client_oid}: "
                    f"{type(fail_closed_exc).__name__}:{fail_closed_exc}"
                )
        except BaseException as exc:
            logger.critical(
                "[OMS] Post-submit audit raised unexpectedly "
                f"context={kind} client_oid={order.client_oid}: "
                f"{type(exc).__name__}:{exc}"
            )

    def _latch_submit_ambiguity_locked(
        self,
        order: Order,
        context: str,
    ) -> str:
        """Durably freeze one symbol without waiting on the current send lease."""
        if not order.is_active():
            return ""
        symbol = str(order.intent.symbol or "").upper()
        reason = f"order_truth:submit_exception:{order.client_oid}"
        owner = self._symbol_guard_owner(reason)
        records = self._ensure_symbol_guard_records_locked(symbol)
        previous_reason = self.symbol_guards.get(symbol, "")
        previous_owner_reason = str(
            (records.get(owner) or {}).get("reason", "") or ""
        )
        epoch = max(
            [
                int(self.symbol_guard_epoch_counters.get(symbol, 0) or 0),
                *(
                    int(record.get("epoch", 0) or 0)
                    for record in records.values()
                ),
            ]
        ) + 1
        self._audit(
            (
                "symbol_frozen"
                if previous_owner_reason != reason
                else "symbol_freeze_reasserted"
            ),
            symbol=symbol,
            reason=reason,
            previous_reason=previous_reason,
            previous_owner_reason=previous_owner_reason,
            owner=owner,
            epoch=epoch,
            source=context,
        )
        records[owner] = {"reason": reason, "epoch": epoch}
        self.symbol_guard_epoch_counters[symbol] = epoch
        self._refresh_symbol_guard_effective_locked(symbol)
        self._refresh_outbound_gate_locked(
            f"submit_exception:{symbol}:{order.client_oid}"
        )
        return reason

    def _close_gate_after_submit_settlement_failure(
        self,
        order: Order,
        context: str,
        exc: BaseException,
    ) -> None:
        """Last-resort in-memory fence when even ambiguity settlement fails."""
        logger.critical(
            "[OMS] Submit exception settlement failed "
            f"context={context} client_oid={order.client_oid}: "
            f"{type(exc).__name__}:{exc}"
        )
        try:
            with self.lock:
                if order.status == OrderStatus.SUBMITTING:
                    order.mark_submit_unknown(
                        f"submit_settlement_failed:{type(exc).__name__}"
                    )
                self._close_outbound_gate_locked(
                    f"submit_settlement_failed:{context}",
                    hold="submit_exception_settlement_failure",
                )
                if self.state not in {
                    LifecycleState.HALTED,
                    LifecycleState.RECONCILING,
                }:
                    self.state = LifecycleState.FROZEN
                    self._lifecycle_generation += 1
                    self.last_freeze_reason = (
                        f"submit_settlement_failed:{context}"
                    )
                    self._sync_capability_mode(self.last_freeze_reason)
        except BaseException as fence_exc:
            logger.critical(
                "[OMS] Could not install submit settlement fallback fence "
                f"client_oid={order.client_oid}: "
                f"{type(fence_exc).__name__}:{fence_exc}"
            )
    def _cleanup_pre_dispatch_submit_exception(
        self,
        order: Order | None,
        exc: BaseException,
        context: str,
        snapshot_source: str,
        **snapshot_extra,
    ) -> JournalError | None:
        """Durably terminate a prepared order which never reached transport."""
        if order is None:
            return None
        journal_failure = None
        try:
            with self.lock:
                if order.status in {
                    OrderStatus.CREATED,
                    OrderStatus.SUBMITTING,
                }:
                    order.mark_rejected_locally(
                        f"pre_dispatch_exception:{type(exc).__name__}"
                    )
                self._record_order_snapshot(
                    order,
                    f"{snapshot_source}_pre_dispatch_exception",
                    exception_type=type(exc).__name__,
                    **snapshot_extra,
                )
                if order.is_terminal():
                    self._write_tombstone(order)
                    self.orders.pop(order.client_oid, None)
                    self.exposure.update_open_orders(self.orders)
                    self.account.calculate()
        except JournalError as journal_exc:
            self._latch_journal_failure(
                journal_exc,
                f"{context}_cleanup",
                order.intent.symbol,
            )
            journal_failure = journal_exc
            try:
                with self.lock:
                    if order.status in {
                        OrderStatus.CREATED,
                        OrderStatus.SUBMITTING,
                    }:
                        order.mark_rejected_locally(
                            "durable_journal_unavailable"
                        )
                    if order.is_terminal():
                        self.orders.pop(order.client_oid, None)
                        self.exposure.update_open_orders(self.orders)
                        self.account.calculate()
            except BaseException as cleanup_exc:
                self._close_gate_after_submit_settlement_failure(
                    order,
                    f"{context}_journal_cleanup",
                    cleanup_exc,
                )
        except BaseException as cleanup_exc:
            self._close_gate_after_submit_settlement_failure(
                order,
                context,
                cleanup_exc,
            )
        self._notify_order_state_safely(order, context)
        return journal_failure

    def _settle_post_dispatch_submit_exception(
        self,
        order: Order,
        command_id: str,
        exc: BaseException,
        context: str,
        snapshot_source: str,
        exchange_oid: str = "",
        **snapshot_extra,
    ) -> JournalError | None:
        """Persist ambiguity and quarantine the symbol before lease release."""
        journal_failure = None
        error_message = (
            f"post_dispatch_exception:{type(exc).__name__}:"
            f"{str(exc)[:512]}"
        )
        try:
            self._record_command_result(
                command_id,
                "SUBMIT",
                order,
                CommandOutcome.UNKNOWN,
                exchange_oid=exchange_oid or order.exchange_oid,
                error_message=error_message,
            )
            with self.lock:
                if order.status == OrderStatus.SUBMITTING:
                    order.mark_submit_unknown(error_message)
                self._record_order_snapshot(
                    order,
                    f"{snapshot_source}_post_dispatch_exception",
                    exception_type=type(exc).__name__,
                    exception_message=str(exc)[:512],
                    **snapshot_extra,
                )
                self._latch_submit_ambiguity_locked(order, context)
        except JournalError as journal_exc:
            self._latch_journal_failure(
                journal_exc,
                f"{context}_settlement",
                order.intent.symbol,
            )
            journal_failure = journal_exc
            try:
                with self.lock:
                    if order.status == OrderStatus.SUBMITTING:
                        order.mark_submit_unknown("result_not_durable")
            except BaseException as cleanup_exc:
                self._close_gate_after_submit_settlement_failure(
                    order,
                    f"{context}_journal_cleanup",
                    cleanup_exc,
                )
        except BaseException as settlement_exc:
            self._close_gate_after_submit_settlement_failure(
                order,
                context,
                settlement_exc,
            )

        self._notify_order_state_safely(order, context)
        try:
            self._on_order_truth_check(
                f"Post-dispatch submit exception: {type(exc).__name__}",
                suspicious_oid=order.client_oid,
            )
        except BaseException as truth_exc:
            logger.critical(
                "[OMS] Could not start client-ID truth resolution after "
                f"submit exception client_oid={order.client_oid}: "
                f"{type(truth_exc).__name__}:{truth_exc}"
            )
        return journal_failure

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

    def _execution_id(self, order: Order, update: ExchangeOrderUpdate) -> str:
        venue = str(getattr(self.gateway, "gateway_name", "UNKNOWN") or "UNKNOWN").upper()
        if update.trade_id >= 0:
            return f"{venue}:{order.intent.symbol}:{update.trade_id}"
        exchange_order_id = update.exchange_oid or order.exchange_oid or order.client_oid
        return (
            f"{venue}:{order.intent.symbol}:{exchange_order_id}:"
            f"{int(float(update.update_time or 0.0) * 1000)}:"
            f"{float(update.cum_filled_qty):.12g}"
        )

    def _record_execution(
        self,
        order: Order,
        update: ExchangeOrderUpdate,
        fill_qty: float,
        fee: float,
    ) -> bool:
        execution_id = self._execution_id(order, update)
        if execution_id in self.execution_ids:
            return False

        self.audit_logger.audit(
            "execution_record",
            {
                "execution_id": execution_id,
                "venue": str(
                    getattr(self.gateway, "gateway_name", "UNKNOWN") or "UNKNOWN"
                ).upper(),
                "client_oid": order.client_oid,
                "exchange_oid": update.exchange_oid or order.exchange_oid,
                "strategy_id": order.intent.strategy_id,
                "symbol": order.intent.symbol,
                "side": order.intent.side.value,
                "fill_qty": fill_qty,
                "fill_price": update.filled_price,
                "cum_filled_qty": update.cum_filled_qty,
                "exchange_status": update.status,
                "exchange_time": update.update_time,
                "trade_id": update.trade_id,
                "commission": update.commission,
                "commission_asset": update.commission_asset,
                "booked_fee": fee,
                "realized_pnl": update.realized_pnl,
                "is_maker": update.is_maker,
                "order_type": order.intent.order_type,
                "time_in_force": order.intent.time_in_force,
                "is_rpi": order.intent.is_rpi,
                "reduce_only": order.intent.reduce_only,
                "pre_status": order.status.value,
            },
        )
        self.execution_ids.add(execution_id)
        return True

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

    def _on_order_truth_check(self, reason: str, suspicious_oid: str = None):
        if not suspicious_oid:
            return
        with self.lock:
            if suspicious_oid in self._order_truth_resolution_inflight:
                return
            self._order_truth_resolution_inflight.add(suspicious_oid)

        handle = self._submit_background_task(
            f"order-truth:{suspicious_oid}",
            self._resolve_order_truth,
            suspicious_oid,
            reason,
            name=f"OrderTruth-{suspicious_oid}",
        )
        if handle is None:
            with self.lock:
                self._order_truth_resolution_inflight.discard(
                    suspicious_oid
                )

    def _resolve_order_truth(self, client_oid: str, reason: str = ""):
        try:
            with self.lock:
                order = self.orders.get(client_oid)
                if not order or order.is_terminal():
                    return
                symbol = order.intent.symbol
                target_id = order.exchange_oid or order.client_oid
                local_status = order.status

            remote = self.query_order(symbol, target_id)
            if remote is None:
                self._audit(
                    "order_truth_query_unavailable",
                    client_oid=client_oid,
                    symbol=symbol,
                    reason=reason,
                )
                if local_status in {OrderStatus.SUBMIT_UNKNOWN, OrderStatus.CANCEL_UNKNOWN}:
                    self.order_monitor.on_order_update(client_oid, local_status)
                return

            if remote.get("_query_status") == "NOT_FOUND":
                count = self._unknown_not_found_counts.get(client_oid, 0) + 1
                self._unknown_not_found_counts[client_oid] = count
                with self.lock:
                    order = self.orders.get(client_oid)
                    elapsed = (
                        time.perf_counter() - order.updated_monotonic
                        if order
                        else 0.0
                    )
                    current_status = order.status if order else None

                self._audit(
                    "order_truth_not_found",
                    client_oid=client_oid,
                    symbol=symbol,
                    confirmations=count,
                    elapsed_sec=elapsed,
                    local_status=current_status.value if current_status else "",
                )
                if (
                    current_status == OrderStatus.SUBMIT_UNKNOWN
                    and count >= self.unknown_order_min_not_found
                    and elapsed >= self.unknown_order_resolution_timeout_sec
                ):
                    with self.lock:
                        order = self.orders.get(client_oid)
                        if order and order.status == OrderStatus.SUBMIT_UNKNOWN:
                            order.mark_rejected_locally("exchange_confirmed_order_absent")
                            self._record_order_snapshot(order, "submit_unknown_absent")
                            self._emit_order_update(order)
                            self._write_tombstone(order)
                            self.exposure.update_open_orders(self.orders)
                            self.account.calculate()
                    self._clear_order_truth_guard(symbol, client_oid)
                    return

                if current_status in {OrderStatus.SUBMIT_UNKNOWN, OrderStatus.CANCEL_UNKNOWN}:
                    if (
                        current_status == OrderStatus.CANCEL_UNKNOWN
                        and count >= self.unknown_order_min_not_found
                        and elapsed >= self.unknown_order_resolution_timeout_sec
                    ):
                        self.trigger_reconcile(
                            "Cancel outcome remained unresolvable",
                            suspicious_oid=client_oid,
                        )
                        return
                    self.order_monitor.on_order_update(client_oid, current_status)
                elif current_status in {OrderStatus.CANCELLING, OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}:
                    self.trigger_reconcile("Order absent from targeted query", suspicious_oid=client_oid)
                return

            self._unknown_not_found_counts.pop(client_oid, None)
            if not self._backfill_trade_history(
                symbols={symbol},
                end_time_ms=time_service.now(),
            ):
                self.trigger_reconcile(
                    "Exact trade history unavailable during order truth query",
                    suspicious_oid=client_oid,
                )
                return
            if not self._apply_exchange_order_snapshot(
                remote,
                source="targeted_order_query",
            ):
                self.trigger_reconcile(
                    "Order snapshot is ahead of exact trade history",
                    suspicious_oid=client_oid,
                )
                return

            with self.lock:
                order = self.orders.get(client_oid)
                resolved_status = order.status if order else None

            if (
                local_status in {OrderStatus.CANCEL_UNKNOWN, OrderStatus.CANCELLING}
                and resolved_status in {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}
            ):
                self.cancel_order(client_oid)
            elif resolved_status not in {OrderStatus.SUBMIT_UNKNOWN, OrderStatus.CANCEL_UNKNOWN}:
                self._clear_order_truth_guard(symbol, client_oid)
        except Exception as exc:
            self._audit(
                "order_truth_resolution_failed",
                client_oid=client_oid,
                reason=reason,
                error=str(exc),
            )
            logger.error(f"[OMS] Order truth resolution failed for {client_oid}: {exc}")
        finally:
            with self.lock:
                self._order_truth_resolution_inflight.discard(client_oid)

    def _clear_order_truth_guard(self, symbol: str, client_oid: str = ""):
        symbol = str(symbol or "").upper()
        client_oid = str(client_oid or "")
        with self.lock:
            records = self._ensure_symbol_guard_records_locked(symbol)
            matches = [
                (
                    owner,
                    str(record.get("reason", "") or ""),
                    int(record.get("epoch", 0) or 0),
                )
                for owner, record in records.items()
                if str(record.get("reason", "") or "").startswith("order_truth:")
                and (
                    not client_oid
                    or str(record.get("reason", "") or "").endswith(
                        f":{client_oid}"
                    )
                )
            ]
        for owner, reason, epoch in matches:
            self.clear_symbol_freeze(
                symbol,
                reason="order truth resolved",
                expected_epoch=epoch,
                expected_reason=reason,
                expected_owner=owner,
            )

    def _create_recovered_order(self, remote: dict) -> Order:
        symbol = str(remote.get("symbol", "") or "").upper()
        exchange_oid = str(remote.get("orderId", "") or "")
        client_oid = str(remote.get("clientOrderId", "") or "")
        if not client_oid:
            client_oid = f"EXTERNAL_{symbol}_{exchange_oid}"
        side = Side(str(remote.get("side", "BUY") or "BUY").upper())
        volume = float(remote.get("origQty", remote.get("executedQty", 0.0)) or 0.0)
        if volume <= 0:
            volume = float(remote.get("qty", 0.0) or 0.0)
        price = float(
            remote.get("price", 0.0)
            or remote.get("avgPrice", 0.0)
            or remote.get("trade_price", 0.0)
            or 1.0
        )
        intent = OrderIntent(
            strategy_id="exchange_recovery",
            symbol=symbol,
            side=side,
            price=price,
            volume=volume,
            order_type=str(remote.get("type", "LIMIT") or "LIMIT"),
            time_in_force=str(remote.get("timeInForce", TIF_GTC) or TIF_GTC),
            is_post_only=str(remote.get("timeInForce", "") or "").upper()
            in {TIF_GTX, TIF_RPI},
            reduce_only=bool(remote.get("reduceOnly", False)),
            policy=ExecutionPolicy.PASSIVE,
            tag="exchange_recovered",
        )
        order = Order(client_oid, intent)
        order.mark_submitting()
        order.mark_new(exchange_oid=exchange_oid)
        self.orders[client_oid] = order
        if exchange_oid:
            self.exchange_id_map[exchange_oid] = order
        self._schedule_rpi_calibration_runtime_enforcement(
            terminal_truth_changed=True,
        )
        self._audit(
            "external_order_recovered",
            client_oid=client_oid,
            exchange_oid=exchange_oid,
            symbol=symbol,
        )
        return order

    def _apply_exchange_order_snapshot(self, remote: dict, source: str = "order_query"):
        if not isinstance(remote, dict) or remote.get("_query_status"):
            return False
        client_oid = str(remote.get("clientOrderId", "") or "")
        exchange_oid = str(remote.get("orderId", "") or "")
        with self.lock:
            order = self.orders.get(client_oid) if client_oid else None
            if not order and exchange_oid:
                order = self.exchange_id_map.get(exchange_oid)
            if not order:
                order = self._create_recovered_order(remote)

        status = str(remote.get("status", "") or "").upper()
        if status == "EXPIRED_IN_MATCH":
            status = "EXPIRED"
        if status not in {"NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED", "REJECTED"}:
            self._schedule_rpi_calibration_runtime_enforcement(
                terminal_truth_changed=True,
            )
            self._audit("unhandled_order_snapshot_status", status=status, source=source)
            return False

        executed_qty = float(remote.get("executedQty", 0.0) or 0.0)
        avg_price = float(remote.get("avgPrice", 0.0) or remote.get("price", 0.0) or 0.0)
        update_time_ms = remote.get("updateTime") or remote.get("time") or time_service.now()
        remote_order_type = str(remote.get("type", "") or "").upper()
        remote_time_in_force = str(remote.get("timeInForce", "") or "").upper()
        if executed_qty > order.filled_volume + 1e-9:
            self._schedule_rpi_calibration_runtime_enforcement(
                terminal_truth_changed=True,
            )
            truth_reason = (
                f"execution_truth:{order.intent.symbol}:{order.client_oid}:"
                "order_snapshot_ahead"
            )
            self._audit(
                "order_snapshot_trade_truth_missing",
                client_oid=order.client_oid,
                exchange_oid=exchange_oid,
                local_filled=order.filled_volume,
                exchange_filled=executed_qty,
                source=source,
            )
            self.freeze_system(
                truth_reason,
                cancel_active_orders=False,
            )
            return False

        update = ExchangeOrderUpdate(
            client_oid=order.client_oid,
            exchange_oid=exchange_oid or order.exchange_oid,
            symbol=order.intent.symbol,
            status=status,
            filled_qty=max(0.0, executed_qty - order.filled_volume),
            filled_price=avg_price,
            cum_filled_qty=executed_qty,
            update_time=float(update_time_ms) / 1000.0,
            seq=0,
            order_type=remote_order_type,
            time_in_force=remote_time_in_force,
        )
        self._apply_event(Event(EVENT_EXCHANGE_ORDER_UPDATE, update))
        self._audit(
            "order_snapshot_applied",
            client_oid=order.client_oid,
            exchange_oid=update.exchange_oid,
            exchange_status=status,
            source=source,
        )
        return True

    def _advance_trade_cursor(self, symbol: str, trade_id: int, trade_time: float, source: str):
        symbol = str(symbol or "").upper()
        trade_id = int(trade_id)
        if trade_id < 0 or str(source or "") == "user_stream":
            return False
        with self.lock:
            current = int(self.trade_cursors.get(symbol, -1))
            if trade_id <= current:
                return False
            self.audit_logger.audit(
                "trade_cursor_advanced",
                {
                    "symbol": symbol,
                    "trade_id": trade_id,
                    "trade_time": trade_time,
                    "source": source,
                },
            )
            self.trade_cursors[symbol] = trade_id
        return True

    def _apply_exchange_trade(self, trade: dict) -> bool:
        if not isinstance(trade, dict):
            return False
        symbol = str(trade.get("symbol", "") or "").upper()
        try:
            trade_id = int(trade.get("id", -1))
        except (TypeError, ValueError):
            return False
        if not symbol or trade_id < 0:
            return False
        if trade_id <= int(self.trade_cursors.get(symbol, -1)):
            return True

        exchange_oid = str(trade.get("orderId", "") or "")
        with self.lock:
            order = self.exchange_id_map.get(exchange_oid)
        if not order:
            remote = self.query_order(symbol, exchange_oid)
            if not remote or remote.get("_query_status") == "NOT_FOUND":
                return False
            remote = dict(remote)
            remote["trade_price"] = trade.get("price", 0.0)
            with self.lock:
                order = self._create_recovered_order(remote)

        venue = str(
            getattr(self.gateway, "gateway_name", "UNKNOWN") or "UNKNOWN"
        ).upper()
        execution_id = f"{venue}:{symbol}:{trade_id}"
        with self.lock:
            if execution_id in self.execution_ids:
                self.rest_confirmed_execution_ids.add(execution_id)
                self._advance_trade_cursor(
                    symbol,
                    trade_id,
                    float(trade.get("time", 0) or 0) / 1000.0,
                    source="rest_confirmed_duplicate",
                )
                self._audit(
                    "trade_backfill_duplicate_confirmed",
                    symbol=symbol,
                    trade_id=trade_id,
                    execution_id=execution_id,
                )
                return True

        original_terminal_status = order.status if order.is_terminal() else None
        original_terminal_time = order.last_exchange_update_time

        qty = float(trade.get("qty", 0.0) or 0.0)
        price = float(trade.get("price", 0.0) or 0.0)
        if qty <= 0 or price <= 0:
            return False
        cumulative = min(order.intent.volume, order.filled_volume + qty)
        status = "FILLED" if cumulative >= order.intent.volume - 1e-8 else "PARTIALLY_FILLED"
        trade_time_ms = int(trade.get("time", time_service.now()) or time_service.now())
        update = ExchangeOrderUpdate(
            client_oid=order.client_oid,
            exchange_oid=order.exchange_oid or exchange_oid,
            symbol=symbol,
            status=status,
            filled_qty=qty,
            filled_price=price,
            cum_filled_qty=cumulative,
            update_time=trade_time_ms / 1000.0,
            seq=0,
            commission=float(trade.get("commission", 0.0) or 0.0),
            commission_asset=str(trade.get("commissionAsset", "") or ""),
            realized_pnl=float(trade.get("realizedPnl", 0.0) or 0.0),
            is_maker=bool(trade.get("maker")) if "maker" in trade else None,
            trade_id=trade_id,
        )
        self._apply_event(Event(EVENT_EXCHANGE_ORDER_UPDATE, update))
        if order.filled_volume + 1e-9 < cumulative:
            self._audit(
                "trade_backfill_apply_failed",
                symbol=symbol,
                trade_id=trade_id,
                client_oid=order.client_oid,
                expected_cumulative=cumulative,
                actual_cumulative=order.filled_volume,
            )
            return False
        if (
            original_terminal_status in {OrderStatus.CANCELLED, OrderStatus.EXPIRED}
            and order.status == OrderStatus.PARTIALLY_FILLED
        ):
            with self.lock:
                if original_terminal_status == OrderStatus.CANCELLED:
                    order.mark_cancelled(
                        update_time=max(original_terminal_time, update.update_time),
                        exchange_status="CANCELED",
                    )
                else:
                    order.mark_expired(
                        update_time=max(original_terminal_time, update.update_time),
                    )
                self.order_monitor.on_order_update(order.client_oid, order.status)
                self.exposure.update_open_orders(self.orders)
                self.account.calculate()
                self._record_order_snapshot(order, "terminal_restored_after_trade_backfill")
                self._emit_order_update(order)
        with self.lock:
            self.rest_confirmed_execution_ids.add(execution_id)
        self._advance_trade_cursor(symbol, trade_id, update.update_time, source="rest_backfill")
        return True

    def _backfill_trade_history(self, symbols=None, end_time_ms: int = None) -> bool:
        query = getattr(self.gateway, "get_user_trades", None)
        if not callable(query):
            return True
        symbols = set(symbols or self.config.get("symbols", []))
        end_time_ms = int(end_time_ms or time_service.now())
        limit = 1000

        for symbol in sorted(symbols):
            cursor = int(self.trade_cursors.get(symbol, -1))
            request_from_id = (
                max(0, cursor - self.trade_recovery_id_overlap + 1)
                if cursor >= 0
                else None
            )
            page_count = 0
            trades = []
            while page_count < 20:
                page_count += 1
                if request_from_id is not None:
                    trades = self.query_user_trades(
                        symbol,
                        from_id=request_from_id,
                        limit=limit,
                    )
                else:
                    prior_scan = int(self.trade_scan_end_ms.get(symbol, 0))
                    start_time = max(
                        0,
                        (prior_scan or end_time_ms - self.trade_recovery_lookback_ms)
                        - self.trade_recovery_overlap_ms,
                    )
                    trades = self.query_user_trades(
                        symbol,
                        start_time=start_time,
                        end_time=end_time_ms,
                        limit=limit,
                    )
                if trades is None:
                    return False
                if not isinstance(trades, (list, tuple)):
                    return False
                normalized_trades = []
                for trade in trades:
                    if not isinstance(trade, dict):
                        return False
                    try:
                        trade_id = int(trade.get("id", -1))
                    except (TypeError, ValueError):
                        return False
                    trade_symbol = str(
                        trade.get("symbol", symbol) or symbol
                    ).upper()
                    if trade_id < 0 or trade_symbol != str(symbol).upper():
                        return False
                    normalized_trades.append(trade)
                trades = sorted(
                    normalized_trades,
                    key=lambda trade: int(trade["id"]),
                )
                for trade in trades:
                    if not self._apply_exchange_trade(trade):
                        return False
                    cursor = max(cursor, int(trade.get("id", -1)))
                if len(trades) < limit:
                    break
                page_max_id = max(
                    int(trade.get("id", -1))
                    for trade in trades
                )
                if request_from_id is not None and page_max_id < request_from_id:
                    return False
                request_from_id = page_max_id + 1
            if page_count >= 20 and len(trades) >= limit:
                return False
            self.trade_scan_end_ms[symbol] = end_time_ms
            self.audit_logger.audit(
                "trade_scan_completed",
                {
                    "symbol": symbol,
                    "end_time_ms": end_time_ms,
                    "cursor": int(self.trade_cursors.get(symbol, -1)),
                },
            )
        return True

    def _schedule_trade_tail_verification(
        self,
        symbol: str,
        trade_id: int | None = None,
        reason: str = "execution_update",
    ) -> bool:
        if not callable(getattr(self.gateway, "get_user_trades", None)):
            return True
        symbol = str(symbol or "").upper()
        if not symbol:
            return False
        venue = str(
            getattr(self.gateway, "gateway_name", "UNKNOWN") or "UNKNOWN"
        ).upper()
        execution_id = (
            f"{venue}:{symbol}:{int(trade_id)}"
            if trade_id is not None and int(trade_id) >= 0
            else ""
        )
        with self.lock:
            if self._shutdown_requested or self._stopped:
                return False
            if execution_id:
                self.trade_tail_expected_ids.setdefault(symbol, set()).add(
                    execution_id
                )
            if symbol in self.trade_tail_verification_inflight:
                return True
            self.trade_tail_verification_inflight.add(symbol)

        def verify_tail():
            cleaned = False
            try:
                last_ok = False
                pending = set()
                for attempt in range(1, self.trade_tail_verification_attempts + 1):
                    if self._shutdown_requested or self._stopped:
                        return
                    last_ok = self._backfill_trade_history(
                        symbols={symbol},
                        end_time_ms=time_service.now(),
                    )
                    with self.lock:
                        expected = set(
                            self.trade_tail_expected_ids.get(symbol, set())
                        )
                        pending = (
                            expected - self.rest_confirmed_execution_ids
                        )
                    if last_ok and not pending:
                        with self.lock:
                            expected = set(
                                self.trade_tail_expected_ids.get(
                                    symbol,
                                    set(),
                                )
                            )
                            pending = (
                                expected - self.rest_confirmed_execution_ids
                            )
                            if not pending:
                                self.trade_tail_verification_inflight.discard(
                                    symbol
                                )
                                self.trade_tail_expected_ids.pop(symbol, None)
                                cleaned = True
                        if not pending:
                            return
                    if attempt < self.trade_tail_verification_attempts:
                        time.sleep(self.trade_tail_verification_retry_sec)

                self._audit(
                    "trade_tail_verification_failed",
                    symbol=symbol,
                    reason=reason,
                    pending_execution_ids=sorted(pending),
                    rest_query_ok=last_ok,
                )
                self.trigger_reconcile(
                    f"Trade truth verification failed for {symbol}: {reason}"
                )
            finally:
                if not cleaned:
                    with self.lock:
                        self.trade_tail_verification_inflight.discard(symbol)
                        self.trade_tail_expected_ids.pop(symbol, None)

        handle = self._submit_background_task(
            f"trade-tail:{symbol}",
            verify_tail,
            name=f"TradeTruth-{symbol}",
            delay_sec=self.trade_tail_verification_delay_sec,
        )
        if handle is None:
            with self.lock:
                self.trade_tail_verification_inflight.discard(symbol)
            return False
        return True

    def _prime_trade_history_baseline(self, end_time_ms: int, symbols=None) -> bool:
        query = getattr(self.gateway, "get_user_trades", None)
        if not callable(query):
            return True
        start_time = max(0, int(end_time_ms) - self.trade_recovery_lookback_ms)
        symbols = set(symbols or self.config.get("symbols", []))
        for symbol in sorted(symbols):
            trades = self.query_user_trades(
                symbol,
                start_time=start_time,
                end_time=int(end_time_ms),
                limit=1000,
            )
            if trades is None:
                return False
            valid_ids = [
                int(trade.get("id", -1))
                for trade in trades
                if isinstance(trade, dict) and int(trade.get("id", -1)) >= 0
            ]
            if valid_ids:
                self._advance_trade_cursor(
                    symbol,
                    max(valid_ids),
                    float(end_time_ms) / 1000.0,
                    source="bootstrap_baseline",
                )
            self.trade_scan_end_ms[symbol] = int(end_time_ms)
            self.audit_logger.audit(
                "trade_scan_completed",
                {
                    "symbol": symbol,
                    "end_time_ms": int(end_time_ms),
                    "cursor": int(self.trade_cursors.get(symbol, -1)),
                    "source": "bootstrap_baseline",
                },
            )
        return True

    @staticmethod
    def _utc_day_start_ms(now_ms: int = None) -> int:
        now = datetime.fromtimestamp(
            float(now_ms or time_service.now()) / 1000.0,
            tz=timezone.utc,
        )
        return int(
            now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            * 1000
        )

    def _external_cash_flow_income_id(self, income: dict) -> str:
        income_type = str(
            income.get("incomeType", income.get("income_type", "")) or ""
        ).upper()
        transaction_id = income.get("tranId", income.get("trandId"))
        if transaction_id not in (None, ""):
            return f"{income_type}:{transaction_id}"
        fingerprint = "|".join(
            str(income.get(key, "") or "")
            for key in ("time", "asset", "income", "symbol", "info")
        )
        digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        return f"{income_type}:hash:{digest}"

    def _apply_external_cash_flow_rows(self, rows, source: str) -> int:
        if not isinstance(rows, (list, tuple)):
            raise ValueError("income_history_not_a_list")

        normalized_rows = sorted(
            (row for row in rows if isinstance(row, dict)),
            key=lambda row: (
                int(row.get("time", 0) or 0),
                str(row.get("incomeType", row.get("income_type", "")) or ""),
                self._external_cash_flow_income_id(row),
            ),
        )
        applied = 0
        with self.lock:
            for income in normalized_rows:
                income_type = str(
                    income.get("incomeType", income.get("income_type", "")) or ""
                ).upper()
                if income_type not in self.external_income_types:
                    continue
                asset = str(income.get("asset", "") or "").upper()
                if asset not in self.external_cash_flow_assets:
                    raise ValueError(f"unsupported_external_cash_flow_asset:{asset}")
                try:
                    amount = float(income.get("income", 0.0) or 0.0)
                except (TypeError, ValueError) as exc:
                    raise ValueError("invalid_external_cash_flow_amount") from exc
                if not math.isfinite(amount):
                    raise ValueError("non_finite_external_cash_flow_amount")

                income_id = self._external_cash_flow_income_id(income)
                if income_id in self.external_cash_flow_ids:
                    continue
                self.audit_logger.audit(
                    "external_cash_flow_record",
                    {
                        "income_id": income_id,
                        "income_type": income_type,
                        "asset": asset,
                        "amount": amount,
                        "income_time_ms": int(income.get("time", 0) or 0),
                        "symbol": str(income.get("symbol", "") or ""),
                        "source": source,
                    },
                )
                self.external_cash_flow_ids.add(income_id)
                self.account.external_cash_flow_total += amount
                applied += 1
        return applied

    def mark_external_cash_flow_truth_unavailable(self, reason: str = ""):
        if not self.external_cash_flow_truth_enabled:
            return
        with self.lock:
            self.account.mark_external_cash_flow_truth_unavailable()
        self._audit(
            "external_cash_flow_truth_unavailable",
            reason=reason or "income_history_unavailable",
        )

    def backfill_external_cash_flow_history(
        self,
        query=None,
        end_time_ms: int = None,
        source: str = "rest_income_history",
    ) -> bool:
        if not self.external_cash_flow_truth_enabled:
            return True
        query = query or self.query_income_history
        if not callable(query):
            self.mark_external_cash_flow_truth_unavailable("income_query_unavailable")
            return False

        end_time_ms = int(end_time_ms or time_service.now())
        day_start_ms = self._utc_day_start_ms(end_time_ms)
        if self.external_cash_flow_scan_end_ms:
            start_time_ms = max(
                day_start_ms,
                self.external_cash_flow_scan_end_ms
                - self.external_cash_flow_recovery_overlap_ms,
            )
        else:
            start_time_ms = max(
                day_start_ms,
                end_time_ms - self.external_cash_flow_recovery_lookback_ms,
            )

        limit = 1000
        page = 1
        last_page_full = False
        total_rows = 0
        try:
            while page <= self.external_cash_flow_max_pages:
                rows = query(
                    start_time=start_time_ms,
                    end_time=end_time_ms,
                    page=page,
                    limit=limit,
                )
                if rows is None:
                    self.mark_external_cash_flow_truth_unavailable(
                        "income_history_request_failed"
                    )
                    return False
                if not isinstance(rows, list):
                    raise ValueError("income_history_not_a_list")
                total_rows += len(rows)
                self._apply_external_cash_flow_rows(rows, source=source)
                last_page_full = len(rows) >= limit
                if not last_page_full:
                    break
                page += 1

            if last_page_full and page > self.external_cash_flow_max_pages:
                self.mark_external_cash_flow_truth_unavailable(
                    "income_history_page_limit_exceeded"
                )
                return False

            with self.lock:
                self.audit_logger.audit(
                    "cash_flow_scan_completed",
                    {
                        "start_time_ms": start_time_ms,
                        "end_time_ms": end_time_ms,
                        "rows": total_rows,
                        "source": source,
                    },
                )
                self.external_cash_flow_scan_end_ms = end_time_ms
                self.account.sync_external_cash_flow_truth(
                    self.account.external_cash_flow_total,
                    snapshot_time=time.time(),
                    snapshot_monotonic=time.perf_counter(),
                )
            self._audit(
                "external_cash_flow_truth_synced",
                rows=total_rows,
                total=self.account.external_cash_flow_total,
                start_time_ms=start_time_ms,
                end_time_ms=end_time_ms,
                source=source,
            )
            return True
        except (JournalError, ValueError, TypeError) as exc:
            self.mark_external_cash_flow_truth_unavailable(str(exc))
            fail_closed = getattr(self, "_fail_closed_on_journal_error", None)
            if isinstance(exc, JournalError) and callable(fail_closed):
                fail_closed(exc, "external_cash_flow_history")
            return False

    def poll_external_cash_flow_truth(self, query=None, now: float = None) -> bool:
        if not self.external_cash_flow_truth_enabled:
            return True
        now = time.perf_counter() if now is None else float(now)
        if (
            now - self.last_external_cash_flow_poll_at
            < self.external_cash_flow_poll_interval_sec
        ):
            return bool(self.account.cash_flow_snapshot_synced)
        self.last_external_cash_flow_poll_at = now
        return self.backfill_external_cash_flow_history(
            query=query,
            end_time_ms=int(time.time() * 1000),
            source="live_loop_income_history",
        )

    def _refresh_missing_local_order_terminals(self, remote_orders) -> bool:
        query = getattr(self.gateway, "get_order", None)
        if not callable(query):
            return True
        remote_identifiers = {
            identifier
            for item in self._normalize_remote_open_orders(remote_orders)
            for identifier in item["identifiers"]
        }
        with self.lock:
            missing = [
                (order.client_oid, order.intent.symbol, order.exchange_oid or order.client_oid)
                for order in self.orders.values()
                if order.is_active()
                and order.client_oid not in remote_identifiers
                and (not order.exchange_oid or order.exchange_oid not in remote_identifiers)
            ]

        for client_oid, symbol, target_id in missing:
            remote = self.query_order(symbol, target_id)
            if remote is None:
                return False
            if remote.get("_query_status") == "NOT_FOUND":
                self._audit(
                    "active_order_missing_from_exchange_history",
                    client_oid=client_oid,
                    symbol=symbol,
                )
                continue
            if not self._apply_exchange_order_snapshot(
                remote,
                source="reconcile_missing_terminal",
            ):
                return False
        return True

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

    def freeze_system(self, reason: str, cancel_active_orders: bool = False):
        with self.lock:
            if self.state == LifecycleState.HALTED:
                self._close_outbound_gate_locked(reason)
                return

            previous_state = self.state
            self._lifecycle_generation += 1
            self.state = LifecycleState.FROZEN
            self._sync_capability_mode(reason)
            self.last_freeze_reason = reason

        if previous_state != LifecycleState.FROZEN:
            logger.error(f"OMS FROZEN: {reason}")
            self._audit(
                "lifecycle",
                state=self.state.value,
                reason=reason,
                previous_state=previous_state.value,
            )
        else:
            logger.error(f"OMS still FROZEN: {reason}")
            self._audit("freeze_reasserted", reason=reason)

        self._wait_for_outbound_risk_sends(f"system_freeze:{reason}")
        if not cancel_active_orders:
            return

        self._audit(
            "freeze_cancel_all_requested",
            reason=reason,
            symbols=self._account_cancel_symbols(),
        )
        try:
            for symbol in self._account_cancel_symbols():
                self._cancel_all_orders_unchecked(
                    symbol,
                    source="system_freeze",
                )
        except Exception:
            pass

    def halt_system(self, reason: str):
        emit_halt_event = False
        with self.lock:
            self._lifecycle_generation += 1
            if self.state == LifecycleState.HALTED:
                self.last_halt_reason = reason
                self.manual_rearm_required = True
                self._sync_capability_mode(reason)
                self._audit("halt_reasserted", reason=reason)
            else:
                self.state = LifecycleState.HALTED
                self._sync_capability_mode(reason)
                self.manual_rearm_required = True
                self.last_halt_reason = reason
                self.last_freeze_reason = ""
                logger.critical(f"OMS HALTED: {reason}")
                self._audit(
                    "lifecycle",
                    state=self.state.value,
                    reason=reason,
                    manual_rearm_required=True,
                )
                emit_halt_event = True
        if emit_halt_event:
            try:
                self.event_engine.put(
                    Event(EVENT_SYSTEM_HEALTH, f"HALT:{reason}")
                )
            except Exception as event_exc:
                logger.critical(
                    "[OMS] Failed to publish HALT event: "
                    f"{type(event_exc).__name__}:{event_exc}"
                )
        self._wait_for_outbound_risk_sends(f"system_halt:{reason}")
        try:
            for symbol in self._account_cancel_symbols():
                self._cancel_all_orders_unchecked(
                    symbol,
                    source="system_halt",
                )
        except Exception:
            pass

    def rearm_system(self, reason: str = "manual"):
        with self.lock:
            if self.state != LifecycleState.HALTED or not self.manual_rearm_required:
                self._audit("rearm_ignored", reason=reason)
                return False

            logger.warning(f"OMS manual rearm requested: {reason}")
            self._audit(
                "rearm_requested",
                reason=reason,
                halted_reason=self.last_halt_reason,
            )
            self.state = LifecycleState.RECONCILING
            self._lifecycle_generation += 1
            self._sync_capability_mode(f"manual_rearm:{reason}")
            self._audit(
                "lifecycle",
                state=self.state.value,
                reason=f"manual_rearm:{reason}",
            )
        self._perform_full_reset()
        with self.lock:
            if self.state == LifecycleState.LIVE:
                self.manual_rearm_required = False
                self.last_halt_reason = ""
                self._audit(
                    "rearm_completed",
                    state=self.state.value,
                    reason=reason,
                )
                return True

            self.manual_rearm_required = True
            return False

    def stop(self, clean_shutdown: bool = False, reason: str = ""):
        with self.lock:
            self._stopped = True
            self._outbound_all_order_seal_reason = reason or "oms_stop"
            self._close_outbound_gate_locked("oms_stop", hold="stopped")
        drained = self._wait_for_outbound_order_sends("oms_stop")
        background_tasks_stopped = self._background_tasks.shutdown(
            timeout=self.outbound_gate_drain_timeout_sec
        )
        if not background_tasks_stopped:
            logger.critical(
                "[OMS] Bounded background executor did not stop cleanly"
            )
        with self.lock:
            self.reconcile_retry_scheduled = False
            self._submit_settlement_inflight_oids.clear()
            self._submit_cancel_requested_oids.clear()
            self._deferred_cancel_oids.clear()
            self._deferred_cancel_all_symbols.clear()
            self.trade_tail_verification_inflight.clear()
            self._order_truth_resolution_inflight.clear()
        clean_shutdown = bool(
            clean_shutdown
            and drained
            and background_tasks_stopped
            and self._shutdown_requested
            and self._shutdown_cancel_verified
        )
        audit_ok = True
        try:
            if clean_shutdown:
                self._audit(
                    "oms_stopped",
                    state=self.state.value,
                    reason=reason or self._shutdown_reason,
                    cancel_verified=True,
                    manual_rearm_required=self.manual_rearm_required,
                    symbol_guard_count=len(self.symbol_guards),
                    venue_guard_count=len(self.venue_guards),
                    strategy_guard_count=len(self.strategy_guards),
                    strategy_symbol_guard_count=len(self.strategy_symbol_guards),
                )
            else:
                self._audit(
                    "shutdown_cancel_unverified",
                    state=self.state.value,
                    reason=reason or self._shutdown_reason or "oms_stop_without_verification",
                    drain_completed=drained,
                    cancel_verified=self._shutdown_cancel_verified,
                )
        except Exception as exc:
            audit_ok = False
            logger.critical(
                "[OMS] Shutdown audit could not be persisted: "
                f"{type(exc).__name__}:{exc}"
            )

        with self.lock:
            self._refresh_outbound_gate_locked("oms_stopped")

        order_monitor_stopped = True
        try:
            monitor_result = self.order_monitor.stop()
            order_monitor_stopped = monitor_result is not False
        except Exception as exc:
            order_monitor_stopped = False
            logger.critical(
                "[OMS] Order monitor did not stop cleanly: "
                f"{type(exc).__name__}:{exc}"
            )

        fence_released = True
        if (
            self.single_writer_fence is not None
            and getattr(self.single_writer_fence, "handle", None) is not None
        ):
            try:
                release_result = self.single_writer_fence.release()
                fence_released = release_result is not False
            except Exception as exc:
                fence_released = False
                logger.critical(
                    "[OMS] Single-writer fence release failed: "
                    f"{type(exc).__name__}:{exc}"
                )

        stopped = bool(
            background_tasks_stopped
            and order_monitor_stopped
            and fence_released
        )
        return {
            "stopped": stopped,
            "drained": bool(drained),
            "background_tasks_stopped": bool(background_tasks_stopped),
            "clean": bool(clean_shutdown and audit_ok and stopped),
        }

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

    def cancel_order(self, client_oid: str):
        if not self.can_cancel_orders():
            self._audit(
                "cancel_rejected",
                client_oid=client_oid,
                reason=self._get_capability_block_reason("cancel"),
            )
            return False

        command_id = f"CANCEL:{client_oid}:{uuid.uuid4().hex}"
        message_reservation = None
        try:
            with self.lock:
                order = self.orders.get(client_oid)
                if not order or not order.is_active():
                    return False
                if client_oid in self._submit_settlement_inflight_oids:
                    if client_oid in self._submit_cancel_requested_oids:
                        return True
                    self._submit_cancel_requested_oids.add(client_oid)
                    self._audit(
                        "cancel_queued_until_submit_settled",
                        client_oid=client_oid,
                        symbol=order.intent.symbol,
                    )
                    return True
                if client_oid in self._submit_cancel_requested_oids:
                    return True
                if order.status == OrderStatus.CANCELLING:
                    return True
                (
                    message_reservation,
                    budget_rejection,
                ) = self._outbound_budget.reserve_token(
                    self.OUTBOUND_CANCEL,
                )
                if budget_rejection:
                    scheduled = self._schedule_cancel_order_retry(client_oid)
                    deferred = scheduled or client_oid in self._deferred_cancel_oids
                    self._audit(
                        "cancel_message_budget_deferred",
                        client_oid=client_oid,
                        symbol=order.intent.symbol,
                        reason=budget_rejection,
                        scheduled=scheduled,
                    )
                    return deferred
                target_id = order.exchange_oid if order.exchange_oid else client_oid
                try:
                    order.mark_cancelling()
                except ValueError:
                    self._outbound_budget.rollback(message_reservation)
                    return order.status == OrderStatus.CANCELLING
                self._record_order_snapshot(order, "cancel_requested")
                self._emit_order_update(order)
                request = CancelRequest(order.intent.symbol, target_id)
            self._record_command_prepared(command_id, "CANCEL", order, request)
        except JournalError as exc:
            self._outbound_budget.rollback(message_reservation)
            self._fail_closed_on_journal_error(
                exc,
                "prepare_cancel",
                order.intent.symbol if order else "",
            )
            return False

        try:
            response = self.gateway.cancel_order(request)
        except Exception as exc:
            logger.error(f"[OMS] Cancel command raised for {client_oid}: {exc}")
            response = None
        error_code = ""
        error_message = ""
        if response is not None and getattr(response, "status_code", 0) != 200:
            try:
                payload = response.json()
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                raw_code = payload.get("code")
                error_code = "" if raw_code is None else str(raw_code)
                error_message = str(payload.get("msg", "") or "")

        command_outcome = (
            CommandOutcome.ACKNOWLEDGED
            if response is not None and getattr(response, "status_code", 0) == 200
            else CommandOutcome.UNKNOWN
        )
        try:
            self._record_command_result(
                command_id,
                "CANCEL",
                order,
                command_outcome,
                exchange_oid=order.exchange_oid,
                error_code=error_code,
                error_message=error_message,
            )
        except JournalError as exc:
            self._latch_journal_failure(
                exc,
                "result_cancel",
                request.symbol,
            )
            with self.lock:
                current = self.orders.get(client_oid)
                if current and current.is_active():
                    try:
                        current.mark_cancel_unknown("result_not_durable")
                    except ValueError:
                        pass
            if current is not None:
                self._notify_order_state_safely(
                    current,
                    "cancel_result_journal_failure",
                )
            try:
                self._fail_closed_on_journal_error(
                    exc,
                    "result_cancel",
                    request.symbol,
                )
            except BaseException as fail_closed_exc:
                logger.critical(
                    "[OMS] Cancel result failure could not complete "
                    f"fail-closed: {type(fail_closed_exc).__name__}:"
                    f"{fail_closed_exc}"
                )
            try:
                self._on_order_truth_check(
                    "Cancel result could not be persisted",
                    suspicious_oid=client_oid,
                )
            except BaseException as truth_exc:
                logger.critical(
                    "[OMS] Cancel result failure could not start truth "
                    f"resolution: {type(truth_exc).__name__}:"
                    f"{truth_exc}"
                )
            return True

        if response is not None and getattr(response, "status_code", 0) == 200:
            try:
                payload = response.json()
            except Exception:
                payload = {}
            if isinstance(payload, dict) and payload.get("status"):
                try:
                    snapshot_applied = self._apply_exchange_order_snapshot(
                        payload,
                        source="cancel_rest_ack",
                    )
                except JournalError as exc:
                    self._fail_closed_on_journal_error(
                        exc,
                        "snapshot_cancel_ack",
                        request.symbol,
                    )
                    return True
                if not snapshot_applied:
                    self.trigger_reconcile(
                        "Cancel acknowledgement is ahead of exact trade history",
                        suspicious_oid=client_oid,
                    )
            self._audit(
                "cancel_acknowledged",
                client_oid=client_oid,
                target_id=target_id,
                symbol=request.symbol,
            )
            return True

        try:
            with self.lock:
                order = self.orders.get(client_oid)
                if order and order.is_active():
                    try:
                        order.mark_cancel_unknown(error_message or error_code or "cancel_outcome_unknown")
                    except ValueError:
                        order.note_exchange_update(
                            exchange_status="CANCEL_UNKNOWN",
                            update_time=time.time(),
                        )
                    self._record_order_snapshot(
                        order,
                        "cancel_unknown",
                        exchange_error_code=error_code,
                        exchange_error_message=error_message,
                    )
                    self._emit_order_update(order)
                    self.order_monitor.on_order_update(order.client_oid, order.status)
        except JournalError as exc:
            self._fail_closed_on_journal_error(exc, "snapshot_cancel_unknown", request.symbol)
            self._on_order_truth_check(
                "Cancel result could not be persisted",
                suspicious_oid=client_oid,
            )
            return True

        self.freeze_symbol(
            request.symbol,
            f"order_truth:cancel_unknown:{client_oid}",
            cancel_active_orders=False,
        )
        self._audit(
            "cancel_outcome_unknown",
            client_oid=client_oid,
            target_id=target_id,
            symbol=request.symbol,
            error_code=error_code,
            error_message=error_message,
        )
        self._on_order_truth_check("Order cancel outcome unknown", suspicious_oid=client_oid)
        return True

    def _cancel_all_orders_unchecked(
        self,
        symbol: str,
        source: str,
        audit: bool = True,
        bypass_message_budget: bool = False,
    ) -> bool:
        budget_rejection = ""
        message_reservation = None
        if not bypass_message_budget:
            (
                message_reservation,
                budget_rejection,
            ) = self._outbound_budget.reserve_token(
                self.OUTBOUND_CANCEL,
            )
        if budget_rejection:
            logger.error(
                f"[OMS] Mass cancel blocked for {symbol}: {budget_rejection}"
            )
            scheduled = self._schedule_cancel_all_retry(symbol, source)
            with self.lock:
                deferred = (
                    scheduled or symbol.upper() in self._deferred_cancel_all_symbols
                )
            if audit:
                self._audit(
                    "cancel_all_message_budget_deferred",
                    symbol=symbol,
                    source=source,
                    reason=budget_rejection,
                    scheduled=scheduled,
                )
            return deferred

        if audit:
            try:
                self._audit(
                    "cancel_all_submitted",
                    symbol=symbol,
                    source=source,
                )
            except Exception:
                self._outbound_budget.rollback(message_reservation)
                raise
        try:
            response = self.gateway.cancel_all_orders(symbol)
        except Exception as exc:
            logger.error(f"[OMS] Mass cancel failed for {symbol}: {exc}")
            self._audit(
                "cancel_all_outcome_unknown",
                symbol=symbol,
                source=source,
                error=f"{type(exc).__name__}:{exc}",
            )
            self.freeze_symbol(
                symbol,
                f"order_truth:cancel_all_unknown:{source}",
                cancel_active_orders=False,
            )
            self._schedule_cancel_all_retry(symbol, source)
            self._on_order_truth_check("Mass cancel outcome unknown")
            return False

        status_code = getattr(response, "status_code", None)
        if response is True or status_code == 200:
            if audit:
                self._audit(
                    "cancel_all_acknowledged",
                    symbol=symbol,
                    source=source,
                )
            return True

        error_code = ""
        error_message = ""
        try:
            payload = response.json() if response is not None else {}
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            error_code = str(payload.get("code", "") or "")
            error_message = str(payload.get("msg", "") or "")
        logger.error(
            f"[OMS] Mass cancel outcome unknown for {symbol}: "
            f"status={status_code} code={error_code} msg={error_message}"
        )
        self._audit(
            "cancel_all_outcome_unknown",
            symbol=symbol,
            source=source,
            status_code=status_code,
            error_code=error_code,
            error_message=error_message,
        )
        self.freeze_symbol(
            symbol,
            f"order_truth:cancel_all_unknown:{source}",
            cancel_active_orders=False,
        )
        self._schedule_cancel_all_retry(symbol, source)
        self._on_order_truth_check("Mass cancel outcome unknown")
        return False

    def _account_cancel_symbols(self, remote_orders=None) -> list[str]:
        normalized_remote = (
            self._normalize_remote_open_orders(remote_orders)
            if remote_orders is not None
            else []
        )
        with self.lock:
            symbols = set(self._known_account_order_symbols)
            symbols.update(
                str(symbol or "").upper()
                for symbol in self.config.get("symbols", [])
                if str(symbol or "").strip()
            )
            symbols.update(
                str(order.intent.symbol or "").upper()
                for order in self.orders.values()
                if order.is_active() and str(order.intent.symbol or "").strip()
            )
            symbols.update(
                str(symbol or "").upper()
                for symbol in self.symbol_guards
                if str(symbol or "").strip()
            )
            symbols.update(
                item["symbol"] for item in normalized_remote if item.get("symbol")
            )
            self._known_account_order_symbols.update(symbols)
        return sorted(symbols)

    def cancel_all_account_orders_verified(
        self,
        snapshot_provider=None,
        *,
        source: str = "shutdown",
        timeout_sec: float | None = None,
        required_empty_snapshots: int | None = None,
        settle_interval_sec: float | None = None,
    ) -> bool:
        """Cancel every discovered symbol and prove two consecutive empty snapshots."""
        provider = snapshot_provider or self.gateway
        query = getattr(provider, "get_open_orders", None)
        if not callable(query):
            self._audit(
                "account_cancel_verification_failed",
                source=source,
                reason="open_orders_query_unavailable",
            )
            return False

        timeout_sec = max(
            1.0,
            float(
                self.shutdown_cancel_timeout_sec
                if timeout_sec is None
                else timeout_sec
            ),
        )
        required_empty_snapshots = max(
            2,
            int(
                self.shutdown_empty_snapshots_required
                if required_empty_snapshots is None
                else required_empty_snapshots
            ),
        )
        settle_interval_sec = max(
            0.01,
            float(
                self.shutdown_cancel_settle_interval_sec
                if settle_interval_sec is None
                else settle_interval_sec
            ),
        )
        deadline = time.perf_counter() + timeout_sec
        empty_snapshots = 0
        cancel_sweep_completed = False
        last_error = ""

        if not self._wait_for_outbound_order_sends(f"{source}:pre_cancel"):
            last_error = "outbound_order_sends_not_drained"
        else:
            while time.perf_counter() < deadline:
                try:
                    remote_orders = query()
                    normalized_remote = self._normalize_remote_open_orders(
                        remote_orders
                    )
                except Exception as exc:
                    remote_orders = None
                    normalized_remote = []
                    last_error = f"open_orders_query:{type(exc).__name__}:{exc}"
                    empty_snapshots = 0

                if remote_orders is not None:
                    cancel_targets = self._account_cancel_symbols(remote_orders)
                    if normalized_remote or not cancel_sweep_completed:
                        for symbol in cancel_targets:
                            acknowledged = self._cancel_all_orders_unchecked(
                                symbol,
                                source=f"{source}:account_sweep",
                                bypass_message_budget=True,
                            )
                            if not acknowledged:
                                last_error = f"cancel_unverified:{symbol}"
                        cancel_sweep_completed = True
                        empty_snapshots = 0
                    elif self.get_outbound_gate_snapshot()[
                        "order_sends_inflight"
                    ] == 0:
                        empty_snapshots += 1
                        if empty_snapshots >= required_empty_snapshots:
                            with self.lock:
                                if self._shutdown_requested:
                                    self._shutdown_cancel_verified = True
                            self._audit(
                                "account_cancel_verified",
                                source=source,
                                empty_snapshots=empty_snapshots,
                                symbols=cancel_targets,
                            )
                            return True
                    else:
                        last_error = "outbound_order_send_reappeared"
                        empty_snapshots = 0

                time.sleep(settle_interval_sec)

        self._audit(
            "account_cancel_verification_failed",
            source=source,
            reason=last_error or "verification_timeout",
            empty_snapshots=empty_snapshots,
            timeout_sec=timeout_sec,
        )
        return False

    def cancel_all_orders(self, symbol: str):
        if not self.can_cancel_orders():
            self._audit(
                "cancel_all_rejected",
                symbol=symbol,
                reason=self._get_capability_block_reason("cancel"),
            )
            return False
        return self._cancel_all_orders_unchecked(
            symbol,
            source="public_cancel_all",
        )

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
