import json
from pathlib import Path

import pytest

from governance.approval import LiveApprovalError, validate_live_approval_binding
from governance.canonical import canonical_config_digest
from governance.contracts import LIVE_APPROVAL_SCHEMA
from governance.release_manifest import (
    ReleaseManifestError,
    build_release_manifest,
    verify_release_manifest,
    write_release_manifest,
)


def _release_tree(root: Path) -> None:
    (root / "service").mkdir()
    (root / "config").mkdir()
    (root / "web").mkdir()
    (root / "service" / "runtime.py").write_text("VALUE = 1\n", encoding="ascii")
    (root / "config.json").write_text("{}\n", encoding="ascii")
    (root / "config" / "risk.json").write_text("{}\n", encoding="ascii")
    (root / "web" / "dashboard.html").write_text("ok\n", encoding="ascii")
    (root / "pyproject.toml").write_text("[project]\n", encoding="ascii")
    (root / "uv.lock").write_text("version = 1\n", encoding="ascii")


def test_release_manifest_is_deterministic_and_covers_every_artifact_class(tmp_path):
    _release_tree(tmp_path)

    first = build_release_manifest(tmp_path)
    second = build_release_manifest(tmp_path)

    assert first == second
    paths = {entry["path"]: entry["kind"] for entry in first["files"]}
    assert paths == {
        "config.json": "schema",
        "config/risk.json": "schema",
        "pyproject.toml": "dependency_lock",
        "service/runtime.py": "python",
        "uv.lock": "dependency_lock",
        "web/dashboard.html": "web",
    }

    baseline = first["release_digest"]
    for relative in (
        "service/runtime.py",
        "config/risk.json",
        "web/dashboard.html",
        "uv.lock",
    ):
        path = tmp_path / relative
        original = path.read_bytes()
        path.write_bytes(original + b"changed\n")
        assert build_release_manifest(tmp_path)["release_digest"] != baseline
        path.write_bytes(original)


def test_release_manifest_verification_rejects_stale_file_hash(tmp_path):
    _release_tree(tmp_path)
    manifest_path = tmp_path / "release-manifest.json"
    written = write_release_manifest(tmp_path, manifest_path)

    assert verify_release_manifest(tmp_path, manifest_path) == written
    (tmp_path / "web" / "dashboard.html").write_text("changed\n", encoding="ascii")
    with pytest.raises(ReleaseManifestError, match="does not match"):
        verify_release_manifest(tmp_path, manifest_path)


def test_live_approval_v3_binds_full_config_and_release(tmp_path):
    _release_tree(tmp_path)
    manifest_path = tmp_path / "release-manifest.json"
    release = write_release_manifest(tmp_path, manifest_path)
    config = {"execution": {"mode": "live"}, "symbols": ["BTCUSDT"]}
    approval_path = tmp_path / "approval.json"
    approval = {
        "schema": LIVE_APPROVAL_SCHEMA,
        "canonical_config_sha256": canonical_config_digest(config),
        "release_manifest_path": manifest_path.name,
        "release_digest": release["release_digest"],
    }

    validated = validate_live_approval_binding(
        approval,
        config=config,
        approval_path=approval_path,
        project_root=tmp_path,
    )
    assert validated["release_digest"] == release["release_digest"]

    changed_config = json.loads(json.dumps(config))
    changed_config["symbols"].append("ETHUSDT")
    with pytest.raises(LiveApprovalError, match="config digest mismatch"):
        validate_live_approval_binding(
            approval,
            config=changed_config,
            approval_path=approval_path,
            project_root=tmp_path,
        )


def test_obsolete_live_approval_is_explicitly_rejected(tmp_path):
    with pytest.raises(LiveApprovalError, match="obsolete Live approval schema"):
        validate_live_approval_binding(
            {"schema": "chronoshft.calibration_approval.v2"},
            config={},
            approval_path=tmp_path / "approval.json",
            project_root=tmp_path,
        )
