import argparse
import multiprocessing
import os
import sys
import time
import webbrowser

from rich.live import Live

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
from ui.dashboard import TUIDashboard
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


# P0 TODO: Replace the host-local OMS file lock with replicated leader election
# and a monotonic fencing token before supporting multi-host active/passive OMS.
def main(argv=None):
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

    config["system"]["log_console"] = False
    logger.init_logging(config)

    event_engine_config = config.get("system", {}).get("event_engine", {})
    engine = EventEngine(event_engine_config)
    dashboard = TUIDashboard()
    logger.set_ui_callback(dashboard.add_log)

    market_data_config = config.get("system", {}).get("market_data", {})
    gateway, truth_provider = build_gateway_bundle(
        engine,
        config,
        market_data_config,
    )
    oms_system = OMS(engine, gateway, config)
    risk_controller = RiskManager(engine, config, oms=oms_system, gateway=gateway)
    risk_supervisor = IndependentRiskSupervisor(
        oms_system,
        config,
        risk_manager=risk_controller,
    )
    if paper_trade and risk_supervisor.enabled:
        raise RuntimeError("Paper Trade must not enable IndependentRiskSupervisor")
    alpha_process_config = (
        config.get("system", {})
        .get("strategy_runtime", {})
        .get("alpha_process", {"enabled": True})
    )
    alpha_process_config = dict(alpha_process_config or {})
    alpha_process_config.setdefault("processes", min(4, max(1, len(config.get("symbols", [])))))
    strategy = create_primary_strategy(
        engine,
        oms_system,
        config,
        alpha_process_config=alpha_process_config,
    )
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
    recorder = DataRecorder(engine, config["symbols"]) if config.get("record_data", False) else None
    truth_monitor = TruthMonitor(oms_system, truth_provider, config, start_thread=False)
    venue_supervisor = VenueSupervisor(oms_system, gateway, config, start_thread=False)
    admin_control = AdminControlServer(
        oms_system,
        config,
        risk_manager=risk_controller,
        risk_supervisor=risk_supervisor,
    )
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

        def on_dashboard_log(message):
            dashboard.add_log(message)
            web_dashboard.add_log(message)

        logger.set_ui_callback(on_dashboard_log)

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

    time_service.clear_listeners()
    time_service.configure(config.get("system", {}).get("time_sync", {}))
    time_service.register_listener(on_time_service_health)
    time_service.start(testnet=config["testnet"])
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

    main.last_tick_time = time.time()
    main.stale_watchdog_triggered = False
    main.event_engine_watchdog_state = {}
    main.strategy_runtime_watchdog_state = {}

    def on_hot_tick(_event):
        main.last_tick_time = time.time()
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
        dashboard.update_market(event.data)
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
        dashboard.update_position(event.data)
        if web_dashboard is not None:
            web_dashboard.update_position(event.data)

    def on_account_cold(event):
        strategy_runtime.on_account_update(event.data)
        dashboard.update_account(event.data)
        if web_dashboard is not None:
            web_dashboard.update_account(event.data)

    def on_strategy_cold(event):
        dashboard.update_strategy(event.data)
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
    risk_supervisor.start()
    if web_dashboard is not None:
        try:
            dashboard_url = web_dashboard.start()
            logger.info(f"Local dashboard ready: {dashboard_url}")
            if bool(web_dashboard_config.get("open_browser", False)):
                webbrowser.open(dashboard_url)
        except OSError as exc:
            logger.error(f"Local dashboard failed to start: {exc}")
    gateway.connect(config["symbols"])

    warmup_deadline = time.monotonic() + 3.0
    while time.monotonic() < warmup_deadline:
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

    if paper_trade:
        logger.info(
            "ChronosHFT PAPER · LIVE DATA: Binance production public market "
            "data, local simulated execution, private API disabled."
        )
    else:
        logger.info("ChronosHFT Core Engine LIVE · REAL MONEY.")

    try:
        with Live(dashboard.render(), refresh_per_second=4, screen=True) as live:
            while True:
                live.update(dashboard.render())
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
                dashboard.update_runtime_metrics(runtime_metrics)
                if web_dashboard is not None:
                    web_dashboard.update_runtime_metrics(runtime_metrics)
                admin_control.poll_once()
    except KeyboardInterrupt:
        logger.info("Shutdown signal received.")
        if recorder:
            recorder.close()
        venue_supervisor.stop()
        truth_monitor.stop()
        strategy_runtime.stop()
        truth_provider.close()
        time_service.stop()
        risk_supervisor.stop(cancel_orders=True)
        if paper_trade:
            # Keep OMS/execution handlers alive while the local venue emits
            # final cancel acknowledgements and stops its matching worker.
            gateway.close()
            shutdown_drain_timeout_sec = max(
                0.0,
                float(event_engine_config.get("shutdown_drain_timeout_sec", 5.0)),
            )
            if not engine.wait_until_idle(shutdown_drain_timeout_sec):
                logger.warning(
                    "[Paper] EventEngine shutdown drain timed out: "
                    f"{engine.get_queue_snapshot()}"
                )
            oms_system.stop()
            engine.stop()
        else:
            oms_system.stop()
            engine.stop()
            gateway.close()
        if web_dashboard is not None:
            web_dashboard.publish_snapshot(force=True)
            web_dashboard.stop()
        logger.info("ChronosHFT Shutdown Complete.")
        sys.exit(0)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
