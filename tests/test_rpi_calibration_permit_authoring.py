import argparse
import ast
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from infrastructure.rpi_calibration_permit import (
    RPI_CALIBRATION_PERMIT_SCHEMA,
    rpi_calibration_permit_sha256,
)
from scripts import create_rpi_calibration_permit as permit_authoring
from scripts.create_rpi_calibration_permit import (
    DEFAULT_PASSPHRASE_ENV,
    PROJECT_ROOT,
    PermitAuthoringError,
    _resolve_bound_path,
    _resolve_local_path,
    generate_key,
    sign_permit,
)
from tests.test_live_config_guard import (
    safe_rpi_calibration_config,
    safe_rpi_target_config,
)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )


def test_authoring_tool_has_no_direct_network_gateway_or_oms_imports():
    source = PROJECT_ROOT / "scripts" / "create_rpi_calibration_permit.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    forbidden_prefixes = (
        "aiohttp",
        "gateway",
        "httpx",
        "oms",
        "requests",
        "socket",
        "urllib",
    )
    assert not {
        module
        for module in imported_modules
        if module.startswith(forbidden_prefixes)
    }


@pytest.mark.parametrize(
    "remote_path",
    (
        r"\\server\share\permit.json",
        "//server/share/permit.json",
        r"\\?\UNC\server\share\permit.json",
    ),
)
def test_local_path_guard_rejects_unc_before_resolution(
    remote_path,
    monkeypatch,
):
    def unexpected_path_resolution(*args, **kwargs):
        raise AssertionError("UNC path reached filesystem resolution")

    monkeypatch.setattr(Path, "absolute", unexpected_path_resolution)
    monkeypatch.setattr(Path, "resolve", unexpected_path_resolution)
    with pytest.raises(PermitAuthoringError, match="UNC/device"):
        _resolve_local_path(
            remote_path,
            field="test path",
            strict=False,
        )


def test_relative_path_rejects_unc_working_directory_before_resolution(
    monkeypatch,
):
    monkeypatch.setattr(
        permit_authoring,
        "_current_directory",
        lambda: Path(r"\\server\share\offline"),
    )

    def unexpected_path_resolution(*args, **kwargs):
        raise AssertionError("UNC working directory reached resolution")

    monkeypatch.setattr(Path, "resolve", unexpected_path_resolution)
    with pytest.raises(PermitAuthoringError, match="UNC/device"):
        _resolve_local_path(
            "permit.json",
            field="test path",
            strict=False,
        )


def test_local_path_guard_rejects_mapped_network_drive(monkeypatch):
    monkeypatch.setattr(
        permit_authoring,
        "_windows_drive_type",
        lambda root: 4 if root.upper() == "Z:\\" else 3,
    )

    with pytest.raises(PermitAuthoringError, match="mapped network drive"):
        _resolve_local_path(
            r"Z:\offline\permit.json",
            field="test path",
            strict=False,
        )


@pytest.mark.parametrize(
    "bound_path",
    (
        "../target.json",
        "sub/../target.json",
        r"sub\..\target.json",
    ),
)
def test_bound_path_rejects_runtime_parent_components(tmp_path, bound_path):
    with pytest.raises(PermitAuthoringError, match=r"must not contain '\.\.'"):
        _resolve_bound_path(tmp_path, bound_path, "bound path")


@pytest.mark.parametrize("bound_path", (" target.json", "target.json "))
def test_bound_path_rejects_runtime_whitespace(tmp_path, bound_path):
    with pytest.raises(PermitAuthoringError, match="must be configured"):
        _resolve_bound_path(tmp_path, bound_path, "bound path")


@pytest.mark.parametrize("bound_path", ("./target.json", "target..json"))
def test_bound_path_allows_non_traversal_names(tmp_path, bound_path):
    assert _resolve_bound_path(tmp_path, bound_path, "bound path") == (
        tmp_path / bound_path
    ).resolve()


def test_drive_relative_path_is_rejected_before_resolution(monkeypatch):
    monkeypatch.setattr(
        permit_authoring,
        "_windows_drive_type",
        lambda root: 3,
    )

    def unexpected_path_resolution(*args, **kwargs):
        raise AssertionError("drive-relative path reached resolution")

    monkeypatch.setattr(Path, "absolute", unexpected_path_resolution)
    monkeypatch.setattr(Path, "resolve", unexpected_path_resolution)
    with pytest.raises(PermitAuthoringError, match="drive-relative"):
        _resolve_local_path(
            r"Z:offline\permit.json",
            field="test path",
            strict=False,
        )


