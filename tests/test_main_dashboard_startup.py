import ast
import copy
import io
import inspect
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import main as main_module


class FakeLogger:
    def __init__(self):
        self.callback = None
        self.messages = []

    def init_logging(self, _config):
        return None

    def set_ui_callback(self, callback):
        self.callback = callback

    def _record(self, level, message):
        self.messages.append((level, message))

    def info(self, message):
        self._record("INFO", message)

    def warning(self, message):
        self._record("WARNING", message)

    def error(self, message):
        self._record("ERROR", message)

    def critical(self, message):
        self._record("CRITICAL", message)


class FailedClock:
    def __init__(self):
        self.stop_calls = 0
        self.listeners_cleared = 0
        self.config = None

    def clear_listeners(self):
        self.listeners_cleared += 1

    def configure(self, config):
        self.config = config

    def start(self, testnet=False):
        self.testnet = testnet
        return False

    def is_ready(self):
        return False

    def health_snapshot(self, *, notify_listeners=True):
        return {
            "state": "unsynchronized",
            "ready": False,
            "reason": "exchange clock quorum failed (0/4)",
        }

    def stop(self):
        self.stop_calls += 1


class FakeDashboard:
    def __init__(self):
        self.startup_status = None
        self.health_event = None
        self.logs = []
        self.publish_calls = 0
        self.stop_calls = 0

    def set_startup_status(self, **status):
        self.startup_status = status

    def update_system_health(self, event):
        self.health_event = event

    def add_log(self, message):
        self.logs.append(message)

    def start(self):
        return "http://127.0.0.1:8765/"

    def publish_snapshot(self, force=True):
        self.publish_calls += 1
        return force

    def stop(self):
        self.stop_calls += 1


