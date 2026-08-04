"""Deterministic release inventory and digest verification."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from governance.canonical import canonical_json_bytes
from governance.contracts import (
    RELEASE_DIGEST_ALGORITHM,
    RELEASE_DIGEST_DOMAIN,
    RELEASE_MANIFEST_SCHEMA,
)


class ReleaseManifestError(ValueError):
    """Raised when a release inventory is incomplete, stale, or malformed."""


_EXCLUDED_TOP_LEVEL = frozenset(
    {
        ".agents",
        ".codex",
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        ".tmp",
        ".uv-cache",
        ".venv",
        "__pycache__",
        "logs",
        "storage",
        "tests",
    }
)
_DEPENDENCY_FILES = frozenset({"pyproject.toml", "uv.lock"})
_WEB_ROOTS = frozenset({"web"})
_SCHEMA_ROOTS = frozenset({"config", "schemas"})


def _release_kind(relative_path: Path) -> str | None:
    parts = relative_path.parts
    if not parts or parts[0] in _EXCLUDED_TOP_LEVEL:
        return None
    if len(parts) == 1 and relative_path.name in _DEPENDENCY_FILES:
        return "dependency_lock"
    if relative_path.suffix == ".py":
        return "python"
    if parts[0] in _SCHEMA_ROOTS and relative_path.suffix == ".json":
        return "schema"
    if len(parts) == 1 and relative_path.name == "config.json":
        return "schema"
    if parts[0] in _WEB_ROOTS:
        return "web"
    if parts[0] == "deploy" or parts[0] == "scripts":
        return "deployment"
    return None


def release_files(project_root: str | Path) -> tuple[tuple[Path, str], ...]:
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise ReleaseManifestError(f"release root is not a directory: {root}")
    selected: list[tuple[Path, str]] = []
    for directory, child_directories, filenames in os.walk(root, topdown=True):
        current = Path(directory)
        relative_directory = current.relative_to(root)
        if not relative_directory.parts:
            child_directories[:] = [
                name for name in child_directories if name not in _EXCLUDED_TOP_LEVEL
            ]
        else:
            child_directories[:] = [
                name
                for name in child_directories
                if name != "__pycache__" and not name.startswith(".")
            ]
        for filename in filenames:
            path = current / filename
            relative = path.relative_to(root)
            kind = _release_kind(relative)
            if kind is None:
                continue
            if path.is_symlink():
                raise ReleaseManifestError(
                    "release inputs must not traverse symlinks: "
                    f"{relative.as_posix()}"
                )
            selected.append((relative, kind))
    selected.sort(key=lambda item: item[0].as_posix())
    if not selected:
        raise ReleaseManifestError("release inventory is empty")
    return tuple(selected)


def _sha256_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def build_release_manifest(project_root: str | Path) -> dict[str, Any]:
    root = Path(project_root).resolve()
    entries = []
    for relative, kind in release_files(root):
        size, digest = _sha256_file(root / relative)
        entries.append(
            {
                "path": relative.as_posix(),
                "kind": kind,
                "size": size,
                "sha256": digest,
            }
        )
    unsigned = {
        "schema": RELEASE_MANIFEST_SCHEMA,
        "algorithm": RELEASE_DIGEST_ALGORITHM,
        "files": entries,
    }
    release_digest = hashlib.sha256(
        RELEASE_DIGEST_DOMAIN
        + canonical_json_bytes(unsigned, label="release manifest")
    ).hexdigest()
    return {**unsigned, "release_digest": release_digest}


def write_release_manifest(
    project_root: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    manifest = build_release_manifest(project_root)
    destination = Path(output_path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        manifest,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    ) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            Path(temporary_name).unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return manifest


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseManifestError(f"cannot read release manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReleaseManifestError("release manifest must be a JSON object")
    return payload


def verify_release_manifest(
    project_root: str | Path,
    manifest_path: str | Path,
) -> dict[str, Any]:
    path = Path(manifest_path).resolve()
    declared = _read_manifest(path)
    if declared.get("schema") != RELEASE_MANIFEST_SCHEMA:
        raise ReleaseManifestError(
            f"release manifest schema must be {RELEASE_MANIFEST_SCHEMA!r}"
        )
    expected_keys = {"schema", "algorithm", "files", "release_digest"}
    if set(declared) != expected_keys:
        raise ReleaseManifestError("release manifest keys are invalid")
    current = build_release_manifest(project_root)
    if canonical_json_bytes(declared, label="declared release manifest") != (
        canonical_json_bytes(current, label="current release manifest")
    ):
        raise ReleaseManifestError(
            "release manifest does not match the current deployment artifacts"
        )
    return current


__all__ = [
    "ReleaseManifestError",
    "build_release_manifest",
    "release_files",
    "verify_release_manifest",
    "write_release_manifest",
]
