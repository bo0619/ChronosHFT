import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from infrastructure.config_scaling import load_config_document
from migration.runtime_state import (
    MigrationError,
    apply_migration_plan,
    build_migration_plan,
    inspect_sources,
    verify_migration_receipt,
)
from oms.journal import OMSJournal

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_migration_cli_supports_direct_script_invocation(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(REPOSITORY_ROOT / "scripts" / "migrate_runtime_state.py"),
            "--help",
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "{inspect,plan,apply,verify}" in result.stdout


def _canonical(value) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_v2_journal(path: Path) -> None:
    previous_hash = ""
    rows = (
        (
            "paper_run_started",
            {
                "paper_run_id": "migrated-run",
                "started_at_utc": "2026-01-01T00:00:00.000Z",
                "symbols": ["BTCUSDT"],
            },
        ),
        (
            "execution_record",
            {
                "paper_run_id": "migrated-run",
                "execution_id": "BINANCE:BTCUSDT:1",
                "venue": "BINANCE",
                "strategy_id": "alpha",
                "client_oid": "order-1",
                "exchange_oid": "exchange-1",
                "trade_id": 1,
                "symbol": "BTCUSDT",
                "side": "BUY",
                "fill_qty": 1.0,
                "fill_price": 100.0,
                "cum_filled_qty": 1.0,
                "exchange_status": "FILLED",
            },
        ),
    )
    encoded = []
    for sequence, (kind, payload) in enumerate(rows, start=1):
        unsigned = {
            "version": 2,
            "seq": sequence,
            "ts": f"2026-01-01T00:00:0{sequence}.000Z",
            "kind": kind,
            "payload": payload,
            "prev_hash": previous_hash,
        }
        previous_hash = hashlib.sha256(_canonical(unsigned)).hexdigest()
        encoded.append(json.dumps({**unsigned, "hash": previous_hash}))
    path.write_text("\n".join(encoded) + "\n", encoding="utf-8")


def _write_sidecar_v1(path: Path) -> None:
    payload = {
        "schema_version": 1,
        "generation": 9,
        "deployment_id": "old-deployment",
        "kill_latched": False,
        "stage": "ARMED",
        "deployment_start_equity": 10_000.0,
        "deployment_start_external_cash_flow_total": 500.0,
        "deployment_loss": 20.0,
    }
    path.write_text(
        json.dumps(
            {
                "payload": payload,
                "sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
            }
        ),
        encoding="utf-8",
    )


def _write_flat_proof(path: Path) -> None:
    proof = {
        "scope": "ACCOUNT_WIDE",
        "account_scope_id": "account-a",
        "proof_id": "proof-1",
        "open_order_count": 0,
        "nonzero_position_count": 0,
        "complete": True,
    }
    proof["proof_sha256"] = hashlib.sha256(_canonical(proof)).hexdigest()
    path.write_bytes(_canonical(proof) + b"\n")


def _write_legacy_config_manifest(root: Path) -> tuple[Path, Path]:
    root.mkdir()
    tracked = json.loads(
        (REPOSITORY_ROOT / "config/oms.json").read_text(encoding="utf-8")
    )
    legacy = {
        key: value
        for key, value in tracked.items()
        if key not in {"$schema", "fragment", "version"}
    }
    for field in (
        "journal_format_version",
        "journal_require_existing",
        "journal_segment_max_records",
        "journal_segment_max_bytes",
        "journal_max_frame_bytes",
    ):
        legacy["oms"].pop(field)
    fragment = root / "oms.json"
    fragment.write_text(json.dumps(legacy), encoding="utf-8")
    manifest = root / "config.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "chronoshft.config_manifest.v1",
                "includes": ["oms.json"],
            }
        ),
        encoding="utf-8",
    )
    return manifest, fragment


def test_end_to_end_offline_migration_is_digest_bound_and_atomic(tmp_path):
    sidecar = tmp_path / "sidecar-v1.json"
    journal = tmp_path / "journal-v2.jsonl"
    paper = tmp_path / "paper-v4.sqlite3"
    proof = tmp_path / "flat-proof.json"
    _write_sidecar_v1(sidecar)
    _write_v2_journal(journal)
    _write_flat_proof(proof)
    with sqlite3.connect(paper) as connection:
        connection.execute("PRAGMA user_version=4")

    source_hashes = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (sidecar, journal, paper, proof)
    }
    inspection = inspect_sources(
        sidecar_state=sidecar,
        journal=journal,
        paper_database=paper,
    )
    target = tmp_path / "migrated"
    plan = build_migration_plan(
        inspection,
        target_root=target,
        account_scope_id="account-a",
        deployment_id="new-deployment",
        cash_flow_deployment_start_ms=1_700_000_000_000,
        flat_proof_receipt=proof,
    )

    receipt = apply_migration_plan(
        plan,
        expected_plan_sha256=plan["plan_sha256"],
        backup_directory=tmp_path / "backup",
    )

    assert target.is_dir()
    assert not any(target.parent.glob(f".{target.name}.migration-*"))
    assert verify_migration_receipt(receipt)["valid"] is True
    assert all(
        hashlib.sha256(path.read_bytes()).hexdigest() == digest
        for path, digest in source_hashes.items()
    )
    migrated_journal = OMSJournal(
        {
            "oms": {
                "journal_path": str(target / "storage/oms/oms_journal"),
                "journal_require_existing": True,
            }
        }
    )
    assert len(migrated_journal.read_all()) == 2
    with sqlite3.connect(target / "storage/paper/trades.sqlite3") as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert connection.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0] == 1
    with sqlite3.connect(
        target / "storage/risk/accounts/account-a/state.sqlite3"
    ) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM state_head WHERE singleton=1"
            ).fetchone()[0]
        )
    assert payload["kill_latched"] is True
    assert payload["stage"] == "KILL"
    assert payload["deployment_baseline_pending"] is True


