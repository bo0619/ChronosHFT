"""Offline-only, digest-bound migration of runtime durable state."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Mapping

from governance.contracts import (
    CONFIG_DOCUMENT_VERSION,
    CONFIG_FRAGMENT_SCHEMA,
    CONFIG_MANIFEST_SCHEMA,
    CONFIG_UNKNOWN_KEY_POLICY,
)
from infrastructure.config_schema import (
    FRAGMENT_SCHEMAS,
    ConfigSchemaError,
    ObjectSpec,
    validate_composed_config,
    validate_fragment_document,
    validate_versioned_manifest,
)
from oms.journal import OMSJournal, decode_legacy_journal
from oms.paper_trade_database import PaperTradeDatabase
from risk.sidecar_state_store import SidecarStateStore


PLAN_SCHEMA = "chronoshft.runtime-migration-plan.v1"
RECEIPT_SCHEMA = "chronoshft.runtime-migration-receipt.v1"
LEGACY_CONFIG_MANIFEST_SCHEMAS = frozenset(
    {
        "chronoshft.config_manifest.v1",
        "chronoshft.config_manifest.v2",
    }
)
LEGACY_MONOLITHIC_CONFIG_SCHEMAS = frozenset(
    {
        "chronoshft.config.v1",
        "chronoshft.config.v2",
        "chronoshft.runtime_config.v1",
        "chronoshft.runtime_config.v2",
    }
)
CONFIG_FRAGMENT_ORDER = (
    "execution",
    "paper_trade",
    "paper_trade_database",
    "symbols",
    "data_recording",
    "system.logging",
    "system.rate_limit",
    "system.dashboard",
    "system.shutdown",
    "system.admin_control",
    "system.event_engine",
    "system.strategy_runtime",
    "system.resource_monitor",
    "system.market_data",
    "system.time_sync",
    "account",
    "risk.core",
    "risk.independent_supervisor",
    "risk.limits",
    "risk.price_sanity",
    "risk.technical_health",
    "risk.black_swan",
    "alerts",
    "backtest",
    "oms",
    "strategy.core",
    "strategy.capital_scaling",
    "strategy.order_sizing",
    "strategy.model_readiness",
    "strategy.glft",
    "strategy.avellaneda_stoikov",
)
_FRAGMENT_METADATA_KEYS = frozenset({"$schema", "fragment", "version"})
_MONOLITHIC_METADATA_KEYS = frozenset(
    {"$schema", "schema", "config_version", "unknown_keys"}
)
_OMS_V3_MIGRATION_DEFAULTS = {
    "journal_segment_max_records": 100_000,
    "journal_segment_max_bytes": 256 * 1024 * 1024,
    "journal_max_frame_bytes": 64 * 1024 * 1024,
}


class MigrationError(RuntimeError):
    """Raised when an offline migration cannot be proven safe."""


def _reject_duplicate_json_keys(pairs) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key {key!r}")
        value[key] = item
    return value


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
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, ValueError) as exc:
        raise MigrationError(f"Invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise MigrationError(f"Invalid {label}: expected object")
    return value


def _source_file_record(path: Path, *, relative_path: str = "") -> dict:
    record = {
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": _file_digest(path),
    }
    if relative_path:
        record["relative_path"] = relative_path
    return record


def _resolve_config_include(manifest_path: Path, value: object) -> Path:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MigrationError(
            "Legacy config includes must be non-empty relative paths"
        )
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise MigrationError(
            f"Legacy config include escapes its source root: {value!r}"
        )
    if relative.suffix.lower() != ".json":
        raise MigrationError(
            f"Legacy config include must be JSON: {value!r}"
        )
    source_root = manifest_path.parent.resolve()
    lexical = Path(os.path.abspath(source_root / relative))
    resolved = lexical.resolve()
    if source_root != resolved.parent and source_root not in resolved.parents:
        raise MigrationError(
            f"Legacy config include escapes its source root: {value!r}"
        )
    if os.path.normcase(str(lexical)) != os.path.normcase(str(resolved)):
        raise MigrationError(
            f"Legacy config include must not traverse a symlink: {value!r}"
        )
    return _absolute_existing_file(resolved, "legacy config fragment")


def _merge_legacy_fragment(
    merged: dict,
    fragment: Mapping,
    *,
    source: str,
    path: tuple[str, ...] = (),
) -> None:
    for key, value in fragment.items():
        field_path = (*path, str(key))
        if key not in merged:
            merged[key] = deepcopy(value)
            continue
        current = merged[key]
        if isinstance(current, dict) and isinstance(value, Mapping):
            _merge_legacy_fragment(
                current,
                value,
                source=source,
                path=field_path,
            )
            continue
        raise MigrationError(
            "Legacy config defines a field more than once: "
            f"{'.'.join(field_path)} at {source}"
        )


def _decode_legacy_include(value: object) -> tuple[str, str, int | None]:
    if isinstance(value, str):
        return value, "", None
    if not isinstance(value, Mapping):
        raise MigrationError(
            "Legacy config includes must contain paths or include objects"
        )
    allowed = {"path", "fragment", "version"}
    unknown = sorted(set(value).difference(allowed))
    if unknown:
        raise MigrationError(
            f"Legacy config include has unknown keys: {unknown}"
        )
    path = value.get("path")
    fragment = value.get("fragment", "")
    version = value.get("version")
    if fragment is not None and not isinstance(fragment, str):
        raise MigrationError("Legacy config include fragment must be a string")
    if version is not None and (
        isinstance(version, bool) or not isinstance(version, int)
    ):
        raise MigrationError("Legacy config include version must be an integer")
    return path, str(fragment or ""), version


def _strip_legacy_fragment_envelope(
    payload: Mapping,
    *,
    declared_fragment: str,
    declared_version: int | None,
    source: Path,
) -> dict:
    document_fragment = payload.get("fragment")
    document_version = payload.get("version")
    if (
        declared_fragment
        and document_fragment is not None
        and document_fragment != declared_fragment
    ):
        raise MigrationError(
            f"Legacy config fragment identity mismatch at {source}"
        )
    if (
        declared_version is not None
        and document_version is not None
        and document_version != declared_version
    ):
        raise MigrationError(
            f"Legacy config fragment version mismatch at {source}"
        )
    has_envelope = any(key in payload for key in _FRAGMENT_METADATA_KEYS)
    content = {
        key: deepcopy(value)
        for key, value in payload.items()
        if not has_envelope or key not in _FRAGMENT_METADATA_KEYS
    }
    if not content:
        raise MigrationError(f"Legacy config fragment is empty: {source}")
    return content


def _project_config_value(value: object, spec: object) -> tuple[object, bool]:
    if not isinstance(spec, ObjectSpec):
        return deepcopy(value), True
    if not isinstance(value, Mapping):
        return deepcopy(value), True
    projected = {}
    claimed = False
    for key, child_spec in spec.fields.items():
        if key not in value:
            continue
        child, child_claimed = _project_config_value(value[key], child_spec)
        if child_claimed:
            projected[key] = child
            claimed = True
    return projected, claimed


def _terminal_config_paths(
    value: object,
    path: tuple[str, ...] = (),
) -> set[tuple[str, ...]]:
    if isinstance(value, Mapping):
        keys = [
            key
            for key in value
            if not (isinstance(key, str) and key.startswith("_comment"))
        ]
        if not keys:
            return {path} if path else set()
        paths: set[tuple[str, ...]] = set()
        for key in keys:
            paths.update(
                _terminal_config_paths(value[key], (*path, str(key)))
            )
        return paths
    return {path}


def _config_comments(
    value: object,
    path: tuple[str, ...] = (),
) -> list[tuple[tuple[str, ...], object]]:
    if not isinstance(value, Mapping):
        return []
    comments = []
    for key, item in value.items():
        child_path = (*path, str(key))
        if isinstance(key, str) and key.startswith("_comment"):
            comments.append((child_path, item))
        else:
            comments.extend(_config_comments(item, child_path))
    return comments


def _schema_object_at(spec: object, path: tuple[str, ...]) -> bool:
    current = spec
    for key in path:
        if not isinstance(current, ObjectSpec):
            return False
        current = current.fields.get(key)
        if current is None:
            return False
    return isinstance(current, ObjectSpec)


def _mapping_at(value: dict, path: tuple[str, ...]) -> dict | None:
    current = value
    for key in path:
        child = current.get(key)
        if not isinstance(child, dict):
            return None
        current = child
    return current


def _fragment_target_path(fragment: str) -> str:
    return f"config/{fragment.replace('.', '/')}.json"


def _prepare_legacy_config(config: Mapping) -> dict:
    prepared = deepcopy(dict(config))
    oms = prepared.get("oms")
    if isinstance(oms, dict):
        oms["journal_format_version"] = 3
        mode = str(
            (prepared.get("execution", {}) or {}).get("mode", "") or ""
        ).strip().lower()
        oms.setdefault("journal_require_existing", mode == "live")
        for field, default in _OMS_V3_MIGRATION_DEFAULTS.items():
            oms.setdefault(field, default)
    return prepared


def _generate_v3_configuration(config: Mapping) -> tuple[dict, list[dict]]:
    unknown_contracts = sorted(
        set(FRAGMENT_SCHEMAS).difference(CONFIG_FRAGMENT_ORDER)
    )
    missing_contracts = sorted(
        set(CONFIG_FRAGMENT_ORDER).difference(FRAGMENT_SCHEMAS)
    )
    if unknown_contracts or missing_contracts:
        raise MigrationError(
            "Configuration migration registry is incomplete: "
            f"unplaced={unknown_contracts}, missing={missing_contracts}"
        )
    prepared = _prepare_legacy_config(config)
    generated = []
    merged = {}
    for fragment in CONFIG_FRAGMENT_ORDER:
        version = 1
        spec = FRAGMENT_SCHEMAS[fragment][version]
        content, claimed = _project_config_value(prepared, spec)
        if not claimed:
            continue
        document = {
            "$schema": CONFIG_FRAGMENT_SCHEMA,
            "fragment": fragment,
            "version": version,
            **content,
        }
        generated.append(
            {
                "path": _fragment_target_path(fragment),
                "fragment": fragment,
                "version": version,
                "document": document,
            }
        )

    if not generated:
        raise MigrationError(
            "Legacy configuration contains no fields owned by v3 fragments"
        )

    for comment_path, value in _config_comments(prepared):
        if not isinstance(value, str):
            raise MigrationError(
                f"Legacy config comment {'.'.join(comment_path)} must be a string"
            )
        parent_path = comment_path[:-1]
        for fragment in generated:
            spec = FRAGMENT_SCHEMAS[fragment["fragment"]][fragment["version"]]
            if not _schema_object_at(spec, parent_path):
                continue
            target = _mapping_at(fragment["document"], parent_path)
            if target is None:
                continue
            target[comment_path[-1]] = value
            break

    for fragment in generated:
        try:
            content = validate_fragment_document(
                fragment["document"],
                expected_fragment=fragment["fragment"],
                expected_version=fragment["version"],
                source=fragment["path"],
            )
        except ConfigSchemaError as exc:
            raise MigrationError(
                f"Legacy configuration cannot satisfy strict v3: {exc}"
            ) from exc
        _merge_legacy_fragment(
            merged,
            content,
            source=fragment["path"],
        )

    unclaimed = sorted(
        _terminal_config_paths(prepared).difference(
            _terminal_config_paths(merged)
        )
    )
    if unclaimed:
        rendered = [".".join(path) for path in unclaimed[:12]]
        raise MigrationError(
            "Legacy configuration has no strict v3 owner for: "
            + ", ".join(rendered)
        )
    try:
        validate_composed_config(merged)
    except ConfigSchemaError as exc:
        raise MigrationError(
            f"Legacy configuration violates strict v3 invariants: {exc}"
        ) from exc

    manifest = {
        "schema": CONFIG_MANIFEST_SCHEMA,
        "config_version": CONFIG_DOCUMENT_VERSION,
        "unknown_keys": CONFIG_UNKNOWN_KEY_POLICY,
        "includes": [
            {
                "path": fragment["path"],
                "fragment": fragment["fragment"],
                "version": fragment["version"],
            }
            for fragment in generated
        ],
    }
    validate_versioned_manifest(manifest)
    return manifest, generated


def _inspect_config_source(path: Path) -> dict:
    root = _load_json_object(path, "legacy/v2 configuration")
    schema = str(root.get("schema", root.get("$schema", "")) or "")
    if schema == CONFIG_MANIFEST_SCHEMA:
        raise MigrationError(
            "Configuration migration requires explicit legacy/v2 input, "
            "not an existing v3 manifest"
        )
    input_files = [_source_file_record(path)]
    is_manifest = "includes" in root or schema.startswith(
        "chronoshft.config_manifest.v"
    )
    if is_manifest:
        if schema not in LEGACY_CONFIG_MANIFEST_SCHEMAS:
            raise MigrationError(
                f"Unsupported legacy configuration manifest schema: {schema!r}"
            )
        allowed = {"schema", "includes", "config_version", "unknown_keys"}
        unknown = sorted(set(root).difference(allowed))
        if unknown:
            raise MigrationError(
                f"Legacy configuration manifest has unknown keys: {unknown}"
            )
        includes = root.get("includes")
        if not isinstance(includes, list) or not includes:
            raise MigrationError(
                "Legacy configuration manifest includes must be non-empty"
            )
        if len(includes) > 128:
            raise MigrationError(
                "Legacy configuration manifest has more than 128 includes"
            )
        merged: dict = {}
        seen_paths = set()
        for raw_include in includes:
            include, declared_fragment, declared_version = (
                _decode_legacy_include(raw_include)
            )
            include_path = _resolve_config_include(path, include)
            normalized = os.path.normcase(str(include_path))
            if normalized in seen_paths:
                raise MigrationError(
                    f"Duplicate legacy config include: {include!r}"
                )
            seen_paths.add(normalized)
            payload = _load_json_object(
                include_path,
                "legacy config fragment",
            )
            if "includes" in payload:
                raise MigrationError(
                    f"Nested legacy config manifest is unsupported: {include}"
                )
            content = _strip_legacy_fragment_envelope(
                payload,
                declared_fragment=declared_fragment,
                declared_version=declared_version,
                source=include_path,
            )
            _merge_legacy_fragment(
                merged,
                content,
                source=str(include_path),
            )
            input_files.append(
                _source_file_record(
                    include_path,
                    relative_path=str(include).replace("\\", "/"),
                )
            )
        source_format = schema.rsplit(".", 1)[-1] + "-manifest"
    else:
        if schema and schema not in LEGACY_MONOLITHIC_CONFIG_SCHEMAS:
            raise MigrationError(
                f"Unsupported legacy configuration schema: {schema!r}"
            )
        config_version = root.get("config_version")
        if config_version is not None and (
            isinstance(config_version, bool)
            or not isinstance(config_version, int)
            or config_version not in (1, 2)
        ):
            raise MigrationError(
                "Legacy monolithic config_version must be integer 1 or 2"
            )
        merged = {
            key: deepcopy(value)
            for key, value in root.items()
            if key not in _MONOLITHIC_METADATA_KEYS
        }
        source_format = (
            f"v{config_version}-monolithic"
            if config_version is not None
            else "legacy-monolithic"
        )
    manifest, fragments = _generate_v3_configuration(merged)
    return {
        "source_schema": schema,
        "source_format": source_format,
        "input_files": input_files,
        "target_manifest": manifest,
        "target_fragments": fragments,
    }


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
            item.update(_inspect_config_source(path))
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
    cash_flow_deployment_start_ms: int = 0,
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
        try:
            cash_flow_deployment_start_ms = int(
                cash_flow_deployment_start_ms
            )
        except (TypeError, ValueError) as exc:
            raise MigrationError(
                "Sidecar migration requires cash_flow_deployment_start_ms"
            ) from exc
        if cash_flow_deployment_start_ms <= 0:
            raise MigrationError(
                "Sidecar migration requires cash_flow_deployment_start_ms"
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
                "cash_flow_deployment_start_ms": (
                    cash_flow_deployment_start_ms
                ),
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
    legacy_payload = dict(source_record["payload"])
    expected = hashlib.sha256(_canonical_bytes(legacy_payload)).hexdigest()
    if source_record.get("sha256") != expected:
        raise MigrationError("Sidecar v1 checksum mismatch during apply")
    proof = _validate_flat_proof(action)
    payload = {
        "schema_version": 2,
        "legacy_source_schema": 1,
        "legacy_source_sha256": action["source"]["sha256"],
        "deployment_id": action["deployment_id"],
        "account_scope_id": action["account_scope_id"],
        "cash_flow_deployment_start_ms": int(
            action["cash_flow_deployment_start_ms"]
        ),
        "kill_latched": True,
        "kill_reason": "offline_migration_requires_flat_proof_and_rearm",
        "stage": "KILL",
        "quiesced": False,
        "quiesce_reason": "",
        "manual_rearm_required": True,
        "flat_proof_id": str((proof or {}).get("proof_id", "") or ""),
    }
    for field in (
        "risk_day",
        "day_start_equity",
        "day_start_external_cash_flow_total",
        "peak_adjusted_equity",
        "last_equity",
        "deployment_start_equity",
        "deployment_start_external_cash_flow_total",
        "deployment_adjusted_equity",
        "deployment_loss",
        "declared_account_equity",
        "max_deployed_capital",
        "deployment_policy_fingerprint",
        "account_key_fingerprint",
    ):
        if field in legacy_payload:
            payload[field] = legacy_payload[field]
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
    source = action["source"]
    manifest = deepcopy(source.get("target_manifest"))
    fragments = deepcopy(source.get("target_fragments"))
    if not isinstance(manifest, dict) or not isinstance(fragments, list):
        raise MigrationError("Configuration plan has no generated v3 payload")
    try:
        includes = validate_versioned_manifest(manifest)
    except ConfigSchemaError as exc:
        raise MigrationError(f"Generated v3 manifest is invalid: {exc}") from exc
    by_path = {
        str(fragment.get("path", "")): fragment
        for fragment in fragments
        if isinstance(fragment, Mapping)
    }
    if len(by_path) != len(fragments) or set(by_path) != {
        include.path for include in includes
    }:
        raise MigrationError(
            "Generated v3 manifest and fragment payloads do not match"
        )
    merged = {}
    written_paths = []
    for include in includes:
        fragment = by_path[include.path]
        if (
            fragment.get("fragment") != include.fragment
            or fragment.get("version") != include.version
        ):
            raise MigrationError(
                f"Generated v3 fragment metadata mismatch: {include.path}"
            )
        document = fragment.get("document")
        if not isinstance(document, Mapping):
            raise MigrationError(
                f"Generated v3 fragment is not an object: {include.path}"
            )
        try:
            content = validate_fragment_document(
                document,
                expected_fragment=include.fragment,
                expected_version=include.version,
                source=include.path,
            )
        except ConfigSchemaError as exc:
            raise MigrationError(
                f"Generated v3 fragment is invalid: {exc}"
            ) from exc
        _merge_legacy_fragment(
            merged,
            content,
            source=include.path,
        )
        relative = Path(include.path)
        if relative.is_absolute() or ".." in relative.parts:
            raise MigrationError(
                f"Generated v3 fragment path is unsafe: {include.path!r}"
            )
        fragment_path = (staging_root / relative).resolve()
        if staging_root != fragment_path.parent and staging_root not in (
            fragment_path.parents
        ):
            raise MigrationError(
                f"Generated v3 fragment path is unsafe: {include.path!r}"
            )
        fragment_path.parent.mkdir(parents=True, exist_ok=True)
        fragment_path.write_bytes(_canonical_bytes(document) + b"\n")
        written_paths.append(fragment_path)
    try:
        validate_composed_config(merged)
    except ConfigSchemaError as exc:
        raise MigrationError(
            f"Generated v3 configuration invariants failed: {exc}"
        ) from exc
    target = staging_root / action["target_relative"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_canonical_bytes(manifest) + b"\n")
    written_paths.append(target)
    artifacts = []
    for path in written_paths:
        artifacts.extend(_artifact_records(staging_root, path))
    return artifacts


def _planned_source_records(action: Mapping) -> list[dict]:
    source = dict(action["source"])
    if action.get("kind") == "config_manifest_v3":
        records = source.get("input_files")
        if not isinstance(records, list) or not records:
            raise MigrationError(
                "Configuration plan has no digest-bound input files"
            )
        return [dict(record) for record in records]
    return [source]


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
    sources = [
        source
        for action in actions
        for source in _planned_source_records(action)
    ]
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