def test_reparse_component_is_rejected_before_resolution(tmp_path, monkeypatch):
    reparse_dir = tmp_path / "reparse"
    reparse_dir.mkdir()
    output = reparse_dir / "permit.json"
    original_lstat = os.lstat

    def fake_lstat(path, *args, **kwargs):
        if Path(path) == reparse_dir:
            return SimpleNamespace(
                st_mode=stat.S_IFDIR,
                st_file_attributes=0x0400,
            )
        return original_lstat(path, *args, **kwargs)

    def unexpected_path_resolution(*args, **kwargs):
        raise AssertionError("reparse path reached resolution")

    monkeypatch.setattr(os, "lstat", fake_lstat)
    monkeypatch.setattr(Path, "resolve", unexpected_path_resolution)
    with pytest.raises(PermitAuthoringError, match="symlink or reparse"):
        _resolve_local_path(
            output,
            field="test path",
            strict=False,
        )


def test_generate_key_keeps_published_outputs_on_temp_cleanup_failure(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(
        DEFAULT_PASSPHRASE_ENV,
        "offline-test-passphrase-32-bytes",
    )
    private_key = tmp_path / "offline-private-key.pem"
    trust_output = tmp_path / "trusted-signer.json"
    original_unlink = Path.unlink

    def fail_private_temp_cleanup(path, *args, **kwargs):
        if path.name.startswith(f".{private_key.name}.") and path.suffix == ".tmp":
            raise PermissionError("simulated locked temporary hard link")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_private_temp_cleanup)
    result = generate_key(
        argparse.Namespace(
            private_key=str(private_key),
            trust_output=str(trust_output),
            key_id="test-rpi-cleanup-warning",
            passphrase_env=DEFAULT_PASSPHRASE_ENV,
        )
    )

    assert private_key.read_bytes().startswith(
        b"-----BEGIN ENCRYPTED PRIVATE KEY-----"
    )
    assert _read_json(trust_output)["key_id"] == "test-rpi-cleanup-warning"
    assert any("published output is valid" in item for item in result["warnings"])


def test_generate_key_rolls_back_trust_on_prepublish_private_failure(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(
        DEFAULT_PASSPHRASE_ENV,
        "offline-test-passphrase-32-bytes",
    )
    private_key = tmp_path / "offline-private-key.pem"
    trust_output = tmp_path / "trusted-signer.json"
    original_link = os.link

    def fail_private_publish(source, destination, *args, **kwargs):
        if Path(destination) == private_key:
            raise PermissionError("simulated private-key publish failure")
        return original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "link", fail_private_publish)
    with pytest.raises(PermitAuthoringError, match="cannot atomically create"):
        generate_key(
            argparse.Namespace(
                private_key=str(private_key),
                trust_output=str(trust_output),
                key_id="test-rpi-publish-failure",
                passphrase_env=DEFAULT_PASSPHRASE_ENV,
            )
        )

    assert not private_key.exists()
    assert not trust_output.exists()


def test_prepublish_cleanup_failure_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv(
        DEFAULT_PASSPHRASE_ENV,
        "offline-test-passphrase-32-bytes",
    )
    private_key = tmp_path / "offline-private-key.pem"
    trust_output = tmp_path / "trusted-signer.json"
    original_link = os.link
    original_unlink = Path.unlink

    def fail_private_publish(source, destination, *args, **kwargs):
        if Path(destination) == private_key:
            raise PermissionError("simulated private-key publish failure")
        return original_link(source, destination, *args, **kwargs)

    def fail_private_temp_cleanup(path, *args, **kwargs):
        if path.name.startswith(f".{private_key.name}.") and path.suffix == ".tmp":
            raise PermissionError("simulated residual encrypted key")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "link", fail_private_publish)
    monkeypatch.setattr(Path, "unlink", fail_private_temp_cleanup)
    with pytest.raises(PermitAuthoringError, match="cleanup incomplete") as exc_info:
        generate_key(
            argparse.Namespace(
                private_key=str(private_key),
                trust_output=str(trust_output),
                key_id="test-rpi-residual-temp",
                passphrase_env=DEFAULT_PASSPHRASE_ENV,
            )
        )

    assert "could not remove temporary file" in str(exc_info.value)
    assert private_key.name in str(exc_info.value)
    assert not private_key.exists()
    assert not trust_output.exists()


