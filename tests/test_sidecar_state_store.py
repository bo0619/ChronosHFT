from __future__ import annotations

import multiprocessing
import shutil
import sqlite3
from pathlib import Path

import pytest

from risk.sidecar_state_store import (
    AccountWriterFence,
    SidecarStateCasError,
    SidecarStateStore,
    SidecarStateStoreError,
    SidecarWriterFenceError,
)


def _hold_fence(path: str, ready, release) -> None:
    fence = AccountWriterFence(Path(path), {"worker": "test"})
    fence.acquire()
    ready.set()
    release.wait(10.0)
    fence.release()


def _provision(root: Path) -> dict:
    return SidecarStateStore.provision(
        root,
        account_scope_id="account-scope-1",
        deployment_id="deployment-1",
        genesis_id="genesis-1",
        initial_payload={
            "schema_version": 2,
            "kill_latched": True,
            "stage": "KILL",
        },
    )


def _store(root: Path, writer_id: str = "writer-1") -> SidecarStateStore:
    return SidecarStateStore(
        root,
        account_scope_id="account-scope-1",
        deployment_id="deployment-1",
        genesis_id="genesis-1",
        writer_id=writer_id,
    )


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"unknown": True}, "state_payload_unknown_fields:unknown"),
        ({"schema_version": 3}, "state_payload_schema_unsupported"),
        (
            {"account_scope_id": "other-account"},
            "state_payload_account_scope_mismatch",
        ),
        (
            {"deployment_id": "other-deployment"},
            "state_payload_deployment_mismatch",
        ),
        ({"stage": "FUTURE"}, "state_payload_stage_unsupported"),
        (
            {"kill_latched": False, "stage": "FLATTENING"},
            "state_payload_kill_stage_mismatch",
        ),
        (
            {"stage": "QUIESCED", "quiesced": False},
            "state_payload_quiesce_stage_mismatch",
        ),
        ({"deployment_loss": -1.0}, "state_payload_deployment_loss_negative"),
    ],
)
def test_provision_rejects_invalid_v2_payload(
    tmp_path: Path,
    override: dict,
    reason: str,
) -> None:
    payload = {
        "schema_version": 2,
        "kill_latched": True,
        "stage": "KILL",
        **override,
    }
    with pytest.raises(SidecarStateStoreError, match=reason):
        SidecarStateStore.provision(
            tmp_path,
            account_scope_id="account-scope-1",
            deployment_id="deployment-1",
            genesis_id="genesis-1",
            initial_payload=payload,
        )


def test_cas_rejects_unknown_payload_without_advancing_state(
    tmp_path: Path,
) -> None:
    _provision(tmp_path)
    store = _store(tmp_path)
    payload, version = store.open_recover()

    with pytest.raises(
        SidecarStateStoreError,
        match="state_payload_unknown_fields:future_field",
    ):
        store.compare_and_swap(
            version,
            {**payload, "future_field": True},
            event="invalid_payload",
        )

    assert store.version == version
    store.close()


@pytest.mark.parametrize(
    "filename",
    ["risk-sidecar.writer.lock", "runtime-parent.writer.lock"],
)
def test_account_fences_are_single_writer_across_processes(
    tmp_path: Path,
    filename: str,
) -> None:
    _provision(tmp_path)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_fence,
        args=(str(tmp_path / filename), ready, release),
    )
    process.start()
    try:
        assert ready.wait(5.0)
        competing = AccountWriterFence(
            tmp_path / filename,
            {"worker": "competing"},
        )
        with pytest.raises(
            SidecarWriterFenceError,
            match="writer_fence_already_held",
        ):
            competing.acquire()
    finally:
        release.set()
        process.join(5.0)
        if process.is_alive():
            process.terminate()
            process.join(5.0)
    assert process.exitcode == 0


