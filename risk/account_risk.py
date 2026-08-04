"""Account equity, cash-flow and margin risk evaluation."""

from __future__ import annotations

import math
import time

from event.type import Event, OMSCapabilityMode
from risk.deployment_loss import (
    MAX_CANARY_DEPLOYED_EQUITY_FRACTION,
    deployed_capital_within_equity_limit,
    deployment_loss_action,
    update_deployment_loss,
)


class AccountRiskField:
    """Expose one declared account-risk field through RiskManager."""

    def __init__(self, attribute: str):
        self.attribute = attribute

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return getattr(instance.account_risk, self.attribute)

    def __set__(self, instance, value) -> None:
        setattr(instance.account_risk, self.attribute, value)


class AccountRiskMethod:
    """Bind one RiskManager method to its account-risk controller."""

    def __init__(self, method_name: str | None = None):
        self.method_name = method_name

    def __set_name__(self, owner, name: str) -> None:
        if self.method_name is None:
            self.method_name = name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return getattr(instance.account_risk, self.method_name)

    def __set__(self, instance, value) -> None:
        setattr(instance.account_risk, self.method_name, value)


class AccountRiskController:
    """Own account-level loss and truth-recovery state machines."""

    def __init__(
        self,
        *,
        risk_config: dict,
        active: bool,
        oms,
        risk_state_repository,
        max_daily_loss: float,
        max_drawdown_pct: float,
        max_deployed_capital: float,
        max_deployment_loss: float,
        deployment_loss_reduce_only_fraction: float,
        refresh_rearm_state,
        is_killed,
        trigger_kill_switch,
        current_risk_day,
        set_trading_mode,
        clear_trading_mode,
    ) -> None:
        self.active = active
        self.oms = oms
        self.risk_state_repository = risk_state_repository
        self.max_daily_loss = max_daily_loss
        self.max_drawdown_pct = max_drawdown_pct
        self.max_deployed_capital = max_deployed_capital
        self.max_deployment_loss = max_deployment_loss
        self.deployment_loss_reduce_only_fraction = (
            deployment_loss_reduce_only_fraction
        )
        self._refresh_rearm_state = refresh_rearm_state
        self._is_killed = is_killed
        self.trigger_kill_switch = trigger_kill_switch
        self._current_risk_day = current_risk_day
        self._set_trading_mode = set_trading_mode
        self._clear_trading_mode = clear_trading_mode

        margin = risk_config.get("margin_health", {})
        self.margin_health_enabled = bool(margin.get("enabled", True))
        self.margin_degraded_ratio = max(
            0.0,
            float(margin.get("degraded_ratio", 0.50) or 0.0),
        )
        self.margin_reduce_only_ratio = max(
            self.margin_degraded_ratio,
            float(margin.get("reduce_only_ratio", 0.70) or 0.0),
        )
        self.margin_kill_ratio = max(
            self.margin_reduce_only_ratio,
            float(margin.get("kill_ratio", 0.90) or 0.0),
        )
        self.margin_recovery_ratio = min(
            self.margin_degraded_ratio,
            max(0.0, float(margin.get("recovery_ratio", 0.40) or 0.0)),
        )
        self.margin_snapshot_max_age_sec = max(
            0.0,
            float(margin.get("max_snapshot_age_sec", 15.0) or 0.0),
        )
        self.margin_recovery_checks = max(
            1,
            int(margin.get("recovery_checks", 3) or 3),
        )
        self.margin_recovery_count = 0

        cash_flow = risk_config.get("cash_flow_truth", {})
        self.cash_flow_truth_enabled = bool(cash_flow.get("enabled", False))
        self.cash_flow_truth_require_snapshot = bool(
            cash_flow.get("require_snapshot", True)
        )
        self.cash_flow_snapshot_max_age_sec = max(
            0.0,
            float(cash_flow.get("max_snapshot_age_sec", 45.0) or 0.0),
        )
        self.cash_flow_recovery_checks = max(
            1,
            int(cash_flow.get("recovery_checks", 2) or 2),
        )
        self.cash_flow_recovery_count = 0

    @property
    def kill_switch_triggered(self) -> bool:
        return bool(self._is_killed())

    @property
    def risk_day(self):
        return self.risk_state_repository.state.risk_day

    @risk_day.setter
    def risk_day(self, value) -> None:
        self.risk_state_repository.state.risk_day = value

    @property
    def initial_equity(self):
        return self.risk_state_repository.state.initial_equity

    @initial_equity.setter
    def initial_equity(self, value) -> None:
        self.risk_state_repository.state.initial_equity = value

    @property
    def initial_external_cash_flow_total(self):
        return self.risk_state_repository.state.initial_external_cash_flow_total

    @initial_external_cash_flow_total.setter
    def initial_external_cash_flow_total(self, value) -> None:
        self.risk_state_repository.state.initial_external_cash_flow_total = value

    @property
    def peak_equity(self):
        return self.risk_state_repository.state.peak_equity

    @peak_equity.setter
    def peak_equity(self, value) -> None:
        self.risk_state_repository.state.peak_equity = value

    @property
    def last_equity(self):
        return self.risk_state_repository.state.last_equity

    @last_equity.setter
    def last_equity(self, value) -> None:
        self.risk_state_repository.state.last_equity = value

    @property
    def deployment_start_equity(self):
        return self.risk_state_repository.state.deployment_start_equity

    @deployment_start_equity.setter
    def deployment_start_equity(self, value) -> None:
        self.risk_state_repository.state.deployment_start_equity = value

    @property
    def deployment_start_external_cash_flow_total(self):
        return self.risk_state_repository.state.deployment_start_external_cash_flow_total

    @deployment_start_external_cash_flow_total.setter
    def deployment_start_external_cash_flow_total(self, value) -> None:
        self.risk_state_repository.state.deployment_start_external_cash_flow_total = value

    @property
    def deployment_adjusted_equity(self):
        return self.risk_state_repository.state.deployment_adjusted_equity

    @deployment_adjusted_equity.setter
    def deployment_adjusted_equity(self, value) -> None:
        self.risk_state_repository.state.deployment_adjusted_equity = value

    @property
    def deployment_loss(self):
        return self.risk_state_repository.state.deployment_loss

    @deployment_loss.setter
    def deployment_loss(self, value) -> None:
        self.risk_state_repository.state.deployment_loss = value

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
            self.risk_state_repository.persist("account_baseline_or_peak")

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
