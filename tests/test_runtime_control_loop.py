from types import SimpleNamespace

from infrastructure import runtime_control_loop as loop_module
from infrastructure.runtime_control_loop import (
    RuntimeControlLoop,
    RuntimeControlServices,
)
from infrastructure.runtime_readiness import RuntimeReadinessEvaluator


class _Watchdog:
    def __init__(self, calls):
        self.calls = calls

    def pulse(self):
        self.calls.append(("watchdog.pulse",))

    def snapshot(self):
        return {"healthy": True}


class _Engine:
    def get_metrics_snapshot(self):
        return {"engine": True}

    def get_handler_metrics_snapshot(self, *, limit):
        return {"limit": limit}


class _StrategyRuntime:
    def get_metrics_snapshot(self):
        return {"strategy": True}


class _ResourceMonitor:
    def __init__(self, calls, *, failure=""):
        self.calls = calls
        self.failure = failure

    def sample(self, pids):
        self.calls.append(("resources.sample", list(pids)))
        return {"sampled_at_monotonic": 12.5, "healthy": not self.failure}

    def consume_fail_closed_reason(self):
        return self.failure


class _OMS:
    def __init__(self, calls):
        self.calls = calls

    def freeze_system(self, reason, *, cancel_active_orders):
        self.calls.append(("oms.freeze", reason, cancel_active_orders))


class _Bindings:
    def __init__(self, calls):
        self.calls = calls

    def record_paper_observation(self, *args):
        self.calls.append(("paper.record", *args))


class _Dashboard:
    def __init__(self, calls):
        self.calls = calls

    def update_runtime_metrics(self, metrics):
        self.calls.append(("dashboard.metrics", metrics))


class _TelemetryPublisher:
    def __init__(self, calls):
        self.calls = calls

    def submit(self, metrics):
        self.calls.append(("dashboard.metrics", metrics))
        return True


class _Admin:
    def __init__(self, calls):
        self.calls = calls

    def poll_once(self):
        self.calls.append(("admin.poll",))


class _Logger:
    def get_metrics_snapshot(self):
        return {"logger": True}


def _build_loop(
    monkeypatch,
    *,
    paper_trade=True,
    resource_failure="",
    with_readiness=False,
    clock_ready=True,
):
    calls = []
    monkeypatch.setattr(
        loop_module,
        "emit_market_data_stale_if_needed",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        loop_module,
        "emit_event_engine_backlog_if_needed",
        lambda *_args: {"engine": "watched"},
    )
    monkeypatch.setattr(
        loop_module,
        "emit_strategy_runtime_backlog_if_needed",
        lambda *_args: {"strategy": "watched"},
    )

    services = RuntimeControlServices(
        enforce_external_alert_health=lambda *_args: calls.append(
            ("alerts.enforce",)
        ),
        enforce_live_evidence_health=lambda *_args: calls.append(
            ("evidence.enforce",)
        ),
        run_live_risk_checks=lambda *_args: calls.append(("risk.check",)),
        external_alert_health=lambda _service: {"alerts": True},
        live_evidence_health=lambda _recorder: {"evidence": True},
        build_runtime_resource_event=lambda resource, watchdog: {
            "resource": resource,
            "watchdog": watchdog,
        },
    )
    watchdog_state = SimpleNamespace(
        last_tick_time=1.0,
        stale_watchdog_triggered=False,
        event_engine={},
        strategy_runtime={},
    )
    data_recorder = SimpleNamespace(
        _writer_process=SimpleNamespace(pid=101)
    )
    risk_supervisor = SimpleNamespace(process=SimpleNamespace(pid=202))
    oms = _OMS(calls)
    control_loop = RuntimeControlLoop(
        config={"system": {"strategy_runtime": {"capacity": 10}}},
        paper_trade=paper_trade,
        systemd_watchdog=_Watchdog(calls),
        external_alerts=object(),
        live_evidence_recorder=object(),
        oms=oms,
        risk_controller=object(),
        risk_supervisor=risk_supervisor,
        engine=_Engine(),
        event_engine_config={"capacity": 20},
        gateway=SimpleNamespace(gateway_name="BINANCE"),
        strategy=SimpleNamespace(name="maker"),
        strategy_runtime=_StrategyRuntime(),
        data_recorder=data_recorder,
        resource_monitor=_ResourceMonitor(
            calls,
            failure=resource_failure,
        ),
        event_bindings=_Bindings(calls),
        watchdog_state=watchdog_state,
        web_dashboard=_Dashboard(calls),
        admin_control=_Admin(calls),
        logger=_Logger(),
        services=services,
        telemetry_publisher=_TelemetryPublisher(calls),
        readiness_evaluator=(
            RuntimeReadinessEvaluator(monotonic=lambda: 7.0)
            if with_readiness
            else None
        ),
        readiness_components=(
            {
                "risk_policy": True,
                "resources": True,
                "alerts": True,
                "clock": None,
                "execution": True,
            }
            if with_readiness
            else None
        ),
        readiness_required=(
            ("risk_policy", "resources", "alerts", "clock")
            if with_readiness
            else ()
        ),
        clock_service=object() if with_readiness else None,
        read_clock_health=(
            (lambda _clock: {"ready": clock_ready})
            if with_readiness
            else None
        ),
    )
    return control_loop, watchdog_state, calls


