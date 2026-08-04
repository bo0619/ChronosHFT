"""Compatibility descriptors for the sidecar core facade."""

class _ObservationAttribute:
    """Explicit compatibility field backed by observation state."""

    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return getattr(instance.observation, self.name)

    def __set__(self, instance, value) -> None:
        setattr(instance.observation, self.name, value)


class _EquityStateAttribute:
    """Explicit compatibility field backed by the account-risk state."""

    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return getattr(instance.account_risk.state, self.name)

    def __set__(self, instance, value) -> None:
        setattr(instance.account_risk.state, self.name, value)


class _FundingStateAttribute:
    """Explicit compatibility field backed by the funding controller."""

    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return getattr(instance.funding_risk, self.name)

    def __set__(self, instance, value) -> None:
        setattr(instance.funding_risk, self.name, value)


class _ControlStateAttribute:
    """Explicit compatibility field backed by control state."""

    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return getattr(instance.control.state, self.name)

    def __set__(self, instance, value) -> None:
        setattr(instance.control.state, self.name, value)


class _ControlAttribute:
    """Explicit compatibility field backed by control configuration."""

    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return getattr(instance.control, self.name)

    def __set__(self, instance, value) -> None:
        setattr(instance.control, self.name, value)


class RiskSidecarCompatibilityFields:
    """Legacy field names backed by the extracted sidecar controllers."""

    funding_observations = _FundingStateAttribute("observations")
    funding_guard_states = _FundingStateAttribute("guard_states")
    funding_guard_decisions = _FundingStateAttribute("decisions")
    funding_action = _FundingStateAttribute("action")
    funding_reason = _FundingStateAttribute("reason")
    deployment_id = _EquityStateAttribute("deployment_id")
    risk_day = _EquityStateAttribute("risk_day")
    day_start_equity = _EquityStateAttribute("day_start_equity")
    day_start_external_cash_flow_total = _EquityStateAttribute(
        "day_start_external_cash_flow_total"
    )
    peak_adjusted_equity = _EquityStateAttribute("peak_adjusted_equity")
    last_equity = _EquityStateAttribute("last_equity")
    deployment_start_equity = _EquityStateAttribute(
        "deployment_start_equity"
    )
    deployment_start_external_cash_flow_total = _EquityStateAttribute(
        "deployment_start_external_cash_flow_total"
    )
    deployment_adjusted_equity = _EquityStateAttribute(
        "deployment_adjusted_equity"
    )
    deployment_loss = _EquityStateAttribute("deployment_loss")
    kill_latched = _ControlStateAttribute("kill_latched")
    kill_reason = _ControlStateAttribute("kill_reason")
    stage = _ControlStateAttribute("stage")
    unsafe_since = _ControlStateAttribute("unsafe_since")
    flat_verification_count = _ControlStateAttribute(
        "flat_verification_count"
    )
    last_verified_snapshot_sequence = _ControlStateAttribute(
        "last_verified_snapshot_sequence"
    )
    quiesced = _ControlStateAttribute("quiesced")
    quiesce_reason = _ControlStateAttribute("quiesce_reason")
    quiesced_at = _ControlStateAttribute("quiesced_at")
    quiesce_snapshot_sequence = _ControlStateAttribute(
        "quiesce_snapshot_sequence"
    )
    stop_requested = _ControlStateAttribute("stop_requested")
    stop_request_id = _ControlStateAttribute("stop_request_id")
    cancel_on_stop = _ControlStateAttribute("cancel_on_stop")
    last_quiesce_request_id = _ControlStateAttribute(
        "last_quiesce_request_id"
    )
    last_quiesce_accepted = _ControlStateAttribute(
        "last_quiesce_accepted"
    )
    last_quiesce_reason = _ControlStateAttribute("last_quiesce_reason")
    last_quiesce_persisted = _ControlStateAttribute(
        "last_quiesce_persisted"
    )
    last_shutdown_resume_request_id = _ControlStateAttribute(
        "last_shutdown_resume_request_id"
    )
    last_shutdown_resume_accepted = _ControlStateAttribute(
        "last_shutdown_resume_accepted"
    )
    last_shutdown_resume_reason = _ControlStateAttribute(
        "last_shutdown_resume_reason"
    )
    last_shutdown_resume_persisted = _ControlStateAttribute(
        "last_shutdown_resume_persisted"
    )
    last_stop_request_id = _ControlStateAttribute("last_stop_request_id")
    last_stop_accepted = _ControlStateAttribute("last_stop_accepted")
    last_stop_reason = _ControlStateAttribute("last_stop_reason")
    last_stop_quiesced = _ControlStateAttribute("last_stop_quiesced")
    last_stop_cancel_requested = _ControlStateAttribute(
        "last_stop_cancel_requested"
    )
    last_stop_cancel_attempted = _ControlStateAttribute(
        "last_stop_cancel_attempted"
    )
    last_stop_cancel_ok = _ControlStateAttribute("last_stop_cancel_ok")
    prepared_rearm = _ControlStateAttribute("prepared_rearm")
    last_rearm_request_id = _ControlStateAttribute(
        "last_rearm_request_id"
    )
    last_rearm_phase = _ControlStateAttribute("last_rearm_phase")
    last_rearm_accepted = _ControlStateAttribute("last_rearm_accepted")
    last_rearm_reason = _ControlStateAttribute("last_rearm_reason")
    last_rearm_token = _ControlStateAttribute("last_rearm_token")
    cancel_retry_sec = _ControlAttribute("cancel_retry_sec")
    flatten_enabled = _ControlAttribute("flatten_enabled")
    flatten_retry_sec = _ControlAttribute("flatten_retry_sec")
    flat_verification_checks = _ControlAttribute(
        "flat_verification_checks"
    )
    rearm_prepare_ttl_sec = _ControlAttribute("rearm_prepare_ttl_sec")
    snapshot_worker = _ObservationAttribute("snapshot_worker")
    account_risk = _ObservationAttribute("account_risk")
    funding_risk = _ObservationAttribute("funding_risk")
    last_exchange_poll_at = _ObservationAttribute(
        "last_exchange_poll_at"
    )
    last_exchange_success_at = _ObservationAttribute(
        "last_exchange_success_at"
    )
    last_snapshot_result_sequence = _ObservationAttribute(
        "last_snapshot_result_sequence"
    )
    snapshot_request_sequence = _ObservationAttribute(
        "snapshot_request_sequence"
    )
    snapshot_request_inflight_sequence = _ObservationAttribute(
        "snapshot_request_inflight_sequence"
    )
    snapshot_request_inflight_since = _ObservationAttribute(
        "snapshot_request_inflight_since"
    )
    risk_snapshot_captured_at = _ObservationAttribute(
        "risk_snapshot_captured_at"
    )
    risk_snapshot_captured_monotonic = _ObservationAttribute(
        "risk_snapshot_captured_monotonic"
    )
    exchange_healthy = _ObservationAttribute("exchange_healthy")
    exchange_reason = _ObservationAttribute("exchange_reason")
    risk_action = _ObservationAttribute("risk_action")
    risk_reason = _ObservationAttribute("risk_reason")
    risk_metrics = _ObservationAttribute("risk_metrics")
    risk_snapshot_sequence = _ObservationAttribute(
        "risk_snapshot_sequence"
    )

