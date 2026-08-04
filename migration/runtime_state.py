"""Offline-only, digest-bound migration of runtime durable state."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Mapping

from oms.journal import OMSJournal, decode_legacy_journal
from oms.paper_trade_database import PaperTradeDatabase
from risk.sidecar_state_store import SidecarStateStore


PLAN_SCHEMA = "chronoshft.runtime-migration-plan.v1"
RECEIPT_SCHEMA = "chronoshft.runtime-migration-receipt.v1"


class MigrationError(RuntimeError):
    """Raised when an offline migration cannot be proven safe."""


def _canonical_bytes(value: Mapping) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _document_digest(value: Mapping, field: str) -> str:
    unsigned = dict(value)
    unsigned.pop(field, None)
    return hashlib.sha256(_canonical_bytes(unsigned)).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _absolute_existing_file(value: str | os.PathLike, label: str) -> Path:
    path = Path(value).resolve()
    if not path.is_file():
        raise MigrationError(f"{label} is not an existing file: {path}")
    return path


def _load_json_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8-sig"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-standard numeric constant {value}")
            ),
        )
    except (OSError, ValueError) as exc:
        raise MigrationError(f"Invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise MigrationError(f"Invalid {label}: expected object")
    return value


def inspect_sources(
    *,
    sidecar_state: str | os.PathLike | None = None,
    journal: str | os.PathLike | None = None,
    paper_database: str | os.PathLike | None = None,
    config_manifest: str | os.PathLike | None = None,
) -> dict:
    """Inspect candidate inputs without creating or modifying any file."""
    result = {"schema": "chronoshft.runtime-migration-inspection.v1", "sources": {}}
    configured = {
        "sidecar_state": sidecar_state,
        "journal": journal,
        "paper_database": paper_database,
        "config_manifest": config_manifest,
    }
    for label, value in configured.items():
        if value is None:
            continue
        path = _absolute_existing_file(value, label)
        item = {
            "path": str(path),
            "size": path.stat().st_size,
            "sha256": _file_digest(path),
        }
        if label == "journal":
            records = decode_legacy_journal(path)
            versions = sorted(
                {
                    int(record["version"])
                    for record in records
                    if "version" in record
                }
            )
            item.update(
                {
                    "record_count": len(records),
                    "source_format": "v2" if versions == [2] else "legacy",
                }
            )
        elif label == "sidecar_state":
            record = _load_json_object(path, "sidecar v1 state")
            payload = record.get("payload")
            if not isinstance(payload, dict) or payload.get("schema_version") != 1:
                raise MigrationError("Sidecar source is not schema v1")
            expected = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
            if record.get("sha256") != expected:
                raise MigrationError("Sidecar v1 checksum mismatch")
            item.update(
                {
                    "source_format": "v1-json",
                    "generation": int(payload.get("generation", 0) or 0),
                    "deployment_id": str(payload.get("deployment_id", "") or ""),
                }
            )
        elif label == "paper_database":
            uri = f"file:{path.as_posix()}?mode=ro"
            try:
                with sqlite3.connect(uri, uri=True) as connection:
                    item["schema_version"] = int(
                        connection.execute("PRAGMA user_version").fetchone()[0]
                    )
                    item["integrity"] = str(
                        connection.execute("PRAGMA quick_check").fetchone()[0]
                    )
                    if item["integrity"].lower() != "ok":
                        raise MigrationError(
                            f"Paper database integrity check failed: {item['integrity']}"
                        )
            except sqlite3.Error as exc:
                raise MigrationError(f"Invalid Paper database: {exc}") from exc
        elif label == "config_manifest":
            manifest = _load_json_object(path, "configuration manifest")
            item["source_schema"] = str(
                manifest.get("schema", manifest.get("$schema", "")) or ""
            )
        result["sources"][label] = item
    if not result["sources"]:
        raise MigrationError("At least one migration source is required")
    result["inspection_sha256"] = _document_digest(
        result,
        "inspection_sha256",
    )
    return result


def build_migration_plan(
    inspection: Mapping,
    *,
    target_root: str | os.PathLike,
    account_scope_id: str = "",
    deployment_id: str = "",
    cash_flow_history_complete: bool = False,
    flat_proof_receipt: str | os.PathLike | None = None,
) -> dict:
    """Build a deterministic plan; applying it is a separate operation."""
    if inspection.get("schema") != "chronoshft.runtime-migration-inspection.v1":
        raise MigrationError("Unknown migration inspection schema")
    if inspection.get("inspection_sha256") != _document_digest(
        inspection,
        "inspection_sha256",
    ):
        raise MigrationError("Migration inspection digest mismatch")
    target = Path(target_root).resolve()
    sources = dict(inspection.get("sources", {}))
    actions = []
    if "sidecar_state" in sources:
        if not str(account_scope_id or "").strip() or not str(
            deployment_id or ""
        ).strip():
            raise MigrationError(
                "Sidecar migration requires account_scope_id and deployment_id"
            )
        source_deployment = str(sources["sidecar_state"].get("deployment_id", ""))
        proof = None
        if flat_proof_receipt is not None:
            proof_path = _absolute_existing_file(flat_proof_receipt, "flat proof receipt")
            proof = {
                "path": str(proof_path),
                "sha256": _file_digest(proof_path),
            }
        if not cash_flow_history_complete:
            if proof is None:
                raise MigrationError(
                    "Incomplete cash-flow history requires an account-wide flat proof"
                )
            if str(deployment_id) == source_deployment:
                raise MigrationError(
                    "Incomplete cash-flow history requires a new deployment_id"
                )
        actions.append(
            {
                "kind": "sidecar_v1_to_v2",
                "source": sources["sidecar_state"],
                "target_relative": (
                    f"storage/risk/accounts/{str(account_scope_id).strip()}"
                ),
                "account_scope_id": str(account_scope_id).strip(),
                "deployment_id": str(deployment_id).strip(),
                "cash_flow_history_complete": bool(cash_flow_history_complete),
                "flat_proof_receipt": proof,
            }
        )
    if "journal" in sources:
        actions.append(
            {
                "kind": "journal_to_v3",
                "source": sources["journal"],
                "target_relative": "storage/oms/oms_journal",
            }
        )
    if "paper_database" in sources:
        if "journal" not in sources:
            raise MigrationError(
                "Paper database rebuild requires a journal migration source"
            )
        actions.append(
            {
                "kind": "paper_rebuild_v5",
                "source": sources["paper_database"],
                "target_relative": "storage/paper/trades.sqlite3",
            }
        )
    if "config_manifest" in sources:
        actions.append(
            {
                "kind": "config_manifest_v3",
                "source": sources["config_manifest"],
                "target_relative": "config.json",
            }
        )
    plan = {
        "schema": PLAN_SCHEMA,
        "target_root": str(target),
        "inspection_sha256": inspection["inspection_sha256"],
        "actions": actions,
    }
    plan["plan_sha256"] = _document_digest(plan, "plan_sha256")
    return plan


def _validate_flat_proof(action: Mapping) -> dict | None:
    reference = action.get("flat_proof_receipt")
    if reference is None:
        return None
    path = _absolute_existing_file(reference["path"], "flat proof receipt")
    if _file_digest(path) != reference.get("sha256"):
        raise MigrationError("Flat proof receipt changed after planning")
    proof = _load_json_object(path, "flat proof receipt")
    required = {
        "scope": "ACCOUNT_WIDE",
        "account_scope_id": action["account_scope_id"],
        "open_order_count": 0,
        "nonzero_position_count": 0,
        "complete": True,
    }
    for field, expected in required.items():
        if proof.get(field) != expected:
            raise MigrationError(f"Flat proof receipt has invalid {field}")
    if not str(proof.get("proof_id", "") or "") or not str(
        proof.get("proof_sha256", "") or ""
    ):
        raise MigrationError("Flat proof receipt is not durable")
    if proof["proof_sha256"] != _document_digest(proof, "proof_sha256"):
        raise MigrationError("Flat proof receipt digest mismatch")
    return proof


def _copy_backup(sources: list[dict], backup_directory: Path) -> dict:
    if backup_directory.exists():
        raise MigrationError("Backup directory must not already exist")
    backup_directory.mkdir(parents=True)
    copied = []
    for index, source in enumerate(sources, start=1):
        source_path = Path(source["path"])
        destination = backup_directory / f"{index:02d}-{source_path.name}"
        shutil.copy2(source_path, destination)
        copied.append(
            {
                "source": str(source_path),
                "backup": str(destination),
                "sha256": _file_digest(destination),
            }
        )
    manifest = {"schema": "chronoshft.migration-backup.v1", "files": copied}
    manifest["manifest_sha256"] = _document_digest(manifest, "manifest_sha256")
    manifest_path = backup_directory / "backup-manifest.json"
    manifest_path.write_bytes(_canonical_bytes(manifest) + b"\n")
    return manifest


def _migrate_sidecar(action: Mapping, staging_root: Path) -> list[dict]:
    source_path = Path(action["source"]["path"])
    source_record = _load_json_object(source_path, "sidecar v1 state")
    payload = dict(source_record["payload"])
    expected = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    if source_record.get("sha256") != expected:
        raise MigrationError("Sidecar v1 checksum mismatch during apply")
    proof = _validate_flat_proof(action)
    payload.update(
        {
            "schema_version": 2,
            "legacy_source_schema": 1,
            "legacy_source_sha256": action["source"]["sha256"],
            "generation": 0,
            "deployment_id": action["deployment_id"],
            "account_scope_id": action["account_scope_id"],
            "kill_latched": True,
            "kill_reason": "offline_migration_requires_flat_proof_and_rearm",
            "stage": "KILL",
            "quiesced": True,
            "quiesce_reason": "offline_migration_locked",
            "manual_rearm_required": True,
            "flat_proof_id": str((proof or {}).get("proof_id", "") or ""),
        }
    )
    if not action["cash_flow_history_complete"]:
        payload.update(
            {
                "deployment_start_equity": 0.0,
                "deployment_start_external_cash_flow_total": 0.0,
                "deployment_adjusted_equity": 0.0,
                "deployment_loss": 0.0,
                "deployment_baseline_pending": True,
            }
        )
    target = staging_root / action["target_relative"]
    SidecarStateStore.provision(
        target,
        account_scope_id=action["account_scope_id"],
        deployment_id=action["deployment_id"],
        initial_payload=payload,
    )
    return _artifact_records(staging_root, target)


def _migrate_journal(action: Mapping, staging_root: Path) -> tuple[OMSJournal, list[dict]]:
    records = decode_legacy_journal(action["source"]["path"])
    target_base = staging_root / action["target_relative"]
    config = {
        "oms": {
            "journal_enabled": True,
            "journal_fsync": True,
            "journal_integrity_check": True,
            "journal_format_version": 3,
            "journal_path": str(target_base),
        }
    }
    journal = OMSJournal(config)
    for record in records:
        kind = str(record.get("kind", "") or "")
        payload = record.get("payload")
        if not kind or not isinstance(payload, dict):
            raise MigrationError("Legacy journal record has no kind/payload object")
        migrated_payload = dict(payload)
        if record.get("ts"):
            migrated_payload.setdefault(
                "_migration_source_journal_ts",
                str(record["ts"]),
            )
        journal.append(kind, migrated_payload)
    return journal, _artifact_records(staging_root, target_base.parent)


def _rebuild_paper(
    action: Mapping,
    staging_root: Path,
    journal: OMSJournal,
) -> tuple[dict, list[dict]]:
    target = staging_root / action["target_relative"]
    config = {
        "execution": {"mode": "paper"},
        "paper_trade": {"enabled": True},
        "paper_trade_database": {
            "enabled": True,
            "path": str(target),
        }
    }
    projection = PaperTradeDatabase.rebuild_offline(
        config,
        journal,
        destination_path=target,
    )
    return projection, _artifact_records(staging_root, target)


def _migrate_config(action: Mapping, staging_root: Path) -> list[dict]:
    source = _load_json_object(Path(action["source"]["path"]), "config manifest")
    schema = str(source.get("schema", "") or "")
    if schema != "chronoshft.config_manifest.v3":
        raise MigrationError(
            "Configuration must first be generated as strict manifest v3"
        )
    target = staging_root / action["target_relative"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_canonical_bytes(source) + b"\n")
    return _artifact_records(staging_root, target)


def _artifact_records(staging_root: Path, target: Path) -> list[dict]:
    files = [target] if target.is_file() else sorted(target.rglob("*"))
    return [
        {
            "path": str(path.relative_to(staging_root)).replace("\\", "/"),
            "size": path.stat().st_size,
            "sha256": _file_digest(path),
        }
        for path in files
        if path.is_file()
    ]


def apply_migration_plan(
    plan: Mapping,
    *,
    expected_plan_sha256: str,
    backup_directory: str | os.PathLike,
) -> dict:
    """Apply a previously reviewed plan into a new, atomically switched root."""
    if plan.get("schema") != PLAN_SCHEMA:
        raise MigrationError("Unknown migration plan schema")
    actual_plan_digest = _document_digest(plan, "plan_sha256")
    if plan.get("plan_sha256") != actual_plan_digest:
        raise MigrationError("Migration plan digest is invalid")
    if expected_plan_sha256 != actual_plan_digest:
        raise MigrationError("Provided plan digest does not match the plan")
    actions = list(plan.get("actions", []))
    if not actions:
        raise MigrationError("Migration plan has no actions")
    sources = [dict(action["source"]) for action in actions]
    unique_sources = {source["path"]: source for source in sources}
    for source in unique_sources.values():
        path = _absolute_existing_file(source["path"], "planned source")
        if _file_digest(path) != source.get("sha256"):
            raise MigrationError(f"Migration source changed after planning: {path}")

    target_root = Path(plan["target_root"]).resolve()
    backup_root = Path(backup_directory).resolve()
    if target_root.exists():
        raise MigrationError("Migration target root already exists")
    if backup_root == target_root or target_root in backup_root.parents:
        raise MigrationError("Backup directory must be independent of target root")
    _copy_backup(list(unique_sources.values()), backup_root)

    target_root.parent.mkdir(parents=True, exist_ok=True)
    staging_root = target_root.parent / (
        f".{target_root.name}.migration-{uuid.uuid4().hex}"
    )
    staging_root.mkdir()
    artifacts: list[dict] = []
    action_results = []
    journal = None
    try:
        for action in actions:
            kind = action.get("kind")
            if kind == "sidecar_v1_to_v2":
                produced = _migrate_sidecar(action, staging_root)
                result = {"kind": kind, "artifact_count": len(produced)}
            elif kind == "journal_to_v3":
                journal, produced = _migrate_journal(action, staging_root)
                result = {
                    "kind": kind,
                    "journal_id": journal.journal_id,
                    "record_count": journal.health_snapshot()["next_seq"] - 1,
                }
            elif kind == "paper_rebuild_v5":
                if journal is None:
                    raise MigrationError("Paper rebuild has no migrated journal")
                result, produced = _rebuild_paper(action, staging_root, journal)
                result = {"kind": kind, **result}
            elif kind == "config_manifest_v3":
                produced = _migrate_config(action, staging_root)
                result = {"kind": kind, "artifact_count": len(produced)}
            else:
                raise MigrationError(f"Unknown migration action: {kind!r}")
            artifacts.extend(produced)
            action_results.append(result)

        deduplicated = {item["path"]: item for item in artifacts}
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "plan_sha256": actual_plan_digest,
            "target_root": str(target_root),
            "backup_directory": str(backup_root),
            "sources": list(unique_sources.values()),
            "actions": action_results,
            "artifacts": [deduplicated[key] for key in sorted(deduplicated)],
        }
        receipt["receipt_sha256"] = _document_digest(receipt, "receipt_sha256")
        receipt_path = staging_root / "migration-receipt.json"
        receipt_path.write_bytes(_canonical_bytes(receipt) + b"\n")
        os.replace(staging_root, target_root)
    except Exception:
        if staging_root.exists() and staging_root.parent == target_root.parent:
            shutil.rmtree(staging_root)
        raise
    return receipt


def verify_migration_receipt(receipt: Mapping) -> dict:
    """Read-only verification of a completed migration receipt."""
    if receipt.get("schema") != RECEIPT_SCHEMA:
        raise MigrationError("Unknown migration receipt schema")
    if receipt.get("receipt_sha256") != _document_digest(
        receipt,
        "receipt_sha256",
    ):
        raise MigrationError("Migration receipt digest mismatch")
    target_root = Path(receipt["target_root"]).resolve()
    if not target_root.is_dir():
        raise MigrationError("Migration target root is missing")
    verified = 0
    for artifact in receipt.get("artifacts", []):
        relative = Path(str(artifact.get("path", "") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise MigrationError("Migration receipt contains an unsafe path")
        path = (target_root / relative).resolve()
        if target_root not in path.parents or not path.is_file():
            raise MigrationError(f"Migration artifact is missing: {relative}")
        if path.stat().st_size != int(artifact.get("size", -1)):
            raise MigrationError(f"Migration artifact size mismatch: {relative}")
        if _file_digest(path) != artifact.get("sha256"):
            raise MigrationError(f"Migration artifact digest mismatch: {relative}")
        verified += 1
    return {
        "valid": True,
        "receipt_sha256": receipt["receipt_sha256"],
        "verified_artifact_count": verified,
    }


__all__ = [
    "MigrationError",
    "apply_migration_plan",
    "build_migration_plan",
    "inspect_sources",
    "verify_migration_receipt",
]