def test_apply_rejects_changed_source_before_creating_backup_or_target(tmp_path):
    journal = tmp_path / "journal-v2.jsonl"
    _write_v2_journal(journal)
    inspection = inspect_sources(journal=journal)
    target = tmp_path / "migrated"
    backup = tmp_path / "backup"
    plan = build_migration_plan(inspection, target_root=target)
    journal.write_text("changed", encoding="utf-8")

    with pytest.raises(MigrationError, match="changed after planning"):
        apply_migration_plan(
            plan,
            expected_plan_sha256=plan["plan_sha256"],
            backup_directory=backup,
        )

    assert not target.exists()
    assert not backup.exists()


def test_config_migration_generates_digest_bound_v3_manifest_and_fragments(
    tmp_path,
):
    manifest, fragment = _write_legacy_config_manifest(tmp_path / "legacy")
    source_digests = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (manifest, fragment)
    }
    inspection = inspect_sources(config_manifest=manifest)
    config_source = inspection["sources"]["config_manifest"]

    assert config_source["source_format"] == "v1-manifest"
    assert len(config_source["input_files"]) == 2
    assert config_source["target_manifest"]["schema"] == (
        "chronoshft.config_manifest.v3"
    )
    assert config_source["target_manifest"]["unknown_keys"] == "reject"

    target = tmp_path / "migrated"
    plan = build_migration_plan(inspection, target_root=target)
    receipt = apply_migration_plan(
        plan,
        expected_plan_sha256=plan["plan_sha256"],
        backup_directory=tmp_path / "backup",
    )

    generated = load_config_document(target / "config.json")
    assert generated["oms"]["journal_format_version"] == 3
    assert generated["oms"]["journal_require_existing"] is False
    assert generated["oms"]["journal_segment_max_records"] == 100_000
    assert generated["oms"]["journal_segment_max_bytes"] == 256 * 1024 * 1024
    assert generated["oms"]["journal_max_frame_bytes"] == 64 * 1024 * 1024
    assert verify_migration_receipt(receipt)["valid"] is True
    assert {
        artifact["path"] for artifact in receipt["artifacts"]
    } == {"config.json", "config/oms.json"}
    assert all(
        hashlib.sha256(path.read_bytes()).hexdigest() == digest
        for path, digest in source_digests.items()
    )


def test_config_migration_binds_every_legacy_include_before_apply(tmp_path):
    manifest, fragment = _write_legacy_config_manifest(tmp_path / "legacy")
    inspection = inspect_sources(config_manifest=manifest)
    target = tmp_path / "migrated"
    backup = tmp_path / "backup"
    plan = build_migration_plan(inspection, target_root=target)
    fragment.write_text("{}", encoding="utf-8")

    with pytest.raises(MigrationError, match="changed after planning"):
        apply_migration_plan(
            plan,
            expected_plan_sha256=plan["plan_sha256"],
            backup_directory=backup,
        )

    assert not target.exists()
    assert not backup.exists()


def test_v2_monolithic_config_generates_supervisor_identity_fragment(tmp_path):
    source = tmp_path / "config-v2.json"
    source.write_text(
        json.dumps(
            {
                "$schema": "chronoshft.config.v2",
                "config_version": 2,
                "risk": {
                    "independent_supervisor": {
                        "enabled": True,
                        "state_store_root": "storage/risk/accounts/account-a",
                        "account_scope_id": "account-a",
                        "state_genesis_id": "genesis-a",
                        "cash_flow_deployment_start_ms": 1_700_000_000_000,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    inspection = inspect_sources(config_manifest=source)
    generated = inspection["sources"]["config_manifest"]

    assert generated["source_format"] == "v2-monolithic"
    assert generated["target_manifest"]["includes"] == [
        {
            "path": "config/risk/independent_supervisor.json",
            "fragment": "risk.independent_supervisor",
            "version": 1,
        }
    ]
    supervisor = generated["target_fragments"][0]["document"]["risk"][
        "independent_supervisor"
    ]
    assert supervisor["state_genesis_id"] == "genesis-a"


def test_config_migration_rejects_unknown_fields_and_existing_v3(tmp_path):
    unknown = tmp_path / "unknown-v2.json"
    unknown.write_text(
        json.dumps(
            {
                "config_version": 2,
                "execution": {"mode": "paper"},
                "future_runtime": {"enabled": True},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(MigrationError, match="no strict v3 owner"):
        inspect_sources(config_manifest=unknown)

    current = tmp_path / "current-v3.json"
    current.write_text(
        json.dumps(
            {
                "schema": "chronoshft.config_manifest.v3",
                "config_version": 3,
                "unknown_keys": "reject",
                "includes": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(MigrationError, match="explicit legacy/v2 input"):
        inspect_sources(config_manifest=current)
