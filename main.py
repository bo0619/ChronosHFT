import argparse
import multiprocessing
import threading
import time
import webbrowser
from decimal import Decimal, InvalidOperation

from data.cache import data_cache
from data.live_evidence import LiveEvidenceRecorder, RecorderGroup
from data.recorder import DataRecorder
from data.ref_data import ref_data_manager
from event.engine import EventEngine
from event.type import (
    EVENT_ACCOUNT_UPDATE,
    EVENT_AGG_TRADE,
    EVENT_ALERT,
    EVENT_API_LIMIT,
    EVENT_EXCHANGE_ACCOUNT_UPDATE,
    EVENT_EXCHANGE_ORDER_UPDATE,
    EVENT_LOG,
    EVENT_MARK_PRICE,
    EVENT_ORDERBOOK,
    EVENT_ORDER_UPDATE,
    EVENT_POSITION_UPDATE,
    EVENT_STRATEGY_UPDATE,
    EVENT_SYSTEM_HEALTH,
    EVENT_TRADE_UPDATE,
    OMSCapabilityMode,
)
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
from infrastructure.rpi_policy import (
    requires_zero_rpi_commission,
    validate_live_rpi_policy,
)
from infrastructure.system_health import handle_system_health_event
from infrastructure.time_service import time_service
from infrastructure.truth_monitor import TruthMonitor
from infrastructure.venue_supervisor import VenueSupervisor
from infrastructure.watchdog import (
    emit_event_engine_backlog_if_needed,
    emit_market_data_stale_if_needed,
    emit_strategy_runtime_backlog_if_needed,
)
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
    while not completed.wait(interval):
        if not pulse():
            raise RuntimeError(
                "IndependentRiskSupervisor stopped during gateway startup"
            )
    worker.join(timeout=0.0)

    error = outcome.get("error")
    if error is not None:
        raise error
    if not pulse():
        raise RuntimeError(
            "IndependentRiskSupervisor stopped as gateway startup completed"
        )
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


def run_startup_blocked_dashboard(config, clock_health, clock_service=None):
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


