from risk.independent_supervisor import RiskSidecarCore
from risk.sidecar_observation import SidecarObservationController


class AccountRisk:
    def initial_metrics(self, funding):
        return {
            "open_order_count": 0,
            "nonzero_position_count": 0,
            "funding_guard": funding,
        }

    def evaluate(self, snapshot):
        return (
            snapshot.get("action", "NONE"),
            snapshot.get("reason", ""),
            {
                "open_order_count": len(snapshot.get("open_orders", [])),
                "nonzero_position_count": len(
                    snapshot.get("positions", [])
                ),
            },
        )

    def fallback_metrics(self, _snapshot):
        return {"open_order_count": 0, "nonzero_position_count": 0}


class FundingRisk:
    def __init__(self):
        self.ingested = []

    def initial_metrics(self):
        return {"enabled": False}

    def ingest(self, snapshot):
        self.ingested.append(snapshot)

    def evaluate(self, _now):
        return "NONE", "", {"enabled": False}


class Worker:
    def __init__(self, results=()):
        self.results = list(results)
        self.submitted = []
        self.thread = None

    def take_latest(self):
        return self.results.pop(0) if self.results else None

    def submit(self, sequence, now):
        self.submitted.append((sequence, now))
        return True


def make_observation(*, worker=None, exchange=None):
    return SidecarObservationController(
        exchange=exchange or object(),
        snapshot_worker=worker,
        account_risk=AccountRisk(),
        funding_risk=FundingRisk(),
        exchange_poll_interval_sec=1.0,
        exchange_max_age_sec=2.0,
        snapshot_worker_timeout_sec=1.0,
        clock_failure_requires_kill=lambda reason: reason.startswith(
            "clock_"
        ),
        wall_time=lambda: 123.0,
    )


def test_full_snapshot_commits_evaluated_truth_and_generation():
    observation = make_observation()
    snapshot = {
        "captured_at": 100.0,
        "open_orders": [{"orderId": 1}],
        "positions": [],
    }

    observation.apply_result(
        healthy=True,
        snapshot=snapshot,
        reason="",
        completed_monotonic=10.0,
        completed_at=100.0,
        full_snapshot=True,
    )

    assert observation.exchange_healthy
    assert observation.risk_snapshot_sequence == 1
    assert observation.risk_snapshot_captured_monotonic == 10.0
    assert observation.risk_snapshot_captured_at == 100.0
    assert observation.account_truth_counts() == (1, 0)
    assert observation.funding_risk.ingested == [snapshot]


def test_worker_result_must_match_requested_generation_and_timestamps():
    worker = Worker(
        [
            {
                "sequence": 2,
                "requested_monotonic": 8.0,
                "completed_monotonic": 9.0,
                "completed_at": 100.0,
                "healthy": True,
                "snapshot": {},
                "full_snapshot": True,
            }
        ]
    )
    observation = make_observation(worker=worker)
    observation.snapshot_request_inflight_sequence = 1
    observation.snapshot_request_inflight_since = 9.5

    observation.service_worker(10.0)

    assert not observation.exchange_healthy
    assert observation.exchange_reason == (
        "exchange_snapshot_worker_timestamp_invalid"
    )
    assert observation.risk_action == "REDUCE_ONLY"
    assert observation.risk_snapshot_sequence == 0


def test_worker_request_state_is_owned_and_generation_fenced():
    worker = Worker()
    observation = make_observation(worker=worker)

    observation.service_worker(10.0, force=True)

    assert worker.submitted == [(1, 10.0)]
    assert observation.snapshot_request_sequence == 1
    assert observation.snapshot_request_inflight_sequence == 1
    assert observation.snapshot_request_inflight_since == 10.0


def test_snapshot_valid_requires_one_fresh_coherent_capture():
    observation = make_observation()
    observation.exchange_healthy = True
    observation.risk_snapshot_sequence = 1
    observation.last_exchange_success_at = 10.0
    observation.risk_snapshot_captured_monotonic = 10.0

    assert observation.snapshot_valid(11.0)
    assert not observation.snapshot_valid(13.0)
    observation.risk_snapshot_captured_monotonic = 9.5
    assert not observation.snapshot_valid(10.0)


def test_clock_failure_escalates_observation_to_kill():
    observation = make_observation()

    observation.mark_unhealthy("clock_phase_error_kill:250ms")

    assert observation.risk_action == "KILL"
    assert observation.risk_reason == "clock_phase_error_kill:250ms"


def test_core_observation_fields_are_not_duplicated_on_owner():
    core = RiskSidecarCore(
        object(),
        {"symbols": ["BTCUSDT"]},
        now=10.0,
    )
    owned_fields = {
        "snapshot_worker",
        "account_risk",
        "funding_risk",
        "last_exchange_success_at",
        "exchange_healthy",
        "exchange_reason",
        "risk_action",
        "risk_reason",
        "risk_metrics",
        "risk_snapshot_sequence",
    }

    assert isinstance(core.observation, SidecarObservationController)
    assert owned_fields.isdisjoint(core.__dict__)
    core.exchange_healthy = True
    core.risk_snapshot_sequence = 7
    assert core.observation.exchange_healthy
    assert core.observation.risk_snapshot_sequence == 7
    assert "owner" not in core.observation.__dict__
    assert "core" not in core.observation.__dict__
