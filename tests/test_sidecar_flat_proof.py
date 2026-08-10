from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, replace

import pytest

from risk.exchange_port import (
    AccountTruthSnapshot,
    StateVersion,
    TruthResult,
)
from risk.sidecar_command_runtime import rearm_proof_binding
from risk.sidecar_core import RiskSidecarCore
from risk.sidecar_flat_proof import FlatProofEngine, FlatProofError
from risk.sidecar_state_store import SidecarStateStore, SidecarStateStoreError

STATE_DIGEST = "a" * 64


def _snapshot(
    sequence: int,
    *,
    account_scope_id: str = "account-1",
    positions=(),
    open_orders=(),
) -> AccountTruthSnapshot:
    return AccountTruthSnapshot(
        account_scope_id=account_scope_id,
        truth_sequence=sequence,
        captured_monotonic=10.0 + sequence,
        captured_utc_ms=1_700_000_000_000 + sequence,
        orders_scope="ACCOUNT_WIDE",
        positions_scope="ACCOUNT_WIDE",
        complete=True,
        consistency_digest=f"digest-{sequence}",
        positions=tuple(positions),
        open_orders=tuple(open_orders),
    )


class _TruthExchange:
    def __init__(self, *results: TruthResult):
        self.results = list(results)

    def read_account_truth(self, _purpose):
        return self.results.pop(0)


def _version(**changes) -> StateVersion:
    values = {
        "writer_epoch": 3,
        "owner_epoch": 4,
        "safety_epoch": 5,
        "generation": 6,
        "state_sha256": STATE_DIGEST,
    }
    values.update(changes)
    return StateVersion(**values)


def _capture_engine(*snapshots: AccountTruthSnapshot) -> FlatProofEngine:
    return FlatProofEngine(
        _TruthExchange(*(TruthResult(True, item) for item in snapshots)),
        expected_account_scope_id="account-1",
        allowed_symbols=("BTCUSDT",),
        monotonic=lambda: 20.0,
        proof_id_factory=lambda: "proof-1",
    )


def test_flat_proof_binds_complete_state_version() -> None:
    version = _version()
    proof = _capture_engine(_snapshot(1), _snapshot(2)).capture(
        purpose="REARM",
        deployment_id="deployment-1",
        version=version,
        barrier_monotonic=0.0,
    )

    assert proof.is_valid(20.0, version)
    assert not proof.is_valid(20.0, _version(generation=7))
    assert not proof.is_valid(20.0, _version(safety_epoch=6))
    assert not proof.is_valid(23.0, version)


def test_flat_proof_rejects_unknown_account_symbol() -> None:
    engine = _capture_engine(
        _snapshot(
            1,
            positions=({"symbol": "ETHUSDT", "positionAmt": "0"},),
        )
    )

    with pytest.raises(FlatProofError, match="unknown_symbol:ETHUSDT"):
        engine.capture(
            purpose="REARM",
            deployment_id="deployment-1",
            version=_version(),
            barrier_monotonic=0.0,
        )


def test_flat_proof_rejects_account_identity_drift() -> None:
    engine = _capture_engine(
        _snapshot(1),
        _snapshot(2, account_scope_id="account-2"),
    )

    with pytest.raises(FlatProofError, match="account_scope_mismatch"):
        engine.capture(
            purpose="STOP",
            deployment_id="deployment-1",
            version=_version(),
            barrier_monotonic=0.0,
        )


def test_flat_proof_propagates_exchange_query_failure() -> None:
    engine = FlatProofEngine(
        _TruthExchange(TruthResult(False, reason="positions_query_failed")),
        expected_account_scope_id="account-1",
    )

    with pytest.raises(FlatProofError, match="positions_query_failed"):
        engine.capture(
            purpose="ORPHAN_EXIT",
            deployment_id="deployment-1",
            version=_version(),
            barrier_monotonic=0.0,
        )


class _ActionExchange:
    def get_risk_snapshot(self):
        return True, {
            "account": {
                "totalMaintMargin": "0",
                "totalMarginBalance": "1000",
            },
            "positions": [],
            "open_orders": [],
        }, ""

    def emergency_cancel(self, _symbols, _countdown_time_ms):
        return True, ""

    def emergency_flatten(self):
        return True, 0, ""


@pytest.mark.parametrize("action", ("_emergency_cancel", "_emergency_flatten"))
def test_any_exchange_action_invalidates_previous_proof(action: str) -> None:
    core = RiskSidecarCore(
        _ActionExchange(),
        {"symbols": ["BTCUSDT"], "deployment_id": "deployment-1"},
        now=10.0,
    )
    proof = _capture_engine(_snapshot(1), _snapshot(2)).capture(
        purpose="REARM",
        deployment_id="deployment-1",
        version=core.state_version,
        barrier_monotonic=0.0,
    )
    core.last_flat_proof = proof

    getattr(core, action)(11.0)

    assert core.last_flat_proof is None
    assert core._pending_safety_epoch == core.state_version.safety_epoch + 1


