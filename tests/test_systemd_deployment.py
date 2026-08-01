from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UNIT_TEMPLATE = ROOT / "deploy" / "systemd" / "chronoshft.service.in"
INSTALLER = ROOT / "scripts" / "install_systemd_service.sh"


def test_systemd_unit_preserves_runtime_shutdown_and_restart_contract():
    unit = UNIT_TEMPLATE.read_text(encoding="utf-8")

    assert 'ExecStartPre="@PYTHON_BIN@" "@ROOT_DIR@/main.py"' in unit
    assert 'ExecStart="@PYTHON_BIN@" "@ROOT_DIR@/main.py"' in unit
    assert "launcher.py" not in unit
    assert "Restart=on-failure" in unit
    assert "RestartPreventExitStatus=2" in unit
    assert "NotifyAccess=main" in unit
    assert "WatchdogSec=60s" in unit
    assert "WatchdogSignal=SIGABRT" in unit
    assert "LimitCORE=0" in unit
    assert "TasksMax=128" in unit
    assert "MemoryHigh=1400M" in unit
    assert "MemoryMax=1600M" in unit
    assert "MemorySwapMax=0" in unit
    assert "OOMPolicy=stop" in unit
    assert "StartLimitIntervalSec=3600" in unit
    assert "StartLimitBurst=10" in unit
    assert "KillSignal=SIGINT" in unit
    assert "KillMode=mixed" in unit
    assert "TimeoutStopSec=120" in unit
    assert "Environment=OPENBLAS_NUM_THREADS=1" in unit
    assert "Environment=OMP_NUM_THREADS=1" in unit
    assert "Environment=MKL_NUM_THREADS=1" in unit
    assert "Environment=NUMEXPR_NUM_THREADS=1" in unit
    assert "Environment=PYTHONFAULTHANDLER=1" in unit
    assert "Environment=MALLOC_ARENA_MAX=2" in unit


def test_systemd_unit_is_loopback_dashboard_compatible_and_hardened():
    unit = UNIT_TEMPLATE.read_text(encoding="utf-8")

    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in unit
    assert "NoNewPrivileges=true" in unit
    assert "CapabilityBoundingSet=" in unit
    assert "ProtectSystem=full" in unit
    assert "ProtectClock=true" in unit
    assert "ProtectProc=invisible" in unit
    assert "ProcSubset=pid" in unit
    assert "RestrictNamespaces=true" in unit
    assert "PrivateDevices=true" in unit
    assert "ProtectHome=" not in unit


def test_installer_validates_paper_config_and_avoids_blind_double_start():
    installer = INSTALLER.read_text(encoding="utf-8")

    assert '[[ "${EUID}" -eq 0 ]]' in installer
    assert "systemd-analyze verify" in installer
    assert "verify_effective_unit" in installer
    assert "effective_property WatchdogUSec" in installer
    assert "effective_property TasksMax" in installer
    assert "effective_property MemoryHigh" in installer
    assert "effective_property MemoryMax" in installer
    assert "effective_property MemorySwapMax" in installer
    assert "--check-config" in installer
    assert '"CONFIG_OK mode=paper "' in installer
    assert "find_project_python_pids" in installer
    assert "project Python processes are already running" in installer
    assert "systemctl enable" in installer
    assert "systemctl start" in installer
    assert "Effective runtime limits were not checked" in installer
    assert installer.index("systemctl daemon-reload") < installer.index(
        "systemctl enable"
    )
    assert installer.index("systemctl enable") < installer.rindex(
        "verify_effective_unit"
    )
    assert "pkill" not in installer
