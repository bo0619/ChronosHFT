import argparse
import multiprocessing
import threading
import time
import webbrowser
from decimal import Decimal, InvalidOperation

from data.live_evidence import LiveEvidenceRecorder, RecorderGroup
from data.recorder import DataRecorder
from data.ref_data import ref_data_manager
from event.engine import EventEngine
from event.type import OMSCapabilityMode
from gateway.binance.gateway import BinanceGateway
from gateway.binance.rate_limit_budget import BinanceRateLimitBudget
from gateway.binance.truth_provider import BinanceTruthSnapshotProvider
from infrastructure.admin_control import (
    AdminControlServer,
    coordinated_rearm,
    load_admin_control_config,
    submit_admin_command,
)
from infrastructure.config_scaling import load_root_config
from infrastructure.commission_truth import parse_commission_rate_payload
from infrastructure.external_alerts import ExternalAlertService
from infrastructure.live_config_guard import (
    validate_live_account_equity_truth,
    validate_live_flat_start_truth,
)
from infrastructure.logger import logger
from infrastructure.paper_trade import apply_paper_trade_mode, is_paper_trade
from infrastructure.process_resources import ProcessResourceMonitor
from infrastructure.systemd_watchdog import SystemdWatchdog
from infrastructure.rpi_policy import (
    requires_zero_rpi_commission,
    validate_live_rpi_policy,
)
from infrastructure.runtime_application import (
    RuntimeApplication,
    RuntimeApplicationServices,
    RuntimeConfigurationServices,
    RuntimeFactoryServices,
    RuntimeLoopServices,
    RuntimePlatformServices,
    RuntimeSafetyServices,
)
from infrastructure.runtime_bindings import (
    RuntimeEventBindings,
    RuntimeWatchdogState,
)
from infrastructure.runtime_control_loop import (
    RuntimeControlLoop,
    RuntimeControlServices,
)
from infrastructure.runtime_failure_policy import RuntimeFailurePolicy
from infrastructure.runtime_resources import RuntimeResources
from infrastructure.runtime_shutdown import (
    RuntimeShutdownCoordinator,
    RuntimeShutdownServices,
)
from infrastructure.time_service import time_service
from infrastructure.truth_monitor import TruthMonitor
from infrastructure.venue_supervisor import VenueSupervisor
from oms.engine import OMS
from risk.independent_supervisor import IndependentRiskSupervisor
from risk.manager import RiskManager
from strategy.registry import create_primary_strategy
from strategy.runtime import StrategyRuntime
from ui.web_dashboard import LocalWebDashboard


def parse_cli_args(argv=None):
    parser = argparse.ArgumentParser(description="ChronosHFT trading engine")
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to the root config JSON file.",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate and summarize configuration without starting runtime components.",
    )
    parser.add_argument(
        "--rearm",
        action="store_true",
        help="If the recovered OMS state requires manual rearm, execute it automatically at startup.",
    )
    parser.add_argument(
        "--rearm-reason",
        default="cli",
        help="Operator reason recorded when --rearm is used.",
    )
    parser.add_argument(
        "--admin-command",
        choices=["rearm", "status"],
        help="Send an admin command to an already-running ChronosHFT process and exit.",
    )
    parser.add_argument(
        "--admin-reason",
        default="operator_ack",
        help="Reason recorded for the admin command.",
    )
    parser.add_argument(
        "--admin-timeout",
        type=float,
        default=5.0,
        help="How long to wait for a running process to acknowledge an admin command.",
    )
    return parser.parse_args(argv)


def load_config(path="config.json"):
    return load_root_config(path)


def bootstrap_or_rearm(
    oms_system,
    auto_rearm=False,
    rearm_reason="cli",
    risk_manager=None,
    risk_supervisor=None,
):
    bootstrapped = oms_system.bootstrap()
    if bootstrapped:
        return True

    if getattr(oms_system, "manual_rearm_required", False):
        hint = "python main.py --admin-command rearm --admin-reason operator_ack"
        logger.warning(f"[OMS] Manual rearm required. Command: {hint}")
        if auto_rearm:
            logger.warning(f"[OMS] Auto rearm requested via CLI: {rearm_reason}")
            result = coordinated_rearm(
                oms_system,
                rearm_reason,
                risk_manager=risk_manager,
                risk_supervisor=risk_supervisor,
            )
            return bool(result.get("accepted", False))
    return False


