"""Strict schema for the durable sidecar state payload."""

from __future__ import annotations

import math
from collections.abc import Mapping

from risk.exchange_port import FlatProof

SCHEMA_VERSION = 2

_STAGES = frozenset(
    {
        "ARMED",
        "KILL",
        "CANCEL_PENDING",
        "CANCEL_VERIFIED",
        "FLATTENING",
        "FLAT_VERIFIED",
        "QUIESCED",
        "FAILED",
    }
)
_FLOAT_DEFAULTS = {
    "quiesced_at": 0.0,
    "day_start_equity": 0.0,
    "day_start_external_cash_flow_total": 0.0,
    "peak_adjusted_equity": 0.0,
    "last_equity": 0.0,
    "deployment_start_equity": 0.0,
    "deployment_start_external_cash_flow_total": 0.0,
    "deployment_adjusted_equity": 0.0,
    "deployment_loss": 0.0,
}
_NONNEGATIVE_FLOATS = frozenset(
    {
        "peak_adjusted_equity",
        "deployment_loss",
    }
)
_STRING_DEFAULTS = {
    "kill_reason": "",
    "quiesce_reason": "",
    "risk_day": "",
}
_MIGRATION_FIELDS = frozenset(
    {
        "legacy_source_schema",
        "legacy_source_sha256",
        "legacy_event",
        "legacy_updated_at",
        "legacy_writer_pid",
        "declared_account_equity",
        "max_deployed_capital",
        "deployment_policy_fingerprint",
        "account_key_fingerprint",
        "manual_rearm_required",
        "flat_proof_id",
        "deployment_baseline_pending",
    }
)
_ALLOWED_FIELDS = frozenset(
    {
        "schema_version",
        "account_scope_id",
        "deployment_id",
        "kill_latched",
        "stage",
        "quiesced",
        "cash_flow_deployment_start_ms",
        "last_flat_proof",
        *_FLOAT_DEFAULTS,
        *_STRING_DEFAULTS,
        *_MIGRATION_FIELDS,
    }
)
_PROOF_FIELDS = frozenset(FlatProof.__dataclass_fields__)


class SidecarStatePayloadError(ValueError):
    """Raised when a durable state payload violates schema v2."""


def _strict_bool(value, field: str) -> bool:
    if not isinstance(value, bool):
        raise SidecarStatePayloadError(f"state_payload_{field}_invalid")
    return value