class MainDashboardStartupTests(unittest.TestCase):
    @staticmethod
    def config():
        return {
            "api_key": "test-key",
            "api_secret": "test-secret",
            "testnet": False,
            "symbols": ["BTCUSDT"],
            "execution": {"mode": "live"},
            "system": {
                "log_console": False,
                "time_sync": {"startup_required": True},
                "web_dashboard": {
                    "enabled": True,
                    "host": "127.0.0.1",
                    "port": 8765,
                    "open_browser": True,
                    "refresh_interval_ms": 100,
                },
            },
        }

    def test_clock_gate_failure_opens_diagnostics_without_trading_components(self):
        clock = FailedClock()
        dashboard = FakeDashboard()
        fake_logger = FakeLogger()
        browser_open = Mock(return_value=True)

        with (
            patch.object(main_module, "load_config", return_value=self.config()),
            patch.object(main_module, "time_service", clock),
            patch.object(main_module, "logger", fake_logger),
            patch.object(main_module, "LocalWebDashboard", return_value=dashboard),
            patch.object(main_module.webbrowser, "open", browser_open),
            patch.object(main_module.time, "sleep", side_effect=KeyboardInterrupt),
            patch.object(main_module, "EventEngine") as event_engine_type,
            patch.object(main_module, "build_gateway_bundle") as gateway_builder,
            patch.object(main_module, "OMS") as oms_type,
            patch.object(main_module, "create_primary_strategy") as strategy_factory,
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                main_module.main(["--config", "config.json"])

        self.assertIn("STARTUP_BLOCKED / OBSERVE_ONLY", output.getvalue())
        self.assertIn("http://127.0.0.1:8765/", output.getvalue())
        self.assertEqual(dashboard.startup_status["state"], "STARTUP_BLOCKED")
        self.assertFalse(dashboard.startup_status["execution_enabled"])
        self.assertTrue(dashboard.startup_status["restart_required"])
        self.assertEqual(dashboard.stop_calls, 1)
        self.assertGreaterEqual(dashboard.publish_calls, 2)
        self.assertEqual(clock.stop_calls, 1)
        browser_open.assert_called_once_with("http://127.0.0.1:8765/")
        event_engine_type.assert_not_called()
        gateway_builder.assert_not_called()
        oms_type.assert_not_called()
        strategy_factory.assert_not_called()

    def test_shutdown_runtime_verifies_cancel_before_closing_truth_and_clock(self):
        calls = []

        class FakeOms:
            def begin_shutdown(self, reason):
                calls.append(("oms_begin", reason))
                return True

            def cancel_all_account_orders_verified(self, provider, source):
                calls.append(("cancel_verified", provider, source))
                return True

            def stop(self, clean_shutdown=False, reason=""):
                calls.append(("oms_stop", clean_shutdown, reason))

        class Component:
            def __init__(self, name):
                self.name = name

            def stop(self, *args, **kwargs):
                calls.append((f"{self.name}_stop", args, kwargs))

            def close(self):
                calls.append((f"{self.name}_close",))

        class FakeEngine(Component):
            def wait_until_idle(self, timeout_sec):
                calls.append(("engine_drain", timeout_sec))
                return True

            def get_queue_snapshot(self):
                return {}

        strategy = Component("strategy")
        venue = Component("venue")
        truth_monitor = Component("truth_monitor")
        risk = Component("risk")
        gateway = Component("gateway")
        truth_provider = Component("truth_provider")
        recorder = Component("recorder")
        engine = FakeEngine("engine")
        clock = Component("clock")
        oms = FakeOms()
        runtime = {
            "oms": oms,
            "engine": engine,
            "gateway": gateway,
            "truth_provider": truth_provider,
            "strategy_runtime": strategy,
            "risk_supervisor": risk,
            "truth_monitor": truth_monitor,
            "venue_supervisor": venue,
            "recorder": recorder,
            "time_service": clock,
            "event_engine_config": {"shutdown_drain_timeout_sec": 1.5},
        }

        self.assertTrue(main_module.shutdown_runtime(runtime, "test_exit"))
        first_call_count = len(calls)
        self.assertTrue(main_module.shutdown_runtime(runtime, "duplicate"))
        self.assertEqual(len(calls), first_call_count)

        names = [call[0] for call in calls]
        self.assertLess(names.index("strategy_stop"), names.index("oms_begin"))
        self.assertLess(names.index("oms_begin"), names.index("cancel_verified"))
        self.assertLess(names.index("cancel_verified"), names.index("gateway_close"))
        self.assertLess(names.index("gateway_close"), names.index("engine_drain"))
        self.assertLess(names.index("engine_drain"), names.index("oms_stop"))
        self.assertLess(names.index("oms_stop"), names.index("engine_stop"))
        self.assertLess(names.index("engine_stop"), names.index("clock_stop"))
        self.assertIn(("oms_stop", True, "test_exit"), calls)

    def test_gateway_connect_keeps_independent_heartbeat_alive(self):
        enough_heartbeats = threading.Event()

        class BlockingGateway:
            def connect(self, symbols):
                self.symbols = list(symbols)
                if not enough_heartbeats.wait(1.0):
                    raise TimeoutError("startup heartbeat was not pumped")
                return True

        class Supervisor:
            enabled = True

            def __init__(self):
                self.pulses = 0

            def pulse_parent_heartbeat(self):
                self.pulses += 1
                if self.pulses >= 3:
                    enough_heartbeats.set()
                return True

        gateway = BlockingGateway()
        supervisor = Supervisor()
        self.assertTrue(
            main_module.connect_gateway_with_risk_heartbeat(
                gateway,
                ["BTCUSDT"],
                supervisor,
                poll_interval_sec=0.001,
            )
        )
        self.assertEqual(gateway.symbols, ["BTCUSDT"])
        self.assertGreaterEqual(supervisor.pulses, 3)

    def test_gateway_connect_fails_closed_if_sidecar_dies(self):
        release = threading.Event()

        class BlockingGateway:
            def connect(self, _symbols):
                release.wait(1.0)
                return True

        class DeadSupervisor:
            enabled = True

            def pulse_parent_heartbeat(self):
                return False

        with self.assertRaisesRegex(
            RuntimeError,
            "unavailable before gateway startup",
        ):
            main_module.connect_gateway_with_risk_heartbeat(
                BlockingGateway(),
                ["BTCUSDT"],
                DeadSupervisor(),
                poll_interval_sec=0.001,
            )
        release.set()


class CommissionConfigStartupTests(unittest.TestCase):
    class Gateway:
        def __init__(self, rates):
            self.rates = rates
            self.calls = []

        def get_commission_rate(self, symbol):
            self.calls.append(symbol)
            return self.rates[symbol]

    def test_synchronizes_conservative_fees_and_symbol_rpi_rates(self):
        gateway = self.Gateway(
            {
                "BTCUSDT": {
                    "symbol": "BTCUSDT",
                    "makerCommissionRate": "0.0002",
                    "takerCommissionRate": "0.0006",
                    "rpiCommissionRate": "0.00001",
                },
                "ETHUSDT": {
                    "symbol": "ETHUSDT",
                    "makerCommissionRate": "0.0003",
                    "takerCommissionRate": "0.0005",
                    "rpiCommissionRate": "0.00002",
                },
            }
        )
        config = {"backtest": {"preserved": True}}

        result = main_module.synchronize_commission_config(
            gateway,
            config,
            ["btcusdt", "ETHUSDT"],
        )

        self.assertEqual(gateway.calls, ["BTCUSDT", "ETHUSDT"])
        self.assertEqual(result["maker_fee"], 0.0003)
        self.assertEqual(result["taker_fee"], 0.0006)
        self.assertEqual(
            result["rpi_commission_rates"],
            {"BTCUSDT": 0.00001, "ETHUSDT": 0.00002},
        )
        self.assertEqual(config["backtest"]["maker_fee"], 0.0003)
        self.assertEqual(config["backtest"]["taker_fee"], 0.0006)
        self.assertEqual(config["backtest"]["rpi_commission_rate"], 0.0)
        self.assertEqual(
            config["backtest"]["rpi_commission_rates"],
            {"BTCUSDT": 0.00001, "ETHUSDT": 0.00002},
        )
        self.assertTrue(config["backtest"]["preserved"])

    def test_missing_malformed_and_out_of_range_truth_fail_closed(self):
        valid = {
            "makerCommissionRate": "0.0002",
            "takerCommissionRate": "0.0005",
            "rpiCommissionRate": "0.0001",
        }
        invalid_payloads = {
            "missing": {
                "makerCommissionRate": "0.0002",
                "takerCommissionRate": "0.0005",
            },
            "malformed": {**valid, "makerCommissionRate": "not-a-rate"},
            "non_finite": {**valid, "takerCommissionRate": "nan"},
            "out_of_range": {**valid, "rpiCommissionRate": "0.010001"},
            "unavailable": None,
        }

        for case, payload in invalid_payloads.items():
            with self.subTest(case=case):
                config = {
                    "backtest": {
                        "maker_fee": 0.009,
                        "taker_fee": 0.009,
                        "rpi_commission_rate": 0.009,
                        "rpi_commission_rates": {"OLD": 0.009},
                    }
                }
                original = copy.deepcopy(config)
                gateway = self.Gateway({"BTCUSDT": payload})

                with self.assertRaises(RuntimeError):
                    main_module.synchronize_commission_config(
                        gateway,
                        config,
                        ["BTCUSDT"],
                    )

                self.assertEqual(config, original)

    def test_startup_syncs_fees_after_connect_and_before_oms_bootstrap(self):
        source = ast.parse(inspect.getsource(main_module._run_main))
        call_lines = {}
        expected = {
            "connect_gateway_with_risk_heartbeat",
            "synchronize_commission_config",
            "bootstrap_or_rearm",
        }
        for node in ast.walk(source):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in expected:
                    call_lines[node.func.id] = node.lineno

        self.assertEqual(set(call_lines), expected)
        self.assertLess(
            call_lines["connect_gateway_with_risk_heartbeat"],
            call_lines["synchronize_commission_config"],
        )
        self.assertLess(
            call_lines["synchronize_commission_config"],
            call_lines["bootstrap_or_rearm"],
        )


if __name__ == "__main__":
    unittest.main()