def run_live_risk_checks(risk_controller, independent_supervisor=None):
    supervisor_healthy = True
    if independent_supervisor is not None:
        supervisor_healthy = bool(independent_supervisor.tick())
    set_supervisor_health = getattr(
        risk_controller,
        "set_venue_dms_supervisor_health",
        None,
    )
    if callable(set_supervisor_health):
        set_supervisor_health(supervisor_healthy)
    funding_healthy = bool(risk_controller.check_funding_guard())
    risk_healthy = bool(risk_controller.check_market_data_freshness())
    return supervisor_healthy and funding_healthy and risk_healthy


_RUNTIME_RESOURCE_FIELDS = (
    "sampled_at_monotonic",
    "available",
    "healthy",
    "status",
    "reason",
    "breaches",
    "warnings",
    "rss_bytes",
    "rss_warn_bytes",
    "rss_freeze_bytes",
    "rss_growth_bytes_per_min",
    "main_thread_count",
    "max_main_threads",
    "total_thread_count",
    "max_total_threads",
    "main_fd_count",
    "max_main_fds",
    "total_fd_count",
    "max_total_fds",
    "cpu_percent_one_core",
    "cpu_warn_percent_one_core",
    "process_count",
    "explicit_process_count",
    "discovered_process_count",
    "process_discovery_complete",
    "process_discovery_reason",
    "max_processes",
    "missing_pids",
    "sample_interval_sec",
)
_RUNTIME_WATCHDOG_FIELDS = (
    "enabled",
    "reason",
    "watchdog_period_sec",
    "ping_interval_sec",
    "send_count",
    "error_count",
    "last_error",
    "last_success_at_monotonic",
)


def build_runtime_resource_event(
    resource_snapshot,
    watchdog_snapshot,
    *,
    event_time=None,
):
    """Build one bounded, versioned Paper soak-observation payload."""

    resource = dict(resource_snapshot or {})
    watchdog = dict(watchdog_snapshot or {})
    status = str(resource.get("status", "unknown") or "unknown")
    if not bool(resource.get("healthy", True)):
        severity = "CRITICAL"
    elif status == "breach_pending":
        severity = "ERROR"
    elif status in {"warning", "unavailable"}:
        severity = "WARNING"
    else:
        severity = "INFO"
    return {
        "event_time": time.time() if event_time is None else float(event_time),
        "level": severity,
        "state": status,
        "details": {
            "schema": "chronoshft.paper_runtime_resources.v1",
            "resource": {
                field: resource.get(field)
                for field in _RUNTIME_RESOURCE_FIELDS
            },
            "systemd_watchdog": {
                field: watchdog.get(field)
                for field in _RUNTIME_WATCHDOG_FIELDS
            },
        },
    }


def wait_for_initial_market_data_readiness(
    risk_controller,
    independent_supervisor=None,
    *,
    timeout_sec: float = 5.0,
    poll_interval_sec: float = 0.1,
) -> bool:
    """Keep safety leases alive while waiting for fresh mark and book truth."""
    readiness = getattr(
        risk_controller,
        "market_data_readiness_failures",
        None,
    )
    if not callable(readiness):
        return False
    deadline = time.perf_counter() + max(0.0, float(timeout_sec or 0.0))
    while True:
        run_live_risk_checks(risk_controller, independent_supervisor)
        failures = readiness()
        if not failures:
            return True
        remaining = deadline - time.perf_counter()
        if remaining <= 0.0:
            logger.error(
                "Initial market-data readiness timed out: "
                f"{failures}"
            )
            return False
        time.sleep(
            min(
                max(0.01, float(poll_interval_sec or 0.0)),
                remaining,
            )
        )


