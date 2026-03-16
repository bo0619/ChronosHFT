import time
from collections import deque
from unittest.mock import patch
import unittest
from datetime import datetime

from event.type import OrderBook
from strategy.ml_sniper.alpha_process import MLSniperAlphaProcess


class FakeProcess:
    def __init__(self, alive=False, pid=1234):
        self._alive = alive
        self.pid = pid

    def is_alive(self):
        return self._alive


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

    def test_restart_burst_quarantines_worker_and_restarts_with_standby_profile(self):
        alpha_process = MLSniperAlphaProcess(
            {
                "enabled": True,
                "queue_size": 32,
                "processes": 1,
                "auto_restart": True,
                "restart_cooldown_sec": 0.5,
                "max_restart_burst": 2,
                "restart_window_sec": 30.0,
                "quarantine_sec": 5.0,
                "tick_interval_sec": 0.1,
                "cycle_interval_sec": 1.0,
                "standby_profile": {
                    "tick_interval_sec": 0.5,
                    "cycle_interval_sec": 2.0,
                },
            }
        )
        worker = alpha_process._new_worker_state(0)
        worker["process"] = FakeProcess(alive=False)
        now = time.time()
        worker["restart_history"] = deque([now - 2.0, now - 1.0])
        alpha_process._workers = [worker]
        alpha_process._worker_symbols[0].add("BTCUSDT")

        self.assertFalse(alpha_process._restart_worker(worker))
        self.assertIn("BTCUSDT", alpha_process.get_quarantined_symbols())
        self.assertIn("BTCUSDT", alpha_process.drain_quarantine_events())
        self.assertEqual(alpha_process.get_metrics_snapshot()["quarantined_symbols"], 1)

        worker["quarantined_until"] = time.time() - 1.0
        worker["last_restart_attempt_ts"] = 0.0
        started_profiles = []

        def fake_start(target_worker, worker_config, profile_name="primary"):
            target_worker["in_queue"] = object()
            target_worker["out_queue"] = object()
            target_worker["process"] = FakeProcess(alive=True, pid=4321)
            target_worker["generation"] = int(target_worker.get("generation", 0)) + 1
            target_worker["last_restart_attempt_ts"] = time.time()
            target_worker["profile_name"] = profile_name
            started_profiles.append((profile_name, worker_config["tick_interval_sec"], worker_config["cycle_interval_sec"]))

        with patch.object(alpha_process, "_start_worker", side_effect=fake_start):
            self.assertTrue(alpha_process._restart_worker(worker))

        self.assertEqual(started_profiles[-1], ("standby", 0.5, 2.0))
        self.assertIn("BTCUSDT", alpha_process.get_recovering_symbols())
        self.assertNotIn("BTCUSDT", alpha_process.get_quarantined_symbols())
        metrics = alpha_process.get_metrics_snapshot()
        self.assertEqual(metrics["standby_workers"], 1)


if __name__ == "__main__":
    unittest.main()
