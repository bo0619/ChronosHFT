import os
import tempfile
import unittest

from event.type import OrderIntent, OrderStatus, Side
from oms.journal import OMSJournal
from oms.order import Order


class OrderStateMachineTests(unittest.TestCase):
    def test_order_follows_institutional_lifecycle(self):
        intent = OrderIntent("test", "BTCUSDT", Side.BUY, 100.0, 2.0)
        order = Order("oid-1", intent)

        order.mark_submitting()
        order.mark_pending_ack("ex-1")
        order.mark_new("ex-1", update_time=1.0, seq=1)
        order.add_fill(1.0, 100.0, update_time=2.0, seq=2)
        order.add_fill(1.0, 101.0, update_time=3.0, seq=3, exchange_status="FILLED")
        order.mark_filled(update_time=3.0, seq=3)

        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertEqual(order.exchange_oid, "ex-1")
        self.assertAlmostEqual(order.filled_volume, 2.0)
        self.assertGreater(order.avg_price, 100.0)

    def test_invalid_transition_raises(self):
        intent = OrderIntent("test", "BTCUSDT", Side.BUY, 100.0, 1.0)
        order = Order("oid-2", intent)
        order.mark_submitting()
        order.mark_pending_ack("ex-2")
        order.mark_new("ex-2", update_time=1.0, seq=1)
        order.mark_cancelled(update_time=2.0, seq=2)

        with self.assertRaises(ValueError):
            order.mark_new("ex-2", update_time=3.0, seq=3)


class JournalTests(unittest.TestCase):
    def test_journal_appends_and_loads_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "oms_journal.jsonl")
            journal = OMSJournal(
                {
                    "oms": {
                        "journal_enabled": True,
                        "replay_journal_on_startup": True,
                        "journal_path": path,
                    }
                }
            )
            journal.append("decision", {"hello": "world"})
            journal.append("lifecycle", {"state": "LIVE"})

            records = journal.load()
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["kind"], "decision")
            self.assertEqual(records[1]["payload"]["state"], "LIVE")


if __name__ == "__main__":
    unittest.main()
