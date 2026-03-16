import time
import unittest
from datetime import datetime

from event.type import OrderBook
from strategy.ml_sniper.alpha_process import MLSniperAlphaProcess


class MLSniperAlphaProcessTests(unittest.TestCase):
    def test_alpha_process_returns_snapshot(self):
        alpha_process = MLSniperAlphaProcess(
            {
                "enabled": True,
                "queue_size": 32,
                "processes": 2,
                "tick_interval_sec": 0.0,
                "cycle_interval_sec": 1.0,
                "labeling": {
                    "maker_fee_bps": 0.0,
                    "taker_fee_bps": 0.0,
                },
            }
        )
        if not alpha_process.start():
            self.skipTest("multiprocessing is not available in this environment")
        try:
            orderbook = OrderBook(
                symbol="BTCUSDT",
                exchange="BINANCE",
                datetime=datetime.utcnow(),
                bids={99.9: 1.0, 99.8: 2.0},
                asks={100.1: 1.0, 100.2: 2.0},
            )
            alpha_process.submit_orderbook(orderbook)

            results = []
            deadline = time.time() + 3.0
            while time.time() < deadline and not results:
                results = alpha_process.poll()
                time.sleep(0.05)

            self.assertTrue(results)
            snapshot = results[0]
            self.assertEqual(snapshot["kind"], "alpha_snapshot")
            self.assertEqual(snapshot["symbol"], "BTCUSDT")
            self.assertIn("preds", snapshot)
            self.assertEqual(alpha_process.get_metrics_snapshot()["worker_count"], 2)
        finally:
            alpha_process.stop()


if __name__ == "__main__":
    unittest.main()
