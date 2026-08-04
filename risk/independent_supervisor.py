import os
import signal
import time

from risk.binance_sidecar_exchange import (
    BinanceRiskSidecarExchange,
    _RiskSnapshotWorker,
)
from risk.sidecar_core import RiskSidecarCore
from risk.sidecar_process import SidecarProcessBootstrap
from risk.sidecar_runtime import SidecarRuntime
from risk.sidecar_supervisor import SidecarParentSupervisor
from risk.sidecar_transport import SidecarTransport


def _isolate_sidecar_console_interrupts() -> None:
    if os.name == "nt":
        # The parent owns coordinated shutdown for the shared Windows console.
        signal.signal(signal.SIGINT, signal.SIG_IGN)

def _put_latest(target_queue, payload):
    return SidecarTransport.put_latest(target_queue, payload)


def run_sidecar_loop(
    command_queue,
    status_queue,
    settings: dict,
    exchange,
    snapshot_exchange=None,
    heartbeat_queue=None,
):
    SidecarRuntime.run(
        command_queue,
        status_queue,
        settings,
        exchange,
        snapshot_exchange=snapshot_exchange,
        heartbeat_queue=heartbeat_queue,
        snapshot_worker_factory=_RiskSnapshotWorker,
        core_factory=RiskSidecarCore,
        isolated_exchange_type=BinanceRiskSidecarExchange,
        put_latest=_put_latest,
        perf_counter=time.perf_counter,
        wall_time=time.time,
        getpid=os.getpid,
        sleep=time.sleep,
    )


def _risk_sidecar_process(
    command_queue,
    status_queue,
    settings: dict,
    heartbeat_queue=None,
):
    SidecarProcessBootstrap.run(
        command_queue,
        status_queue,
        settings,
        heartbeat_queue,
        isolate_console_interrupts=_isolate_sidecar_console_interrupts,
        exchange_factory=BinanceRiskSidecarExchange,
        run_loop=run_sidecar_loop,
        put_latest=_put_latest,
        getpid=os.getpid,
        wall_time=time.time,
    )


class IndependentRiskSupervisor(SidecarParentSupervisor):
    """Compatibility entry point wired to this module's process target."""

    def __init__(self, oms, config: dict, risk_manager=None):
        super().__init__(
            oms,
            config,
            risk_manager,
            process_target=_risk_sidecar_process,
        )