def test_orphan_exit_releases_sidecar_fence_without_lock_stealing(
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
    process.terminate()
    process.join(5.0)
    assert not process.is_alive()

    replacement = AccountWriterFence(
        tmp_path / "risk-sidecar.writer.lock",
        {"worker": "replacement"},
    )
    replacement.acquire()
    replacement.release()


def test_generation_cas_conflict_fails_closed(tmp_path: Path) -> None:
    _provision(tmp_path)
    store = _store(tmp_path)
    payload, stale = store.open_recover()
    current = store.compare_and_swap(stale, payload, event="first")

    with pytest.raises(SidecarStateCasError, match="state_cas_conflict"):
        store.compare_and_swap(stale, payload, event="stale")

    assert store.version == current
    store.close()


def test_fence_loss_rejects_further_state_writes(tmp_path: Path) -> None:
    _provision(tmp_path)
    store = _store(tmp_path)
    payload, version = store.open_recover()
    store.fence.release()

    with pytest.raises(
        SidecarWriterFenceError,
        match="writer_fence_not_held",
    ):
        store.compare_and_swap(version, payload, event="after_fence_loss")

    store.close()


@pytest.mark.parametrize(
    ("filename", "reason"),
    [
        ("state.sqlite3", "state_database_missing"),
        ("rollback-anchor.json", "rollback_anchor_missing"),
        ("cash-flow-anchor.json", "cash_flow_anchor_missing"),
        ("account.manifest.json", "manifest_missing"),
    ],
)
def test_required_state_artifact_deletion_rejects_startup(
    tmp_path: Path,
    filename: str,
    reason: str,
) -> None:
    _provision(tmp_path)
    (tmp_path / filename).unlink()

    with pytest.raises(SidecarStateStoreError, match=reason):
        _store(tmp_path).open_recover()


def test_single_file_database_rollback_is_detected(tmp_path: Path) -> None:
    _provision(tmp_path)
    backup = tmp_path / "old-state.sqlite3"
    shutil.copy2(tmp_path / "state.sqlite3", backup)
    store = _store(tmp_path)
    payload, version = store.open_recover()
    store.compare_and_swap(version, payload, event="advanced")
    store.close()
    shutil.copy2(backup, tmp_path / "state.sqlite3")

    with pytest.raises(
        SidecarStateStoreError,
        match="rollback_or_split_brain_suspected",
    ):
        _store(tmp_path, "writer-2").open_recover()


def test_hash_chain_tampering_is_detected_before_writer_epoch(tmp_path: Path) -> None:
    _provision(tmp_path)
    store = _store(tmp_path)
    payload, version = store.open_recover()
    store.compare_and_swap(version, payload, event="advanced")
    store.close()

    connection = sqlite3.connect(tmp_path / "state.sqlite3")
    try:
        connection.execute(
            "UPDATE state_history SET prev_state_sha256 = 'tampered' "
            "WHERE head_revision = 1"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        SidecarStateStoreError,
        match="state_history_chain_mismatch",
    ):
        _store(tmp_path, "writer-2").open_recover()


def _cash_event(event_id: str, event_time_ms: int, amount: float) -> dict:
    return {
        "event_id": event_id,
        "event_time_ms": event_time_ms,
        "asset": "USDT",
        "amount": amount,
        "raw_sha256": (event_id.encode("ascii").hex() + "0" * 64)[:64],
    }


def test_cash_flow_ledger_deduplicates_and_accepts_late_events(
    tmp_path: Path,
) -> None:
    _provision(tmp_path)
    store = _store(tmp_path)
    store.open_recover()
    first = _cash_event("transfer-1", 1_000, 100.0)
    generation = store.commit_cash_flow_refresh(
        [first, first],
        start_time_ms=0,
        complete_through_ms=2_000,
    )
    assert generation == 1
    assert store.cash_flow_total(0, 2_000) == 100.0

    generation = store.commit_cash_flow_refresh(
        [
            first,
            _cash_event("late-withdrawal", 500, -25.0),
        ],
        start_time_ms=0,
        complete_through_ms=3_000,
    )

    assert generation == 2
    assert store.cash_flow_total(0, 3_000) == 75.0
    assert store.cash_flow_cursor()["complete_through_ms"] == 3_000
    store.close()


def test_cash_flow_identity_collision_rolls_back_without_advancing_cursor(
    tmp_path: Path,
) -> None:
    _provision(tmp_path)
    store = _store(tmp_path)
    store.open_recover()
    event = _cash_event("transfer-1", 1_000, 100.0)
    store.commit_cash_flow_refresh(
        [event],
        start_time_ms=0,
        complete_through_ms=2_000,
    )
    before = store.cash_flow_cursor()
    conflicting = {**event, "amount": 99.0}

    with pytest.raises(
        SidecarStateStoreError,
        match="cash_flow_event_identity_collision",
    ):
        store.commit_cash_flow_refresh(
            [conflicting],
            start_time_ms=0,
            complete_through_ms=3_000,
        )

    assert store.cash_flow_cursor() == before
    assert store.cash_flow_total(0, 3_000) == 100.0
    store.close()


def test_cash_flow_database_rollback_is_detected_by_independent_anchor(
    tmp_path: Path,
) -> None:
    _provision(tmp_path)
    store = _store(tmp_path)
    store.open_recover()
    store.commit_cash_flow_refresh(
        [_cash_event("transfer-1", 1_000, 100.0)],
        start_time_ms=0,
        complete_through_ms=2_000,
    )
    store.close()

    store = _store(tmp_path, "writer-2")
    store.open_recover()
    backup = tmp_path / "cash-flow-old.sqlite3"
    shutil.copy2(tmp_path / "state.sqlite3", backup)
    store.commit_cash_flow_refresh(
        [_cash_event("withdrawal-1", 2_500, -10.0)],
        start_time_ms=0,
        complete_through_ms=3_000,
    )
    store.close()
    shutil.copy2(backup, tmp_path / "state.sqlite3")

    with pytest.raises(
        SidecarStateStoreError,
        match="cash_flow_rollback_suspected",
    ):
        _store(tmp_path, "writer-3").open_recover()