def connect_gateway_with_risk_heartbeat(
    gateway,
    symbols,
    independent_supervisor=None,
    poll_interval_sec: float = 0.05,
    cancellation_timeout_sec: float = 2.0,
):
    """Keep the independent sidecar parent heartbeat alive during startup I/O."""
    if independent_supervisor is None or not bool(
        getattr(independent_supervisor, "enabled", False)
    ):
        return gateway.connect(symbols)

    pulse = getattr(independent_supervisor, "pulse_parent_heartbeat", None)
    if not callable(pulse):
        raise RuntimeError(
            "IndependentRiskSupervisor does not expose a startup heartbeat"
        )
    if not pulse():
        raise RuntimeError(
            "IndependentRiskSupervisor is unavailable before gateway startup"
        )

    completed = threading.Event()
    outcome = {}

    def connect_worker():
        try:
            outcome["connected"] = gateway.connect(symbols)
        except BaseException as exc:
            outcome["error"] = exc
        finally:
            completed.set()

    worker = threading.Thread(
        target=connect_worker,
        daemon=True,
        name="GatewayStartup",
    )
    worker.start()
    interval = max(0.01, float(poll_interval_sec or 0.05))

    def cancel_startup() -> None:
        begin_shutdown = getattr(gateway, "begin_shutdown", None)
        if not callable(begin_shutdown):
            raise RuntimeError(
                "Gateway does not expose begin_shutdown for startup "
                "cancellation"
            )
        begin_shutdown()
        worker.join(
            timeout=max(0.0, float(cancellation_timeout_sec or 0.0))
        )
        if worker.is_alive():
            raise RuntimeError(
                "Gateway startup worker did not stop after cancellation"
            )

    try:
        while not completed.wait(interval):
            try:
                heartbeat_ok = bool(pulse())
            except BaseException as exc:
                cancel_startup()
                raise RuntimeError(
                    "IndependentRiskSupervisor heartbeat failed during "
                    "gateway startup"
                ) from exc
            if not heartbeat_ok:
                cancel_startup()
                raise RuntimeError(
                    "IndependentRiskSupervisor stopped during gateway "
                    "startup"
                )
        worker.join(timeout=0.0)

        error = outcome.get("error")
        if error is not None:
            cancel_startup()
            raise error
        try:
            heartbeat_ok = bool(pulse())
        except BaseException as exc:
            cancel_startup()
            raise RuntimeError(
                "IndependentRiskSupervisor heartbeat failed as gateway "
                "startup completed"
            ) from exc
        if not heartbeat_ok:
            cancel_startup()
            raise RuntimeError(
                "IndependentRiskSupervisor stopped as gateway startup "
                "completed"
            )
    except BaseException:
        if worker.is_alive():
            cancel_startup()
        raise

    return outcome.get("connected")


def call_with_risk_heartbeat(
    callback,
    independent_supervisor=None,
    poll_interval_sec: float = 0.05,
):
    """Keep the sidecar heartbeat live while a blocking safety call runs."""
    if independent_supervisor is None or not bool(
        getattr(independent_supervisor, "enabled", False)
    ):
        return callback()

    pulse = getattr(independent_supervisor, "pulse_parent_heartbeat", None)
    if not callable(pulse):
        raise RuntimeError(
            "IndependentRiskSupervisor does not expose a startup heartbeat"
        )
    if not pulse():
        raise RuntimeError("IndependentRiskSupervisor is unavailable")

    stop_event = threading.Event()
    heartbeat_failure = {}
    interval = max(0.01, float(poll_interval_sec or 0.05))

    def heartbeat_worker():
        while not stop_event.wait(interval):
            try:
                heartbeat_ok = bool(pulse())
            except BaseException as exc:
                heartbeat_failure["reason"] = (
                    f"{type(exc).__name__}:{exc}"
                )
                return
            if not heartbeat_ok:
                heartbeat_failure["reason"] = "supervisor_process_down"
                return

    worker = threading.Thread(
        target=heartbeat_worker,
        daemon=True,
        name="RiskHeartbeatGuard",
    )
    worker.start()
    try:
        result = callback()
    finally:
        stop_event.set()
        worker.join(timeout=max(0.1, interval * 2.0))

    failure_reason = heartbeat_failure.get("reason")
    if failure_reason:
        raise RuntimeError(
            "IndependentRiskSupervisor stopped during blocking safety call: "
            f"{failure_reason}"
        )
    if not pulse():
        raise RuntimeError(
            "IndependentRiskSupervisor stopped as blocking safety call completed"
        )
    return result


def wait_for_initial_truth_snapshot(
    truth_monitor,
    independent_supervisor=None,
) -> bool:
    """Synchronously prove exchange account truth before OMS bootstrap."""
    try:
        healthy = call_with_risk_heartbeat(
            truth_monitor.poll_once,
            independent_supervisor,
        )
    except Exception as exc:
        logger.error(
            "[Startup] Initial truth snapshot failed: "
            f"{type(exc).__name__}:{exc}"
        )
        return False
    if not healthy:
        logger.error("[Startup] Initial truth snapshot was not healthy")
        return False
    return True


