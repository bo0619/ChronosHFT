import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")

    class Request:
        def __init__(self, method, url, params=None, headers=None):
            self.method = method
            self.url = url
            self.params = params or {}
            self.headers = headers or {}

    requests_stub.Request = Request
    requests_stub.Session = lambda: None
    requests_stub.get = lambda *args, **kwargs: None
    sys.modules["requests"] = requests_stub

from infrastructure.time_service import TimeService


class DummyResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class TimeServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = TimeService()
        self.service.stop()
        self.service.clear_listeners()
        self.service._offset_ms = 0.0
        self.service.last_sync_time = 0.0
        self.service.last_rtt_ms = 0.0
        self.service.last_error = ""
        self.service.consecutive_failures = 0
        self.service.freeze_breach_count = 0
        self.service.halt_breach_count = 0
        self.service.recovery_success_count = 0
        self.service._health_state = "unsynchronized"
        self.service._synchronized = False
        self.service._ready = False
        self.service._anchor_epoch_ns = 0
        self.service._anchor_mono_ns = 0
        self.service._anchor_wall_ns = 0
        self.service._anchor_offset_ms = 0.0
        self.service._last_sync_mono_ns = 0
        self.service._last_now_ns = 0
        self.service._last_phase_error_ms = 0.0
        self.service._last_notified_fault = ""
        self.service.configure(
            {
                # Legacy keys remain supported, but now mean phase error.
                "max_offset_ms": 25.0,
                "halt_offset_ms": 100.0,
                "max_initial_offset_ms": 5000.0,
                "max_rtt_ms": 5000.0,
                "max_uncertainty_ms": 5000.0,
                "max_consecutive_failures": 2,
                "freeze_breach_threshold": 1,
                "halt_breach_threshold": 1,
                "recovery_success_threshold": 1,
                "sample_count": 1,
                "min_successful_samples": 1,
                "low_rtt_sample_count": 1,
                "sample_spacing_ms": 0.0,
                "max_offset_dispersion_ms": 25.0,
                "max_sync_age_sec": 10.0,
                "max_wall_clock_step_ms": 100.0,
                "health_poll_interval_sec": 0.01,
            }
        )
        self.events = []
        self.service.register_listener(
            lambda severity, reason, details: self.events.append((severity, reason, details))
        )

    def _sync_samples(self, samples, *, wall_ns=1_000_000_000_000, mono_ns=10_000_000_000):
        with (
            patch.object(self.service, "_request_sample", side_effect=samples),
            patch.object(self.service, "monotonic_ns", return_value=mono_ns),
            patch.object(self.service, "_wall_time_ns", return_value=wall_ns),
        ):
            return self.service._sync()

    def test_stable_large_offset_is_corrected_and_phase_jump_freezes(self):
        self.assertTrue(
            self._sync_samples([{"offset_ms": -800.0, "rtt_ms": 2.0}])
        )
        self.assertTrue(
            self._sync_samples(
                [{"offset_ms": -800.0, "rtt_ms": 2.0}],
                wall_ns=1_010_000_000_000,
                mono_ns=20_000_000_000,
            )
        )
        self.assertEqual(self.events, [])
        self.assertEqual(self.service.offset, -800.0)
        self.assertEqual(
            self.service.health_snapshot()["phase_error_ms"],
            0.0,
        )

        self.assertFalse(
            self._sync_samples(
                [{"offset_ms": -750.0, "rtt_ms": 2.0}],
                wall_ns=1_020_000_000_000,
                mono_ns=30_000_000_000,
            )
        )
        self.assertEqual(self.events[-1][0], "freeze")
        self.assertIn("phase error", self.events[-1][1])

        self.assertTrue(
            self._sync_samples(
                [{"offset_ms": -800.0, "rtt_ms": 2.0}],
                wall_ns=1_030_000_000_000,
                mono_ns=40_000_000_000,
            )
        )
        self.assertEqual(self.events[-1][0], "recovered")

    def test_phase_error_waits_for_configured_breach_threshold(self):
        self.service.configure(
            {
                "freeze_breach_threshold": 2,
                "halt_breach_threshold": 2,
                "recovery_success_threshold": 2,
            }
        )
        self._sync_samples([{"offset_ms": -800.0, "rtt_ms": 2.0}])
        self._sync_samples([{"offset_ms": -750.0, "rtt_ms": 2.0}])
        self.assertEqual(self.events, [])
        self._sync_samples([{"offset_ms": -750.0, "rtt_ms": 2.0}])
        self.assertEqual(self.events[0][0], "freeze")
        self._sync_samples([{"offset_ms": -800.0, "rtt_ms": 2.0}])
        self.assertEqual(len(self.events), 1)
        self._sync_samples([{"offset_ms": -800.0, "rtt_ms": 2.0}])
        self.assertEqual(self.events[1][0], "recovered")

    def test_hard_phase_breach_freezes_before_halt_quorum(self):
        self.service.configure(
            {
                "freeze_breach_threshold": 1,
                "halt_breach_threshold": 2,
            }
        )
        self.assertTrue(
            self._sync_samples(
                [{"offset_ms": -800.0, "rtt_ms": 2.0}],
                wall_ns=1_000_000_000_000,
                mono_ns=10_000_000_000,
            )
        )
        original_anchor = (
            self.service._anchor_epoch_ns,
            self.service._anchor_mono_ns,
        )

        self.assertFalse(
            self._sync_samples(
                [{"offset_ms": -680.0, "rtt_ms": 2.0}],
                wall_ns=1_010_000_000_000,
                mono_ns=20_000_000_000,
            )
        )
        self.assertEqual(self.events[-1][0], "freeze")
        self.assertEqual(self.service._health_state, "freeze")
        self.assertEqual(
            (self.service._anchor_epoch_ns, self.service._anchor_mono_ns),
            original_anchor,
        )

        self.assertFalse(
            self._sync_samples(
                [{"offset_ms": -680.0, "rtt_ms": 2.0}],
                wall_ns=1_020_000_000_000,
                mono_ns=30_000_000_000,
            )
        )
        self.assertEqual(self.events[-1][0], "halt")
        self.assertEqual(self.service._health_state, "halt")

    def test_clock_config_rejects_non_finite_or_negative_values(self):
        invalid_configs = (
            {"max_rtt_ms": float("nan")},
            {"max_phase_error_ms": float("inf")},
            {"max_initial_offset_ms": -1.0},
            {"sample_count": float("nan")},
            {"sample_spacing_ms": -0.1},
        )
        for invalid in invalid_configs:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.service.configure(invalid)

    def test_explicit_phase_thresholds_update_legacy_aliases(self):
        self.service.configure(
            {
                "max_phase_error_ms": 30.0,
                "halt_phase_error_ms": 120.0,
            }
        )

        self.assertEqual(self.service.max_phase_error_ms, 30.0)
        self.assertEqual(self.service.halt_phase_error_ms, 120.0)
        self.assertEqual(self.service.max_offset_ms, 30.0)
        self.assertEqual(self.service.halt_offset_ms, 120.0)

    def test_initial_offset_over_hard_limit_is_halted_without_anchor(self):
        self.assertFalse(
            self._sync_samples([{"offset_ms": 5001.0, "rtt_ms": 1.0}])
        )

        self.assertEqual(self.events[-1][0], "halt")
        self.assertIn("initial clock offset", self.events[-1][1])
        self.assertFalse(self.service._synchronized)
        self.assertEqual(self.service._anchor_epoch_ns, 0)

    def test_initial_healthy_calibration_is_ready_without_recovery_quorum(self):
        self.service.configure({"recovery_success_threshold": 3})

        self.assertTrue(
            self._sync_samples([{"offset_ms": 10.0, "rtt_ms": 2.0}])
        )

        with (
            patch.object(
                self.service,
                "monotonic_ns",
                return_value=10_000_000_000,
            ),
            patch.object(
                self.service,
                "_wall_time_ns",
                return_value=1_000_000_000_000,
            ),
        ):
            self.assertTrue(self.service.ready)
            self.assertEqual(
                self.service.health_snapshot()["state"],
                "healthy",
            )

    def test_selects_median_from_lowest_rtt_samples(self):
        self.service.configure(
            {
                "sample_count": 5,
                "min_successful_samples": 3,
                "low_rtt_sample_count": 3,
            }
        )
        samples = [
            {"offset_ms": 900.0, "rtt_ms": 100.0},
            {"offset_ms": 20.0, "rtt_ms": 1.0},
            {"offset_ms": 21.0, "rtt_ms": 2.0},
            {"offset_ms": 19.0, "rtt_ms": 3.0},
            {"offset_ms": -700.0, "rtt_ms": 80.0},
        ]

        self.assertTrue(self._sync_samples(samples))
        with (
            patch.object(self.service, "monotonic_ns", return_value=10_000_000_000),
            patch.object(self.service, "_wall_time_ns", return_value=1_000_000_000_000),
        ):
            snapshot = self.service.health_snapshot()

        self.assertAlmostEqual(self.service.offset, 20.0)
        self.assertAlmostEqual(self.service.last_rtt_ms, 2.0)
        self.assertEqual(snapshot["samples"], 5)
        self.assertEqual(snapshot["selected_samples"], 3)
        self.assertTrue(snapshot["ready"])

    @patch("infrastructure.time_service.requests.get")
    def test_request_sample_uses_monotonic_rtt_and_midpoint(self, mock_get):
        mock_get.return_value = DummyResponse({"serverTime": 1_000_103.0})
        with (
            patch.object(
                self.service,
                "monotonic_ns",
                side_effect=[1_000_000_000, 1_004_000_000],
            ),
            patch.object(
                self.service,
                "_wall_time_ns",
                side_effect=[1_000_000_000_000, 1_000_006_000_000],
            ),
        ):
            sample = self.service._request_sample()

        self.assertAlmostEqual(sample["rtt_ms"], 4.0)
        self.assertAlmostEqual(sample["offset_ms"], 101.0)
        mock_get.assert_called_once_with(
            self.service.url, timeout=self.service.request_timeout_sec
        )

    @patch("infrastructure.time_service.requests.get")
    def test_request_sample_rejects_wall_step(self, mock_get):
        mock_get.return_value = DummyResponse({"serverTime": 1_000_103.0})
        self.service.max_wall_clock_step_ms = 20.0
        with (
            patch.object(
                self.service,
                "monotonic_ns",
                side_effect=[1_000_000_000, 1_001_000_000],
            ),
            patch.object(
                self.service,
                "_wall_time_ns",
                side_effect=[1_000_000_000_000, 1_000_100_000_000],
            ),
            self.assertRaisesRegex(ValueError, "wall clock stepped"),
        ):
            self.service._request_sample()

    def test_startup_sync_failure_is_immediately_fail_closed(self):
        with patch.object(
            self.service, "_request_sample", side_effect=RuntimeError("network down")
        ):
            self.assertFalse(self.service._sync())

        self.assertFalse(self.service.ready)
        self.assertEqual(self.events[0][0], "halt")
        self.assertIn("startup time sync failed", self.events[0][1])

    def test_runtime_sync_failure_freezes_then_halts(self):
        self.assertTrue(
            self._sync_samples([{"offset_ms": 10.0, "rtt_ms": 2.0}])
        )
        with patch.object(
            self.service, "_request_sample", side_effect=RuntimeError("network down")
        ):
            self.service._sync()
            self.service._sync()

        self.assertEqual(self.events[-2][0], "freeze")
        self.assertEqual(self.events[-1][0], "halt")
        self.assertIn("failed 2 times", self.events[-1][1])

    def test_corrected_epoch_advances_only_with_monotonic_clock(self):
        anchor_wall_ns = 1_000_000_000_000
        anchor_mono_ns = 10_000_000_000
        self._sync_samples(
            [{"offset_ms": 25.0, "rtt_ms": 1.0}],
            wall_ns=anchor_wall_ns,
            mono_ns=anchor_mono_ns,
        )
        with (
            patch.object(
                self.service,
                "monotonic_ns",
                side_effect=[10_100_000_000, 10_200_000_000],
            ),
            patch.object(
                self.service,
                "_wall_time_ns",
                side_effect=AssertionError("hot path must not read wall time"),
            ),
        ):
            first = self.service.now_ns()
            second = self.service.now_ns()

        self.assertEqual(first, anchor_wall_ns + 25_000_000 + 100_000_000)
        self.assertEqual(second - first, 100_000_000)

    def test_capture_timestamp_uses_anchor_when_wall_clock_steps(self):
        self._sync_samples(
            [{"offset_ms": 10.0, "rtt_ms": 1.0}],
            wall_ns=1_000_000_000_000,
            mono_ns=10_000_000_000,
        )
        with (
            patch.object(
                self.service,
                "monotonic_ns",
                return_value=10_100_000_000,
            ),
            patch.object(
                self.service,
                "_wall_time_ns",
                return_value=2_000_000_000_000,
            ),
        ):
            wall_time, monotonic_time, corrected_time, offset_ms = (
                self.service.capture_timestamp()
            )

        self.assertEqual(wall_time, 2000.0)
        self.assertEqual(monotonic_time, 10.1)
        self.assertAlmostEqual(corrected_time, 1000.11)
        self.assertEqual(offset_ms, 10.0)

    def test_bad_candidate_does_not_replace_last_known_good_anchor(self):
        self.service.configure(
            {
                "halt_breach_threshold": 2,
                "freeze_breach_threshold": 2,
                "recovery_success_threshold": 2,
            }
        )
        self._sync_samples(
            [{"offset_ms": -800.0, "rtt_ms": 1.0}],
            wall_ns=1_000_000_000_000,
            mono_ns=10_000_000_000,
        )
        original_anchor = (
            self.service._anchor_epoch_ns,
            self.service._anchor_mono_ns,
            self.service._anchor_wall_ns,
        )

        self.assertFalse(
            self._sync_samples(
                [{"offset_ms": -680.0, "rtt_ms": 1.0}],
                wall_ns=1_010_000_000_000,
                mono_ns=20_000_000_000,
            )
        )

        self.assertFalse(self.service._ready)
        self.assertEqual(self.service._health_state, "degraded")
        self.assertEqual(self.service.offset, -800.0)
        self.assertEqual(
            (
                self.service._anchor_epoch_ns,
                self.service._anchor_mono_ns,
                self.service._anchor_wall_ns,
            ),
            original_anchor,
        )
        self.assertEqual(self.service.recovery_success_count, 0)

    def test_failure_resets_recovery_quorum(self):
        self.service.configure({"recovery_success_threshold": 2})
        self._sync_samples([{"offset_ms": 10.0, "rtt_ms": 1.0}])
        self._sync_samples([{"offset_ms": 11.0, "rtt_ms": 1.0}])
        self.assertEqual(self.service.recovery_success_count, 2)

        with patch.object(
            self.service,
            "_request_sample",
            side_effect=RuntimeError("network down"),
        ):
            self.service._sync()

        self.assertEqual(self.service.recovery_success_count, 0)
        self._sync_samples([{"offset_ms": 12.0, "rtt_ms": 1.0}])
        self.assertFalse(self.service._ready)
        self._sync_samples([{"offset_ms": 13.0, "rtt_ms": 1.0}])
        self.assertTrue(self.service._ready)

    def test_stopped_generation_cannot_commit_blocked_sample(self):
        request_started = threading.Event()
        release_request = threading.Event()
        result = []
        with self.service._state_lock:
            self.service._generation += 1
            generation = self.service._generation
            stop_event = threading.Event()
            sync_lock = threading.Lock()
            self.service._stop_event = stop_event
            self.service._sync_lock = sync_lock
            self.service.active = True
            self.service._health_state = "starting"

        def blocked_sample():
            request_started.set()
            release_request.wait(2.0)
            return {"offset_ms": 25.0, "rtt_ms": 1.0}

        with patch.object(self.service, "_request_sample", blocked_sample):
            sync_thread = threading.Thread(
                target=lambda: result.append(
                    self.service._sync(
                        generation=generation,
                        stop_event=stop_event,
                        sync_lock=sync_lock,
                    )
                ),
                daemon=True,
            )
            sync_thread.start()
            self.assertTrue(request_started.wait(1.0))
            stop_thread = threading.Thread(target=self.service.stop, daemon=True)
            stop_thread.start()
            self.assertTrue(stop_event.wait(1.0))
            release_request.set()
            stop_thread.join(2.0)
            sync_thread.join(2.0)

        self.assertEqual(result, [False])
        self.assertFalse(self.service._ready)
        self.assertEqual(self.service._health_state, "stopped")
        self.assertEqual(self.service.offset, 0.0)

    def test_direct_offset_assignment_reanchors_for_sidecar_compatibility(self):
        self._sync_samples([{"offset_ms": 10.0, "rtt_ms": 1.0}])
        self.service._last_now_ns = 0
        with (
            patch.object(self.service, "monotonic_ns", return_value=20_000_000_000),
            patch.object(self.service, "_wall_time_ns", return_value=2_000_000_000_000),
        ):
            self.service.offset = 500.0
        with patch.object(
            self.service, "monotonic_ns", return_value=20_100_000_000
        ):
            corrected_ns = self.service.now_ns()

        self.assertEqual(corrected_ns, 2_000_600_000_000)

    def test_direct_offset_assignment_anchors_an_unstarted_sidecar_clock(self):
        self.service._anchor_mono_ns = 0
        self.service._last_now_ns = 0
        with (
            patch.object(self.service, "monotonic_ns", return_value=5_000_000_000),
            patch.object(
                self.service,
                "_wall_time_ns",
                return_value=1_000_000_000_000,
            ),
        ):
            self.service.offset = 20.0
        with patch.object(
            self.service,
            "monotonic_ns",
            return_value=5_100_000_000,
        ):
            self.assertEqual(self.service.now_ns(), 1_000_120_000_000)

    def test_clock_uncertainty_is_fail_closed(self):
        self.service.max_uncertainty_ms = 5.0

        self.assertFalse(
            self._sync_samples([{"offset_ms": 10.0, "rtt_ms": 12.0}])
        )

        snapshot = self.service.health_snapshot()
        self.assertFalse(snapshot["ready"])
        self.assertEqual(snapshot["state"], "freeze")
        self.assertAlmostEqual(snapshot["estimated_uncertainty_ms"], 6.0)

    def test_stale_calibration_emits_halt(self):
        self._sync_samples(
            [{"offset_ms": 10.0, "rtt_ms": 1.0}],
            wall_ns=1_000_000_000_000,
            mono_ns=10_000_000_000,
        )
        self.service.max_sync_age_sec = 1.0
        self.service.active = True
        with (
            patch.object(self.service, "monotonic_ns", return_value=12_000_000_000),
            patch.object(self.service, "_wall_time_ns", return_value=1_002_000_000_000),
        ):
            self.assertFalse(self.service.ready)

        self.assertEqual(self.events[-1][0], "halt")
        self.assertIn("stale", self.events[-1][1])

    def test_wall_clock_step_emits_halt_without_moving_corrected_epoch(self):
        self._sync_samples(
            [{"offset_ms": 10.0, "rtt_ms": 1.0}],
            wall_ns=1_000_000_000_000,
            mono_ns=10_000_000_000,
        )
        self.service.active = True
        with (
            patch.object(self.service, "monotonic_ns", return_value=11_000_000_000),
            patch.object(self.service, "_wall_time_ns", return_value=1_001_250_000_000),
        ):
            self.assertFalse(self.service.ready)

        self.assertEqual(self.events[-1][0], "halt")
        self.assertIn("wall clock step", self.events[-1][1])
        self.assertAlmostEqual(
            self.events[-1][2]["wall_clock_step_ms"], 250.0
        )

    def test_stale_fault_cannot_override_newer_calibration(self):
        self._sync_samples(
            [{"offset_ms": 10.0, "rtt_ms": 1.0}],
            wall_ns=1_000_000_000_000,
            mono_ns=10_000_000_000,
        )
        self.service.active = True
        expected_generation = self.service._generation
        stale_sync_mono_ns = self.service._last_sync_mono_ns
        self._sync_samples(
            [{"offset_ms": 11.0, "rtt_ms": 1.0}],
            wall_ns=1_010_000_000_000,
            mono_ns=20_000_000_000,
        )

        applied = self.service._set_runtime_fault(
            "stale result from prior anchor",
            expected_generation=expected_generation,
            expected_sync_mono_ns=stale_sync_mono_ns,
        )

        self.assertFalse(applied)
        self.assertTrue(self.service._ready)
        self.assertEqual(self.service._health_state, "healthy")

    def test_health_snapshot_after_stop_does_not_emit_fault(self):
        self._sync_samples([{"offset_ms": 10.0, "rtt_ms": 1.0}])
        self.service.stop()
        event_count = len(self.events)

        snapshot = self.service.health_snapshot()

        self.assertFalse(snapshot["ready"])
        self.assertEqual(snapshot["state"], "stopped")
        self.assertEqual(len(self.events), event_count)

    def test_synchronize_now_requires_active_generation(self):
        self.service.active = False
        with patch.object(self.service, "_sync") as sync:
            self.assertFalse(self.service.synchronize_now())
        sync.assert_not_called()

    def test_old_generation_notification_is_dropped(self):
        self.service.active = True
        old_generation = self.service._generation
        old_stop_event = self.service._stop_event
        self.service._generation += 1

        delivered = self.service._notify(
            "halt",
            "stale generation",
            expected_generation=old_generation,
            expected_stop_event=old_stop_event,
        )

        self.assertFalse(delivered)
        self.assertEqual(self.events, [])

    def test_stop_interrupts_long_auto_sync_wait(self):
        self.service.sync_interval_sec = 60.0
        self.service.active = True
        self.service._stop_event.clear()
        thread = threading.Thread(target=self.service._auto_sync_loop, daemon=True)
        self.service._thread = thread
        thread.start()
        time.sleep(0.02)

        started = time.perf_counter()
        self.service.stop()

        self.assertLess(time.perf_counter() - started, 0.2)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
