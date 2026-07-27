import ast
import copy
import io
import inspect
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import launcher as launcher_module
import main as main_module
from infrastructure import admin_control as admin_control_module


class FakeLogger:
    def __init__(self):
        self.callback = None
        self.alert_callback = None
        self.messages = []

    def init_logging(self, _config):
        return None

    def set_ui_callback(self, callback):
        self.callback = callback

    def set_alert_callback(self, callback):
        self.alert_callback = callback

    def flush(self, timeout_sec=1.0):
        self.flush_timeout_sec = timeout_sec
        return True

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
    def test_flat_shutdown_truth_uses_emergency_rest_reserve(self):
        class SnapshotProvider:
            supports_emergency_query_priority = True

            def __init__(self):
                self.priorities = []

            def get_all_positions(self, *, emergency=False):
                self.priorities.append(bool(emergency))
                return []

        provider = SnapshotProvider()

        self.assertTrue(
            main_module.verify_account_flat(
                provider,
                required_flat_snapshots=2,
                settle_interval_sec=0.01,
            )
        )
        self.assertEqual(provider.priorities, [True, True])

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
            patch.object(
                main_module,
                "start_external_alert_service",
                return_value=None,
            ),
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

    def test_config_check_does_not_construct_runtime_components(self):
        config = self.config()
        config["strategy"] = {"primary_model": "glft"}
        with (
            patch.object(main_module, "load_config", return_value=config),
            patch.object(main_module, "EventEngine") as event_engine_type,
            patch.object(main_module, "build_gateway_bundle") as gateway_builder,
            patch.object(main_module, "OMS") as oms_type,
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                result = main_module._run_main(
                    ["--config", "config.json", "--check-config"],
                    runtime={},
                )

        self.assertEqual(result, 0)
        self.assertIn("CONFIG_OK mode=live symbols=1 primary_model=glft", output.getvalue())
        event_engine_type.assert_not_called()
        gateway_builder.assert_not_called()
        oms_type.assert_not_called()

    def test_clock_gate_returns_error_when_diagnostics_cannot_start(self):
        clock = FailedClock()
        with (
            patch.object(main_module, "load_config", return_value=self.config()),
            patch.object(main_module, "time_service", clock),
            patch.object(main_module, "logger", FakeLogger()),
            patch.object(
                main_module,
                "start_external_alert_service",
                return_value=None,
            ),
            patch.object(
                main_module,
                "run_startup_blocked_dashboard",
                return_value=False,
            ),
            patch.object(main_module, "EventEngine") as event_engine_type,
        ):
            result = main_module._run_main(
                ["--config", "config.json"],
                runtime={},
            )

        self.assertEqual(result, 2)
        event_engine_type.assert_not_called()

    def test_shutdown_runtime_verifies_cancel_before_closing_truth_and_clock(self):
        calls = []

        class FakeOms:
            def close_outbound_gate(self, reason, wait=False):
                calls.append(("outbound_gate_close", reason, wait))
                return True

            def begin_shutdown(self, reason):
                calls.append(("oms_begin", reason))
                return True

            def cancel_all_account_orders_verified(self, provider, source):
                calls.append(("cancel_verified", provider, source))
                return True

            def stop(self, clean_shutdown=False, reason=""):
                calls.append(("oms_stop", clean_shutdown, reason))
                return {"stopped": True, "clean": clean_shutdown}

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

        class FakeGateway(Component):
            def begin_shutdown(self):
                calls.append(("gateway_begin_shutdown",))
                return True

        class FakeRiskController:
            kill_switch_triggered = True
            kill_state = "FLAT_VERIFIED"

        class FakeTruthProvider(Component):
            def get_all_positions(self):
                return []

        strategy = Component("strategy")
        venue = Component("venue")
        truth_monitor = Component("truth_monitor")
        risk = Component("risk")
        gateway = FakeGateway("gateway")
        truth_provider = FakeTruthProvider("truth_provider")
        recorder = Component("recorder")
        admin_control = Component("admin_control")
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
            "risk_controller": FakeRiskController(),
            "truth_monitor": truth_monitor,
            "venue_supervisor": venue,
            "recorder": recorder,
            "admin_control": admin_control,
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
        self.assertLess(names.index("admin_control_close"), names.index("gateway_close"))
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

    def test_required_live_dashboard_bind_failure_is_fatal(self):
        class FailedDashboard:
            def start(self):
                raise OSError("address already in use")

        fake_logger = FakeLogger()
        with patch.object(main_module, "logger", fake_logger):
            with self.assertRaisesRegex(
                RuntimeError,
                "Local dashboard failed to start",
            ):
                main_module.start_local_dashboard(
                    FailedDashboard(),
                    {"open_browser": False},
                    required=True,
                )

    def test_live_dashboard_bind_precedes_gateway_connection_statically(self):
        source = inspect.getsource(main_module._run_main)

        self.assertLess(
            source.index("start_local_dashboard("),
            source.index("connect_gateway_with_risk_heartbeat("),
        )


class PaperLauncherTests(unittest.TestCase):
    def test_watchdog_uses_workspace_and_explicit_config_path(self):
        process = Mock()
        process.wait.return_value = 0
        watchdog = launcher_module.ProcessWatchdog(
            target_script=launcher_module.TARGET_SCRIPT,
            config_path=launcher_module.CONFIG_PATH,
            restart_interval_sec=0.0,
        )

        with patch.object(
            launcher_module.subprocess,
            "Popen",
            return_value=process,
        ) as popen:
            result = watchdog.run()

        self.assertEqual(result, 0)
        command = popen.call_args.args[0]
        self.assertEqual(command[0], launcher_module.sys.executable)
        self.assertEqual(command[1], str(launcher_module.TARGET_SCRIPT))
        self.assertEqual(command[2:], ["--config", str(launcher_module.CONFIG_PATH)])
        self.assertEqual(
            popen.call_args.kwargs["cwd"],
            str(launcher_module.WORKSPACE_DIR),
        )

    def test_launcher_returns_error_when_config_is_rejected(self):
        with (
            patch.object(
                launcher_module,
                "launcher_allows_runtime",
                return_value=(False, "bad config"),
            ),
            patch.object(launcher_module, "ProcessWatchdog") as watchdog_type,
        ):
            result = launcher_module.main()

        self.assertEqual(result, 2)
        watchdog_type.assert_not_called()


class MainShutdownLatchOrderStaticTests(unittest.TestCase):
    @staticmethod
    def shutdown_source_tree():
        source = inspect.getsource(main_module.shutdown_runtime)
        return source, ast.parse(source)

    @staticmethod
    def run_step_line(tree, step_name):
        lines = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "run_step":
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            if node.args[0].value == step_name:
                lines.append(node.lineno)
        if not lines:
            raise AssertionError(f"run_step {step_name!r} was not found")
        return min(lines)

    @staticmethod
    def call_lines(tree, function_name):
        lines = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                called_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                called_name = node.func.attr
            else:
                continue
            if called_name == function_name:
                lines.append(node.lineno)
        return sorted(lines)

    @staticmethod
    def runtime_key_assignment_lines(tree, key):
        lines = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Subscript):
                    continue
                if not isinstance(target.value, ast.Name):
                    continue
                if target.value.id != "runtime":
                    continue
                if (
                    isinstance(target.slice, ast.Constant)
                    and target.slice.value == key
                ):
                    lines.append(node.lineno)
        return sorted(lines)

    def test_flat_verification_precedes_latches_and_final_truth_statically(self):
        _, tree = self.shutdown_source_tree()

        close_gate = self.run_step_line(tree, "oms_close_outbound_gate")
        stop_strategy = self.run_step_line(tree, "strategy_stop")
        oms_latch = self.run_step_line(tree, "oms_begin_shutdown")
        wait_for_flat = next(
            line
            for line in self.call_lines(
                tree,
                "wait_for_kill_flatten_verification",
            )
            if stop_strategy < line < oms_latch
        )
        flat_verified = min(
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Compare)
            and any(
                isinstance(comparator, ast.Constant)
                and comparator.value == "FLAT_VERIFIED"
                for comparator in node.comparators
            )
        )
        gateway_latch = self.run_step_line(tree, "gateway_begin_shutdown")
        final_order_truth = self.call_lines(tree, "verify_account_orders")[0]
        final_position_truth = self.call_lines(
            tree,
            "verify_account_positions",
        )[0]
        sidecar_quiesce = self.run_step_line(
            tree,
            "risk_supervisor_quiesce",
        )
        sidecar_stop = self.run_step_line(tree, "risk_supervisor_stop")

        self.assertLess(close_gate, stop_strategy)
        self.assertLess(stop_strategy, wait_for_flat)
        self.assertLess(wait_for_flat, flat_verified)
        self.assertLess(flat_verified, oms_latch)
        self.assertLess(oms_latch, gateway_latch)
        self.assertLess(gateway_latch, final_order_truth)
        self.assertLess(final_order_truth, final_position_truth)
        self.assertLess(final_position_truth, sidecar_quiesce)
        self.assertLess(sidecar_quiesce, sidecar_stop)

    def test_failed_flat_verification_returns_before_latches_statically(self):
        source, tree = self.shutdown_source_tree()
        oms_latch = self.run_step_line(tree, "oms_begin_shutdown")
        gateway_latch = self.run_step_line(tree, "gateway_begin_shutdown")
        guard = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and "account_shutdown_proof_required" in ast.dump(node.test)
            and "kill_flatten_verified" in ast.dump(node.test)
        )
        publisher = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "publish_shutdown_blocked"
        )
        guard_source = ast.get_source_segment(source, guard) or ""
        publisher_source = ast.get_source_segment(source, publisher) or ""
        guard_returns = [
            node
            for node in ast.walk(ast.Module(body=guard.body, type_ignores=[]))
            if isinstance(node, ast.Return)
            and isinstance(node.value, ast.Constant)
            and node.value.value is False
        ]

        self.assertIn("publish_shutdown_blocked", guard_source)
        self.assertIn('runtime["_shutdown_retryable"] = True', publisher_source)
        self.assertTrue(guard_returns)
        self.assertLess(guard.lineno, oms_latch)
        self.assertLess(guard.lineno, gateway_latch)
        self.assertGreater(
            self.runtime_key_assignment_lines(tree, "_shutdown_complete")[0],
            guard.lineno,
        )
        self.assertGreater(
            self.runtime_key_assignment_lines(tree, "_shutdown_in_progress")[-1],
            guard.lineno,
        )

    def test_preconnect_and_paper_truth_boundaries_remain_statically(self):
        source, tree = self.shutdown_source_tree()
        live_boundary_source = inspect.getsource(
            main_module.requires_live_canary_shutdown_truth
        )
        verify_orders = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "verify_account_orders"
        )
        verify_positions = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "verify_account_positions"
        )
        order_source = ast.get_source_segment(source, verify_orders) or ""
        position_source = ast.get_source_segment(source, verify_positions) or ""

        self.assertIn("if not account_shutdown_proof_required", order_source)
        self.assertIn(
            "verify_preconnect_shutdown_no_order_path",
            order_source,
        )
        self.assertIn("shutdown_latched", order_source)
        self.assertIn("if not live_canary_truth_required", position_source)
        self.assertIn("return bool(kill_flatten_verified)", position_source)
        self.assertIn(
            'not runtime.get("paper_trade", False)',
            live_boundary_source,
        )
        self.assertIn(
            'stage in {"canary", "rpi_calibration_canary"}',
            live_boundary_source,
        )
        self.assertLess(
            self.run_step_line(tree, "gateway_begin_shutdown"),
            self.call_lines(tree, "verify_account_orders")[0],
        )

    def test_parent_flatten_retry_does_not_depend_on_sidecar_resume(self):
        source, tree = self.shutdown_source_tree()
        resumed_branch = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "resumed"
        )
        restart_helper = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "restart_parent_kill_after_truth_drift"
        )
        helper_source = ast.get_source_segment(source, restart_helper) or ""
        helper_calls = self.call_lines(
            tree,
            "restart_parent_kill_after_truth_drift",
        )
        sidecar_quiesce = self.run_step_line(tree, "risk_supervisor_quiesce")

        self.assertEqual(len(helper_calls), 2)
        self.assertIn("restart_kill_switch_after_truth_drift", helper_source)
        self.assertIn("wait_for_kill_flatten_verification", helper_source)
        self.assertLess(helper_calls[0], sidecar_quiesce)
        self.assertGreater(helper_calls[1], resumed_branch.end_lineno)


