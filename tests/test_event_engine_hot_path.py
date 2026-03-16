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


if __name__ == "__main__":
    unittest.main()
