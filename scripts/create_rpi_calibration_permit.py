"""Create and sign a deployment-bound RPI calibration permit offline.

This utility imports no gateway, OMS, or network client. Private keys are
required to live outside the repository and are stored as encrypted PKCS8 PEM.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from infrastructure.config_scaling import (  # noqa: E402
    normalize_root_config_preapproval,
)
from infrastructure.rpi_calibration_permit import (  # noqa: E402
    RPI_CALIBRATION_PERMIT_SCHEMA,
    RPI_CALIBRATION_SIGNATURE_ALGORITHM,
    RPI_CALIBRATION_STAGE,
    RPI_CALIBRATION_TARGET_STAGE,
    RPI_CALIBRATION_VENUE,
    rpi_calibration_permit_sha256,
    rpi_calibration_permit_signature_payload,
    validate_rpi_calibration_permit,
)
from strategy.model_readiness import (  # noqa: E402
    deployment_config_sha256,
    implementation_sha256_for_model,
    strategy_policy_sha256,
)
from strategy.registry import canonical_model_key  # noqa: E402


DEFAULT_PASSPHRASE_ENV = "CHRONOSHFT_PERMIT_KEY_PASSPHRASE"
PRIVATE_KEY_LABEL = "ChronosHFT RPI calibration signing key"
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{1,127}$")
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
_WINDOWS_DRIVE_PREFIX_RE = re.compile(r"^([A-Za-z]:)")
_WINDOWS_DRIVE_REMOTE = 4
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(
    stat,
    "FILE_ATTRIBUTE_REPARSE_POINT",
    0x0400,
)


class PermitAuthoringError(ValueError):
    """Raised when an offline permit cannot be authored safely."""


def _reject_json_constant(value: str) -> None:
    raise PermitAuthoringError(
        f"non-standard JSON number {value!r} is not allowed"
    )


def _object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PermitAuthoringError(
                f"duplicate JSON object key {key!r} is not allowed"
            )
        result[key] = value
    return result


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            value = json.load(
                handle,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_object_without_duplicate_keys,
            )
    except (OSError, json.JSONDecodeError, PermitAuthoringError) as exc:
        raise PermitAuthoringError(
            f"cannot read {label} at {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise PermitAuthoringError(f"{label} must be a JSON object")
    return value


def _load_live_config(path: Path, label: str) -> dict[str, Any]:
    raw = _read_json_object(path, label)
    try:
        config = normalize_root_config_preapproval(raw)
    except (TypeError, ValueError) as exc:
        raise PermitAuthoringError(
            f"cannot normalize {label}: {exc}"
        ) from exc
    execution = config.get("execution")
    paper = config.get("paper_trade")
    if (
        not isinstance(execution, Mapping)
        or str(execution.get("mode", "") or "").strip().lower() != "live"
        or not isinstance(paper, Mapping)
        or paper.get("enabled") is not False
        or config.get("testnet") is not False
    ):
        raise PermitAuthoringError(
            f"{label} must be an explicit production Live configuration"
        )
    return config


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PermitAuthoringError(
            "value cannot be encoded as strict canonical JSON"
        ) from exc


def _pretty_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PermitAuthoringError(
            "value cannot be encoded as strict JSON"
        ) from exc


def _sync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_create(
    path: Path,
    payload: bytes,
    *,
    mode: int,
) -> tuple[str, ...]:
    if os.path.lexists(path):
        raise PermitAuthoringError(
            f"refusing to overwrite existing output: {path}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = -1
    temporary_path: Path | None = None
    published = False
    cleanup_warnings: list[str] = []
    primary_error: PermitAuthoringError | None = None
    try:
        descriptor, raw_temporary_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(raw_temporary_path)
        try:
            os.chmod(temporary_path, mode)
        except OSError:
            pass
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError as exc:
            raise PermitAuthoringError(
                f"refusing to overwrite existing output: {path}"
            ) from exc
        published = True
        try:
            temporary_path.unlink()
        except OSError:
            # Retry in finally. The durable target already exists at this point.
            pass
        else:
            temporary_path = None
        try:
            os.chmod(path, mode)
        except OSError:
            pass
        _sync_directory(path.parent)
    except PermitAuthoringError as exc:
        if published:
            cleanup_warnings.append(
                f"created {path}, but post-publish cleanup reported: {exc}"
            )
        else:
            primary_error = exc
    except OSError as exc:
        if published:
            cleanup_warnings.append(
                f"created {path}, but post-publish cleanup reported: {exc}"
            )
        else:
            primary_error = PermitAuthoringError(
                f"cannot atomically create {path}: {exc}"
            )
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as exc:
                cleanup_warnings.append(
                    f"could not close temporary descriptor for {path}: {exc}"
                )
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                state = "published output is valid" if published else "output failed"
                cleanup_warnings.append(
                    f"could not remove temporary file {temporary_path} "
                    f"({state}): {exc}"
                )
    if primary_error is not None:
        detail = str(primary_error)
        if cleanup_warnings:
            detail += "; cleanup incomplete: " + "; ".join(cleanup_warnings)
        raise PermitAuthoringError(detail) from primary_error
    return tuple(cleanup_warnings)


def _windows_drive_type(root: str) -> int | None:
    if os.name != "nt":
        return None
    import ctypes

    return int(ctypes.windll.kernel32.GetDriveTypeW(str(root)))


def _current_directory() -> Path:
    return Path.cwd()


def _reject_windows_anchor(text: str, field: str) -> bool:
    windows_text = text.replace("/", "\\")
    if windows_text.startswith("\\\\"):
        raise PermitAuthoringError(
            f"{field} must be on a local filesystem, not UNC/device storage"
        )

    drive_match = _WINDOWS_DRIVE_PREFIX_RE.match(windows_text)
    if drive_match is None:
        return False
    drive_root = drive_match.group(1) + "\\"
    if _windows_drive_type(drive_root) == _WINDOWS_DRIVE_REMOTE:
        raise PermitAuthoringError(
            f"{field} must not use a mapped network drive"
        )
    if len(windows_text) == 2 or windows_text[2] != "\\":
        raise PermitAuthoringError(
            f"{field} must not use a drive-relative path"
        )
    return True


def _reject_remote_path(value: str | Path, field: str) -> None:
    text = os.fspath(value)
    if not isinstance(text, str):
        raise PermitAuthoringError(f"{field} must be a text path")
    if _reject_windows_anchor(text, field):
        return
    _reject_windows_anchor(str(_current_directory()), field)


def _reject_reparse_components(path: Path, field: str) -> None:
    parts = path.parts
    if not parts:
        return
    current = Path(parts[0])
    for part in parts[1:]:
        current = current / part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            break
        except OSError as exc:
            raise PermitAuthoringError(
                f"cannot inspect {field} component {current}: {exc}"
            ) from exc
        attributes = int(
            getattr(metadata, "st_file_attributes", 0) or 0
        )
        if stat.S_ISLNK(metadata.st_mode) or (
            attributes & _FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise PermitAuthoringError(
                f"{field} must not traverse symlink or reparse components: "
                f"{current}"
            )


def _resolve_local_path(
    value: str | Path,
    *,
    field: str,
    strict: bool,
    base: Path | None = None,
    expand_user: bool = True,
) -> Path:
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        raise PermitAuthoringError(f"{field} must be a non-empty text path")
    _reject_remote_path(raw, field)
    candidate = Path(raw)
    if expand_user:
        candidate = candidate.expanduser()
    if not candidate.is_absolute():
        candidate = (base if base is not None else _current_directory()) / candidate
    _reject_remote_path(candidate, field)
    _reject_reparse_components(candidate, field)
    try:
        resolved = candidate.resolve(strict=strict)
    except (OSError, RuntimeError) as exc:
        requirement = "an existing local path" if strict else "a local path"
        raise PermitAuthoringError(
            f"{field} must resolve to {requirement}: {exc}"
        ) from exc
    _reject_remote_path(resolved, field)
    return resolved


def _is_inside_project(path: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(PROJECT_ROOT)
    except ValueError:
        return False
    return True


def _private_key_path(value: str, *, must_exist: bool) -> Path:
    candidate = Path(value).expanduser()
    _reject_remote_path(candidate, "private key path")
    if must_exist and candidate.is_symlink():
        raise PermitAuthoringError(
            "private key must not be accessed through a symlink"
        )
    path = _resolve_local_path(
        candidate,
        field="private key path",
        strict=must_exist,
        expand_user=False,
    )
    if _is_inside_project(path):
        raise PermitAuthoringError(
            "private signing keys must be stored outside the repository"
        )
    if must_exist:
        if not path.is_file() or path.is_symlink():
            raise PermitAuthoringError(
                "private key must be an existing regular, non-symlink file"
            )
    return path


def _passphrase(environ: Mapping[str, str], variable_name: str) -> bytes:
    if not _ENV_NAME_RE.fullmatch(variable_name):
        raise PermitAuthoringError(
            "passphrase environment variable name is invalid"
        )
    raw = environ.get(variable_name, "")
    encoded = raw.encode("utf-8")
    if len(encoded) < 16:
        raise PermitAuthoringError(
            f"{variable_name} must contain at least 16 UTF-8 bytes"
        )
    return encoded


def _canonical_public_key(value: str, field: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise PermitAuthoringError(
            f"{field} must be canonical base64"
        ) from exc
    if len(decoded) != 32 or base64.b64encode(decoded).decode("ascii") != value:
        raise PermitAuthoringError(
            f"{field} must encode exactly one Ed25519 public key"
        )
    return decoded


def _load_private_key(path: Path, passphrase: bytes) -> Ed25519PrivateKey:
    try:
        encoded = path.read_bytes()
    except OSError as exc:
        raise PermitAuthoringError(
            f"cannot read private key at {path}: {exc}"
        ) from exc
    if len(encoded) > 64 * 1024:
        raise PermitAuthoringError("private key file is unexpectedly large")
    try:
        key = serialization.load_pem_private_key(
            encoded,
            password=passphrase,
        )
    except (TypeError, ValueError) as exc:
        raise PermitAuthoringError(
            "private key or passphrase is invalid"
        ) from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise PermitAuthoringError("private key must be Ed25519")
    return key


def _public_key_bytes(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _parse_utc(value: str, field: str) -> datetime:
    text = str(value or "").strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise PermitAuthoringError(
            f"{field} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PermitAuthoringError(f"{field} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="auto"
    ).replace("+00:00", "Z")


def _json_number(value: Any, field: str) -> int | float:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PermitAuthoringError(
            f"{field} must be a positive decimal"
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise PermitAuthoringError(
            f"{field} must be a positive decimal"
        )
    if parsed == parsed.to_integral_value():
        return int(parsed)
    converted = float(parsed)
    if not converted > 0.0 or Decimal(str(converted)) != parsed.normalize():
        raise PermitAuthoringError(
            f"{field} cannot be represented exactly as a JSON number"
        )
    return converted


def _single_symbol(config: Mapping[str, Any], label: str) -> str:
    symbols = config.get("symbols")
    if not isinstance(symbols, list) or len(symbols) != 1:
        raise PermitAuthoringError(
            f"{label} must contain exactly one symbol"
        )
    symbol = str(symbols[0] or "").strip().upper()
    if not symbol:
        raise PermitAuthoringError(f"{label} symbol is empty")
    return symbol


def _primary_model(config: Mapping[str, Any], label: str) -> str:
    strategy = config.get("strategy")
    if not isinstance(strategy, Mapping):
        raise PermitAuthoringError(f"{label} strategy must be an object")
    try:
        return canonical_model_key(
            strategy.get("primary_model", strategy.get("name"))
        )
    except ValueError as exc:
        raise PermitAuthoringError(
            f"{label} primary model is invalid: {exc}"
        ) from exc


def _configured_cycle_interval(config: Mapping[str, Any]) -> Decimal:
    strategy = config.get("strategy")
    strategy = strategy if isinstance(strategy, Mapping) else {}
    glft = strategy.get("glft")
    glft = glft if isinstance(glft, Mapping) else {}
    raw = strategy.get("cycle_interval", glft.get("cycle_interval"))
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PermitAuthoringError(
            "calibration GLFT cycle_interval must be a positive decimal"
        ) from exc
    if not value.is_finite() or value <= 0:
        raise PermitAuthoringError(
            "calibration GLFT cycle_interval must be a positive decimal"
        )
    return value


def _resolve_bound_path(base: Path, value: Any, field: str) -> Path:
    if isinstance(value, Path):
        raw = str(value)
    elif isinstance(value, str):
        raw = value
    else:
        raw = ""
    if not raw or raw != raw.strip():
        raise PermitAuthoringError(f"{field} must be configured")
    path_parts = tuple(
        part
        for part in raw.replace("\\", "/").split("/")
        if part not in {"", "."}
    )
    if ".." in path_parts:
        raise PermitAuthoringError(
            f"{field} must not contain '..' components"
        )
    normalized = raw.replace("\\", os.sep).replace("/", os.sep)
    return _resolve_local_path(
        normalized,
        field=field,
        strict=False,
        base=base,
        expand_user=False,
    )


def _trusted_public_key(
    calibration_config: Mapping[str, Any],
    key_id: str,
) -> bytes:
    launch = calibration_config.get("live_launch")
    launch = launch if isinstance(launch, Mapping) else {}
    trusted = launch.get("calibration_permit_trusted_signers")
    if not isinstance(trusted, Mapping):
        raise PermitAuthoringError(
            "calibration_permit_trusted_signers must be configured before signing"
        )
    entry = trusted.get(key_id)
    if not isinstance(entry, Mapping) or set(entry) != {
        "algorithm",
        "public_key_base64",
    }:
        raise PermitAuthoringError(
            f"trusted signer {key_id!r} is missing or malformed"
        )
    if entry.get("algorithm") != RPI_CALIBRATION_SIGNATURE_ALGORITHM:
        raise PermitAuthoringError(
            f"trusted signer {key_id!r} must use ED25519"
        )
    return _canonical_public_key(
        str(entry.get("public_key_base64", "") or ""),
        f"trusted signer {key_id!r} public_key_base64",
    )


def generate_key(args: argparse.Namespace) -> dict[str, Any]:
    if not _KEY_ID_RE.fullmatch(args.key_id):
        raise PermitAuthoringError("key-id is invalid")
    private_path = _private_key_path(args.private_key, must_exist=False)
    trust_path = _resolve_local_path(
        args.trust_output,
        field="trust output path",
        strict=False,
    )
    if private_path == trust_path:
        raise PermitAuthoringError(
            "private-key and trust-output must be different files"
        )
    if os.path.lexists(private_path) or os.path.lexists(trust_path):
        raise PermitAuthoringError(
            "key generation refuses to overwrite either output"
        )
    passphrase = _passphrase(os.environ, args.passphrase_env)
    key = Ed25519PrivateKey.generate()
    private_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(
            passphrase
        ),
    )
    public_base64 = base64.b64encode(_public_key_bytes(key)).decode("ascii")
    trust_document = {
        "calibration_permit_trusted_signers": {
            args.key_id: {
                "algorithm": RPI_CALIBRATION_SIGNATURE_ALGORITHM,
                "public_key_base64": public_base64,
            }
        },
        "key_id": args.key_id,
    }
    authoring_warnings = list(
        _atomic_create(
            trust_path,
            _pretty_json_bytes(trust_document),
            mode=0o644,
        )
    )
    try:
        authoring_warnings.extend(
            _atomic_create(private_path, private_bytes, mode=0o600)
        )
    except Exception as exc:
        try:
            trust_path.unlink()
        except OSError as rollback_exc:
            raise PermitAuthoringError(
                f"{exc}; could not roll back trust output {trust_path}: "
                f"{rollback_exc}"
            ) from exc
        raise
    return {
        "private_key_path": str(private_path),
        "trust_output_path": str(trust_path),
        "key_id": args.key_id,
        "public_key_sha256": hashlib.sha256(
            _public_key_bytes(key)
        ).hexdigest(),
        "warnings": authoring_warnings,
    }


def sign_permit(args: argparse.Namespace) -> dict[str, Any]:
    calibration_path = _resolve_local_path(
        args.calibration_config,
        field="calibration config path",
        strict=True,
    )
    target_path = _resolve_local_path(
        args.target_config,
        field="target config path",
        strict=True,
    )
    output_path = _resolve_local_path(
        args.output,
        field="permit output path",
        strict=False,
    )
    private_path = _private_key_path(args.private_key, must_exist=True)
    if len({calibration_path, target_path, output_path, private_path}) != 4:
        raise PermitAuthoringError(
            "configs, permit output, and private key must be distinct files"
        )
    if os.path.lexists(output_path):
        raise PermitAuthoringError(
            f"refusing to overwrite existing permit: {output_path}"
        )
    calibration = _load_live_config(
        calibration_path,
        "calibration config",
    )
    target = _load_live_config(target_path, "target config")
    calibration_launch = calibration.get("live_launch")
    target_launch = target.get("live_launch")
    calibration_launch = (
        calibration_launch
        if isinstance(calibration_launch, Mapping)
        else {}
    )
    target_launch = target_launch if isinstance(target_launch, Mapping) else {}
    if calibration_launch.get("stage") != RPI_CALIBRATION_STAGE:
        raise PermitAuthoringError(
            "calibration config stage must be rpi_calibration_canary"
        )
    if target_launch.get("stage") != RPI_CALIBRATION_TARGET_STAGE:
        raise PermitAuthoringError("target config stage must be canary")
    if _resolve_bound_path(
        calibration_path.parent,
        calibration_launch.get("target_deployment_config_path"),
        "live_launch.target_deployment_config_path",
    ) != target_path:
        raise PermitAuthoringError(
            "calibration config is not bound to the requested target config"
        )
    if _resolve_bound_path(
        calibration_path.parent,
        calibration_launch.get("calibration_permit_path"),
        "live_launch.calibration_permit_path",
    ) != output_path:
        raise PermitAuthoringError(
            "permit output must exactly match live_launch.calibration_permit_path"
        )

    symbol = _single_symbol(calibration, "calibration config")
    if _single_symbol(target, "target config") != symbol:
        raise PermitAuthoringError(
            "calibration and target symbols must match"
        )
    model = _primary_model(calibration, "calibration config")
    if model != "glft" or _primary_model(target, "target config") != model:
        raise PermitAuthoringError(
            "calibration and target primary models must both be glft"
        )
    deployment_id = str(
        calibration_launch.get("deployment_id", "") or ""
    ).strip()
    if not deployment_id or deployment_id != str(
        target_launch.get("deployment_id", "") or ""
    ).strip():
        raise PermitAuthoringError(
            "calibration and target deployment_id values must match"
        )

    issued_at = _parse_utc(args.issued_at, "issued-at")
    not_before = _parse_utc(args.not_before, "not-before")
    expires_at = _parse_utc(args.expires_at, "expires-at")
    if not issued_at <= not_before < expires_at:
        raise PermitAuthoringError(
            "timestamps must satisfy issued-at <= not-before < expires-at"
        )
    fixed_depths = [
        _json_number(value, "fixed-depth-bps")
        for value in args.fixed_depth_bps
    ]
    minimum_interval = _json_number(
        args.min_order_interval_sec,
        "min-order-interval-sec",
    )
    if _configured_cycle_interval(calibration) < Decimal(
        str(minimum_interval)
    ):
        raise PermitAuthoringError(
            "calibration GLFT cycle_interval must be at least the signed "
            "min-order-interval-sec"
        )
    max_notional = _json_number(
        args.max_order_notional_usdt,
        "max-order-notional-usdt",
    )
    cumulative = (
        _json_number(
            args.max_cumulative_submitted_notional_usdt,
            "max-cumulative-submitted-notional-usdt",
        )
        if args.max_cumulative_submitted_notional_usdt is not None
        else _json_number(
            Decimal(str(max_notional)) * Decimal(args.max_order_count),
            "derived max-cumulative-submitted-notional-usdt",
        )
    )
    permit: dict[str, Any] = {
        "schema": RPI_CALIBRATION_PERMIT_SCHEMA,
        "permit_id": args.permit_id,
        "authorized_by": args.authorized_by,
        "deployment_id": deployment_id,
        "stage": RPI_CALIBRATION_STAGE,
        "venue": RPI_CALIBRATION_VENUE,
        "symbol": symbol,
        "model": model,
        "issued_at_utc": _utc_text(issued_at),
        "not_before_utc": _utc_text(not_before),
        "expires_at_utc": _utc_text(expires_at),
        "calibration_config_sha256": deployment_config_sha256(
            calibration
        ),
        "target_deployment_config_sha256": deployment_config_sha256(
            target
        ),
        "strategy_policy_sha256": strategy_policy_sha256(
            calibration,
            model,
        ),
        "implementation_sha256": implementation_sha256_for_model(model),
        "policy": {
            "fixed_depths_bps": fixed_depths,
            "order_ttl_sec": _json_number(
                args.order_ttl_sec,
                "order-ttl-sec",
            ),
            "min_order_interval_sec": minimum_interval,
            "max_active_orders": 1,
            "max_order_count": args.max_order_count,
            "min_order_notional_usdt": _json_number(
                args.min_order_notional_usdt,
                "min-order-notional-usdt",
            ),
            "max_order_notional_usdt": max_notional,
            "max_cumulative_submitted_notional_usdt": cumulative,
            "max_calibration_loss_usdt": _json_number(
                args.max_calibration_loss_usdt,
                "max-calibration-loss-usdt",
            ),
        },
    }

    passphrase = _passphrase(os.environ, args.passphrase_env)
    private_key = _load_private_key(private_path, passphrase)
    trusted_public = _trusted_public_key(calibration, args.key_id)
    if _public_key_bytes(private_key) != trusted_public:
        raise PermitAuthoringError(
            "private key does not match the configured trusted signer"
        )
    signature_payload = rpi_calibration_permit_signature_payload(permit)
    signature = private_key.sign(signature_payload)
    permit["signature"] = {
        "algorithm": RPI_CALIBRATION_SIGNATURE_ALGORITHM,
        "key_id": args.key_id,
        "signer": args.authorized_by,
        "signed_payload_sha256": hashlib.sha256(
            signature_payload
        ).hexdigest(),
        "signature_base64": base64.b64encode(signature).decode("ascii"),
    }

    try:
        validated = validate_rpi_calibration_permit(
            permit,
            calibration_config=calibration,
            target_deployment_config=target,
            trusted_signers=calibration_launch.get(
                "calibration_permit_trusted_signers",
                {},
            ),
            now_utc=not_before,
            calibration_config_base_dir=calibration_path.parent,
            target_deployment_config_base_dir=target_path.parent,
        )
    except (TypeError, ValueError) as exc:
        raise PermitAuthoringError(
            f"signed permit failed independent validation: {exc}"
        ) from exc
    if _canonical_json_bytes(validated["permit"]) != _canonical_json_bytes(
        permit
    ):
        raise PermitAuthoringError(
            "signed permit changed during independent validation"
        )
    authoring_warnings = _atomic_create(
        output_path,
        _pretty_json_bytes(permit),
        mode=0o600,
    )
    return {
        "permit_path": str(output_path),
        "permit_id": args.permit_id,
        "permit_sha256": rpi_calibration_permit_sha256(permit),
        "deployment_id": deployment_id,
        "symbol": symbol,
        "max_order_count": args.max_order_count,
        "warnings": list(authoring_warnings),
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate an encrypted Ed25519 key or sign an RPI calibration "
            "permit entirely offline. No network, Gateway, OMS, or order "
            "path is used."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    key_parser = subparsers.add_parser(
        "generate-key",
        help="Generate an encrypted offline Ed25519 signing key",
    )
    key_parser.add_argument("--private-key", required=True)
    key_parser.add_argument("--trust-output", required=True)
    key_parser.add_argument("--key-id", required=True)
    key_parser.add_argument(
        "--passphrase-env",
        default=DEFAULT_PASSPHRASE_ENV,
    )

    sign_parser = subparsers.add_parser(
        "sign",
        help="Create, sign, self-verify, and atomically write one permit",
    )
    sign_parser.add_argument("--calibration-config", required=True)
    sign_parser.add_argument("--target-config", required=True)
    sign_parser.add_argument("--private-key", required=True)
    sign_parser.add_argument("--output", required=True)
    sign_parser.add_argument("--key-id", required=True)
    sign_parser.add_argument("--authorized-by", required=True)
    sign_parser.add_argument("--permit-id", required=True)
    sign_parser.add_argument("--issued-at", required=True)
    sign_parser.add_argument("--not-before", required=True)
    sign_parser.add_argument("--expires-at", required=True)
    sign_parser.add_argument(
        "--fixed-depth-bps",
        action="append",
        required=True,
        help="Repeat at least three times in strictly increasing order",
    )
    sign_parser.add_argument("--order-ttl-sec", default="10")
    sign_parser.add_argument("--min-order-interval-sec", default="10")
    sign_parser.add_argument("--max-order-count", type=int, default=10)
    sign_parser.add_argument("--min-order-notional-usdt", default="5")
    sign_parser.add_argument("--max-order-notional-usdt", default="8")
    sign_parser.add_argument(
        "--max-cumulative-submitted-notional-usdt"
    )
    sign_parser.add_argument("--max-calibration-loss-usdt", default="1")
    sign_parser.add_argument(
        "--passphrase-env",
        default=DEFAULT_PASSPHRASE_ENV,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        result = (
            generate_key(args)
            if args.command == "generate-key"
            else sign_permit(args)
        )
    except (OSError, PermitAuthoringError, ValueError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
