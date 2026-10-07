"""Stable durable-state path identity contracts for Live deployments."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

_DEPLOYMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$")


def _section(config: Mapping, key: str) -> Mapping:
    value = config.get(key, {})
    return value if isinstance(value, Mapping) else {}


def raw_path_parts(value: object) -> tuple[str, ...]:
    normalized = str(value or "").strip().replace("\\", "/")
    return tuple(
        part for part in normalized.split("/") if part not in {"", "."}
    )


def resolved_path_identity(
    value: object,
    *,
    base_dir: str | Path | None = None,
) -> tuple[str, tuple[str, ...]]:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("path must be configured")
    if ".." in raw_path_parts(raw):
        raise ValueError("path must not contain '..' components")

    normalized = raw.replace("\\", os.sep).replace("/", os.sep)
    candidate = Path(normalized)
    if not candidate.is_absolute():
        candidate = Path(base_dir or Path.cwd()) / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"path cannot be resolved: {exc}") from exc
    identity = os.path.normcase(os.path.normpath(str(resolved)))
    parts = tuple(os.path.normcase(part) for part in resolved.parts)
    return identity, parts


def validate_live_state_path_bindings(
    config: Mapping,
    *,
    base_dir: str | Path | None = None,
) -> dict[str, str]:
    """Resolve every durable Live path and bind it to one deployment."""

    live_launch = _section(config, "live_launch")
    oms = _section(config, "oms")
    risk = _section(config, "risk")
    supervisor = _section(risk, "independent_supervisor")
    writer_fence = _section(oms, "single_writer_fence")
    system = _section(config, "system")
    evidence = _section(system, "evidence_recorder")
    evidence_fence = _section(evidence, "single_writer_fence")
    admin_control = _section(system, "admin_control")
    alert = _section(config, "alert")
    deployment_id = str(
        live_launch.get("deployment_id", "") or ""
    ).strip()
    if not _DEPLOYMENT_ID_RE.fullmatch(deployment_id):
        raise ValueError(
            "live_launch.deployment_id must be 6-128 path-safe characters"
        )

    raw_paths = {
        "oms.journal_path": oms.get("journal_path"),
        "oms.single_writer_fence.path": writer_fence.get("path"),
        "risk.independent_supervisor.state_store_root": supervisor.get(
            "state_store_root"
        ),
        "system.evidence_recorder.path": evidence.get("path"),
        "system.evidence_recorder.single_writer_fence.path": (
            evidence_fence.get("path")
        ),
        "system.admin_control.path": admin_control.get("path"),
        "alert.failure_spool_path": alert.get("failure_spool_path"),
    }
    identities: dict[str, str] = {}
    resolved_parts: dict[str, tuple[str, ...]] = {}
    for field, raw_path in raw_paths.items():
        try:
            identity, parts = resolved_path_identity(
                raw_path,
                base_dir=base_dir,
            )
        except ValueError as exc:
            raise ValueError(f"{field} {exc}") from exc
        identities[field] = identity
        resolved_parts[field] = parts

    deployment_component = os.path.normcase(deployment_id)
    unbound = [
        field
        for field, parts in resolved_parts.items()
        if deployment_component not in parts
    ]
    if unbound:
        raise ValueError(
            "state paths must contain deployment_id as a resolved path "
            "component: " + ", ".join(unbound)
        )

    journal_raw = str(raw_paths["oms.journal_path"] or "").strip()
    expected_fence, _ = resolved_path_identity(
        f"{journal_raw}.lock",
        base_dir=base_dir,
    )
    if identities["oms.single_writer_fence.path"] != expected_fence:
        raise ValueError(
            "oms.single_writer_fence.path must resolve to "
            "oms.journal_path + '.lock'"
        )

    evidence_raw = str(
        raw_paths["system.evidence_recorder.path"] or ""
    ).strip()
    expected_evidence_fence, _ = resolved_path_identity(
        f"{evidence_raw}.lock",
        base_dir=base_dir,
    )
    if (
        identities[
            "system.evidence_recorder.single_writer_fence.path"
        ]
        != expected_evidence_fence
    ):
        raise ValueError(
            "system.evidence_recorder.single_writer_fence.path must resolve "
            "to system.evidence_recorder.path + '.lock'"
        )

    if len(set(identities.values())) != len(identities):
        raise ValueError(
            "all durable Live state, journal, fence, and alert spool paths "
            "must resolve to different files"
        )
    return identities


__all__ = [
    "raw_path_parts",
    "resolved_path_identity",
    "validate_live_state_path_bindings",
]