def test_rearm_binding_rejects_version_change() -> None:
    core = RiskSidecarCore(
        _ActionExchange(),
        {"symbols": ["BTCUSDT"], "deployment_id": "deployment-1"},
        now=10.0,
    )
    proof = _capture_engine(_snapshot(1), _snapshot(2)).capture(
        purpose="REARM",
        deployment_id="deployment-1",
        version=core.state_version,
        barrier_monotonic=0.0,
    )
    core.last_flat_proof = proof
    assert rearm_proof_binding(core, 20.0) is not None

    core.state_version = replace(core.state_version, generation=1)

    assert rearm_proof_binding(core, 20.0) is None


class _CrashAfterDispatchExchange(_ActionExchange):
    def __init__(self):
        self.core = None
        self.dispatched_versions = []

    def _crash(self):
        self.dispatched_versions.append(self.core.state_version)
        raise KeyboardInterrupt("simulated_process_crash")

    def emergency_cancel(self, _symbols, _countdown_time_ms):
        self._crash()

    def emergency_flatten(self):
        self._crash()


@pytest.mark.parametrize("action", ("_emergency_cancel", "_emergency_flatten"))
def test_exchange_action_commits_safety_epoch_before_dispatch(
    tmp_path,
    action: str,
) -> None:
    SidecarStateStore.provision(
        tmp_path,
        account_scope_id="account-1",
        deployment_id="deployment-1",
        genesis_id="genesis-1",
        initial_payload={
            "schema_version": 2,
            "kill_latched": True,
            "stage": "KILL",
        },
    )
    settings = {
        "symbols": ["BTCUSDT"],
        "deployment_id": "deployment-1",
        "state_store_root": str(tmp_path),
        "account_scope_id": "account-1",
        "state_genesis_id": "genesis-1",
    }
    exchange = _CrashAfterDispatchExchange()
    core = RiskSidecarCore(exchange, settings, now=10.0)
    exchange.core = core
    base_version = core.state_version
    core.last_flat_proof = _capture_engine(
        _snapshot(1),
        _snapshot(2),
    ).capture(
        purpose="REARM",
        deployment_id="deployment-1",
        version=base_version,
        barrier_monotonic=0.0,
    )
    try:
        with pytest.raises(KeyboardInterrupt, match="simulated_process_crash"):
            getattr(core, action)(11.0)
        dispatched = exchange.dispatched_versions[-1]
        assert dispatched.safety_epoch == base_version.safety_epoch + 1
        assert dispatched.generation == base_version.generation + 1
        assert core.last_flat_proof is None
    finally:
        core.close()

    recovered = RiskSidecarCore(_ActionExchange(), settings, now=12.0)
    try:
        assert recovered.state_version.safety_epoch == dispatched.safety_epoch
        assert recovered.last_flat_proof is None
    finally:
        recovered.close()


def test_recovery_durably_invalidates_proof_from_previous_writer_epoch(
    tmp_path,
) -> None:
    proof = _capture_engine(_snapshot(1), _snapshot(2)).capture(
        purpose="ORPHAN_EXIT",
        deployment_id="deployment-1",
        version=_version(),
        barrier_monotonic=0.0,
    )
    SidecarStateStore.provision(
        tmp_path,
        account_scope_id="account-1",
        deployment_id="deployment-1",
        genesis_id="genesis-1",
        initial_payload={
            "schema_version": 2,
            "kill_latched": True,
            "stage": "FLAT_VERIFIED",
            "last_flat_proof": asdict(proof),
        },
    )
    settings = {
        "symbols": ["BTCUSDT"],
        "deployment_id": "deployment-1",
        "state_store_root": str(tmp_path),
        "account_scope_id": "account-1",
        "state_genesis_id": "genesis-1",
    }

    core = RiskSidecarCore(_ActionExchange(), settings, now=21.0)
    try:
        assert core.last_flat_proof is None
        assert core.last_flat_proof_error == (
            "flat_proof_invalidated_by_writer_epoch"
        )
        assert core.state_generation == 2
    finally:
        core.close()

    with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
        event, payload_json = connection.execute(
            "SELECT last_event, payload_json FROM state_head WHERE singleton=1"
        ).fetchone()
    assert event == "flat_proof_invalidated_on_recovery"
    assert json.loads(payload_json)["last_flat_proof"] is None


def test_state_store_rejects_flat_proof_for_another_account(tmp_path) -> None:
    proof = _capture_engine(_snapshot(1), _snapshot(2)).capture(
        purpose="STOP",
        deployment_id="deployment-1",
        version=_version(),
        barrier_monotonic=0.0,
    )
    foreign_proof = replace(proof, account_scope_id="account-2")

    with pytest.raises(
        SidecarStateStoreError,
        match="state_payload_flat_proof_account_scope_mismatch",
    ):
        SidecarStateStore.provision(
            tmp_path,
            account_scope_id="account-1",
            deployment_id="deployment-1",
            genesis_id="genesis-1",
            initial_payload={
                "schema_version": 2,
                "kill_latched": True,
                "stage": "FLAT_VERIFIED",
                "last_flat_proof": asdict(foreign_proof),
            },
        )
