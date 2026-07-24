from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from strategy.model_readiness import (
    deployment_config_sha256,
    implementation_sha256_for_model,
    strategy_policy_sha256,
    verify_ed25519_signature,
)
from strategy.registry import canonical_model_key


RPI_CALIBRATION_PERMIT_SCHEMA = "chronoshft.rpi_calibration_permit.v1"
RPI_CALIBRATION_SIGNATURE_ALGORITHM = "ED25519"
RPI_CALIBRATION_SIGNATURE_DOMAIN = (
    b"chronoshft.rpi_calibration_permit.v1.signature\0"
)
RPI_CALIBRATION_STAGE = "rpi_calibration_canary"
RPI_CALIBRATION_TARGET_STAGE = "canary"
RPI_CALIBRATION_VENUE = "BINANCE_USDM"

MAX_PERMIT_TTL_SEC = 86_400
MIN_FIXED_DEPTH_LEVELS = 3
MAX_FIXED_DEPTH_LEVELS = 16
MIN_FIXED_DEPTH_SPAN_BPS = Decimal("0.5")
MAX_FIXED_DEPTH_BPS = Decimal("1000")
MAX_ORDER_TTL_SEC = Decimal("60")
MIN_ORDER_INTERVAL_SEC = Decimal("5")
MAX_ORDER_INTERVAL_SEC = Decimal("3600")
MAX_ACTIVE_ORDERS = 1
MAX_ORDER_COUNT = 100
MAX_ORDER_NOTIONAL_USDT = Decimal("8")
MAX_CALIBRATION_LOSS_USDT = Decimal("2")

_PERMIT_KEYS = frozenset(
    {
        "schema",
        "permit_id",
        "authorized_by",
        "deployment_id",
        "stage",
        "venue",
        "symbol",
        "model",
        "issued_at_utc",
        "not_before_utc",
        "expires_at_utc",
        "calibration_config_sha256",
        "target_deployment_config_sha256",
        "strategy_policy_sha256",
        "implementation_sha256",
        "policy",
        "signature",
    }
)
_UNSIGNED_PERMIT_KEYS = _PERMIT_KEYS - {"signature"}
_POLICY_KEYS = frozenset(
    {
        "fixed_depths_bps",
        "order_ttl_sec",
        "min_order_interval_sec",
        "max_active_orders",
        "max_order_count",
        "min_order_notional_usdt",
        "max_order_notional_usdt",
        "max_cumulative_submitted_notional_usdt",
        "max_calibration_loss_usdt",
    }
)
_SIGNATURE_KEYS = frozenset(
    {
        "algorithm",
        "key_id",
        "signer",
        "signed_payload_sha256",
        "signature_base64",
    }
)
_TRUSTED_SIGNER_KEYS = frozenset(
    {
        "algorithm",
        "public_key_base64",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PERMIT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{5,127}$")
_DEPLOYMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$")
_SIGNER_KEY_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$"
)
_AUTHORIZED_BY_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9 ._@:+-]{2,127}$"
)
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{3,32}$")

PermitSignatureVerifier = Callable[[str, str, bytes, bytes], bool]


class RpiCalibrationPermitError(ValueError):
    """A fail-closed RPI calibration permit validation error."""


def rpi_calibration_permit_signature_payload(
    permit: Mapping[str, Any],
) -> bytes:
    """Return the independently domain-separated bytes an operator signs."""
    if not isinstance(permit, Mapping):
        raise RpiCalibrationPermitError("RPI calibration permit must be an object")
    keys = set(permit)
    if keys != _PERMIT_KEYS and keys != _UNSIGNED_PERMIT_KEYS:
        raise RpiCalibrationPermitError(
            "RPI calibration permit keys are invalid"
        )
    unsigned = dict(permit)
    unsigned.pop("signature", None)
    return RPI_CALIBRATION_SIGNATURE_DOMAIN + _canonical_json_bytes(
        unsigned,
        "unsigned RPI calibration permit",
    )


