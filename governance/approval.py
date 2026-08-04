"""Live approval v3 release and configuration binding."""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from governance.canonical import canonical_config_digest
from governance.contracts import LIVE_APPROVAL_SCHEMA
from governance.release_manifest import verify_release_manifest


class LiveApprovalError(ValueError):
    """Raised when a Live approval is stale or uses an obsolete contract."""


LIVE_APPROVAL_BINDING_FIELDS = frozenset(
    {
        "canonical_config_sha256",
        "release_digest",
        "release_manifest_path",
    }
)


def validate_live_approval_binding(
    approval: Mapping[str, Any],
    *,
    config: Mapping[str, Any],
    approval_path: str | Path,
    project_root: str | Path,
) -> dict[str, Any]:
    if not isinstance(approval, Mapping):
        raise LiveApprovalError("Live approval must be a JSON object")
    schema = approval.get("schema")
    if schema != LIVE_APPROVAL_SCHEMA:
        if isinstance(schema, str) and schema.startswith(
            "chronoshft.calibration_approval.v"
        ):
            raise LiveApprovalError(
                f"obsolete Live approval schema {schema!r} is rejected; "
                f"reissue approval using {LIVE_APPROVAL_SCHEMA!r}"
            )
        raise LiveApprovalError(
            f"Live approval schema must be {LIVE_APPROVAL_SCHEMA!r}"
        )
    missing = sorted(LIVE_APPROVAL_BINDING_FIELDS.difference(approval))
    if missing:
        raise LiveApprovalError(
            f"Live approval v3 is missing release bindings: {missing}"
        )
    expected_config_digest = canonical_config_digest(config)
    declared_config_digest = approval.get("canonical_config_sha256")
    if not isinstance(declared_config_digest, str) or not hmac.compare_digest(
        declared_config_digest,
        expected_config_digest,
    ):
        raise LiveApprovalError("Live approval canonical config digest mismatch")

    raw_manifest_path = approval.get("release_manifest_path")
    if (
        not isinstance(raw_manifest_path, str)
        or not raw_manifest_path.strip()
        or raw_manifest_path != raw_manifest_path.strip()
    ):
        raise LiveApprovalError(
            "Live approval release_manifest_path must be a non-empty path"
        )
    manifest_path = Path(raw_manifest_path)
    if not manifest_path.is_absolute():
        manifest_path = Path(approval_path).resolve().parent / manifest_path
    release_manifest = verify_release_manifest(project_root, manifest_path)
    declared_release_digest = approval.get("release_digest")
    if not isinstance(declared_release_digest, str) or not hmac.compare_digest(
        declared_release_digest,
        release_manifest["release_digest"],
    ):
        raise LiveApprovalError("Live approval release digest mismatch")
    return {
        "canonical_config_sha256": expected_config_digest,
        "release_digest": release_manifest["release_digest"],
        "release_manifest_path": str(manifest_path.resolve()),
    }


__all__ = [
    "LIVE_APPROVAL_BINDING_FIELDS",
    "LiveApprovalError",
    "validate_live_approval_binding",
]