def test_trust_rollback_failure_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv(
        DEFAULT_PASSPHRASE_ENV,
        "offline-test-passphrase-32-bytes",
    )
    private_key = tmp_path / "offline-private-key.pem"
    trust_output = tmp_path / "trusted-signer.json"
    original_link = os.link
    original_unlink = Path.unlink

    def fail_private_publish(source, destination, *args, **kwargs):
        if Path(destination) == private_key:
            raise PermissionError("simulated private-key publish failure")
        return original_link(source, destination, *args, **kwargs)

    def fail_trust_rollback(path, *args, **kwargs):
        if path == trust_output:
            raise PermissionError("simulated trust rollback failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "link", fail_private_publish)
    monkeypatch.setattr(Path, "unlink", fail_trust_rollback)
    with pytest.raises(PermitAuthoringError, match="could not roll back trust"):
        generate_key(
            argparse.Namespace(
                private_key=str(private_key),
                trust_output=str(trust_output),
                key_id="test-rpi-trust-rollback",
                passphrase_env=DEFAULT_PASSPHRASE_ENV,
            )
        )

    assert not private_key.exists()
    assert trust_output.exists()


def test_generate_sign_and_revalidate_permit_offline(tmp_path, monkeypatch):
    passphrase = "offline-test-passphrase-32-bytes"
    monkeypatch.setenv(DEFAULT_PASSPHRASE_ENV, passphrase)
    private_key = tmp_path / "offline-private-key.pem"
    trust_output = tmp_path / "trusted-signer.json"
    key_id = "test-rpi-signer-2026"

    generated = generate_key(
        argparse.Namespace(
            private_key=str(private_key),
            trust_output=str(trust_output),
            key_id=key_id,
            passphrase_env=DEFAULT_PASSPHRASE_ENV,
        )
    )
    assert generated["key_id"] == key_id
    assert private_key.read_bytes().startswith(b"-----BEGIN ENCRYPTED PRIVATE KEY-----")
    trust = _read_json(trust_output)

    calibration = safe_rpi_calibration_config()
    calibration.pop("_validated_rpi_calibration_permit", None)
    target = safe_rpi_target_config()
    calibration_path = tmp_path / "calibration.json"
    target_path = tmp_path / "target.json"
    permit_path = tmp_path / "permit.json"
    calibration["live_launch"]["target_deployment_config_path"] = (
        target_path.name
    )
    calibration["live_launch"]["calibration_permit_path"] = permit_path.name
    calibration["live_launch"][
        "calibration_permit_trusted_signers"
    ] = trust["calibration_permit_trusted_signers"]
    _write_json(calibration_path, calibration)
    _write_json(target_path, target)

    signed = sign_permit(
        argparse.Namespace(
            calibration_config=str(calibration_path),
            target_config=str(target_path),
            private_key=str(private_key),
            output=str(permit_path),
            key_id=key_id,
            authorized_by="Offline Test Operator",
            permit_id="rpi-framework-test-20260724-001",
            issued_at="2026-07-24T12:00:00Z",
            not_before="2026-07-24T12:05:00Z",
            expires_at="2026-07-24T13:00:00Z",
            fixed_depth_bps=["1", "1.5", "2"],
            order_ttl_sec="10",
            min_order_interval_sec="10",
            max_order_count=6,
            min_order_notional_usdt="5",
            max_order_notional_usdt="8",
            max_cumulative_submitted_notional_usdt=None,
            max_calibration_loss_usdt="1",
            passphrase_env=DEFAULT_PASSPHRASE_ENV,
        )
    )
    permit = _read_json(permit_path)
    assert permit["schema"] == RPI_CALIBRATION_PERMIT_SCHEMA
    assert permit["policy"]["max_order_count"] == 6
    assert permit["policy"]["max_cumulative_submitted_notional_usdt"] == 48
    assert signed["permit_sha256"] == rpi_calibration_permit_sha256(
        permit
    )
