import unittest

from event.type import OMSCapabilityMode
from infrastructure.watchdog import (
    emit_event_engine_backlog_if_needed,
    emit_market_data_stale_if_needed,
)


class DummyEngine:
    def __init__(self):
        self.events = []
        self.metrics = {
            "lanes": {
                "market": {
                    "depth": 0,
                    "oldest_queued_ms": 0.0,
                    "inflight_ms": 0.0,
                    "handler_inflight_ms": 0.0,
                    "last_event_type": "",
                    "inflight_event_type": "",
                    "inflight_handler_name": "",
                },
                "execution": {
                    "depth": 0,
                    "oldest_queued_ms": 0.0,
                    "inflight_ms": 0.0,
                    "handler_inflight_ms": 0.0,
                    "last_event_type": "",
                    "inflight_event_type": "",
                    "inflight_handler_name": "",
                },
            }
        }

    def put(self, event):
        self.events.append(event)

    def get_metrics_snapshot(self):
        return self.metrics


class DummyOMS:
    def __init__(self):
        self.modes = []
        self.clears = []
        self.frozen = []
        self.unfrozen = []
        self.venue_reason = ""

    def set_trading_mode(self, mode, reason):
        self.modes.append((mode, reason))

    def clear_trading_mode(self, reason="", prefixes=()):
        self.clears.append((reason, tuple(prefixes or ())))
        return True

    def freeze_venue(self, venue, reason, cancel_active_orders=True):
        self.venue_reason = reason
        self.frozen.append((venue, reason, cancel_active_orders))

    def clear_venue_freeze(self, venue, reason=""):
        self.venue_reason = ""
        self.unfrozen.append((venue, reason))
        return True

    def get_venue_freeze_reason(self, _venue):
        return self.venue_reason


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

    def test_event_engine_backlog_degrades_then_recovers(self):
        engine = DummyEngine()
        oms = DummyOMS()

        engine.metrics["lanes"]["market"].update(
            {
                "depth": 0,
                "oldest_queued_ms": 300.0,
                "last_event_type": "eOrderBook",
            }
        )
        state = emit_event_engine_backlog_if_needed(
            engine,
            oms,
            "BINANCE",
            {},
            {"degraded_backlog_ms": {"market": 250}, "recovery_checks": 2},
        )
        self.assertEqual(state["severity"], 1)
        self.assertEqual(oms.modes[-1][0], OMSCapabilityMode.DEGRADED)

        engine.metrics["lanes"]["market"].update({"oldest_queued_ms": 0.0})
        state = emit_event_engine_backlog_if_needed(
            engine,
            oms,
            "BINANCE",
            state,
            {"degraded_backlog_ms": {"market": 250}, "recovery_checks": 2},
        )
        self.assertEqual(state["healthy_checks"], 1)
        state = emit_event_engine_backlog_if_needed(
            engine,
            oms,
            "BINANCE",
            state,
            {"degraded_backlog_ms": {"market": 250}, "recovery_checks": 2},
        )
        self.assertEqual(state["severity"], 0)
        self.assertTrue(oms.clears)

    def test_event_engine_backlog_freezes_venue(self):
        engine = DummyEngine()
        oms = DummyOMS()
        engine.metrics["lanes"]["execution"].update(
            {
                "depth": 120,
                "last_event_type": "eExchangeOrderUpdate",
            }
        )

        state = emit_event_engine_backlog_if_needed(
            engine,
            oms,
            "BINANCE",
            {},
            {"freeze_queue_depth": {"execution": 100}},
        )
        self.assertEqual(state["severity"], 3)
        self.assertEqual(oms.frozen[-1][0], "BINANCE")
        self.assertTrue(oms.frozen[-1][1].startswith("event_engine_backlog:execution"))


if __name__ == "__main__":
    unittest.main()
