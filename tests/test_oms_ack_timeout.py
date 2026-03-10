import unittest

from event.type import Event, OrderRequest, OrderStatus, OrderSubmitted
from oms.order_manager import OrderManager


class OrderManagerAckTimeoutTests(unittest.TestCase):
    def test_ack_timeout_only_escalates_once_within_cooldown(self):
        callbacks = []
        monitor = OrderManager(
            engine=None,
            gateway=None,
            dirty_callback=lambda reason, suspicious_oid=None: callbacks.append((reason, suspicious_oid)),
            monitor_config={
                "ack_timeout_sec": 1.0,
                "ack_timeout_recheck_sec": 60.0,
                "monitor_check_interval_sec": 0.01,
            },
            start_thread=False,
        )
        try:
            submitted = OrderSubmitted(
                req=OrderRequest(symbol="BTCUSDT", price=100.0, volume=1.0, side="BUY"),
                order_id="oid-1",
                timestamp=10.0,
            )
            monitor.on_order_submitted(Event("eOrderSubmitted", submitted))

            monitor._check_once(now=12.0)
            monitor._check_once(now=13.0)
            monitor._check_once(now=14.0)

            self.assertEqual(callbacks, [("Order ACK Timeout", "oid-1")])
        finally:
            monitor.stop()

    def test_ack_timeout_rearms_after_ack_progress(self):
        callbacks = []
        monitor = OrderManager(
            engine=None,
            gateway=None,
            dirty_callback=lambda reason, suspicious_oid=None: callbacks.append((reason, suspicious_oid)),
            monitor_config={
                "ack_timeout_sec": 1.0,
                "ack_timeout_recheck_sec": 60.0,
            },
            start_thread=False,
        )
        try:
            submitted = OrderSubmitted(
                req=OrderRequest(symbol="BTCUSDT", price=100.0, volume=1.0, side="BUY"),
                order_id="oid-2",
                timestamp=10.0,
            )
            monitor.on_order_submitted(Event("eOrderSubmitted", submitted))
            monitor._check_once(now=12.0)
            monitor.on_order_update("oid-2", OrderStatus.NEW)

            self.assertEqual(len(callbacks), 1)
            self.assertGreater(monitor.monitored_orders["oid-2"]["last_ack_time"], 0.0)
            self.assertFalse(monitor.monitored_orders["oid-2"]["ack_timeout_reported"])
        finally:
            monitor.stop()


if __name__ == "__main__":
    unittest.main()