def rpi_calibration_permit_sha256(permit: Mapping[str, Any]) -> str:
    """Hash the complete signed permit for durable activation journals."""
    if not isinstance(permit, Mapping) or set(permit) != _PERMIT_KEYS:
        raise RpiCalibrationPermitError(
            "complete RPI calibration permit keys are invalid"
        )
    return hashlib.sha256(
        _canonical_json_bytes(permit, "complete RPI calibration permit")
    ).hexdigest()


def parse_rpi_calibration_permit_json(
    raw: str | bytes,
    *,
    label: str = "RPI calibration permit",
) -> dict[str, Any]:
    """Parse strict JSON, rejecting duplicate keys and non-finite constants."""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RpiCalibrationPermitError(
                f"{label} is not valid UTF-8"
            ) from exc
    if not isinstance(raw, str):
        raise RpiCalibrationPermitError(f"{label} JSON must be text or bytes")
    try:
        parsed = json.loads(
            raw,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (json.JSONDecodeError, RpiCalibrationPermitError) as exc:
        raise RpiCalibrationPermitError(
            f"cannot parse {label}: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise RpiCalibrationPermitError(f"{label} must be a JSON object")
    return parsed


def validate_rpi_calibration_permit(
    permit: Mapping[str, Any],
    *,
    calibration_config: Mapping[str, Any],
    target_deployment_config: Mapping[str, Any],
    trusted_signers: Mapping[str, Any],
    now_utc: datetime | None = None,
    signature_verifier: PermitSignatureVerifier | None = None,
    calibration_config_base_dir: str | Path | None = None,
    target_deployment_config_base_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Validate a signed permit against both effective deployment configs."""
    if not isinstance(permit, Mapping):
        raise RpiCalibrationPermitError("RPI calibration permit must be an object")
    if set(permit) != _PERMIT_KEYS:
        raise RpiCalibrationPermitError(
            "RPI calibration permit keys are invalid"
        )
    _canonical_json_bytes(permit, "RPI calibration permit")
    if permit.get("schema") != RPI_CALIBRATION_PERMIT_SCHEMA:
        raise RpiCalibrationPermitError(
            "RPI calibration permit schema must be "
            f"{RPI_CALIBRATION_PERMIT_SCHEMA!r}"
        )

    permit_id = _strict_string(
        permit.get("permit_id"),
        "permit_id",
        pattern=_PERMIT_ID_RE,
    )
    authorized_by = _strict_string(
        permit.get("authorized_by"),
        "authorized_by",
        pattern=_AUTHORIZED_BY_RE,
    )
    del permit_id

    calibration_live_launch = _mapping_field(
        calibration_config,
        "live_launch",
        "calibration config",
    )
    target_live_launch = _mapping_field(
        target_deployment_config,
        "live_launch",
        "target deployment config",
    )
    configured_stage = _strict_string(
        calibration_live_launch.get("stage"),
        "calibration config live_launch.stage",
    )
    if configured_stage != RPI_CALIBRATION_STAGE:
        raise RpiCalibrationPermitError(
            "calibration config live_launch.stage must be "
            f"{RPI_CALIBRATION_STAGE!r}"
        )
    if permit.get("stage") != configured_stage:
        raise RpiCalibrationPermitError(
            "permit stage must match the calibration config"
        )
    if permit.get("venue") != RPI_CALIBRATION_VENUE:
        raise RpiCalibrationPermitError(
            f"permit venue must be {RPI_CALIBRATION_VENUE!r}"
        )

    deployment_id = _strict_string(
        calibration_live_launch.get("deployment_id"),
        "calibration config live_launch.deployment_id",
        pattern=_DEPLOYMENT_ID_RE,
    )
    if permit.get("deployment_id") != deployment_id:
        raise RpiCalibrationPermitError(
            "permit deployment_id must match the calibration config"
        )
    target_stage = _strict_string(
        target_live_launch.get("stage"),
        "target deployment config live_launch.stage",
    )
    if target_stage != RPI_CALIBRATION_TARGET_STAGE:
        raise RpiCalibrationPermitError(
            "target deployment config live_launch.stage must be "
            f"{RPI_CALIBRATION_TARGET_STAGE!r}"
        )
    target_deployment_id = _strict_string(
        target_live_launch.get("deployment_id"),
        "target deployment config live_launch.deployment_id",
        pattern=_DEPLOYMENT_ID_RE,
    )
    if target_deployment_id != deployment_id:
        raise RpiCalibrationPermitError(
            "calibration and target deployment_id values must match"
        )

    symbol = _single_symbol(calibration_config, "calibration config")
    target_symbol = _single_symbol(
        target_deployment_config,
        "target deployment config",
    )
    if symbol != target_symbol:
        raise RpiCalibrationPermitError(
            "calibration and target deployment symbols must match"
        )
    if permit.get("symbol") != symbol:
        raise RpiCalibrationPermitError(
            "permit symbol must exactly match the configured symbol"
        )

    model = _primary_model(calibration_config, "calibration config")
    target_model = _primary_model(
        target_deployment_config,
        "target deployment config",
    )
    if target_model != model:
        raise RpiCalibrationPermitError(
            "calibration and target deployment primary models must match"
        )
    if permit.get("model") != model:
        raise RpiCalibrationPermitError(
            "permit model must be the canonical configured primary model"
        )
    _validate_cross_config_state_isolation(
        calibration_config,
        target_deployment_config,
        calibration_base_dir=calibration_config_base_dir,
        target_base_dir=target_deployment_config_base_dir,
    )

    effective_now = _effective_now_utc(now_utc)
    issued_at = _parse_utc_timestamp(
        permit.get("issued_at_utc"),
        "issued_at_utc",
    )
    not_before = _parse_utc_timestamp(
        permit.get("not_before_utc"),
        "not_before_utc",
    )
    expires_at = _parse_utc_timestamp(
        permit.get("expires_at_utc"),
        "expires_at_utc",
    )
    if not issued_at <= not_before < expires_at:
        raise RpiCalibrationPermitError(
            "permit timestamps must satisfy issued_at_utc <= "
            "not_before_utc < expires_at_utc"
        )
    permit_ttl_sec = (expires_at - issued_at).total_seconds()
    if permit_ttl_sec > MAX_PERMIT_TTL_SEC:
        raise RpiCalibrationPermitError(
            f"permit TTL must not exceed {MAX_PERMIT_TTL_SEC} seconds"
        )
    if effective_now < not_before:
        raise RpiCalibrationPermitError("RPI calibration permit is not active yet")
    if effective_now >= expires_at:
        raise RpiCalibrationPermitError("RPI calibration permit has expired")

    try:
        calibration_digest = deployment_config_sha256(calibration_config)
        target_digest = deployment_config_sha256(target_deployment_config)
        policy_digest = strategy_policy_sha256(calibration_config, model)
        target_policy_digest = strategy_policy_sha256(
            target_deployment_config,
            model,
        )
        implementation_digest = implementation_sha256_for_model(model)
    except Exception as exc:
        raise RpiCalibrationPermitError(
            "cannot compute bound RPI calibration permit digests"
        ) from exc
    if hmac.compare_digest(calibration_digest, target_digest):
        raise RpiCalibrationPermitError(
            "calibration and target deployment config digests must be distinct"
        )
    if not hmac.compare_digest(policy_digest, target_policy_digest):
        raise RpiCalibrationPermitError(
            "calibration and target strategy policy digests must match"
        )
    _require_digest_binding(
        permit,
        "calibration_config_sha256",
        calibration_digest,
    )
    _require_digest_binding(
        permit,
        "target_deployment_config_sha256",
        target_digest,
    )
    _require_digest_binding(
        permit,
        "strategy_policy_sha256",
        policy_digest,
    )
    _require_digest_binding(
        permit,
        "implementation_sha256",
        implementation_digest,
    )

    _validate_signed_policy(
        permit.get("policy"),
        calibration_config=calibration_config,
        active_duration_sec=Decimal(
            str((expires_at - not_before).total_seconds())
        ),
    )
    _validate_signature(
        permit,
        authorized_by=authorized_by,
        trusted_signers=trusted_signers,
        verifier=signature_verifier,
    )

    permit_copy = copy.deepcopy(dict(permit))
    return {
        "permit": permit_copy,
        "permit_sha256": rpi_calibration_permit_sha256(permit_copy),
        "calibration_config_sha256": calibration_digest,
        "target_deployment_config_sha256": target_digest,
    }


def load_and_validate_rpi_calibration_permit(
    config: Mapping[str, Any],
    *,
    config_path: str | Path,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Load the target config and permit named by the calibration config."""
    if not isinstance(config, Mapping):
        raise RpiCalibrationPermitError(
            "calibration configuration must be an object"
        )
    live_launch = _mapping_field(config, "live_launch", "calibration config")
    config_file = _resolve_existing_file(
        config_path,
        base_dir=Path.cwd(),
        field="config_path",
        allow_parent_components=True,
    )
    permit_path = _resolve_existing_file(
        live_launch.get("calibration_permit_path"),
        base_dir=config_file.parent,
        field="live_launch.calibration_permit_path",
    )
    target_config_path = _resolve_existing_file(
        live_launch.get("target_deployment_config_path"),
        base_dir=config_file.parent,
        field="live_launch.target_deployment_config_path",
    )
    if len({config_file, permit_path, target_config_path}) != 3:
        raise RpiCalibrationPermitError(
            "calibration config, target config, and permit paths must differ"
        )

    permit = _read_strict_json_object(
        permit_path,
        "RPI calibration permit",
        max_bytes=1_048_576,
    )
    raw_target_config = _read_strict_json_object(
        target_config_path,
        "target deployment config",
        max_bytes=4_194_304,
    )
    try:
        from infrastructure.config_scaling import (
            normalize_root_config_preapproval,
        )

        target_config = normalize_root_config_preapproval(raw_target_config)
    except Exception as exc:
        raise RpiCalibrationPermitError(
            "cannot normalize target deployment config before permit "
            "validation"
        ) from exc
    if not isinstance(target_config, Mapping):
        raise RpiCalibrationPermitError(
            "normalized target deployment config must be an object"
        )
    trusted_signers = live_launch.get(
        "calibration_permit_trusted_signers"
    )
    if not isinstance(trusted_signers, Mapping):
        raise RpiCalibrationPermitError(
            "live_launch.calibration_permit_trusted_signers must be an object"
        )
    return validate_rpi_calibration_permit(
        permit,
        calibration_config=config,
        target_deployment_config=target_config,
        trusted_signers=trusted_signers,
        now_utc=now_utc,
        calibration_config_base_dir=config_file.parent,
        target_deployment_config_base_dir=target_config_path.parent,
    )


def _validate_signed_policy(
    policy: Any,
    *,
    calibration_config: Mapping[str, Any],
    active_duration_sec: Decimal,
) -> None:
    if not isinstance(policy, Mapping) or set(policy) != _POLICY_KEYS:
        raise RpiCalibrationPermitError(
            "RPI calibration permit policy keys are invalid"
        )

    depths = policy.get("fixed_depths_bps")
    if not isinstance(depths, list):
        raise RpiCalibrationPermitError(
            "policy.fixed_depths_bps must be a JSON array"
        )
    if not MIN_FIXED_DEPTH_LEVELS <= len(depths) <= MAX_FIXED_DEPTH_LEVELS:
        raise RpiCalibrationPermitError(
            "policy.fixed_depths_bps must contain between "
            f"{MIN_FIXED_DEPTH_LEVELS} and {MAX_FIXED_DEPTH_LEVELS} levels"
        )
    parsed_depths = [
        _positive_decimal(value, f"policy.fixed_depths_bps[{index}]")
        for index, value in enumerate(depths)
    ]
    if any(depth > MAX_FIXED_DEPTH_BPS for depth in parsed_depths):
        raise RpiCalibrationPermitError(
            "policy.fixed_depths_bps exceeds the maximum permitted depth"
        )
    if any(
        current <= previous
        for previous, current in zip(parsed_depths, parsed_depths[1:])
    ):
        raise RpiCalibrationPermitError(
            "policy.fixed_depths_bps must be strictly increasing"
        )
    if parsed_depths[-1] - parsed_depths[0] < MIN_FIXED_DEPTH_SPAN_BPS:
        raise RpiCalibrationPermitError(
            "policy.fixed_depths_bps span must be at least "
            f"{MIN_FIXED_DEPTH_SPAN_BPS} bps"
        )

    order_ttl_sec = _positive_decimal(
        policy.get("order_ttl_sec"),
        "policy.order_ttl_sec",
    )
    if order_ttl_sec > MAX_ORDER_TTL_SEC:
        raise RpiCalibrationPermitError(
            f"policy.order_ttl_sec must not exceed {MAX_ORDER_TTL_SEC}"
        )
    min_order_interval_sec = _positive_decimal(
        policy.get("min_order_interval_sec"),
        "policy.min_order_interval_sec",
    )
    if not (
        MIN_ORDER_INTERVAL_SEC
        <= min_order_interval_sec
        <= MAX_ORDER_INTERVAL_SEC
    ):
        raise RpiCalibrationPermitError(
            "policy.min_order_interval_sec is outside the permitted range"
        )
    if order_ttl_sec > min_order_interval_sec:
        raise RpiCalibrationPermitError(
            "policy.order_ttl_sec must not exceed "
            "policy.min_order_interval_sec"
        )

    max_active_orders = _positive_int(
        policy.get("max_active_orders"),
        "policy.max_active_orders",
    )
    if max_active_orders != MAX_ACTIVE_ORDERS:
        raise RpiCalibrationPermitError(
            f"policy.max_active_orders must be exactly {MAX_ACTIVE_ORDERS}"
        )
    max_order_count = _positive_int(
        policy.get("max_order_count"),
        "policy.max_order_count",
    )
    if max_order_count > MAX_ORDER_COUNT:
        raise RpiCalibrationPermitError(
            f"policy.max_order_count must not exceed {MAX_ORDER_COUNT}"
        )

    min_notional = _positive_decimal(
        policy.get("min_order_notional_usdt"),
        "policy.min_order_notional_usdt",
    )
    max_notional = _positive_decimal(
        policy.get("max_order_notional_usdt"),
        "policy.max_order_notional_usdt",
    )
    if min_notional > max_notional:
        raise RpiCalibrationPermitError(
            "policy minimum order notional must not exceed its maximum"
        )
    if max_notional > MAX_ORDER_NOTIONAL_USDT:
        raise RpiCalibrationPermitError(
            "policy.max_order_notional_usdt must not exceed "
            f"{MAX_ORDER_NOTIONAL_USDT}"
        )

    max_cumulative = _positive_decimal(
        policy.get("max_cumulative_submitted_notional_usdt"),
        "policy.max_cumulative_submitted_notional_usdt",
    )
    if max_cumulative < min_notional:
        raise RpiCalibrationPermitError(
            "policy cumulative submitted-notional cap cannot fund one "
            "minimum-notional order"
        )
    if max_cumulative > max_notional * max_order_count:
        raise RpiCalibrationPermitError(
            "policy cumulative submitted-notional cap exceeds "
            "max_order_count * max_order_notional_usdt"
        )

    max_loss = _positive_decimal(
        policy.get("max_calibration_loss_usdt"),
        "policy.max_calibration_loss_usdt",
    )
    if max_loss > MAX_CALIBRATION_LOSS_USDT:
        raise RpiCalibrationPermitError(
            "policy.max_calibration_loss_usdt must not exceed "
            f"{MAX_CALIBRATION_LOSS_USDT}"
        )

    live_launch = _mapping_field(
        calibration_config,
        "live_launch",
        "calibration config",
    )
    deployment_loss_cap = _positive_decimal(
        live_launch.get("max_deployment_loss_usdt"),
        "calibration config live_launch.max_deployment_loss_usdt",
    )
    if max_loss > deployment_loss_cap:
        raise RpiCalibrationPermitError(
            "policy calibration loss cap must not exceed the calibration "
            "deployment loss cap"
        )
    risk = _mapping_field(calibration_config, "risk", "calibration config")
    risk_limits = _mapping_field(risk, "limits", "calibration config risk")
    configured_order_cap = _positive_decimal(
        risk_limits.get("max_order_notional"),
        "calibration config risk.limits.max_order_notional",
    )
    if max_notional > configured_order_cap:
        raise RpiCalibrationPermitError(
            "policy order-notional cap must not exceed the calibration risk cap"
        )

    required_duration_sec = (
        Decimal(max_order_count - 1) * min_order_interval_sec
        + order_ttl_sec
    )
    if required_duration_sec > active_duration_sec:
        raise RpiCalibrationPermitError(
            "policy max_order_count, min_order_interval_sec, and order_ttl_sec "
            "do not fit within the permit validity window"
        )


def _validate_signature(
    permit: Mapping[str, Any],
    *,
    authorized_by: str,
    trusted_signers: Mapping[str, Any],
    verifier: PermitSignatureVerifier | None,
) -> None:
    if not isinstance(trusted_signers, Mapping) or not trusted_signers:
        raise RpiCalibrationPermitError(
            "dedicated RPI calibration permit trusted_signers are required"
        )
    trusted_public_keys: dict[str, bytes] = {}
    for raw_key_id, raw_signer in trusted_signers.items():
        key_id = _strict_string(
            raw_key_id,
            "trusted signer key_id",
            pattern=_SIGNER_KEY_ID_RE,
        )
        if not isinstance(raw_signer, Mapping) or set(raw_signer) != (
            _TRUSTED_SIGNER_KEYS
        ):
            raise RpiCalibrationPermitError(
                f"trusted signer {key_id!r} metadata keys are invalid"
            )
        if (
            raw_signer.get("algorithm")
            != RPI_CALIBRATION_SIGNATURE_ALGORITHM
        ):
            raise RpiCalibrationPermitError(
                f"trusted signer {key_id!r} algorithm must be ED25519"
            )
        trusted_public_keys[key_id] = _canonical_base64_bytes(
            raw_signer.get("public_key_base64"),
            expected_length=32,
            field=f"trusted signer {key_id!r} public_key_base64",
        )

    signature = permit.get("signature")
    if not isinstance(signature, Mapping) or set(signature) != _SIGNATURE_KEYS:
        raise RpiCalibrationPermitError(
            "RPI calibration permit signature keys are invalid"
        )
    if signature.get("algorithm") != RPI_CALIBRATION_SIGNATURE_ALGORITHM:
        raise RpiCalibrationPermitError(
            "RPI calibration permit signature algorithm must be ED25519"
        )
    key_id = _strict_string(
        signature.get("key_id"),
        "signature.key_id",
        pattern=_SIGNER_KEY_ID_RE,
    )
    if key_id not in trusted_public_keys:
        raise RpiCalibrationPermitError(
            "RPI calibration permit signature key_id is not trusted"
        )
    signer = _strict_string(
        signature.get("signer"),
        "signature.signer",
        pattern=_AUTHORIZED_BY_RE,
    )
    if signer != authorized_by:
        raise RpiCalibrationPermitError(
            "signature.signer must match permit authorized_by"
        )

    payload = rpi_calibration_permit_signature_payload(permit)
    signed_payload_digest = _sha256_value(
        signature.get("signed_payload_sha256"),
        "signature.signed_payload_sha256",
    )
    if not hmac.compare_digest(
        signed_payload_digest,
        hashlib.sha256(payload).hexdigest(),
    ):
        raise RpiCalibrationPermitError(
            "RPI calibration permit signed-payload SHA-256 mismatch"
        )
    decoded_signature = _canonical_base64_bytes(
        signature.get("signature_base64"),
        expected_length=64,
        field="signature.signature_base64",
    )
    try:
        if verifier is None:
            verified = verify_ed25519_signature(
                trusted_public_keys[key_id],
                payload,
                decoded_signature,
            )
        else:
            verified = verifier(
                RPI_CALIBRATION_SIGNATURE_ALGORITHM,
                key_id,
                payload,
                decoded_signature,
            )
    except Exception as exc:
        raise RpiCalibrationPermitError(
            "RPI calibration permit Ed25519 verifier failed closed"
        ) from exc
    if verified is not True:
        raise RpiCalibrationPermitError(
            "RPI calibration permit Ed25519 signature verification failed"
        )


def _validate_cross_config_state_isolation(
    calibration_config: Mapping[str, Any],
    target_deployment_config: Mapping[str, Any],
    *,
    calibration_base_dir: str | Path | None,
    target_base_dir: str | Path | None,
) -> None:
    try:
        from infrastructure.live_config_guard import (
            validate_live_state_path_bindings,
        )

        calibration_paths = validate_live_state_path_bindings(
            calibration_config,
            base_dir=calibration_base_dir,
        )
        target_paths = validate_live_state_path_bindings(
            target_deployment_config,
            base_dir=target_base_dir,
        )
    except Exception as exc:
        raise RpiCalibrationPermitError(
            "calibration and target durable state paths are invalid"
        ) from exc
    overlapping_paths = sorted(
        set(calibration_paths.values()) & set(target_paths.values())
    )
    if overlapping_paths:
        raise RpiCalibrationPermitError(
            "calibration and target durable state paths must be fully "
            "isolated"
        )


def _primary_model(config: Mapping[str, Any], label: str) -> str:
    strategy = _mapping_field(config, "strategy", label)
    raw_model = strategy.get("primary_model", strategy.get("name"))
    try:
        return canonical_model_key(raw_model)
    except (TypeError, ValueError) as exc:
        raise RpiCalibrationPermitError(
            f"{label} requires a supported primary model"
        ) from exc


def _single_symbol(config: Mapping[str, Any], label: str) -> str:
    symbols = config.get("symbols")
    if not isinstance(symbols, (list, tuple)) or len(symbols) != 1:
        raise RpiCalibrationPermitError(
            f"{label} must contain exactly one symbol"
        )
    symbol = _strict_string(
        symbols[0],
        f"{label} symbol",
        pattern=_SYMBOL_RE,
    )
    if symbol != symbol.upper():
        raise RpiCalibrationPermitError(
            f"{label} symbol must use canonical uppercase form"
        )
    return symbol


def _mapping_field(
    value: Mapping[str, Any],
    key: str,
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RpiCalibrationPermitError(f"{label} must be an object")
    section = value.get(key)
    if not isinstance(section, Mapping):
        raise RpiCalibrationPermitError(f"{label} {key} must be an object")
    return section


def _strict_string(
    value: Any,
    field: str,
    *,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise RpiCalibrationPermitError(
            f"{field} must be a non-empty canonical string"
        )
    if pattern is not None and not pattern.fullmatch(value):
        raise RpiCalibrationPermitError(f"{field} has an invalid format")
    return value


def _positive_decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RpiCalibrationPermitError(
            f"{field} must be a finite positive JSON number"
        )
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RpiCalibrationPermitError(
            f"{field} must be a finite positive JSON number"
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise RpiCalibrationPermitError(
            f"{field} must be a finite positive JSON number"
        )
    return parsed


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RpiCalibrationPermitError(
            f"{field} must be a positive integer"
        )
    return value


def _sha256_value(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise RpiCalibrationPermitError(
            f"{field} must be a lowercase 64-character SHA-256 digest"
        )
    return value


def _require_digest_binding(
    permit: Mapping[str, Any],
    field: str,
    expected: str,
) -> None:
    declared = _sha256_value(permit.get(field), field)
    if not hmac.compare_digest(declared, expected):
        raise RpiCalibrationPermitError(
            f"RPI calibration permit {field} binding mismatch"
        )


def _parse_utc_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise RpiCalibrationPermitError(
            f"{field} must be an ISO-8601 UTC timestamp"
        )
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise RpiCalibrationPermitError(
            f"{field} must be an ISO-8601 UTC timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(
        parsed
    ):
        raise RpiCalibrationPermitError(
            f"{field} must include an explicit UTC offset"
        )
    return parsed.astimezone(timezone.utc)


def _effective_now_utc(now_utc: datetime | None) -> datetime:
    effective = now_utc or datetime.now(timezone.utc)
    if not isinstance(effective, datetime) or effective.tzinfo is None:
        raise RpiCalibrationPermitError(
            "now_utc must be a timezone-aware datetime"
        )
    return effective.astimezone(timezone.utc)


def _canonical_base64_bytes(
    value: Any,
    *,
    expected_length: int,
    field: str,
) -> bytes:
    if not isinstance(value, str) or value != value.strip():
        raise RpiCalibrationPermitError(
            f"{field} must be canonical base64"
        )
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RpiCalibrationPermitError(
            f"{field} must be canonical base64"
        ) from exc
    if (
        len(decoded) != expected_length
        or base64.b64encode(decoded).decode("ascii") != value
    ):
        raise RpiCalibrationPermitError(
            f"{field} must encode exactly {expected_length} bytes"
        )
    return decoded


def _canonical_json_bytes(value: Any, label: str) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise RpiCalibrationPermitError(
            f"{label} is not canonical JSON"
        ) from exc


def _reject_json_constant(value: str) -> None:
    raise RpiCalibrationPermitError(
        f"non-standard JSON number {value!r} is not allowed"
    )


def _object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RpiCalibrationPermitError(
                f"duplicate JSON object key {key!r} is not allowed"
            )
        result[key] = value
    return result


def _resolve_existing_file(
    value: Any,
    *,
    base_dir: Path,
    field: str,
    allow_parent_components: bool = False,
) -> Path:
    if isinstance(value, Path):
        raw = str(value)
    elif isinstance(value, str):
        raw = value
    else:
        raw = ""
    if not raw or raw != raw.strip():
        raise RpiCalibrationPermitError(f"{field} must be configured")
    path_parts = tuple(
        part
        for part in raw.replace("\\", "/").split("/")
        if part not in {"", "."}
    )
    if not allow_parent_components and ".." in path_parts:
        raise RpiCalibrationPermitError(
            f"{field} must not contain '..' components"
        )
    normalized = raw.replace("\\", os.sep).replace("/", os.sep)
    candidate = Path(normalized)
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RpiCalibrationPermitError(
            f"{field} cannot be resolved to an existing file"
        ) from exc
    if not resolved.is_file():
        raise RpiCalibrationPermitError(f"{field} must resolve to a file")
    return resolved


def _read_strict_json_object(
    path: Path,
    label: str,
    *,
    max_bytes: int,
) -> dict[str, Any]:
    try:
        size = path.stat().st_size
        if size > max_bytes:
            raise RpiCalibrationPermitError(
                f"{label} exceeds the {max_bytes}-byte size limit"
            )
        raw = path.read_bytes()
    except OSError as exc:
        raise RpiCalibrationPermitError(
            f"cannot read {label} at {path}"
        ) from exc
    return parse_rpi_calibration_permit_json(raw, label=label)


__all__ = [
    "MAX_ACTIVE_ORDERS",
    "MAX_CALIBRATION_LOSS_USDT",
    "MAX_ORDER_COUNT",
    "MAX_ORDER_NOTIONAL_USDT",
    "MAX_PERMIT_TTL_SEC",
    "PermitSignatureVerifier",
    "RPI_CALIBRATION_PERMIT_SCHEMA",
    "RPI_CALIBRATION_SIGNATURE_ALGORITHM",
    "RPI_CALIBRATION_SIGNATURE_DOMAIN",
    "RPI_CALIBRATION_STAGE",
    "RPI_CALIBRATION_TARGET_STAGE",
    "RPI_CALIBRATION_VENUE",
    "RpiCalibrationPermitError",
    "load_and_validate_rpi_calibration_permit",
    "parse_rpi_calibration_permit_json",
    "rpi_calibration_permit_sha256",
    "rpi_calibration_permit_signature_payload",
    "validate_rpi_calibration_permit",
]
