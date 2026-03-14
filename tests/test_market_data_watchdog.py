import unittest

from infrastructure.watchdog import emit_market_data_stale_if_needed


class DummyEngine:
    def __init__(self):
        self.events = []

    def put(self, event):
        self.events.append(event)


class MarketDataWatchdogTests(unittest.TestCase):
    def test_emit_market_data_stale_if_needed_emits_once(self):
        engine = DummyEngine()

        triggered = emit_market_data_stale_if_needed(
            engine,
            last_tick_time=10.0,
            triggered=False,
            threshold_sec=60.0,
            now=71.0,
        )
        self.assertTrue(triggered)
        self.assertEqual(len(engine.events), 1)
        self.assertEqual(engine.events[0].type, "eSystemHealth")
        self.assertIn("MARKET_DATA_STALE", engine.events[0].data)

        triggered = emit_market_data_stale_if_needed(
            engine,
            last_tick_time=10.0,
            triggered=triggered,
            threshold_sec=60.0,
            now=72.0,
        )
        self.assertTrue(triggered)
        self.assertEqual(len(engine.events), 1)


if __name__ == "__main__":
    unittest.main()