def test_run_once_executes_health_watchdogs_resources_and_outputs(monkeypatch):
    control_loop, watchdog_state, calls = _build_loop(monkeypatch)

    metrics = control_loop.run_once()

    assert calls[:4] == [
        ("watchdog.pulse",),
        ("alerts.enforce",),
        ("risk.check",),
        ("resources.sample", [101, 202]),
    ]
    assert calls[-2][0] == "dashboard.metrics"
    assert calls[-1] == ("admin.poll",)
    assert watchdog_state.stale_watchdog_triggered is True
    assert watchdog_state.event_engine == {"engine": "watched"}
    assert watchdog_state.strategy_runtime == {"strategy": "watched"}
    assert metrics["event_handlers"] == {"limit": 50}
    assert metrics["external_alerts"] == {"alerts": True}


def test_paper_resource_observation_is_recorded_once_per_sample(monkeypatch):
    control_loop, _, calls = _build_loop(monkeypatch)

    control_loop.run_once()
    control_loop.run_once()

    paper_calls = [call for call in calls if call[0] == "paper.record"]
    assert len(paper_calls) == 1
    assert paper_calls[0][1:3] == (
        "record_paper_system_event",
        "runtime_resources",
    )


def test_live_tick_enforces_evidence_and_skips_paper_observation(monkeypatch):
    control_loop, _, calls = _build_loop(monkeypatch, paper_trade=False)

    control_loop.run_once()

    assert ("evidence.enforce",) in calls
    assert not any(call[0] == "paper.record" for call in calls)


def test_resource_failure_freezes_system(monkeypatch):
    control_loop, _, calls = _build_loop(
        monkeypatch,
        resource_failure="rss_limit",
    )

    control_loop.run_once()

    assert ("oms.freeze", "ProcessResources: rss_limit", True) in calls


def test_runtime_tick_publishes_authoritative_readiness_snapshot(monkeypatch):
    control_loop, _, calls = _build_loop(
        monkeypatch,
        with_readiness=True,
    )

    metrics = control_loop.run_once()

    assert metrics["runtime_readiness"]["ready"] is True
    assert metrics["runtime_readiness"]["phase"] == "runtime"
    published = [call for call in calls if call[0] == "dashboard.metrics"]
    assert published[-1][1]["runtime_readiness"] == (
        metrics["runtime_readiness"]
    )


def test_runtime_tick_invalidates_readiness_when_clock_degrades(monkeypatch):
    control_loop, _, _ = _build_loop(
        monkeypatch,
        with_readiness=True,
        clock_ready=False,
    )

    metrics = control_loop.run_once()

    assert metrics["runtime_readiness"]["ready"] is False
    assert "clock_unready" in metrics["runtime_readiness"]["reasons"]
