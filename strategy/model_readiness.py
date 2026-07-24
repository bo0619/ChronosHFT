from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from alpha.rpi_intensity import (
    RPIExposureBin,
    RPIIntensityRequirements,
    estimate_rpi_intensity,
)
from data.oos_reconstruction import (
    OOS_RECONSTRUCTION_SCHEMA,
    RAW_OOS_EVIDENCE_SCHEMA,
)
from strategy.formula_governance import (
    LIVE_APPROVED_FORMULA_VERSIONS,
)
from strategy.quote_math import (
    AS_FORMULA_VERSION,
    GLFT_FORMULA_VERSION,
    UNITS_VERSION,
)
from strategy.registry import effective_primary_strategy_config

CALIBRATION_APPROVAL_SCHEMA = "chronoshft.calibration_approval.v2"
GLFT_CALIBRATION_ARTIFACT_SCHEMA = "chronoshft.glft_rpi_calibration.v3"
GLFT_CALIBRATION_DATA_SOURCE = "LIVE_BINANCE_RPI_ACK"
GLFT_CALIBRATION_VENUE = "BINANCE_USDM"
GLFT_CALIBRATION_SOURCE_EVIDENCE_SCHEMA = (
    "chronoshft.glft_rpi_source_evidence.v4"
)
DEPLOYMENT_CONFIG_PROJECTION_SCHEMA = (
    "chronoshft.live_deployment_config_projection.v1"
)
CALIBRATION_EVIDENCE_BUNDLE_SCHEMA = (
    "chronoshft.calibration_evidence_bundle.v1"
)
APPROVAL_SIGNATURE_ALGORITHM = "ED25519"
APPROVAL_SIGNATURE_DOMAIN = b"chronoshft.calibration_approval.v2\0"
OOS_EVIDENCE_DOMAIN = b"chronoshft.glft_rpi_oos_evidence.v2\0"
IMPLEMENTED_UNITS_VERSION = UNITS_VERSION
IMPLEMENTED_FORMULA_VERSIONS = {
    "glft": GLFT_FORMULA_VERSION,
    "avellaneda_stoikov": AS_FORMULA_VERSION,
}

DEFAULT_MODEL_READINESS_CONFIG = {
    "enabled": True,
    "min_volatility_samples": 50,
    "min_model_samples": 30,
    "models": {
        "glft": {
            "min_volatility_samples": 50,
            "min_model_samples": 30,
        },
        "avellaneda_stoikov": {
            "min_volatility_samples": 50,
            "min_model_samples": 60,
        },
    },
    "live_approval": {
        "manifest_path": "",
        "min_data_duration_sec": 604_800,
        "min_oos_samples": 10_000,
        "trusted_signers": {},
    },
}

LIVE_MIN_VOLATILITY_SAMPLES = 50
LIVE_MIN_MODEL_SAMPLES = 30
LIVE_MIN_DATA_DURATION_SEC = 604_800.0
LIVE_MIN_OOS_SAMPLES = 10_000
LIVE_MIN_OOS_FILLS = 100
LIVE_REQUIRED_MARKOUT_HORIZONS_MS = (1_000, 5_000)
LIVE_MIN_OOS_UTC_DAY_CLUSTERS = 5
LIVE_MAX_MARKOUT_LAG_MS = 2_000
LIVE_MAX_PNL_CROSSCHECK_TOLERANCE_USDT = Decimal("0.000001")

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SIGNER_KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
_MODEL_ALIASES = {
    "glft": "glft",
    "glftmultiscale": "glft",
    "glft_multiscale": "glft",
    "as": "avellaneda_stoikov",
    "avellanedastoikov": "avellaneda_stoikov",
    "avellaneda_stoikov": "avellaneda_stoikov",
}
_COMMON_LIVE_IMPLEMENTATION_SOURCE_FILES = (
    "data/cache.py",
    "data/live_evidence.py",
    "data/oos_reconstruction.py",
    "data/orderbook.py",
    "data/ref_data.py",
    "event/type.py",
    "gateway/binance/constants.py",
    "gateway/binance/gateway.py",
    "gateway/binance/rest_api.py",
    "gateway/binance/rest_metrics.py",
    "gateway/binance/truth_provider.py",
    "gateway/binance/ws_api.py",
    "infrastructure/admin_control.py",
    "infrastructure/commission_truth.py",
    "infrastructure/config_scaling.py",
    "infrastructure/external_alerts.py",
    "infrastructure/live_config_guard.py",
    "infrastructure/rpi_calibration_permit.py",
    "infrastructure/rpi_policy.py",
    "infrastructure/single_writer_fence.py",
    "infrastructure/system_health.py",
    "infrastructure/time_service.py",
    "infrastructure/truth_monitor.py",
    "infrastructure/venue_supervisor.py",
    "infrastructure/watchdog.py",
    "launcher.py",
    "main.py",
    "oms/account_manager.py",
    "oms/engine.py",
    "oms/exposure.py",
    "oms/journal.py",
    "oms/order.py",
    "oms/order_manager.py",
    "oms/sequence.py",
    "oms/validator.py",
    "risk/deployment_loss.py",
    "risk/funding_guard.py",
    "risk/independent_supervisor.py",
    "risk/manager.py",
    "scripts/build_live_oos_evidence.py",
    "strategy/base.py",
    "strategy/model_readiness.py",
    "strategy/quote_math.py",
    "strategy/registry.py",
    "strategy/runtime.py",
)
_IMPLEMENTATION_SOURCE_FILES = {
    "glft": (
        "alpha/engine.py",
        "alpha/factors.py",
        "alpha/gate.py",
        "alpha/rpi_intensity.py",
        "alpha/signal.py",
        "scripts/build_rpi_calibration_artifact.py",
        "strategy/glft.py",
        *_COMMON_LIVE_IMPLEMENTATION_SOURCE_FILES,
    ),
    "avellaneda_stoikov": (
        "strategy/avellaneda_stoikov.py",
        *_COMMON_LIVE_IMPLEMENTATION_SOURCE_FILES,
    ),
}

ApprovalSignatureVerifier = Callable[[str, str, bytes, bytes], bool]


@dataclass(frozen=True)
class ReadinessRequirements:
    enabled: bool
    min_volatility_samples: int
    min_model_samples: int


@dataclass(frozen=True)
class SymbolReadiness:
    model: str
    ready: bool
    state: str
    volatility_samples: int
    min_volatility_samples: int
    model_samples: int
    min_model_samples: int
    reasons: tuple[str, ...]

    def as_params(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "state": self.state,
            "model": self.model,
            "volatility_samples": self.volatility_samples,
            "min_volatility_samples": self.min_volatility_samples,
            "model_samples": self.model_samples,
            "min_model_samples": self.min_model_samples,
            "reasons": list(self.reasons),
            "units_version": IMPLEMENTED_UNITS_VERSION,
            "validated_formula_version": formula_version_for_model(self.model),
            "formula_live_approved": (
                formula_version_for_model(self.model)
                in LIVE_APPROVED_FORMULA_VERSIONS
            ),
        }


def canonical_model_key(value: Any) -> str:
    normalized = "".join(
        character
        for character in str(value or "").casefold()
        if character.isalnum() or character == "_"
    )
    model = _MODEL_ALIASES.get(normalized)
    if model is None:
        raise ValueError(f"Unsupported market-making model {value!r}")
    return model


def formula_version_for_model(model: Any) -> str:
    return IMPLEMENTED_FORMULA_VERSIONS[canonical_model_key(model)]


def apply_model_readiness_defaults(strategy_config: dict) -> dict:
    if not isinstance(strategy_config, dict):
        raise TypeError("strategy configuration must be a JSON object")
    _merge_missing(strategy_config, {"model_readiness": DEFAULT_MODEL_READINESS_CONFIG})
    return strategy_config


def readiness_requirements(
    strategy_config: Mapping[str, Any] | None,
    model: Any,
) -> ReadinessRequirements:
    config = strategy_config if isinstance(strategy_config, Mapping) else {}
    raw = config.get("model_readiness")
    if not isinstance(raw, Mapping):
        # Direct strategy construction is used by isolated unit fixtures. The
        # root loader always injects explicit production defaults.
        return ReadinessRequirements(False, 0, 0)

    model_key = canonical_model_key(model)
    settings = dict(raw)
    model_settings = raw.get("models", {})
    if model_settings is not None and not isinstance(model_settings, Mapping):
        raise ValueError("strategy.model_readiness.models must be an object")
    for configured_model, configured_values in (model_settings or {}).items():
        try:
            configured_key = canonical_model_key(configured_model)
        except ValueError:
            continue
        if configured_key != model_key:
            continue
        if not isinstance(configured_values, Mapping):
            raise ValueError(
                f"strategy.model_readiness.models.{configured_model} must be an object"
            )
        settings.update(configured_values)

    return ReadinessRequirements(
        enabled=_strict_bool(
            settings.get("enabled", True),
            "strategy.model_readiness.enabled",
        ),
        min_volatility_samples=_nonnegative_int(
            settings.get("min_volatility_samples", 0),
            "strategy.model_readiness.min_volatility_samples",
        ),
        min_model_samples=_nonnegative_int(
            settings.get("min_model_samples", 0),
            "strategy.model_readiness.min_model_samples",
        ),
    )


def evaluate_symbol_readiness(
    model: Any,
    requirements: ReadinessRequirements,
    *,
    volatility_samples: Any,
    model_samples: Any,
) -> SymbolReadiness:
    model_key = canonical_model_key(model)
    volatility_count = _sample_count(volatility_samples)
    model_count = _sample_count(model_samples)
    reasons = []
    if requirements.enabled:
        if volatility_count < requirements.min_volatility_samples:
            reasons.append(
                "volatility_samples:"
                f"{volatility_count}<{requirements.min_volatility_samples}"
            )
        if model_count < requirements.min_model_samples:
            reasons.append(
                f"model_samples:{model_count}<{requirements.min_model_samples}"
            )
    ready = not reasons
    return SymbolReadiness(
        model=model_key,
        ready=ready,
        state="READY" if ready else "WARMING_UP",
        volatility_samples=volatility_count,
        min_volatility_samples=requirements.min_volatility_samples,
        model_samples=model_count,
        min_model_samples=requirements.min_model_samples,
        reasons=tuple(reasons),
    )


