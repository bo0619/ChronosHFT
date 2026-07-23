import argparse
import math
import multiprocessing
import os
import threading
import time
import webbrowser

from data.cache import data_cache
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
    EVENT_ORDER_SUBMITTED,
    EVENT_ORDER_UPDATE,
    EVENT_POSITION_UPDATE,
    EVENT_STRATEGY_UPDATE,
    EVENT_SYSTEM_HEALTH,
    EVENT_TRADE_UPDATE,
)
from gateway.binance.gateway import BinanceGateway
from gateway.binance.truth_provider import BinanceTruthSnapshotProvider
from infrastructure.admin_control import (
    AdminControlServer,
    coordinated_rearm,
    submit_admin_command,
)
from infrastructure.config_scaling import load_root_config
from infrastructure.logger import logger
from infrastructure.paper_trade import apply_paper_trade_mode, is_paper_trade
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
    config = load_root_config(path)
    if config:
        return config
    if not os.path.exists(path):
        print(f"Error: {path} not found.")
    return None


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
    risk_healthy = bool(risk_controller.check_market_data_freshness())
    return supervisor_healthy and risk_healthy


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


def synchronize_commission_config(gateway, config, symbols):
    """Load account-specific execution fees before strategy activation."""
    get_rate = getattr(gateway, "get_commission_rate", None)
    if not callable(get_rate):
        raise RuntimeError("Gateway does not expose commission-rate truth")

    maker_rates = []
    taker_rates = []
    rpi_rates = {}
    for raw_symbol in symbols or []:
        symbol = str(raw_symbol or "").upper()
        payload = get_rate(symbol)
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Commission-rate truth unavailable for {symbol}"
            )

        parsed = {}
        for field in (
            "makerCommissionRate",
            "takerCommissionRate",
            "rpiCommissionRate",
        ):
            if field not in payload:
                raise RuntimeError(
                    f"Missing {field} for {symbol}"
                )
            raw_value = payload[field]
            try:
                value = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Invalid {field} for {symbol}"
                ) from exc
            if not math.isfinite(value) or abs(value) > 0.01:
                raise RuntimeError(
                    f"Out-of-range {field} for {symbol}: {value}"
                )
            parsed[field] = value

        maker_rates.append(parsed["makerCommissionRate"])
        taker_rates.append(parsed["takerCommissionRate"])
        rpi_rates[symbol] = parsed["rpiCommissionRate"]

    if not maker_rates:
        raise RuntimeError("No configured symbols for commission-rate sync")

    fee_config = config.setdefault("backtest", {})
    fee_config["maker_fee"] = max(maker_rates)
    fee_config["taker_fee"] = max(taker_rates)
    fee_config["rpi_commission_rate"] = 0.0
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

    gateway = BinanceGateway(
        engine,
        config["api_key"],
        config["api_secret"],
        testnet=config["testnet"],
        market_data_config=market_data_config,
    )
    truth_provider = BinanceTruthSnapshotProvider(
        config["api_key"],
        config["api_secret"],
        testnet=config["testnet"],
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


def start_local_dashboard(web_dashboard, web_dashboard_config):
    """Bind the local dashboard, print its URL, and optionally open it."""
    if web_dashboard is None:
        return ""
    try:
        dashboard_url = web_dashboard.start()
    except OSError as exc:
        message = f"Local dashboard failed to start: {exc}"
        logger.error(message)
        print(message, flush=True)
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


# P0 TODO: Replace the host-local OMS file lock with replicated leader election
# and a monotonic fencing token before supporting multi-host active/passive OMS.
def _run_main(argv=None, runtime=None):
    runtime = runtime if runtime is not None else {}
    args = parse_cli_args(argv)
    config = load_config(args.config)
    if not config:
        return

    paper_trade = is_paper_trade(config)
    if paper_trade:
        config = apply_paper_trade_mode(config)

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
        return

    if args.admin_command:
        result = submit_admin_command(
            action=args.admin_command,
            reason=str(args.admin_reason or "operator_ack"),
            config=config,
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
        return

    logger.init_logging(config)
    logger.set_ui_callback(None)

    time_service.clear_listeners()
    time_sync_config = config.get("system", {}).get("time_sync", {}) or {}
    time_service.configure(time_sync_config)
    initial_clock_sync_ok = time_service.start(testnet=config["testnet"])
    clock_startup_required = bool(time_sync_config.get("startup_required", True))
    if clock_startup_required and not (
        initial_clock_sync_ok and time_service.is_ready()
    ):
        run_startup_blocked_dashboard(
            config,
            read_clock_health(time_service),
            clock_service=time_service,
        )
        return

    runtime.update(
        {
            "config": config,
            "paper_trade": paper_trade,
            "time_service": time_service,
        }
    )

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
    strategy_runtime = StrategyRuntime(
        strategy,
        config.get("system", {}).get("strategy_runtime", {}),
        start_thread=False,
    )
    runtime["strategy_runtime"] = strategy_runtime
    recorder = DataRecorder(engine, config["symbols"]) if config.get("record_data", False) else None
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

    def on_time_service_health(severity, reason, details):
        if severity == "freeze":
            oms_system.freeze_system(f"TimeSync: {reason}", cancel_active_orders=True)
            return
        if severity == "halt":
            risk_controller.trigger_kill_switch(f"TimeSync: {reason}")
            return
        if severity == "recovered" and oms_system.state.value == "FROZEN":
            if oms_system.last_freeze_reason.startswith("TimeSync:"):
                oms_system.trigger_reconcile("Time sync recovered")

    time_service.register_listener(on_time_service_health)
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
    register_execution(EVENT_ORDER_SUBMITTED, lambda e: oms_system.order_monitor.on_order_submitted(e))
    register_execution(
        EVENT_SYSTEM_HEALTH,
        lambda e: handle_system_health_event(e, risk_controller, oms_system),
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
    register_cold(
        EVENT_ALERT,
        lambda e: web_dashboard.update_alert(e.data) if web_dashboard else None,
    )
    register_cold(
        EVENT_LOG,
        lambda e: web_dashboard.add_log(e.data) if web_dashboard else None,
    )

    engine.start()
    strategy_runtime.start()
    if not risk_supervisor.start():
        raise RuntimeError("IndependentRiskSupervisor failed to start")
    start_local_dashboard(web_dashboard, web_dashboard_config)
    gateway_connected = connect_gateway_with_risk_heartbeat(
        gateway,
        config["symbols"],
        risk_supervisor,
    )
    if gateway_connected is False:
        raise RuntimeError("Gateway failed to reach transport-and-book readiness")
    synchronize_commission_config(
        gateway,
        config,
        config["symbols"],
    )

    warmup_deadline = time.perf_counter() + 3.0
    while time.perf_counter() < warmup_deadline:
        run_live_risk_checks(risk_controller, risk_supervisor)
        time.sleep(0.1)
    if not risk_supervisor.wait_until_healthy(timeout_sec=2.0):
        oms_system.halt_system("independent_risk_supervisor_unavailable")
    risk_controller.resume_kill_switch_supervision()
    bootstrap_or_rearm(
        oms_system,
        auto_rearm=bool(args.rearm),
        rearm_reason=str(args.rearm_reason or "cli"),
        risk_manager=risk_controller,
        risk_supervisor=risk_supervisor,
    )
    run_live_risk_checks(risk_controller, risk_supervisor)
    truth_monitor.start()
    venue_supervisor.start()

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

    while True:
        time.sleep(0.1)
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
        }
        if web_dashboard is not None:
            web_dashboard.update_runtime_metrics(runtime_metrics)
        admin_control.poll_once()


def shutdown_runtime(runtime, reason: str = "main_exit") -> bool:
    """Idempotently stop trading only after proving the venue has no orders."""
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
    risk_supervisor = runtime.get("risk_supervisor")
    truth_monitor = runtime.get("truth_monitor")
    venue_supervisor = runtime.get("venue_supervisor")
    recorder = runtime.get("recorder")
    web_dashboard = runtime.get("web_dashboard")
    clock_service = runtime.get("time_service")
    event_engine_config = runtime.get("event_engine_config", {}) or {}

    cancel_verified = False
    gateway_closed = gateway is None
    event_drained = engine is None

    def run_step(name, callback):
        try:
            return True, callback()
        except BaseException as exc:
            logger.error(
                f"[Shutdown] {name} failed: {type(exc).__name__}:{exc}"
            )
            return False, None

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
        if strategy_runtime is not None:
            run_step("strategy_stop", strategy_runtime.stop)

        shutdown_latched = False
        if oms_system is not None:
            _ok, shutdown_latched = run_step(
                "oms_begin_shutdown",
                lambda: oms_system.begin_shutdown(reason),
            )

        if gateway is not None:
            begin_gateway_shutdown = getattr(gateway, "begin_shutdown", None)
            if callable(begin_gateway_shutdown):
                run_step("gateway_begin_shutdown", begin_gateway_shutdown)

        if venue_supervisor is not None:
            run_step("venue_supervisor_stop", venue_supervisor.stop)
        if truth_monitor is not None:
            run_step("truth_monitor_stop", truth_monitor.stop)
        if risk_supervisor is not None:
            run_step(
                "risk_supervisor_stop",
                lambda: risk_supervisor.stop(cancel_orders=False),
            )

        if oms_system is not None and truth_provider is not None:
            _ok, cancel_verified = run_step(
                "verified_account_cancel",
                lambda: oms_system.cancel_all_account_orders_verified(
                    truth_provider,
                    source="process_shutdown",
                ),
            )
            cancel_verified = bool(cancel_verified and shutdown_latched)

        if gateway is not None:
            gateway_closed, _value = run_step("gateway_close", gateway.close)

        if engine is not None and gateway_closed:
            shutdown_drain_timeout_sec = max(
                0.0,
                float(event_engine_config.get("shutdown_drain_timeout_sec", 5.0)),
            )
            _ok, event_drained = run_step(
                "event_engine_drain",
                lambda: engine.wait_until_idle(shutdown_drain_timeout_sec),
            )
            event_drained = bool(event_drained)
            if not event_drained:
                logger.warning(
                    "[Shutdown] EventEngine drain timed out: "
                    f"{engine.get_queue_snapshot()}"
                )

        if recorder is not None:
            run_step("recorder_close", recorder.close)
        if truth_provider is not None:
            run_step("truth_provider_close", truth_provider.close)

        clean_shutdown = bool(
            cancel_verified and gateway_closed and event_drained
        )
        if oms_system is not None:
            run_step(
                "oms_stop",
                lambda: oms_system.stop(
                    clean_shutdown=clean_shutdown,
                    reason=reason,
                ),
            )
        if engine is not None:
            run_step("event_engine_stop", engine.stop)
        if web_dashboard is not None:
            run_step(
                "dashboard_publish_final",
                lambda: web_dashboard.publish_snapshot(force=True),
            )
            run_step("dashboard_stop", web_dashboard.stop)

        runtime["_shutdown_verified"] = clean_shutdown
        logger.info(
            "ChronosHFT Shutdown Complete. "
            f"verified_cancel={cancel_verified} event_drained={event_drained}"
        )
        return clean_shutdown
    finally:
        logger.set_ui_callback(None)
        if clock_service is not None:
            run_step("time_service_stop", clock_service.stop)
        runtime["_shutdown_in_progress"] = False
        runtime["_shutdown_complete"] = True


def main(argv=None):
    runtime = {}
    shutdown_reason = "main_return"
    try:
        return _run_main(argv, runtime)
    except KeyboardInterrupt:
        shutdown_reason = "keyboard_interrupt"
        logger.info("Shutdown signal received.")
        return None
    except BaseException as exc:
        shutdown_reason = f"fatal:{type(exc).__name__}:{exc}"
        logger.critical(f"ChronosHFT fatal error: {type(exc).__name__}:{exc}")
        print(f"ChronosHFT fatal error: {type(exc).__name__}: {exc}", flush=True)
        raise
    finally:
        shutdown_runtime(runtime, shutdown_reason)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
