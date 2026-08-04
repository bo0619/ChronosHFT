from types import SimpleNamespace

from infrastructure.runtime_application import RuntimeApplication


class _StageApplication(RuntimeApplication):
    def __init__(self, resources, services):
        super().__init__(resources, services)
        self.calls = []

    def _load_configuration(self):
        self.calls.append("configuration")
        return None

    def _initialize_process_services(self):
        self.calls.append("process_services")
        return None

    def _assemble_runtime_graph(self):
        self.calls.append("assembly")

    def _register_events_and_start_core(self):
        self.calls.append("core_start")

    def _connect_execution_transport(self):
        self.calls.append("transport")

    def _pass_startup_gates(self):
        self.calls.append("startup_gates")

    def _activate_execution(self):
        self.calls.append("activation")

    def _run_control_loop(self):
        self.calls.append("control_loop")
        return 17


def _services(args):
    return SimpleNamespace(
        configuration=SimpleNamespace(
            parse_cli_args=lambda _argv: args,
        )
    )


def test_run_exposes_ordered_independently_overridable_startup_phases():
    args = SimpleNamespace(admin_command=None)
    app = _StageApplication({}, _services(args))

    assert app.run(["--unused"]) == 17
    assert app.calls == [
        "configuration",
        "process_services",
        "assembly",
        "core_start",
        "transport",
        "startup_gates",
        "activation",
        "control_loop",
    ]


def test_configuration_short_circuit_constructs_no_runtime_components():
    args = SimpleNamespace(admin_command=None)

    class ConfigOnlyApplication(_StageApplication):
        def _load_configuration(self):
            self.calls.append("configuration")
            return 0

    app = ConfigOnlyApplication({}, _services(args))

    assert app.run(["--check-config"]) == 0
    assert app.calls == ["configuration"]


def test_admin_command_bypasses_configuration_and_assembly():
    args = SimpleNamespace(admin_command="status")

    class AdminApplication(_StageApplication):
        def _run_admin_command(self):
            self.calls.append("admin")
            return 0

    app = AdminApplication({}, _services(args))

    assert app.run(["--admin-command", "status"]) == 0
    assert app.calls == ["admin"]


def test_owned_resources_remain_visible_to_partial_startup_cleanup():
    backing = {}
    app = RuntimeApplication(backing, SimpleNamespace())
    gateway = object()

    assert app._own("gateway", gateway) is gateway
    assert app.gateway is gateway
    assert app.resources.gateway is gateway
    assert backing["gateway"] is gateway