def _validate_live_calibration_approval_with_fence_held(
    config: Mapping[str, Any],
    *,
    config_path: str | Path,
    approved_formula_versions: set[str] | frozenset[str] | None = None,
    now_utc: datetime | None = None,
    signature_verifier: ApprovalSignatureVerifier | None = None,
    expected_locked_journal_path: Path | None = None,
) -> dict[str, Any]:
    strategy = config.get("strategy", {})
    if not isinstance(strategy, Mapping):
        raise ValueError("strategy must be an object")

    model = canonical_model_key(strategy.get("primary_model", strategy.get("name")))
    requirements = readiness_requirements(strategy, model)
    if not requirements.enabled:
        raise ValueError("Live trading requires strategy.model_readiness.enabled=true")
    if requirements.min_volatility_samples < LIVE_MIN_VOLATILITY_SAMPLES:
        raise ValueError(
            "Live trading requires at least "
            f"{LIVE_MIN_VOLATILITY_SAMPLES} volatility samples"
        )
    if requirements.min_model_samples < LIVE_MIN_MODEL_SAMPLES:
        raise ValueError(
            "Live trading requires at least "
            f"{LIVE_MIN_MODEL_SAMPLES} model samples"
        )

    readiness = strategy.get("model_readiness", {})
    approval = readiness.get("live_approval", {})
    if not isinstance(approval, Mapping):
        raise ValueError("strategy.model_readiness.live_approval must be an object")
    raw_manifest_path = str(approval.get("manifest_path", "") or "").strip()
    if not raw_manifest_path:
        raise ValueError(
            "Live trading requires "
            "strategy.model_readiness.live_approval.manifest_path"
        )

    config_file = Path(config_path).resolve()
    manifest_path = _resolve_path(config_file.parent, raw_manifest_path)
    manifest = _read_json_object(manifest_path, "calibration approval manifest")

    required_manifest_keys = {
        "schema",
        "model",
        "symbols",
        "units_version",
        "validated_formula_version",
        "deployment_config_sha256",
        "data_duration_sec",
        "oos_samples",
        "approved",
        "approved_by",
        "approved_at",
        "artifact_path",
        "artifact_sha256",
        "source_data_path",
        "source_data_sha256",
        "oos_evidence_sha256",
        "evidence_bundle_sha256",
        "formula_sha256",
        "strategy_config_sha256",
        "signature",
    }
    manifest_keys = set(manifest)
    manifest_keys.discard("_warning")
    if manifest_keys != required_manifest_keys:
        raise ValueError("Calibration approval manifest keys are invalid")
    if manifest.get("schema") != CALIBRATION_APPROVAL_SCHEMA:
        raise ValueError(
            "Calibration approval manifest schema must be "
            f"{CALIBRATION_APPROVAL_SCHEMA!r}"
        )
    if canonical_model_key(manifest.get("model")) != model:
        raise ValueError("Calibration approval model does not match primary_model")

    configured_symbols = _normalized_symbols(config.get("symbols"))
    approved_symbols = _normalized_symbols(manifest.get("symbols"))
    if not configured_symbols or approved_symbols != configured_symbols:
        raise ValueError(
            "Calibration approval symbols must exactly match configured symbols"
        )

    if manifest.get("units_version") != IMPLEMENTED_UNITS_VERSION:
        raise ValueError(
            "Calibration approval units_version does not match this build"
        )
    implemented_formula = formula_version_for_model(model)
    if manifest.get("validated_formula_version") != implemented_formula:
        raise ValueError(
            "Calibration approval validated_formula_version does not match this build"
        )

    allowed_versions = (
        LIVE_APPROVED_FORMULA_VERSIONS
        if approved_formula_versions is None
        else frozenset(approved_formula_versions)
    )
    if implemented_formula not in allowed_versions:
        raise ValueError(
            f"Formula {implemented_formula!r} is not live-approved by this build"
        )
    if manifest.get("approved") is not True:
        raise ValueError("Calibration approval manifest must contain approved=true")

    min_duration = max(
        LIVE_MIN_DATA_DURATION_SEC,
        _positive_finite(
            approval.get("min_data_duration_sec", LIVE_MIN_DATA_DURATION_SEC),
            "strategy.model_readiness.live_approval.min_data_duration_sec",
        ),
    )
    data_duration = _positive_finite(
        manifest.get("data_duration_sec"),
        "calibration approval data_duration_sec",
    )
    if data_duration < min_duration:
        raise ValueError(
            f"Calibration data duration {data_duration:g}s is below {min_duration:g}s"
        )

    min_oos_samples = max(
        LIVE_MIN_OOS_SAMPLES,
        _positive_int(
            approval.get("min_oos_samples", LIVE_MIN_OOS_SAMPLES),
            "strategy.model_readiness.live_approval.min_oos_samples",
        ),
    )
    oos_samples = _positive_int(
        manifest.get("oos_samples"),
        "calibration approval oos_samples",
    )
    if oos_samples < min_oos_samples:
        raise ValueError(
            f"Calibration OOS samples {oos_samples} are below {min_oos_samples}"
        )

    artifact_path_value = str(manifest.get("artifact_path", "") or "").strip()
    if not artifact_path_value:
        raise ValueError("Calibration approval artifact_path is required")
    artifact_sha256 = _sha256_value(
        manifest.get("artifact_sha256"),
        "calibration approval artifact_sha256",
    )
    source_data_path_value = str(
        manifest.get("source_data_path", "") or ""
    ).strip()
    if not source_data_path_value:
        raise ValueError("Calibration approval source_data_path is required")
    source_data_sha256 = _sha256_value(
        manifest.get("source_data_sha256"),
        "calibration approval source_data_sha256",
    )
    if artifact_sha256 == source_data_sha256:
        raise ValueError(
            "Calibration artifact and source-data hashes must identify distinct inputs"
        )

    artifact_path = _resolve_path(manifest_path.parent, artifact_path_value)
    artifact_document = _read_hashed_json_object(
        artifact_path,
        "GLFT RPI calibration artifact",
        expected_sha256=artifact_sha256,
        mismatch_message="Calibration artifact SHA-256 mismatch",
    )
    source_data_path = _resolve_path(
        manifest_path.parent,
        source_data_path_value,
    )
    source_evidence_document = _read_hashed_json_object(
        source_data_path,
        "GLFT calibration source evidence",
        expected_sha256=source_data_sha256,
        mismatch_message="Calibration source-data SHA-256 mismatch",
    )

    formula_sha256 = _sha256_value(
        manifest.get("formula_sha256"),
        "calibration approval formula_sha256",
    )
    if implementation_sha256_for_model(model) != formula_sha256:
        raise ValueError(
            "Calibration approval implementation-bundle SHA-256 mismatch"
        )

    configured_policy_sha256 = strategy_policy_sha256(config, model)
    approved_policy_sha256 = _sha256_value(
        manifest.get("strategy_config_sha256"),
        "calibration approval strategy_config_sha256",
    )
    if approved_policy_sha256 != configured_policy_sha256:
        raise ValueError(
            "Calibration approval strategy configuration SHA-256 mismatch"
        )
    configured_deployment_sha256 = deployment_config_sha256(config)
    approved_deployment_sha256 = _sha256_value(
        manifest.get("deployment_config_sha256"),
        "calibration approval deployment_config_sha256",
    )
    if approved_deployment_sha256 != configured_deployment_sha256:
        raise ValueError(
            "Calibration approval deployment configuration SHA-256 mismatch"
        )

    approved_by = str(manifest.get("approved_by", "") or "").strip()
    approved_at = str(manifest.get("approved_at", "") or "").strip()
    if not approved_by or not approved_at:
        raise ValueError(
            "Calibration approval approved_by and approved_at are required"
        )
    if model != "glft":
        raise ValueError(
            "Live source-evidence validation is not implemented for "
            f"{model!r}"
        )
    source_evidence = _validate_glft_source_evidence(
        source_data_path,
        configured_symbols=configured_symbols,
        strategy_config_sha256=configured_policy_sha256,
        target_deployment_config_sha256=configured_deployment_sha256,
        implementation_sha256=formula_sha256,
        deployment_id=_configured_deployment_id(config),
        calibration_artifact_sha256=artifact_sha256,
        formula_version=implemented_formula,
        min_data_duration_sec=min_duration,
        min_oos_samples=min_oos_samples,
        max_deployment_loss_usdt=_configured_deployment_loss_cap(config),
        target_deployment_config=config,
        target_deployment_config_path=config_file,
        expected_locked_journal_path=expected_locked_journal_path,
        evidence_document=source_evidence_document,
    )
    if not math.isclose(
        data_duration,
        source_evidence["data_duration_sec"],
        rel_tol=0.0,
        abs_tol=1.0,
    ):
        raise ValueError(
            "Calibration approval data_duration_sec does not match source "
            "evidence"
        )
    if oos_samples != source_evidence["oos_samples"]:
        raise ValueError(
            "Calibration approval oos_samples does not match source evidence"
        )
    approved_at_utc = _parse_utc_timestamp(
        approved_at,
        "calibration approval approved_at",
    )
    if approved_at_utc < source_evidence["capture_ended_at_utc"]:
        raise ValueError(
            "Calibration approval approved_at must not precede data capture end"
        )
    effective_now = now_utc or datetime.now(timezone.utc)
    if effective_now.tzinfo is None:
        effective_now = effective_now.replace(tzinfo=timezone.utc)
    else:
        effective_now = effective_now.astimezone(timezone.utc)
    if approved_at_utc > effective_now:
        raise ValueError(
            "Calibration approval approved_at must not be in the future"
        )
    result = dict(manifest)
    result["_validated_source_evidence"] = source_evidence
    if model == "glft":
        result["_runtime_calibration"] = _validate_glft_calibration_artifact(
            configured_symbols=configured_symbols,
            strategy_config=strategy,
            min_model_samples=requirements.min_model_samples,
            strategy_config_sha256=configured_policy_sha256,
            deployment_config_sha256=configured_deployment_sha256,
            implementation_sha256=formula_sha256,
            deployment_id=source_evidence["deployment_id"],
            source_evidence=source_evidence,
            artifact_document=artifact_document,
        )
    approved_oos_sha256 = _sha256_value(
        manifest.get("oos_evidence_sha256"),
        "calibration approval oos_evidence_sha256",
    )
    if approved_oos_sha256 != source_evidence["oos_evidence_sha256"]:
        raise ValueError(
            "Calibration approval OOS evidence SHA-256 mismatch"
        )
    expected_bundle_sha256 = calibration_evidence_bundle_sha256(
        artifact_sha256=artifact_sha256,
        source_data_sha256=source_data_sha256,
        oos_sha256=approved_oos_sha256,
        deployment_config_digest=configured_deployment_sha256,
        strategy_policy_digest=configured_policy_sha256,
        implementation_digest=formula_sha256,
    )
    if (
        _sha256_value(
            manifest.get("evidence_bundle_sha256"),
            "calibration approval evidence_bundle_sha256",
        )
        != expected_bundle_sha256
    ):
        raise ValueError(
            "Calibration approval evidence-bundle SHA-256 mismatch"
        )
    _validate_approval_signature(
        manifest,
        approval_config=approval,
        verifier=signature_verifier,
    )
    return result


def _approval_journal_fence_inputs(
    config: Mapping[str, Any],
    *,
    config_path: str | Path,
) -> tuple[Path, Mapping[str, Any], Path] | None:
    strategy = config.get("strategy")
    if not isinstance(strategy, Mapping) or canonical_model_key(
        strategy.get("primary_model", strategy.get("name"))
    ) != "glft":
        return None
    readiness = strategy.get("model_readiness")
    approval = (
        readiness.get("live_approval")
        if isinstance(readiness, Mapping)
        else None
    )
    if not isinstance(approval, Mapping):
        return None
    raw_manifest_path = str(
        approval.get("manifest_path", "") or ""
    ).strip()
    if not raw_manifest_path:
        return None

    config_file = Path(config_path).resolve()
    manifest_path = _resolve_path(config_file.parent, raw_manifest_path)
    manifest = _read_json_object(
        manifest_path,
        "calibration approval manifest",
    )
    source_path_value = str(
        manifest.get("source_data_path", "") or ""
    ).strip()
    if not source_path_value:
        raise ValueError("Calibration approval source_data_path is required")
    source_path = _resolve_path(manifest_path.parent, source_path_value)
    source_evidence = _read_json_object(
        source_path,
        "GLFT calibration source evidence",
    )
    calibration_path_value = str(
        source_evidence.get("calibration_config_path", "") or ""
    ).strip()
    journal = source_evidence.get("journal")
    journal_path_value = (
        str(journal.get("path", "") or "").strip()
        if isinstance(journal, Mapping)
        else ""
    )
    if not calibration_path_value:
        raise ValueError(
            "GLFT source evidence calibration_config_path is required"
        )
    if not journal_path_value:
        raise ValueError("GLFT source evidence journal.path is required")
    calibration_path = _resolve_path(
        source_path.parent,
        calibration_path_value,
    )

    from scripts.build_rpi_calibration_artifact import (
        _load_effective_deployment_config,
    )

    calibration_config = _load_effective_deployment_config(
        calibration_path
    )
    journal_path = _resolve_path(source_path.parent, journal_path_value)
    return journal_path, calibration_config, calibration_path


def validate_live_calibration_approval(
    config: Mapping[str, Any],
    *,
    config_path: str | Path,
    approved_formula_versions: set[str] | frozenset[str] | None = None,
    now_utc: datetime | None = None,
    signature_verifier: ApprovalSignatureVerifier | None = None,
) -> dict[str, Any]:
    """Validate the complete approval graph under the journal writer fence."""
    from scripts.build_rpi_calibration_artifact import (
        CalibrationArtifactError,
        _authorized_journal_fence,
    )

    try:
        fence_inputs = _approval_journal_fence_inputs(
            config,
            config_path=config_path,
        )
    except CalibrationArtifactError as exc:
        raise ValueError(
            f"GLFT calibration config validation failed: {exc}"
        ) from exc
    if fence_inputs is None:
        return _validate_live_calibration_approval_with_fence_held(
            config,
            config_path=config_path,
            approved_formula_versions=approved_formula_versions,
            now_utc=now_utc,
            signature_verifier=signature_verifier,
            expected_locked_journal_path=None,
        )

    journal_path, calibration_config, calibration_path = fence_inputs
    try:
        with _authorized_journal_fence(
            journal_path,
            calibration_config=calibration_config,
            calibration_config_path=calibration_path,
        ):
            return _validate_live_calibration_approval_with_fence_held(
                config,
                config_path=config_path,
                approved_formula_versions=approved_formula_versions,
                now_utc=now_utc,
                signature_verifier=signature_verifier,
                expected_locked_journal_path=journal_path,
            )
    except CalibrationArtifactError as exc:
        raise ValueError(
            f"GLFT source journal fence validation failed: {exc}"
        ) from exc


