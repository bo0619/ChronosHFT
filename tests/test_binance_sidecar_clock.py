from unittest.mock import patch

from risk.binance_sidecar_clock import BinanceSidecarClock


def _finite_float(value, label):
    result = float(value)
    if result != result:
        raise ValueError(f"{label} must be finite")
    return result


class _Owner:
    def __init__(self):
        self.clock_sync_enabled = True
        self.clock_sync_interval_sec = 30.0
        self.clock_sample_count = 1
        self.clock_min_successful_samples = 1
        self.clock_low_rtt_sample_count = 1
        self.clock_sample_spacing_ms = 0.0
        self.clock_max_rtt_ms = 200.0
        self.clock_max_uncertainty_ms = 50.0
        self.clock_max_offset_dispersion_ms = 10.0
        self.clock_max_wall_step_ms = 20.0
        self.clock_max_initial_offset_ms = 5_000.0
        self.clock_reduce_only_phase_error_ms = 25.0
        self.clock_kill_phase_error_ms = 40.0
        self.last_clock_sync_monotonic = 0.0
        self.clock_offset_ms = 0.0
        self.clock_phase_error_ms = 0.0
        self.clock_rtt_ms = 0.0
        self.clock_uncertainty_ms = 0.0
        self.clock_offset_dispersion_ms = 0.0
        self._clock_anchor_epoch_ms = 0.0
        self._clock_anchor_monotonic = 0.0
        self.clock_reason = "clock_sync_missing"
        self.samples = None
        self.sync_calls = []

    @staticmethod
    def _response_payload(response, expected_type, _label):
        payload = response.json()
        return (
            response.status_code == 200 and isinstance(payload, expected_type),
            payload,
            "",
        )

    def _collect_clock_samples(self, *, emergency=False):
        return self.samples, ""

    def sync_exchange_clock(self, *, emergency=False):
        self.sync_calls.append(emergency)
        return True, ""


def test_corrected_epoch_uses_monotonic_anchor_and_rejects_regression():
    owner = _Owner()
    owner._clock_anchor_epoch_ms = 1_000_000.0
    owner._clock_anchor_monotonic = 10.0
    clock = BinanceSidecarClock(owner, _finite_float)

    assert clock.corrected_epoch_at(12.5) == 1_002.5
    assert clock.corrected_epoch_at(9.9) is None


def test_cached_clock_avoids_sync_but_force_uses_emergency_channel():
    owner = _Owner()
    owner.last_clock_sync_monotonic = 90.0
    owner.clock_reason = ""
    clock = BinanceSidecarClock(owner, _finite_float)

    with patch(
        "risk.binance_sidecar_clock.time.perf_counter",
        return_value=100.0,
    ):
        assert clock.ensure() == (True, "")
        assert owner.sync_calls == []
        assert clock.ensure(force=True) == (True, "")

    assert owner.sync_calls == [True]


def test_kill_phase_candidate_preserves_last_known_good_anchor():
    owner = _Owner()
    owner.samples = {
        "offset_ms": 50.0,
        "rtt_ms": 2.0,
        "dispersion_ms": 1.0,
        "uncertainty_ms": 2.0,
    }
    owner._clock_anchor_epoch_ms = 1_000_000.0
    owner._clock_anchor_monotonic = 10.0
    clock = BinanceSidecarClock(owner, _finite_float)

    with (
        patch(
            "risk.binance_sidecar_clock.time.perf_counter",
            return_value=20.0,
        ),
        patch(
            "risk.binance_sidecar_clock.time.time",
            return_value=1_010.0,
        ),
    ):
        ok, reason = clock.sync()

    assert not ok
    assert reason == "clock_phase_error_kill:50.000ms"
    assert owner._clock_anchor_epoch_ms == 1_000_000.0
    assert owner._clock_anchor_monotonic == 10.0
    assert owner.clock_phase_error_ms == 50.0
