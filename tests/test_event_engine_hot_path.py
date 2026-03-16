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


if __name__ == "__main__":
    unittest.main()