def wait_for_kill_flatten_verification(
    risk_controller,
    independent_supervisor=None,
) -> bool:
    """Keep the sidecar alive until an active kill/flatten reaches a terminal state."""
    tick = getattr(independent_supervisor, "tick", None)
    if callable(tick):
        try:
            tick()
        except Exception as exc:
            logger.critical(
                "[Shutdown] Could not drain IndependentRiskSupervisor status "
                "before kill/flatten verification: "
                f"{type(exc).__name__}:{exc}"
            )

    if risk_controller is None or not bool(
        getattr(risk_controller, "kill_switch_triggered", False)
    ):
        return True

    verify_interval = max(
        0.05,
        float(getattr(risk_controller, "kill_verify_interval_sec", 1.0) or 1.0),
    )
    timeout_sec = max(
        verify_interval,
        float(getattr(risk_controller, "kill_verify_timeout_sec", 30.0) or 30.0),
    )
    heartbeat_interval = min(
        0.25,
        max(
            0.01,
            float(
                getattr(
                    independent_supervisor,
                    "heartbeat_interval_sec",
                    0.25,
                )
                or 0.25
            ),
        ),
    )
    pulse = getattr(independent_supervisor, "pulse_parent_heartbeat", None)
    deadline = time.perf_counter() + timeout_sec + verify_interval

    while True:
        kill_state = str(
            getattr(risk_controller, "kill_state", "") or ""
        ).upper()
        if kill_state == "FLAT_VERIFIED":
            return True
        if kill_state == "FAILED":
            return False

        remaining = deadline - time.perf_counter()
        if remaining <= 0.0:
            logger.critical(
                "[Shutdown] Kill/flatten verification did not reach a terminal "
                f"state: state={kill_state or 'UNKNOWN'}"
            )
            return False

        if callable(pulse):
            try:
                if not pulse():
                    logger.critical(
                        "[Shutdown] IndependentRiskSupervisor stopped before "
                        "kill/flatten verification completed"
                    )
            except Exception as exc:
                logger.critical(
                    "[Shutdown] IndependentRiskSupervisor heartbeat failed "
                    "during kill/flatten verification: "
                    f"{type(exc).__name__}:{exc}"
                )
        time.sleep(min(heartbeat_interval, remaining))


def requires_live_canary_shutdown_truth(runtime: dict) -> bool:
    """Return whether shutdown must prove venue cancellation and flatness."""
    config = runtime.get("config", {}) or {}
    execution = config.get("execution", {}) or {}
    live_launch = config.get("live_launch", {}) or {}
    stage = str(live_launch.get("stage", "") or "").strip().lower()
    return bool(
        not runtime.get("paper_trade", False)
        and str(execution.get("mode", "") or "").strip().lower() == "live"
        and stage in {"canary", "rpi_calibration_canary"}
    )


def verify_account_flat(
    snapshot_provider,
    *,
    required_flat_snapshots: int = 2,
    settle_interval_sec: float = 0.25,
) -> bool:
    """Prove flatness from consecutive snapshots on the independent truth plane."""
    query = getattr(snapshot_provider, "get_all_positions", None)
    if not callable(query):
        return False

    required = max(2, int(required_flat_snapshots or 2))
    settle = max(0.01, float(settle_interval_sec or 0.25))
    for snapshot_index in range(required):
        positions = (
            query(emergency=True)
            if bool(
                getattr(
                    snapshot_provider,
                    "supports_emergency_query_priority",
                    False,
                )
            )
            else query()
        )
        if not isinstance(positions, (list, tuple)):
            return False
        for position in positions:
            if not isinstance(position, dict) or "positionAmt" not in position:
                return False
            try:
                amount = Decimal(str(position["positionAmt"]))
            except (InvalidOperation, TypeError, ValueError):
                return False
            if not amount.is_finite() or amount != Decimal(0):
                return False
        if snapshot_index + 1 < required:
            time.sleep(settle)
    return True


