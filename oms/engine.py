import hashlib
import math
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone

from data.cache import data_cache
from data.ref_data import ref_data_manager
from infrastructure.logger import logger
from infrastructure.single_writer_fence import SingleWriterFence
from infrastructure.time_service import time_service

from event.type import (
    CancelRequest,
    CommandOutcome,
    ExecutionPolicy,
    Event,
    ExchangeAccountUpdate,
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
    TradeData,
    EVENT_ORDER_SUBMITTED,
    EVENT_EXCHANGE_ORDER_UPDATE,
    EVENT_ORDER_UPDATE,
    EVENT_POSITION_UPDATE,
    EVENT_SYSTEM_HEALTH,
    EVENT_TRADE_UPDATE,
    TIF_GTC,
    TIF_GTX,
    TIF_IOC,
    TIF_RPI,
)

from .account_manager import AccountManager
from .exposure import ExposureManager
from .journal import JournalCorruptionError, JournalError, OMSJournal
from .order import Order
from .order_manager import OrderManager
from .validator import OrderValidator


class OMS:
    """Deterministic OMS for a single Binance perpetual account."""

    SUPPORTED_POSITION_MODES = frozenset({"ONE_WAY"})
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

        self.event_log = []
        self.orders = {}
        self.exchange_id_map = {}
        self.symbol_guards = {}
        self.venue_guards = {}
        self.strategy_guards = {}
        self.strategy_symbol_guards = {}
        self.lock = threading.RLock()
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

        target_leverage = int(config.get("account", {}).get("leverage", 0) or 0)
        if target_leverage > 0:
            self.gateway.target_leverage = target_leverage
        target_margin_type = str(
            config.get("account", {}).get("margin_type", "CROSSED") or "CROSSED"
        ).upper()
        self.gateway.target_margin_type = target_margin_type
        self.gateway.target_position_mode = target_position_mode

        self.max_pos_notional = (
            config.get("risk", {})
            .get("limits", {})
            .get("max_pos_notional", 2000.0)
        )
        self.max_account_gross_notional = (
            config.get("risk", {})
            .get("limits", {})
            .get("max_account_gross_notional", 0.0)
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
        self.TOMBSTONE_MAX = config.get("oms", {}).get("tombstone_max", 2000)
        self.terminated_oids = set()
        self.terminated_oid_queue = deque()
        self.reconcile_retry_scheduled = False
        self._order_truth_resolution_inflight = set()
        self._unknown_not_found_counts = {}
        self.trade_cursors = {}
        self.trade_scan_end_ms = {}
        self.execution_ids = set()
        self.manual_rearm_required = False
        self.last_freeze_reason = ""
        self.last_halt_reason = ""
        self.recovered_guard_cleanup_pending = False
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
        self.outbound_message_budget_enabled = bool(
            outbound_budget.get("enabled", False)
        )
        self.outbound_message_window_sec = max(
            0.05,
            float(outbound_budget.get("window_sec", 1.0) or 1.0),
        )
        self.max_total_messages_per_window = max(
            0,
            int(outbound_budget.get("max_total_messages_per_window", 20) or 0),
        )
        self.max_new_orders_per_window = max(
            0,
            int(outbound_budget.get("max_new_orders_per_window", 10) or 0),
        )
        self.max_reduce_orders_per_window = max(
            0,
            int(outbound_budget.get("max_reduce_orders_per_window", 10) or 0),
        )
        self.max_cancel_messages_per_window = max(
            0,
            int(outbound_budget.get("max_cancel_messages_per_window", 20) or 0),
        )
        configured_reserved = max(
            0,
            int(outbound_budget.get("reserved_risk_messages_per_window", 5) or 0),
        )
        self.reserved_risk_messages_per_window = (
            min(configured_reserved, self.max_total_messages_per_window)
            if self.max_total_messages_per_window > 0
            else configured_reserved
        )
        self.outbound_message_history = deque()
        self._deferred_cancel_oids = set()
        self._deferred_cancel_all_symbols = set()
        self._stopped = False
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
        self.symbol_guards = dict(summary.get("symbol_guards", {}))
        self.venue_guards = dict(summary.get("venue_guards", {}))
        self.strategy_guards = dict(summary.get("strategy_guards", {}))
        self.strategy_symbol_guards = {
            tuple(key.split("|", 1)): value
            for key, value in summary.get("strategy_symbol_guards", {}).items()
            if "|" in key
        }
        self.mode_constraints = {}
        for constraint_key, payload in (summary.get("mode_constraints", {}) or {}).items():
            mode_value = str((payload or {}).get("mode", "") or "")
            reason = str((payload or {}).get("reason", "") or "")
            if not mode_value or not reason:
                continue
            try:
                mode = OMSCapabilityMode(mode_value)
            except ValueError:
                continue
            self.mode_constraints[str(constraint_key)] = (mode, reason)

        override_mode = str(summary.get("mode_override", "") or "")
        override_reason = str(summary.get("mode_override_reason", "") or "")
        if not self.mode_constraints and override_mode and override_reason:
            try:
                legacy_mode = OMSCapabilityMode(override_mode)
            except ValueError:
                legacy_mode = None
            if legacy_mode is not None:
                self.mode_constraints[
                    self._mode_constraint_key(override_reason)
                ] = (legacy_mode, override_reason)
        self._refresh_selected_mode_constraint()

        self.last_freeze_reason = str(summary.get("last_freeze_reason", "") or "")
        self.last_halt_reason = str(summary.get("last_halt_reason", "") or "")
        self.manual_rearm_required = bool(summary.get("manual_rearm_required", False))

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

    def _sync_capability_mode(self, reason: str = ""):
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
        previous_mode = self.mode_override.value if self.mode_override else ""
        previous_reason = self.mode_override_reason
        constraint_key = self._mode_constraint_key(reason)
        if self.mode_constraints.get(constraint_key) == (mode, reason):
            return self.mode_override == mode and self.mode_override_reason == reason

        self.mode_constraints[constraint_key] = (mode, reason)
        self._refresh_selected_mode_constraint()
        self._sync_capability_mode(self.mode_override_reason or reason)
        self._audit(
            "trading_mode_override_set",
            mode=mode.value,
            reason=reason,
            constraint_key=constraint_key,
            selected=(self.mode_override == mode and self.mode_override_reason == reason),
            previous_mode=previous_mode,
            previous_reason=previous_reason,
        )
        if self.mode_override == OMSCapabilityMode.REDUCE_ONLY:
            self._cancel_orders_matching(lambda order: not order.intent.reduce_only)
        return self.mode_override == mode and self.mode_override_reason == reason

    def clear_trading_mode(self, reason: str = "", prefixes=()):
        if not self.mode_constraints:
            return False

        matching_keys = [
            key
            for key, (_mode, constraint_reason) in self.mode_constraints.items()
            if not prefixes or any(constraint_reason.startswith(prefix) for prefix in prefixes)
        ]
        if not matching_keys:
            return False

        previous_mode = self.mode_override.value if self.mode_override else ""
        previous_reason = self.mode_override_reason
        for key in matching_keys:
            self.mode_constraints.pop(key, None)
        self._refresh_selected_mode_constraint()
        self._sync_capability_mode(
            self.mode_override_reason or reason or "trading_mode_cleared"
        )
        self._audit(
            "trading_mode_override_cleared",
            reason=reason or previous_reason,
            cleared_constraint_keys=matching_keys,
            previous_mode=previous_mode,
            previous_reason=previous_reason,
        )
        return True

    def has_trading_mode_constraint(self, prefixes=()) -> bool:
        if not prefixes:
            return bool(self.mode_constraints)
        return any(
            any(constraint_reason.startswith(prefix) for prefix in prefixes)
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
            self.last_risk_control_heartbeat_monotonic = time.monotonic()
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
            max(0.0, time.monotonic() - monotonic_timestamp)
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

        now = time.monotonic() if now is None else float(now)
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
            now = time.monotonic()
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
            }

    def renew_venue_dead_man_switch(self, force: bool = False) -> bool:
        if not self.venue_dead_man_switch_enabled:
            return True

        now = time.monotonic()
        with self.lock:
            since_attempt = now - self.last_venue_dead_man_attempt_monotonic
            if (
                not force
                and self.last_venue_dead_man_attempt_monotonic > 0.0
                and since_attempt < self.venue_dead_man_switch_renewal_interval_sec
            ):
                healthy, _reason = self._venue_dead_man_switch_health_locked(now)
                return healthy
            self.last_venue_dead_man_attempt_monotonic = now

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
            self.set_trading_mode(
                OMSCapabilityMode.REDUCE_ONLY,
                f"venue_dead_man_switch:{failure_reason}",
            )
            self._audit(
                "venue_dead_man_switch_renewal_failed",
                reason=failure_reason,
                renewed_symbols=sorted(renewed_symbols),
                required_symbols=sorted(self.venue_dead_man_switch_symbols),
            )
            return False

        had_constraint = self.has_trading_mode_constraint(
            ("venue_dead_man_switch:",)
        )
        with self.lock:
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
            )
            with self.lock:
                self.venue_dead_man_recovery_count = 0

        if first_success or recovered_from_error or cleared:
            self._audit(
                "venue_dead_man_switch_renewed",
                armed_symbols=sorted(renewed_symbols),
                recovered=bool(cleared),
                recovery_count=recovery_count,
            )
        return True

    def _ensure_venue_dead_man_switch_armed(self, context: str) -> bool:
        if self.renew_venue_dead_man_switch(force=True):
            return True
        reason = f"venue_dead_man_switch_unavailable:{context}"
        logger.critical(f"[OMS] {reason}")
        self.halt_system(reason)
        return False

    def get_capability_snapshot(self) -> dict:
        return {
            "mode": self.capability_mode.value,
            "reason": self.capability_reason,
            "override_mode": self.mode_override.value if self.mode_override else "",
            "override_reason": self.mode_override_reason,
            "mode_constraints": {
                key: {"mode": mode.value, "reason": reason}
                for key, (mode, reason) in self.mode_constraints.items()
            },
            "can_query": self.can_query_exchange(),
            "can_cancel": self.can_cancel_orders(),
            "can_open_risk": self.can_open_new_risk(),
            "risk_control_heartbeat": self.get_risk_control_heartbeat_snapshot(),
            "venue_dead_man_switch": self.get_venue_dead_man_switch_snapshot(),
            "outbound_message_budget": self.get_outbound_message_budget_snapshot(),
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

    def sync_account_margin_health(self, account: dict, snapshot_time: float = None) -> bool:
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
        with self.lock:
            synced = self.account.sync_margin_health(
                maintenance_margin,
                margin_balance,
                snapshot_time=snapshot_time,
            )
        if synced:
            self._audit(
                "account_margin_health_synced",
                maintenance_margin=maintenance_margin,
                margin_balance=margin_balance,
                ratio=self.account.maintenance_margin_ratio,
                snapshot_time=snapshot_time,
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

    def _record_command_prepared(
        self,
        command_id: str,
        command_type: str,
        order: Order,
        request,
    ):
        if isinstance(request, OrderRequest):
            request_payload = {
                "symbol": request.symbol,
                "price": request.price,
                "volume": request.volume,
                "side": request.side,
                "order_type": request.order_type,
                "time_in_force": request.time_in_force,
                "post_only": request.post_only,
                "reduce_only": request.reduce_only,
                "self_trade_prevention_mode": request.self_trade_prevention_mode,
            }
        else:
            request_payload = {
                "symbol": request.symbol,
                "order_id": request.order_id,
            }
        return self.journal.append(
            "command_prepared",
            {
                "command_id": command_id,
                "command_type": command_type,
                "idempotency_key": order.client_oid,
                "client_oid": order.client_oid,
                "exchange_oid": order.exchange_oid,
                "order": order.to_record(),
                "request": request_payload,
            },
        )

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
        return self.journal.append(
            "command_result",
            {
                "command_id": command_id,
                "command_type": command_type,
                "idempotency_key": order.client_oid,
                "client_oid": order.client_oid,
                "exchange_oid": exchange_oid or order.exchange_oid,
                "outcome": outcome.value,
                "error_code": error_code,
                "error_message": error_message,
            },
        )

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
    ) -> str:
        execution_id = self._execution_id(order, update)
        if execution_id in self.execution_ids:
            return execution_id

        self.journal.append(
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
                "pre_status": order.status.value,
            },
        )
        self.execution_ids.add(execution_id)
        return execution_id

    def _fail_closed_on_journal_error(
        self,
        exc: Exception,
        context: str,
        symbol: str = "",
    ):
        """Enter cancel-only mode without depending on the failed journal."""
        reason = f"durable_journal_unavailable:{context}:{exc}"
        logger.critical(f"[OMS] {reason}")
        with self.lock:
            self.state = LifecycleState.HALTED
            self.manual_rearm_required = True
            self.last_halt_reason = reason
            self.last_freeze_reason = ""
            self.capability_mode = OMSCapabilityMode.CANCEL_ONLY
            self.capability_reason = reason
            if symbol:
                self.symbol_guards[symbol.upper()] = reason

        self.event_engine.put(Event(EVENT_SYSTEM_HEALTH, f"HALT:{reason}"))
        try:
            for target_symbol in self.config.get("symbols", []):
                self._cancel_all_orders_unchecked(
                    target_symbol,
                    source="journal_failure",
                    audit=False,
                )
        except Exception as cancel_exc:
            logger.critical(
                f"[OMS] Failed to cancel orders after journal failure: {cancel_exc}"
            )

    def _on_order_truth_check(self, reason: str, suspicious_oid: str = None):
        if not suspicious_oid:
            return
        with self.lock:
            if suspicious_oid in self._order_truth_resolution_inflight:
                return
            self._order_truth_resolution_inflight.add(suspicious_oid)

        threading.Thread(
            target=self._resolve_order_truth,
            args=(suspicious_oid, reason),
            daemon=True,
        ).start()

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
                    elapsed = time.time() - order.updated_at if order else 0.0
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
                    self._clear_order_truth_guard(symbol)
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
            self._backfill_trade_history(
                symbols={symbol},
                end_time_ms=time_service.now(),
            )
            self._apply_exchange_order_snapshot(remote, source="targeted_order_query")

            with self.lock:
                order = self.orders.get(client_oid)
                resolved_status = order.status if order else None

            if (
                local_status in {OrderStatus.CANCEL_UNKNOWN, OrderStatus.CANCELLING}
                and resolved_status in {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}
            ):
                self.cancel_order(client_oid)
            elif resolved_status not in {OrderStatus.SUBMIT_UNKNOWN, OrderStatus.CANCEL_UNKNOWN}:
                self._clear_order_truth_guard(symbol)
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

    def _clear_order_truth_guard(self, symbol: str):
        reason = self.get_symbol_freeze_reason(symbol)
        if reason.startswith("order_truth:"):
            self.clear_symbol_freeze(symbol, reason="order truth resolved")

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
            self._audit("unhandled_order_snapshot_status", status=status, source=source)
            return False

        executed_qty = float(remote.get("executedQty", 0.0) or 0.0)
        avg_price = float(remote.get("avgPrice", 0.0) or remote.get("price", 0.0) or 0.0)
        update_time_ms = remote.get("updateTime") or remote.get("time") or time_service.now()
        remote_order_type = str(remote.get("type", "") or "").upper()
        remote_time_in_force = str(remote.get("timeInForce", "") or "").upper()
        if (
            status in {"CANCELED", "EXPIRED"}
            and executed_qty > order.filled_volume + 1e-9
        ):
            recovery_price = avg_price or order.avg_price or order.intent.price
            self._audit(
                "order_snapshot_fill_inferred",
                client_oid=order.client_oid,
                exchange_oid=exchange_oid,
                local_filled=order.filled_volume,
                exchange_filled=executed_qty,
                source=source,
            )
            fill_update = ExchangeOrderUpdate(
                client_oid=order.client_oid,
                exchange_oid=exchange_oid or order.exchange_oid,
                symbol=order.intent.symbol,
                status="PARTIALLY_FILLED",
                filled_qty=executed_qty - order.filled_volume,
                filled_price=recovery_price,
                cum_filled_qty=executed_qty,
                update_time=float(update_time_ms) / 1000.0,
                seq=0,
                order_type=remote_order_type,
                time_in_force=remote_time_in_force,
            )
            self._apply_event(Event(EVENT_EXCHANGE_ORDER_UPDATE, fill_update))

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
        current = int(self.trade_cursors.get(symbol, -1))
        if trade_id <= current:
            return False
        self.trade_cursors[symbol] = trade_id
        self.journal.append(
            "trade_cursor_advanced",
            {
                "symbol": symbol,
                "trade_id": trade_id,
                "trade_time": trade_time,
                "source": source,
            },
        )
        return True

    def _apply_exchange_trade(self, trade: dict) -> bool:
        symbol = str(trade.get("symbol", "") or "").upper()
        trade_id = int(trade.get("id", -1))
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
            page_count = 0
            while page_count < 20:
                page_count += 1
                if cursor >= 0:
                    trades = self.query_user_trades(symbol, from_id=cursor + 1, limit=limit)
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
                trades = sorted(
                    (trade for trade in trades if isinstance(trade, dict)),
                    key=lambda trade: (int(trade.get("time", 0) or 0), int(trade.get("id", -1))),
                )
                for trade in trades:
                    if not self._apply_exchange_trade(trade):
                        return False
                    cursor = max(cursor, int(trade.get("id", -1)))
                if len(trades) < limit:
                    break
            if page_count >= 20 and len(trades) >= limit:
                return False
            self.trade_scan_end_ms[symbol] = end_time_ms
            self.journal.append(
                "trade_scan_completed",
                {
                    "symbol": symbol,
                    "end_time_ms": end_time_ms,
                    "cursor": int(self.trade_cursors.get(symbol, -1)),
                },
            )
        return True

    def _prime_trade_history_baseline(self, end_time_ms: int) -> bool:
        query = getattr(self.gateway, "get_user_trades", None)
        if not callable(query):
            return True
        start_time = max(0, int(end_time_ms) - self.trade_recovery_lookback_ms)
        for symbol in sorted(set(self.config.get("symbols", []))):
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
            self.journal.append(
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
                self.journal.append(
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
                self.journal.append(
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
        now = time.monotonic() if now is None else float(now)
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
            self._apply_exchange_order_snapshot(remote, source="reconcile_missing_terminal")
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
        order = Order(client_oid, intent)
        command_id = f"SUBMIT:{client_oid}"

        with self.lock:
            message_kind = (
                self.OUTBOUND_REDUCE_ORDER
                if request.reduce_only
                else self.OUTBOUND_NEW_ORDER
            )
            budget_rejection = self._reserve_outbound_message_locked(message_kind)
        if budget_rejection:
            logger.error(
                f"[OMS] Internal order blocked by message budget: {budget_rejection}"
            )
            self._audit(
                "internal_order_message_budget_rejected",
                client_oid=client_oid,
                symbol=intent.symbol,
                reduce_only=request.reduce_only,
                reason=budget_rejection,
            )
            return False

        try:
            with self.lock:
                self.orders[client_oid] = order
                order.mark_submitting()
                self.exposure.update_open_orders(self.orders)
                self.account.calculate()
                self._record_order_snapshot(order, snapshot_source, **audit_extra)
            self._record_command_prepared(
                command_id,
                "SUBMIT",
                order,
                request,
            )
        except JournalError as exc:
            with self.lock:
                order.mark_rejected_locally("durable_journal_unavailable")
                self.orders.pop(client_oid, None)
                self.exposure.update_open_orders(self.orders)
                self.account.calculate()
                self._emit_order_update(order)
            self._fail_closed_on_journal_error(exc, "prepare_internal_submit", intent.symbol)
            return False

        try:
            raw_result = self.gateway.send_order(request, client_oid)
            command = self._normalize_submit_command(raw_result)
        except Exception as exc:
            command = GatewayCommandResult(
                CommandOutcome.UNKNOWN,
                error_message=f"gateway_send_exception:{exc}",
            )

        try:
            self._record_command_result(
                command_id,
                "SUBMIT",
                order,
                command.outcome,
                exchange_oid=command.exchange_oid,
                error_code=command.error_code,
                error_message=command.error_message,
            )
        except JournalError as exc:
            with self.lock:
                if order.status == OrderStatus.SUBMITTING:
                    order.mark_submit_unknown("result_not_durable")
                self._emit_order_update(order)
            self._fail_closed_on_journal_error(exc, "result_internal_submit", intent.symbol)
            self._on_order_truth_check(
                "Submit result could not be persisted",
                suspicious_oid=client_oid,
            )
            return True

        if command.outcome == CommandOutcome.ACKNOWLEDGED:
            exchange_oid = command.exchange_oid
            try:
                with self.lock:
                    order.mark_pending_ack(exchange_oid)
                    self.exchange_id_map[exchange_oid] = order
                    self._record_order_snapshot(order, f"{snapshot_source}_ack", **audit_extra)
                    self._emit_order_update(order)
            except JournalError as exc:
                self._fail_closed_on_journal_error(exc, "snapshot_internal_submit_ack", intent.symbol)
                return True

            event_data = OrderSubmitted(request, client_oid, time.time())
            self.event_engine.put(Event(EVENT_ORDER_SUBMITTED, event_data))
            try:
                self._commit_gateway_submission(client_oid)
            except Exception as exc:
                logger.error(
                    f"[OMS] Gateway submit commit failed for {client_oid}: {exc}"
                )
                self.freeze_symbol(
                    intent.symbol,
                    f"order_truth:submit_commit_failed:{client_oid}",
                    cancel_active_orders=False,
                )
                self._on_order_truth_check(
                    "Gateway submit commit failed",
                    suspicious_oid=client_oid,
                )
            payload = {
                "client_oid": client_oid,
                "exchange_oid": exchange_oid,
                "symbol": intent.symbol,
                "side": intent.side.value,
                "price": intent.price,
                "volume": intent.volume,
            }
            payload.update(audit_extra)
            self._audit(audit_kind, **payload)
            return True

        if command.outcome == CommandOutcome.UNKNOWN:
            try:
                with self.lock:
                    order.mark_submit_unknown(command.error_message or "submit_outcome_unknown")
                    self._record_order_snapshot(
                        order,
                        f"{snapshot_source}_unknown",
                        error_code=command.error_code,
                        **audit_extra,
                    )
                    self._emit_order_update(order)
            except JournalError as exc:
                self._fail_closed_on_journal_error(
                    exc,
                    "snapshot_internal_submit_unknown",
                    intent.symbol,
                )
                self._on_order_truth_check(
                    "Submit result could not be persisted",
                    suspicious_oid=client_oid,
                )
                return True
            self.event_engine.put(
                Event(
                    EVENT_ORDER_SUBMITTED,
                    OrderSubmitted(request, client_oid, time.time(), OrderStatus.SUBMIT_UNKNOWN),
                )
            )
            self.freeze_symbol(
                intent.symbol,
                f"order_truth:submit_unknown:{client_oid}",
                cancel_active_orders=False,
            )
            self._audit(
                f"{audit_kind}_unknown",
                client_oid=client_oid,
                symbol=intent.symbol,
                error_code=command.error_code,
                error_message=command.error_message,
                **audit_extra,
            )
            self._on_order_truth_check("Order submit outcome unknown", suspicious_oid=client_oid)
            return True

        try:
            with self.lock:
                reject_reason = command.error_message or command.error_code or "gateway_send_rejected"
                order.mark_rejected_locally(reject_reason)
                self._record_order_snapshot(order, f"{snapshot_source}_failed", **audit_extra)
                self._emit_order_update(order)
                self.orders.pop(client_oid, None)
                self.exposure.update_open_orders(self.orders)
                self.account.calculate()
        except JournalError as exc:
            self._fail_closed_on_journal_error(
                exc,
                "snapshot_internal_submit_rejected",
                intent.symbol,
            )
            return False
        self._write_tombstone(order)
        payload = {
            "client_oid": client_oid,
            "symbol": intent.symbol,
            "reason": reject_reason,
        }
        payload.update(audit_extra)
        self._audit(f"{audit_kind}_failed", **payload)
        return False

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
                positions[remote_symbol] = float(payload.get("positionAmt", 0.0) or 0.0)

        if not positions:
            with self.lock:
                for local_symbol, volume in self.exposure.net_positions.items():
                    local_symbol = local_symbol.upper()
                    if target_symbols and local_symbol not in target_symbols:
                        continue
                    if abs(volume) > 1e-9:
                        positions[local_symbol] = volume

        submitted = 0
        now = time.time()
        self._audit("emergency_flatten_requested", reason=reason, symbols=sorted(positions.keys()))
        for target_symbol, volume in positions.items():
            if abs(volume) <= 1e-9:
                continue

            last_sent = self.last_emergency_flatten_ts.get(target_symbol, 0.0)
            if now - last_sent < self.emergency_flatten_cooldown_sec:
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
            client_oid = f"EMERGENCY_{target_symbol}_{uuid.uuid4().hex[:16]}"
            intent = OrderIntent(
                "system_emergency",
                target_symbol,
                side,
                estimate_price,
                qty,
                order_type="MARKET",
                time_in_force=TIF_IOC,
                is_post_only=False,
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
                self.last_emergency_flatten_ts[target_symbol] = now
                submitted += 1

        return submitted

    def freeze_symbol(self, symbol: str, reason: str, cancel_active_orders: bool = True):
        if not symbol:
            return

        symbol = symbol.upper()
        previous_reason = self.symbol_guards.get(symbol, "")
        self.symbol_guards[symbol] = reason

        if previous_reason != reason:
            logger.error(f"[OMS] Symbol frozen {symbol}: {reason}")
            self._audit(
                "symbol_frozen",
                symbol=symbol,
                reason=reason,
                previous_reason=previous_reason,
            )
        else:
            self._audit("symbol_freeze_reasserted", symbol=symbol, reason=reason)

        if cancel_active_orders:
            self.cancel_all_orders(symbol)

    def clear_symbol_freeze(self, symbol: str, reason: str = ""):
        if not symbol:
            return False

        symbol = symbol.upper()
        previous_reason = self.symbol_guards.pop(symbol, "")
        if not previous_reason:
            return False

        logger.info(f"[OMS] Symbol restored {symbol}: {reason or previous_reason}")
        self._audit(
            "symbol_unfrozen",
            symbol=symbol,
            reason=reason or previous_reason,
            previous_reason=previous_reason,
        )
        return True

    def get_symbol_freeze_reason(self, symbol: str) -> str:
        if not symbol:
            return ""
        return self.symbol_guards.get(symbol.upper(), "")

    def freeze_venue(self, venue: str, reason: str, cancel_active_orders: bool = True):
        venue = (venue or getattr(self.gateway, "gateway_name", "UNKNOWN")).upper()
        previous_reason = self.venue_guards.get(venue, "")
        self.venue_guards[venue] = reason

        if previous_reason != reason:
            logger.error(f"[OMS] Venue frozen {venue}: {reason}")
            self._audit(
                "venue_frozen",
                venue=venue,
                reason=reason,
                previous_reason=previous_reason,
            )
        else:
            self._audit("venue_freeze_reasserted", venue=venue, reason=reason)

        if not cancel_active_orders:
            return

        try:
            for symbol in self.config.get("symbols", []):
                self.cancel_all_orders(symbol)
        except Exception:
            pass

    def clear_venue_freeze(self, venue: str, reason: str = ""):
        venue = (venue or getattr(self.gateway, "gateway_name", "UNKNOWN")).upper()
        previous_reason = self.venue_guards.pop(venue, "")
        if not previous_reason:
            return False

        logger.info(f"[OMS] Venue restored {venue}: {reason or previous_reason}")
        self._audit(
            "venue_unfrozen",
            venue=venue,
            reason=reason or previous_reason,
            previous_reason=previous_reason,
        )
        return True

    def get_venue_freeze_reason(self, venue: str = "") -> str:
        venue = (venue or getattr(self.gateway, "gateway_name", "UNKNOWN")).upper()
        return self.venue_guards.get(venue, "")

    def freeze_strategy(
        self,
        strategy_id: str,
        reason: str,
        symbol: str = "",
        cancel_active_orders: bool = True,
    ):
        strategy_id = (strategy_id or "").strip()
        if not strategy_id:
            return

        symbol = symbol.upper() if symbol else ""
        if symbol:
            key = (strategy_id, symbol)
            previous_reason = self.strategy_symbol_guards.get(key, "")
            self.strategy_symbol_guards[key] = reason
            payload = {
                "strategy_id": strategy_id,
                "symbol": symbol,
                "reason": reason,
                "previous_reason": previous_reason,
            }
            log_message = f"[OMS] Strategy frozen {strategy_id}/{symbol}: {reason}"
            audit_kind = "strategy_symbol_frozen"
        else:
            previous_reason = self.strategy_guards.get(strategy_id, "")
            self.strategy_guards[strategy_id] = reason
            payload = {
                "strategy_id": strategy_id,
                "reason": reason,
                "previous_reason": previous_reason,
            }
            log_message = f"[OMS] Strategy frozen {strategy_id}: {reason}"
            audit_kind = "strategy_frozen"

        if previous_reason != reason:
            logger.error(log_message)
            self._audit(audit_kind, **payload)
        else:
            self._audit("strategy_freeze_reasserted", **payload)

        if not cancel_active_orders:
            return

        self._cancel_orders_matching(
            lambda order: order.intent.strategy_id == strategy_id
            and (not symbol or order.intent.symbol == symbol)
        )

    def clear_strategy_freeze(self, strategy_id: str, symbol: str = "", reason: str = ""):
        strategy_id = (strategy_id or "").strip()
        if not strategy_id:
            return False

        symbol = symbol.upper() if symbol else ""
        if symbol:
            previous_reason = self.strategy_symbol_guards.pop((strategy_id, symbol), "")
        else:
            previous_reason = self.strategy_guards.pop(strategy_id, "")
        if not previous_reason:
            return False

        payload = {
            "strategy_id": strategy_id,
            "reason": reason or previous_reason,
            "previous_reason": previous_reason,
        }
        if symbol:
            payload["symbol"] = symbol
            logger.info(f"[OMS] Strategy restored {strategy_id}/{symbol}: {reason or previous_reason}")
        else:
            logger.info(f"[OMS] Strategy restored {strategy_id}: {reason or previous_reason}")
        self._audit("strategy_unfrozen", **payload)
        return True

    def get_strategy_freeze_reason(self, strategy_id: str, symbol: str = "") -> str:
        strategy_id = (strategy_id or "").strip()
        if not strategy_id:
            return ""

        symbol = symbol.upper() if symbol else ""
        if symbol:
            scoped_reason = self.strategy_symbol_guards.get((strategy_id, symbol), "")
            if scoped_reason:
                return scoped_reason
        return self.strategy_guards.get(strategy_id, "")

    def clear_transient_guards(self, prefixes=("truth_plane:",)):
        prefixes = tuple(prefixes or ())
        if not prefixes:
            return 0

        cleared = 0
        for symbol, reason in list(self.symbol_guards.items()):
            if any(reason.startswith(prefix) for prefix in prefixes):
                if self.clear_symbol_freeze(symbol, reason=f"transient guard cleared: {reason}"):
                    cleared += 1

        for venue, reason in list(self.venue_guards.items()):
            if any(reason.startswith(prefix) for prefix in prefixes):
                if self.clear_venue_freeze(venue, reason=f"transient guard cleared: {reason}"):
                    cleared += 1

        for strategy_id, reason in list(self.strategy_guards.items()):
            if any(reason.startswith(prefix) for prefix in prefixes):
                if self.clear_strategy_freeze(strategy_id, reason=f"transient guard cleared: {reason}"):
                    cleared += 1

        for (strategy_id, symbol), reason in list(self.strategy_symbol_guards.items()):
            if any(reason.startswith(prefix) for prefix in prefixes):
                if self.clear_strategy_freeze(
                    strategy_id,
                    symbol=symbol,
                    reason=f"transient guard cleared: {reason}",
                ):
                    cleared += 1

        return cleared

    def _clear_recovered_guards_if_pending(self, reason: str = ""):
        if not self.recovered_guard_cleanup_pending:
            return 0

        cleared = 0
        for symbol in list(self.symbol_guards.keys()):
            if self.clear_symbol_freeze(symbol, reason=reason or "recovered_guard_cleared"):
                cleared += 1

        for venue in list(self.venue_guards.keys()):
            if self.clear_venue_freeze(venue, reason=reason or "recovered_guard_cleared"):
                cleared += 1

        for strategy_id in list(self.strategy_guards.keys()):
            if self.clear_strategy_freeze(strategy_id, reason=reason or "recovered_guard_cleared"):
                cleared += 1

        for strategy_id, symbol in list(self.strategy_symbol_guards.keys()):
            if self.clear_strategy_freeze(
                strategy_id,
                symbol=symbol,
                reason=reason or "recovered_guard_cleared",
            ):
                cleared += 1

        self.recovered_guard_cleanup_pending = False
        if cleared:
            self._audit("recovered_guards_cleared", reason=reason or "recovered_guard_cleared", count=cleared)
        return cleared

    def is_symbol_tradeable(self, symbol: str) -> bool:
        return self.can_open_new_risk() and not self.get_symbol_freeze_reason(symbol)

    def can_submit_for_strategy(self, strategy_id: str, symbol: str = "") -> bool:
        return self._get_order_block_reason(strategy_id, symbol) == ""

    def get_order_block_reason(self, strategy_id: str = "", symbol: str = "") -> str:
        return self._get_order_block_reason(strategy_id, symbol)

    def _cancel_orders_matching(self, predicate):
        with self.lock:
            client_oids = [
                order.client_oid
                for order in self.orders.values()
                if order.is_active() and predicate(order)
            ]
        for client_oid in client_oids:
            self.cancel_order(client_oid)

    def _prune_outbound_message_history_locked(self, now: float = None) -> float:
        now = time.monotonic() if now is None else float(now)
        cutoff = now - self.outbound_message_window_sec
        while (
            self.outbound_message_history
            and self.outbound_message_history[0][0] <= cutoff
        ):
            self.outbound_message_history.popleft()
        return now

    def _outbound_message_counts_locked(self) -> dict:
        counts = {
            self.OUTBOUND_NEW_ORDER: 0,
            self.OUTBOUND_REDUCE_ORDER: 0,
            self.OUTBOUND_CANCEL: 0,
        }
        for _timestamp, message_kind in self.outbound_message_history:
            if message_kind in counts:
                counts[message_kind] += 1
        counts["TOTAL"] = len(self.outbound_message_history)
        return counts

    def _reserve_outbound_message_locked(
        self,
        message_kind: str,
        now: float = None,
    ) -> str:
        if not self.outbound_message_budget_enabled:
            return ""
        if message_kind not in {
            self.OUTBOUND_NEW_ORDER,
            self.OUTBOUND_REDUCE_ORDER,
            self.OUTBOUND_CANCEL,
        }:
            raise ValueError(f"unsupported_outbound_message_kind:{message_kind}")

        now = self._prune_outbound_message_history_locked(now)
        counts = self._outbound_message_counts_locked()
        class_limits = {
            self.OUTBOUND_NEW_ORDER: self.max_new_orders_per_window,
            self.OUTBOUND_REDUCE_ORDER: self.max_reduce_orders_per_window,
            self.OUTBOUND_CANCEL: self.max_cancel_messages_per_window,
        }
        class_limit = class_limits[message_kind]
        if class_limit > 0 and counts[message_kind] >= class_limit:
            return (
                f"outbound_message_budget:{message_kind.lower()}_limit:"
                f"{counts[message_kind]}>={class_limit}"
            )

        total_count = counts["TOTAL"]
        if self.max_total_messages_per_window > 0:
            if message_kind == self.OUTBOUND_NEW_ORDER:
                opening_risk_ceiling = max(
                    0,
                    self.max_total_messages_per_window
                    - self.reserved_risk_messages_per_window,
                )
                if total_count >= opening_risk_ceiling:
                    return (
                        "outbound_message_budget:risk_capacity_reserved:"
                        f"{total_count}>={opening_risk_ceiling}"
                    )
            elif total_count >= self.max_total_messages_per_window:
                return (
                    "outbound_message_budget:total_limit:"
                    f"{total_count}>={self.max_total_messages_per_window}"
                )

        self.outbound_message_history.append((now, message_kind))
        return ""

    def get_outbound_message_budget_snapshot(self) -> dict:
        with self.lock:
            self._prune_outbound_message_history_locked()
            counts = self._outbound_message_counts_locked()
            opening_risk_ceiling = (
                max(
                    0,
                    self.max_total_messages_per_window
                    - self.reserved_risk_messages_per_window,
                )
                if self.max_total_messages_per_window > 0
                else None
            )
            return {
                "enabled": self.outbound_message_budget_enabled,
                "window_sec": self.outbound_message_window_sec,
                "counts": counts,
                "limits": {
                    "total": self.max_total_messages_per_window,
                    "new_orders": self.max_new_orders_per_window,
                    "reduce_orders": self.max_reduce_orders_per_window,
                    "cancels": self.max_cancel_messages_per_window,
                    "reserved_risk_messages": self.reserved_risk_messages_per_window,
                    "opening_risk_ceiling": opening_risk_ceiling,
                },
            }

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

        timer = threading.Timer(
            self.outbound_message_window_sec + 0.01,
            retry,
        )
        timer.daemon = True
        timer.start()
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

        timer = threading.Timer(
            self.outbound_message_window_sec + 0.01,
            retry,
        )
        timer.daemon = True
        timer.start()
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
        now = time.time()

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
            if now - float(getattr(order, "created_at", now)) > self.duplicate_intent_window_sec:
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
            time.monotonic() - self.last_risk_control_heartbeat_monotonic,
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

        snapshot_time = float(self.account.margin_snapshot_time or 0.0)
        age_sec = max(0.0, time.time() - snapshot_time) if snapshot_time else float("inf")
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
        if self.state == LifecycleState.HALTED:
            return

        previous_state = self.state
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

        if not cancel_active_orders:
            return

        self._audit(
            "freeze_cancel_all_requested",
            reason=reason,
            symbols=self.config.get("symbols", []),
        )
        try:
            for symbol in self.config["symbols"]:
                self._cancel_all_orders_unchecked(
                    symbol,
                    source="system_freeze",
                )
        except Exception:
            pass

    def halt_system(self, reason: str):
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
            self.event_engine.put(Event(EVENT_SYSTEM_HEALTH, f"HALT:{reason}"))
        try:
            for symbol in self.config["symbols"]:
                self._cancel_all_orders_unchecked(
                    symbol,
                    source="system_halt",
                )
        except Exception:
            pass

    def rearm_system(self, reason: str = "manual"):
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
        self._sync_capability_mode(f"manual_rearm:{reason}")
        self._audit(
            "lifecycle",
            state=self.state.value,
            reason=f"manual_rearm:{reason}",
        )
        self._perform_full_reset()
        if self.state == LifecycleState.LIVE:
            self.manual_rearm_required = False
            self.last_halt_reason = ""
            self._audit("rearm_completed", state=self.state.value, reason=reason)
            return True

        self.manual_rearm_required = True
        return False

    def stop(self):
        try:
            self._audit(
                "oms_stopped",
                state=self.state.value,
                manual_rearm_required=self.manual_rearm_required,
                symbol_guard_count=len(self.symbol_guards),
                venue_guard_count=len(self.venue_guards),
                strategy_guard_count=len(self.strategy_guards),
                strategy_symbol_guard_count=len(self.strategy_symbol_guards),
            )
        finally:
            with self.lock:
                self._stopped = True
            try:
                self.order_monitor.stop()
            finally:
                if self.single_writer_fence is not None:
                    self.single_writer_fence.release()

    def trigger_reconcile(self, reason: str, suspicious_oid: str = None):
        if self.state in [LifecycleState.RECONCILING, LifecycleState.HALTED]:
            return

        self.freeze_system(
            f"Awaiting reconcile: {reason}",
            cancel_active_orders=True,
        )

        now = time.monotonic()
        if self.last_reconcile_failure_ts and now - self.last_reconcile_failure_ts < self.reconcile_api_cooldown_sec:
            logger.warning(f"[OMS] Reconcile suppressed during API cooldown: {reason}")
            self._audit(
                "reconcile_suppressed",
                reason=reason,
                suspicious_oid=suspicious_oid,
                cooldown="api_failure",
            )
            self._schedule_reconcile_retry(reason, suspicious_oid=suspicious_oid)
            return

        if now - self.last_reconcile_request_ts < self.reconcile_min_interval_sec:
            logger.warning(f"[OMS] Reconcile suppressed by min interval: {reason}")
            self._audit(
                "reconcile_suppressed",
                reason=reason,
                suspicious_oid=suspicious_oid,
                cooldown="min_interval",
            )
            self._schedule_reconcile_retry(reason, suspicious_oid=suspicious_oid)
            return

        self.last_reconcile_request_ts = now
        logger.warning(f"OMS dirty: {reason}. State -> RECONCILING")
        self.state = LifecycleState.RECONCILING
        self._sync_capability_mode(reason)
        self._audit(
            "reconcile_requested",
            state=self.state.value,
            reason=reason,
            suspicious_oid=suspicious_oid,
        )
        threading.Thread(
            target=self._execute_reconcile,
            args=(suspicious_oid,),
            daemon=True,
        ).start()

    def _schedule_reconcile_retry(
        self,
        reason: str,
        suspicious_oid: str = None,
        delay_sec: float = None,
    ):
        if self.reconcile_retry_scheduled or self.state == LifecycleState.HALTED:
            return

        if delay_sec is None:
            now = time.monotonic()
            cooldown_remaining = 0.0
            if self.last_reconcile_failure_ts:
                cooldown_remaining = max(
                    0.0,
                    self.reconcile_api_cooldown_sec - (now - self.last_reconcile_failure_ts),
                )
            interval_remaining = max(
                0.0,
                self.reconcile_min_interval_sec - (now - self.last_reconcile_request_ts),
            )
            delay_sec = max(cooldown_remaining, interval_remaining, 0.05)

        delay_sec = max(delay_sec, 0.05)
        self.reconcile_retry_scheduled = True
        self._audit(
            "reconcile_retry_scheduled",
            reason=reason,
            suspicious_oid=suspicious_oid,
            delay_sec=delay_sec,
        )

        def _retry():
            time.sleep(delay_sec)
            self.reconcile_retry_scheduled = False
            if self.state != LifecycleState.FROZEN:
                return
            self.trigger_reconcile(reason, suspicious_oid=suspicious_oid)

        threading.Thread(target=_retry, daemon=True).start()

    def _execute_reconcile(self, suspicious_oid: str):
        self._audit("reconcile_started", suspicious_oid=suspicious_oid)
        try:
            trade_backfill_ok = self._backfill_trade_history(
                end_time_ms=time_service.now(),
            )
            remote_positions = self.query_positions()
            remote_orders = self.query_open_orders()

            if (
                not trade_backfill_ok
                or remote_positions is None
                or remote_orders is None
                or not self._refresh_missing_local_order_terminals(remote_orders)
            ):
                self.consecutive_reconcile_api_failures += 1
                self.last_reconcile_failure_ts = time.monotonic()
                attempt = self.consecutive_reconcile_api_failures
                self._audit(
                    "reconcile_api_unreachable",
                    failures=attempt,
                    suspicious_oid=suspicious_oid,
                )
                if attempt >= self.reconcile_api_failure_threshold:
                    self.halt_system("Reconcile API unreachable")
                else:
                    logger.error(
                        f"[Reconcile] API unreachable ({attempt}/{self.reconcile_api_failure_threshold}); "
                        "keeping FROZEN and backing off."
                    )
                    self.freeze_system("Reconcile API unreachable")
                    self._schedule_reconcile_retry(
                        "Reconcile API retry",
                        suspicious_oid=suspicious_oid,
                    )
                return

            self.consecutive_reconcile_api_failures = 0
            self.last_reconcile_failure_ts = 0.0

            with self.lock:
                remote_map = {
                    pos["symbol"]: float(pos["positionAmt"])
                    for pos in remote_positions
                    if float(pos["positionAmt"]) != 0
                }
                local_map = {
                    symbol: volume
                    for symbol, volume in self.exposure.net_positions.items()
                    if volume != 0
                }
                local_active_orders = self._collect_local_active_orders_locked()

            for symbol in set(remote_map) | set(local_map):
                if abs(local_map.get(symbol, 0.0) - remote_map.get(symbol, 0.0)) > 1e-6:
                    logger.error(
                        f"[Reconcile] Position mismatch {symbol}: "
                        f"Local={local_map.get(symbol, 0.0)}, Exch={remote_map.get(symbol, 0.0)}"
                    )
                    self._audit("reconcile_reset", case="position_mismatch", symbol=symbol)
                    self._perform_full_reset()
                    return

            remote_active_orders = self._normalize_remote_open_orders(remote_orders)
            if local_active_orders != remote_active_orders:
                self._audit(
                    "reconcile_reset",
                    case="open_order_mismatch",
                    local_active_orders=local_active_orders,
                    remote_active_orders=remote_active_orders,
                    suspicious_oid=suspicious_oid,
                )
                self._perform_full_reset()
                return

            remote_has_suspicious = False
            local_has_suspicious = False
            if suspicious_oid:
                remote_has_suspicious = any(
                    suspicious_oid in order["identifiers"]
                    for order in remote_active_orders
                )
                local_has_suspicious = any(
                    suspicious_oid in order["identifiers"]
                    for order in local_active_orders
                )

            if remote_has_suspicious and not local_has_suspicious:
                self._audit(
                    "reconcile_reset",
                    case="missing_local_order",
                    suspicious_oid=suspicious_oid,
                )
                self._perform_full_reset()
            else:
                self.state = LifecycleState.LIVE
                self._sync_capability_mode("reconcile_cleared")
                self.last_freeze_reason = ""
                self._clear_recovered_guards_if_pending("reconcile_cleared")
                self._audit("reconcile_cleared", state=self.state.value)
                logger.info("[Reconcile] False alarm. Resuming LIVE.")

        except Exception as exc:
            self.halt_system(f"Reconcile critical error: {exc}")

    def _exchange_snapshot_signature(self, account, positions, open_orders):
        normalized_positions = tuple(
            sorted(
                (
                    str(pos.get("symbol", "") or ""),
                    float(pos.get("positionAmt", 0.0) or 0.0),
                    float(pos.get("entryPrice", 0.0) or 0.0),
                )
                for pos in positions
                if pos.get("symbol")
            )
        )
        normalized_orders = tuple(
            (
                item["symbol"],
                item["identifiers"],
                item["side"],
            )
            for item in self._normalize_remote_open_orders(open_orders)
        )
        # Exclude mark-to-market fields such as initial margin and available
        # balance: they legitimately move while prices change. The barrier is
        # for structural order/position state plus settled wallet balance.
        account_signature = float(account.get("totalWalletBalance", 0.0) or 0.0)
        return normalized_positions, normalized_orders, account_signature

    def _capture_stable_exchange_snapshot(self, require_no_open_orders=False):
        previous_signature = None
        stable_count = 0
        last_payload = None

        for attempt in range(1, self.snapshot_max_attempts + 1):
            account_floor = time_service.now() / 1000.0
            open_orders = self.query_open_orders()
            account = self.query_account_info()
            positions_floor = time_service.now() / 1000.0
            positions = self.query_positions()
            snapshot_end_ms = time_service.now()
            if open_orders is None or not account or positions is None:
                raise RuntimeError("API failed while acquiring stable exchange snapshot")

            signature = self._exchange_snapshot_signature(account, positions, open_orders)
            if signature == previous_signature:
                stable_count += 1
            else:
                stable_count = 1
                previous_signature = signature

            normalized_orders = self._normalize_remote_open_orders(open_orders)
            last_payload = {
                "open_orders": open_orders,
                "account": account,
                "positions": positions,
                "account_floor": account_floor,
                "positions_floor": positions_floor,
                "end_time_ms": snapshot_end_ms,
                "attempt": attempt,
            }
            if (
                stable_count >= self.snapshot_stability_required
                and (not require_no_open_orders or not normalized_orders)
            ):
                self._audit(
                    "stable_snapshot_acquired",
                    attempts=attempt,
                    stable_count=stable_count,
                    end_time_ms=snapshot_end_ms,
                )
                return last_payload

            time.sleep(self.snapshot_settle_interval_sec)

        residual = (
            self._normalize_remote_open_orders(last_payload["open_orders"])
            if last_payload
            else []
        )
        raise RuntimeError(
            "exchange snapshot did not stabilize"
            + (f"; residual open orders={residual}" if residual else "")
        )

    def _perform_full_reset(self):
        logger.info("[OMS] Performing full state reset...")
        self._audit("full_reset_started", symbols=self.config.get("symbols", []))
        try:
            if not self._ensure_venue_dead_man_switch_armed("full_reset"):
                return
            command_fence_started = time.monotonic()
            for symbol in self.config["symbols"]:
                if not self._cancel_all_orders_unchecked(
                    symbol,
                    source="full_reset_initial",
                ):
                    raise RuntimeError(
                        f"initial mass cancel not admitted for {symbol}"
                    )

            initial_snapshot = self._capture_stable_exchange_snapshot(
                require_no_open_orders=True,
            )
            with self.lock:
                establish_trade_baseline = bool(
                    self.state == LifecycleState.BOOTSTRAP
                    and not self.orders
                    and not self.trade_cursors
                )
            if establish_trade_baseline and not self._prime_trade_history_baseline(
                initial_snapshot["end_time_ms"],
            ):
                raise RuntimeError("trade history baseline failed during bootstrap")
            if self.external_cash_flow_truth_enabled and not self.backfill_external_cash_flow_history(
                end_time_ms=initial_snapshot["end_time_ms"],
                source="bootstrap_income_history",
            ):
                raise RuntimeError("external cash-flow history failed during bootstrap")
            if not self._backfill_trade_history(
                end_time_ms=initial_snapshot["end_time_ms"],
            ):
                raise RuntimeError("trade history backfill failed during reset")

            fence_remaining = max(
                0.0,
                self.command_fence_timeout_sec
                - (time.monotonic() - command_fence_started),
            )
            if fence_remaining > 0:
                self._audit(
                    "command_fence_wait",
                    remaining_sec=fence_remaining,
                )
                time.sleep(fence_remaining)

            # A pre-freeze submit may arrive after the first mass cancel but
            # cannot arrive after its signed recvWindow expires. Cancel again
            # beyond that fence, then establish the committed snapshot.
            for symbol in self.config["symbols"]:
                if not self._cancel_all_orders_unchecked(
                    symbol,
                    source="full_reset_fenced",
                ):
                    raise RuntimeError(
                        f"fenced mass cancel not admitted for {symbol}"
                    )

            snapshot = self._capture_stable_exchange_snapshot(
                require_no_open_orders=True,
            )
            if self.external_cash_flow_truth_enabled and not self.backfill_external_cash_flow_history(
                end_time_ms=snapshot["end_time_ms"],
                source="reset_income_history",
            ):
                raise RuntimeError("external cash-flow history failed during reset")
            if not self._backfill_trade_history(end_time_ms=snapshot["end_time_ms"]):
                raise RuntimeError("final trade history backfill failed during reset")

            remote_orders = snapshot["open_orders"]
            account = snapshot["account"]
            positions = snapshot["positions"]
            account_snapshot_floor = snapshot["account_floor"]
            positions_snapshot_floor = snapshot["positions_floor"]

            residual_orders = self._normalize_remote_open_orders(remote_orders)
            if residual_orders:
                raise RuntimeError(
                    f"remote open orders still present after cancel-all: {residual_orders}"
                )

            with self.lock:
                previously_tracked_symbols = set(self.exposure.net_positions.keys())
                self.orders.clear()
                self.exchange_id_map.clear()
                self.exposure.net_positions.clear()
                self.exposure.avg_prices.clear()
                self.exposure.open_buy_qty.clear()
                self.exposure.open_sell_qty.clear()
                self.exposure.update_open_orders(self.orders)

                for pos in positions:
                    amount = float(pos["positionAmt"])
                    if amount == 0:
                        continue
                    symbol = pos["symbol"]
                    self.exposure.force_sync(symbol, amount, float(pos["entryPrice"]))

                snapshot_symbols = previously_tracked_symbols | set(self.config.get("symbols", []))
                snapshot_symbols.update(
                    str(pos.get("symbol", "") or "")
                    for pos in positions
                    if pos.get("symbol")
                )
                for symbol in snapshot_symbols:
                    self.exposure.reconcile_strategy_position(
                        symbol,
                        self.exposure.net_positions[symbol],
                        self.exposure.avg_prices[symbol],
                    )
                    self._position_state_event_time[symbol] = max(
                        float(self._position_state_event_time.get(symbol, 0.0) or 0.0),
                        positions_snapshot_floor,
                    )
                    self._exchange_position_event_time[symbol] = max(
                        float(self._exchange_position_event_time.get(symbol, 0.0) or 0.0),
                        positions_snapshot_floor,
                    )

                available_balance = account.get("availableBalance")
                self.account.force_sync(
                    float(account["totalWalletBalance"]),
                    float(account["totalInitialMargin"]),
                    float(available_balance) if available_balance is not None else None,
                    maintenance_margin=account.get("totalMaintMargin"),
                    margin_balance=account.get("totalMarginBalance"),
                    margin_snapshot_time=time.time(),
                )
                self._account_state_event_time = max(
                    float(self._account_state_event_time or 0.0),
                    account_snapshot_floor,
                )
                self._exchange_account_event_time = max(
                    float(self._exchange_account_event_time or 0.0),
                    account_snapshot_floor,
                )
                self.order_monitor.monitored_orders.clear()

            for symbol in self.config.get("symbols", []):
                self._emit_position_update(symbol)

            self.state = LifecycleState.LIVE
            self._sync_capability_mode("full_reset_completed")
            self.manual_rearm_required = False
            self.last_freeze_reason = ""
            self.last_halt_reason = ""
            self.reconcile_retry_scheduled = False
            self.clear_transient_guards(prefixes=("truth_plane:",))
            self._clear_recovered_guards_if_pending("full_reset_completed")
            self._audit(
                "full_reset_completed",
                state=self.state.value,
                balance=self.account.balance,
                equity=self.account.equity,
                positions=dict(self.exposure.net_positions),
            )
            logger.info("OMS: Reset complete. System is CLEAN and LIVE.")

        except Exception as exc:
            self.halt_system(f"Reset failed: {exc}")

    def submit_order(self, intent: OrderIntent) -> OrderSubmitResult:
        client_oid = str(uuid.uuid4())
        original_intent = intent
        order = None
        request = None
        rejection_reason = ""
        rejection_extra = {}
        rejection_intent = intent
        command_id = f"SUBMIT:{client_oid}"

        # Risk evaluation and exposure reservation are one critical section.
        # Every concurrent submit sees earlier accepted-but-not-yet-ACKed orders.
        try:
            with self.lock:
                rejection_reason = self._get_order_block_reason(
                    intent.strategy_id,
                    intent.symbol,
                    reduce_only=intent.reduce_only,
                )
                if not rejection_reason:
                    intent, rejection_reason = self.adapt_intent_for_trading_mode(intent)
                    rejection_intent = original_intent if rejection_reason else intent

                if not rejection_reason:
                    valid, rejection_reason = self.validator.validate_params(intent)
                    rejection_intent = intent
                    if valid:
                        rejection_reason = ""

                if not rejection_reason:
                    rejection_reason = self._get_submission_safety_reason_locked(intent)

                notional = intent.price * intent.volume if not rejection_reason else 0.0
                if not rejection_reason and intent.reduce_only:
                    ok, risk_reason = self.exposure.check_reduce_only(
                        intent.symbol,
                        intent.side,
                        intent.volume,
                    )
                    if not ok:
                        rejection_reason = risk_reason
                elif not rejection_reason:
                    if not self.account.check_margin(notional):
                        rejection_reason = "insufficient_margin"
                        rejection_extra = {
                            "notional": notional,
                            "available": self.account.available,
                        }
                    else:
                        ok, risk_reason = self.exposure.check_risk(
                            intent.symbol,
                            intent.side,
                            intent.volume,
                            self.max_pos_notional,
                            self.max_account_gross_notional,
                            intent.price,
                        )
                        if not ok:
                            logger.warning(f"[OMS] Risk rejected: {risk_reason}")
                            rejection_reason = f"exposure_limit:{risk_reason}"

                if not rejection_reason:
                    rejection_reason = (
                        self._get_strategy_budget_rejection_locked(intent)
                    )

                if not rejection_reason:
                    message_kind = (
                        self.OUTBOUND_REDUCE_ORDER
                        if intent.reduce_only
                        else self.OUTBOUND_NEW_ORDER
                    )
                    rejection_reason = self._reserve_outbound_message_locked(
                        message_kind
                    )

                if not rejection_reason:
                    request = OrderRequest(
                        symbol=intent.symbol,
                        price=intent.price,
                        volume=intent.volume,
                        side=intent.side.value,
                        order_type=intent.order_type,
                        time_in_force=intent.time_in_force,
                        post_only=intent.is_post_only,
                        reduce_only=intent.reduce_only,
                        self_trade_prevention_mode=(
                            self.exchange_self_trade_prevention_mode
                        ),
                    )
                    order = Order(client_oid, intent)
                    self.orders[client_oid] = order
                    order.mark_submitting()
                    self.exposure.update_open_orders(self.orders)
                    self.account.calculate()
                    self._record_order_snapshot(order, "accepted")

            if rejection_reason:
                return self._reject_intent_locally(
                    rejection_intent,
                    client_oid,
                    rejection_reason,
                    **rejection_extra,
                )

            # The durable command intent is committed before the first byte is
            # sent to the venue. Recovery queries by client_oid and never
            # blindly resends an ambiguous command.
            self._record_command_prepared(command_id, "SUBMIT", order, request)
        except JournalError as exc:
            with self.lock:
                if order is not None and order.status == OrderStatus.SUBMITTING:
                    order.mark_rejected_locally("durable_journal_unavailable")
                self.orders.pop(client_oid, None)
                self.exposure.update_open_orders(self.orders)
                self.account.calculate()
                if order is not None:
                    self._emit_order_update(order)
            self._fail_closed_on_journal_error(exc, "prepare_submit", intent.symbol)
            return OrderSubmitResult(
                accepted=False,
                client_oid=client_oid,
                reason="durable_journal_unavailable",
                state=self.state.value,
            )

        try:
            raw_result = self.gateway.send_order(request, client_oid)
            command = self._normalize_submit_command(raw_result)
        except Exception as exc:
            command = GatewayCommandResult(
                CommandOutcome.UNKNOWN,
                error_message=f"gateway_send_exception:{exc}",
            )

        try:
            self._record_command_result(
                command_id,
                "SUBMIT",
                order,
                command.outcome,
                exchange_oid=command.exchange_oid,
                error_code=command.error_code,
                error_message=command.error_message,
            )
        except JournalError as exc:
            with self.lock:
                if order.status == OrderStatus.SUBMITTING:
                    order.mark_submit_unknown("result_not_durable")
                self._emit_order_update(order)
            self._fail_closed_on_journal_error(exc, "result_submit", intent.symbol)
            self._on_order_truth_check(
                "Submit result could not be persisted",
                suspicious_oid=client_oid,
            )
            return OrderSubmitResult(
                accepted=True,
                client_oid=client_oid,
                reason="submit_outcome_unknown",
                state=self.state.value,
            )

        if command.outcome == CommandOutcome.ACKNOWLEDGED:
            exchange_oid = command.exchange_oid
            try:
                with self.lock:
                    order.mark_pending_ack(exchange_oid)
                    self.exchange_id_map[exchange_oid] = order
                    self._record_order_snapshot(order, "rest_ack")
                    self._emit_order_update(order)
            except JournalError as exc:
                self._fail_closed_on_journal_error(exc, "snapshot_submit_ack", intent.symbol)
                return OrderSubmitResult(
                    accepted=True,
                    client_oid=client_oid,
                    reason="accepted_but_local_snapshot_failed",
                    state=self.state.value,
                )

            event_data = OrderSubmitted(request, client_oid, time.time())
            self.event_engine.put(Event(EVENT_ORDER_SUBMITTED, event_data))
            try:
                self._commit_gateway_submission(client_oid)
            except Exception as exc:
                logger.error(
                    f"[OMS] Gateway submit commit failed for {client_oid}: {exc}"
                )
                self.freeze_symbol(
                    intent.symbol,
                    f"order_truth:submit_commit_failed:{client_oid}",
                    cancel_active_orders=False,
                )
                self._on_order_truth_check(
                    "Gateway submit commit failed",
                    suspicious_oid=client_oid,
                )
            self._audit(
                "order_submitted",
                client_oid=client_oid,
                exchange_oid=exchange_oid,
                symbol=intent.symbol,
                side=intent.side.value,
                price=intent.price,
                volume=intent.volume,
            )
            return OrderSubmitResult(
                accepted=True,
                client_oid=client_oid,
                state=self.state.value,
            )

        if command.outcome == CommandOutcome.UNKNOWN:
            try:
                with self.lock:
                    order.mark_submit_unknown(command.error_message or "submit_outcome_unknown")
                    self._record_order_snapshot(
                        order,
                        "send_unknown",
                        error_code=command.error_code,
                        error_message=command.error_message,
                    )
                    self._emit_order_update(order)
            except JournalError as exc:
                self._fail_closed_on_journal_error(exc, "snapshot_submit_unknown", intent.symbol)
                self._on_order_truth_check(
                    "Submit result could not be persisted",
                    suspicious_oid=client_oid,
                )
                return OrderSubmitResult(
                    accepted=True,
                    client_oid=client_oid,
                    reason="submit_outcome_unknown",
                    state=self.state.value,
                )
            self.event_engine.put(
                Event(
                    EVENT_ORDER_SUBMITTED,
                    OrderSubmitted(request, client_oid, time.time(), OrderStatus.SUBMIT_UNKNOWN),
                )
            )
            self.freeze_symbol(
                intent.symbol,
                f"order_truth:submit_unknown:{client_oid}",
                cancel_active_orders=False,
            )
            self._audit(
                "order_submit_unknown",
                client_oid=client_oid,
                symbol=intent.symbol,
                error_code=command.error_code,
                error_message=command.error_message,
            )
            self._on_order_truth_check("Order submit outcome unknown", suspicious_oid=client_oid)
            return OrderSubmitResult(
                accepted=True,
                client_oid=client_oid,
                reason="submit_outcome_unknown",
                state=self.state.value,
            )

        try:
            with self.lock:
                reject_reason = command.error_message or command.error_code or "gateway_send_rejected"
                order.mark_rejected_locally(reject_reason)
                self._record_order_snapshot(order, "send_failed")
                self._emit_order_update(order)
                self.orders.pop(client_oid, None)
                self.exposure.update_open_orders(self.orders)
                self.account.calculate()
        except JournalError as exc:
            self._fail_closed_on_journal_error(exc, "snapshot_submit_rejected", intent.symbol)
            return OrderSubmitResult(
                accepted=False,
                client_oid=client_oid,
                reason="durable_journal_unavailable",
                state=self.state.value,
            )
        self._write_tombstone(order)
        self._audit(
            "order_rejected_locally",
            client_oid=client_oid,
            symbol=intent.symbol,
            reason=reject_reason,
        )
        return OrderSubmitResult(
            accepted=False,
            client_oid=client_oid,
            reason=reject_reason,
            state=self.state.value,
        )

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
        try:
            with self.lock:
                order = self.orders.get(client_oid)
                if not order or not order.is_active():
                    return False
                budget_rejection = self._reserve_outbound_message_locked(
                    self.OUTBOUND_CANCEL
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
                    self._record_order_snapshot(order, "cancel_requested")
                    self._emit_order_update(order)
                except ValueError:
                    pass
                request = CancelRequest(order.intent.symbol, target_id)
            self._record_command_prepared(command_id, "CANCEL", order, request)
        except JournalError as exc:
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
            with self.lock:
                current = self.orders.get(client_oid)
                if current and current.is_active():
                    try:
                        current.mark_cancel_unknown("result_not_durable")
                    except ValueError:
                        pass
                    self._emit_order_update(current)
            self._fail_closed_on_journal_error(exc, "result_cancel", request.symbol)
            self._on_order_truth_check(
                "Cancel result could not be persisted",
                suspicious_oid=client_oid,
            )
            return True

        if response is not None and getattr(response, "status_code", 0) == 200:
            try:
                payload = response.json()
            except Exception:
                payload = {}
            if isinstance(payload, dict) and payload.get("status"):
                try:
                    self._apply_exchange_order_snapshot(payload, source="cancel_rest_ack")
                except JournalError as exc:
                    self._fail_closed_on_journal_error(
                        exc,
                        "snapshot_cancel_ack",
                        request.symbol,
                    )
                    return True
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
    ) -> bool:
        with self.lock:
            budget_rejection = self._reserve_outbound_message_locked(
                self.OUTBOUND_CANCEL
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
            self._audit("cancel_all_submitted", symbol=symbol, source=source)
        try:
            self.gateway.cancel_all_orders(symbol)
        except Exception as exc:
            logger.error(f"[OMS] Mass cancel failed for {symbol}: {exc}")
            return False
        return True

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

    def on_exchange_update(self, event):
        try:
            self._append_and_process(event)
        except JournalError as exc:
            symbol = str(getattr(event.data, "symbol", "") or "")
            self._fail_closed_on_journal_error(exc, "exchange_update", symbol)

    def on_exchange_account_update(self, event):
        update: ExchangeAccountUpdate = event.data
        if str(update.reason or "").upper() in self.CASH_FLOW_DIRTY_REASONS:
            self.mark_external_cash_flow_truth_unavailable(
                f"account_update:{str(update.reason).upper()}"
            )
        tracked_symbols = set(self.config.get("symbols", []))
        tracked_positions = {
            symbol: payload
            for symbol, payload in update.positions.items()
            if not tracked_symbols or symbol in tracked_symbols
        }

        event_time = float(update.event_time or 0.0)
        has_balance_update = bool(update.asset or update.balances)
        corrected_positions = {}
        corrected_with_active_order = {}

        with self.lock:
            # ACCOUNT_UPDATE.P is a partial delta: only symbols changed by this
            # account event are present. Absence must never be interpreted as a
            # flat position.
            for symbol, payload in tracked_positions.items():
                state_time = float(self._position_state_event_time.get(symbol, 0.0) or 0.0)
                if event_time and state_time and event_time + 1e-6 < state_time:
                    self._audit(
                        "stale_exchange_position_update_ignored",
                        symbol=symbol,
                        event_time=event_time,
                        state_time=state_time,
                    )
                    continue

                remote_volume = float(payload.get("volume", 0.0) or 0.0)
                remote_entry_price = float(payload.get("entry_price", 0.0) or 0.0)
                local_volume = float(self.exposure.net_positions.get(symbol, 0.0) or 0.0)
                if abs(local_volume - remote_volume) > 1e-9:
                    corrected_positions[symbol] = {
                        "local": local_volume,
                        "exchange": remote_volume,
                        "entry_price": remote_entry_price,
                    }
                    corrected_with_active_order[symbol] = self._has_active_orders_locked({symbol})
                    self.exposure.force_sync(symbol, remote_volume, remote_entry_price)
                    self.exposure.reconcile_strategy_position(
                        symbol,
                        remote_volume,
                        remote_entry_price,
                    )

                if event_time:
                    self._position_state_event_time[symbol] = max(state_time, event_time)
                    self._exchange_position_event_time[symbol] = max(
                        float(self._exchange_position_event_time.get(symbol, 0.0) or 0.0),
                        event_time,
                    )

            account_state_time = float(self._account_state_event_time or 0.0)
            account_update_is_stale = bool(
                event_time
                and account_state_time
                and event_time + 1e-6 < account_state_time
            )
            if account_update_is_stale:
                self._audit(
                    "stale_exchange_account_update_ignored",
                    event_time=event_time,
                    state_time=account_state_time,
                )
                if corrected_positions:
                    self.account.calculate()
            elif has_balance_update:
                self.account.sync_exchange_balance(
                    update.wallet_balance,
                    available=update.available_balance,
                    asset=update.asset,
                    balances=update.balances,
                )
                if event_time:
                    self._account_state_event_time = max(account_state_time, event_time)
                    self._exchange_account_event_time = max(
                        float(self._exchange_account_event_time or 0.0),
                        event_time,
                    )

        if not corrected_positions:
            return

        self._audit(
            "exchange_account_positions_synced",
            reason=update.reason,
            event_time=event_time,
            positions=corrected_positions,
        )

        for symbol in corrected_positions:
            self._emit_position_update(symbol)

        if self.state in {LifecycleState.HALTED, LifecycleState.RECONCILING}:
            return

        unexpected_positions = {
            symbol: payload
            for symbol, payload in corrected_positions.items()
            if not corrected_with_active_order.get(symbol, False)
        }
        if unexpected_positions:
            logger.error(
                "[OMS] Exchange position correction without an active local order: "
                f"{unexpected_positions}"
            )
            self.trigger_reconcile("Unexpected exchange account position correction")

    def _append_and_process(self, event):
        self.event_log.append(event)
        self._apply_event(event)

    def _apply_event(self, event):
        if event.type != "eExchangeOrderUpdate":
            return

        update: ExchangeOrderUpdate = event.data
        update.status = str(update.status or "").upper()
        if update.status == "EXPIRED_IN_MATCH":
            update.status = "EXPIRED"
        with self.lock:
            order = self.orders.get(update.client_oid)
            if not order and update.exchange_oid:
                order = self.exchange_id_map.get(update.exchange_oid)

            if not order:
                suspicious = update.client_oid or update.exchange_oid
                if suspicious in self.terminated_oids:
                    self._audit(
                        "late_duplicate_ignored",
                        suspicious_oid=suspicious,
                        exchange_status=update.status,
                    )
                    return
                self._audit(
                    "unknown_order_update",
                    client_oid=update.client_oid,
                    exchange_oid=update.exchange_oid,
                    status=update.status,
                )
                threading.Thread(
                    target=self.trigger_reconcile,
                    args=(f"Unknown Order {suspicious}", suspicious),
                    daemon=True,
                ).start()
                return

            if update.exchange_oid and order.exchange_oid and order.exchange_oid != update.exchange_oid:
                self._audit(
                    "exchange_oid_mismatch",
                    client_oid=order.client_oid,
                    local_exchange_oid=order.exchange_oid,
                    incoming_exchange_oid=update.exchange_oid,
                )
                threading.Thread(
                    target=self.trigger_reconcile,
                    args=(f"Exchange OID mismatch {order.client_oid}", order.client_oid),
                    daemon=True,
                ).start()
                return

            semantic_mismatches = {}
            if (
                update.order_type
                and update.order_type != order.intent.order_type
            ):
                semantic_mismatches["order_type"] = {
                    "expected": order.intent.order_type,
                    "actual": update.order_type,
                }
            if (
                update.time_in_force
                and order.intent.order_type == "LIMIT"
                and update.time_in_force != order.intent.time_in_force
            ):
                semantic_mismatches["time_in_force"] = {
                    "expected": order.intent.time_in_force,
                    "actual": update.time_in_force,
                }
            if semantic_mismatches:
                reason = f"exchange_order_semantics_mismatch:{order.client_oid}"
                self._audit(
                    "exchange_order_semantics_mismatch",
                    client_oid=order.client_oid,
                    symbol=order.intent.symbol,
                    mismatches=semantic_mismatches,
                )
                threading.Thread(
                    target=self.freeze_symbol,
                    args=(order.intent.symbol, reason),
                    daemon=True,
                ).start()

            if update.seq and update.seq <= order.last_update_seq:
                self._audit(
                    "stale_update_ignored",
                    client_oid=order.client_oid,
                    seq=update.seq,
                    last_seq=order.last_update_seq,
                )
                return

            if (
                not update.seq
                and update.update_time
                and order.last_exchange_update_time
                and update.update_time + 1e-6 < order.last_exchange_update_time
                and update.cum_filled_qty <= order.filled_volume + 1e-9
            ):
                self._audit(
                    "stale_exchange_time_update_ignored",
                    client_oid=order.client_oid,
                    update_time=update.update_time,
                    last_exchange_update_time=order.last_exchange_update_time,
                )
                return

            if update.cum_filled_qty + 1e-9 < order.filled_volume:
                self._audit(
                    "cum_fill_regression",
                    client_oid=order.client_oid,
                    incoming_cum=update.cum_filled_qty,
                    local_cum=order.filled_volume,
                )
                threading.Thread(
                    target=self.trigger_reconcile,
                    args=(f"Cum fill regression {order.client_oid}", order.client_oid),
                    daemon=True,
                ).start()
                return

            if (
                update.status in {"CANCELED", "EXPIRED"}
                and update.cum_filled_qty > order.filled_volume + 1e-9
            ):
                recovery_price = update.filled_price or order.avg_price or order.intent.price
                recovery_status = (
                    "FILLED"
                    if update.cum_filled_qty >= order.intent.volume - 1e-8
                    else "PARTIALLY_FILLED"
                )
                self._audit(
                    "terminal_update_missing_fill_recovered",
                    client_oid=order.client_oid,
                    exchange_status=update.status,
                    local_filled=order.filled_volume,
                    exchange_filled=update.cum_filled_qty,
                )
                self._apply_event(
                    Event(
                        EVENT_EXCHANGE_ORDER_UPDATE,
                        ExchangeOrderUpdate(
                            client_oid=order.client_oid,
                            exchange_oid=update.exchange_oid or order.exchange_oid,
                            symbol=order.intent.symbol,
                            status=recovery_status,
                            filled_qty=update.cum_filled_qty - order.filled_volume,
                            filled_price=recovery_price,
                            cum_filled_qty=update.cum_filled_qty,
                            update_time=update.update_time,
                            seq=update.seq,
                            commission=update.commission,
                            commission_asset=update.commission_asset,
                            realized_pnl=update.realized_pnl,
                            is_maker=update.is_maker,
                            trade_id=update.trade_id,
                        ),
                    )
                )
                order = self.orders.get(order.client_oid, order)
                if order.status == OrderStatus.FILLED:
                    return

            previous_status = order.status
            had_fill = False

            try:
                if update.status == "NEW":
                    order.mark_new(
                        exchange_oid=update.exchange_oid,
                        update_time=update.update_time,
                        seq=update.seq,
                    )
                    if update.exchange_oid:
                        self.exchange_id_map[update.exchange_oid] = order

                elif update.status == "CANCELED":
                    order.mark_cancelled(
                        update_time=update.update_time,
                        seq=update.seq,
                        exchange_status=update.status,
                    )
                    self._write_tombstone(order)

                elif update.status == "EXPIRED":
                    order.mark_expired(update_time=update.update_time, seq=update.seq)
                    self._write_tombstone(order)

                elif update.status == "REJECTED":
                    order.mark_rejected(
                        reason="exchange_rejected",
                        update_time=update.update_time,
                        seq=update.seq,
                        exchange_status=update.status,
                    )
                    self._write_tombstone(order)

                elif update.status in ["FILLED", "PARTIALLY_FILLED"]:
                    delta = update.cum_filled_qty - order.filled_volume
                    if delta > 1e-9:
                        fill_notional = delta * update.filled_price
                        fee = self._get_fill_commission(update, order, fill_notional)
                        # Execution truth is committed before mutating order,
                        # exposure, or account projections. If the process dies
                        # after this point, replay can finish applying the fill.
                        self._record_execution(order, update, delta, fee)
                        had_fill = order.add_fill(
                            delta,
                            update.filled_price,
                            update_time=update.update_time,
                            seq=update.seq,
                            exchange_status=update.status,
                        )
                        symbol = order.intent.symbol
                        if had_fill:
                            self.exposure.on_strategy_fill(
                                order.intent.strategy_id,
                                symbol,
                                order.intent.side,
                                delta,
                                update.filled_price,
                            )
                        exchange_position_time = float(
                            self._exchange_position_event_time.get(symbol, 0.0) or 0.0
                        )
                        position_already_synced = bool(
                            update.update_time
                            and exchange_position_time + 1e-6 >= update.update_time
                        )
                        if position_already_synced:
                            local_realized_pnl = 0.0
                            self._audit(
                                "fill_position_already_covered",
                                client_oid=order.client_oid,
                                symbol=symbol,
                                fill_time=update.update_time,
                                exchange_position_time=exchange_position_time,
                            )
                        else:
                            local_realized_pnl = self.exposure.on_fill(
                                symbol,
                                order.intent.side,
                                delta,
                                update.filled_price,
                            )
                            if update.update_time:
                                self._position_state_event_time[symbol] = max(
                                    float(self._position_state_event_time.get(symbol, 0.0) or 0.0),
                                    update.update_time,
                                )
                        self.exposure.reconcile_strategy_position(
                            symbol,
                            self.exposure.net_positions[symbol],
                            self.exposure.avg_prices[symbol],
                        )
                        realized_pnl = (
                            update.realized_pnl
                            if update.realized_pnl is not None
                            else local_realized_pnl
                        )
                        account_already_synced = bool(
                            update.update_time
                            and float(self._exchange_account_event_time or 0.0) + 1e-6
                            >= update.update_time
                        )
                        if account_already_synced:
                            self._audit(
                                "fill_account_already_covered",
                                client_oid=order.client_oid,
                                fill_time=update.update_time,
                                exchange_account_time=self._exchange_account_event_time,
                            )
                        else:
                            self.account.update_balance(realized_pnl, fee)
                            if update.update_time:
                                self._account_state_event_time = max(
                                    float(self._account_state_event_time or 0.0),
                                    update.update_time,
                                )

                        trade_data = TradeData(
                            symbol=order.intent.symbol,
                            order_id=order.client_oid,
                            trade_id=(
                                str(update.trade_id)
                                if update.trade_id >= 0
                                else f"T{int(update.update_time * 1000)}"
                            ),
                            side=order.intent.side.value,
                            price=update.filled_price,
                            volume=delta,
                            datetime=datetime.now(),
                        )
                        self.event_engine.put(Event(EVENT_TRADE_UPDATE, trade_data))
                    else:
                        order.note_exchange_update(
                            exchange_status=update.status,
                            update_time=update.update_time,
                            seq=update.seq,
                            exchange_oid=update.exchange_oid,
                        )

                    if update.status == "FILLED":
                        order.mark_filled(update_time=update.update_time, seq=update.seq)
                        self._write_tombstone(order)

                    if update.trade_id >= 0:
                        self._advance_trade_cursor(
                            order.intent.symbol,
                            update.trade_id,
                            update.update_time,
                            source="user_stream",
                        )

                else:
                    self._audit(
                        "unhandled_exchange_status",
                        client_oid=order.client_oid,
                        status=update.status,
                    )
                    order.note_exchange_update(
                        exchange_status=update.status,
                        update_time=update.update_time,
                        seq=update.seq,
                        exchange_oid=update.exchange_oid,
                    )
                    return

            except ValueError as exc:
                self._audit(
                    "invalid_transition",
                    client_oid=order.client_oid,
                    current_status=order.status.value,
                    incoming_status=update.status,
                    error=str(exc),
                )
                threading.Thread(
                    target=self.trigger_reconcile,
                    args=(f"Invalid transition {order.client_oid}", order.client_oid),
                    daemon=True,
                ).start()
                return

            self.order_monitor.on_order_update(order.client_oid, order.status)
            self.exposure.update_open_orders(self.orders)
            self.account.calculate()

            if order.status != previous_status or had_fill:
                self._record_order_snapshot(
                    order,
                    "exchange_update",
                    exchange_status=update.status,
                    seq=update.seq,
                    cum_filled_qty=update.cum_filled_qty,
                )
                self._emit_order_update(order)
                if had_fill:
                    self._emit_position_update(order.intent.symbol)

    def _apply_recovered_execution(self, order: Order, payload: dict):
        execution_id = str(payload.get("execution_id", "") or "")
        if not execution_id:
            raise JournalCorruptionError(
                f"Execution record without execution_id for {order.client_oid}"
            )
        self.execution_ids.add(execution_id)

        exchange_oid = str(payload.get("exchange_oid", "") or "")
        if exchange_oid:
            order.exchange_oid = exchange_oid

        try:
            cumulative_qty = float(payload.get("cum_filled_qty", 0.0) or 0.0)
            fill_price = float(payload.get("fill_price", 0.0) or 0.0)
            exchange_time = float(payload.get("exchange_time", 0.0) or 0.0)
        except (TypeError, ValueError) as exc:
            raise JournalCorruptionError(
                f"Malformed execution record {execution_id}: {exc}"
            ) from exc

        delta = cumulative_qty - order.filled_volume
        if delta <= 1e-9:
            return

        pre_status = order.status
        order.add_fill(
            delta,
            fill_price,
            update_time=exchange_time,
            exchange_status=str(payload.get("exchange_status", "") or "PARTIALLY_FILLED"),
        )
        if order.status == OrderStatus.FILLED:
            return
        if pre_status == OrderStatus.CANCELLED:
            order.mark_cancelled(update_time=exchange_time)
        elif pre_status == OrderStatus.EXPIRED:
            order.mark_expired(update_time=exchange_time)

    def _apply_recovered_command_result(self, order: Order, payload: dict):
        command_type = str(payload.get("command_type", "") or "").upper()
        try:
            outcome = CommandOutcome(str(payload.get("outcome", "") or ""))
        except ValueError as exc:
            raise JournalCorruptionError(
                f"Invalid command outcome for {order.client_oid}: {payload.get('outcome')}"
            ) from exc

        error_message = str(
            payload.get("error_message", "")
            or payload.get("error_code", "")
            or "recovered_command_result"
        )
        exchange_oid = str(payload.get("exchange_oid", "") or "")

        if command_type == "SUBMIT":
            if order.status == OrderStatus.CREATED:
                order.mark_submitting()
            if order.status != OrderStatus.SUBMITTING:
                return
            if outcome == CommandOutcome.ACKNOWLEDGED:
                order.mark_pending_ack(exchange_oid)
            elif outcome == CommandOutcome.UNKNOWN:
                order.mark_submit_unknown(error_message)
            else:
                order.mark_rejected_locally(error_message)
            return

        if command_type == "CANCEL" and order.is_active():
            if order.status != OrderStatus.CANCELLING:
                try:
                    order.mark_cancelling()
                except ValueError:
                    pass
            if order.status == OrderStatus.CANCELLING:
                order.mark_cancel_unknown(
                    "recovered_cancel_ack_requires_truth"
                    if outcome == CommandOutcome.ACKNOWLEDGED
                    else error_message
                )

    def rebuild_from_log(self):
        records = self.journal.load()
        if not records:
            return {
                "records": 0,
                "recovered_orders": 0,
                "recovered_active_orders": 0,
                "recovered_terminal_ids": 0,
                "pending_commands": 0,
                "last_lifecycle": None,
                "last_freeze_reason": "",
                "last_halt_reason": "",
                "manual_rearm_required": False,
                "symbol_guards": {},
                "venue_guards": {},
                "strategy_guards": {},
                "strategy_symbol_guards": {},
                "mode_override": "",
                "mode_override_reason": "",
                "mode_constraints": {},
                "clean_shutdown": True,
                "dirty_shutdown": False,
                "trade_cursors": {},
                "trade_scan_end_ms": {},
                "external_cash_flow_total": 0.0,
                "external_cash_flow_ids": [],
                "external_cash_flow_scan_end_ms": 0,
            }

        latest_order_records = {}
        latest_order_record_indexes = {}
        commands = {}
        executions_by_client_oid = {}
        execution_records = []
        last_lifecycle = None
        last_freeze_reason = ""
        last_halt_reason = ""
        manual_rearm_required = False
        symbol_guards = {}
        venue_guards = {}
        strategy_guards = {}
        strategy_symbol_guards = {}
        mode_override = ""
        mode_override_reason = ""
        mode_constraints = {}
        trade_cursors = {}
        trade_scan_end_ms = {}
        external_cash_flow_total = 0.0
        external_cash_flow_ids = set()
        external_cash_flow_scan_end_ms = 0
        clean_shutdown = records[-1].get("kind") == "oms_stopped"
        for record_index, record in enumerate(records):
            payload = record.get("payload", {})
            kind = record.get("kind")
            if kind == "order_snapshot":
                client_oid = payload.get("client_oid")
                if client_oid:
                    latest_order_records[client_oid] = payload
                    latest_order_record_indexes[client_oid] = record_index
            elif kind in {"command_prepared", "command_result"}:
                command_id = str(payload.get("command_id", "") or "")
                if command_id:
                    entry = commands.setdefault(command_id, {})
                    entry[kind] = {
                        "index": record_index,
                        "payload": payload,
                    }
            elif kind == "execution_record":
                client_oid = str(payload.get("client_oid", "") or "")
                execution_records.append(
                    {"index": record_index, "payload": payload}
                )
                if client_oid:
                    executions_by_client_oid.setdefault(client_oid, []).append(
                        {"index": record_index, "payload": payload}
                    )
            elif kind == "lifecycle":
                last_lifecycle = payload.get("state")
                reason = str(payload.get("reason", "") or "")
                if last_lifecycle == LifecycleState.FROZEN.value and reason:
                    last_freeze_reason = reason
                elif last_lifecycle == LifecycleState.HALTED.value:
                    if reason:
                        last_halt_reason = reason
                    manual_rearm_required = bool(payload.get("manual_rearm_required", True))
                elif last_lifecycle == LifecycleState.LIVE.value:
                    manual_rearm_required = False
                    last_halt_reason = ""
                    last_freeze_reason = ""
            elif kind in {"full_reset_completed", "reconcile_cleared", "rearm_completed"}:
                last_lifecycle = payload.get("state") or LifecycleState.LIVE.value
                if last_lifecycle == LifecycleState.LIVE.value:
                    manual_rearm_required = False
                    last_freeze_reason = ""
                    if kind == "rearm_completed":
                        last_halt_reason = ""
            elif kind in {"reconcile_requested", "reconcile_started", "full_reset_started"}:
                last_lifecycle = LifecycleState.RECONCILING.value
            elif kind in {"bootstrap_guarded", "freeze_reasserted"}:
                reason = str(payload.get("reason", "") or "")
                if reason:
                    last_freeze_reason = reason
                if last_lifecycle != LifecycleState.HALTED.value:
                    last_lifecycle = LifecycleState.FROZEN.value
            elif kind == "halt_reasserted":
                last_lifecycle = LifecycleState.HALTED.value
                reason = str(payload.get("reason", "") or "")
                if reason:
                    last_halt_reason = reason
                manual_rearm_required = True
            elif kind in {"symbol_frozen", "symbol_freeze_reasserted"}:
                symbol = str(payload.get("symbol", "") or "").upper()
                reason = str(payload.get("reason", "") or "")
                if symbol and reason:
                    symbol_guards[symbol] = reason
            elif kind == "symbol_unfrozen":
                symbol = str(payload.get("symbol", "") or "").upper()
                if symbol:
                    symbol_guards.pop(symbol, None)
            elif kind in {"venue_frozen", "venue_freeze_reasserted"}:
                venue = str(payload.get("venue", "") or "").upper()
                reason = str(payload.get("reason", "") or "")
                if venue and reason:
                    venue_guards[venue] = reason
            elif kind == "venue_unfrozen":
                venue = str(payload.get("venue", "") or "").upper()
                if venue:
                    venue_guards.pop(venue, None)
            elif kind in {"strategy_frozen", "strategy_freeze_reasserted"}:
                strategy_id = str(payload.get("strategy_id", "") or "").strip()
                symbol = str(payload.get("symbol", "") or "").upper()
                reason = str(payload.get("reason", "") or "")
                if strategy_id and reason:
                    if symbol:
                        strategy_symbol_guards[f"{strategy_id}|{symbol}"] = reason
                    else:
                        strategy_guards[strategy_id] = reason
            elif kind == "strategy_symbol_frozen":
                strategy_id = str(payload.get("strategy_id", "") or "").strip()
                symbol = str(payload.get("symbol", "") or "").upper()
                reason = str(payload.get("reason", "") or "")
                if strategy_id and symbol and reason:
                    strategy_symbol_guards[f"{strategy_id}|{symbol}"] = reason
            elif kind == "strategy_unfrozen":
                strategy_id = str(payload.get("strategy_id", "") or "").strip()
                symbol = str(payload.get("symbol", "") or "").upper()
                if strategy_id and symbol:
                    strategy_symbol_guards.pop(f"{strategy_id}|{symbol}", None)
                elif strategy_id:
                    strategy_guards.pop(strategy_id, None)
            elif kind == "trading_mode_override_set":
                mode_override = str(payload.get("mode", "") or "")
                mode_override_reason = str(payload.get("reason", "") or "")
                constraint_key = str(
                    payload.get("constraint_key", "")
                    or self._mode_constraint_key(mode_override_reason)
                )
                if mode_override and mode_override_reason:
                    mode_constraints[constraint_key] = {
                        "mode": mode_override,
                        "reason": mode_override_reason,
                    }
            elif kind == "trading_mode_override_cleared":
                cleared_keys = payload.get("cleared_constraint_keys", []) or []
                if cleared_keys:
                    for constraint_key in cleared_keys:
                        mode_constraints.pop(str(constraint_key), None)
                else:
                    previous_reason = str(payload.get("previous_reason", "") or "")
                    if previous_reason:
                        mode_constraints.pop(
                            self._mode_constraint_key(previous_reason),
                            None,
                        )
                    else:
                        mode_constraints.clear()
                if mode_constraints:
                    _selected_key, selected = max(
                        mode_constraints.items(),
                        key=lambda item: (
                            self._mode_rank(OMSCapabilityMode(item[1]["mode"])),
                            item[0],
                        ),
                    )
                    mode_override = selected["mode"]
                    mode_override_reason = selected["reason"]
                else:
                    mode_override = ""
                    mode_override_reason = ""
            elif kind == "oms_stopped":
                state = payload.get("state")
                if state:
                    last_lifecycle = state
                if payload.get("manual_rearm_required") is True:
                    manual_rearm_required = True
            elif kind == "trade_cursor_advanced":
                symbol = str(payload.get("symbol", "") or "").upper()
                trade_id = int(payload.get("trade_id", -1))
                if symbol:
                    trade_cursors[symbol] = max(trade_cursors.get(symbol, -1), trade_id)
            elif kind == "trade_scan_completed":
                symbol = str(payload.get("symbol", "") or "").upper()
                end_time_ms = int(payload.get("end_time_ms", 0))
                if symbol:
                    trade_scan_end_ms[symbol] = max(
                        trade_scan_end_ms.get(symbol, 0),
                        end_time_ms,
                    )
            elif kind == "external_cash_flow_record":
                income_id = str(payload.get("income_id", "") or "")
                if income_id in external_cash_flow_ids:
                    continue
                external_cash_flow_ids.add(income_id)
                external_cash_flow_total += float(payload.get("amount", 0.0) or 0.0)
            elif kind == "cash_flow_scan_completed":
                external_cash_flow_scan_end_ms = max(
                    external_cash_flow_scan_end_ms,
                    int(payload.get("end_time_ms", 0) or 0),
                )

        latest_command_results = {}
        pending_commands = 0
        for entry in commands.values():
            prepared = entry.get("command_prepared")
            result = entry.get("command_result")
            if prepared and not result:
                prepared_payload = prepared["payload"]
                client_oid = str(prepared_payload.get("client_oid", "") or "")
                snapshot = latest_order_records.get(client_oid, {})
                snapshot_index = latest_order_record_indexes.get(client_oid, -1)
                snapshot_status = str(snapshot.get("status", "") or "")
                ambiguous_statuses = {
                    OrderStatus.SUBMITTING.value,
                    OrderStatus.SUBMIT_UNKNOWN.value,
                    OrderStatus.CANCELLING.value,
                    OrderStatus.CANCEL_UNKNOWN.value,
                }
                if (
                    not snapshot
                    or snapshot_index <= prepared["index"]
                    or snapshot_status in ambiguous_statuses
                ):
                    pending_commands += 1
            if not result:
                continue
            result_payload = result["payload"]
            client_oid = str(result_payload.get("client_oid", "") or "")
            if not client_oid:
                continue
            current = latest_command_results.get(client_oid)
            if current is None or result["index"] > current["index"]:
                latest_command_results[client_oid] = result

        with self.lock:
            self.orders.clear()
            self.exchange_id_map.clear()
            self.execution_ids.clear()
            self.terminated_oids.clear()
            self.terminated_oid_queue.clear()
            self.exposure.strategy_net_positions.clear()
            self.exposure.strategy_avg_prices.clear()
            self.exposure.strategy_open_buy_qty.clear()
            self.exposure.strategy_open_sell_qty.clear()
            replayed_strategy_executions = set()
            for execution in execution_records:
                payload = execution["payload"]
                execution_id = str(payload.get("execution_id", "") or "")
                if not execution_id:
                    raise JournalCorruptionError(
                        "Execution record without execution_id during strategy replay"
                    )
                if execution_id in replayed_strategy_executions:
                    continue
                client_oid = str(payload.get("client_oid", "") or "")
                order_payload = latest_order_records.get(client_oid, {})
                intent_payload = order_payload.get("intent", {})
                strategy_id = str(
                    payload.get("strategy_id", "")
                    or intent_payload.get("strategy_id", "")
                    or "exchange_recovery"
                )
                symbol = str(
                    payload.get("symbol", "")
                    or intent_payload.get("symbol", "")
                ).upper()
                side_value = str(
                    payload.get("side", "")
                    or intent_payload.get("side", "")
                )
                try:
                    side = Side(side_value)
                    fill_qty = float(payload.get("fill_qty", 0.0) or 0.0)
                    fill_price = float(payload.get("fill_price", 0.0) or 0.0)
                except (TypeError, ValueError) as exc:
                    raise JournalCorruptionError(
                        f"Malformed strategy execution {execution_id}: {exc}"
                    ) from exc
                if (
                    not symbol
                    or fill_qty <= 0.0
                    or fill_price <= 0.0
                    or not math.isfinite(fill_qty)
                    or not math.isfinite(fill_price)
                ):
                    raise JournalCorruptionError(
                        f"Invalid strategy execution values for {execution_id}"
                    )
                self.exposure.on_strategy_fill(
                    strategy_id,
                    symbol,
                    side,
                    fill_qty,
                    fill_price,
                )
                replayed_strategy_executions.add(execution_id)
                self.execution_ids.add(execution_id)
            recovered_terminal_ids = 0
            recovered_active_orders = 0
            for client_oid, payload in latest_order_records.items():
                try:
                    order = Order.from_record(payload)
                except (KeyError, TypeError, ValueError) as exc:
                    raise JournalCorruptionError(
                        f"Invalid order snapshot for {client_oid}: {exc}"
                    ) from exc

                trailing_result = latest_command_results.get(client_oid)
                if (
                    trailing_result
                    and trailing_result["index"] > latest_order_record_indexes[client_oid]
                ):
                    self._apply_recovered_command_result(
                        order,
                        trailing_result["payload"],
                    )

                for execution in executions_by_client_oid.get(client_oid, []):
                    self.execution_ids.add(
                        str(execution["payload"].get("execution_id", "") or "")
                    )
                    if execution["index"] > latest_order_record_indexes[client_oid]:
                        self._apply_recovered_execution(order, execution["payload"])

                # PREPARED/SUBMITTING and PREPARED/CANCELLING are deliberately
                # ambiguous after a process crash. They must be queried by the
                # durable idempotency key and never blindly resent.
                if order.status == OrderStatus.SUBMITTING:
                    order.mark_submit_unknown("recovered_inflight_submit")
                elif order.status == OrderStatus.CANCELLING:
                    order.mark_cancel_unknown("recovered_inflight_cancel")

                if order.is_active():
                    self.orders[order.client_oid] = order
                    if order.exchange_oid:
                        self.exchange_id_map[order.exchange_oid] = order
                    self.order_monitor.recover_order(order)
                    recovered_active_orders += 1
                    continue

                if order.is_terminal():
                    if order.client_oid:
                        self._remember_terminated_oid(order.client_oid)
                        recovered_terminal_ids += 1
                    if order.exchange_oid:
                        self._remember_terminated_oid(order.exchange_oid)
                        recovered_terminal_ids += 1

            self.exposure.update_open_orders(self.orders)
            self.account.calculate()

        summary = {
            "records": len(records),
            "recovered_orders": len(latest_order_records),
            "recovered_active_orders": recovered_active_orders,
            "recovered_terminal_ids": recovered_terminal_ids,
            "pending_commands": pending_commands,
            "last_lifecycle": last_lifecycle,
            "last_freeze_reason": last_freeze_reason,
            "last_halt_reason": last_halt_reason,
            "manual_rearm_required": manual_rearm_required,
            "symbol_guards": symbol_guards,
            "venue_guards": venue_guards,
            "strategy_guards": strategy_guards,
            "strategy_symbol_guards": strategy_symbol_guards,
            "mode_override": mode_override,
            "mode_override_reason": mode_override_reason,
            "mode_constraints": mode_constraints,
            "clean_shutdown": clean_shutdown,
            "dirty_shutdown": not clean_shutdown,
            "trade_cursors": trade_cursors,
            "trade_scan_end_ms": trade_scan_end_ms,
            "external_cash_flow_total": external_cash_flow_total,
            "external_cash_flow_ids": sorted(external_cash_flow_ids),
            "external_cash_flow_scan_end_ms": external_cash_flow_scan_end_ms,
        }
        if recovered_terminal_ids:
            logger.info(
                f"[OMS] Recovered {recovered_terminal_ids} terminal IDs from journal"
            )
        return summary

    def _normalize_remote_open_orders(self, remote_orders):
        tracked_symbols = set(self.config.get("symbols", []))
        normalized = []
        for order in remote_orders:
            symbol = order.get("symbol")
            if tracked_symbols and symbol not in tracked_symbols:
                continue
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
                    "side": order.get("side", ""),
                }
            )
        normalized.sort(key=lambda item: (item["symbol"], item["identifiers"], item["side"]))
        return normalized

    def _collect_local_active_orders_locked(self):
        tracked_symbols = set(self.config.get("symbols", []))
        normalized = []
        for order in self.orders.values():
            if not order.is_active():
                continue
            if tracked_symbols and order.intent.symbol not in tracked_symbols:
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
                    "symbol": order.intent.symbol,
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
        maker_fee = float(fee_config.get("maker_fee", 0.0))
        if order.intent.is_rpi:
            symbol_rates = fee_config.get("rpi_commission_rates", {})
            symbol_rate = (
                symbol_rates.get(order.intent.symbol)
                if isinstance(symbol_rates, dict)
                else None
            )
            maker_fee += float(
                symbol_rate
                if symbol_rate is not None
                else fee_config.get("rpi_commission_rate", 0.0)
            )
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
        }

    def _audit(self, kind: str, **payload):
        payload.setdefault("state", self.state.value)
        payload.setdefault("capability_mode", self.capability_mode.value)
        payload.setdefault("capability_reason", self.capability_reason)
        payload.setdefault("mode_override", self.mode_override.value if self.mode_override else "")
        payload.setdefault("mode_override_reason", self.mode_override_reason)
        self.journal.append(kind, payload)

    def _record_order_snapshot(self, order: Order, source: str, **extra):
        payload = order.to_record()
        payload["source"] = source
        if extra:
            payload["extra"] = extra
        self.journal.append("order_snapshot", payload)

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
