import threading
import time
import unittest

from event.engine import EventEngine
from event.type import Event


class EventEngineHotPathTests(unittest.TestCase):
    def test_cold_lane_does_not_block_hot_lane(self):
        engine = EventEngine()
        hot_seen = []
        cold_started = threading.Event()
        release_cold = threading.Event()
        second_hot_seen = threading.Event()

        def hot_handler(event):
            hot_seen.append(event.data)
            if event.data == "second":
                second_hot_seen.set()

        def cold_handler(_event):
            cold_started.set()
            release_cold.wait(timeout=1.0)

        engine.register_hot("eTest", hot_handler)
        engine.register_cold("eTest", cold_handler)
        engine.start()
        try:
            engine.put(Event("eTest", "first"))
            self.assertTrue(cold_started.wait(timeout=0.5))

            engine.put(Event("eTest", "second"))
            self.assertTrue(second_hot_seen.wait(timeout=0.5))
            self.assertEqual(hot_seen[:2], ["first", "second"])
        finally:
            release_cold.set()
            time.sleep(0.05)
            engine.stop()

    def test_market_lane_does_not_block_execution_lane(self):
        engine = EventEngine()
        market_started = threading.Event()
        release_market = threading.Event()
        execution_seen = threading.Event()

        def market_handler(_event):
            market_started.set()
            release_market.wait(timeout=1.0)

        def execution_handler(_event):
            execution_seen.set()

        engine.register_market("eSplit", market_handler)
        engine.register_execution("eSplit", execution_handler)
        engine.start()
        try:
            engine.put(Event("eSplit", "first"))
            self.assertTrue(market_started.wait(timeout=0.5))
            self.assertTrue(execution_seen.wait(timeout=0.5))
        finally:
            release_market.set()
            time.sleep(0.05)
            engine.stop()

    def test_metrics_capture_backlog_and_slow_handler(self):
        engine = EventEngine(
            {
                "handler_slow_ms": {
                    "market": 1,
                    "execution": 1,
                    "cold": 1,
                }
            }
        )
        release_handler = threading.Event()

        def market_handler(_event):
            time.sleep(0.02)
            release_handler.set()

        engine.register_market("eMetrics", market_handler)
        engine.start()
        try:
            engine.put(Event("eMetrics", "first"))
            self.assertTrue(release_handler.wait(timeout=0.5))
            time.sleep(0.05)
            snapshot = engine.get_metrics_snapshot()
            market_stats = snapshot["lanes"]["market"]
            self.assertGreaterEqual(market_stats["processed"], 1)
            self.assertGreater(market_stats["max_duration_ms"], 1.0)

            handler_stats = engine.get_handler_metrics_snapshot(limit=1)
            self.assertEqual(handler_stats[0]["lane"], "market")
            self.assertEqual(handler_stats[0]["event_type"], "eMetrics")
            self.assertGreater(handler_stats[0]["max_ms"], 1.0)
        finally:
            engine.stop()

    def test_wait_until_idle_includes_inflight_cold_handoff(self):
        engine = EventEngine()
        cold_started = threading.Event()
        release_cold = threading.Event()

        def execution_handler(_event):
            return None

        def cold_handler(_event):
            cold_started.set()
            release_cold.wait(timeout=1.0)

        engine.register_execution("eDrain", execution_handler)
        engine.register_cold("eDrain", cold_handler)
        engine.start()
        try:
            engine.put(Event("eDrain", "cancel-ack"))
            self.assertTrue(cold_started.wait(timeout=0.5))
            self.assertEqual(engine.get_queue_snapshot()["cold_depth"], 0)
            self.assertFalse(engine.wait_until_idle(timeout_sec=0.01))

            release_cold.set()
            self.assertTrue(engine.wait_until_idle(timeout_sec=0.5))
            self.assertEqual(engine.get_queue_snapshot()["pending_work"], 0)
        finally:
            release_cold.set()
            engine.stop()

    def test_wait_until_idle_includes_work_enqueued_by_handler(self):
        engine = EventEngine()
        seen = []

        def execution_handler(event):
            seen.append(event.data)
            if event.data == "first":
                engine.put(Event("eCascade", "second"))

        engine.register_execution("eCascade", execution_handler)
        engine.start()
        try:
            engine.put(Event("eCascade", "first"))
            self.assertTrue(engine.wait_until_idle(timeout_sec=0.5))
            self.assertEqual(seen, ["first", "second"])
        finally:
            engine.stop()

    def test_handler_exception_is_reported_and_observable(self):
        failures = []
        engine = EventEngine()
        engine.set_failure_handler(failures.append)

        def failed_handler(_event):
            raise RuntimeError("forced handler failure")

        engine.register_execution("eFailure", failed_handler)

        self.assertTrue(engine.put(Event("eFailure", "payload")))
        engine.process_existing_events()

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["kind"], "handler_exception")
        self.assertEqual(failures[0]["lane"], "execution")
        self.assertEqual(failures[0]["event_type"], "eFailure")
        snapshot = engine.get_metrics_snapshot()
        self.assertEqual(
            snapshot["lanes"]["execution"]["handler_error_count"],
            1,
        )
        handler_stats = engine.get_handler_metrics_snapshot()
        self.assertEqual(handler_stats[0]["error_count"], 1)
        self.assertEqual(snapshot["queues"]["pending_work"], 0)

    def test_all_lane_overflows_are_nonblocking_and_reported(self):
        registrations = {
            "market": EventEngine.register_market,
            "execution": EventEngine.register_execution,
            "cold": EventEngine.register_cold,
        }
        for lane, register in registrations.items():
            with self.subTest(lane=lane):
                failures = []
                engine = EventEngine({"queue_capacity": 1})
                engine.set_failure_handler(failures.append)
                register(engine, "eFull", lambda _event: None)

                self.assertTrue(engine.put(Event("eFull", "first")))
                started_at = time.perf_counter()
                self.assertFalse(engine.put(Event("eFull", "overflow")))
                self.assertLess(time.perf_counter() - started_at, 0.1)

                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0]["kind"], "queue_overflow")
                self.assertEqual(failures[0]["lane"], lane)
                snapshot = engine.get_metrics_snapshot()
                self.assertEqual(
                    snapshot["lanes"][lane]["queue_overflow_count"],
                    1,
                )
                self.assertEqual(snapshot["queues"]["pending_work"], 1)

                engine.process_existing_events()
                self.assertEqual(
                    engine.get_queue_snapshot()["pending_work"],
                    0,
                )

    def test_cold_handoff_overflow_does_not_leak_pending_work(self):
        failures = []
        engine = EventEngine(
            {
                "queue_capacity": {
                    "market": 1,
                    "execution": 1,
                    "cold": 1,
                }
            }
        )
        engine.set_failure_handler(failures.append)
        engine.register_cold("eColdOnly", lambda _event: None)
        engine.register_execution("eMixed", lambda _event: None)
        engine.register_cold("eMixed", lambda _event: None)

        self.assertTrue(engine.put(Event("eColdOnly", "occupy-cold")))
        self.assertTrue(engine.put(Event("eMixed", "handoff")))
        engine.process_existing_events()

        self.assertEqual(engine.get_queue_snapshot()["pending_work"], 0)
        self.assertEqual(engine._pending_cold, {})
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["kind"], "queue_overflow")
        self.assertEqual(failures[0]["lane"], "cold")

    def test_stop_seals_admission_and_drains_queued_hot_and_cold_work(self):
        engine = EventEngine({"shutdown_drain_timeout_sec": 1.0})
        first_started = threading.Event()
        release_first = threading.Event()
        hot_seen = []
        cold_seen = []
        stop_results = []

        def execution_handler(event):
            hot_seen.append(event.data)
            if event.data == "first":
                first_started.set()
                release_first.wait(timeout=1.0)

        def cold_handler(event):
            cold_seen.append(event.data)

        engine.register_execution("eStopDrain", execution_handler)
        engine.register_cold("eStopDrain", cold_handler)
        engine.start()
        engine.put(Event("eStopDrain", "first"))
        self.assertTrue(first_started.wait(timeout=0.5))
        engine.put(Event("eStopDrain", "second"))

        stopper = threading.Thread(
            target=lambda: stop_results.append(engine.stop()),
        )
        stopper.start()
        deadline = time.monotonic() + 0.5
        while not engine.get_metrics_snapshot()["stopping"]:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.005)

        self.assertFalse(engine.put(Event("eStopDrain", "late")))
        release_first.set()
        stopper.join(timeout=1.0)

        self.assertFalse(stopper.is_alive())
        self.assertEqual(stop_results, [True])
        self.assertEqual(hot_seen, ["first", "second"])
        self.assertEqual(cold_seen, ["first", "second"])
        self.assertEqual(engine.get_queue_snapshot()["pending_work"], 0)
        snapshot = engine.get_metrics_snapshot()
        self.assertFalse(snapshot["accepting"])
        self.assertEqual(snapshot["rejected_put_count"], 1)

    def test_stop_timeout_keeps_workers_alive_for_drain_retry(self):
        engine = EventEngine()
        started = threading.Event()
        release = threading.Event()

        def handler(_event):
            started.set()
            release.wait(timeout=1.0)

        engine.register_execution("eStopRetry", handler)
        engine.start()
        engine.put(Event("eStopRetry", "inflight"))
        self.assertTrue(started.wait(timeout=0.5))

        self.assertFalse(engine.stop(timeout_sec=0.01))
        snapshot = engine.get_metrics_snapshot()
        self.assertTrue(snapshot["active"])
        self.assertTrue(snapshot["stopping"])
        self.assertFalse(snapshot["accepting"])

        release.set()
        self.assertTrue(engine.wait_until_idle(timeout_sec=0.5))
        self.assertTrue(engine.stop(timeout_sec=0.5))
        self.assertFalse(engine.get_metrics_snapshot()["active"])

    def test_stop_before_start_synchronously_drains_existing_events(self):
        engine = EventEngine()
        seen = []
        engine.register_execution("ePrestartStop", lambda event: seen.append(event.data))

        self.assertTrue(engine.put(Event("ePrestartStop", "queued")))
        self.assertTrue(engine.stop(timeout_sec=0.5))

        self.assertEqual(seen, ["queued"])
        self.assertEqual(engine.get_queue_snapshot()["pending_work"], 0)
        self.assertFalse(engine.put(Event("ePrestartStop", "late")))


if __name__ == "__main__":
    unittest.main()
