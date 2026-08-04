"""Explicit application assembly for the ChronosHFT process runtime."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from infrastructure.runtime_configuration import print_config_summary
from infrastructure.runtime_readiness import (
    RuntimeReadinessController,
    RuntimeReadinessEvaluator,
)
from infrastructure.runtime_resources import RuntimeResources


@dataclass(frozen=True)
class RuntimeConfigurationServices:
    parse_cli_args: Callable
    load_config: Callable
    load_admin_control_config: Callable
    submit_admin_command: Callable
    is_paper_trade: Callable
    apply_paper_trade_mode: Callable


@dataclass(frozen=True)
class RuntimePlatformServices:
    logger: Any
    systemd_watchdog_type: Callable
    start_external_alert_service: Callable
    time_service: Any
    read_clock_health: Callable
    run_startup_blocked_dashboard: Callable
    monotonic: Callable[[], float]
    sleep: Callable[[float], None]


@dataclass(frozen=True)
class RuntimeFactoryServices:
    event_engine_type: Callable
    build_gateway_bundle: Callable
    oms_type: Callable
    risk_manager_type: Callable
    failure_policy_type: Callable
    independent_risk_supervisor_type: Callable
    create_primary_strategy: Callable
    strategy_runtime_type: Callable
    data_recorder_type: Callable
    process_resource_monitor_type: Callable
    live_evidence_recorder_type: Callable
    recorder_group_type: Callable
    truth_monitor_type: Callable
    venue_supervisor_type: Callable
    admin_control_server_type: Callable
    local_web_dashboard_type: Callable
    ref_data_manager: Any
    watchdog_state_type: Callable
    event_bindings_type: Callable


@dataclass(frozen=True)
class RuntimeSafetyServices:
    start_local_dashboard: Callable
    connect_gateway_with_risk_heartbeat: Callable
    synchronize_commission_config: Callable
    validate_live_rpi_policy: Callable
    run_live_risk_checks: Callable
    wait_for_initial_truth_snapshot: Callable
    validate_live_account_equity_truth: Callable
    validate_live_flat_start_truth: Callable
    external_alert_health: Callable
    enforce_live_evidence_health: Callable
    wait_for_initial_market_data_readiness: Callable
    bootstrap_or_rearm: Callable
    enforce_external_alert_health: Callable
    live_evidence_health: Callable


@dataclass(frozen=True)
class RuntimeLoopServices:
    runtime_control_loop_type: Callable
    runtime_control_services_type: Callable
    build_runtime_resource_event: Callable


@dataclass(frozen=True)
class RuntimeApplicationServices:
    """Five explicit capability groups used by RuntimeApplication."""

    configuration: RuntimeConfigurationServices
    platform: RuntimePlatformServices
    factories: RuntimeFactoryServices
    safety: RuntimeSafetyServices
    loop: RuntimeLoopServices


class RuntimeApplication:
    """Own the runtime graph and advance it through explicit startup phases."""

    def __init__(
        self,
        resources: RuntimeResources | dict | None,
        services: RuntimeApplicationServices,
    ) -> None:
        self.resources = RuntimeResources.coerce(resources)
        self.services = services
        self.args = None
        self.config: dict = {}
        self.paper_trade = False
        self.systemd_watchdog = None
        self.external_alerts = None
        self.engine = None
        self.gateway = None
        self.truth_provider = None
        self.oms = None
        self.risk_controller = None
        self.failure_policy = None
        self.risk_supervisor = None
        self.strategy = None
        self.strategy_runtime = None
        self.data_recorder = None
        self.resource_monitor = None
        self.live_evidence_recorder = None
        self.recorder = None
        self.truth_monitor = None
        self.venue_supervisor = None
        self.admin_control = None
        self.web_dashboard = None
        self.watchdog_state = None
        self.event_bindings = None
        self.control_loop = None
        self.event_engine_config: dict = {}
        self.web_dashboard_config: dict = {}
        self._listener_unsubscribers: list[Callable[[], object]] = []
        self.readiness_evaluator = RuntimeReadinessEvaluator(
            monotonic=services.platform.monotonic
            if hasattr(services, "platform")
            else time.perf_counter,
        )
        self.readiness_controller = RuntimeReadinessController(
            self.readiness_evaluator
        )
        self.readiness_components: dict[str, bool | None] = {}
        self.readiness_required: tuple[str, ...] = ()
        self.runtime_readiness_snapshot = None
    def run(self, argv=None):
        try:
            self.args = self.services.configuration.parse_cli_args(argv)
            if self.args.admin_command:
                return self._run_admin_command()

            early_result = self._load_configuration()
            if early_result is not None:
                return early_result

            blocked_result = self._initialize_process_services()
            if blocked_result is not None:
                return blocked_result

            self._assemble_runtime_graph()
            self._register_events_and_start_core()
            self._connect_execution_transport()
            self._pass_startup_gates()
            self._activate_execution()
            return self._run_control_loop()
        finally:
            self._release_runtime_listeners()
    def _release_runtime_listeners(self) -> None:
        while self._listener_unsubscribers:
            unsubscribe = self._listener_unsubscribers.pop()
            try:
                unsubscribe()
            except Exception as exc:
                logger = getattr(self.services.platform, "logger", None)
                if logger is not None:
                    logger.warning(
                        "Runtime listener unsubscribe failed: "
                        f"{type(exc).__name__}:{exc}"
                    )
    def _own(self, name: str, value):
        setattr(self, name, value)
        setattr(self.resources, name, value)
        return value
    def _run_admin_command(self) -> int:
        args = self.args
        admin_config = self.services.configuration.load_admin_control_config(args.config)
        result = self.services.configuration.submit_admin_command(
            action=args.admin_command,
            reason=str(args.admin_reason or "operator_ack"),
            config=admin_config,
            wait_timeout_sec=float(args.admin_timeout or 5.0),
        )
        snapshot = result.get("snapshot", {}) or {}
        print(
            f"admin_command={args.admin_command} "
            f"accepted={result.get('accepted')} "
            f"status={result.get('status')} "
            f"message={result.get('message')}"
        )
        if snapshot:
            print(
                "snapshot="
                f"state={snapshot.get('state')} "
                f"mode={snapshot.get('capability_mode')} "
                "manual_rearm_required="
                f"{snapshot.get('manual_rearm_required')} "
                f"halt_reason={snapshot.get('last_halt_reason')}"
            )
        return 0 if bool(result.get("accepted", False)) else 2
    def _load_configuration(self) -> int | None:
        args = self.args
        config = self.services.configuration.load_config(args.config)
        paper_trade = bool(self.services.configuration.is_paper_trade(config))
        if paper_trade:
            config = self.services.configuration.apply_paper_trade_mode(config)
        self.config = config
        self.paper_trade = paper_trade
        self._configure_readiness()

        if args.check_config:
            self._print_config_summary()
            return 0

        missing_credentials = [
            field
            for field in ("api_key", "api_secret")
            if not str(config.get(field, "") or "").strip()
        ]
        if missing_credentials and not paper_trade:
            env_names = [
                str(config.get(f"{field}_env", "") or "")
                for field in missing_credentials
            ]
            print(
                "Error: missing Binance credentials. Set environment "
                "variables: "
                + ", ".join(name for name in env_names if name)
            )
            return 2
        return None

    def _print_config_summary(self) -> None:
        print_config_summary(
            self.config,
            paper_trade=self.paper_trade,
        )
    def _initialize_process_services(self) -> int | None:
        services = self.services
        config = self.config
        logger = services.platform.logger
        logger.init_logging(config)
        logger.set_ui_callback(None)
        logger.set_alert_callback(None)

        watchdog = self._own(
            "systemd_watchdog",
            services.platform.systemd_watchdog_type(),
        )
        self.resources.config = config
        self.resources.paper_trade = self.paper_trade
        self.resources.account_shutdown_proof_required = False
        self.resources.risk_supervisor_started = False
        watchdog.pulse(force=True)

        self._own(
            "external_alerts",
            services.platform.start_external_alert_service(
                config,
                self.resources,
                paper_trade=self.paper_trade,
            ),
        )

        clock = services.platform.time_service
        time_sync_config = config.get("system", {}).get("time_sync", {}) or {}
        clock.configure(time_sync_config)
        initial_clock_sync_ok = clock.start(testnet=config["testnet"])
        clock_required = bool(time_sync_config.get("startup_required", True))
        if clock_required and not (
            initial_clock_sync_ok and clock.is_ready()
        ):
            diagnostics_available = services.platform.run_startup_blocked_dashboard(
                config,
                services.platform.read_clock_health(clock),
                clock_service=clock,
                systemd_watchdog=watchdog,
            )
            return 0 if diagnostics_available else 2

        self._own("time_service", clock)
        self.readiness_components["clock"] = True
        return None
    def _configure_readiness(self) -> None:
        self.readiness_controller.configure(paper_trade=self.paper_trade)
        self.readiness_components = self.readiness_controller.components
        self.readiness_required = self.readiness_controller.required

    def _evaluate_readiness(
        self,
        phase: str,
        *,
        require_execution: bool,
    ):
        capability_mode = getattr(self.oms, "capability_mode", "")
        capability_mode = getattr(capability_mode, "value", capability_mode)
        self.runtime_readiness_snapshot = self.readiness_controller.evaluate(
            phase=phase,
            require_execution=require_execution,
            operating_mode=str(capability_mode or ""),
        )
        return self.runtime_readiness_snapshot

    def _assemble_runtime_graph(self) -> None:
        services = self.services
        config = self.config
        self.event_engine_config = (
            config.get("system", {}).get("event_engine", {}) or {}
        )
        self.resources.event_engine_config = self.event_engine_config
        engine = self._own(
            "engine",
            services.factories.event_engine_type(self.event_engine_config),
        )

        market_data_config = (
            config.get("system", {}).get("market_data", {}) or {}
        )
        gateway, truth_provider = services.factories.build_gateway_bundle(
            engine,
            config,
            market_data_config,
        )
        self._own("gateway", gateway)
        self._own("truth_provider", truth_provider)
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

        oms = self._own("oms", services.factories.oms_type(engine, gateway, config))
        risk = self._own(
            "risk_controller",
            services.factories.risk_manager_type(
                engine,
                config,
                oms=oms,
                gateway=gateway,
            ),
        )
        failure_policy = self._own(
            "failure_policy",
            services.factories.failure_policy_type(
                oms=oms,
                risk_controller=risk,
                gateway_name=getattr(gateway, "gateway_name", "UNKNOWN"),
                logger=services.platform.logger,
            ),
        )
        unsubscribe = services.platform.time_service.register_listener(
            failure_policy.on_time_service_health
        )
        if callable(unsubscribe):
            self._listener_unsubscribers.append(unsubscribe)
        if not bool(
            services.platform.read_clock_health(services.platform.time_service).get(
                "ready",
                False,
            )
        ):
            oms.halt_system("time_sync_unhealthy_during_startup")
            raise RuntimeError(
                "Exchange clock became unhealthy while runtime components "
                "were being constructed"
            )

        risk_supervisor = self._own(
            "risk_supervisor",
            services.factories.independent_risk_supervisor_type(
                oms,
                config,
                risk_manager=risk,
            ),
        )
        if self.paper_trade and risk_supervisor.enabled:
            raise RuntimeError(
                "Paper Trade must not enable IndependentRiskSupervisor"
            )

        strategy = self._own(
            "strategy",
            services.factories.create_primary_strategy(engine, oms, config),
        )
        services.platform.logger.info(
            "[StrategyRegistry] "
            f"primary={strategy.model_key} strategy_id={strategy.name} "
            f"registered={','.join(strategy.registered_models)} "
            "execution_policy=single_primary"
        )
        failure_policy.bind_strategy(strategy.name)
        engine.set_failure_handler(failure_policy.on_event_engine_failure)
        self._own(
            "strategy_runtime",
            services.factories.strategy_runtime_type(
                strategy,
                config.get("system", {}).get("strategy_runtime", {}),
                start_thread=False,
                failure_callback=(
                    failure_policy.on_strategy_runtime_failure
                ),
            ),
        )
        self._assemble_observability_services()

    def _assemble_observability_services(self) -> None:
        services = self.services
        config = self.config
        data_config = config.get("data_recorder", {}) or {}
        if not isinstance(data_config, dict):
            raise ValueError("data_recorder must be an object")
        data_recorder = None
        if config.get("record_data", False):
            data_recorder = services.factories.data_recorder_type(
                self.engine,
                config["symbols"],
                save_path=str(data_config.get("save_path", "storage") or "storage"),
                flush_threshold=data_config.get("flush_threshold", 500),
                queue_capacity=data_config.get("queue_capacity", 8192),
                close_timeout_sec=data_config.get("close_timeout_sec", 30.0),
                min_free_bytes=data_config.get("min_free_bytes", 0),
                process_niceness=data_config.get("process_niceness", 0),
            )
        self._own("data_recorder", data_recorder)

        resource_config = (
            config.get("system", {}).get("resource_monitor", {}) or {}
        )
        if not isinstance(resource_config, dict):
            raise ValueError("system.resource_monitor must be an object")
        self._own(
            "resource_monitor",
            services.factories.process_resource_monitor_type(resource_config),
        )
        self.readiness_components["resources"] = True

        evidence = None
        if not self.paper_trade:
            journal_snapshot = getattr(
                getattr(self.oms, "journal", None),
                "health_snapshot",
                None,
            )
            if not callable(journal_snapshot):
                raise RuntimeError(
                    "OMS journal health snapshot is required for Live evidence"
                )
            evidence = services.factories.live_evidence_recorder_type(
                self.engine,
                config,
                failure_callback=(
                    self.failure_policy.on_live_evidence_failure
                ),
                oms_journal_snapshot=journal_snapshot,
            )
        self._own("live_evidence_recorder", evidence)
        recorder = (
            services.factories.recorder_group_type(self.data_recorder, evidence)
            if evidence is not None
            else self.data_recorder
        )
        self._own("recorder", recorder)

        self._own(
            "truth_monitor",
            services.factories.truth_monitor_type(
                self.oms,
                self.truth_provider,
                config,
                start_thread=False,
            ),
        )
        self._own(
            "venue_supervisor",
            services.factories.venue_supervisor_type(
                self.oms,
                self.gateway,
                config,
                start_thread=False,
            ),
        )
        self._own(
            "admin_control",
            services.factories.admin_control_server_type(
                self.oms,
                config,
                risk_manager=self.risk_controller,
                risk_supervisor=self.risk_supervisor,
            ),
        )

        self.web_dashboard_config = (
            config.get("system", {}).get("web_dashboard", {}) or {}
        )
        dashboard = None
        if bool(self.web_dashboard_config.get("enabled", True)):
            dashboard = services.factories.local_web_dashboard_type(
                oms=self.oms,
                gateway=self.gateway,
                risk_manager=self.risk_controller,
                risk_supervisor=self.risk_supervisor,
                config=config,
                time_service=services.platform.time_service,
                truth_monitor=self.truth_monitor,
                venue_supervisor=self.venue_supervisor,
                event_engine=self.engine,
                strategy_runtime=self.strategy_runtime,
            )
            services.platform.logger.set_ui_callback(dashboard.add_log)
        self._own("web_dashboard", dashboard)
        services.factories.ref_data_manager.init(testnet=config["testnet"])

    def _register_events_and_start_core(self) -> None:
        services = self.services
        watchdog_state = self._own(
            "watchdog_state",
            services.factories.watchdog_state_type(),
        )
        bindings = self._own(
            "event_bindings",
            services.factories.event_bindings_type(
                engine=self.engine,
                oms=self.oms,
                strategy_runtime=self.strategy_runtime,
                risk_controller=self.risk_controller,
                risk_supervisor=self.risk_supervisor,
                web_dashboard=self.web_dashboard,
                external_alerts=self.external_alerts,
                watchdog_state=watchdog_state,
            ),
        )
        bindings.register_all()
        self.engine.start()

        supervisor_started = False
        try:
            supervisor_started = bool(self.risk_supervisor.start())
        finally:
            process = getattr(self.risk_supervisor, "process", None)
            process_alive = bool(
                process is not None and process.is_alive()
            )
            self.resources.risk_supervisor_started = bool(
                self.risk_supervisor.enabled
                and (supervisor_started or process_alive)
            )
        if not supervisor_started:
            raise RuntimeError("IndependentRiskSupervisor failed to start")

    def _connect_execution_transport(self) -> None:
        live_stage = str(
            (self.config.get("live_launch", {}) or {}).get("stage", "")
            or ""
        ).strip().lower()
        dashboard_required = bool(
            not self.paper_trade
            and live_stage in {"canary", "rpi_calibration_canary"}
        )
        self.services.safety.start_local_dashboard(
            self.web_dashboard,
            self.web_dashboard_config,
            required=dashboard_required,
        )
        connected = self.services.safety.connect_gateway_with_risk_heartbeat(
            self.gateway,
            self.config["symbols"],
            self.risk_supervisor,
        )
        if connected is not True:
            self.readiness_components["transport"] = False
            raise RuntimeError(
                "Gateway failed to reach transport-and-book readiness"
            )
        self.readiness_components["transport"] = True
        self.resources.account_shutdown_proof_required = True
        commission_truth = self.services.safety.synchronize_commission_config(
            self.gateway,
            self.config,
            self.config["symbols"],
        )
        if not self.paper_trade:
            self.services.safety.validate_live_rpi_policy(
                self.config,
                self.config["symbols"],
                {
                    str(symbol or "").upper(): (
                        self.services.factories.ref_data_manager.supports_rpi(symbol)
                    )
                    for symbol in self.config["symbols"]
                },
                commission_truth["rpi_commission_rates"],
            )

    def _pass_startup_gates(self) -> None:
        self._warm_up_risk_services()
        risk_supervisor_ready = self.risk_supervisor.wait_until_healthy(
            timeout_sec=2.0
        )
        self.readiness_components["risk_supervisor"] = bool(
            risk_supervisor_ready
        )
        if not risk_supervisor_ready:
            self._halt_and_raise(
                "independent_risk_supervisor_unavailable",
                "IndependentRiskSupervisor startup health gate failed",
            )
        truth_ready = self.services.safety.wait_for_initial_truth_snapshot(
            self.truth_monitor,
            self.risk_supervisor,
        )
        self.readiness_components["truth"] = bool(truth_ready)
        if not truth_ready:
            self._halt_and_raise(
                "initial_truth_snapshot_unhealthy",
                "Initial exchange truth snapshot health gate failed",
            )
        self._validate_live_account_start_truth()
        self._require_risk_alert_and_evidence_health("before")
        clock_ready = bool(
            self.services.platform.read_clock_health(
                self.services.platform.time_service
            ).get("ready", False)
        )
        self.readiness_components["clock"] = clock_ready
        if not clock_ready:
            self._halt_and_raise(
                "time_sync_unhealthy_before_oms_bootstrap",
                "Exchange clock health gate failed immediately before "
                "OMS bootstrap",
            )
        market_data_ready = (
            self.services.safety.wait_for_initial_market_data_readiness(
                self.risk_controller,
                self.risk_supervisor,
            )
        )
        self.readiness_components["market_data"] = bool(market_data_ready)
        if not market_data_ready:
            self._halt_and_raise(
                "initial_market_data_unready",
                "Initial market-data readiness gate failed before OMS "
                "bootstrap",
            )

        self.risk_controller.resume_kill_switch_supervision()
        bootstrap_ready = self.services.safety.bootstrap_or_rearm(
            self.oms,
            auto_rearm=bool(self.args.rearm),
            rearm_reason=str(self.args.rearm_reason or "cli"),
            risk_manager=self.risk_controller,
            risk_supervisor=self.risk_supervisor,
        )
        if not bootstrap_ready:
            self.readiness_components["oms"] = False
            raise RuntimeError(
                "OMS bootstrap/rearm did not reach an executable state"
            )
        self.readiness_components["oms"] = True
        self._require_risk_alert_and_evidence_health("after")
        readiness = self._evaluate_readiness(
            "startup_gates",
            require_execution=False,
        )
        if not readiness.ready:
            self._halt_and_raise(
                "runtime_readiness_gate_failed",
                "Runtime readiness gate failed: "
                + ", ".join(readiness.reasons),
            )

    def _warm_up_risk_services(self) -> None:
        policy = getattr(self.risk_controller, "funding_guard_policy", None)
        enabled = bool(getattr(policy, "enabled", False))
        recovery_updates = (
            int(getattr(policy, "recovery_updates", 0) or 0)
            if enabled
            else 0
        )
        hold_sec = (
            float(getattr(policy, "post_funding_hold_sec", 0.0) or 0.0)
            if enabled
            else 0.0
        )
        deadline = self.services.platform.monotonic() + max(
            3.0,
            hold_sec + 2.0 + recovery_updates * 1.5,
        )
        while self.services.platform.monotonic() < deadline:
            self.systemd_watchdog.pulse()
            self.services.safety.run_live_risk_checks(
                self.risk_controller,
                self.risk_supervisor,
            )
            self.services.platform.sleep(0.1)

    def _validate_live_account_start_truth(self) -> None:
        if self.paper_trade:
            self.readiness_components["account_truth"] = True
            return
        try:
            self.services.safety.validate_live_account_equity_truth(
                self.config,
                self.truth_monitor.last_account_snapshot,
            )
            self.services.safety.validate_live_flat_start_truth(
                self.truth_monitor.last_positions_snapshot,
                self.truth_monitor.last_open_orders_snapshot,
            )
            self.readiness_components["account_truth"] = True
        except (TypeError, ValueError) as exc:
            self.readiness_components["account_truth"] = False
            self.oms.halt_system("live_account_start_truth_rejected")
            raise RuntimeError(
                f"Live account start truth rejected: {exc}"
            ) from exc

    def _require_risk_alert_and_evidence_health(self, phase: str) -> None:
        risk_ready = self.services.safety.run_live_risk_checks(
            self.risk_controller,
            self.risk_supervisor,
        )
        self.readiness_components["risk_policy"] = bool(risk_ready)
        if not risk_ready:
            self._halt_and_raise(
                "startup_risk_health_check_failed",
                f"Startup risk health gate failed {phase} OMS bootstrap",
            )
        alert_health = (
            self.services.safety.external_alert_health(self.external_alerts)
            if self.external_alerts is not None
            else {"healthy": True}
        )
        alert_ready = bool(alert_health.get("healthy", False))
        self.readiness_components["alerts"] = alert_ready
        if not alert_ready:
            self._halt_and_raise(
                f"external_alert_channel_unhealthy_{phase}_bootstrap",
                f"External alert health gate failed {phase} OMS bootstrap",
            )
        if not self.paper_trade:
            evidence_ready = self.services.safety.enforce_live_evidence_health(
                self.live_evidence_recorder,
                self.oms,
            )
            self.readiness_components["evidence"] = bool(evidence_ready)
            if not evidence_ready:
                raise RuntimeError(
                    f"Live evidence health gate failed {phase} OMS bootstrap"
                )

    def _halt_and_raise(self, halt_reason: str, message: str) -> None:
        self.oms.halt_system(halt_reason)
        raise RuntimeError(message)

    def _activate_execution(self) -> None:
        self.failure_policy.arm_clock_runtime()
        self.truth_monitor.start()
        self.venue_supervisor.start()
        self.strategy_runtime.start()

        execution_enabled = bool(
            getattr(self.gateway, "active", False)
            and getattr(self.strategy_runtime, "_active", False)
        )
        self.readiness_components["execution"] = execution_enabled
        readiness = self._evaluate_readiness(
            "runtime",
            require_execution=True,
        )
        if self.web_dashboard is not None:
            self.web_dashboard.set_startup_status(
                state="RUNNING" if readiness.ready else "STARTUP_DEGRADED",
                operating_mode=readiness.operating_mode,
                startup_blocked=False,
                execution_enabled=execution_enabled,
                restart_required=False,
                reason="; ".join(readiness.reasons)
                or str(getattr(self.oms, "capability_reason", "") or ""),
            )
            self.web_dashboard.publish_snapshot(force=True)

        mode = "PAPER / LIVE DATA" if self.paper_trade else "LIVE / REAL MONEY"
        self.services.platform.logger.info(f"ChronosHFT Core Engine {mode}.")

    def _run_control_loop(self):
        services = self.services
        control_services = services.loop.runtime_control_services_type(
            enforce_external_alert_health=(
                services.safety.enforce_external_alert_health
            ),
            enforce_live_evidence_health=(
                services.safety.enforce_live_evidence_health
            ),
            run_live_risk_checks=services.safety.run_live_risk_checks,
            external_alert_health=services.safety.external_alert_health,
            live_evidence_health=services.safety.live_evidence_health,
            build_runtime_resource_event=(
                services.loop.build_runtime_resource_event
            ),
        )
        loop = self._own(
            "control_loop",
            services.loop.runtime_control_loop_type(
                config=self.config,
                paper_trade=self.paper_trade,
                systemd_watchdog=self.systemd_watchdog,
                external_alerts=self.external_alerts,
                live_evidence_recorder=self.live_evidence_recorder,
                oms=self.oms,
                risk_controller=self.risk_controller,
                risk_supervisor=self.risk_supervisor,
                engine=self.engine,
                event_engine_config=self.event_engine_config,
                gateway=self.gateway,
                strategy=self.strategy,
                strategy_runtime=self.strategy_runtime,
                data_recorder=self.data_recorder,
                resource_monitor=self.resource_monitor,
                event_bindings=self.event_bindings,
                watchdog_state=self.watchdog_state,
                web_dashboard=self.web_dashboard,
                admin_control=self.admin_control,
                logger=services.platform.logger,
                services=control_services,
                readiness_evaluator=self.readiness_evaluator,
                readiness_components=self.readiness_components,
                readiness_required=self.readiness_required,
                clock_service=services.platform.time_service,
                read_clock_health=services.platform.read_clock_health,
            ),
        )
        return loop.run_forever()
