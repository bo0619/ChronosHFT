"""Account snapshot risk evaluation for the independent sidecar."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import time

from risk.deployment_loss import (
    MAX_CANARY_DEPLOYED_EQUITY_FRACTION,
    deployed_capital_equity_ratio,
    deployed_capital_within_equity_limit,
    deployment_loss_action,
    update_deployment_loss,
)
from risk.sidecar_policy import RiskSidecarPolicy


def _is_truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


@dataclass(slots=True)
class SidecarEquityState:
    """Mutable equity baselines owned by the account-risk controller."""

    deployment_id: str
    risk_day: str
    day_start_equity: float
    day_start_external_cash_flow_total: float
    peak_adjusted_equity: float
    last_equity: float
    deployment_start_equity: float
    deployment_start_external_cash_flow_total: float
    deployment_adjusted_equity: float
    deployment_loss: float

    @classmethod
    def from_settings(
        cls,
        settings: dict,
        finite_float: Callable[[object, str], float],
        *,
        deployment_id: str,
    ) -> SidecarEquityState:
        return cls(
            deployment_id=str(deployment_id or ""),
            risk_day=str(settings.get("seed_risk_day", "") or ""),
            day_start_equity=finite_float(
                settings.get("seed_day_start_equity", 0.0) or 0.0,
                "seed_day_start_equity",
            ),
            day_start_external_cash_flow_total=finite_float(
                settings.get("seed_external_cash_flow_total", 0.0) or 0.0,
                "seed_external_cash_flow_total",
            ),
            peak_adjusted_equity=finite_float(
                settings.get("seed_peak_adjusted_equity", 0.0) or 0.0,
                "seed_peak_adjusted_equity",
            ),
            last_equity=finite_float(
                settings.get("seed_last_equity", 0.0) or 0.0,
                "seed_last_equity",
            ),
            deployment_start_equity=finite_float(
                settings.get("seed_deployment_start_equity", 0.0) or 0.0,
                "seed_deployment_start_equity",
            ),
            deployment_start_external_cash_flow_total=finite_float(
                settings.get(
                    "seed_deployment_external_cash_flow_total",
                    0.0,
                )
                or 0.0,
                "seed_deployment_external_cash_flow_total",
            ),
            deployment_adjusted_equity=finite_float(
                settings.get("seed_deployment_adjusted_equity", 0.0) or 0.0,
                "seed_deployment_adjusted_equity",
            ),
            deployment_loss=max(
                0.0,
                finite_float(
                    settings.get("seed_deployment_loss", 0.0) or 0.0,
                    "seed_deployment_loss",
                ),
            ),
        )


class SidecarAccountRiskController:
    """Own account-risk policy evaluation and durable equity baselines."""

    __slots__ = ("policy", "state", "_wall_time")

    def __init__(
        self,
        policy: RiskSidecarPolicy,
        state: SidecarEquityState,
        *,
        wall_time: Callable[[], float] = time.time,
    ):
        self.policy = policy
        self.state = state
        self._wall_time = wall_time

    @classmethod
    def from_settings(
        cls,
        policy: RiskSidecarPolicy,
        settings: dict,
        finite_float: Callable[[object, str], float],
        *,
        wall_time: Callable[[], float] = time.time,
    ) -> SidecarAccountRiskController:
        return cls(
            policy,
            SidecarEquityState.from_settings(
                settings,
                finite_float,
                deployment_id=policy.deployment_id,
            ),
            wall_time=wall_time,
        )

    def initial_metrics(self, funding_guard: dict) -> dict:
        policy = self.policy
        return {
            "maintenance_margin_ratio": 0.0,
            "position_gross_notional": 0.0,
            "opening_order_notional": 0.0,
            "projected_gross_notional": 0.0,
            "open_order_count": 0,
            "nonzero_position_count": 0,
            "risk_day": "",
            "equity": 0.0,
            "cash_flow_adjusted_equity": 0.0,
            "cash_flow_adjusted_daily_loss": 0.0,
            "deployment_id": self.state.deployment_id,
            "deployment_start_equity": 0.0,
            "deployment_adjusted_equity": 0.0,
            "deployment_loss": 0.0,
            "max_deployment_loss": policy.max_deployment_loss,
            "declared_account_equity": policy.declared_account_equity,
            "max_deployed_capital": policy.max_deployed_capital,
            "deployment_policy_fingerprint": (
                policy.deployment_policy_fingerprint
            ),
            "peak_adjusted_equity": 0.0,
            "peak_drawdown_pct": 0.0,
            "external_cash_flow_total": 0.0,
            "daily_external_cash_flow_total": 0.0,
            "deployment_external_cash_flow_total": 0.0,
            "clock_offset_ms": 0.0,
            "clock_phase_error_ms": 0.0,
            "clock_rtt_ms": 0.0,
            "clock_uncertainty_ms": 0.0,
            "clock_offset_dispersion_ms": 0.0,
            "minimum_liquidation_distance_pct": None,
            "minimum_liquidation_distance_symbol": "",
            "funding_guard": dict(funding_guard),
        }

    def evaluate_equity(
        self,
        snapshot: dict,
        metrics: dict,
    ) -> tuple[str, str]:
        policy = self.policy
        state = self.state
        if not policy.daily_loss_enabled and policy.max_deployment_loss <= 0.0:
            return "NONE", ""
        account = snapshot.get("account", {}) or {}
        try:
            equity = float(account.get("totalMarginBalance", 0.0) or 0.0)
            daily_external_cash_flow_total = float(
                snapshot["daily_external_cash_flow_total"]
            )
            deployment_external_cash_flow_total = float(
                snapshot["deployment_external_cash_flow_total"]
            )
            captured_at = float(
                snapshot.get("captured_at", self._wall_time())
            )
        except (KeyError, TypeError, ValueError):
            return "REDUCE_ONLY", "daily_equity_snapshot_invalid"
        if (
            not math.isfinite(equity)
            or not math.isfinite(daily_external_cash_flow_total)
            or not math.isfinite(deployment_external_cash_flow_total)
            or not math.isfinite(captured_at)
            or captured_at <= 0.0
        ):
            return "REDUCE_ONLY", "daily_equity_snapshot_invalid"
        if policy.max_deployed_capital > 0.0:
            try:
                deployed_equity_ratio = deployed_capital_equity_ratio(
                    equity=equity,
                    max_deployed_capital=policy.max_deployed_capital,
                )
                capital_envelope_safe = deployed_capital_within_equity_limit(
                    equity=equity,
                    max_deployed_capital=policy.max_deployed_capital,
                    maximum_fraction=MAX_CANARY_DEPLOYED_EQUITY_FRACTION,
                )
            except ValueError:
                return "KILL", "canary_account_equity_invalid"
            metrics["deployed_capital_equity_ratio"] = deployed_equity_ratio
            if not capital_envelope_safe:
                return (
                    "KILL",
                    "canary_deployed_capital_exceeds_current_equity_fraction",
                )

        current_risk_day = datetime.fromtimestamp(
            captured_at,
            tz=timezone.utc,
        ).date().isoformat()
        cash_flow_risk_day = str(
            snapshot.get("cash_flow_risk_day", current_risk_day)
            or current_risk_day
        )
        if cash_flow_risk_day != current_risk_day:
            return "REDUCE_ONLY", "cash_flow_risk_day_mismatch"
        baseline_missing = bool(
            state.day_start_equity == 0.0
            and state.peak_adjusted_equity == 0.0
            and state.last_equity == 0.0
        )
        if state.risk_day != current_risk_day or baseline_missing:
            state.risk_day = current_risk_day
            state.day_start_equity = equity
            state.day_start_external_cash_flow_total = (
                daily_external_cash_flow_total
            )
            state.peak_adjusted_equity = equity

        cash_flow_delta = (
            daily_external_cash_flow_total
            - state.day_start_external_cash_flow_total
        )
        adjusted_equity = equity - cash_flow_delta
        if policy.max_deployment_loss > 0.0:
            (
                state.deployment_start_equity,
                state.deployment_start_external_cash_flow_total,
                state.deployment_adjusted_equity,
                state.deployment_loss,
            ) = update_deployment_loss(
                equity=equity,
                external_cash_flow_total=(
                    deployment_external_cash_flow_total
                ),
                start_equity=state.deployment_start_equity,
                start_external_cash_flow_total=(
                    state.deployment_start_external_cash_flow_total
                ),
            )
        if state.peak_adjusted_equity <= 0.0:
            state.peak_adjusted_equity = adjusted_equity
        elif adjusted_equity > state.peak_adjusted_equity:
            state.peak_adjusted_equity = adjusted_equity
        state.last_equity = equity
        daily_loss = max(0.0, state.day_start_equity - adjusted_equity)
        peak_drawdown_pct = (
            max(
                0.0,
                (state.peak_adjusted_equity - adjusted_equity)
                / state.peak_adjusted_equity,
            )
            if state.peak_adjusted_equity > 0.0
            else 0.0
        )
        metrics.update(
            {
                "risk_day": state.risk_day,
                "equity": equity,
                "cash_flow_adjusted_equity": adjusted_equity,
                "cash_flow_adjusted_daily_loss": daily_loss,
                "deployment_id": state.deployment_id,
                "deployment_start_equity": state.deployment_start_equity,
                "deployment_adjusted_equity": (
                    state.deployment_adjusted_equity
                ),
                "deployment_loss": state.deployment_loss,
                "max_deployment_loss": policy.max_deployment_loss,
                "peak_adjusted_equity": state.peak_adjusted_equity,
                "peak_drawdown_pct": peak_drawdown_pct,
                "external_cash_flow_total": (
                    daily_external_cash_flow_total
                ),
                "daily_external_cash_flow_total": (
                    daily_external_cash_flow_total
                ),
                "deployment_external_cash_flow_total": (
                    deployment_external_cash_flow_total
                ),
            }
        )

        deployment_action = deployment_loss_action(
            loss=state.deployment_loss,
            maximum_loss=policy.max_deployment_loss,
            reduce_only_fraction=policy.deployment_loss_reduce_only_fraction,
        )
        if deployment_action == "KILL":
            return (
                "KILL",
                f"deployment_loss_kill:{state.deployment_loss:.6f}",
            )
        if (
            policy.daily_loss_enabled
            and policy.max_daily_loss > 0.0
            and daily_loss >= policy.max_daily_loss
        ):
            return "KILL", f"daily_loss_kill:{daily_loss:.6f}"
        if (
            policy.daily_loss_enabled
            and policy.max_drawdown_pct > 0.0
            and peak_drawdown_pct >= policy.max_drawdown_pct
        ):
            return "KILL", f"peak_drawdown_kill:{peak_drawdown_pct:.6f}"
        if deployment_action == "REDUCE_ONLY":
            return (
                "REDUCE_ONLY",
                f"deployment_loss_reduce_only:{state.deployment_loss:.6f}",
            )
        reduce_fraction = policy.daily_loss_reduce_only_fraction
        if (
            policy.daily_loss_enabled
            and reduce_fraction > 0.0
            and policy.max_daily_loss > 0.0
            and daily_loss >= policy.max_daily_loss * reduce_fraction
        ):
            return "REDUCE_ONLY", f"daily_loss_reduce_only:{daily_loss:.6f}"
        if (
            policy.daily_loss_enabled
            and reduce_fraction > 0.0
            and policy.max_drawdown_pct > 0.0
            and peak_drawdown_pct
            >= policy.max_drawdown_pct * reduce_fraction
        ):
            return (
                "REDUCE_ONLY",
                f"peak_drawdown_reduce_only:{peak_drawdown_pct:.6f}",
            )
        return "NONE", ""

    def evaluate(self, snapshot: dict) -> tuple[str, str, dict]:
        policy = self.policy
        if not isinstance(snapshot, dict):
            return "REDUCE_ONLY", "risk_snapshot_invalid", {}
        account = snapshot.get("account")
        positions = snapshot.get("positions")
        open_orders = snapshot.get("open_orders")
        if (
            not isinstance(account, dict)
            or not isinstance(positions, list)
            or not isinstance(open_orders, list)
        ):
            return "REDUCE_ONLY", "risk_snapshot_invalid", {}
        if (
            "totalMaintMargin" not in account
            or "totalMarginBalance" not in account
        ):
            return "REDUCE_ONLY", "margin_snapshot_missing", {}
        try:
            maintenance_margin = float(account["totalMaintMargin"])
            margin_balance = float(account["totalMarginBalance"])
        except (TypeError, ValueError, OverflowError):
            return "REDUCE_ONLY", "margin_snapshot_invalid", {}
        if (
            not math.isfinite(maintenance_margin)
            or not math.isfinite(margin_balance)
            or maintenance_margin < 0.0
            or margin_balance < 0.0
        ):
            return "REDUCE_ONLY", "margin_snapshot_invalid", {}
        if margin_balance <= 0.0:
            return "KILL", "account_margin_balance_non_positive", {}
        maintenance_margin_ratio = maintenance_margin / margin_balance

        mark_prices = {}
        position_gross_notional = 0.0
        nonzero_position_count = 0
        minimum_liquidation_distance_pct = None
        minimum_liquidation_distance_symbol = ""
        for position in positions:
            try:
                symbol = str(position.get("symbol", "") or "").upper()
                amount = float(position.get("positionAmt", 0.0) or 0.0)
                mark_price = float(position.get("markPrice", 0.0) or 0.0)
                entry_price = float(position.get("entryPrice", 0.0) or 0.0)
                liquidation_price = float(
                    position.get("liquidationPrice", 0.0) or 0.0
                )
            except (AttributeError, TypeError, ValueError):
                return "REDUCE_ONLY", "position_snapshot_invalid:", {}
            if not math.isfinite(amount):
                return "REDUCE_ONLY", f"position_amount_invalid:{symbol}", {}
            price = mark_price if mark_price > 0.0 else entry_price
            if price > 0.0 and math.isfinite(price):
                mark_prices[symbol] = price
            if abs(amount) <= 1e-9:
                continue
            if price <= 0.0 or not math.isfinite(price):
                return "REDUCE_ONLY", f"position_price_unavailable:{symbol}", {}
            nonzero_position_count += 1
            position_notional = abs(amount) * price
            if not math.isfinite(position_notional):
                return "KILL", f"position_notional_overflow:{symbol}", {}
            position_gross_notional += position_notional
            if not math.isfinite(position_gross_notional):
                return "KILL", "account_position_notional_overflow", {}
            if policy.liquidation_proximity_enabled:
                if mark_price <= 0.0 or not math.isfinite(mark_price):
                    return (
                        "REDUCE_ONLY",
                        f"liquidation_mark_price_unavailable:{symbol}",
                        {},
                    )
                if (
                    liquidation_price <= 0.0
                    or not math.isfinite(liquidation_price)
                ):
                    if policy.require_liquidation_price:
                        return (
                            "REDUCE_ONLY",
                            f"liquidation_price_unavailable:{symbol}",
                            {},
                        )
                    continue
                liquidation_distance_pct = (
                    (mark_price - liquidation_price) / mark_price
                    if amount > 0.0
                    else (liquidation_price - mark_price) / mark_price
                )
                if (
                    minimum_liquidation_distance_pct is None
                    or liquidation_distance_pct
                    < minimum_liquidation_distance_pct
                ):
                    minimum_liquidation_distance_pct = (
                        liquidation_distance_pct
                    )
                    minimum_liquidation_distance_symbol = symbol

        opening_order_notional = 0.0
        for order in open_orders:
            try:
                reduce_only = _is_truthy(order.get("reduceOnly", False))
                symbol = str(order.get("symbol", "") or "").upper()
            except AttributeError:
                return "REDUCE_ONLY", "open_order_invalid:", {}
            if reduce_only:
                continue
            try:
                original_qty = float(
                    order.get("origQty", order.get("quantity", 0.0)) or 0.0
                )
                executed_qty = float(order.get("executedQty", 0.0) or 0.0)
                price = float(order.get("price", 0.0) or 0.0)
                stop_price = float(order.get("stopPrice", 0.0) or 0.0)
            except (TypeError, ValueError):
                return "REDUCE_ONLY", f"open_order_invalid:{symbol}", {}
            if (
                not math.isfinite(original_qty)
                or not math.isfinite(executed_qty)
                or not math.isfinite(price)
                or not math.isfinite(stop_price)
                or original_qty < 0.0
                or executed_qty < 0.0
                or executed_qty > original_qty + 1e-9
            ):
                return "REDUCE_ONLY", f"open_order_invalid:{symbol}", {}
            remaining_qty = max(0.0, original_qty - executed_qty)
            if remaining_qty <= 1e-9:
                continue
            risk_price = price or stop_price or mark_prices.get(symbol, 0.0)
            if risk_price <= 0.0 or not math.isfinite(risk_price):
                return (
                    "REDUCE_ONLY",
                    f"open_order_price_unavailable:{symbol}",
                    {},
                )
            order_notional = remaining_qty * risk_price
            if not math.isfinite(order_notional):
                return (
                    "REDUCE_ONLY",
                    f"open_order_notional_overflow:{symbol}",
                    {},
                )
            opening_order_notional += order_notional
            if not math.isfinite(opening_order_notional):
                return "REDUCE_ONLY", "opening_order_notional_overflow", {}

        projected_gross_notional = (
            position_gross_notional + opening_order_notional
        )
        if not math.isfinite(projected_gross_notional):
            return "KILL", "projected_gross_notional_overflow", {}
        metrics = {
            "maintenance_margin_ratio": maintenance_margin_ratio,
            "position_gross_notional": position_gross_notional,
            "opening_order_notional": opening_order_notional,
            "projected_gross_notional": projected_gross_notional,
            "open_order_count": len(open_orders),
            "nonzero_position_count": nonzero_position_count,
            "minimum_liquidation_distance_pct": (
                minimum_liquidation_distance_pct
            ),
            "minimum_liquidation_distance_symbol": (
                minimum_liquidation_distance_symbol
            ),
        }
        daily_action, daily_reason = self.evaluate_equity(snapshot, metrics)
        clock_action = "NONE"
        clock_reason = ""
        if policy.clock_sync_enabled:
            try:
                clock_offset_ms = float(snapshot["clock_offset_ms"])
                clock_phase_error_ms = float(snapshot["clock_phase_error_ms"])
                clock_rtt_ms = float(snapshot["clock_rtt_ms"])
                clock_uncertainty_ms = float(
                    snapshot["clock_uncertainty_ms"]
                )
                clock_offset_dispersion_ms = float(
                    snapshot["clock_offset_dispersion_ms"]
                )
            except (KeyError, TypeError, ValueError):
                return "REDUCE_ONLY", "clock_snapshot_invalid", metrics
            if (
                not math.isfinite(clock_offset_ms)
                or not math.isfinite(clock_phase_error_ms)
                or not math.isfinite(clock_rtt_ms)
                or not math.isfinite(clock_uncertainty_ms)
                or not math.isfinite(clock_offset_dispersion_ms)
                or clock_rtt_ms < 0.0
                or clock_uncertainty_ms < 0.0
                or clock_offset_dispersion_ms < 0.0
            ):
                return "REDUCE_ONLY", "clock_snapshot_invalid", metrics
            metrics.update(
                {
                    "clock_offset_ms": clock_offset_ms,
                    "clock_phase_error_ms": clock_phase_error_ms,
                    "clock_rtt_ms": clock_rtt_ms,
                    "clock_uncertainty_ms": clock_uncertainty_ms,
                    "clock_offset_dispersion_ms": clock_offset_dispersion_ms,
                }
            )
            if (
                policy.clock_kill_phase_error_ms > 0.0
                and abs(clock_phase_error_ms)
                >= policy.clock_kill_phase_error_ms
            ):
                clock_action = "KILL"
                clock_reason = (
                    f"clock_phase_error_kill:{clock_phase_error_ms:.3f}ms"
                )
            elif (
                policy.clock_reduce_only_phase_error_ms > 0.0
                and abs(clock_phase_error_ms)
                >= policy.clock_reduce_only_phase_error_ms
            ):
                clock_action = "REDUCE_ONLY"
                clock_reason = (
                    "clock_phase_error_reduce_only:"
                    f"{clock_phase_error_ms:.3f}ms"
                )
            elif (
                policy.clock_max_rtt_ms > 0.0
                and clock_rtt_ms >= policy.clock_max_rtt_ms
            ):
                clock_action = "REDUCE_ONLY"
                clock_reason = f"clock_rtt_reduce_only:{clock_rtt_ms:.3f}ms"
            elif (
                policy.clock_max_uncertainty_ms > 0.0
                and clock_uncertainty_ms >= policy.clock_max_uncertainty_ms
            ):
                clock_action = "REDUCE_ONLY"
                clock_reason = (
                    "clock_uncertainty_reduce_only:"
                    f"{clock_uncertainty_ms:.3f}ms"
                )
            elif (
                policy.clock_max_offset_dispersion_ms > 0.0
                and clock_offset_dispersion_ms
                >= policy.clock_max_offset_dispersion_ms
            ):
                clock_action = "REDUCE_ONLY"
                clock_reason = (
                    "clock_dispersion_reduce_only:"
                    f"{clock_offset_dispersion_ms:.3f}ms"
                )

        if maintenance_margin_ratio >= policy.margin_kill_ratio:
            return (
                "KILL",
                f"maintenance_margin_kill:{maintenance_margin_ratio:.6f}",
                metrics,
            )
        if (
            minimum_liquidation_distance_pct is not None
            and minimum_liquidation_distance_pct
            <= policy.liquidation_kill_distance_pct
        ):
            return (
                "KILL",
                "liquidation_distance_kill:"
                f"{minimum_liquidation_distance_symbol}:"
                f"{minimum_liquidation_distance_pct:.6f}",
                metrics,
            )
        if clock_action == "KILL":
            return clock_action, clock_reason, metrics
        if daily_action == "KILL":
            return daily_action, daily_reason, metrics
        if (
            policy.max_account_gross_notional > 0.0
            and projected_gross_notional
            >= policy.max_account_gross_notional * policy.gross_kill_multiplier
        ):
            return (
                "KILL",
                f"gross_notional_kill:{projected_gross_notional:.6f}",
                metrics,
            )
        if maintenance_margin_ratio >= policy.margin_reduce_only_ratio:
            return (
                "REDUCE_ONLY",
                "maintenance_margin_reduce_only:"
                f"{maintenance_margin_ratio:.6f}",
                metrics,
            )
        if (
            minimum_liquidation_distance_pct is not None
            and minimum_liquidation_distance_pct
            <= policy.liquidation_reduce_only_distance_pct
        ):
            return (
                "REDUCE_ONLY",
                "liquidation_distance_reduce_only:"
                f"{minimum_liquidation_distance_symbol}:"
                f"{minimum_liquidation_distance_pct:.6f}",
                metrics,
            )
        if clock_action == "REDUCE_ONLY":
            return clock_action, clock_reason, metrics
        if daily_action == "REDUCE_ONLY":
            return daily_action, daily_reason, metrics
        if (
            policy.max_account_gross_notional > 0.0
            and projected_gross_notional > policy.max_account_gross_notional
        ):
            return (
                "REDUCE_ONLY",
                f"gross_notional_reduce_only:{projected_gross_notional:.6f}",
                metrics,
            )
        if policy.max_open_orders > 0 and len(open_orders) > policy.max_open_orders:
            return (
                "REDUCE_ONLY",
                f"open_order_count_limit:{len(open_orders)}>{policy.max_open_orders}",
                metrics,
            )
        return "NONE", "", metrics

    def fallback_metrics(self, snapshot: dict) -> dict:
        policy = self.policy
        state = self.state
        positions = snapshot.get("positions", []) or []
        open_orders = snapshot.get("open_orders", []) or []
        nonzero_position_count = 0
        for position in positions:
            try:
                amount = float(position.get("positionAmt", 0.0) or 0.0)
                nonzero_position_count += int(
                    not math.isfinite(amount) or abs(amount) > 1e-9
                )
            except (AttributeError, TypeError, ValueError):
                nonzero_position_count += 1
        return {
            "maintenance_margin_ratio": 0.0,
            "position_gross_notional": 0.0,
            "opening_order_notional": 0.0,
            "projected_gross_notional": 0.0,
            "open_order_count": len(open_orders),
            "nonzero_position_count": nonzero_position_count,
            "risk_day": state.risk_day,
            "equity": state.last_equity,
            "cash_flow_adjusted_equity": state.last_equity,
            "cash_flow_adjusted_daily_loss": 0.0,
            "deployment_id": state.deployment_id,
            "deployment_start_equity": state.deployment_start_equity,
            "deployment_adjusted_equity": state.deployment_adjusted_equity,
            "deployment_loss": state.deployment_loss,
            "max_deployment_loss": policy.max_deployment_loss,
            "declared_account_equity": policy.declared_account_equity,
            "max_deployed_capital": policy.max_deployed_capital,
            "deployment_policy_fingerprint": (
                policy.deployment_policy_fingerprint
            ),
            "peak_adjusted_equity": state.peak_adjusted_equity,
            "peak_drawdown_pct": 0.0,
            "external_cash_flow_total": 0.0,
            "daily_external_cash_flow_total": 0.0,
            "deployment_external_cash_flow_total": 0.0,
            "clock_offset_ms": 0.0,
            "clock_phase_error_ms": 0.0,
            "clock_rtt_ms": 0.0,
            "clock_uncertainty_ms": 0.0,
            "clock_offset_dispersion_ms": 0.0,
            "minimum_liquidation_distance_pct": None,
            "minimum_liquidation_distance_symbol": "",
        }