def formula_source_path_for_model(model: Any) -> Path:
    canonical_model_key(model)
    return Path(__file__).resolve().with_name("quote_math.py")


def implementation_source_paths_for_model(model: Any) -> tuple[Path, ...]:
    model_key = canonical_model_key(model)
    project_root = Path(__file__).resolve().parents[1]
    return tuple(
        project_root / relative_path
        for relative_path in _IMPLEMENTATION_SOURCE_FILES[model_key]
    )


def implementation_sha256_for_model(model: Any) -> str:
    """Hash every source file that can alter model units or live quotes."""
    model_key = canonical_model_key(model)
    project_root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for relative_path in _IMPLEMENTATION_SOURCE_FILES[model_key]:
        path = project_root / relative_path
        digest.update(relative_path.encode("ascii"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(path)))
        digest.update(b"\0")
    return digest.hexdigest()


def strategy_policy_sha256(
    config: Mapping[str, Any],
    model: Any,
) -> str:
    """Bind approval to the exact effective quoting and sampling policy."""
    model_key = canonical_model_key(model)
    root_strategy = config.get("strategy", {})
    if not isinstance(root_strategy, Mapping):
        raise ValueError("strategy must be an object")
    if root_strategy.get("registered_models"):
        effective = effective_primary_strategy_config(config)
    else:
        effective = dict(root_strategy)

    if model_key == "glft":
        model_config = effective.get("glft", {})
        if not isinstance(model_config, Mapping):
            model_config = {}
        root_use_rpi = effective.get("use_rpi", False)
        use_rpi_for_glft = effective.get("use_rpi_for_glft", True)
        model_use_rpi = model_config.get(
            "use_rpi",
            use_rpi_for_glft,
        )
        root_fallback = effective.get("rpi_fallback_to_gtx", True)
        model_fallback = model_config.get(
            "rpi_fallback_to_gtx",
            root_fallback,
        )
        policy = {
            "model": model_key,
            "symbols": list(_normalized_symbols(config.get("symbols"))),
            "units_version": IMPLEMENTED_UNITS_VERSION,
            "formula_version": formula_version_for_model(model_key),
            "use_rpi": root_use_rpi,
            "use_rpi_for_glft": use_rpi_for_glft,
            "glft_use_rpi": model_config.get("use_rpi"),
            "effective_use_rpi": bool(root_use_rpi)
            and bool(model_use_rpi),
            "rpi_fallback_to_gtx": root_fallback,
            "glft_rpi_fallback_to_gtx": model_config.get(
                "rpi_fallback_to_gtx"
            ),
            "effective_rpi_fallback_to_gtx": bool(model_fallback),
            "target_order_notional": effective.get(
                "target_order_notional"
            ),
            "max_pos_usdt": effective.get("max_pos_usdt"),
            "gamma": effective.get("gamma", model_config.get("gamma")),
            "cycle_interval": effective.get(
                "cycle_interval",
                model_config.get("cycle_interval"),
            ),
            "target_inventory_notional_usdt": effective.get(
                "target_inventory_notional_usdt",
                model_config.get("target_inventory_notional_usdt"),
            ),
            "inventory_lot_notional_usdt": effective.get(
                "inventory_lot_notional_usdt",
                model_config.get("inventory_lot_notional_usdt"),
            ),
            "alpha": effective.get("alpha", model_config.get("alpha")),
            "calibrator": effective.get(
                "calibrator",
                model_config.get("calibrator"),
            ),
            "rpi_intensity": effective.get(
                "rpi_intensity",
                model_config.get("rpi_intensity"),
            ),
            "execution": effective.get(
                "execution",
                model_config.get("execution"),
            ),
        }
    else:
        model_config = effective.get("as_parameters", {})
        if not isinstance(model_config, Mapping):
            model_config = {}
        policy = {
            "model": model_key,
            "symbols": list(_normalized_symbols(config.get("symbols"))),
            "units_version": IMPLEMENTED_UNITS_VERSION,
            "formula_version": formula_version_for_model(model_key),
            "use_rpi": effective.get("use_rpi"),
            "rpi_fallback_to_gtx": effective.get("rpi_fallback_to_gtx"),
            "target_order_notional": effective.get(
                "target_order_notional"
            ),
            "max_pos_usdt": effective.get("max_pos_usdt"),
            "as_parameters": dict(model_config),
        }

    try:
        encoded = json.dumps(
            policy,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError(
            "Effective strategy policy is not canonical JSON"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def deployment_config_projection(
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the deterministic, secret-free live deployment identity.

    The projection intentionally omits approval paths/content, runtime
    calibration, runtime commission truth, and API credentials. This makes
    the dependency graph acyclic: config -> artifact -> source evidence ->
    approval. All trading and safety settings which can change the canary's
    risk or execution behavior remain bound.
    """
    if not isinstance(config, Mapping):
        raise TypeError("deployment configuration must be a mapping")
    symbols = _normalized_symbols(config.get("symbols"))
    if not symbols:
        raise ValueError(
            "deployment configuration requires unique non-empty symbols"
        )
    raw_strategy = config.get("strategy")
    if not isinstance(raw_strategy, Mapping):
        raise ValueError("strategy must be an object")
    model = canonical_model_key(
        raw_strategy.get("primary_model", raw_strategy.get("name"))
    )

    strategy = _redacted_json_projection(
        raw_strategy,
        excluded_keys={
            "commission_truth",
            "maker_fee",
            "rpi_commission_rate",
            "rpi_commission_rates",
            "taker_fee",
        },
    )
    readiness = strategy.get("model_readiness")
    if isinstance(readiness, dict):
        live_approval = readiness.get("live_approval")
        if isinstance(live_approval, dict):
            # The manifest contains this config digest. Excluding its path
            # makes that relationship explicit and prevents a reference loop.
            live_approval.pop("manifest_path", None)

    system = _redacted_section(
        config,
        "system",
        excluded_keys={
            "api_key",
            "api_key_env",
            "api_secret",
            "api_secret_env",
        },
    )
    # Dashboard presentation cannot alter scheduling, risk, or order behavior.
    # Every other system field is bound, including event/runtime/watchdog knobs.
    system.pop("web_dashboard", None)

    projection = {
        "schema": DEPLOYMENT_CONFIG_PROJECTION_SCHEMA,
        "symbols": list(symbols),
        "execution": _redacted_section(config, "execution"),
        "paper_trade": _redacted_section(config, "paper_trade"),
        "testnet": config.get("testnet"),
        "record_data": config.get("record_data"),
        "live_launch": _redacted_section(config, "live_launch"),
        "system": system,
        "account": _redacted_section(config, "account"),
        "oms": _redacted_section(config, "oms"),
        "risk": _redacted_section(
            config,
            "risk",
            excluded_keys={
                "api_key",
                "api_key_env",
                "api_secret",
                "api_secret_env",
            },
        ),
        "strategy": {
            "primary_model": model,
            "strategy_policy_sha256": strategy_policy_sha256(config, model),
            "config": strategy,
        },
    }
    # Reject NaN, non-string object keys, and non-JSON runtime objects now,
    # rather than allowing different serializers to disagree later.
    _canonical_json_bytes(projection, "deployment configuration projection")
    return projection


def deployment_config_sha256(config: Mapping[str, Any]) -> str:
    projection = deployment_config_projection(config)
    return hashlib.sha256(
        _canonical_json_bytes(
            projection,
            "deployment configuration projection",
        )
    ).hexdigest()


def oos_evidence_sha256(oos: Mapping[str, Any]) -> str:
    if not isinstance(oos, Mapping):
        raise ValueError("OOS evidence must be an object")
    encoded = _canonical_json_bytes(oos, "OOS evidence")
    return hashlib.sha256(OOS_EVIDENCE_DOMAIN + encoded).hexdigest()


def calibration_evidence_bundle_sha256(
    *,
    artifact_sha256: str,
    source_data_sha256: str,
    oos_sha256: str,
    deployment_config_digest: str,
    strategy_policy_digest: str,
    implementation_digest: str,
) -> str:
    bundle = {
        "schema": CALIBRATION_EVIDENCE_BUNDLE_SCHEMA,
        "artifact_sha256": _sha256_value(
            artifact_sha256,
            "evidence bundle artifact_sha256",
        ),
        "source_data_sha256": _sha256_value(
            source_data_sha256,
            "evidence bundle source_data_sha256",
        ),
        "oos_evidence_sha256": _sha256_value(
            oos_sha256,
            "evidence bundle oos_evidence_sha256",
        ),
        "deployment_config_sha256": _sha256_value(
            deployment_config_digest,
            "evidence bundle deployment_config_sha256",
        ),
        "strategy_policy_sha256": _sha256_value(
            strategy_policy_digest,
            "evidence bundle strategy_policy_sha256",
        ),
        "implementation_sha256": _sha256_value(
            implementation_digest,
            "evidence bundle implementation_sha256",
        ),
    }
    return hashlib.sha256(
        _canonical_json_bytes(bundle, "calibration evidence bundle")
    ).hexdigest()


def approval_signature_payload(manifest: Mapping[str, Any]) -> bytes:
    """Return the domain-separated bytes an external Ed25519 signer signs."""
    if not isinstance(manifest, Mapping):
        raise ValueError("calibration approval manifest must be an object")
    unsigned = dict(manifest)
    unsigned.pop("signature", None)
    return APPROVAL_SIGNATURE_DOMAIN + _canonical_json_bytes(
        unsigned,
        "unsigned calibration approval manifest",
    )


def verify_ed25519_signature(
    public_key: bytes,
    message: bytes,
    signature: bytes,
) -> bool:
    """Verify with the mature cryptography backend or fail closed."""
    if (
        not isinstance(public_key, bytes)
        or not isinstance(message, bytes)
        or not isinstance(signature, bytes)
        or len(public_key) != 32
        or len(signature) != 64
    ):
        raise ValueError("Ed25519 verification inputs are invalid")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
    except Exception as exc:
        raise ValueError(
            "cryptography Ed25519 verification backend is unavailable"
        ) from exc
    try:
        verifier = Ed25519PublicKey.from_public_bytes(public_key)
    except Exception as exc:
        raise ValueError(
            "cryptography Ed25519 backend rejected the trusted public key"
        ) from exc
    try:
        verifier.verify(signature, message)
    except InvalidSignature:
        return False
    except Exception as exc:
        raise ValueError(
            "cryptography Ed25519 verification backend failed closed"
        ) from exc
    return True


def _validate_approval_signature(
    manifest: Mapping[str, Any],
    *,
    approval_config: Mapping[str, Any],
    verifier: ApprovalSignatureVerifier | None,
) -> None:
    raw_trusted_signers = approval_config.get("trusted_signers")
    if not isinstance(raw_trusted_signers, Mapping) or not raw_trusted_signers:
        raise ValueError(
            "Live approval requires provisioned trusted_signers public keys"
        )
    trusted_public_keys = {}
    for raw_key_id, raw_signer in raw_trusted_signers.items():
        key_id = str(raw_key_id or "").strip()
        if not _SIGNER_KEY_ID_RE.fullmatch(key_id):
            raise ValueError(
                "strategy.model_readiness.live_approval.trusted_signers "
                "contains an invalid key_id"
            )
        if not isinstance(raw_signer, Mapping) or set(raw_signer) != {
            "algorithm",
            "public_key_base64",
        }:
            raise ValueError(
                f"Trusted signer {key_id!r} metadata is invalid"
            )
        if raw_signer.get("algorithm") != APPROVAL_SIGNATURE_ALGORITHM:
            raise ValueError(
                f"Trusted signer {key_id!r} algorithm must be ED25519"
            )
        encoded_public_key = str(
            raw_signer.get("public_key_base64", "") or ""
        ).strip()
        try:
            public_key = base64.b64decode(
                encoded_public_key,
                validate=True,
            )
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                f"Trusted signer {key_id!r} public key is invalid"
            ) from exc
        if (
            len(public_key) != 32
            or base64.b64encode(public_key).decode("ascii")
            != encoded_public_key
        ):
            raise ValueError(
                f"Trusted signer {key_id!r} Ed25519 public key must be "
                "canonical 32-byte base64"
            )
        trusted_public_keys[key_id] = public_key

    signature = manifest.get("signature")
    if not isinstance(signature, Mapping) or set(signature) != {
        "algorithm",
        "key_id",
        "signer",
        "signed_payload_sha256",
        "signature_base64",
    }:
        raise ValueError(
            "Calibration approval signature contract is invalid"
        )
    algorithm = str(signature.get("algorithm", "") or "").strip()
    if algorithm != APPROVAL_SIGNATURE_ALGORITHM:
        raise ValueError(
            "Calibration approval signature algorithm must be ED25519"
        )
    key_id = str(signature.get("key_id", "") or "").strip()
    if not _SIGNER_KEY_ID_RE.fullmatch(key_id):
        raise ValueError("Calibration approval signature key_id is invalid")
    if key_id not in trusted_public_keys:
        raise ValueError(
            "Calibration approval signature key_id is not trusted by config"
        )
    signer = str(signature.get("signer", "") or "").strip()
    approved_by = str(manifest.get("approved_by", "") or "").strip()
    if not signer or signer != approved_by:
        raise ValueError(
            "Calibration approval signature signer must match approved_by"
        )

    payload = approval_signature_payload(manifest)
    declared_payload_sha256 = _sha256_value(
        signature.get("signed_payload_sha256"),
        "calibration approval signature signed_payload_sha256",
    )
    if not hmac.compare_digest(
        declared_payload_sha256,
        hashlib.sha256(payload).hexdigest(),
    ):
        raise ValueError(
            "Calibration approval signed-payload SHA-256 mismatch"
        )

    encoded_signature = str(
        signature.get("signature_base64", "") or ""
    ).strip()
    try:
        decoded_signature = base64.b64decode(
            encoded_signature,
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            "Calibration approval signature_base64 is invalid"
        ) from exc
    if (
        len(decoded_signature) != 64
        or base64.b64encode(decoded_signature).decode("ascii")
        != encoded_signature
    ):
        raise ValueError(
            "Calibration approval Ed25519 signature must be canonical 64-byte "
            "base64"
    )
    if verifier is None:
        verified = verify_ed25519_signature(
            trusted_public_keys[key_id],
            payload,
            decoded_signature,
        )
    else:
        try:
            verified = verifier(
                algorithm,
                key_id,
                payload,
                decoded_signature,
            )
        except Exception as exc:
            raise ValueError(
                "Trusted approval signature verifier failed closed"
            ) from exc
    if verified is not True:
        raise ValueError(
            "Calibration approval Ed25519 signature verification failed"
        )
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
        raise ValueError(f"{label} is not canonical JSON") from exc


def _redacted_section(
    config: Mapping[str, Any],
    key: str,
    *,
    excluded_keys: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    value = config.get(key, {})
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} must be an object")
    return _redacted_json_projection(value, excluded_keys=excluded_keys)


def _redacted_json_projection(
    value: Any,
    *,
    excluded_keys: set[str] | frozenset[str] = frozenset(),
) -> Any:
    if isinstance(value, Mapping):
        projected = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise ValueError("deployment configuration keys must be strings")
            if raw_key.startswith("_") or raw_key in excluded_keys:
                continue
            projected[raw_key] = _redacted_json_projection(
                item,
                excluded_keys=excluded_keys,
            )
        return projected
    if isinstance(value, (list, tuple)):
        return [
            _redacted_json_projection(item, excluded_keys=excluded_keys)
            for item in value
        ]
    return value


def sha256_file(path: str | Path) -> str:
    resolved = Path(path).resolve()
    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ValueError(f"Cannot read hash input {resolved}") from exc
    return digest.hexdigest()


def _merge_missing(target: dict, defaults: Mapping[str, Any]) -> None:
    for key, default in defaults.items():
        if key not in target:
            target[key] = _copy_json_value(default)
            continue
        if isinstance(target[key], dict) and isinstance(default, Mapping):
            _merge_missing(target[key], default)


def _copy_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _copy_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_json_value(item) for item in value]
    return value


def _strict_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a non-negative integer") from exc
    if parsed < 0 or parsed != value:
        raise ValueError(f"{field} must be a non-negative integer")
    return parsed


def _positive_int(value: Any, field: str) -> int:
    parsed = _nonnegative_int(value, field)
    if parsed <= 0:
        raise ValueError(f"{field} must be positive")
    return parsed


def _positive_finite(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be positive and finite") from exc
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise ValueError(f"{field} must be positive and finite")
    return parsed


def _sample_count(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _normalized_symbols(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        return ()
    normalized = tuple(str(symbol or "").strip().upper() for symbol in value)
    if any(not symbol for symbol in normalized) or len(set(normalized)) != len(
        normalized
    ):
        return ()
    return normalized


def _resolve_path(base: Path, value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON numeric constant {value!r}")


def _object_without_duplicate_keys(pairs) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _json_object_from_bytes(
    raw: bytes,
    *,
    path: Path,
    label: str,
) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8-sig"),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Cannot read {label} at {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _read_json_bytes(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f"Cannot read {label} at {path}") from exc


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    raw = _read_json_bytes(path, label)
    return _json_object_from_bytes(raw, path=path, label=label)


def _read_hashed_json_object(
    path: Path,
    label: str,
    *,
    expected_sha256: str,
    mismatch_message: str,
) -> dict[str, Any]:
    raw = _read_json_bytes(path, label)
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise ValueError(mismatch_message)
    return _json_object_from_bytes(raw, path=path, label=label)


def _parse_utc_timestamp(value: Any, field: str) -> datetime:
    normalized = str(value or "").strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _configured_deployment_loss_cap(config: Mapping[str, Any]) -> float:
    live_launch = config.get("live_launch", {})
    if not isinstance(live_launch, Mapping):
        raise ValueError("live_launch must be an object")
    return _positive_finite(
        live_launch.get("max_deployment_loss_usdt"),
        "live_launch.max_deployment_loss_usdt",
    )


def _configured_deployment_id(config: Mapping[str, Any]) -> str:
    live_launch = config.get("live_launch", {})
    if not isinstance(live_launch, Mapping):
        raise ValueError("live_launch must be an object")
    deployment_id = str(live_launch.get("deployment_id", "") or "").strip()
    if not deployment_id:
        raise ValueError("live_launch.deployment_id is required")
    return deployment_id


def _nonnegative_finite(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be non-negative and finite") from exc
    if not math.isfinite(parsed) or parsed < 0.0:
        raise ValueError(f"{field} must be non-negative and finite")
    return parsed


def _decimal_rate(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not parsed.is_finite() or abs(parsed) > Decimal("0.01"):
        raise ValueError(f"{field} must be a finite rate within +/-0.01")
    return parsed


def _finite_decimal(
    value: Any,
    field: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    if positive and parsed <= 0:
        raise ValueError(f"{field} must be positive")
    if nonnegative and parsed < 0:
        raise ValueError(f"{field} must be non-negative")
    return parsed


def _require_exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    field: str,
) -> None:
    if set(value) != expected:
        raise ValueError(f"{field} keys are invalid")


def _validate_raw_oos_evidence(
    value: Any,
    *,
    deployment_id: str,
    deployment_config_sha256: str,
    oos_sample_count: int,
    oos_fill_count: int,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(
            "GLFT source evidence oos.raw_evidence must be an object"
        )
    _require_exact_keys(
        value,
        {
            "schema",
            "deployment_id",
            "deployment_config_sha256",
            "oms_journal",
            "market_evidence_journal",
            "reconstruction",
        },
        "GLFT source evidence oos.raw_evidence",
    )
    if value.get("schema") != RAW_OOS_EVIDENCE_SCHEMA:
        raise ValueError(
            "GLFT source evidence raw OOS schema must be "
            f"{RAW_OOS_EVIDENCE_SCHEMA!r}"
        )
    if value.get("deployment_id") != deployment_id:
        raise ValueError(
            "GLFT source evidence raw OOS deployment_id mismatch"
        )
    if (
        _sha256_value(
            value.get("deployment_config_sha256"),
            "GLFT source evidence raw OOS deployment_config_sha256",
        )
        != deployment_config_sha256
    ):
        raise ValueError(
            "GLFT source evidence raw OOS deployment config digest mismatch"
        )

    oms_journal = value.get("oms_journal")
    if not isinstance(oms_journal, Mapping):
        raise ValueError(
            "GLFT source evidence raw OOS oms_journal must be an object"
        )
    _require_exact_keys(
        oms_journal,
        {
            "sha256",
            "record_count",
            "first_seq",
            "last_seq",
            "final_hash",
            "last_kind",
        },
        "GLFT source evidence raw OOS oms_journal",
    )
    _sha256_value(
        oms_journal.get("sha256"),
        "GLFT source evidence raw OOS OMS journal sha256",
    )
    _sha256_value(
        oms_journal.get("final_hash"),
        "GLFT source evidence raw OOS OMS journal final_hash",
    )
    oms_record_count = _positive_int(
        oms_journal.get("record_count"),
        "GLFT source evidence raw OOS OMS journal record_count",
    )
    oms_first_seq = _positive_int(
        oms_journal.get("first_seq"),
        "GLFT source evidence raw OOS OMS journal first_seq",
    )
    oms_last_seq = _positive_int(
        oms_journal.get("last_seq"),
        "GLFT source evidence raw OOS OMS journal last_seq",
    )
    if (
        oms_first_seq != 1
        or oms_last_seq != oms_record_count
        or oms_journal.get("last_kind") != "oms_stopped"
    ):
        raise ValueError(
            "GLFT source evidence raw OOS OMS journal must be contiguous "
            "from sequence 1 and end with oms_stopped"
        )
    if oms_record_count < oos_fill_count:
        raise ValueError(
            "GLFT source evidence raw OOS OMS record count is below fills"
        )

    market_journal = value.get("market_evidence_journal")
    if not isinstance(market_journal, Mapping):
        raise ValueError(
            "GLFT source evidence raw OOS market_evidence_journal must be "
            "an object"
        )
    _require_exact_keys(
        market_journal,
        {
            "sha256",
            "record_count",
            "final_hash",
            "mark_price_count",
            "account_update_count",
            "last_kind",
        },
        "GLFT source evidence raw OOS market_evidence_journal",
    )
    _sha256_value(
        market_journal.get("sha256"),
        "GLFT source evidence raw OOS market journal sha256",
    )
    _sha256_value(
        market_journal.get("final_hash"),
        "GLFT source evidence raw OOS market journal final_hash",
    )
    market_record_count = _positive_int(
        market_journal.get("record_count"),
        "GLFT source evidence raw OOS market journal record_count",
    )
    mark_price_count = _positive_int(
        market_journal.get("mark_price_count"),
        "GLFT source evidence raw OOS market journal mark_price_count",
    )
    account_update_count = _positive_int(
        market_journal.get("account_update_count"),
        "GLFT source evidence raw OOS market journal account_update_count",
    )
    if market_journal.get("last_kind") != "clean_stop":
        raise ValueError(
            "GLFT source evidence raw OOS market journal must end with "
            "clean_stop"
        )
    if (
        market_record_count
        < mark_price_count + account_update_count + 2
        or mark_price_count < oos_sample_count
    ):
        raise ValueError(
            "GLFT source evidence raw OOS market journal counts are "
            "inconsistent"
        )

    reconstruction = value.get("reconstruction")
    if not isinstance(reconstruction, Mapping):
        raise ValueError(
            "GLFT source evidence raw OOS reconstruction must be an object"
        )
    _require_exact_keys(
        reconstruction,
        {
            "schema",
            "flat_tolerance",
            "pnl_crosscheck_tolerance_usdt",
            "max_markout_lag_ms",
            "min_utc_day_clusters",
        },
        "GLFT source evidence raw OOS reconstruction",
    )
    if reconstruction.get("schema") != OOS_RECONSTRUCTION_SCHEMA:
        raise ValueError(
            "GLFT source evidence OOS reconstruction schema mismatch"
        )
    _finite_decimal(
        reconstruction.get("flat_tolerance"),
        "GLFT source evidence OOS reconstruction flat_tolerance",
        positive=True,
    )
    pnl_tolerance = _finite_decimal(
        reconstruction.get("pnl_crosscheck_tolerance_usdt"),
        "GLFT source evidence OOS PnL cross-check tolerance",
        positive=True,
    )
    if pnl_tolerance > LIVE_MAX_PNL_CROSSCHECK_TOLERANCE_USDT:
        raise ValueError(
            "GLFT source evidence OOS PnL cross-check tolerance exceeds "
            "the Live maximum"
        )
    max_markout_lag_ms = _positive_int(
        reconstruction.get("max_markout_lag_ms"),
        "GLFT source evidence OOS max_markout_lag_ms",
    )
    if max_markout_lag_ms > LIVE_MAX_MARKOUT_LAG_MS:
        raise ValueError(
            "GLFT source evidence OOS max_markout_lag_ms exceeds the Live "
            "maximum"
        )
    min_utc_day_clusters = _positive_int(
        reconstruction.get("min_utc_day_clusters"),
        "GLFT source evidence OOS min_utc_day_clusters",
    )
    if min_utc_day_clusters < LIVE_MIN_OOS_UTC_DAY_CLUSTERS:
        raise ValueError(
            "GLFT source evidence OOS UTC-day cluster requirement is below "
            "the Live minimum"
        )
    return {
        "pnl_tolerance": pnl_tolerance,
        "max_markout_lag_ms": max_markout_lag_ms,
        "min_utc_day_clusters": min_utc_day_clusters,
    }


def _validate_glft_source_evidence(
    path: Path,
    *,
    configured_symbols: tuple[str, ...],
    strategy_config_sha256: str,
    target_deployment_config_sha256: str,
    implementation_sha256: str,
    deployment_id: str,
    calibration_artifact_sha256: str,
    formula_version: str,
    min_data_duration_sec: float,
    min_oos_samples: int,
    max_deployment_loss_usdt: float,
    target_deployment_config: Mapping[str, Any],
    target_deployment_config_path: Path,
    expected_locked_journal_path: Path | None,
    evidence_document: Mapping[str, Any],
) -> dict[str, Any]:
    if not configured_symbols:
        raise ValueError("GLFT source evidence requires configured symbols")
    if not deployment_id:
        raise ValueError("GLFT source evidence requires a deployment_id")
    evidence = evidence_document
    expected_evidence_keys = {
        "schema",
        "model",
        "venue",
        "data_source",
        "units_version",
        "validated_formula_version",
        "strategy_config_sha256",
        "deployment_config_sha256",
        "implementation_sha256",
        "deployment_id",
        "calibration_artifact_sha256",
        "calibration_config_path",
        "calibration_config_sha256",
        "symbols",
        "capture_started_at_utc",
        "capture_ended_at_utc",
        "order_sample_count",
        "unique_order_count",
        "journal",
        "oos_evidence_sha256",
        "oos",
    }
    evidence_keys = set(evidence)
    evidence_keys.discard("_warning")
    if evidence_keys != expected_evidence_keys:
        raise ValueError("GLFT source evidence keys are invalid")
    if evidence.get("schema") != GLFT_CALIBRATION_SOURCE_EVIDENCE_SCHEMA:
        raise ValueError(
            "GLFT source evidence schema must be "
            f"{GLFT_CALIBRATION_SOURCE_EVIDENCE_SCHEMA!r}"
        )
    expected_fields = {
        "model": "glft",
        "venue": GLFT_CALIBRATION_VENUE,
        "data_source": GLFT_CALIBRATION_DATA_SOURCE,
        "units_version": IMPLEMENTED_UNITS_VERSION,
        "validated_formula_version": formula_version,
        "strategy_config_sha256": strategy_config_sha256,
        "deployment_config_sha256": target_deployment_config_sha256,
        "implementation_sha256": implementation_sha256,
        "deployment_id": deployment_id,
        "calibration_artifact_sha256": calibration_artifact_sha256,
    }
    for field, expected in expected_fields.items():
        if evidence.get(field) != expected:
            raise ValueError(
                f"GLFT source evidence {field} must equal {expected!r}"
            )
    if _normalized_symbols(evidence.get("symbols")) != configured_symbols:
        raise ValueError(
            "GLFT source evidence symbols must exactly match configured symbols"
        )

    calibration_config_path_value = str(
        evidence.get("calibration_config_path", "") or ""
    ).strip()
    if not calibration_config_path_value:
        raise ValueError(
            "GLFT source evidence calibration_config_path is required"
        )
    calibration_config_path = _resolve_path(
        path.parent,
        calibration_config_path_value,
    )
    target_deployment_config_path = (
        target_deployment_config_path.resolve()
    )
    if calibration_config_path in {
        path.resolve(),
        target_deployment_config_path,
    }:
        raise ValueError(
            "GLFT calibration, target, and source-evidence files must be "
            "distinct"
        )
    calibration_config_sha256 = _sha256_value(
        evidence.get("calibration_config_sha256"),
        "GLFT source evidence calibration_config_sha256",
    )
    from scripts.build_rpi_calibration_artifact import (
        CalibrationArtifactError,
        _load_effective_deployment_config,
        _validate_rpi_calibration_journal_unlocked,
    )

    try:
        calibration_config = _load_effective_deployment_config(
            calibration_config_path
        )
    except CalibrationArtifactError as exc:
        raise ValueError(
            f"GLFT calibration config validation failed: {exc}"
        ) from exc
    if target_deployment_config_sha256 != deployment_config_sha256(
        target_deployment_config
    ):
        raise ValueError(
            "GLFT target deployment config changed during source validation"
        )
    if (
        deployment_config_sha256(calibration_config)
        != calibration_config_sha256
    ):
        raise ValueError(
            "GLFT source evidence calibration config digest mismatch"
        )
    calibration_launch = calibration_config.get("live_launch")
    if not isinstance(calibration_launch, Mapping):
        raise ValueError(
            "GLFT calibration config live_launch must be an object"
        )
    target_path_value = str(
        calibration_launch.get("target_deployment_config_path", "") or ""
    ).strip()
    if (
        not target_path_value
        or _resolve_path(calibration_config_path.parent, target_path_value)
        != target_deployment_config_path
    ):
        raise ValueError(
            "GLFT calibration config must reference the exact target "
            "deployment config"
        )

    capture_started = _parse_utc_timestamp(
        evidence.get("capture_started_at_utc"),
        "GLFT source evidence capture_started_at_utc",
    )
    capture_ended = _parse_utc_timestamp(
        evidence.get("capture_ended_at_utc"),
        "GLFT source evidence capture_ended_at_utc",
    )
    order_sample_count = _positive_int(
        evidence.get("order_sample_count"),
        "GLFT source evidence order_sample_count",
    )
    if order_sample_count < LIVE_MIN_MODEL_SAMPLES:
        raise ValueError(
            "GLFT source evidence order_sample_count is below the live minimum"
        )
    unique_order_count = _positive_int(
        evidence.get("unique_order_count"),
        "GLFT source evidence unique_order_count",
    )
    if unique_order_count != order_sample_count:
        raise ValueError(
            "GLFT source evidence unique_order_count must equal "
            "order_sample_count"
        )

    journal = evidence.get("journal")
    if not isinstance(journal, Mapping):
        raise ValueError("GLFT source evidence journal must be an object")
    expected_journal_keys = {
        "path",
        "sha256",
        "deployment_id",
        "first_seq",
        "last_seq",
        "final_hash",
        "record_count",
        "first_record_at_utc",
        "last_record_at_utc",
        "first_sample_at_utc",
        "last_sample_at_utc",
        "first_ack_at_utc",
        "last_terminal_at_utc",
        "sample_count",
        "unique_order_count",
        "censored_sample_count",
        "strategy_policy_sha256",
        "implementation_sha256",
        "calibration_config_sha256",
        "target_deployment_config_sha256",
        "permit_activation_count",
        "permit_sha256s",
        "reservation_count",
        "cumulative_submitted_notional_microu",
    }
    if set(journal) != expected_journal_keys:
        raise ValueError("GLFT source evidence journal keys are invalid")
    if journal.get("deployment_id") != deployment_id:
        raise ValueError(
            "GLFT source evidence journal deployment_id mismatch"
        )
    journal_path_value = str(journal.get("path", "") or "").strip()
    if not journal_path_value:
        raise ValueError("GLFT source evidence journal.path is required")
    journal_path = _resolve_path(path.parent, journal_path_value)
    if journal_path == path.resolve():
        raise ValueError(
            "GLFT source evidence journal must be distinct from its manifest"
        )
    if (
        expected_locked_journal_path is not None
        and journal_path != expected_locked_journal_path.resolve()
    ):
        raise ValueError(
            "GLFT source evidence journal changed after its writer fence "
            "was selected"
        )
    journal_sha256 = _sha256_value(
        journal.get("sha256"),
        "GLFT source evidence journal.sha256",
    )
    declared_first_seq = _positive_int(
        journal.get("first_seq"),
        "GLFT source evidence journal.first_seq",
    )
    if declared_first_seq != 1:
        raise ValueError("GLFT source evidence journal.first_seq must be 1")
    declared_last_seq = _positive_int(
        journal.get("last_seq"),
        "GLFT source evidence journal.last_seq",
    )
    if declared_last_seq < order_sample_count:
        raise ValueError(
            "GLFT source evidence journal.last_seq is below order_sample_count"
        )
    declared_final_hash = _sha256_value(
        journal.get("final_hash"),
        "GLFT source evidence journal.final_hash",
    )
    declared_sample_count = _positive_int(
        journal.get("sample_count"),
        "GLFT source evidence journal.sample_count",
    )
    declared_unique_order_count = _positive_int(
        journal.get("unique_order_count"),
        "GLFT source evidence journal.unique_order_count",
    )
    declared_record_count = _positive_int(
        journal.get("record_count"),
        "GLFT source evidence journal.record_count",
    )
    declared_censored_sample_count = _nonnegative_int(
        journal.get("censored_sample_count"),
        "GLFT source evidence journal.censored_sample_count",
    )
    declared_permit_activation_count = _positive_int(
        journal.get("permit_activation_count"),
        "GLFT source evidence journal.permit_activation_count",
    )
    raw_permit_sha256s = journal.get("permit_sha256s")
    if (
        not isinstance(raw_permit_sha256s, list)
        or len(raw_permit_sha256s) != declared_permit_activation_count
    ):
        raise ValueError(
            "GLFT source evidence journal.permit_sha256s must match its "
            "activation count"
        )
    declared_permit_sha256s = tuple(
        _sha256_value(
            value,
            "GLFT source evidence journal.permit_sha256s",
        )
        for value in raw_permit_sha256s
    )
    if len(set(declared_permit_sha256s)) != len(declared_permit_sha256s):
        raise ValueError(
            "GLFT source evidence journal.permit_sha256s must be unique"
        )
    declared_reservation_count = _positive_int(
        journal.get("reservation_count"),
        "GLFT source evidence journal.reservation_count",
    )
    declared_cumulative_submitted_notional_microu = _positive_int(
        journal.get("cumulative_submitted_notional_microu"),
        "GLFT source evidence "
        "journal.cumulative_submitted_notional_microu",
    )
    if declared_reservation_count < (
        declared_sample_count + declared_censored_sample_count
    ):
        raise ValueError(
            "GLFT source evidence reservation count is below its exposure "
            "sample count"
        )
    declared_first_record_at = _parse_utc_timestamp(
        journal.get("first_record_at_utc"),
        "GLFT source evidence journal.first_record_at_utc",
    )
    declared_last_record_at = _parse_utc_timestamp(
        journal.get("last_record_at_utc"),
        "GLFT source evidence journal.last_record_at_utc",
    )
    declared_first_sample_at = _parse_utc_timestamp(
        journal.get("first_sample_at_utc"),
        "GLFT source evidence journal.first_sample_at_utc",
    )
    declared_last_sample_at = _parse_utc_timestamp(
        journal.get("last_sample_at_utc"),
        "GLFT source evidence journal.last_sample_at_utc",
    )
    declared_first_ack_at = _parse_utc_timestamp(
        journal.get("first_ack_at_utc"),
        "GLFT source evidence journal.first_ack_at_utc",
    )
    declared_last_terminal_at = _parse_utc_timestamp(
        journal.get("last_terminal_at_utc"),
        "GLFT source evidence journal.last_terminal_at_utc",
    )
    if (
        _sha256_value(
            journal.get("strategy_policy_sha256"),
            "GLFT source evidence journal.strategy_policy_sha256",
        )
        != strategy_config_sha256
    ):
        raise ValueError(
            "GLFT source evidence journal strategy policy hash mismatch"
        )
    if (
        _sha256_value(
            journal.get("implementation_sha256"),
            "GLFT source evidence journal.implementation_sha256",
        )
        != implementation_sha256
    ):
        raise ValueError(
            "GLFT source evidence journal implementation hash mismatch"
        )
    if (
        _sha256_value(
            journal.get("calibration_config_sha256"),
            "GLFT source evidence journal.calibration_config_sha256",
        )
        != calibration_config_sha256
        or _sha256_value(
            journal.get("target_deployment_config_sha256"),
            "GLFT source evidence "
            "journal.target_deployment_config_sha256",
        )
        != target_deployment_config_sha256
    ):
        raise ValueError(
            "GLFT source evidence journal deployment config hash mismatch"
        )

    journal_summaries = []
    try:
        # The public approval validator holds the configured writer fence
        # across this replay and every downstream artifact/signature check.
        for symbol in configured_symbols:
            journal_summaries.append(
                _validate_rpi_calibration_journal_unlocked(
                    journal_path,
                    symbol=symbol,
                    calibration_config=calibration_config,
                    target_deployment_config=target_deployment_config,
                    calibration_config_path=calibration_config_path,
                    target_deployment_config_path=(
                        target_deployment_config_path
                    ),
                )
            )
    except CalibrationArtifactError as exc:
        raise ValueError(
            f"GLFT source journal validation failed: {exc}"
        ) from exc

    first_summary = journal_summaries[0]
    first_record_at = _parse_utc_timestamp(
        first_summary.first_record_at_utc,
        "validated GLFT journal first_record_at_utc",
    )
    last_record_at = _parse_utc_timestamp(
        first_summary.last_record_at_utc,
        "validated GLFT journal last_record_at_utc",
    )
    first_sample_at = min(
        _parse_utc_timestamp(
            summary.first_sample_at_utc,
            "validated GLFT journal first_sample_at_utc",
        )
        for summary in journal_summaries
    )
    last_sample_at = max(
        _parse_utc_timestamp(
            summary.last_sample_at_utc,
            "validated GLFT journal last_sample_at_utc",
        )
        for summary in journal_summaries
    )
    first_ack_at = min(
        _parse_utc_timestamp(
            summary.first_ack_at_utc,
            "validated GLFT journal first_ack_at_utc",
        )
        for summary in journal_summaries
    )
    last_terminal_at = max(
        _parse_utc_timestamp(
            summary.last_terminal_at_utc,
            "validated GLFT journal last_terminal_at_utc",
        )
        for summary in journal_summaries
    )
    for summary in journal_summaries:
        if (
            summary.journal_sha256 != journal_sha256
            or summary.first_seq != declared_first_seq
            or summary.last_seq != declared_last_seq
            or summary.final_hash != declared_final_hash
            or summary.record_count != declared_record_count
        ):
            raise ValueError(
                "GLFT source evidence journal metadata does not match "
                "validated journal"
            )
        if summary.deployment_id != deployment_id:
            raise ValueError(
                "GLFT source journal deployment_id mismatch"
            )
        if summary.strategy_policy_sha256 != strategy_config_sha256:
            raise ValueError(
                "GLFT source journal strategy policy hash mismatch"
            )
        if summary.implementation_sha256 != implementation_sha256:
            raise ValueError(
                "GLFT source journal implementation hash mismatch"
            )
        if (
            summary.calibration_config_sha256
            != calibration_config_sha256
            or summary.target_deployment_config_sha256
            != target_deployment_config_sha256
            or summary.permit_activation_count
            != declared_permit_activation_count
            or summary.permit_sha256s != declared_permit_sha256s
            or summary.reservation_count != declared_reservation_count
            or summary.cumulative_submitted_notional_microu
            != declared_cumulative_submitted_notional_microu
        ):
            raise ValueError(
                "GLFT source journal permit or reservation evidence mismatch"
            )
        if (
            _parse_utc_timestamp(
                summary.first_record_at_utc,
                "validated GLFT journal first_record_at_utc",
            )
            != first_record_at
            or _parse_utc_timestamp(
                summary.last_record_at_utc,
                "validated GLFT journal last_record_at_utc",
            )
            != last_record_at
        ):
            raise ValueError(
                "GLFT source journal record boundaries changed during "
                "validation"
            )
    if (
        declared_first_record_at != first_record_at
        or declared_last_record_at != last_record_at
        or declared_first_sample_at != first_sample_at
        or declared_last_sample_at != last_sample_at
        or declared_first_ack_at != first_ack_at
        or declared_last_terminal_at != last_terminal_at
    ):
        raise ValueError(
            "GLFT source evidence journal timestamps do not match the "
            "validated journal"
        )
    if capture_started != first_ack_at or capture_ended != last_terminal_at:
        raise ValueError(
            "GLFT source evidence capture boundaries must exactly match "
            "the validated uncensored ACK-to-terminal span"
        )
    if not (
        first_record_at <= first_sample_at <= last_sample_at <= last_record_at
    ):
        raise ValueError(
            "GLFT source journal sample timestamps fall outside its record "
            "boundaries"
        )
    data_duration_sec = (last_terminal_at - first_ack_at).total_seconds()
    if data_duration_sec < min_data_duration_sec:
        raise ValueError(
            "GLFT source journal duration is below the live minimum"
        )
    validated_sample_count = sum(
        summary.sample_count for summary in journal_summaries
    )
    validated_unique_order_count = sum(
        summary.unique_order_count for summary in journal_summaries
    )
    validated_censored_sample_count = sum(
        summary.censored_sample_count for summary in journal_summaries
    )
    if (
        validated_sample_count != declared_sample_count
        or validated_sample_count != order_sample_count
    ):
        raise ValueError(
            "GLFT source evidence sample count does not match validated "
            "journal"
        )
    if (
        validated_unique_order_count != declared_unique_order_count
        or validated_unique_order_count != unique_order_count
    ):
        raise ValueError(
            "GLFT source evidence unique order count does not match "
            "validated journal"
        )
    if validated_censored_sample_count != declared_censored_sample_count:
        raise ValueError(
            "GLFT source evidence censored sample count does not match "
            "validated journal"
        )

    oos = evidence.get("oos")
    if not isinstance(oos, Mapping):
        raise ValueError("GLFT source evidence oos must be an object")
    _require_exact_keys(
        oos,
        {
            "method",
            "training_ended_at_utc",
            "started_at_utc",
            "ended_at_utc",
            "sample_count",
            "fill_count",
            "maker_fill_fraction",
            "rpi_fill_fraction",
            "rpi_commission_rate",
            "total_commission_usdt",
            "total_booked_fee_usdt",
            "funding_pnl_usdt",
            "net_pnl_usdt",
            "exchange_net_pnl_usdt",
            "max_drawdown_usdt",
            "markout",
            "raw_evidence",
        },
        "GLFT source evidence oos",
    )
    declared_oos_sha256 = _sha256_value(
        evidence.get("oos_evidence_sha256"),
        "GLFT source evidence oos_evidence_sha256",
    )
    calculated_oos_sha256 = oos_evidence_sha256(oos)
    if declared_oos_sha256 != calculated_oos_sha256:
        raise ValueError("GLFT source evidence OOS SHA-256 mismatch")
    if oos.get("method") != "WALK_FORWARD":
        raise ValueError(
            "GLFT source evidence oos.method must be 'WALK_FORWARD'"
        )
    training_ended = _parse_utc_timestamp(
        oos.get("training_ended_at_utc"),
        "GLFT source evidence oos.training_ended_at_utc",
    )
    oos_started = _parse_utc_timestamp(
        oos.get("started_at_utc"),
        "GLFT source evidence oos.started_at_utc",
    )
    oos_ended = _parse_utc_timestamp(
        oos.get("ended_at_utc"),
        "GLFT source evidence oos.ended_at_utc",
    )
    if not (
        capture_started < training_ended <= oos_started < oos_ended
        <= capture_ended
    ):
        raise ValueError(
            "GLFT source evidence requires non-overlapping chronological "
            "training and OOS windows"
        )

    oos_samples = _positive_int(
        oos.get("sample_count"),
        "GLFT source evidence oos.sample_count",
    )
    if oos_samples < min_oos_samples:
        raise ValueError(
            "GLFT source evidence OOS sample count is below the live minimum"
        )
    oos_fill_count = _positive_int(
        oos.get("fill_count"),
        "GLFT source evidence oos.fill_count",
    )
    if oos_fill_count < LIVE_MIN_OOS_FILLS:
        raise ValueError(
            "GLFT source evidence OOS fill count is below the live minimum"
        )
    raw_oos_requirements = _validate_raw_oos_evidence(
        oos.get("raw_evidence"),
        deployment_id=deployment_id,
        deployment_config_sha256=target_deployment_config_sha256,
        oos_sample_count=oos_samples,
        oos_fill_count=oos_fill_count,
    )
    maker_fill_fraction = _finite_decimal(
        oos.get("maker_fill_fraction"),
        "GLFT source evidence oos.maker_fill_fraction",
        positive=True,
    )
    if maker_fill_fraction != Decimal(1):
        raise ValueError(
            "GLFT source evidence oos.maker_fill_fraction must equal 1"
        )
    rpi_fill_fraction = _finite_decimal(
        oos.get("rpi_fill_fraction"),
        "GLFT source evidence oos.rpi_fill_fraction",
        positive=True,
    )
    if rpi_fill_fraction != Decimal(1):
        raise ValueError(
            "GLFT source evidence oos.rpi_fill_fraction must equal 1"
        )
    if _decimal_rate(
        oos.get("rpi_commission_rate"),
        "GLFT source evidence oos.rpi_commission_rate",
    ) != Decimal(0):
        raise ValueError(
            "GLFT source evidence OOS RPI commission must be exactly zero"
        )
    total_commission = _finite_decimal(
        oos.get("total_commission_usdt"),
        "GLFT source evidence oos.total_commission_usdt",
        nonnegative=True,
    )
    total_booked_fee = _finite_decimal(
        oos.get("total_booked_fee_usdt"),
        "GLFT source evidence oos.total_booked_fee_usdt",
        nonnegative=True,
    )
    if total_commission != 0 or total_booked_fee != 0:
        raise ValueError(
            "GLFT source evidence OOS RPI commission and booked fee must "
            "both be exactly zero"
        )
    _finite_decimal(
        oos.get("funding_pnl_usdt"),
        "GLFT source evidence oos.funding_pnl_usdt",
    )
    reconstructed_net_pnl = _finite_decimal(
        oos.get("net_pnl_usdt"),
        "GLFT source evidence oos.net_pnl_usdt",
        positive=True,
    )
    exchange_net_pnl = _finite_decimal(
        oos.get("exchange_net_pnl_usdt"),
        "GLFT source evidence oos.exchange_net_pnl_usdt",
    )
    if reconstructed_net_pnl <= 0:
        raise ValueError("GLFT source evidence OOS net PnL must be positive")
    if (
        abs(reconstructed_net_pnl - exchange_net_pnl)
        > raw_oos_requirements["pnl_tolerance"]
    ):
        raise ValueError(
            "GLFT source evidence exchange and reconstructed OOS PnL do "
            "not match"
        )
    max_drawdown = _nonnegative_finite(
        oos.get("max_drawdown_usdt"),
        "GLFT source evidence oos.max_drawdown_usdt",
    )
    if max_drawdown > max_deployment_loss_usdt:
        raise ValueError(
            "GLFT source evidence OOS drawdown exceeds the deployment loss cap"
        )

    markout = oos.get("markout")
    if not isinstance(markout, Mapping):
        raise ValueError("GLFT source evidence oos.markout must be an object")
    for horizon_ms in LIVE_REQUIRED_MARKOUT_HORIZONS_MS:
        metrics = markout.get(str(horizon_ms))
        if not isinstance(metrics, Mapping):
            raise ValueError(
                "GLFT source evidence is missing required markout horizon "
                f"{horizon_ms}ms"
            )
        _require_exact_keys(
            metrics,
            {
                "sample_count",
                "mean_net_edge_bps",
                "net_edge_bps_lcb95",
                "cluster_count",
                "cluster_unit",
                "estimator",
                "max_mark_lag_ms",
            },
            f"GLFT source evidence markout {horizon_ms}ms",
        )
        metric_samples = _positive_int(
            metrics.get("sample_count"),
            "GLFT source evidence markout sample_count",
        )
        if metric_samples != oos_fill_count:
            raise ValueError(
                "GLFT source evidence markout sample count must equal OOS "
                "fills"
            )
        _finite_decimal(
            metrics.get("mean_net_edge_bps"),
            "GLFT source evidence markout mean_net_edge_bps",
        )
        if _finite_decimal(
            metrics.get("net_edge_bps_lcb95"),
            "GLFT source evidence markout net_edge_bps_lcb95",
            positive=True,
        ) <= 0:
            raise ValueError(
                "GLFT source evidence markout net edge lower bound must be "
                "positive"
            )
        cluster_count = _positive_int(
            metrics.get("cluster_count"),
            "GLFT source evidence markout cluster_count",
        )
        if (
            cluster_count
            < raw_oos_requirements["min_utc_day_clusters"]
            or metrics.get("cluster_unit") != "UTC_DAY"
            or metrics.get("estimator")
            != "T_DISTRIBUTION_CLUSTER_MEAN"
        ):
            raise ValueError(
                "GLFT source evidence markout must use the required "
                "UTC-day clustered t estimator"
            )
        mark_lag = _positive_int(
            metrics.get("max_mark_lag_ms"),
            "GLFT source evidence markout max_mark_lag_ms",
        )
        if mark_lag != raw_oos_requirements["max_markout_lag_ms"]:
            raise ValueError(
                "GLFT source evidence markout lag does not match raw "
                "reconstruction policy"
            )

    return {
        "capture_started_at_utc": capture_started,
        "capture_ended_at_utc": capture_ended,
        "data_duration_sec": data_duration_sec,
        "order_sample_count": order_sample_count,
        "unique_order_count": unique_order_count,
        "oos_samples": oos_samples,
        "oos_fill_count": oos_fill_count,
        "oos_rpi_fill_fraction": str(rpi_fill_fraction),
        "oos_total_commission_usdt": str(total_commission),
        "oos_total_booked_fee_usdt": str(total_booked_fee),
        "oos_reconstructed_net_pnl_usdt": str(
            reconstructed_net_pnl
        ),
        "oos_exchange_net_pnl_usdt": str(exchange_net_pnl),
        "raw_oms_journal_sha256": oos["raw_evidence"][
            "oms_journal"
        ]["sha256"],
        "raw_market_evidence_sha256": oos["raw_evidence"][
            "market_evidence_journal"
        ]["sha256"],
        "journal_path": str(journal_path),
        "journal_sha256": journal_sha256,
        "journal_first_seq": first_summary.first_seq,
        "journal_last_seq": first_summary.last_seq,
        "journal_final_hash": first_summary.final_hash,
        "journal_record_count": declared_record_count,
        "journal_sample_count": validated_sample_count,
        "journal_unique_order_count": validated_unique_order_count,
        "journal_censored_sample_count": validated_censored_sample_count,
        "journal_permit_activation_count": (
            declared_permit_activation_count
        ),
        "journal_permit_sha256s": declared_permit_sha256s,
        "journal_reservation_count": declared_reservation_count,
        "journal_cumulative_submitted_notional_microu": (
            declared_cumulative_submitted_notional_microu
        ),
        "journal_first_record_at_utc": first_record_at,
        "journal_last_record_at_utc": last_record_at,
        "journal_first_sample_at_utc": first_sample_at,
        "journal_last_sample_at_utc": last_sample_at,
        "journal_first_ack_at_utc": first_ack_at,
        "journal_last_terminal_at_utc": last_terminal_at,
        "journal_bins_by_symbol": {
            symbol: summary.exposure_bins
            for symbol, summary in zip(
                configured_symbols,
                journal_summaries,
                strict=True,
            )
        },
        "journal_sample_count_by_symbol": {
            symbol: summary.sample_count
            for symbol, summary in zip(
                configured_symbols,
                journal_summaries,
                strict=True,
            )
        },
        "journal_unique_order_count_by_symbol": {
            symbol: summary.unique_order_count
            for symbol, summary in zip(
                configured_symbols,
                journal_summaries,
                strict=True,
            )
        },
        "strategy_config_sha256": strategy_config_sha256,
        "deployment_config_sha256": target_deployment_config_sha256,
        "calibration_config_path": str(calibration_config_path),
        "calibration_config_sha256": calibration_config_sha256,
        "implementation_sha256": implementation_sha256,
        "deployment_id": deployment_id,
        "calibration_artifact_sha256": calibration_artifact_sha256,
        "oos_evidence_sha256": calculated_oos_sha256,
    }


def _validate_glft_calibration_artifact(
    *,
    configured_symbols: tuple[str, ...],
    strategy_config: Mapping[str, Any],
    min_model_samples: int,
    strategy_config_sha256: str,
    deployment_config_sha256: str,
    implementation_sha256: str,
    deployment_id: str,
    source_evidence: Mapping[str, Any],
    artifact_document: Mapping[str, Any],
) -> dict[str, Any]:
    artifact = artifact_document
    expected_artifact_keys = {
        "schema",
        "model",
        "venue",
        "data_source",
        "units_version",
        "validated_formula_version",
        "deployment_id",
        "strategy_policy_sha256",
        "deployment_config_sha256",
        "implementation_sha256",
        "order_sample_count",
        "unique_order_count",
        "exposure_sample_count",
        "source_journal",
        "symbols",
    }
    if set(artifact) != expected_artifact_keys:
        raise ValueError("GLFT calibration artifact keys are invalid")
    if artifact.get("schema") != GLFT_CALIBRATION_ARTIFACT_SCHEMA:
        raise ValueError(
            "GLFT calibration artifact schema must be "
            f"{GLFT_CALIBRATION_ARTIFACT_SCHEMA!r}"
        )
    if canonical_model_key(artifact.get("model")) != "glft":
        raise ValueError("GLFT calibration artifact model must be 'glft'")
    if artifact.get("venue") != GLFT_CALIBRATION_VENUE:
        raise ValueError(
            "GLFT calibration artifact venue must be "
            f"{GLFT_CALIBRATION_VENUE!r}"
        )
    if artifact.get("data_source") != GLFT_CALIBRATION_DATA_SOURCE:
        raise ValueError(
            "GLFT calibration artifact data_source must be "
            f"{GLFT_CALIBRATION_DATA_SOURCE!r}"
        )
    if artifact.get("units_version") != IMPLEMENTED_UNITS_VERSION:
        raise ValueError(
            "GLFT calibration artifact units_version does not match this build"
        )
    if (
        artifact.get("validated_formula_version")
        != IMPLEMENTED_FORMULA_VERSIONS["glft"]
    ):
        raise ValueError(
            "GLFT calibration artifact formula version does not match this build"
        )
    if artifact.get("deployment_id") != deployment_id:
        raise ValueError(
            "GLFT calibration artifact deployment_id does not match "
            "source evidence"
        )
    if (
        _sha256_value(
            artifact.get("strategy_policy_sha256"),
            "GLFT calibration artifact strategy_policy_sha256",
        )
        != strategy_config_sha256
    ):
        raise ValueError(
            "GLFT calibration artifact strategy policy hash mismatch"
        )
    if (
        _sha256_value(
            artifact.get("deployment_config_sha256"),
            "GLFT calibration artifact deployment_config_sha256",
        )
        != deployment_config_sha256
    ):
        raise ValueError(
            "GLFT calibration artifact deployment configuration hash mismatch"
        )
    if (
        _sha256_value(
            artifact.get("implementation_sha256"),
            "GLFT calibration artifact implementation_sha256",
        )
        != implementation_sha256
    ):
        raise ValueError(
            "GLFT calibration artifact implementation hash mismatch"
        )
    order_sample_count = _positive_int(
        artifact.get("order_sample_count"),
        "GLFT calibration artifact order_sample_count",
    )
    unique_order_count = _positive_int(
        artifact.get("unique_order_count"),
        "GLFT calibration artifact unique_order_count",
    )
    exposure_sample_count = _positive_int(
        artifact.get("exposure_sample_count"),
        "GLFT calibration artifact exposure_sample_count",
    )
    if order_sample_count != unique_order_count:
        raise ValueError(
            "GLFT calibration artifact order and unique-order counts differ"
        )
    if (
        order_sample_count != source_evidence["order_sample_count"]
        or unique_order_count != source_evidence["unique_order_count"]
    ):
        raise ValueError(
            "GLFT calibration artifact order counts do not match source "
            "evidence"
        )

    source_journal = artifact.get("source_journal")
    if not isinstance(source_journal, Mapping):
        raise ValueError(
            "GLFT calibration artifact source_journal must be an object"
        )
    expected_source_journal_keys = {
        "sha256",
        "deployment_id",
        "first_seq",
        "last_seq",
        "final_hash",
        "record_count",
        "first_record_at_utc",
        "last_record_at_utc",
        "first_sample_at_utc",
        "last_sample_at_utc",
        "first_ack_at_utc",
        "last_terminal_at_utc",
        "sample_count",
        "unique_order_count",
        "censored_sample_count",
        "strategy_policy_sha256",
        "implementation_sha256",
        "calibration_config_sha256",
        "target_deployment_config_sha256",
        "permit_activation_count",
        "permit_sha256s",
        "reservation_count",
        "cumulative_submitted_notional_microu",
    }
    if set(source_journal) != expected_source_journal_keys:
        raise ValueError(
            "GLFT calibration artifact source_journal keys are invalid"
        )
    source_journal_checks = {
        "sha256": _sha256_value(
            source_journal.get("sha256"),
            "GLFT calibration artifact source_journal.sha256",
        ),
        "first_seq": _positive_int(
            source_journal.get("first_seq"),
            "GLFT calibration artifact source_journal.first_seq",
        ),
        "last_seq": _positive_int(
            source_journal.get("last_seq"),
            "GLFT calibration artifact source_journal.last_seq",
        ),
        "final_hash": _sha256_value(
            source_journal.get("final_hash"),
            "GLFT calibration artifact source_journal.final_hash",
        ),
        "record_count": _positive_int(
            source_journal.get("record_count"),
            "GLFT calibration artifact source_journal.record_count",
        ),
        "sample_count": _positive_int(
            source_journal.get("sample_count"),
            "GLFT calibration artifact source_journal.sample_count",
        ),
        "unique_order_count": _positive_int(
            source_journal.get("unique_order_count"),
            "GLFT calibration artifact source_journal.unique_order_count",
        ),
        "censored_sample_count": _nonnegative_int(
            source_journal.get("censored_sample_count"),
            "GLFT calibration artifact source_journal.censored_sample_count",
        ),
        "permit_activation_count": _positive_int(
            source_journal.get("permit_activation_count"),
            "GLFT calibration artifact "
            "source_journal.permit_activation_count",
        ),
        "reservation_count": _positive_int(
            source_journal.get("reservation_count"),
            "GLFT calibration artifact source_journal.reservation_count",
        ),
        "cumulative_submitted_notional_microu": _positive_int(
            source_journal.get("cumulative_submitted_notional_microu"),
            "GLFT calibration artifact "
            "source_journal.cumulative_submitted_notional_microu",
        ),
    }
    expected_source_journal = {
        "sha256": source_evidence["journal_sha256"],
        "first_seq": source_evidence["journal_first_seq"],
        "last_seq": source_evidence["journal_last_seq"],
        "final_hash": source_evidence["journal_final_hash"],
        "record_count": source_evidence["journal_record_count"],
        "sample_count": source_evidence["journal_sample_count"],
        "unique_order_count": (
            source_evidence["journal_unique_order_count"]
        ),
        "censored_sample_count": (
            source_evidence["journal_censored_sample_count"]
        ),
        "permit_activation_count": (
            source_evidence["journal_permit_activation_count"]
        ),
        "reservation_count": source_evidence["journal_reservation_count"],
        "cumulative_submitted_notional_microu": (
            source_evidence[
                "journal_cumulative_submitted_notional_microu"
            ]
        ),
    }
    if source_journal_checks != expected_source_journal:
        raise ValueError(
            "GLFT calibration artifact source_journal metadata does not "
            "match source evidence"
        )
    if source_journal.get("deployment_id") != deployment_id:
        raise ValueError(
            "GLFT calibration artifact source_journal deployment_id mismatch"
        )
    raw_source_permit_sha256s = source_journal.get("permit_sha256s")
    if not isinstance(raw_source_permit_sha256s, list):
        raise ValueError(
            "GLFT calibration artifact source_journal.permit_sha256s must "
            "be a list"
        )
    source_permit_sha256s = tuple(
        _sha256_value(
            value,
            "GLFT calibration artifact source_journal.permit_sha256s",
        )
        for value in raw_source_permit_sha256s
    )
    if (
        len(source_permit_sha256s)
        != source_journal_checks["permit_activation_count"]
        or len(set(source_permit_sha256s)) != len(source_permit_sha256s)
        or source_permit_sha256s
        != source_evidence["journal_permit_sha256s"]
    ):
        raise ValueError(
            "GLFT calibration artifact permit activation evidence mismatch"
        )
    if (
        _sha256_value(
            source_journal.get("strategy_policy_sha256"),
            "GLFT calibration artifact source_journal.strategy_policy_sha256",
        )
        != strategy_config_sha256
        or _sha256_value(
            source_journal.get("implementation_sha256"),
            "GLFT calibration artifact source_journal.implementation_sha256",
        )
        != implementation_sha256
        or _sha256_value(
            source_journal.get("calibration_config_sha256"),
            "GLFT calibration artifact "
            "source_journal.calibration_config_sha256",
        )
        != source_evidence["calibration_config_sha256"]
        or _sha256_value(
            source_journal.get("target_deployment_config_sha256"),
            "GLFT calibration artifact "
            "source_journal.target_deployment_config_sha256",
        )
        != deployment_config_sha256
    ):
        raise ValueError(
            "GLFT calibration artifact source_journal identity mismatch"
        )
    timestamp_checks = {
        "first_record_at_utc": _parse_utc_timestamp(
            source_journal.get("first_record_at_utc"),
            "GLFT calibration artifact source_journal.first_record_at_utc",
        ),
        "last_record_at_utc": _parse_utc_timestamp(
            source_journal.get("last_record_at_utc"),
            "GLFT calibration artifact source_journal.last_record_at_utc",
        ),
        "first_sample_at_utc": _parse_utc_timestamp(
            source_journal.get("first_sample_at_utc"),
            "GLFT calibration artifact source_journal.first_sample_at_utc",
        ),
        "last_sample_at_utc": _parse_utc_timestamp(
            source_journal.get("last_sample_at_utc"),
            "GLFT calibration artifact source_journal.last_sample_at_utc",
        ),
        "first_ack_at_utc": _parse_utc_timestamp(
            source_journal.get("first_ack_at_utc"),
            "GLFT calibration artifact source_journal.first_ack_at_utc",
        ),
        "last_terminal_at_utc": _parse_utc_timestamp(
            source_journal.get("last_terminal_at_utc"),
            "GLFT calibration artifact source_journal.last_terminal_at_utc",
        ),
    }
    expected_timestamps = {
        "first_record_at_utc": (
            source_evidence["journal_first_record_at_utc"]
        ),
        "last_record_at_utc": (
            source_evidence["journal_last_record_at_utc"]
        ),
        "first_sample_at_utc": (
            source_evidence["journal_first_sample_at_utc"]
        ),
        "last_sample_at_utc": (
            source_evidence["journal_last_sample_at_utc"]
        ),
        "first_ack_at_utc": (
            source_evidence["journal_first_ack_at_utc"]
        ),
        "last_terminal_at_utc": (
            source_evidence["journal_last_terminal_at_utc"]
        ),
    }
    if timestamp_checks != expected_timestamps:
        raise ValueError(
            "GLFT calibration artifact source_journal timestamps do not "
            "match source evidence"
        )

    raw_symbols = artifact.get("symbols")
    if not isinstance(raw_symbols, Mapping):
        raise ValueError("GLFT calibration artifact symbols must be an object")
    normalized_keys = tuple(
        str(symbol or "").strip().upper() for symbol in raw_symbols
    )
    if (
        any(not symbol for symbol in normalized_keys)
        or len(set(normalized_keys)) != len(normalized_keys)
        or set(normalized_keys) != set(configured_symbols)
    ):
        raise ValueError(
            "GLFT calibration artifact symbols must exactly match configured symbols"
        )

    requirements = _glft_intensity_requirements(
        strategy_config,
        min_model_samples=min_model_samples,
    )
    canonical_symbols = {}
    symbol_order_sample_count = 0
    symbol_unique_order_count = 0
    symbol_exposure_sample_count = 0
    for raw_symbol, raw_payload in raw_symbols.items():
        symbol = str(raw_symbol).strip().upper()
        if not isinstance(raw_payload, Mapping):
            raise ValueError(
                f"GLFT calibration artifact symbol {symbol} must be an object"
            )
        if set(raw_payload) != {
            "order_sample_count",
            "unique_order_count",
            "rpi_exposure_bins",
        }:
            raise ValueError(
                f"GLFT calibration artifact {symbol} keys are invalid"
            )
        current_order_sample_count = _positive_int(
            raw_payload.get("order_sample_count"),
            f"GLFT calibration artifact {symbol} order_sample_count",
        )
        current_unique_order_count = _positive_int(
            raw_payload.get("unique_order_count"),
            f"GLFT calibration artifact {symbol} unique_order_count",
        )
        if current_order_sample_count != current_unique_order_count:
            raise ValueError(
                f"GLFT calibration artifact {symbol} order counts differ"
            )
        if (
            current_order_sample_count
            != source_evidence["journal_sample_count_by_symbol"][symbol]
            or current_unique_order_count
            != source_evidence["journal_unique_order_count_by_symbol"][symbol]
        ):
            raise ValueError(
                f"GLFT calibration artifact {symbol} order counts do not "
                "match the validated journal"
            )
        if current_unique_order_count < requirements.min_sample_count:
            raise ValueError(
                f"GLFT calibration artifact {symbol} unique_order_count "
                "is below the live minimum"
            )
        raw_bins = raw_payload.get("rpi_exposure_bins")
        if not isinstance(raw_bins, (list, tuple)) or not raw_bins:
            raise ValueError(
                f"GLFT calibration artifact {symbol} requires rpi_exposure_bins"
            )
        bins = []
        for raw_bin in raw_bins:
            if (
                not isinstance(raw_bin, Mapping)
                or set(raw_bin)
                != {
                    "depth_bps",
                    "exposure_seconds",
                    "fill_count",
                    "sample_count",
                }
            ):
                raise ValueError(
                    f"GLFT calibration artifact {symbol} contains an invalid bin"
                )
            if (
                isinstance(raw_bin.get("depth_bps"), bool)
                or not isinstance(raw_bin.get("depth_bps"), (int, float))
                or isinstance(raw_bin.get("exposure_seconds"), bool)
                or not isinstance(
                    raw_bin.get("exposure_seconds"),
                    (int, float),
                )
                or isinstance(raw_bin.get("fill_count"), bool)
                or not isinstance(raw_bin.get("fill_count"), int)
                or isinstance(raw_bin.get("sample_count"), bool)
                or not isinstance(raw_bin.get("sample_count"), int)
            ):
                raise ValueError(
                    f"GLFT calibration artifact {symbol} bin types are invalid"
                )
            bins.append(
                RPIExposureBin(
                    depth_bps=raw_bin.get("depth_bps"),
                    exposure_seconds=raw_bin.get("exposure_seconds"),
                    fill_count=raw_bin.get("fill_count"),
                    sample_count=raw_bin.get("sample_count"),
                )
            )
        estimate = estimate_rpi_intensity(
            bins,
            requirements=requirements,
        )
        journal_estimate = estimate_rpi_intensity(
            source_evidence["journal_bins_by_symbol"][symbol],
            requirements=requirements,
        )
        if estimate.bins != journal_estimate.bins:
            raise ValueError(
                f"GLFT calibration artifact {symbol} exposure bins do not "
                "match the validated journal"
            )
        if not estimate.ready:
            reasons = ",".join(estimate.reasons) or estimate.state
            raise ValueError(
                f"GLFT calibration artifact {symbol} is not ready: {reasons}"
            )
        symbol_order_sample_count += current_order_sample_count
        symbol_unique_order_count += current_unique_order_count
        symbol_exposure_sample_count += estimate.sample_count
        canonical_symbols[symbol] = {
            "order_sample_count": current_order_sample_count,
            "unique_order_count": current_unique_order_count,
            "rpi_exposure_bins": [
                {
                    "depth_bps": item.depth_bps,
                    "exposure_seconds": item.exposure_seconds,
                    "fill_count": item.fill_count,
                    "sample_count": item.sample_count,
                }
                for item in estimate.bins
            ],
            "estimate": {
                "A_per_s": estimate.A_per_s,
                "k_per_bps": estimate.k_per_bps,
                "sample_count": estimate.sample_count,
                "depth_level_count": estimate.depth_level_count,
                "total_exposure_seconds": estimate.total_exposure_seconds,
                "fill_count": estimate.fill_count,
                "zero_fill_depth_level_count": (
                    estimate.zero_fill_depth_level_count
                ),
                "zero_fill_exposure_seconds": (
                    estimate.zero_fill_exposure_seconds
                ),
                "log_likelihood": estimate.log_likelihood,
            },
        }
    if (
        symbol_order_sample_count != order_sample_count
        or symbol_unique_order_count != unique_order_count
        or symbol_exposure_sample_count != exposure_sample_count
    ):
        raise ValueError(
            "GLFT calibration artifact aggregate counts do not match symbols"
        )
    return {
        "schema": GLFT_CALIBRATION_ARTIFACT_SCHEMA,
        "deployment_id": deployment_id,
        "strategy_policy_sha256": strategy_config_sha256,
        "deployment_config_sha256": deployment_config_sha256,
        "implementation_sha256": implementation_sha256,
        "order_sample_count": order_sample_count,
        "unique_order_count": unique_order_count,
        "exposure_sample_count": exposure_sample_count,
        "source_journal": dict(source_journal),
        "venue": GLFT_CALIBRATION_VENUE,
        "data_source": GLFT_CALIBRATION_DATA_SOURCE,
        "units_version": IMPLEMENTED_UNITS_VERSION,
        "validated_formula_version": IMPLEMENTED_FORMULA_VERSIONS["glft"],
        "symbols": canonical_symbols,
    }


def _glft_intensity_requirements(
    strategy_config: Mapping[str, Any],
    *,
    min_model_samples: int,
) -> RPIIntensityRequirements:
    raw_glft = strategy_config.get("glft", {})
    glft = raw_glft if isinstance(raw_glft, Mapping) else {}
    raw_intensity = glft.get(
        "rpi_intensity",
        strategy_config.get("rpi_intensity", {}),
    )
    intensity = raw_intensity if isinstance(raw_intensity, Mapping) else {}
    defaults = RPIIntensityRequirements()
    return RPIIntensityRequirements(
        min_sample_count=intensity.get(
            "min_sample_count",
            max(defaults.min_sample_count, min_model_samples),
        ),
        min_depth_level_count=intensity.get(
            "min_depth_level_count",
            defaults.min_depth_level_count,
        ),
        min_total_exposure_seconds=intensity.get(
            "min_total_exposure_seconds",
            defaults.min_total_exposure_seconds,
        ),
        min_fill_count=intensity.get(
            "min_fill_count",
            defaults.min_fill_count,
        ),
        min_depth_span_bps=intensity.get(
            "min_depth_span_bps",
            defaults.min_depth_span_bps,
        ),
        min_k_per_bps=intensity.get(
            "min_k_per_bps",
            defaults.min_k_per_bps,
        ),
        max_k_per_bps=intensity.get(
            "max_k_per_bps",
            defaults.max_k_per_bps,
        ),
    )


def _sha256_value(value: Any, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise ValueError(f"{field} must be a 64-character SHA-256 hex digest")
    return normalized


__all__ = [
    "APPROVAL_SIGNATURE_ALGORITHM",
    "CALIBRATION_APPROVAL_SCHEMA",
    "CALIBRATION_EVIDENCE_BUNDLE_SCHEMA",
    "DEFAULT_MODEL_READINESS_CONFIG",
    "DEPLOYMENT_CONFIG_PROJECTION_SCHEMA",
    "GLFT_CALIBRATION_ARTIFACT_SCHEMA",
    "GLFT_CALIBRATION_DATA_SOURCE",
    "GLFT_CALIBRATION_SOURCE_EVIDENCE_SCHEMA",
    "GLFT_CALIBRATION_VENUE",
    "IMPLEMENTED_FORMULA_VERSIONS",
    "IMPLEMENTED_UNITS_VERSION",
    "LIVE_APPROVED_FORMULA_VERSIONS",
    "ApprovalSignatureVerifier",
    "ReadinessRequirements",
    "SymbolReadiness",
    "apply_model_readiness_defaults",
    "approval_signature_payload",
    "calibration_evidence_bundle_sha256",
    "deployment_config_projection",
    "deployment_config_sha256",
    "evaluate_symbol_readiness",
    "formula_source_path_for_model",
    "formula_version_for_model",
    "implementation_sha256_for_model",
    "implementation_source_paths_for_model",
    "oos_evidence_sha256",
    "readiness_requirements",
    "sha256_file",
    "strategy_policy_sha256",
    "validate_live_calibration_approval",
    "verify_ed25519_signature",
]