def synchronize_commission_config(gateway, config, symbols):
    """Load account-specific execution fees before strategy activation."""
    get_rate = getattr(gateway, "get_commission_rate", None)
    if not callable(get_rate):
        raise RuntimeError("Gateway does not expose commission-rate truth")

    maker_rates = []
    taker_rates = []
    rpi_rates = {}
    require_zero_rpi = requires_zero_rpi_commission(config)
    for raw_symbol in symbols or []:
        symbol = str(raw_symbol or "").upper()
        payload = get_rate(symbol)
        try:
            parsed = parse_commission_rate_payload(
                payload,
                symbol=symbol,
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

        if (
            require_zero_rpi
            and parsed["rpiCommissionRate"] != Decimal(0)
        ):
            raise RuntimeError(
                "Account-specific rpiCommissionRate must be exactly zero "
                f"for {symbol}, got {parsed['rpiCommissionRate']}"
            )
        maker_rates.append(float(parsed["makerCommissionRate"]))
        taker_rates.append(float(parsed["takerCommissionRate"]))
        rpi_rates[symbol] = float(parsed["rpiCommissionRate"])

    if not maker_rates:
        raise RuntimeError("No configured symbols for commission-rate sync")

    fee_config = config.setdefault("backtest", {})
    fee_config["maker_fee"] = max(maker_rates)
    fee_config["taker_fee"] = max(taker_rates)
    fee_config["rpi_commission_rate"] = max(rpi_rates.values())
    fee_config["rpi_commission_rates"] = rpi_rates
    logger.info(
        "[Fees] Account commission truth synchronized for "
        f"{len(rpi_rates)} symbols; conservative maker/taker maxima applied."
    )
    return {
        "maker_fee": fee_config["maker_fee"],
        "taker_fee": fee_config["taker_fee"],
        "rpi_commission_rates": dict(rpi_rates),
    }


def build_gateway_bundle(engine, config, market_data_config):
    """Build mutually exclusive live or paper exchange capabilities."""
    if is_paper_trade(config):
        from gateway.binance.paper_gateway import (
            BinancePaperGateway,
            PaperTruthSnapshotProvider,
        )

        paper_config = apply_paper_trade_mode(config)
        paper_market_data_config = paper_config.get("system", {}).get(
            "market_data",
            market_data_config,
        )
        gateway = BinancePaperGateway(
            engine,
            paper_config,
            paper_market_data_config,
        )
        return gateway, PaperTruthSnapshotProvider(gateway)

    rate_limit_config = dict(
        config.get("system", {}).get("binance_rest_rate_limit", {}) or {}
    )
    if rate_limit_config.get("enabled", True) is not True:
        raise RuntimeError(
            "Live Binance REST rate-limit coordination cannot be disabled"
        )
    rate_limit_budget = BinanceRateLimitBudget.from_config(rate_limit_config)
    full_open_orders_audit_interval_sec = float(
        rate_limit_config.get(
            "full_open_orders_audit_interval_sec",
            60.0,
        )
        or 60.0
    )

    gateway = BinanceGateway(
        engine,
        config["api_key"],
        config["api_secret"],
        testnet=config["testnet"],
        market_data_config=market_data_config,
        rate_limit_budget=rate_limit_budget,
    )
    truth_provider = BinanceTruthSnapshotProvider(
        config["api_key"],
        config["api_secret"],
        testnet=config["testnet"],
        rate_limit_budget=rate_limit_budget,
        symbols=config.get("symbols", ()),
        full_open_orders_audit_interval_sec=(
            full_open_orders_audit_interval_sec
        ),
    )
    return gateway, truth_provider


def read_clock_health(clock_service):
    """Read clock telemetry without dispatching health listeners."""
    health_reader = getattr(clock_service, "health_snapshot", None)
    if not callable(health_reader):
        return {}
    try:
        health = health_reader(notify_listeners=False)
    except TypeError:
        health = health_reader()
    return health if isinstance(health, dict) else {}


def start_local_dashboard(
    web_dashboard,
    web_dashboard_config,
    *,
    required: bool = False,
):
    """Bind the local dashboard, print its URL, and optionally open it."""
    if web_dashboard is None:
        if required:
            raise RuntimeError("Live canary requires the local web dashboard")
        return ""
    try:
        dashboard_url = web_dashboard.start()
    except OSError as exc:
        message = f"Local dashboard failed to start: {exc}"
        logger.error(message)
        print(message, flush=True)
        if required:
            raise RuntimeError(message) from exc
        return ""

    if not str(dashboard_url or "").strip():
        message = "Local dashboard failed to return a listening URL"
        logger.error(message)
        print(message, flush=True)
        if required:
            raise RuntimeError(message)
        return ""

    logger.info(f"Local dashboard ready: {dashboard_url}")
    print(f"ChronosHFT dashboard: {dashboard_url}", flush=True)
    if bool(web_dashboard_config.get("open_browser", False)):
        try:
            browser_opened = webbrowser.open(dashboard_url)
        except (OSError, webbrowser.Error) as exc:
            browser_opened = False
            logger.warning(f"Could not open the dashboard browser: {exc}")
        if not browser_opened:
            print(
                f"Browser did not open automatically. Open {dashboard_url} manually.",
                flush=True,
            )
    return dashboard_url


def run_startup_blocked_dashboard(
    config,
    clock_health,
    clock_service=None,
    systemd_watchdog=None,
):
    """Keep read-only diagnostics available after the HFT clock gate fails."""
    clock_service = clock_service or time_service
    web_config = config.get("system", {}).get("web_dashboard", {}) or {}
    state = str(clock_health.get("state", "unsynchronized") or "unsynchronized")
    reason = str(
        clock_health.get("reason", "exchange clock calibration failed")
        or "exchange clock calibration failed"
    )
    message = (
        "HFT clock startup gate rejected execution: "
        f"state={state} reason={reason}"
    )
    logger.critical(message)
    print(f"STARTUP_BLOCKED / OBSERVE_ONLY: {message}", flush=True)

    web_dashboard = None
    try:
        if not bool(web_config.get("enabled", True)):
            print(
                "The local dashboard is disabled; no diagnostic service was started.",
                flush=True,
            )
            return False

        try:
            web_dashboard = LocalWebDashboard(
                config=config,
                time_service=clock_service,
            )
        except (OSError, ValueError) as exc:
            print(f"Local dashboard initialization failed: {exc}", flush=True)
            return False

        web_dashboard.set_startup_status(
            state="STARTUP_BLOCKED",
            operating_mode="OBSERVE_ONLY",
            startup_blocked=True,
            execution_enabled=False,
            restart_required=True,
            reason=reason,
        )
        web_dashboard.update_system_health(
            {
                "state": "STARTUP_BLOCKED",
                "severity": "HALT",
                "source": "hft_clock_startup_gate",
                "operating_mode": "OBSERVE_ONLY",
                "execution_enabled": False,
                "restart_required": True,
                "reason": reason,
            }
        )
        web_dashboard.add_log(f"[CRITICAL] {message}")
        logger.set_ui_callback(web_dashboard.add_log)
        dashboard_url = start_local_dashboard(web_dashboard, web_config)
        if not dashboard_url:
            return False

        print(
            "Trading components were not started. Clock recovery is telemetry-only; "
            "restart the process to pass the startup gate. Press Ctrl+C to stop.",
            flush=True,
        )
        refresh_sec = max(
            0.10,
            float(web_config.get("refresh_interval_ms", 1000) or 1000) / 1000.0,
        )
        try:
            while True:
                if systemd_watchdog is not None:
                    systemd_watchdog.pulse()
                web_dashboard.publish_snapshot(force=True)
                time.sleep(refresh_sec)
        except KeyboardInterrupt:
            logger.info("Shutdown signal received in startup diagnostics mode.")
        return True
    finally:
        logger.set_ui_callback(None)
        if web_dashboard is not None:
            web_dashboard.publish_snapshot(force=True)
            web_dashboard.stop()
        clock_service.stop()


def start_external_alert_service(config, runtime, *, paper_trade: bool):
    service = ExternalAlertService.from_config(config)
    if service is None:
        if not paper_trade:
            raise RuntimeError(
                "Live external alert service is not configured"
            )
        return None

    runtime["external_alerts"] = service
    if not service.start():
        raise RuntimeError("External alert worker failed to start")
    logger.set_alert_callback(service.enqueue_log)

    alert_config = config.get("alert", {}) or {}
    startup_probe_required = bool(
        alert_config.get("startup_probe_required", not paper_trade)
    )
    if (
        startup_probe_required
        and not service.probe_startup(
            timeout_sec=alert_config.get("startup_probe_timeout_sec")
        )
    ):
        raise RuntimeError(
            "External alert startup delivery probe failed"
        )
    return service


def external_alert_health(service) -> dict:
    if service is None:
        return {
            "available": False,
            "enabled": False,
            "healthy": True,
            "reason": "disabled",
        }
    try:
        snapshot = service.get_health_snapshot()
    except Exception:
        return {
            "available": False,
            "enabled": True,
            "healthy": False,
            "reason": "health_snapshot_failed",
        }
    if not isinstance(snapshot, dict):
        return {
            "available": False,
            "enabled": True,
            "healthy": False,
            "reason": "health_snapshot_invalid",
        }
    return snapshot


def enforce_external_alert_health(
    service,
    oms_system,
    constraint_state: dict,
) -> bool:
    """Apply alert health to OMS only from the main control loop."""
    snapshot = external_alert_health(service)
    if service is None or bool(snapshot.get("healthy", False)):
        if constraint_state.get("active", False):
            try:
                oms_system.clear_trading_mode(
                    reason="external alert channel recovered",
                    prefixes=("external_alerts:",),
                )
            except Exception:
                oms_system.halt_system(
                    "external_alerts_health_guard_clear_failed"
                )
                return False
            constraint_state["active"] = bool(
                oms_system.has_trading_mode_constraint(
                    ("external_alerts:",)
                )
            )
        return not constraint_state.get("active", False)

    reason = str(snapshot.get("reason", "unhealthy") or "unhealthy")
    constraint_reason = f"external_alerts:{reason}"
    if (
        not constraint_state.get("active", False)
        or constraint_state.get("reason") != constraint_reason
    ):
        try:
            oms_system.set_trading_mode(
                OMSCapabilityMode.REDUCE_ONLY,
                constraint_reason,
            )
        except Exception:
            oms_system.halt_system(
                "external_alerts_health_guard_set_failed"
            )
            return False
        constraint_state["active"] = True
        constraint_state["reason"] = constraint_reason
    return False


def stop_external_alert_service(runtime) -> bool:
    service = runtime.get("external_alerts") if runtime else None
    if service is None:
        return True
    logger.flush(timeout_sec=2.0)
    logger.set_alert_callback(None)
    try:
        return bool(service.stop())
    except Exception:
        return False


def live_evidence_health(recorder) -> dict:
    if recorder is None:
        return {
            "available": False,
            "enabled": False,
            "healthy": False,
            "failure_reason": "not_configured",
        }
    try:
        snapshot = recorder.health_snapshot()
    except Exception:
        return {
            "available": False,
            "enabled": True,
            "healthy": False,
            "failure_reason": "health_snapshot_failed",
        }
    if not isinstance(snapshot, dict):
        return {
            "available": False,
            "enabled": True,
            "healthy": False,
            "failure_reason": "health_snapshot_invalid",
        }
    return {"available": True, **snapshot}


def enforce_live_evidence_health(recorder, oms_system) -> bool:
    """Freeze new risk when the immutable Live evidence stream is unavailable."""
    snapshot = live_evidence_health(recorder)
    if bool(snapshot.get("healthy", False)):
        return True

    reason = str(
        snapshot.get("failure_reason", "unhealthy") or "unhealthy"
    )
    state = getattr(getattr(oms_system, "state", None), "value", "")
    normalized_state = str(state or "").upper()
    last_freeze_reason = str(
        getattr(oms_system, "last_freeze_reason", "") or ""
    )
    already_restricted = normalized_state in {
        "HALTED",
        "SHUTTING_DOWN",
        "STOPPED",
    }
    evidence_frozen = (
        normalized_state == "FROZEN"
        and last_freeze_reason.startswith("LiveEvidence:")
    )
    if not already_restricted and not evidence_frozen:
        oms_system.freeze_system(
            f"LiveEvidence: {reason}",
            cancel_active_orders=True,
        )
    return False


def build_runtime_application(runtime=None) -> RuntimeApplication:
    return RuntimeApplication(
        runtime,
        RuntimeApplicationServices(
            configuration=RuntimeConfigurationServices(
                parse_cli_args=parse_cli_args,
                load_config=load_config,
                load_admin_control_config=load_admin_control_config,
                submit_admin_command=submit_admin_command,
                is_paper_trade=is_paper_trade,
                apply_paper_trade_mode=apply_paper_trade_mode,
            ),
            platform=RuntimePlatformServices(
                logger=logger,
                systemd_watchdog_type=SystemdWatchdog,
                start_external_alert_service=start_external_alert_service,
                time_service=time_service,
                read_clock_health=read_clock_health,
                run_startup_blocked_dashboard=(
                    run_startup_blocked_dashboard
                ),
                monotonic=time.perf_counter,
                sleep=time.sleep,
            ),
            factories=RuntimeFactoryServices(
                event_engine_type=EventEngine,
                build_gateway_bundle=build_gateway_bundle,
                oms_type=OMS,
                risk_manager_type=RiskManager,
                failure_policy_type=RuntimeFailurePolicy,
                independent_risk_supervisor_type=(
                    IndependentRiskSupervisor
                ),
                create_primary_strategy=create_primary_strategy,
                strategy_runtime_type=StrategyRuntime,
                data_recorder_type=DataRecorder,
                process_resource_monitor_type=ProcessResourceMonitor,
                live_evidence_recorder_type=LiveEvidenceRecorder,
                recorder_group_type=RecorderGroup,
                truth_monitor_type=TruthMonitor,
                venue_supervisor_type=VenueSupervisor,
                admin_control_server_type=AdminControlServer,
                local_web_dashboard_type=LocalWebDashboard,
                ref_data_manager=ref_data_manager,
                watchdog_state_type=RuntimeWatchdogState,
                event_bindings_type=RuntimeEventBindings,
            ),
            safety=RuntimeSafetyServices(
                start_local_dashboard=start_local_dashboard,
                connect_gateway_with_risk_heartbeat=(
                    connect_gateway_with_risk_heartbeat
                ),
                synchronize_commission_config=(
                    synchronize_commission_config
                ),
                validate_live_rpi_policy=validate_live_rpi_policy,
                run_live_risk_checks=run_live_risk_checks,
                wait_for_initial_truth_snapshot=(
                    wait_for_initial_truth_snapshot
                ),
                validate_live_account_equity_truth=(
                    validate_live_account_equity_truth
                ),
                validate_live_flat_start_truth=(
                    validate_live_flat_start_truth
                ),
                external_alert_health=external_alert_health,
                enforce_live_evidence_health=(
                    enforce_live_evidence_health
                ),
                wait_for_initial_market_data_readiness=(
                    wait_for_initial_market_data_readiness
                ),
                bootstrap_or_rearm=bootstrap_or_rearm,
                enforce_external_alert_health=(
                    enforce_external_alert_health
                ),
                live_evidence_health=live_evidence_health,
            ),
            loop=RuntimeLoopServices(
                runtime_control_loop_type=RuntimeControlLoop,
                runtime_control_services_type=RuntimeControlServices,
                build_runtime_resource_event=build_runtime_resource_event,
            ),
        ),
    )


# P0 TODO: Replace the host-local OMS file lock with replicated leader election
# and a monotonic fencing token before supporting multi-host active/passive OMS.
def _run_main(argv=None, runtime=None):
    return build_runtime_application(runtime).run(argv)


def build_runtime_shutdown_services() -> RuntimeShutdownServices:
    return RuntimeShutdownServices(
        logger=logger,
        call_with_risk_heartbeat=call_with_risk_heartbeat,
        verify_account_flat=verify_account_flat,
        wait_for_kill_flatten_verification=(
            wait_for_kill_flatten_verification
        ),
        requires_live_canary_shutdown_truth=(
            requires_live_canary_shutdown_truth
        ),
    )


def shutdown_runtime(runtime, reason: str = "main_exit") -> bool:
    return RuntimeShutdownCoordinator.execute(
        runtime,
        reason,
        build_runtime_shutdown_services(),
    )


def shutdown_runtime_until_terminal(
    runtime,
    reason: str = "main_exit",
) -> bool:
    """Drive retryable shutdown phases to a bounded terminal outcome."""
    if not runtime:
        return True
    shutdown_config = (
        (runtime.get("config", {}) or {})
        .get("system", {})
        .get("shutdown", {})
        or {}
    )
    max_attempts = max(
        1,
        int(shutdown_config.get("max_attempts", 3) or 1),
    )
    retry_interval_sec = max(
        0.0,
        min(
            5.0,
            float(
                shutdown_config.get("retry_interval_sec", 0.5)
                or 0.0
            ),
        ),
    )
    supervisor = runtime.get("risk_supervisor")
    pulse = getattr(supervisor, "pulse_parent_heartbeat", None)

    for attempt in range(1, max_attempts + 1):
        runtime["_shutdown_attempts"] = attempt
        verified = shutdown_runtime(runtime, reason)
        if verified:
            return True
        if runtime.get("_shutdown_complete"):
            return False
        if not runtime.get("_shutdown_retryable", False):
            return False
        if attempt >= max_attempts:
            break

        logger.warning(
            "[Shutdown] Retrying incomplete teardown "
            f"attempt={attempt + 1}/{max_attempts}"
        )
        deadline = time.perf_counter() + retry_interval_sec
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0.0:
                break
            if callable(pulse):
                try:
                    pulse()
                except Exception as exc:
                    logger.error(
                        "[Shutdown] Sidecar heartbeat failed between "
                        f"retries: {type(exc).__name__}:{exc}"
                    )
            time.sleep(min(0.05, remaining))

    runtime["_shutdown_verified"] = False
    logger.critical(
        "[Shutdown] Bounded retries exhausted; teardown remains unverified"
    )
    return False


def main(argv=None):
    runtime = RuntimeResources()
    shutdown_reason = "main_return"
    result = None
    pending_exception = None
    pending_traceback = None
    try:
        result = _run_main(argv, runtime)
    except KeyboardInterrupt:
        shutdown_reason = "keyboard_interrupt"
        logger.info("Shutdown signal received.")
    except BaseException as exc:
        shutdown_reason = f"fatal:{type(exc).__name__}:{exc}"
        logger.critical(f"ChronosHFT fatal error: {type(exc).__name__}:{exc}")
        print(f"ChronosHFT fatal error: {type(exc).__name__}: {exc}", flush=True)
        pending_exception = exc
        pending_traceback = exc.__traceback__

    shutdown_verified = shutdown_runtime_until_terminal(
        runtime,
        shutdown_reason,
    )
    external_alerts_stopped = stop_external_alert_service(runtime)
    logger.flush(timeout_sec=2.0)
    if pending_exception is not None:
        raise pending_exception.with_traceback(pending_traceback)
    if runtime and (not shutdown_verified or not external_alerts_stopped):
        print(
            "ChronosHFT shutdown or external alert drain was not verified; "
            "see local durable state before restarting.",
            flush=True,
        )
        return 1
    return result


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main_result = main()
    raise SystemExit(main_result if type(main_result) is int else 0)