class AdminControlStartupTests(unittest.TestCase):
    def test_atomic_json_write_retries_transient_replace_conflict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "state.json")
            real_replace = os.replace
            attempts = []

            def replace_after_transient_conflict(source, destination):
                attempts.append((source, destination))
                if len(attempts) == 1:
                    raise PermissionError(13, "transient sharing conflict")
                return real_replace(source, destination)

            with (
                patch(
                    "infrastructure.admin_control.os.replace",
                    side_effect=replace_after_transient_conflict,
                ),
                patch(
                    "infrastructure.admin_control."
                    "_sleep_before_atomic_replace_retry"
                ) as sleep,
            ):
                admin_control_module._atomic_write_json(
                    target,
                    {"ready": True},
                )

            with open(target, "r", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), {"ready": True})
            self.assertEqual(len(attempts), 2)
            sleep.assert_called_once_with(0.01)

    def test_atomic_json_write_exhausts_retry_budget_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "state.json")
            with (
                patch(
                    "infrastructure.admin_control.os.replace",
                    side_effect=PermissionError(13, "persistent access denied"),
                ) as replace,
                patch(
                    "infrastructure.admin_control."
                    "_sleep_before_atomic_replace_retry"
                ) as sleep,
                self.assertRaises(PermissionError),
            ):
                admin_control_module._atomic_write_json(
                    target,
                    {"ready": False},
                )

            attempts = admin_control_module.ATOMIC_REPLACE_MAX_ATTEMPTS
            self.assertEqual(replace.call_count, attempts)
            self.assertEqual(sleep.call_count, attempts - 1)
            self.assertEqual(os.listdir(tmpdir), [])

    def test_admin_session_heartbeat_is_throttled_below_expiry_window(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "system": {
                    "admin_control": {
                        "path": tmpdir,
                        "session_max_age_sec": 2.0,
                    }
                }
            }
            with (
                patch(
                    "infrastructure.admin_control.time.perf_counter",
                    side_effect=[100.0, 100.1, 100.6],
                ),
                patch(
                    "infrastructure.admin_control._atomic_write_json"
                ) as write_json,
            ):
                server = admin_control_module.AdminControlServer(
                    object(),
                    config,
                )
                server.poll_once()
                server.poll_once()

            self.assertEqual(server.session_heartbeat_interval_sec, 0.5)
            self.assertEqual(write_json.call_count, 2)

    def test_admin_close_withdraws_only_its_owned_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "system": {
                    "admin_control": {
                        "path": tmpdir,
                        "session_max_age_sec": 2.0,
                    }
                }
            }
            server = admin_control_module.AdminControlServer(
                object(),
                config,
            )
            session_path = server.paths["session_path"]
            self.assertTrue(os.path.exists(session_path))

            self.assertTrue(server.close())

            self.assertFalse(os.path.exists(session_path))
            self.assertFalse(server.poll_once())

    def test_admin_close_does_not_delete_a_replacement_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "system": {
                    "admin_control": {
                        "path": tmpdir,
                        "session_max_age_sec": 2.0,
                    }
                }
            }
            server = admin_control_module.AdminControlServer(
                object(),
                config,
            )
            replacement = {
                "schema": admin_control_module.ADMIN_SESSION_SCHEMA,
                "session_id": "replacement",
            }
            admin_control_module._atomic_write_json(
                server.paths["session_path"],
                replacement,
            )

            self.assertTrue(server.close())

            with open(
                server.paths["session_path"],
                "r",
                encoding="utf-8",
            ) as handle:
                self.assertEqual(json.load(handle), replacement)

    def test_admin_cli_bypasses_full_live_config_loader(self):
        minimal_config = {
            "live_launch": {"deployment_id": "canary-test-001"},
            "system": {
                "admin_control": {
                    "path": os.path.abspath("storage/admin"),
                    "command_ttl_sec": 10.0,
                    "session_max_age_sec": 2.0,
                }
            },
        }
        submit = Mock(
            return_value={
                "accepted": True,
                "status": "ok",
                "message": "OMS status snapshot.",
            }
        )
        with (
            patch.object(
                main_module,
                "load_admin_control_config",
                return_value=minimal_config,
            ) as minimal_loader,
            patch.object(
                main_module,
                "load_config",
                side_effect=AssertionError("full loader must not run"),
            ) as full_loader,
            patch.object(main_module, "submit_admin_command", submit),
        ):
            main_module._run_main(
                ["--config", "live.json", "--admin-command", "status"]
            )

        minimal_loader.assert_called_once_with("live.json")
        full_loader.assert_not_called()
        submit.assert_called_once()

    def test_minimal_loader_ignores_live_secrets_and_approvals(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            admin_dir = os.path.join(tmpdir, "admin")
            config_path = os.path.join(tmpdir, "live.json")
            payload = {
                "execution": {"mode": "live"},
                "api_key_env": "",
                "api_secret_env": "",
                "live_launch": {"permit_path": "missing.json"},
                "system": {
                    "admin_control": {"path": admin_dir},
                    "external_alerts": {"webhook_env": "MISSING_WEBHOOK"},
                },
            }
            with open(config_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)

            loaded = main_module.load_admin_control_config(config_path)

        self.assertEqual(
            loaded,
            {
                "live_launch": {"deployment_id": ""},
                "system": {
                    "admin_control": {
                        "path": os.path.abspath(admin_dir),
                        "command_ttl_sec": 10.0,
                        "session_max_age_sec": 2.0,
                    }
                }
            },
        )

    def test_minimal_loader_resolves_relative_admin_path_from_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            deploy_dir = os.path.join(tmpdir, "deploy")
            caller_dir = os.path.join(tmpdir, "caller")
            os.makedirs(deploy_dir)
            os.makedirs(caller_dir)
            config_path = os.path.join(deploy_dir, "live.json")
            with open(config_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "system": {
                            "admin_control": {"path": "state/admin"}
                        }
                    },
                    handle,
                )
            previous = os.getcwd()
            try:
                os.chdir(caller_dir)
                loaded = main_module.load_admin_control_config(config_path)
            finally:
                os.chdir(previous)

        self.assertEqual(
            loaded["system"]["admin_control"]["path"],
            os.path.abspath(os.path.join(deploy_dir, "state", "admin")),
        )

    def test_minimal_loader_rejects_malformed_and_non_object_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            for name, payload, expected in (
                ("malformed", "{", "malformed"),
                ("array", "[]", "JSON object"),
                ("bad-system", '{"system": 1}', "system must be a JSON object"),
                (
                    "bad-admin",
                    '{"system":{"admin_control":1}}',
                    "admin_control must be a JSON object",
                ),
                (
                    "duplicate",
                    '{"system":{},"system":{}}',
                    "duplicate JSON object key",
                ),
            ):
                with self.subTest(name=name):
                    config_path = os.path.join(tmpdir, f"{name}.json")
                    with open(config_path, "w", encoding="utf-8") as handle:
                        handle.write(payload)
                    with self.assertRaisesRegex((ValueError, OSError), expected):
                        main_module.load_admin_control_config(config_path)

    def test_minimal_loader_rejects_dangerous_or_ambiguous_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "live.json")
            cases = {
                "": "non-empty",
                ".": "working directory",
                "..\\admin": "parent traversal",
                "C:admin": "drive-relative",
                "\\\\server\\share\\admin": "UNC or device",
                "~/admin": "user or environment expansion",
            }
            for admin_path, expected in cases.items():
                with self.subTest(admin_path=admin_path):
                    with open(config_path, "w", encoding="utf-8") as handle:
                        json.dump(
                            {
                                "system": {
                                    "admin_control": {"path": admin_path}
                                }
                            },
                            handle,
                        )
                    with self.assertRaisesRegex(ValueError, expected):
                        main_module.load_admin_control_config(config_path)


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
        self.assertEqual(config["backtest"]["rpi_commission_rate"], 0.00002)
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
            "wait_for_initial_market_data_readiness",
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
            call_lines["wait_for_initial_market_data_readiness"],
        )
        self.assertLess(
            call_lines["wait_for_initial_market_data_readiness"],
            call_lines["bootstrap_or_rearm"],
        )

    def test_strategy_runtime_starts_only_after_bootstrap_and_monitors(self):
        source = ast.parse(inspect.getsource(main_module._run_main))
        call_lines = {}
        for node in ast.walk(source):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "bootstrap_or_rearm":
                call_lines["bootstrap"] = node.lineno
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr != "start":
                continue
            owner = node.func.value
            if isinstance(owner, ast.Name) and owner.id in {
                "strategy_runtime",
                "truth_monitor",
                "venue_supervisor",
            }:
                call_lines[owner.id] = node.lineno

        self.assertEqual(
            set(call_lines),
            {"bootstrap", "strategy_runtime", "truth_monitor", "venue_supervisor"},
        )
        self.assertLess(call_lines["bootstrap"], call_lines["truth_monitor"])
        self.assertLess(call_lines["bootstrap"], call_lines["venue_supervisor"])
        self.assertLess(call_lines["truth_monitor"], call_lines["strategy_runtime"])
        self.assertLess(call_lines["venue_supervisor"], call_lines["strategy_runtime"])


if __name__ == "__main__":
    unittest.main()