# P0 TODO: Replace the host-local OMS file lock with replicated leader election
# and a monotonic fencing token before supporting multi-host active/passive OMS.
def _run_main(argv=None, runtime=None):
    runtime = runtime if runtime is not None else {}
    args = parse_cli_args(argv)
    if args.admin_command:
        admin_config = load_admin_control_config(args.config)
        result = submit_admin_command(
            action=args.admin_command,
            reason=str(args.admin_reason or "operator_ack"),
            config=admin_config,
            wait_timeout_sec=float(args.admin_timeout or 5.0),
        )
        snapshot = result.get("snapshot", {}) or {}
        print(
            f"admin_command={args.admin_command} accepted={result.get('accepted')} "
            f"status={result.get('status')} message={result.get('message')}"
        )
        if snapshot:
            print(
                "snapshot="
                f"state={snapshot.get('state')} "
                f"mode={snapshot.get('capability_mode')} "
                f"manual_rearm_required={snapshot.get('manual_rearm_required')} "
                f"halt_reason={snapshot.get('last_halt_reason')}"
            )
        return 0 if bool(result.get("accepted", False)) else 2

    config = load_config(args.config)

    paper_trade = is_paper_trade(config)
    if paper_trade:
        config = apply_paper_trade_mode(config)

    if args.check_config:
        strategy = config.get("strategy", {}) or {}
        mode = "paper" if paper_trade else "live"
        print(
            "CONFIG_OK "
            f"mode={mode} "
            f"symbols={len(config.get('symbols', []) or [])} "
            f"primary_model={strategy.get('primary_model', '')}",
            flush=True,
        )
        return 0

    missing_credentials = [
        field
        for field in ("api_key", "api_secret")
        if not str(config.get(field, "") or "").strip()
    ]
    if missing_credentials and not paper_trade and not args.admin_command:
        env_names = [
            str(config.get(f"{field}_env", "") or "")
            for field in missing_credentials
        ]
        print(
            "Error: missing Binance credentials. Set environment variables: "
            + ", ".join(name for name in env_names if name)
        )
        return 2

    logger.init_logging(config)
    logger.set_ui_callback(None)
    logger.set_alert_callback(None)
    runtime.update(
        {
            "config": config,
            "paper_trade": paper_trade,
            "_account_shutdown_proof_required": False,
            "_risk_supervisor_started": False,
        }
    )
    external_alerts = start_external_alert_service(
        config,
        runtime,
        paper_trade=paper_trade,
    )

    time_service.clear_listeners()
    time_sync_config = config.get("system", {}).get("time_sync", {}) or {}
    time_service.configure(time_sync_config)
    initial_clock_sync_ok = time_service.start(testnet=config["testnet"])
    clock_startup_required = bool(time_sync_config.get("startup_required", True))
    if clock_startup_required and not (
        initial_clock_sync_ok and time_service.is_ready()
    ):
        diagnostics_available = run_startup_blocked_dashboard(
            config,
            read_clock_health(time_service),
            clock_service=time_service,
        )
        return 0 if diagnostics_available else 2

    runtime["time_service"] = time_service

    event_engine_config = config.get("system", {}).get("event_engine", {})
    engine = EventEngine(event_engine_config)
    runtime["event_engine_config"] = event_engine_config
    runtime["engine"] = engine

    market_data_config = config.get("system", {}).get("market_data", {})
    gateway, truth_provider = build_gateway_bundle(
        engine,
        config,
        market_data_config,
    )
    runtime["gateway"] = gateway
    runtime["truth_provider"] = truth_provider
    gateway.require_healthy_clock = bool(
        (config.get("system", {}).get("time_sync", {}) or {}).get(
            "require_healthy_for_trading",
            True,
        )
    )
    gateway_rest = getattr(gateway, "rest", None)
    if hasattr(gateway_rest, "order_clock_guard"):
        gateway_rest.order_clock_guard = (
            gateway._clock_health_guard
            if gateway.require_healthy_clock
            else None
        )
    oms_system = OMS(engine, gateway, config)
    runtime["oms"] = oms_system
    risk_controller = RiskManager(engine, config, oms=oms_system, gateway=gateway)
    runtime["risk_controller"] = risk_controller

    clock_runtime_armed = {"value": False}

    def on_time_service_health(severity, reason, details):
        if severity == "freeze":
            oms_system.freeze_system(f"TimeSync: {reason}", cancel_active_orders=True)
            return
        if severity == "halt":
            if clock_runtime_armed["value"]:
                risk_controller.trigger_kill_switch(f"TimeSync: {reason}")
            else:
                oms_system.halt_system(f"TimeSync startup: {reason}")
            return
        if severity == "recovered" and oms_system.state.value == "FROZEN":
            if oms_system.last_freeze_reason.startswith("TimeSync:"):
                oms_system.trigger_reconcile("Time sync recovered")

    time_service.register_listener(on_time_service_health)
    startup_clock_health = read_clock_health(time_service)
    if not bool(startup_clock_health.get("ready", False)):
        oms_system.halt_system("time_sync_unhealthy_during_startup")
        raise RuntimeError(
            "Exchange clock became unhealthy while runtime components "
            "were being constructed"
        )

    risk_supervisor = IndependentRiskSupervisor(
        oms_system,
        config,
        risk_manager=risk_controller,
    )
    runtime["risk_supervisor"] = risk_supervisor
    if paper_trade and risk_supervisor.enabled:
        raise RuntimeError("Paper Trade must not enable IndependentRiskSupervisor")
    strategy = create_primary_strategy(
        engine,
        oms_system,
        config,
    )
    runtime["strategy"] = strategy
    logger.info(
        "[StrategyRegistry] "
        f"primary={strategy.model_key} strategy_id={strategy.name} "
        f"registered={','.join(strategy.registered_models)} "
        "execution_policy=single_primary"
    )
    def fail_closed_runtime(
        *,
        scope: str,
        reason: str,
        freeze_action,
    ) -> None:
        try:
            freeze_action()
        except BaseException as exc:
            logger.critical(
                f"[{scope}] Fail-closed freeze raised: "
                f"{type(exc).__name__}:{exc}; escalating kill-switch"
            )
            risk_controller.trigger_kill_switch(
                f"{reason}:freeze_failed:{type(exc).__name__}"
            )

    def on_event_engine_failure(failure: dict) -> None:
        lane = str(failure.get("lane", "") or "unknown")
        event_type = str(failure.get("event_type", "") or "unknown")
        failure_kind = str(failure.get("kind", "") or "unknown")
        handler_name = str(
            failure.get("handler_name", "") or "unavailable"
        )
        reason = (
            f"event_engine_failure:{failure_kind}:{lane}:"
            f"{event_type}:{handler_name}"
        )
        if lane == "cold":
            fail_closed_runtime(
                scope="EventEngine",
                reason=reason,
                freeze_action=lambda: oms_system.freeze_strategy(
                    strategy.name,
                    reason,
                    cancel_active_orders=True,
                ),
            )
            return
        fail_closed_runtime(
            scope="EventEngine",
            reason=reason,
            freeze_action=lambda: oms_system.freeze_venue(
                getattr(gateway, "gateway_name", "UNKNOWN"),
                reason,
                cancel_active_orders=True,
            ),
        )

    def on_strategy_runtime_failure(failure: dict) -> None:
        reason = (
            "strategy_runtime_failure:"
            f"{failure.get('phase', 'unknown')}:"
            f"{failure.get('kind', 'unknown')}:"
            f"{failure.get('handler_name', 'unavailable')}"
        )
        fail_closed_runtime(
            scope="StrategyRuntime",
            reason=reason,
            freeze_action=lambda: oms_system.freeze_strategy(
                strategy.name,
                reason,
                cancel_active_orders=True,
            ),
        )

    engine.set_failure_handler(on_event_engine_failure)
    strategy_runtime = StrategyRuntime(
        strategy,
        config.get("system", {}).get("strategy_runtime", {}),
        start_thread=False,
        failure_callback=on_strategy_runtime_failure,
    )
    runtime["strategy_runtime"] = strategy_runtime
    data_recorder = (
        DataRecorder(engine, config["symbols"])
        if config.get("record_data", False)
        else None
    )
    runtime["data_recorder"] = data_recorder
    live_evidence_recorder = None
    if not paper_trade:
        journal_snapshot = getattr(
            getattr(oms_system, "journal", None),
            "health_snapshot",
            None,
        )
        if not callable(journal_snapshot):
            raise RuntimeError(
                "OMS journal health snapshot is required for Live evidence"
            )

        def on_live_evidence_failure(reason):
            oms_system.freeze_system(
                f"LiveEvidence: {reason}",
                cancel_active_orders=True,
            )

        live_evidence_recorder = LiveEvidenceRecorder(
            engine,
            config,
            failure_callback=on_live_evidence_failure,
            oms_journal_snapshot=journal_snapshot,
        )
    runtime["live_evidence_recorder"] = live_evidence_recorder
    recorder = (
        RecorderGroup(data_recorder, live_evidence_recorder)
        if live_evidence_recorder is not None
        else data_recorder
    )
    runtime["recorder"] = recorder
    truth_monitor = TruthMonitor(oms_system, truth_provider, config, start_thread=False)
    runtime["truth_monitor"] = truth_monitor
    venue_supervisor = VenueSupervisor(oms_system, gateway, config, start_thread=False)
    runtime["venue_supervisor"] = venue_supervisor
    admin_control = AdminControlServer(
        oms_system,
        config,
        risk_manager=risk_controller,
        risk_supervisor=risk_supervisor,
    )
    runtime["admin_control"] = admin_control
    web_dashboard_config = config.get("system", {}).get("web_dashboard", {}) or {}
    web_dashboard = None
    if bool(web_dashboard_config.get("enabled", True)):
        web_dashboard = LocalWebDashboard(
            oms=oms_system,
            gateway=gateway,
            risk_manager=risk_controller,
            risk_supervisor=risk_supervisor,
            config=config,
            time_service=time_service,
            truth_monitor=truth_monitor,
            venue_supervisor=venue_supervisor,
            event_engine=engine,
            strategy_runtime=strategy_runtime,
        )
        runtime["web_dashboard"] = web_dashboard

        logger.set_ui_callback(web_dashboard.add_log)

    ref_data_manager.init(testnet=config["testnet"])

    register_market = getattr(engine, "register_market", None)
    if not callable(register_market):
        register_market = getattr(engine, "register_hot", engine.register)
    register_execution = getattr(engine, "register_execution", None)
    if not callable(register_execution):
        register_execution = getattr(engine, "register_hot", engine.register)
    register_cold = getattr(engine, "register_cold", engine.register)

    register_market(EVENT_ORDERBOOK, lambda e: data_cache.update_book(e.data))
    register_market(EVENT_MARK_PRICE, lambda e: data_cache.update_mark_price(e.data))
    register_market(EVENT_AGG_TRADE, lambda e: data_cache.update_trade(e.data))

    main.last_tick_time = time.perf_counter()
    main.stale_watchdog_triggered = False
    main.event_engine_watchdog_state = {}
    main.strategy_runtime_watchdog_state = {}

    def on_hot_tick(_event):
        main.last_tick_time = time.perf_counter()
        main.stale_watchdog_triggered = False

    register_market(EVENT_ORDERBOOK, on_hot_tick)
    register_execution(EVENT_EXCHANGE_ORDER_UPDATE, oms_system.on_exchange_update)
    register_execution(EVENT_EXCHANGE_ACCOUNT_UPDATE, oms_system.on_exchange_account_update)
    register_execution(
        EVENT_SYSTEM_HEALTH,
        lambda e: handle_system_health_event(
            e,
            risk_controller,
            oms_system,
            risk_supervisor,
        ),
    )

    def on_orderbook_cold(event):
        strategy_runtime.on_orderbook(event.data)
        if web_dashboard is not None:
            web_dashboard.update_market(event.data)

    def on_market_trade_cold(event):
        strategy_runtime.on_market_trade(event.data)
        if web_dashboard is not None:
            web_dashboard.update_market_trade(event.data)

    def on_order_cold(event):
        strategy_runtime.on_order(event.data)
        if web_dashboard is not None:
            web_dashboard.update_order(event.data)

    def on_trade_cold(event):
        strategy_runtime.on_trade(event.data)
        if web_dashboard is not None:
            web_dashboard.update_trade(event.data)

    def on_position_cold(event):
        strategy_runtime.on_position(event.data)
        if web_dashboard is not None:
            web_dashboard.update_position(event.data)

    def on_account_cold(event):
        strategy_runtime.on_account_update(event.data)
        if web_dashboard is not None:
            web_dashboard.update_account(event.data)

    def on_strategy_cold(event):
        if web_dashboard is not None:
            web_dashboard.update_strategy(event.data)

    def on_system_health_cold(event):
        strategy_runtime.on_system_health(event.data)
        if web_dashboard is not None:
            web_dashboard.update_system_health(event.data)

    def on_alert_cold(event):
        if web_dashboard is not None:
            web_dashboard.update_alert(event.data)
        if external_alerts is not None:
            external_alerts.enqueue_event(event.data)

    register_cold(EVENT_ORDERBOOK, on_orderbook_cold)
    register_cold(
        EVENT_MARK_PRICE,
        lambda e: web_dashboard.update_mark_price(e.data) if web_dashboard else None,
    )
    register_cold(EVENT_AGG_TRADE, on_market_trade_cold)
    register_cold(
        EVENT_EXCHANGE_ORDER_UPDATE,
        lambda e: web_dashboard.update_exchange_order(e.data) if web_dashboard else None,
    )
    register_cold(EVENT_ORDER_UPDATE, on_order_cold)
    register_cold(EVENT_TRADE_UPDATE, on_trade_cold)
    register_cold(EVENT_POSITION_UPDATE, on_position_cold)
    register_cold(EVENT_ACCOUNT_UPDATE, on_account_cold)
    register_cold(EVENT_STRATEGY_UPDATE, on_strategy_cold)
    register_cold(EVENT_SYSTEM_HEALTH, on_system_health_cold)
    register_cold(
        EVENT_API_LIMIT,
        lambda e: web_dashboard.update_api_limit(e.data) if web_dashboard else None,
    )
    register_cold(EVENT_ALERT, on_alert_cold)
    register_cold(
        EVENT_LOG,
        lambda e: web_dashboard.add_log(e.data) if web_dashboard else None,
    )

    engine.start()
    risk_supervisor_started = False
    try:
        risk_supervisor_started = bool(risk_supervisor.start())
    finally:
        supervisor_process = getattr(risk_supervisor, "process", None)
        supervisor_process_alive = bool(
            supervisor_process is not None
            and supervisor_process.is_alive()
        )
        runtime["_risk_supervisor_started"] = bool(
            risk_supervisor.enabled
            and (risk_supervisor_started or supervisor_process_alive)
        )
    if not risk_supervisor_started:
        raise RuntimeError("IndependentRiskSupervisor failed to start")
    live_launch_stage = str(
        (config.get("live_launch", {}) or {}).get("stage", "") or ""
    ).strip().lower()
    dashboard_required = bool(
        not paper_trade
        and live_launch_stage in {"canary", "rpi_calibration_canary"}
    )
    start_local_dashboard(
        web_dashboard,
        web_dashboard_config,
        required=dashboard_required,
    )
    gateway_connected = connect_gateway_with_risk_heartbeat(
        gateway,
        config["symbols"],
        risk_supervisor,
    )
    if gateway_connected is not True:
        raise RuntimeError("Gateway failed to reach transport-and-book readiness")
    runtime["_account_shutdown_proof_required"] = True
    commission_truth = synchronize_commission_config(
        gateway,
        config,
        config["symbols"],
    )
    if not paper_trade:
        validate_live_rpi_policy(
            config,
            config["symbols"],
            {
                str(symbol or "").upper(): ref_data_manager.supports_rpi(symbol)
                for symbol in config["symbols"]
            },
            commission_truth["rpi_commission_rates"],
        )

    funding_policy = getattr(
        risk_controller,
        "funding_guard_policy",
        None,
    )
    funding_recovery_updates = (
        int(getattr(funding_policy, "recovery_updates", 0) or 0)
        if bool(getattr(funding_policy, "enabled", False))
        else 0
    )
    funding_startup_hold_sec = (
        float(
            getattr(
                funding_policy,
                "post_funding_hold_sec",
                0.0,
            )
            or 0.0
        )
        if bool(getattr(funding_policy, "enabled", False))
        else 0.0
    )
    startup_warmup_sec = max(
        3.0,
        (
            funding_startup_hold_sec
            + 2.0
            + funding_recovery_updates * 1.5
        ),
    )
    warmup_deadline = time.perf_counter() + startup_warmup_sec
    while time.perf_counter() < warmup_deadline:
        run_live_risk_checks(risk_controller, risk_supervisor)
        time.sleep(0.1)
    if not risk_supervisor.wait_until_healthy(timeout_sec=2.0):
        oms_system.halt_system("independent_risk_supervisor_unavailable")
        raise RuntimeError("IndependentRiskSupervisor startup health gate failed")
    if not wait_for_initial_truth_snapshot(truth_monitor, risk_supervisor):
        oms_system.halt_system("initial_truth_snapshot_unhealthy")
        raise RuntimeError("Initial exchange truth snapshot health gate failed")
    if not paper_trade:
        try:
            validate_live_account_equity_truth(
                config,
                truth_monitor.last_account_snapshot,
            )
            validate_live_flat_start_truth(
                truth_monitor.last_positions_snapshot,
                truth_monitor.last_open_orders_snapshot,
            )
        except (TypeError, ValueError) as exc:
            oms_system.halt_system("live_account_start_truth_rejected")
            raise RuntimeError(
                f"Live account start truth rejected: {exc}"
            ) from exc
    if not run_live_risk_checks(risk_controller, risk_supervisor):
        oms_system.halt_system("startup_risk_health_check_failed")
        raise RuntimeError("Startup risk health gate failed before OMS bootstrap")
    if external_alerts is not None and not bool(
        external_alert_health(external_alerts).get("healthy", False)
    ):
        oms_system.halt_system(
            "external_alert_channel_unhealthy_before_bootstrap"
        )
        raise RuntimeError(
            "External alert health gate failed before OMS bootstrap"
        )
    if not paper_trade and not enforce_live_evidence_health(
        live_evidence_recorder,
        oms_system,
    ):
        raise RuntimeError(
            "Live evidence health gate failed before OMS bootstrap"
        )
    final_startup_clock_health = read_clock_health(time_service)
    if not bool(final_startup_clock_health.get("ready", False)):
        oms_system.halt_system("time_sync_unhealthy_before_oms_bootstrap")
        raise RuntimeError(
            "Exchange clock health gate failed immediately before OMS bootstrap"
        )
    if not wait_for_initial_market_data_readiness(
        risk_controller,
        risk_supervisor,
    ):
        oms_system.halt_system("initial_market_data_unready")
        raise RuntimeError(
            "Initial market-data readiness gate failed before OMS bootstrap"
        )
    risk_controller.resume_kill_switch_supervision()
    bootstrap_ready = bootstrap_or_rearm(
        oms_system,
        auto_rearm=bool(args.rearm),
        rearm_reason=str(args.rearm_reason or "cli"),
        risk_manager=risk_controller,
        risk_supervisor=risk_supervisor,
    )
    if not bootstrap_ready:
        raise RuntimeError("OMS bootstrap/rearm did not reach an executable state")
    if not run_live_risk_checks(risk_controller, risk_supervisor):
        oms_system.halt_system("startup_risk_health_check_failed")
        raise RuntimeError("Startup risk health gate failed after OMS bootstrap")
    if external_alerts is not None and not bool(
        external_alert_health(external_alerts).get("healthy", False)
    ):
        oms_system.halt_system(
            "external_alert_channel_unhealthy_after_bootstrap"
        )
        raise RuntimeError(
            "External alert health gate failed after OMS bootstrap"
        )
    if not paper_trade and not enforce_live_evidence_health(
        live_evidence_recorder,
        oms_system,
    ):
        raise RuntimeError(
            "Live evidence health gate failed after OMS bootstrap"
        )
    clock_runtime_armed["value"] = True
    truth_monitor.start()
    venue_supervisor.start()
    strategy_runtime.start()

    if web_dashboard is not None:
        capability_mode = getattr(oms_system, "capability_mode", "READ_ONLY")
        capability_mode = getattr(capability_mode, "value", capability_mode)
        execution_enabled = bool(
            getattr(gateway, "active", False)
            and getattr(strategy_runtime, "_active", False)
        )
        web_dashboard.set_startup_status(
            state="RUNNING" if execution_enabled else "STARTUP_DEGRADED",
            operating_mode=str(capability_mode),
            startup_blocked=False,
            execution_enabled=execution_enabled,
            restart_required=False,
            reason=str(getattr(oms_system, "capability_reason", "") or ""),
        )
        web_dashboard.publish_snapshot(force=True)

    if paper_trade:
        logger.info(
            "ChronosHFT PAPER · LIVE DATA: Binance production public market "
            "data, local simulated execution, private API disabled."
        )
    else:
        logger.info("ChronosHFT Core Engine LIVE · REAL MONEY.")

    external_alert_constraint_state = {}
    while True:
        time.sleep(0.1)
        enforce_external_alert_health(
            external_alerts,
            oms_system,
            external_alert_constraint_state,
        )
        if not paper_trade:
            enforce_live_evidence_health(
                live_evidence_recorder,
                oms_system,
            )
        run_live_risk_checks(risk_controller, risk_supervisor)
        main.stale_watchdog_triggered = emit_market_data_stale_if_needed(
            engine,
            main.last_tick_time,
            main.stale_watchdog_triggered,
        )
        main.event_engine_watchdog_state = emit_event_engine_backlog_if_needed(
            engine,
            oms_system,
            getattr(gateway, "gateway_name", "UNKNOWN"),
            main.event_engine_watchdog_state,
            event_engine_config,
        )
        main.strategy_runtime_watchdog_state = emit_strategy_runtime_backlog_if_needed(
            strategy_runtime,
            oms_system,
            strategy.name,
            main.strategy_runtime_watchdog_state,
            config.get("system", {}).get("strategy_runtime", {}),
        )
        runtime_metrics = {
            "event_engine": engine.get_metrics_snapshot(),
            "event_handlers": engine.get_handler_metrics_snapshot(limit=50),
            "strategy_runtime": strategy_runtime.get_metrics_snapshot(),
            "external_alerts": external_alert_health(external_alerts),
            "live_evidence": live_evidence_health(
                live_evidence_recorder
            ),
        }
        if web_dashboard is not None:
            web_dashboard.update_runtime_metrics(runtime_metrics)
        admin_control.poll_once()