def _strict_int(value, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SidecarStatePayloadError(f"state_payload_{field}_invalid")
    if value < minimum:
        raise SidecarStatePayloadError(f"state_payload_{field}_invalid")
    return value


def _finite_float(value, field: str) -> float:
    if isinstance(value, bool):
        raise SidecarStatePayloadError(f"state_payload_{field}_invalid")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise SidecarStatePayloadError(
            f"state_payload_{field}_invalid"
        ) from exc
    if not math.isfinite(parsed):
        raise SidecarStatePayloadError(f"state_payload_{field}_invalid")
    if field in _NONNEGATIVE_FLOATS and parsed < 0.0:
        raise SidecarStatePayloadError(f"state_payload_{field}_negative")
    return parsed


def _strict_string(value, field: str, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise SidecarStatePayloadError(f"state_payload_{field}_invalid")
    parsed = value.strip() if required else value
    if required and not parsed:
        raise SidecarStatePayloadError(f"state_payload_{field}_missing")
    return parsed


def _parse_flat_proof(value) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise SidecarStatePayloadError("state_payload_flat_proof_invalid")
    unknown = set(value) - _PROOF_FIELDS
    missing = _PROOF_FIELDS - set(value)
    if unknown or missing:
        raise SidecarStatePayloadError("state_payload_flat_proof_invalid")
    try:
        proof = FlatProof(**dict(value))
    except (TypeError, ValueError) as exc:
        raise SidecarStatePayloadError(
            "state_payload_flat_proof_invalid"
        ) from exc
    for field in (
        "proof_id",
        "purpose",
        "account_scope_id",
        "deployment_id",
        "state_sha256",
        "snapshot_digest",
    ):
        _strict_string(getattr(proof, field), f"flat_proof_{field}", required=True)
    for field in (
        "writer_epoch",
        "owner_epoch",
        "safety_epoch",
        "generation",
        "first_truth_sequence",
        "last_truth_sequence",
        "sample_count",
        "open_order_count",
        "nonzero_position_count",
    ):
        _strict_int(getattr(proof, field), f"flat_proof_{field}")
    _finite_float(proof.verified_monotonic, "flat_proof_verified_monotonic")
    _finite_float(proof.valid_until_monotonic, "flat_proof_valid_until_monotonic")
    if (
        proof.sample_count < 2
        or proof.last_truth_sequence <= proof.first_truth_sequence
        or proof.open_order_count != 0
        or proof.nonzero_position_count != 0
    ):
        raise SidecarStatePayloadError("state_payload_flat_proof_not_flat")
    if proof.valid_until_monotonic < proof.verified_monotonic:
        raise SidecarStatePayloadError("state_payload_flat_proof_expiry_invalid")
    return dict(value)


def parse_sidecar_state_payload(
    payload: Mapping,
    *,
    account_scope_id: str,
    deployment_id: str,
) -> dict:
    """Validate and normalize a complete schema-v2 state projection."""
    if not isinstance(payload, Mapping):
        raise SidecarStatePayloadError("state_payload_not_object")
    unknown = set(payload) - _ALLOWED_FIELDS
    if unknown:
        raise SidecarStatePayloadError(
            "state_payload_unknown_fields:" + ",".join(sorted(unknown))
        )
    version = _strict_int(payload.get("schema_version"), "schema_version")
    if version != SCHEMA_VERSION:
        raise SidecarStatePayloadError("state_payload_schema_unsupported")
    expected_scope = _strict_string(
        account_scope_id,
        "expected_account_scope_id",
        required=True,
    )
    expected_deployment = _strict_string(
        deployment_id,
        "expected_deployment_id",
        required=True,
    )
    actual_scope = _strict_string(
        payload.get("account_scope_id", expected_scope),
        "account_scope_id",
        required=True,
    )
    actual_deployment = _strict_string(
        payload.get("deployment_id", expected_deployment),
        "deployment_id",
        required=True,
    )
    if actual_scope != expected_scope:
        raise SidecarStatePayloadError("state_payload_account_scope_mismatch")
    if actual_deployment != expected_deployment:
        raise SidecarStatePayloadError("state_payload_deployment_mismatch")

    kill_latched = _strict_bool(payload.get("kill_latched"), "kill_latched")
    quiesced = _strict_bool(payload.get("quiesced", False), "quiesced")
    stage = _strict_string(payload.get("stage"), "stage", required=True).upper()
    if stage not in _STAGES:
        raise SidecarStatePayloadError("state_payload_stage_unsupported")
    if quiesced != (stage == "QUIESCED"):
        raise SidecarStatePayloadError("state_payload_quiesce_stage_mismatch")
    if not kill_latched and stage in {
        "KILL",
        "FLATTENING",
        "FLAT_VERIFIED",
        "FAILED",
    }:
        raise SidecarStatePayloadError("state_payload_kill_stage_mismatch")

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "account_scope_id": actual_scope,
        "deployment_id": actual_deployment,
        "kill_latched": kill_latched,
        "stage": stage,
        "quiesced": quiesced,
        "cash_flow_deployment_start_ms": _strict_int(
            payload.get("cash_flow_deployment_start_ms", 0),
            "cash_flow_deployment_start_ms",
        ),
    }
    for field, default in _STRING_DEFAULTS.items():
        normalized[field] = _strict_string(payload.get(field, default), field)
    for field, default in _FLOAT_DEFAULTS.items():
        normalized[field] = _finite_float(payload.get(field, default), field)
    normalized["last_flat_proof"] = _parse_flat_proof(
        payload.get("last_flat_proof")
    )
    if normalized["last_flat_proof"] is not None:
        proof = normalized["last_flat_proof"]
        if proof["account_scope_id"] != actual_scope:
            raise SidecarStatePayloadError(
                "state_payload_flat_proof_account_scope_mismatch"
            )
        if proof["deployment_id"] != actual_deployment:
            raise SidecarStatePayloadError(
                "state_payload_flat_proof_deployment_mismatch"
            )

    for field in _MIGRATION_FIELDS & set(payload):
        value = payload[field]
        if field in {
            "manual_rearm_required",
            "deployment_baseline_pending",
        }:
            normalized[field] = _strict_bool(value, field)
        elif field in {
            "legacy_source_schema",
            "legacy_writer_pid",
        }:
            normalized[field] = _strict_int(value, field)
        elif field in {
            "legacy_updated_at",
            "declared_account_equity",
            "max_deployed_capital",
        }:
            normalized[field] = _finite_float(value, field)
        else:
            normalized[field] = _strict_string(value, field)
    return normalized
