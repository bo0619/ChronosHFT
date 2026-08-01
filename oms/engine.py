from collections.abc import Mapping
import json
import math
import time

from .account_truth import OMSAccountTruth
from .audit_logger import OMSAuditLogger
from .background_tasks import OMSBackgroundTaskManager
from .capability_manager import OMSCapabilityManager
from .cancellation_manager import OMSCancellationManager
from .component import component_method
from .durability_manager import OMSDurabilityManager
from .guard_manager import OMSGuardManager
from .initializer import OMSInitializer
from .lifecycle_controller import OMSLifecycleController
from .order import Order
from .order_accounting import OMSOrderAccounting
from .order_policy import OMSOrderPolicy
from .rpi_calibration_manager import RpiCalibrationManager
from .rpi_calibration_replay import RpiCalibrationReplay
from .rpi_calibration_runtime import RpiCalibrationRuntime
from .submit_settlement import OMSSubmitSettlement
from .state_publisher import OMSStatePublisher


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
    RPI_CALIBRATION_PERMIT_KEYS = RpiCalibrationManager.RPI_CALIBRATION_PERMIT_KEYS
    RPI_CALIBRATION_POLICY_KEYS = RpiCalibrationManager.RPI_CALIBRATION_POLICY_KEYS
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
        "background_task_manager": OMSBackgroundTaskManager,
        "capability_manager": OMSCapabilityManager,
        "cancellation_manager": OMSCancellationManager,
        "durability_manager": OMSDurabilityManager,
        "lifecycle_controller": OMSLifecycleController,
        "order_accounting": OMSOrderAccounting,
        "order_policy": OMSOrderPolicy,
        "state_publisher": OMSStatePublisher,
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
        OMSInitializer(self).initialize(event_engine, gateway, config)

    def record_paper_order_event(self, snapshot) -> bool:
        database = getattr(self, "paper_trade_database", None)
        if database is None:
            return False
        client_oid = str(getattr(snapshot, "client_oid", "") or "")
        with self.lock:
            order = self.orders.get(client_oid)
            intent = order.intent if order is not None else None
            strategy_id = str(intent.strategy_id or "") if intent else ""
            order_type = str(intent.order_type or "") if intent else ""
            reduce_only = bool(intent.reduce_only) if intent else False
            tag = str(intent.tag or "") if intent else ""
        status = getattr(snapshot, "status", "")
        side = getattr(snapshot, "side", "")
        return database.record_order_event(
            {
                "client_oid": client_oid,
                "exchange_oid": str(
                    getattr(snapshot, "exchange_oid", "") or ""
                ),
                "symbol": str(getattr(snapshot, "symbol", "") or ""),
                "strategy_id": strategy_id,
                "side": getattr(side, "value", side) or "",
                "status": getattr(status, "value", status) or "",
                "price": getattr(snapshot, "price", 0.0),
                "quantity": getattr(snapshot, "volume", 0.0),
                "filled_quantity": getattr(snapshot, "filled_volume", 0.0),
                "average_price": getattr(snapshot, "avg_price", 0.0),
                "time_in_force": str(
                    getattr(snapshot, "time_in_force", "") or ""
                ),
                "is_post_only": bool(
                    getattr(snapshot, "is_post_only", False)
                ),
                "is_rpi": bool(getattr(snapshot, "is_rpi", False)),
                "order_type": order_type,
                "reduce_only": reduce_only,
                "tag": tag,
                "created_monotonic": getattr(
                    snapshot,
                    "created_monotonic",
                    None,
                ),
                "updated_monotonic": getattr(
                    snapshot,
                    "updated_monotonic",
                    None,
                ),
                "event_time": getattr(snapshot, "update_time", None),
                "error_message": str(
                    getattr(snapshot, "error_msg", "") or ""
                ),
            }
        )

    def record_paper_strategy_sample(self, strategy_data) -> bool:
        database = getattr(self, "paper_trade_database", None)
        if database is None:
            return False
        return database.record_strategy_sample(
            {
                "sample_time": getattr(strategy_data, "timestamp", None),
                "symbol": str(getattr(strategy_data, "symbol", "") or ""),
                "fair_value": getattr(strategy_data, "fair_value", None),
                "alpha_bps": getattr(strategy_data, "alpha_bps", None),
                "params": dict(getattr(strategy_data, "params", {}) or {}),
            }
        )

    def record_paper_markout(self, payload: dict) -> bool:
        database = getattr(self, "paper_trade_database", None)
        if database is None:
            return False
        return database.record_markout(payload)

    def record_paper_account_sample(self, account_data) -> bool:
        database = getattr(self, "paper_trade_database", None)
        if database is None:
            return False
        sample_datetime = getattr(account_data, "datetime", None)
        timestamp_method = getattr(sample_datetime, "timestamp", None)
        sample_time = (
            timestamp_method() if callable(timestamp_method) else time.time()
        )
        balance = getattr(account_data, "balance", 0.0)
        equity = getattr(account_data, "equity", 0.0)
        return database.record_account_sample(
            {
                "sample_time": sample_time,
                "balance": balance,
                "equity": equity,
                "unrealized_pnl": float(equity) - float(balance),
                "available": getattr(account_data, "available", 0.0),
                "used_margin": getattr(account_data, "used_margin", 0.0),
                "budget_balance": getattr(
                    account_data,
                    "budget_balance",
                    0.0,
                ),
                "budget_available": getattr(
                    account_data,
                    "budget_available",
                    0.0,
                ),
                "maintenance_margin": getattr(
                    account_data,
                    "maintenance_margin",
                    0.0,
                ),
                "margin_balance": getattr(
                    account_data,
                    "margin_balance",
                    0.0,
                ),
                "maintenance_margin_ratio": getattr(
                    account_data,
                    "maintenance_margin_ratio",
                    0.0,
                ),
                "margin_snapshot_time": getattr(
                    account_data,
                    "margin_snapshot_time",
                    None,
                ),
                "margin_snapshot_synced": bool(
                    getattr(account_data, "margin_snapshot_synced", False)
                ),
                "external_cash_flow_total": getattr(
                    account_data,
                    "external_cash_flow_total",
                    0.0,
                ),
                "cash_flow_snapshot_time": getattr(
                    account_data,
                    "cash_flow_snapshot_time",
                    None,
                ),
                "cash_flow_snapshot_synced": bool(
                    getattr(account_data, "cash_flow_snapshot_synced", False)
                ),
            }
        )

    def record_paper_system_event(self, event_kind: str, data) -> bool:
        database = getattr(self, "paper_trade_database", None)
        if database is None:
            return False
        event_kind = str(event_kind or "system_health")
        is_mapping = isinstance(data, Mapping)

        def read_value(name, default=None):
            if is_mapping:
                return data.get(name, default)
            return getattr(data, name, default)

        message = data if isinstance(data, str) else read_value("msg", "")
        if not message:
            message = read_value("message", "")
        if not message and is_mapping and data.get("details") is not None:
            try:
                message = json.dumps(
                    data["details"],
                    allow_nan=False,
                    default=str,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            except (TypeError, ValueError):
                message = "system_event_details_unserializable"
        message = str(message or "")
        if len(message) > 16_384:
            message = message[:16_384]
        severity = str(read_value("level", "") or "").upper()
        if not severity:
            if message.startswith(("HALT:", "KILL:", "MARKET_DATA_STALE:")):
                severity = "CRITICAL"
            elif message.startswith(
                (
                    "FREEZE_",
                    "WS_",
                    "USER_STREAM_",
                    "PAPER_DMS_",
                )
            ):
                severity = "ERROR"
            else:
                severity = "INFO"
        state = read_value("state", "")
        state = getattr(state, "value", state) or ""
        return database.record_system_event(
            {
                "event_time": (
                    read_value("event_time", None)
                    or read_value("timestamp", None)
                    or time.time()
                ),
                "event_kind": event_kind,
                "severity": severity,
                "message": message,
                "state": str(state),
                "total_exposure": read_value("total_exposure", None),
                "margin_ratio": read_value("margin_ratio", None),
                "order_count_local": read_value("order_count_local", None),
                "order_count_remote": read_value("order_count_remote", None),
                "cancelling_count": read_value("cancelling_count", None),
                "fill_ratio": read_value("fill_ratio", None),
                "api_weight": read_value(
                    "api_weight",
                    read_value("weight_used_1m", None),
                ),
                "is_sync_error": read_value("is_sync_error", None),
            }
        )

    def record_paper_market_sample(self, market_data) -> bool:
        database = getattr(self, "paper_trade_database", None)
        if database is None:
            return False
        try:
            mark_price = float(getattr(market_data, "mark_price", 0.0))
            index_price = float(getattr(market_data, "index_price", 0.0))
        except (TypeError, ValueError):
            return False
        if (
            not math.isfinite(mark_price)
            or not math.isfinite(index_price)
            or mark_price <= 0.0
            or index_price <= 0.0
        ):
            return False

        exchange_time = float(
            getattr(market_data, "exchange_timestamp", 0.0) or 0.0
        )
        received_time = float(
            getattr(market_data, "received_timestamp", 0.0) or 0.0
        )
        corrected_received_time = float(
            getattr(
                market_data,
                "corrected_received_timestamp",
                0.0,
            )
            or 0.0
        )
        received_monotonic = float(
            getattr(market_data, "received_monotonic", 0.0) or 0.0
        )
        dispatch_monotonic = float(
            getattr(market_data, "dispatch_monotonic", 0.0) or 0.0
        )
        transport_latency_ms = (
            (corrected_received_time - exchange_time) * 1000.0
            if corrected_received_time > 0.0 and exchange_time > 0.0
            else None
        )
        gateway_processing_latency_ms = (
            (dispatch_monotonic - received_monotonic) * 1000.0
            if dispatch_monotonic >= received_monotonic > 0.0
            else None
        )
        next_funding_time = getattr(
            market_data,
            "next_funding_timestamp",
            0.0,
        )
        if not next_funding_time:
            value = getattr(market_data, "next_funding_time", None)
            timestamp_method = getattr(value, "timestamp", None)
            next_funding_time = (
                timestamp_method() if callable(timestamp_method) else None
            )
        return database.record_market_sample(
            {
                "sample_time": (
                    corrected_received_time or received_time or time.time()
                ),
                "symbol": str(getattr(market_data, "symbol", "") or ""),
                "mark_price": mark_price,
                "index_price": index_price,
                "basis_bps": math.log(mark_price / index_price) * 10_000.0,
                "funding_rate": getattr(market_data, "funding_rate", 0.0),
                "next_funding_time": next_funding_time,
                "exchange_time": exchange_time or None,
                "received_time": received_time or None,
                "corrected_received_time": corrected_received_time or None,
                "dispatch_time": getattr(
                    market_data,
                    "dispatch_timestamp",
                    None,
                ),
                "received_monotonic": received_monotonic or None,
                "dispatch_monotonic": dispatch_monotonic or None,
                "clock_offset_ms": getattr(
                    market_data,
                    "clock_offset_ms",
                    None,
                ),
                "transport_latency_ms": transport_latency_ms,
                "gateway_processing_latency_ms": (
                    gateway_processing_latency_ms
                ),
            }
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
    _parse_utc_exchange_ns = staticmethod(RpiCalibrationManager._parse_utc_exchange_ns)
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
    _acquire_outbound_order_send_permit_locked = component_method(
        "lifecycle_controller"
    )
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

    _rpi_calibration_active_orders_locked = component_method("rpi_calibration_runtime")

    @staticmethod
    def _exchange_ns_to_iso(value: int) -> str:
        return RpiCalibrationRuntime._exchange_ns_to_iso(value)

    _rpi_calibration_snapshot_locked = component_method("rpi_calibration_runtime")

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

    _rpi_calibration_equity_truth_locked = component_method("rpi_calibration_runtime")
    _observe_rpi_calibration_loss_locked = component_method("rpi_calibration_runtime")
    _rpi_calibration_activation_payload_locked = component_method(
        "rpi_calibration_runtime"
    )
    _activate_rpi_calibration_permit_locked = component_method(
        "rpi_calibration_runtime"
    )
    _expire_rpi_calibration_permit_locked = component_method("rpi_calibration_runtime")
    expire_rpi_calibration_permit = component_method("rpi_calibration_runtime")
    _validate_rpi_calibration_sample_locked = component_method(
        "rpi_calibration_runtime"
    )
    _reserve_rpi_calibration_sample_locked = component_method("rpi_calibration_runtime")
    _audit_rpi_calibration_emergency_bypass_locked = component_method(
        "rpi_calibration_runtime"
    )
    _mark_rpi_calibration_terminal_pending = component_method("rpi_calibration_runtime")
    _enforce_rpi_calibration_terminal_once = component_method("rpi_calibration_runtime")
    enforce_rpi_calibration_runtime_limits = component_method("rpi_calibration_runtime")
    _schedule_rpi_calibration_runtime_enforcement = component_method(
        "rpi_calibration_runtime"
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
    _venue_dead_man_switch_renewal_allowed_locked = component_method(
        "capability_manager"
    )
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

    _record_command_prepared = component_method(
        "audit_logger",
        "record_command_prepared",
    )
    _command_prepared_payload = staticmethod(OMSAuditLogger.command_prepared_payload)
    _build_submit_prepared_records = component_method(
        "audit_logger",
        "build_submit_prepared_records",
    )
    _record_submit_prepared_batch = component_method(
        "audit_logger",
        "record_submit_prepared_batch",
    )

    _record_command_result = component_method("durability_manager")
    record_strategy_evidence = component_method("durability_manager")

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

    _latch_journal_failure_locked = component_method("durability_manager")
    _latch_journal_failure = component_method("durability_manager")
    _fail_closed_on_journal_error = component_method("durability_manager")

    adapt_intent_for_trading_mode = component_method("order_policy")
    _estimate_emergency_price = component_method("order_policy")
    emergency_reduce_only_flatten = component_method("order_policy")
    _get_order_block_reason = component_method("order_policy")
    _get_submission_safety_reason_locked = component_method("order_policy")
    _get_paper_database_rejection_locked = component_method("order_policy")
    _get_clock_health_rejection_locked = component_method("order_policy")

    _get_self_trade_prevention_rejection_locked = component_method("order_policy")
    _get_risk_control_heartbeat_rejection_locked = component_method("order_policy")
    _get_venue_dead_man_switch_rejection_locked = component_method("order_policy")
    _get_margin_health_rejection_locked = component_method("order_policy")
    _get_strategy_budget_rejection_locked = component_method("order_policy")
    get_strategy_risk_budget_snapshot = component_method("order_policy")

    _submit_internal_order = component_method("order_submission")

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

    _latch_background_task_failure = component_method("background_task_manager")
    _on_background_task_error = component_method("background_task_manager")
    _submit_background_task = component_method("background_task_manager")
    get_background_task_snapshot = component_method("background_task_manager")

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
    _normalize_remote_open_orders = component_method("reconciler")
    _collect_local_active_orders_locked = component_method("reconciler")
    _collect_exchange_position_drift_locked = component_method("reconciler")
    _has_active_orders_locked = component_method("reconciler")

    submit_order = component_method("order_submission")
    _reject_intent_locally = component_method("order_submission")

    cancel_order = component_method("cancellation_manager")
    _schedule_cancel_order_retry = component_method("cancellation_manager")
    _schedule_cancel_all_retry = component_method("cancellation_manager")
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

    _journal_decimal = component_method("rpi_calibration_replay")

    _new_rpi_calibration_replay_state = staticmethod(
        RpiCalibrationReplay._new_rpi_calibration_replay_state
    )

    _verify_replayed_rpi_calibration_permit = component_method("rpi_calibration_replay")
    _replay_rpi_calibration_activation = component_method("rpi_calibration_replay")
    _replay_rpi_calibration_reservation = component_method("rpi_calibration_replay")
    _replay_rpi_calibration_expiry = component_method("rpi_calibration_replay")
    _replay_rpi_calibration_bypass = component_method("rpi_calibration_replay")
    _replay_rpi_calibration_record = component_method("rpi_calibration_replay")
    _finalize_rpi_calibration_replay = component_method("rpi_calibration_replay")
    rebuild_from_log = component_method("journal_rebuilder")

    _extract_quote_asset = component_method("order_accounting")
    _tracked_quote_assets = component_method("order_accounting")
    _get_fill_commission = component_method("order_accounting")
    _get_fee_rate = component_method("order_accounting")

    _serialize_intent = component_method("state_publisher")
    _audit = component_method("state_publisher")
    record_rpi_commission_truth = component_method("state_publisher")
    _record_order_snapshot = component_method(
        "audit_logger",
        "record_order_snapshot",
    )
    _emit_order_update = component_method("state_publisher")
    _emit_position_update = component_method("state_publisher")

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