def shutdown_runtime(runtime, reason: str = "main_exit") -> bool:
    """Stop only after a quiesced sidecar and final account-wide proof."""
    if not runtime or runtime.get("_shutdown_complete"):
        return bool(runtime and runtime.get("_shutdown_verified", False))
    if runtime.get("_shutdown_in_progress"):
        return False

    runtime["_shutdown_in_progress"] = True
    reason = str(reason or "main_exit")
    oms_system = runtime.get("oms")
    engine = runtime.get("engine")
    gateway = runtime.get("gateway")
    truth_provider = runtime.get("truth_provider")
    strategy_runtime = runtime.get("strategy_runtime")
    risk_controller = runtime.get("risk_controller")
    risk_supervisor = runtime.get("risk_supervisor")
    truth_monitor = runtime.get("truth_monitor")
    venue_supervisor = runtime.get("venue_supervisor")
    recorder = runtime.get("recorder")
    admin_control = runtime.get("admin_control")
    web_dashboard = runtime.get("web_dashboard")
    clock_service = runtime.get("time_service")
    event_engine_config = runtime.get("event_engine_config", {}) or {}
    shutdown_phase = runtime.setdefault("_shutdown_phase", {})
    proof_marker = runtime.get("_account_shutdown_proof_required")
    account_shutdown_proof_required = bool(
        proof_marker
        if proof_marker is not None
        else oms_system is not None and truth_provider is not None
    )
    live_canary_truth_required = requires_live_canary_shutdown_truth(runtime)
    supervisor_enabled = bool(
        risk_supervisor is not None
        and getattr(risk_supervisor, "enabled", False)
    )
    supervisor_start_marker = runtime.get("_risk_supervisor_started")
    supervisor_process = getattr(risk_supervisor, "process", None)
    supervisor_process_alive = bool(
        supervisor_process is not None
        and supervisor_process.is_alive()
    )
    supervisor_started = bool(
        supervisor_enabled
        and (
            bool(supervisor_start_marker)
            or supervisor_process_alive
            or supervisor_start_marker is None
        )
    )
    runtime["_risk_supervisor_started"] = supervisor_started
    shutdown_barrier_verified = bool(
        shutdown_phase.get("barrier_verified", False)
    )
    cancel_verified = bool(
        shutdown_barrier_verified or not account_shutdown_proof_required
    )
    independently_flat = cancel_verified
    kill_flatten_verified = cancel_verified
    flatten_verified = cancel_verified
    supervisor_quiesced = bool(
        shutdown_phase.get("supervisor_quiesced", not supervisor_started)
    )
    supervisor_stopped = bool(
        shutdown_phase.get("supervisor_stopped", not supervisor_started)
    )
    outbound_gate_closed = bool(
        shutdown_phase.get("outbound_gate_closed", oms_system is None)
    )
    shutdown_latched = bool(
        shutdown_phase.get("shutdown_latched", oms_system is None)
    )
    gateway_shutdown_latched = bool(
        shutdown_phase.get("gateway_shutdown_latched", gateway is None)
    )
    strategy_stopped = bool(
        shutdown_phase.get("strategy_stopped", strategy_runtime is None)
    )
    venue_supervisor_stopped = bool(
        shutdown_phase.get(
            "venue_supervisor_stopped",
            venue_supervisor is None,
        )
    )
    truth_monitor_stopped = bool(
        shutdown_phase.get("truth_monitor_stopped", truth_monitor is None)
    )
    gateway_closed = bool(
        shutdown_phase.get("gateway_closed", gateway is None)
    )
    event_drained = bool(
        shutdown_phase.get("event_drained", engine is None)
    )
    truth_provider_closed = bool(
        shutdown_phase.get("truth_provider_closed", truth_provider is None)
    )
    recorder_closed = bool(
        shutdown_phase.get("recorder_closed", recorder is None)
    )
    admin_control_closed = bool(
        shutdown_phase.get(
            "admin_control_closed",
            admin_control is None,
        )
    )
    oms_stopped = bool(
        shutdown_phase.get("oms_stopped", oms_system is None)
    )
    oms_clean = bool(
        shutdown_phase.get("oms_clean", oms_system is None)
    )
    engine_stopped = bool(
        shutdown_phase.get("engine_stopped", engine is None)
    )
    dashboard_stopped = bool(
        shutdown_phase.get("dashboard_stopped", web_dashboard is None)
    )
    clock_stopped = bool(
        shutdown_phase.get("clock_stopped", clock_service is None)
    )
    terminal_shutdown = False

    def run_step(name, callback):
        try:
            return True, callback()
        except BaseException as exc:
            logger.error(
                f"[Shutdown] {name} failed: {type(exc).__name__}:{exc}"
            )
            return False, None

    def step_acknowledged(ok, result) -> bool:
        return bool(ok and result is not False)

    def verify_account_orders(source: str) -> bool:
        if not account_shutdown_proof_required:
            if oms_system is None:
                return True
            if not shutdown_latched:
                return False
            verify_preconnect = getattr(
                oms_system,
                "verify_preconnect_shutdown_no_order_path",
                None,
            )
            if not callable(verify_preconnect):
                return False
            _ok, verified = run_step(
                f"verified_preconnect_shutdown_{source}",
                lambda: verify_preconnect(source=source),
            )
            return bool(_ok and verified and shutdown_latched)
        if (
            not shutdown_latched
            or oms_system is None
            or truth_provider is None
        ):
            return False
        _ok, verified = run_step(
            f"verified_account_cancel_{source}",
            lambda: call_with_risk_heartbeat(
                lambda: oms_system.cancel_all_account_orders_verified(
                    truth_provider,
                    source=source,
                ),
                risk_supervisor,
            ),
        )
        return bool(_ok and verified and shutdown_latched)

    def verify_account_positions(source: str) -> bool:
        if not account_shutdown_proof_required:
            return True
        if not live_canary_truth_required:
            return bool(kill_flatten_verified)
        if truth_provider is None:
            return False
        _ok, verified = run_step(
            f"verified_account_flat_{source}",
            lambda: call_with_risk_heartbeat(
                lambda: verify_account_flat(
                    truth_provider,
                    required_flat_snapshots=getattr(
                        oms_system,
                        "shutdown_empty_snapshots_required",
                        2,
                    ),
                    settle_interval_sec=getattr(
                        oms_system,
                        "shutdown_cancel_settle_interval_sec",
                        0.25,
                    ),
                ),
                risk_supervisor,
            ),
        )
        return bool(_ok and verified)

    def quiesce_acknowledged(result) -> bool:
        return bool(
            isinstance(result, dict)
            and result.get("accepted") is True
            and result.get("quiesced") is True
            and result.get("persisted") is True
        )

    def stop_acknowledged(result) -> bool:
        return bool(
            isinstance(result, dict)
            and result.get("accepted") is True
            and result.get("quiesced") is True
            and result.get("cancel_requested") is False
            and result.get("process_exited") is True
            and result.get("forced_terminated") is False
        )

    def shutdown_resume_acknowledged(result) -> bool:
        return bool(
            isinstance(result, dict)
            and result.get("accepted") is True
            and result.get("quiesced") is False
            and result.get("kill_latched") is True
            and result.get("persisted") is True
        )

    def restart_parent_kill_after_truth_drift(
        drift_reason: str,
        step_name: str,
    ) -> bool:
        restart_kill = getattr(
            risk_controller,
            "restart_kill_switch_after_truth_drift",
            None,
        )
        if not callable(restart_kill):
            return False
        _restart_ok, restart_result = run_step(
            step_name,
            lambda: restart_kill(drift_reason),
        )
        return bool(
            _restart_ok
            and restart_result is not False
            and wait_for_kill_flatten_verification(
                risk_controller,
                risk_supervisor,
            )
        )

    def publish_shutdown_blocked(block_reason: str) -> None:
        runtime["_shutdown_verified"] = False
        runtime["_shutdown_retryable"] = True
        if web_dashboard is not None and not dashboard_stopped:
            run_step(
                "dashboard_shutdown_blocked",
                lambda: web_dashboard.set_startup_status(
                    state="SHUTDOWN_BLOCKED",
                    operating_mode="LOCKDOWN",
                    startup_blocked=True,
                    execution_enabled=False,
                    restart_required=True,
                    reason=block_reason,
                ),
            )
            run_step(
                "dashboard_publish_blocked",
                lambda: web_dashboard.publish_snapshot(force=True),
            )

    try:
        if web_dashboard is not None:
            run_step(
                "dashboard_status",
                lambda: web_dashboard.set_startup_status(
                    state="SHUTTING_DOWN",
                    operating_mode="CANCEL_ONLY",
                    startup_blocked=False,
                    execution_enabled=False,
                    restart_required=False,
                    reason=reason,
                ),
            )
            run_step(
                "dashboard_publish_shutting_down",
                lambda: web_dashboard.publish_snapshot(force=True),
            )

        if not outbound_gate_closed:
            close_outbound_gate = getattr(
                oms_system,
                "close_outbound_gate",
                None,
            )
            if callable(close_outbound_gate):
                _ok, gate_result = run_step(
                    "oms_close_outbound_gate",
                    lambda: close_outbound_gate(
                        f"shutdown:{reason}",
                        wait=False,
                    ),
                )
                outbound_gate_closed = step_acknowledged(
                    _ok,
                    gate_result,
                )
            shutdown_phase["outbound_gate_closed"] = (
                outbound_gate_closed
            )

        if not strategy_stopped:
            strategy_stop_timeout_sec = max(
                0.0,
                float(
                    getattr(
                        strategy_runtime,
                        "shutdown_timeout_sec",
                        5.0,
                    )
                    or 0.0
                ),
            )

            def stop_strategy():
                try:
                    return strategy_runtime.stop(
                        timeout_sec=strategy_stop_timeout_sec,
                    )
                except TypeError:
                    return strategy_runtime.stop()

            _ok, stop_result = run_step(
                "strategy_stop",
                stop_strategy,
            )
            strategy_stopped = step_acknowledged(_ok, stop_result)
            shutdown_phase["strategy_stopped"] = strategy_stopped

        if not shutdown_barrier_verified:
            if account_shutdown_proof_required:
                if risk_controller is not None:
                    if not bool(
                        getattr(
                            risk_controller,
                            "kill_switch_triggered",
                            False,
                        )
                    ):
                        run_step(
                            "risk_shutdown_flatten",
                            lambda: risk_controller.trigger_kill_switch(
                                f"ProcessShutdown: {reason}"
                            ),
                        )
                    elif (
                        str(
                            getattr(risk_controller, "kill_state", "") or ""
                        ).upper()
                        == "FAILED"
                    ):
                        resume_kill = getattr(
                            risk_controller,
                            "resume_kill_switch_supervision",
                            None,
                        )
                        if callable(resume_kill):
                            run_step(
                                "risk_resume_shutdown_kill",
                                resume_kill,
                            )
                    kill_flatten_verified = (
                        bool(
                            getattr(
                                risk_controller,
                                "kill_switch_triggered",
                                False,
                            )
                        )
                        and wait_for_kill_flatten_verification(
                            risk_controller,
                            risk_supervisor,
                        )
                        and bool(
                            getattr(
                                risk_controller,
                                "kill_switch_triggered",
                                False,
                            )
                        )
                        and str(
                            getattr(
                                risk_controller,
                                "kill_state",
                                "",
                            )
                            or ""
                        ).upper()
                        == "FLAT_VERIFIED"
                    )
                else:
                    kill_flatten_verified = False
            else:
                kill_flatten_verified = True

            if (
                account_shutdown_proof_required
                and not kill_flatten_verified
            ):
                publish_shutdown_blocked(
                    "kill/flatten did not reach FLAT_VERIFIED before "
                    "shutdown latches"
                )
                logger.critical(
                    "[Shutdown] OMS and Gateway shutdown latches were "
                    "deferred so emergency cancellation and reduce-only "
                    "flattening remain retryable"
                )
                return False

            if not shutdown_latched:
                _ok, latch_result = run_step(
                    "oms_begin_shutdown",
                    lambda: oms_system.begin_shutdown(reason),
                )
                shutdown_latched = step_acknowledged(
                    _ok,
                    latch_result,
                )
                shutdown_phase["shutdown_latched"] = shutdown_latched

            if shutdown_latched and not gateway_shutdown_latched:
                begin_gateway_shutdown = getattr(
                    gateway,
                    "begin_shutdown",
                    None,
                )
                if callable(begin_gateway_shutdown):
                    _ok, latch_result = run_step(
                        "gateway_begin_shutdown",
                        begin_gateway_shutdown,
                    )
                    gateway_shutdown_latched = step_acknowledged(
                        _ok,
                        latch_result,
                    )
                else:
                    gateway_shutdown_latched = gateway is None
                shutdown_phase["gateway_shutdown_latched"] = (
                    gateway_shutdown_latched
                )

            final_truth_ready = bool(
                shutdown_latched and gateway_shutdown_latched
            )
            cancel_verified = bool(
                final_truth_ready
                and verify_account_orders(
                    "process_shutdown_pre_quiesce"
                )
            )
            independently_flat = bool(
                final_truth_ready
                and verify_account_positions(
                    "process_shutdown_pre_quiesce"
                )
            )
            flatten_verified = bool(
                kill_flatten_verified and independently_flat
            )

            if final_truth_ready and (
                not cancel_verified or not flatten_verified
            ):
                kill_flatten_verified = (
                    restart_parent_kill_after_truth_drift(
                        "Pre-quiesce account truth drift",
                        "risk_restart_after_pre_quiesce_drift",
                    )
                )
                cancel_verified = False
                independently_flat = False
                flatten_verified = False

            if supervisor_started and not supervisor_quiesced:
                pre_quiesce_barrier = bool(
                    outbound_gate_closed
                    and gateway_shutdown_latched
                    and strategy_stopped
                    and shutdown_latched
                    and cancel_verified
                    and flatten_verified
                )
                if pre_quiesce_barrier:
                    quiesce = getattr(risk_supervisor, "quiesce", None)
                    if callable(quiesce):
                        _ok, quiesce_result = run_step(
                            "risk_supervisor_quiesce",
                            lambda: quiesce(
                                reason=f"ProcessShutdown: {reason}",
                            ),
                        )
                        supervisor_quiesced = bool(
                            _ok
                            and quiesce_acknowledged(quiesce_result)
                        )
                        shutdown_phase["supervisor_quiesced"] = (
                            supervisor_quiesced
                        )

            if supervisor_started and supervisor_quiesced:
                cancel_verified = verify_account_orders(
                    "process_shutdown_post_quiesce"
                )
                independently_flat = verify_account_positions(
                    "process_shutdown_post_quiesce"
                )
                flatten_verified = bool(
                    kill_flatten_verified and independently_flat
                )
                if not cancel_verified or not flatten_verified:
                    resume_guard = getattr(
                        risk_supervisor,
                        "resume_shutdown_guard",
                        None,
                    )
                    _ok, resume_result = (
                        run_step(
                            "risk_supervisor_resume_shutdown_guard",
                            lambda: resume_guard(
                                reason=(
                                    "Post-quiesce account truth drift: "
                                    f"{reason}"
                                ),
                            ),
                        )
                        if callable(resume_guard)
                        else (False, None)
                    )
                    resumed = bool(
                        _ok
                        and shutdown_resume_acknowledged(
                            resume_result
                        )
                    )
                    if resumed:
                        supervisor_quiesced = False
                        supervisor_stopped = False
                        shutdown_phase["supervisor_quiesced"] = False
                        shutdown_phase["supervisor_stopped"] = False
                    else:
                        logger.critical(
                            "[Shutdown] Post-quiesce account truth failed "
                            "and the independent shutdown guard could not "
                            "be resumed"
                        )
                    kill_flatten_verified = (
                        restart_parent_kill_after_truth_drift(
                            "Post-quiesce account truth drift",
                            "risk_restart_after_post_quiesce_drift",
                        )
                    )
                    cancel_verified = False
                    independently_flat = False
                    flatten_verified = False

            if supervisor_started and not supervisor_stopped:
                sidecar_stop_barrier = bool(
                    outbound_gate_closed
                    and strategy_stopped
                    and shutdown_latched
                    and gateway_shutdown_latched
                    and cancel_verified
                    and flatten_verified
                    and supervisor_quiesced
                )
                if sidecar_stop_barrier:
                    _ok, stop_result = run_step(
                        "risk_supervisor_stop",
                        lambda: risk_supervisor.stop(
                            cancel_orders=False
                        ),
                    )
                    supervisor_stopped = bool(
                        _ok and stop_acknowledged(stop_result)
                    )
                    shutdown_phase["supervisor_stopped"] = (
                        supervisor_stopped
                    )
                if not supervisor_stopped:
                    logger.critical(
                        "[Shutdown] IndependentRiskSupervisor remains "
                        "active or unclean because its quiesce/stop "
                        "barrier was not fully acknowledged"
                    )

            shutdown_barrier_verified = bool(
                outbound_gate_closed
                and strategy_stopped
                and shutdown_latched
                and gateway_shutdown_latched
                and cancel_verified
                and flatten_verified
                and supervisor_quiesced
                and supervisor_stopped
            )
            shutdown_phase["barrier_verified"] = (
                shutdown_barrier_verified
            )
        if not shutdown_barrier_verified:
            publish_shutdown_blocked(
                "final account or sidecar shutdown proof failed"
            )
            logger.critical(
                "[Shutdown] Trading teardown remains retryable and dirty: "
                f"outbound_gate_closed={outbound_gate_closed} "
                f"strategy_stopped={strategy_stopped} "
                f"shutdown_latched={shutdown_latched} "
                f"gateway_shutdown_latched={gateway_shutdown_latched} "
                f"cancel_verified={cancel_verified} "
                f"flatten_verified={flatten_verified} "
                f"supervisor_quiesced={supervisor_quiesced} "
                f"supervisor_stopped={supervisor_stopped}"
            )
            return False

        if not admin_control_closed:
            _ok, close_result = run_step(
                "admin_control_close",
                admin_control.close,
            )
            admin_control_closed = step_acknowledged(
                _ok,
                close_result,
            )
            shutdown_phase["admin_control_closed"] = (
                admin_control_closed
            )
        if not admin_control_closed:
            publish_shutdown_blocked(
                "admin control session withdrawal failed"
            )
            return False

        if not venue_supervisor_stopped:
            _ok, stop_result = run_step(
                "venue_supervisor_stop",
                venue_supervisor.stop,
            )
            venue_supervisor_stopped = step_acknowledged(
                _ok,
                stop_result,
            )
            shutdown_phase["venue_supervisor_stopped"] = (
                venue_supervisor_stopped
            )
        if not truth_monitor_stopped:
            _ok, stop_result = run_step(
                "truth_monitor_stop",
                truth_monitor.stop,
            )
            truth_monitor_stopped = step_acknowledged(
                _ok,
                stop_result,
            )
            shutdown_phase["truth_monitor_stopped"] = (
                truth_monitor_stopped
            )
        if not venue_supervisor_stopped or not truth_monitor_stopped:
            publish_shutdown_blocked(
                "truth or venue monitor did not stop cleanly"
            )
            return False

        if not gateway_closed:
            _ok, close_result = run_step(
                "gateway_close",
                gateway.close,
            )
            gateway_closed = step_acknowledged(_ok, close_result)
            shutdown_phase["gateway_closed"] = gateway_closed
        if not gateway_closed:
            publish_shutdown_blocked("gateway close failed")
            return False

        if not event_drained:
            shutdown_drain_timeout_sec = max(
                0.0,
                float(event_engine_config.get("shutdown_drain_timeout_sec", 5.0)),
            )
            _ok, drain_result = run_step(
                "event_engine_drain",
                lambda: engine.wait_until_idle(shutdown_drain_timeout_sec),
            )
            event_drained = bool(_ok and drain_result)
            shutdown_phase["event_drained"] = event_drained
            if not event_drained:
                _snapshot_ok, queue_snapshot = run_step(
                    "event_engine_queue_snapshot",
                    engine.get_queue_snapshot,
                )
                logger.warning(
                    "[Shutdown] EventEngine drain timed out: "
                    f"{queue_snapshot if _snapshot_ok else 'unavailable'}"
                )
        if not event_drained:
            publish_shutdown_blocked("event engine drain timed out")
            return False

        if not recorder_closed:
            _ok, close_result = run_step(
                "recorder_close",
                recorder.close,
            )
            recorder_closed = step_acknowledged(_ok, close_result)
            shutdown_phase["recorder_closed"] = recorder_closed
        if not truth_provider_closed:
            _ok, close_result = run_step(
                "truth_provider_close",
                truth_provider.close,
            )
            truth_provider_closed = step_acknowledged(
                _ok,
                close_result,
            )
            shutdown_phase["truth_provider_closed"] = (
                truth_provider_closed
            )
        if not recorder_closed or not truth_provider_closed:
            publish_shutdown_blocked(
                "recorder or truth provider close failed"
            )
            return False

        clean_shutdown = bool(
            shutdown_barrier_verified
            and gateway_closed
            and event_drained
        )
        if not (oms_stopped and oms_clean):
            _ok, stop_result = run_step(
                "oms_stop",
                lambda: oms_system.stop(
                    clean_shutdown=clean_shutdown,
                    reason=reason,
                ),
            )
            if isinstance(stop_result, dict):
                oms_stopped = bool(
                    oms_stopped
                    or (_ok and stop_result.get("stopped") is True)
                )
                oms_clean = bool(
                    oms_clean
                    or (_ok and stop_result.get("clean") is True)
                )
            else:
                current_stopped = step_acknowledged(_ok, stop_result)
                oms_stopped = bool(oms_stopped or current_stopped)
                oms_clean = bool(
                    oms_clean or (current_stopped and clean_shutdown)
                )
            shutdown_phase["oms_stopped"] = oms_stopped
            shutdown_phase["oms_clean"] = oms_clean
        clean_shutdown = bool(clean_shutdown and oms_clean)
        if not oms_stopped or not oms_clean:
            publish_shutdown_blocked(
                "OMS stop was incomplete or durably unclean"
            )
            return False

        if not engine_stopped:
            _ok, stop_result = run_step(
                "event_engine_stop",
                engine.stop,
            )
            engine_stopped = step_acknowledged(_ok, stop_result)
            shutdown_phase["engine_stopped"] = engine_stopped
        if not engine_stopped:
            publish_shutdown_blocked("event engine stop failed")
            return False

        if not dashboard_stopped:
            run_step(
                "dashboard_publish_final",
                lambda: web_dashboard.publish_snapshot(force=True),
            )
            _ok, stop_result = run_step(
                "dashboard_stop",
                web_dashboard.stop,
            )
            dashboard_stopped = step_acknowledged(_ok, stop_result)
            shutdown_phase["dashboard_stopped"] = dashboard_stopped
        if not dashboard_stopped:
            publish_shutdown_blocked("dashboard stop failed")
            return False

        if not clock_stopped:
            _ok, stop_result = run_step(
                "time_service_stop",
                clock_service.stop,
            )
            clock_stopped = step_acknowledged(_ok, stop_result)
            shutdown_phase["clock_stopped"] = clock_stopped

        terminal_shutdown = bool(
            strategy_stopped
            and venue_supervisor_stopped
            and truth_monitor_stopped
            and gateway_closed
            and event_drained
            and truth_provider_closed
            and recorder_closed
            and admin_control_closed
            and oms_stopped
            and oms_clean
            and engine_stopped
            and dashboard_stopped
            and clock_stopped
            and supervisor_stopped
        )
        verified_shutdown = bool(
            clean_shutdown and oms_clean and terminal_shutdown
        )
        runtime["_shutdown_verified"] = verified_shutdown
        runtime["_shutdown_retryable"] = not terminal_shutdown
        runtime["_shutdown_complete"] = terminal_shutdown
        logger.info(
            "ChronosHFT Shutdown Complete. "
            f"verified_cancel={cancel_verified} "
            f"flatten_verified={flatten_verified} "
            f"supervisor_quiesced={supervisor_quiesced} "
            f"supervisor_stopped={supervisor_stopped} "
            f"event_drained={event_drained} "
            f"oms_clean={oms_clean} "
            f"terminal={terminal_shutdown}"
        )
        return verified_shutdown
    finally:
        if dashboard_stopped:
            logger.set_ui_callback(None)
        runtime["_shutdown_in_progress"] = False


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
    runtime = {}
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
