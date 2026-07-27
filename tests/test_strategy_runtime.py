import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from strategy.runtime import StrategyRuntime


class DummyStrategy:
    def __init__(self):
        self.name = "DummyStrategy"
        self.orderbooks = []
        self.market_trades = []
        self.orders = []
        self.accounts = []
        self.health = []

    def on_orderbook(self, orderbook):
        self.orderbooks.append(orderbook.sequence)

    def on_market_trade(self, trade):
        self.market_trades.append(trade.trade_id)

    def on_order(self, snapshot):
        self.orders.append(snapshot.order_id)

    def on_account_update(self, account):
        self.accounts.append(account.balance)

    def on_system_health(self, message):
        self.health.append(message)


class StrategyRuntimeTests(unittest.TestCase):
    def test_orderbook_updates_coalesce_by_symbol(self):
        strategy = DummyStrategy()
        runtime = StrategyRuntime(strategy, start_thread=False)

        runtime.on_orderbook(SimpleNamespace(symbol="BTCUSDT", sequence=1))
        runtime.on_orderbook(SimpleNamespace(symbol="BTCUSDT", sequence=2))

        processed = runtime.process_pending()
        metrics = runtime.get_metrics_snapshot()

        self.assertEqual(processed, 1)
        self.assertEqual(strategy.orderbooks, [2])
        self.assertEqual(metrics["coalesced_market_events"], 1)

    def test_control_events_are_not_coalesced(self):
        strategy = DummyStrategy()
        runtime = StrategyRuntime(strategy, start_thread=False)

        runtime.on_order(SimpleNamespace(order_id="oid-1"))
        runtime.on_account_update(SimpleNamespace(balance=1000.0))
        runtime.on_system_health("FROZEN:test")
        processed = runtime.process_pending()

        self.assertEqual(processed, 3)
        self.assertEqual(strategy.orders, ["oid-1"])
        self.assertEqual(strategy.accounts, [1000.0])
        self.assertEqual(strategy.health, ["FROZEN:test"])

    def test_start_rebases_prestart_state_without_reporting_false_backlog(self):
        strategy = DummyStrategy()
        runtime = StrategyRuntime(strategy, start_thread=False)
        runtime.on_order(SimpleNamespace(order_id="oid-1"))
        runtime.on_orderbook(SimpleNamespace(symbol="BTCUSDT", sequence=1))
        worker = MagicMock()
        worker.is_alive.return_value = False

        with patch("strategy.runtime.time.perf_counter", return_value=50.0), patch(
            "strategy.runtime.Thread",
            return_value=worker,
        ):
            runtime.start()

        self.assertEqual(runtime._control_queue[0][1], 50.0)
        self.assertEqual(
            runtime._pending_market[("orderbook", "BTCUSDT")][0],
            50.0,
        )
        self.assertEqual(runtime.get_metrics_snapshot()["max_wait_ms"], 0.0)
        worker.start.assert_called_once_with()

    def test_async_runtime_does_not_execute_inline(self):
        strategy = DummyStrategy()
        runtime = StrategyRuntime(strategy, start_thread=True)
        try:
            runtime.on_orderbook(SimpleNamespace(symbol="ETHUSDT", sequence=7))
            self.assertEqual(strategy.orderbooks, [])
            deadline = time.time() + 0.5
            while time.time() < deadline and not strategy.orderbooks:
                time.sleep(0.01)
            self.assertEqual(strategy.orderbooks, [7])
        finally:
            runtime.stop()

    def test_handler_exception_invokes_fail_closed_callback(self):
        class FailedStrategy(DummyStrategy):
            def on_order(self, _snapshot):
                raise ValueError("forced strategy failure")

        failures = []
        runtime = StrategyRuntime(
            FailedStrategy(),
            start_thread=False,
            failure_callback=failures.append,
        )
        runtime.on_order(SimpleNamespace(order_id="oid-failed"))

        self.assertEqual(runtime.process_pending(), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["phase"], "handler")
        self.assertEqual(failures[0]["kind"], "order")
        self.assertIn("ValueError:forced strategy failure", failures[0]["message"])
        metrics = runtime.get_metrics_snapshot()
        self.assertEqual(metrics["handler_error_count"], 1)
        self.assertEqual(metrics["processed"], 0)
        self.assertEqual(metrics["last_error_kind"], "order")


if __name__ == "__main__":
    unittest.main()
