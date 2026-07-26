"""Runtime enforcement for the signed RPI calibration canary."""

from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from data.cache import data_cache
from infrastructure.logger import logger
from infrastructure.time_service import time_service

from event.type import (
    ExecutionPolicy,
    OrderIntent,
    OrderRequest,
    OrderStatus,
    Side,
    TIF_RPI,
)

from .component import OMSComponent
from .journal import JournalCorruptionError, JournalError
from .rpi_calibration_manager import RpiCalibrationManager


class RpiCalibrationRuntime(OMSComponent):
    """Own live calibration quotas, loss caps and terminal verification."""

    USDT_MICRO_SCALE = RpiCalibrationManager.USDT_MICRO_SCALE
    _finite_decimal = staticmethod(RpiCalibrationManager._finite_decimal)

    def _rpi_calibration_active_orders_locked(self) -> list:
        if not self._rpi_calibration["enabled"]:
            return []
        symbol = self._rpi_calibration["symbol"]
        active_orders = []
        for order in self.orders.values():
            if not order.is_active():
                continue
            intent = order.intent
            has_calibration_metadata = bool(
                str(
                    getattr(intent, "calibration_permit_id", "")
                    or ""
                ).strip()
            )
            is_calibration_flow = (
                str(intent.symbol or "").upper() == symbol
                and str(intent.strategy_id or "")
                == self.RPI_CALIBRATION_STRATEGY_ID
                and (
                    has_calibration_metadata
                    or intent.time_in_force == TIF_RPI
                )
            )
            if is_calibration_flow:
                active_orders.append(order)
        return active_orders

    @staticmethod
    def _exchange_ns_to_iso(value: int) -> str:
        if value <= 0:
            return ""
        return datetime.fromtimestamp(
            value / 1_000_000_000,
            tz=timezone.utc,
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _rpi_calibration_snapshot_locked(self) -> dict:
        config = self._rpi_calibration
        if not config["enabled"]:
            return {
                "enabled": False,
                "permit_id": "",
                "permit_sha256": "",
                "expired": False,
                "budget_exhausted": False,
                "reserved_order_count": 0,
                "cumulative_submitted_notional_usdt": 0.0,
                "last_reserved_exchange_time": 0.0,
                "active_order_count": 0,
                "max_order_count": 0,
                "max_cumulative_submitted_notional_usdt": 0.0,
                "expires_at": "",
            }
        active_orders = self._rpi_calibration_active_orders_locked()
        permit_reserved_count = max(
            0,
            self._rpi_calibration_reserved_order_count
            - self._rpi_calibration_permit_start_order_count,
        )
        permit_cumulative_microu = max(
            0,
            self._rpi_calibration_cumulative_notional_microu
            - self._rpi_calibration_permit_start_notional_microu,
        )
        quota_exhausted = bool(
            self._rpi_calibration_budget_exhausted
            or permit_reserved_count >= config["max_order_count"]
            or (
                permit_cumulative_microu
                + config["min_order_notional_microu"]
                > config["max_cumulative_notional_microu"]
            )
        )
        return {
            "enabled": True,
            "stage": self.RPI_CALIBRATION_STAGE,
            "permit_id": config["permit_id"],
            "permit_sha256": config["permit_sha256"],
            "deployment_id": config["deployment_id"],
            "symbol": config["symbol"],
            "expired": self._rpi_calibration_expired,
            "expiry_reason": self._rpi_calibration_expiry_reason,
            "restart_rearm_blocked": (
                self._rpi_calibration_restart_rearm_blocked
            ),
            "budget_exhausted": quota_exhausted,
            "permit_activated": self._rpi_calibration_permit_activated,
            "reserved_order_count": (
                self._rpi_calibration_reserved_order_count
            ),
            "permit_reserved_order_count": permit_reserved_count,
            "cumulative_submitted_notional_usdt": (
                self._rpi_calibration_cumulative_notional_microu / 1_000_000
            ),
            "permit_cumulative_submitted_notional_usdt": (
                permit_cumulative_microu / 1_000_000
            ),
            "last_reserved_exchange_time": (
                self._rpi_calibration_last_reserved_exchange_ns
                / 1_000_000_000
            ),
            "last_reserved_exchange_time_utc": self._exchange_ns_to_iso(
                self._rpi_calibration_last_reserved_exchange_ns
            ),
            "active_order_count": len(active_orders),
            "active_or_unknown_client_oids": sorted(
                order.client_oid for order in active_orders
            ),
            "max_active_orders": config["max_active_orders"],
            "max_order_count": config["max_order_count"],
            "min_order_notional_usdt": (
                config["min_order_notional_microu"] / 1_000_000
            ),
            "max_order_notional_usdt": (
                config["max_order_notional_microu"] / 1_000_000
            ),
            "max_cumulative_submitted_notional_usdt": (
                config["max_cumulative_notional_microu"] / 1_000_000
            ),
            "max_calibration_loss_usdt": (
                self._rpi_calibration_effective_loss_cap_microu
                / 1_000_000
            ),
            "signed_max_calibration_loss_usdt": (
                config["max_calibration_loss_microu"] / 1_000_000
            ),
            "peak_observed_calibration_loss_usdt": (
                self._rpi_calibration_peak_observed_loss_microu / 1_000_000
            ),
            "deployment_start_equity_usdt": (
                self._rpi_calibration_start_equity_microu / 1_000_000
            ),
            "fixed_depths_bps": list(config["fixed_depths_bps"]),
            "order_ttl_sec": float(config["order_ttl_sec"]),
            "min_order_interval_sec": float(
                config["min_order_interval_sec"]
            ),
            "not_before": config["not_before"],
            "expires_at": config["expires_at"],
            "calibration_config_sha256": (
                config["calibration_config_sha256"]
            ),
            "target_deployment_config_sha256": (
                config["target_deployment_config_sha256"]
            ),
            "strategy_policy_sha256": config["strategy_policy_sha256"],
            "implementation_sha256": config["implementation_sha256"],
            "terminal_convergence_verified": bool(
                getattr(
                    self,
                    "_rpi_calibration_terminal_verified",
                    False,
                )
            ),
            "terminal_empty_snapshots": int(
                getattr(
                    self,
                    "_rpi_calibration_terminal_empty_snapshots",
                    0,
                )
                or 0
            ),
            "terminal_pending_reason": str(
                getattr(
                    self,
                    "_rpi_calibration_terminal_pending_reason",
                    "",
                )
                or ""
            ),
            "terminal_generation": int(
                getattr(
                    self,
                    "_rpi_calibration_terminal_generation",
                    0,
                )
                or 0
            ),
        }
    @classmethod
    def _signed_usdt_to_microu(
        cls,
        value,
        *,
        rounding,
        context: str,
    ) -> int:
        parsed = cls._finite_decimal(value, context)
        return int(
            (parsed * cls.USDT_MICRO_SCALE).to_integral_value(
                rounding=rounding
            )
        )

    def _rpi_calibration_equity_truth_locked(self) -> tuple[str, int, int]:
        if not self.account.exchange_balance_synced:
            return "account_equity_not_exchange_synced", 0, 0
        if (
            not self.external_cash_flow_truth_enabled
            or not self.account.cash_flow_snapshot_synced
        ):
            return "external_cash_flow_truth_unavailable", 0, 0
        snapshot_monotonic = float(
            self.account.cash_flow_snapshot_monotonic or 0.0
        )
        if snapshot_monotonic <= 0.0:
            return "external_cash_flow_truth_timestamp_missing", 0, 0
        cash_flow_age_sec = max(
            0.0,
            time.perf_counter() - snapshot_monotonic,
        )
        if (
            self.external_cash_flow_max_age_sec > 0.0
            and cash_flow_age_sec > self.external_cash_flow_max_age_sec
        ):
            return "external_cash_flow_truth_stale", 0, 0

        try:
            balance = self._finite_decimal(
                self.account.balance,
                "RPI calibration account balance",
            )
            external_cash_flow = self._finite_decimal(
                self.account.external_cash_flow_total,
                "RPI calibration external cash flow",
            )
            unrealized_pnl = Decimal("0")
            now_monotonic = time.perf_counter()
            for symbol, raw_position in self.exposure.net_positions.items():
                position = self._finite_decimal(
                    raw_position,
                    f"RPI calibration position {symbol}",
                )
                if abs(position) <= Decimal("0.000000000001"):
                    continue
                average_price = self._positive_decimal(
                    self.exposure.avg_prices.get(symbol, 0.0),
                    f"RPI calibration average price {symbol}",
                )
                market = data_cache.get_risk_snapshot(
                    symbol,
                    now=now_monotonic,
                )
                mark_price = self._finite_decimal(
                    market.get("mark_price", 0.0),
                    f"RPI calibration mark price {symbol}",
                )
                mark_age_ms = market.get("mark_age_ms")
                mark_is_fresh = bool(
                    mark_price > 0
                    and mark_age_ms is not None
                    and float(mark_age_ms)
                    <= self._rpi_calibration["max_mark_age_ms"]
                )
                if mark_is_fresh:
                    valuation_price = mark_price
                else:
                    bid = self._finite_decimal(
                        market.get("bid_price", 0.0),
                        f"RPI calibration bid {symbol}",
                    )
                    ask = self._finite_decimal(
                        market.get("ask_price", 0.0),
                        f"RPI calibration ask {symbol}",
                    )
                    book_age_ms = market.get("book_age_ms")
                    if (
                        bid <= 0
                        or ask <= bid
                        or book_age_ms is None
                        or float(book_age_ms)
                        > self._rpi_calibration["max_book_age_ms"]
                    ):
                        return (
                            f"market_valuation_truth_unavailable:{symbol}",
                            0,
                            0,
                        )
                    valuation_price = (bid + ask) / Decimal("2")
                unrealized_pnl += (
                    valuation_price - average_price
                ) * position
            equity = balance + unrealized_pnl
            if equity <= 0:
                return "account_equity_non_positive", 0, 0
            equity_microu = self._signed_usdt_to_microu(
                equity,
                rounding=ROUND_FLOOR,
                context="RPI calibration equity",
            )
            external_cash_flow_microu = self._signed_usdt_to_microu(
                external_cash_flow,
                rounding=ROUND_CEILING,
                context="RPI calibration external cash flow",
            )
        except (TypeError, ValueError) as exc:
            return f"calibration_loss_truth_invalid:{exc}", 0, 0
        return "", equity_microu, external_cash_flow_microu

    def _observe_rpi_calibration_loss_locked(
        self,
        *,
        initialize_baseline: bool,
    ) -> tuple[str, int]:
        truth_reason, equity_microu, external_cash_flow_microu = (
            self._rpi_calibration_equity_truth_locked()
        )
        if truth_reason:
            return truth_reason, 0
        if self._rpi_calibration_start_equity_microu <= 0:
            if not initialize_baseline:
                return "calibration_loss_baseline_missing", 0
            # Current equity is floored above. One additional micro-USDT
            # makes the deployment baseline conservative even when the
            # unrounded value fell exactly on a micro boundary.
            self._rpi_calibration_start_equity_microu = equity_microu + 1
            self._rpi_calibration_start_external_cash_flow_microu = (
                self._signed_usdt_to_microu(
                    self.account.external_cash_flow_total,
                    rounding=ROUND_FLOOR,
                    context="RPI calibration baseline external cash flow",
                )
            )
        cash_flow_delta_microu = (
            external_cash_flow_microu
            - self._rpi_calibration_start_external_cash_flow_microu
        )
        adjusted_equity_microu = equity_microu - cash_flow_delta_microu
        observed_loss_microu = max(
            0,
            self._rpi_calibration_start_equity_microu
            - adjusted_equity_microu,
        )
        self._rpi_calibration_peak_observed_loss_microu = max(
            self._rpi_calibration_peak_observed_loss_microu,
            observed_loss_microu,
        )
        return "", observed_loss_microu

    def _rpi_calibration_activation_payload_locked(
        self,
        activated_at_exchange_ns: int,
    ) -> dict:
        config = self._rpi_calibration
        return {
            "schema": self.RPI_CALIBRATION_JOURNAL_SCHEMA,
            "signed_permit": config["signed_permit"],
            "permit_id": config["permit_id"],
            "permit_sha256": config["permit_sha256"],
            "deployment_id": config["deployment_id"],
            "stage": self.RPI_CALIBRATION_STAGE,
            "venue": self.RPI_CALIBRATION_VENUE,
            "symbol": config["symbol"],
            "model": self.RPI_CALIBRATION_MODEL,
            "calibration_config_sha256": (
                config["calibration_config_sha256"]
            ),
            "target_deployment_config_sha256": (
                config["target_deployment_config_sha256"]
            ),
            "strategy_policy_sha256": config["strategy_policy_sha256"],
            "implementation_sha256": config["implementation_sha256"],
            "activated_at_exchange_ns": activated_at_exchange_ns,
            "not_before_exchange_ns": config["not_before_ns"],
            "expires_at_exchange_ns": config["expires_at_ns"],
            "fixed_depths_bps": list(config["fixed_depths_bps"]),
            "order_ttl_ns": config["order_ttl_ns"],
            "min_order_interval_ns": config["min_order_interval_ns"],
            "max_active_orders": config["max_active_orders"],
            "max_order_count": config["max_order_count"],
            "min_order_notional_microu": (
                config["min_order_notional_microu"]
            ),
            "max_order_notional_microu": (
                config["max_order_notional_microu"]
            ),
            "max_cumulative_submitted_notional_microu": (
                config["max_cumulative_notional_microu"]
            ),
            "max_calibration_loss_microu": (
                config["max_calibration_loss_microu"]
            ),
            "effective_deployment_loss_cap_microu": (
                self._rpi_calibration_effective_loss_cap_microu
            ),
            "deployment_start_equity_microu": (
                self._rpi_calibration_start_equity_microu
            ),
            "deployment_start_external_cash_flow_microu": (
                self._rpi_calibration_start_external_cash_flow_microu
            ),
            "peak_observed_loss_microu": (
                self._rpi_calibration_peak_observed_loss_microu
            ),
            "starting_reserved_order_count": (
                self._rpi_calibration_reserved_order_count
            ),
            "starting_cumulative_submitted_notional_microu": (
                self._rpi_calibration_cumulative_notional_microu
            ),
        }

    def _activate_rpi_calibration_permit_locked(
        self,
        activated_at_exchange_ns: int,
    ) -> None:
        if self._rpi_calibration_permit_activated:
            return
        if self._rpi_calibration_start_equity_microu <= 0:
            raise JournalError(
                "RPI calibration loss baseline is not initialized"
            )
        committed_seq = self.audit_logger.audit(
            "rpi_calibration_permit_activated",
            self._rpi_calibration_activation_payload_locked(
                activated_at_exchange_ns
            ),
        )
        if not committed_seq:
            raise JournalError(
                "RPI calibration permit activation was not committed"
            )
        self._rpi_calibration_permit_activated = True
        self._rpi_calibration_permit_start_order_count = (
            self._rpi_calibration_reserved_order_count
        )
        self._rpi_calibration_permit_start_notional_microu = (
            self._rpi_calibration_cumulative_notional_microu
        )
        self._rpi_calibration_effective_loss_cap_microu = min(
            self._rpi_calibration_effective_loss_cap_microu,
            self._rpi_calibration["max_calibration_loss_microu"],
        )

    def _expire_rpi_calibration_permit_locked(
        self,
        reason: str,
        *,
        budget_exhausted: bool = False,
    ) -> bool:
        if not self._rpi_calibration["enabled"]:
            return False
        reason = str(reason or "rpi_calibration_expired")
        self._close_outbound_gate_locked(
            f"rpi_calibration:{reason}",
            hold="rpi_calibration_expired",
        )
        if self._rpi_calibration_expired:
            return False
        now_ns = time_service.now_ns()
        committed_seq = self.audit_logger.audit(
            "rpi_calibration_permit_expired",
            {
                "schema": self.RPI_CALIBRATION_JOURNAL_SCHEMA,
                "signed_permit": self._rpi_calibration["signed_permit"],
                "permit_id": self._rpi_calibration["permit_id"],
                "permit_sha256": self._rpi_calibration["permit_sha256"],
                "deployment_id": self._rpi_calibration["deployment_id"],
                "symbol": self._rpi_calibration["symbol"],
                "calibration_config_sha256": (
                    self._rpi_calibration["calibration_config_sha256"]
                ),
                "target_deployment_config_sha256": (
                    self._rpi_calibration[
                        "target_deployment_config_sha256"
                    ]
                ),
                "strategy_policy_sha256": (
                    self._rpi_calibration["strategy_policy_sha256"]
                ),
                "implementation_sha256": (
                    self._rpi_calibration["implementation_sha256"]
                ),
                "reason": reason,
                "budget_exhausted": bool(budget_exhausted),
                "expired_at_exchange_ns": now_ns,
                "reserved_order_count": (
                    self._rpi_calibration_reserved_order_count
                ),
                "cumulative_submitted_notional_microu": (
                    self._rpi_calibration_cumulative_notional_microu
                ),
                "deployment_start_equity_microu": (
                    self._rpi_calibration_start_equity_microu
                ),
                "deployment_start_external_cash_flow_microu": (
                    self._rpi_calibration_start_external_cash_flow_microu
                ),
                "peak_observed_loss_microu": (
                    self._rpi_calibration_peak_observed_loss_microu
                ),
                "effective_deployment_loss_cap_microu": (
                    self._rpi_calibration_effective_loss_cap_microu
                ),
            },
        )
        if not committed_seq:
            raise JournalError(
                "RPI calibration permit expiry was not committed"
            )
        self._rpi_calibration_expired = True
        self._rpi_calibration_expiry_reason = reason
        self._rpi_calibration_budget_exhausted = bool(
            self._rpi_calibration_budget_exhausted or budget_exhausted
        )
        self._rpi_calibration_restart_rearm_blocked = False
        self._rpi_calibration_terminal_cancel_sweep_completed = False
        self._rpi_calibration_terminal_empty_snapshots = 0
        self._rpi_calibration_terminal_verified = False
        self._rpi_calibration_terminal_pending_reason = reason
        self._rpi_calibration_terminal_generation += 1
        return True

    def expire_rpi_calibration_permit(
        self,
        reason: str = "operator_expired",
    ) -> bool:
        """Latch calibration closed, cancel samples, then reduce residual risk."""
        if not self._rpi_calibration["enabled"]:
            return False
        journal_ok = True
        try:
            with self.lock:
                config = self._rpi_calibration
                permit_reserved_count = (
                    self._rpi_calibration_reserved_order_count
                    - self._rpi_calibration_permit_start_order_count
                )
                permit_cumulative_microu = (
                    self._rpi_calibration_cumulative_notional_microu
                    - self._rpi_calibration_permit_start_notional_microu
                )
                quota_exhausted = bool(
                    "budget" in str(reason or "").lower()
                    or permit_reserved_count >= config["max_order_count"]
                    or (
                        permit_cumulative_microu
                        + config["min_order_notional_microu"]
                        > config["max_cumulative_notional_microu"]
                    )
                )
                self._expire_rpi_calibration_permit_locked(
                    reason,
                    budget_exhausted=quota_exhausted,
                )
        except Exception as exc:
            journal_ok = False
            self._fail_closed_on_journal_error(
                exc,
                "expire_rpi_calibration_permit",
                self._rpi_calibration["symbol"],
            )

        sends_drained = self._wait_for_outbound_risk_sends(
            f"rpi_calibration_expired:{reason}",
            self._rpi_calibration["symbol"],
        )
        journal_ok = bool(journal_ok and sends_drained)
        self._cancel_orders_matching(
            lambda order: (
                order.status != OrderStatus.SUBMITTING
                and order in self._rpi_calibration_active_orders_locked()
            )
        )
        self._schedule_rpi_calibration_runtime_enforcement(
            terminal_truth_changed=True,
        )
        return journal_ok

    def _validate_rpi_calibration_sample_locked(
        self,
        intent: OrderIntent,
        request: OrderRequest,
    ) -> tuple[str, int, str, str]:
        config = self._rpi_calibration
        if intent.strategy_id != self.RPI_CALIBRATION_STRATEGY_ID:
            return "rpi_calibration_strategy_mismatch", 0, "", ""
        if str(intent.symbol or "").upper() != config["symbol"]:
            return "rpi_calibration_symbol_mismatch", 0, "", ""
        if str(request.symbol or "").upper() != config["symbol"]:
            return "rpi_calibration_request_symbol_mismatch", 0, "", ""
        if intent.order_type != "LIMIT" or request.order_type != "LIMIT":
            return "rpi_calibration_requires_limit", 0, "", ""
        if (
            intent.time_in_force != TIF_RPI
            or request.time_in_force != TIF_RPI
        ):
            return "rpi_calibration_requires_rpi", 0, "", ""
        if not intent.is_post_only or not request.post_only:
            return "rpi_calibration_requires_post_only", 0, "", ""
        if intent.policy != ExecutionPolicy.PASSIVE:
            return "rpi_calibration_requires_passive_policy", 0, "", ""
        if bool(intent.reduce_only) != bool(request.reduce_only):
            return "rpi_calibration_reduce_only_mismatch", 0, "", ""

        permit_id = str(
            getattr(intent, "calibration_permit_id", "") or ""
        ).strip()
        if permit_id != config["permit_id"]:
            return "rpi_calibration_permit_id_mismatch", 0, "", ""
        try:
            depth = self._positive_decimal(
                getattr(intent, "calibration_depth_bps", None),
                "RPI calibration declared depth",
            )
            reference_mid = self._positive_decimal(
                getattr(intent, "calibration_reference_mid", None),
                "RPI calibration reference mid",
            )
            intent_price = self._positive_decimal(
                intent.price,
                "RPI calibration intent price",
            )
            request_price = self._positive_decimal(
                request.price,
                "RPI calibration request price",
            )
            intent_volume = self._positive_decimal(
                intent.volume,
                "RPI calibration intent quantity",
            )
            request_volume = self._positive_decimal(
                request.volume,
                "RPI calibration request quantity",
            )
        except ValueError as exc:
            return (
                f"rpi_calibration_invalid_numeric:{exc}",
                0,
                "",
                "",
            )
        depth_text = self._decimal_text(depth)
        if depth_text not in config["fixed_depths_bps"]:
            return "rpi_calibration_depth_not_permitted", 0, "", ""
        if intent_price != request_price or intent_volume != request_volume:
            return "rpi_calibration_request_value_mismatch", 0, "", ""
        if intent.side.value != request.side:
            return "rpi_calibration_request_side_mismatch", 0, "", ""
        if intent.side == Side.BUY and intent_price >= reference_mid:
            return "rpi_calibration_buy_not_below_reference", 0, "", ""
        if intent.side == Side.SELL and intent_price <= reference_mid:
            return "rpi_calibration_sell_not_above_reference", 0, "", ""

        notional_microu = int(
            (
                intent_price
                * intent_volume
                * self.USDT_MICRO_SCALE
            ).to_integral_value(rounding=ROUND_CEILING)
        )
        if notional_microu < config["min_order_notional_microu"]:
            return "rpi_calibration_below_min_notional", 0, "", ""
        if notional_microu > config["max_order_notional_microu"]:
            return "rpi_calibration_above_max_notional", 0, "", ""
        return (
            "",
            notional_microu,
            depth_text,
            self._decimal_text(reference_mid),
        )

    def _reserve_rpi_calibration_sample_locked(
        self,
        intent: OrderIntent,
        request: OrderRequest,
        client_oid: str,
    ) -> tuple[str, str]:
        if not self._rpi_calibration["enabled"]:
            return "", ""
        if self._rpi_calibration_expired:
            return (
                "rpi_calibration_permit_expired",
                self._rpi_calibration_expiry_reason
                or "permit_expired",
            )
        if self._rpi_calibration_restart_rearm_blocked:
            terminal_reason = "unclean_restart_requires_new_permit"
            self._expire_rpi_calibration_permit_locked(terminal_reason)
            return (
                "rpi_calibration_unclean_restart_blocked",
                terminal_reason,
            )

        rejection, notional_microu, depth_text, reference_mid = (
            self._validate_rpi_calibration_sample_locked(intent, request)
        )
        if rejection:
            return rejection, ""

        active_count = len(self._rpi_calibration_active_orders_locked())
        if active_count >= self._rpi_calibration["max_active_orders"]:
            return "rpi_calibration_active_order_exists", ""

        now_ns = time_service.now_ns()
        if now_ns < self._rpi_calibration["not_before_ns"]:
            return "rpi_calibration_permit_not_yet_valid", ""
        if now_ns >= self._rpi_calibration["expires_at_ns"]:
            terminal_reason = "permit_expired"
            self._expire_rpi_calibration_permit_locked(terminal_reason)
            return "rpi_calibration_permit_expired", terminal_reason
        last_reserved_ns = self._rpi_calibration_last_reserved_exchange_ns
        if (
            last_reserved_ns > 0
            and now_ns - last_reserved_ns
            < self._rpi_calibration["min_order_interval_ns"]
        ):
            return "rpi_calibration_min_order_interval", ""

        next_count = self._rpi_calibration_reserved_order_count + 1
        next_cumulative = (
            self._rpi_calibration_cumulative_notional_microu
            + notional_microu
        )
        permit_count = (
            next_count
            - self._rpi_calibration_permit_start_order_count
        )
        permit_cumulative = (
            next_cumulative
            - self._rpi_calibration_permit_start_notional_microu
        )
        if permit_count > self._rpi_calibration["max_order_count"]:
            terminal_reason = "max_order_count_exhausted"
            self._expire_rpi_calibration_permit_locked(
                terminal_reason,
                budget_exhausted=True,
            )
            return "rpi_calibration_order_budget_exhausted", terminal_reason
        if (
            permit_cumulative
            > self._rpi_calibration["max_cumulative_notional_microu"]
        ):
            terminal_reason = "max_cumulative_notional_exhausted"
            self._expire_rpi_calibration_permit_locked(
                terminal_reason,
                budget_exhausted=True,
            )
            return "rpi_calibration_notional_budget_exhausted", terminal_reason
        loss_truth_reason, observed_loss_microu = (
            self._observe_rpi_calibration_loss_locked(
                initialize_baseline=True,
            )
        )
        if loss_truth_reason:
            terminal_reason = (
                f"calibration_loss_truth_unavailable:{loss_truth_reason}"
            )
            self._expire_rpi_calibration_permit_locked(terminal_reason)
            return "rpi_calibration_loss_truth_unavailable", terminal_reason
        if (
            self._rpi_calibration_peak_observed_loss_microu
            >= self._rpi_calibration_effective_loss_cap_microu
        ):
            terminal_reason = "max_calibration_loss_exhausted"
            self._expire_rpi_calibration_permit_locked(terminal_reason)
            return "rpi_calibration_loss_cap_exhausted", terminal_reason
        if client_oid in self._rpi_calibration_reservation_ids:
            raise JournalCorruptionError(
                f"Duplicate RPI calibration reservation ID: {client_oid}"
            )

        self._activate_rpi_calibration_permit_locked(now_ns)
        payload = {
            "schema": self.RPI_CALIBRATION_JOURNAL_SCHEMA,
            "reservation_seq": next_count,
            "permit_reservation_seq": permit_count,
            "reservation_id": client_oid,
            "client_oid": client_oid,
            "permit_id": self._rpi_calibration["permit_id"],
            "permit_sha256": self._rpi_calibration["permit_sha256"],
            "deployment_id": self._rpi_calibration["deployment_id"],
            "calibration_config_sha256": (
                self._rpi_calibration["calibration_config_sha256"]
            ),
            "target_deployment_config_sha256": (
                self._rpi_calibration["target_deployment_config_sha256"]
            ),
            "strategy_policy_sha256": (
                self._rpi_calibration["strategy_policy_sha256"]
            ),
            "implementation_sha256": (
                self._rpi_calibration["implementation_sha256"]
            ),
            "reserved_at_exchange_ns": now_ns,
            "symbol": intent.symbol,
            "strategy_id": intent.strategy_id,
            "side": intent.side.value,
            "price": self._decimal_text(
                self._positive_decimal(intent.price, "calibration price")
            ),
            "quantity": self._decimal_text(
                self._positive_decimal(intent.volume, "calibration quantity")
            ),
            "declared_depth_bps": depth_text,
            "calibration_reference_mid": reference_mid,
            "order_type": intent.order_type,
            "time_in_force": intent.time_in_force,
            "post_only": bool(intent.is_post_only),
            "reduce_only": bool(intent.reduce_only),
            "submitted_notional_microu": notional_microu,
            "cumulative_submitted_notional_microu": next_cumulative,
            "permit_cumulative_submitted_notional_microu": (
                permit_cumulative
            ),
            "loss_before_send_microu": observed_loss_microu,
            "effective_deployment_loss_cap_microu": (
                self._rpi_calibration_effective_loss_cap_microu
            ),
        }
        committed_seq = self.audit_logger.audit(
            "rpi_calibration_send_reserved",
            payload,
        )
        if not committed_seq:
            raise JournalError(
                "RPI calibration send reservation was not committed"
            )
        self._rpi_calibration_reserved_order_count = next_count
        self._rpi_calibration_cumulative_notional_microu = next_cumulative
        self._rpi_calibration_last_reserved_exchange_ns = now_ns
        self._rpi_calibration_reservation_ids.add(client_oid)
        self._rpi_calibration_reservation_exchange_ns[client_oid] = now_ns
        return "", ""

    def _audit_rpi_calibration_emergency_bypass_locked(
        self,
        intent: OrderIntent,
        request: OrderRequest,
        client_oid: str,
    ) -> None:
        if not self._rpi_calibration["enabled"]:
            return
        if (
            intent.strategy_id != "system_emergency"
            or not intent.reduce_only
            or not request.reduce_only
        ):
            raise JournalError(
                "Only system_emergency reduce-only orders may bypass "
                "RPI calibration sampling quotas"
            )
        price = self._positive_decimal(
            request.price,
            "emergency bypass price",
        )
        quantity = self._positive_decimal(
            request.volume,
            "emergency bypass quantity",
        )
        notional_microu = int(
            (
                price * quantity * self.USDT_MICRO_SCALE
            ).to_integral_value(rounding=ROUND_CEILING)
        )
        committed_seq = self.audit_logger.audit(
            "rpi_calibration_emergency_reduce_bypass",
            {
                "schema": self.RPI_CALIBRATION_JOURNAL_SCHEMA,
                "bypass_id": client_oid,
                "client_oid": client_oid,
                "permit_id": self._rpi_calibration["permit_id"],
                "permit_sha256": self._rpi_calibration["permit_sha256"],
                "deployment_id": self._rpi_calibration["deployment_id"],
                "recorded_at_exchange_ns": time_service.now_ns(),
                "symbol": request.symbol,
                "side": request.side,
                "price": self._decimal_text(price),
                "quantity": self._decimal_text(quantity),
                "estimated_notional_microu": notional_microu,
                "reduce_only": True,
                "reason": intent.tag,
            },
        )
        if not committed_seq:
            raise JournalError(
                "RPI calibration emergency bypass audit was not committed"
            )

    def _mark_rpi_calibration_terminal_pending(
        self,
        reason: str,
        **details,
    ) -> None:
        reason = str(reason or "terminal_convergence_pending")
        with self.lock:
            changed = bool(
                self._rpi_calibration_terminal_verified
                or self._rpi_calibration_terminal_pending_reason != reason
            )
            self._rpi_calibration_terminal_verified = False
            self._rpi_calibration_terminal_empty_snapshots = 0
            self._rpi_calibration_terminal_pending_reason = reason
        if changed:
            self._audit(
                "rpi_calibration_terminal_convergence_pending",
                permit_id=self._rpi_calibration["permit_id"],
                symbol=self._rpi_calibration["symbol"],
                reason=reason,
                **details,
            )

    def _enforce_rpi_calibration_terminal_once(self) -> bool:
        """Advance an expired permit toward verified no-orders and flat."""
        symbol = self._rpi_calibration["symbol"]
        source = "rpi_calibration_terminal"

        with self.lock:
            terminal_generation = (
                self._rpi_calibration_terminal_generation
            )
            cancel_sweep_completed = (
                self._rpi_calibration_terminal_cancel_sweep_completed
            )
        if not cancel_sweep_completed:
            cancel_targets = self._account_cancel_symbols()
            if symbol not in cancel_targets:
                cancel_targets.append(symbol)
            sweep_acknowledged = True
            for target_symbol in sorted(set(cancel_targets)):
                sweep_acknowledged = bool(
                    self._cancel_all_orders_unchecked(
                        target_symbol,
                        source=f"{source}:initial_sweep",
                        bypass_message_budget=True,
                    )
                    and sweep_acknowledged
                )
            if not sweep_acknowledged:
                self._mark_rpi_calibration_terminal_pending(
                    "initial_cancel_sweep_unverified"
                )
                return False
            with self.lock:
                self._rpi_calibration_terminal_cancel_sweep_completed = True
            self._mark_rpi_calibration_terminal_pending(
                "initial_cancel_sweep_acknowledged"
            )
            return False

        try:
            remote_orders = self.query_open_orders()
            normalized_orders = (
                self._normalize_remote_open_orders(remote_orders)
                if remote_orders is not None
                else None
            )
        except Exception as exc:
            normalized_orders = None
            order_query_error = f"{type(exc).__name__}:{exc}"
        else:
            order_query_error = ""

        if normalized_orders is None:
            self._cancel_all_orders_unchecked(
                symbol,
                source=f"{source}:order_truth_unavailable",
                bypass_message_budget=True,
            )
            self._mark_rpi_calibration_terminal_pending(
                "open_order_truth_unavailable",
                error=order_query_error,
            )
            return False

        with self.lock:
            local_active_orders = [
                order.client_oid
                for order in self.orders.values()
                if order.is_active()
            ]
            with self._outbound_gate_condition:
                order_sends_inflight = self._outbound_order_sends_inflight

        if normalized_orders:
            for target_symbol in self._account_cancel_symbols(remote_orders):
                self._cancel_all_orders_unchecked(
                    target_symbol,
                    source=f"{source}:residual_orders",
                    bypass_message_budget=True,
                )
        if local_active_orders:
            self._cancel_orders_matching(
                lambda order: (
                    order.status != OrderStatus.SUBMITTING
                    and order.client_oid in local_active_orders
                )
            )
        if normalized_orders or local_active_orders or order_sends_inflight:
            self._mark_rpi_calibration_terminal_pending(
                "orders_or_sends_remain",
                remote_open_order_count=len(normalized_orders),
                local_active_order_count=len(local_active_orders),
                order_sends_inflight=order_sends_inflight,
            )
            return False

        try:
            trade_backfill_ok = self._backfill_trade_history(
                symbols={symbol},
                end_time_ms=time_service.now(),
            )
        except Exception as exc:
            trade_backfill_ok = False
            trade_backfill_error = f"{type(exc).__name__}:{exc}"
        else:
            trade_backfill_error = ""
        if not trade_backfill_ok:
            self._mark_rpi_calibration_terminal_pending(
                "trade_backfill_unavailable",
                error=trade_backfill_error,
            )
            return False

        try:
            remote_positions = self.query_positions()
        except Exception as exc:
            remote_positions = None
            position_query_error = f"{type(exc).__name__}:{exc}"
        else:
            position_query_error = ""
        if not isinstance(remote_positions, (list, tuple)):
            self._mark_rpi_calibration_terminal_pending(
                "position_truth_unavailable",
                error=position_query_error,
            )
            return False

        remote_nonzero_positions = {}
        try:
            for payload in remote_positions:
                if not isinstance(payload, dict):
                    raise ValueError("position entry is not an object")
                remote_symbol = str(
                    payload.get("symbol", "") or ""
                ).upper().strip()
                if not remote_symbol:
                    raise ValueError("position entry is missing symbol")
                if "positionAmt" not in payload:
                    raise ValueError(
                        f"position entry is missing positionAmt:{remote_symbol}"
                    )
                amount = float(payload["positionAmt"])
                if not math.isfinite(amount):
                    raise ValueError("position amount is not finite")
                if abs(amount) > 1e-9:
                    remote_nonzero_positions[remote_symbol] = amount
        except (TypeError, ValueError) as exc:
            self._mark_rpi_calibration_terminal_pending(
                "position_truth_invalid",
                error=f"{type(exc).__name__}:{exc}",
            )
            return False

        try:
            with self.lock:
                local_nonzero_positions = {}
                for local_symbol, raw_amount in (
                    self.exposure.net_positions.items()
                ):
                    amount = float(raw_amount or 0.0)
                    if not math.isfinite(amount):
                        raise ValueError(
                            "local position amount is not finite:"
                            f"{local_symbol}"
                        )
                    if abs(amount) > 1e-9:
                        local_nonzero_positions[
                            str(local_symbol or "").upper()
                        ] = amount
        except (TypeError, ValueError) as exc:
            self._mark_rpi_calibration_terminal_pending(
                "local_position_truth_invalid",
                error=f"{type(exc).__name__}:{exc}",
            )
            return False
        if remote_nonzero_positions or local_nonzero_positions:
            submitted = self.emergency_reduce_only_flatten(
                (
                    "rpi_calibration_expired:"
                    f"{self._rpi_calibration_expiry_reason}"
                ),
            )
            self._mark_rpi_calibration_terminal_pending(
                "position_remains",
                remote_positions=remote_nonzero_positions,
                local_positions=local_nonzero_positions,
                flatten_orders_submitted=submitted,
            )
            return False

        generation_changed = False
        with self.lock:
            if (
                self._rpi_calibration_terminal_generation
                != terminal_generation
            ):
                generation_changed = True
            else:
                self._rpi_calibration_terminal_empty_snapshots += 1
                empty_snapshots = (
                    self._rpi_calibration_terminal_empty_snapshots
                )
                required = self.shutdown_empty_snapshots_required
                if empty_snapshots >= required:
                    self._rpi_calibration_terminal_verified = False
                    self._rpi_calibration_terminal_pending_reason = (
                        "terminal_verification_commit_pending"
                    )
                    self._audit(
                        "rpi_calibration_terminal_convergence_verified",
                        permit_id=self._rpi_calibration["permit_id"],
                        symbol=symbol,
                        terminal_generation=terminal_generation,
                        empty_snapshots=empty_snapshots,
                        remote_positions=remote_nonzero_positions,
                        local_positions=local_nonzero_positions,
                    )
                    self._rpi_calibration_terminal_verified = True
                    self._rpi_calibration_terminal_pending_reason = ""
                else:
                    self._rpi_calibration_terminal_pending_reason = (
                        "empty_snapshot_confirmation_pending"
                    )
        if generation_changed:
            self._mark_rpi_calibration_terminal_pending(
                "terminal_truth_changed_during_verification"
            )
            return False
        if empty_snapshots < required:
            return False
        return True

    def enforce_rpi_calibration_runtime_limits(self) -> dict:
        """Cancel stale samples while keeping them active until venue terminal."""
        if not self._rpi_calibration["enabled"]:
            return self.get_outbound_gate_snapshot()["rpi_calibration"]

        with self.lock:
            permit_expired = self._rpi_calibration_expired
            terminal_verified = (
                self._rpi_calibration_terminal_verified
            )
        if permit_expired:
            if not terminal_verified:
                self._schedule_rpi_calibration_runtime_enforcement()
            return self.get_outbound_gate_snapshot()["rpi_calibration"]

        loss_terminal_reason = ""
        try:
            with self.lock:
                if (
                    self._rpi_calibration_restart_rearm_blocked
                    and not self._rpi_calibration_expired
                ):
                    loss_terminal_reason = (
                        "unclean_restart_requires_new_permit"
                    )
                    self._expire_rpi_calibration_permit_locked(
                        loss_terminal_reason
                    )
                elif (
                    self._rpi_calibration_permit_activated
                    and not self._rpi_calibration_expired
                ):
                    truth_reason, _ = (
                        self._observe_rpi_calibration_loss_locked(
                            initialize_baseline=False,
                        )
                    )
                    if truth_reason:
                        loss_terminal_reason = (
                            "calibration_loss_truth_unavailable:"
                            f"{truth_reason}"
                        )
                    elif (
                        self._rpi_calibration_peak_observed_loss_microu
                        >= self._rpi_calibration_effective_loss_cap_microu
                    ):
                        loss_terminal_reason = (
                            "max_calibration_loss_exhausted"
                        )
                    if loss_terminal_reason:
                        self._expire_rpi_calibration_permit_locked(
                            loss_terminal_reason
                        )
        except Exception as exc:
            loss_terminal_reason = (
                "calibration_loss_enforcement_journal_failure"
            )
            self._fail_closed_on_journal_error(
                exc,
                "rpi_calibration_loss_enforcement",
                self._rpi_calibration["symbol"],
            )
        if loss_terminal_reason:
            self.expire_rpi_calibration_permit(
                loss_terminal_reason
            )

        now_ns = time_service.now_ns()
        if now_ns >= self._rpi_calibration["expires_at_ns"]:
            self.expire_rpi_calibration_permit("permit_expired")

        ttl_ns = int(self._rpi_calibration["order_ttl_ns"])
        ttl_sec = ttl_ns / 1_000_000_000
        stale_oids = []
        with self.lock:
            active_orders = self._rpi_calibration_active_orders_locked()
            active_ids = {order.client_oid for order in active_orders}
            self._rpi_calibration_ttl_cancel_oids.intersection_update(
                active_ids
            )
            for order in active_orders:
                if order.status not in {
                    OrderStatus.PENDING_ACK,
                    OrderStatus.NEW,
                    OrderStatus.PARTIALLY_FILLED,
                }:
                    continue
                reserved_at_ns = int(
                    self._rpi_calibration_reservation_exchange_ns.get(
                        order.client_oid,
                        0,
                    )
                    or 0
                )
                age_ns = (
                    max(0, now_ns - reserved_at_ns)
                    if reserved_at_ns > 0
                    else ttl_ns
                )
                age_sec = age_ns / 1_000_000_000
                if (
                    age_ns >= ttl_ns
                    and order.client_oid
                    not in self._rpi_calibration_ttl_cancel_oids
                ):
                    self._rpi_calibration_ttl_cancel_oids.add(
                        order.client_oid
                    )
                    stale_oids.append((order.client_oid, age_sec))

        for client_oid, age_sec in stale_oids:
            try:
                self._audit(
                    "rpi_calibration_order_ttl_expired",
                    client_oid=client_oid,
                    permit_id=self._rpi_calibration["permit_id"],
                    deployment_id=self._rpi_calibration["deployment_id"],
                    age_sec=age_sec,
                    order_ttl_sec=ttl_sec,
                )
            except Exception as exc:
                self._fail_closed_on_journal_error(
                    exc,
                    "rpi_calibration_order_ttl_expired",
                    self._rpi_calibration["symbol"],
                )
            if not self.cancel_order(client_oid):
                with self.lock:
                    self._rpi_calibration_ttl_cancel_oids.discard(
                        client_oid
                    )
        return self.get_outbound_gate_snapshot()["rpi_calibration"]

    def _schedule_rpi_calibration_runtime_enforcement(
        self,
        *,
        terminal_truth_changed: bool = False,
    ) -> bool:
        if not self._rpi_calibration["enabled"]:
            return False
        with self.lock:
            if self._rpi_calibration_expired and terminal_truth_changed:
                self._rpi_calibration_terminal_generation += 1
                self._rpi_calibration_terminal_empty_snapshots = 0
                self._rpi_calibration_terminal_verified = False
                self._rpi_calibration_terminal_pending_reason = (
                    "terminal_truth_changed"
                )
            if (
                (
                    self._rpi_calibration_expired
                    and self._rpi_calibration_terminal_verified
                    and not terminal_truth_changed
                )
                or
                (
                    not self._rpi_calibration_permit_activated
                    and not self._rpi_calibration_expired
                )
                or self._rpi_calibration_enforcement_inflight
                or self._shutdown_requested
                or self._stopped
            ):
                return False
            self._rpi_calibration_enforcement_inflight = True

        def enforce():
            published.wait()
            try:
                with self.lock:
                    expired = self._rpi_calibration_expired
                if not expired:
                    self.enforce_rpi_calibration_runtime_limits()

                retry_delay = max(
                    0.25,
                    self.shutdown_cancel_settle_interval_sec,
                )
                while True:
                    with self.lock:
                        if (
                            not self._rpi_calibration_expired
                            or self._shutdown_requested
                            or self._stopped
                        ):
                            return
                    try:
                        if self._enforce_rpi_calibration_terminal_once():
                            return
                    except Exception as exc:
                        try:
                            self._fail_closed_on_journal_error(
                                exc,
                                "rpi_calibration_terminal_convergence",
                                self._rpi_calibration["symbol"],
                            )
                        except Exception as fail_closed_exc:
                            logger.critical(
                                "[OMS] RPI calibration terminal convergence "
                                "could not fail closed: "
                                f"{type(fail_closed_exc).__name__}:"
                                f"{fail_closed_exc}"
                            )
                    time.sleep(retry_delay)
                    retry_delay = min(5.0, retry_delay * 2.0)
            finally:
                reschedule_terminal_convergence = False
                with self.lock:
                    self._rpi_calibration_enforcement_inflight = False
                    if (
                        self._rpi_calibration_enforcement_thread is not None
                        and self._rpi_calibration_enforcement_thread.is_current()
                    ):
                        self._rpi_calibration_enforcement_thread = None
                    reschedule_terminal_convergence = bool(
                        self._rpi_calibration_expired
                        and not self._rpi_calibration_terminal_verified
                        and not self._shutdown_requested
                        and not self._stopped
                    )
                if reschedule_terminal_convergence:
                    # A fill/account update can invalidate VERIFIED after the
                    # worker's last generation check but before this cleanup.
                    # Hand the pending generation to a fresh worker.
                    self._schedule_rpi_calibration_runtime_enforcement()

        published = threading.Event()
        enforcement_thread = self._submit_background_task(
            (
                "rpi-enforce:"
                f"{self._rpi_calibration['deployment_id']}:"
                f"{self._rpi_calibration['permit_id']}"
            ),
            enforce,
            name="RpiCalibrationRuntimeEnforcer",
            safety=True,
            resubmit_after_current=True,
        )
        with self.lock:
            if (
                enforcement_thread is None
                or self._shutdown_requested
                or self._stopped
            ):
                self._rpi_calibration_enforcement_inflight = False
                published.set()
                return False
            self._rpi_calibration_enforcement_thread = enforcement_thread
        published.set()
        return True

