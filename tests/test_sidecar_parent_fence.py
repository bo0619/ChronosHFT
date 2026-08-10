from __future__ import annotations

import multiprocessing
import threading
import time
from pathlib import Path

import pytest

from risk.sidecar_state_store import AccountWriterFence, SidecarStateStore
from risk.sidecar_supervisor import SidecarParentSupervisor


def _hold_fence(path: str, ready, release) -> None:
    fence = AccountWriterFence(Path(path), {"worker": "orphan"})
    fence.acquire()
    ready.set()
    release.wait(10.0)
    fence.release()


class _Oms:
    def __init__(self) -> None:
        self.heartbeats = []
        self.constraints = []

    def record_risk_control_heartbeat(self, **payload) -> bool:
        self.heartbeats.append(payload)
        return bool(payload["healthy"])

    def set_trading_mode(self, _mode, reason: str) -> None:
        self.constraints.append(reason)


def _provision(root: Path) -> None:
    SidecarStateStore.provision(
        root,
        account_scope_id="account-1",
        deployment_id="deployment-1",
        genesis_id="genesis-1",
        initial_payload={
            "schema_version": 2,
            "kill_latched": True,
            "stage": "KILL",
            "cash_flow_deployment_start_ms": 1_700_000_000_000,
        },
    )


def _config(root: Path, *, wait_timeout: float = 1.0) -> dict:
    return {
        "symbols": ["BTCUSDT"],
        "live_launch": {"deployment_id": "deployment-1"},
        "risk": {
            "risk_control_heartbeat": {
                "enabled": True,
                "required_source": "independent_supervisor",
            },
            "independent_supervisor": {
                "enabled": True,
                "api_key": "risk-key",
                "api_secret": "risk-secret",
                "state_store_root": str(root),
                "account_scope_id": "account-1",
                "state_genesis_id": "genesis-1",
                "cash_flow_deployment_start_ms": 1_700_000_000_000,
                "writer_fence_wait_timeout_sec": wait_timeout,
                "writer_fence_poll_interval_sec": 0.01,
            },
        },
    }


def _supervisor(root: Path, *, wait_timeout: float = 1.0):
    oms = _Oms()
    supervisor = SidecarParentSupervisor(
        oms,
        _config(root, wait_timeout=wait_timeout),
        process_target=lambda *_args: None,
    )
    return supervisor, oms


def test_account_scope_rejects_second_parent(tmp_path: Path) -> None:
    _provision(tmp_path)
    first, _ = _supervisor(tmp_path)
    second, second_oms = _supervisor(tmp_path)
    first._acquire_account_writer_fences()
    try:
        with pytest.raises(RuntimeError, match="runtime_parent_writer_fence_held"):
            second._acquire_account_writer_fences()
        assert second.parent_writer_fence is None
        assert second_oms.constraints[-1].endswith(
            "runtime_parent_writer_fence_held"
        )
    finally:
        first._release_parent_writer_fence()


def test_parent_waits_for_orphan_sidecar_without_lock_stealing(
    tmp_path: Path,
) -> None:
    _provision(tmp_path)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_fence,
        args=(str(tmp_path / "risk-sidecar.writer.lock"), ready, release),
    )
    process.start()
    assert ready.wait(5.0)
    supervisor, oms = _supervisor(tmp_path)
    releaser = threading.Timer(0.15, release.set)
    releaser.start()
    started = time.perf_counter()
    try:
        supervisor._acquire_account_writer_fences()
        elapsed = time.perf_counter() - started
        assert elapsed >= 0.05
        assert supervisor.parent_writer_fence is not None
        assert oms.constraints[-1].endswith("risk_sidecar_writer_fence_wait")
    finally:
        release.set()
        releaser.cancel()
        process.join(5.0)
        if process.is_alive():
            process.terminate()
            process.join(5.0)
        supervisor._release_parent_writer_fence()
    assert process.exitcode == 0


def test_orphan_wait_timeout_releases_parent_fence(tmp_path: Path) -> None:
    _provision(tmp_path)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_fence,
        args=(str(tmp_path / "risk-sidecar.writer.lock"), ready, release),
    )
    process.start()
    assert ready.wait(5.0)
    supervisor, _ = _supervisor(tmp_path, wait_timeout=0.05)
    try:
        with pytest.raises(
            RuntimeError,
            match="risk_sidecar_writer_fence_wait_timeout",
        ):
            supervisor._acquire_account_writer_fences()
        assert supervisor.parent_writer_fence is None

        replacement = AccountWriterFence(
            tmp_path / "runtime-parent.writer.lock",
            {"worker": "replacement-parent"},
        )
        replacement.acquire()
        replacement.release()
    finally:
        release.set()
        process.join(5.0)
        if process.is_alive():
            process.terminate()
            process.join(5.0)
    assert process.exitcode == 0
