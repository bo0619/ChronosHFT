import queue
import signal
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import data.recorder as recorder_module
from data.recorder import DataRecorder
from event.type import Event, OrderBook, EVENT_ORDERBOOK


class DummyEngine:
    def __init__(self):
        self.handlers = {}

    def register_cold(self, event_type, handler):
        self.handlers.setdefault(event_type, []).append(handler)


def make_book():
    now = time.time()
    return OrderBook(
        symbol="BTCUSDT",
        exchange="BINANCE",
        datetime=datetime.fromtimestamp(now),
        bids={100.0: 2.0},
        asks={101.0: 3.0},
        top_bids=((100.0, 2.0),),
        top_asks=((101.0, 3.0),),
        best_bid_price=100.0,
        best_bid_volume=2.0,
        best_ask_price=101.0,
        best_ask_volume=3.0,
        exchange_timestamp=now,
        received_timestamp=now,
        depth_levels=1,
    )


class DataRecorderIsolationTests(unittest.TestCase):
    def test_writer_process_ignores_console_interrupts(self):
        command_queue = queue.Queue()
        status_queue = queue.Queue()
        command_queue.put((recorder_module._STOP,))

        with patch.object(
            recorder_module.signal,
            "signal",
        ) as install_handler:
            recorder_module._recorder_writer_main(
                command_queue,
                status_queue,
                save_path=".",
                symbols=(),
                flush_threshold=1,
            )

        install_handler.assert_called_once_with(
            signal.SIGINT,
            signal.SIG_IGN,
        )
        self.assertEqual(status_queue.get_nowait()["type"], "stopped")

    def test_hdf_flush_runs_outside_event_handler_process(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            engine = DummyEngine()
            recorder = DataRecorder(
                engine,
                ["BTCUSDT"],
                save_path=temporary_directory,
                flush_threshold=500,
                queue_capacity=5_000,
                close_timeout_sec=30.0,
            )
            handler = engine.handlers[EVENT_ORDERBOOK][0]
            event = Event(EVENT_ORDERBOOK, make_book())
            started = time.perf_counter()
            for _ in range(2_000):
                handler(event)
            handler_elapsed = time.perf_counter() - started

            metrics = recorder.get_metrics_snapshot()
            self.assertTrue(metrics["healthy"])
            self.assertEqual(metrics["enqueued_records"], 2_000)
            self.assertEqual(metrics["dropped_records"], 0)
            self.assertLess(handler_elapsed, 2.0)
            self.assertTrue(recorder.close())
            closed_metrics = recorder.get_metrics_snapshot()
            self.assertTrue(closed_metrics["healthy"])
            self.assertTrue(closed_metrics["closed"])
            self.assertEqual(closed_metrics["writer_exitcode"], 0)

            files = list(
                Path(temporary_directory).glob(
                    "BTCUSDT_depth_*.h5"
                )
            )
            self.assertEqual(len(files), 1)
            frame = pd.read_hdf(files[0], key="depth")
            self.assertEqual(len(frame), 2_000)
            self.assertEqual(set(frame["symbol"]), {"BTCUSDT"})


if __name__ == "__main__":
    unittest.main()
