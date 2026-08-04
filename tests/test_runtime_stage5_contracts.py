from types import SimpleNamespace
from threading import Event

import pytest

from infrastructure.runtime_readiness import RuntimeReadinessEvaluator
from infrastructure.runtime_telemetry import TelemetryPublisher
from infrastructure.time_service import TimeService
from strategy.avellaneda_stoikov import AvellanedaStoikovStrategy
from strategy.contracts import OMSStrategyExecutionAdapter
from strategy.glft import GLFTStrategy


def test_strategy_snapshot_is_deeply_immutable_and_detached_from_oms():
    positions = {"BTCUSDT": 1.25}
    strategy_positions = {("maker", "BTCUSDT"): 0.5}
    gate = {"rpi_calibration": {"enabled": True}}
    oms = SimpleNamespace(
        state=SimpleNamespace(value="LIVE"),
        capability_mode=SimpleNamespace(value="LIVE"),
        capability_reason="",
        exposure=SimpleNamespace(
            net_positions=positions,
            strategy_net_positions=strategy_positions,
        ),
        account=SimpleNamespace(equity=1000.0, used_margin=100.0),
        get_outbound_gate_snapshot=lambda: gate,
    )
    port = OMSStrategyExecutionAdapter(oms)

    snapshot = port.state_snapshot("maker")
    positions["BTCUSDT"] = 9.0
    strategy_positions[("maker", "BTCUSDT")] = 8.0
    gate["rpi_calibration"]["enabled"] = False

    assert snapshot.position("BTCUSDT") == 1.25
    assert snapshot.strategy_position("BTCUSDT") == 0.5
    assert snapshot.outbound_gate["rpi_calibration"]["enabled"] is True
    with pytest.raises(TypeError):
        snapshot.positions["BTCUSDT"] = 2.0
    with pytest.raises(TypeError):
        snapshot.outbound_gate["rpi_calibration"]["enabled"] = False


@pytest.mark.parametrize(
    "strategy_type",
    (GLFTStrategy, AvellanedaStoikovStrategy),
)
def test_market_maker_constructor_requires_resolved_strategy_config(
    strategy_type,
):
    with pytest.raises(TypeError, match="resolved"):
        strategy_type(object(), object(), None)


def test_readiness_evaluator_fails_closed_for_unknown_required_component():
    evaluator = RuntimeReadinessEvaluator(monotonic=lambda: 12.5)

    snapshot = evaluator.evaluate(
        {"clock": True, "risk": None},
        required=("clock", "risk"),
        phase="startup",
        execution_enabled=False,
        require_execution=False,
        operating_mode="READ_ONLY",
    )

    assert snapshot.ready is False
    assert snapshot.reasons == ("risk_unknown",)
    assert snapshot.component_state("clock") == "ready"
    assert snapshot.as_dict()["evaluated_at_monotonic"] == 12.5


def test_readiness_evaluator_uses_same_policy_for_runtime_execution():
    evaluator = RuntimeReadinessEvaluator(monotonic=lambda: 1.0)
    components = {"clock": True, "risk": True}

    startup = evaluator.evaluate(
        components,
        required=("clock", "risk"),
        phase="startup",
        execution_enabled=False,
        require_execution=False,
    )
    runtime = evaluator.evaluate(
        components,
        required=("clock", "risk"),
        phase="runtime",
        execution_enabled=False,
        require_execution=True,
    )

    assert startup.ready is True
    assert runtime.ready is False
    assert runtime.reasons == ("execution_disabled",)


def test_telemetry_publisher_replaces_backlog_with_latest_value():
    first_started = Event()
    release_first = Event()
    published = []

    def publish(snapshot):
        published.append(snapshot["sequence"])
        if snapshot["sequence"] == 1:
            first_started.set()
            assert release_first.wait(timeout=2.0)

    publisher = TelemetryPublisher(publish)
    assert publisher.submit({"sequence": 1})
    assert first_started.wait(timeout=2.0)

    for sequence in range(2, 101):
        assert publisher.submit({"sequence": sequence})
    release_first.set()

    assert publisher.flush(timeout_sec=2.0)
    metrics = publisher.metrics_snapshot()
    assert publisher.close(timeout_sec=2.0)
    assert published == [1, 100]
    assert metrics["mailbox_capacity"] == 1
    assert metrics["replaced"] == 98


def test_time_service_listener_unsubscribe_is_owned_and_idempotent():
    service = TimeService()
    service.stop()
    service.clear_listeners()

    def first(*_args):
        return None

    def second(*_args):
        return None

    unsubscribe_first = service.register_listener(first)
    service.register_listener(second)

    assert unsubscribe_first() is True
    assert unsubscribe_first() is False
    assert first not in service.listeners
    assert second in service.listeners
    service.clear_listeners()
