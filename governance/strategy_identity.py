"""Pure strategy/release identity functions shared by runtime and tooling."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from governance.canonical import canonical_json_bytes
from governance.release_manifest import build_release_manifest, release_files
from strategy.quote_math import (
    AS_FORMULA_VERSION,
    GLFT_FORMULA_VERSION,
    UNITS_VERSION,
)


_ALIASES = {
    "glft": "glft",
    "glftmultiscale": "glft",
    "as": "avellaneda_stoikov",
    "avellanedastoikov": "avellaneda_stoikov",
}
_CONTROL_KEYS = {"name", "primary_model", "registered_models", "shared", "models"}
_TRUST_KEYS = frozenset(
    {
        "_rpi_sampling_identity",
        "_validated_calibration",
        "_validated_rpi_calibration_permit",
        "model_readiness",
    }
)
_FORMULA_VERSIONS = {
    "glft": GLFT_FORMULA_VERSION,
    "avellaneda_stoikov": AS_FORMULA_VERSION,
}


def _normalized_name(value: Any) -> str:
    return "".join(
        character
        for character in str(value or "").casefold()
        if character.isalnum()
    )


def canonical_model_key(value: Any) -> str:
    model = _ALIASES.get(_normalized_name(value))
    if model is None:
        raise ValueError(
            f"Unsupported model {value!r}; expected glft or avellaneda_stoikov"
        )
    return model


def formula_version_for_model(model: Any) -> str:
    return _FORMULA_VERSIONS[canonical_model_key(model)]


def _known_model(value: Any) -> str | None:
    return _ALIASES.get(_normalized_name(value))


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict:
    merged = deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _reject_trust_overrides(value: Mapping[str, Any], location: str) -> None:
    protected = sorted(_TRUST_KEYS.intersection(value))
    if protected:
        raise ValueError(
            f"{location} cannot override root-validated strategy fields: "
            + ", ".join(protected)
        )


def _effective_strategy_config(
    config: Mapping[str, Any],
    model: str,
) -> dict[str, Any]:
    strategy = config.get("strategy")
    if not isinstance(strategy, Mapping):
        raise ValueError("strategy must be an object")
    shared = {
        key: deepcopy(value)
        for key, value in strategy.items()
        if key not in _CONTROL_KEYS
        and key != "as_parameters"
        and _known_model(key) is None
    }
    explicit_shared = strategy.get("shared", {})
    if not isinstance(explicit_shared, Mapping):
        raise TypeError("strategy.shared must be an object")
    _reject_trust_overrides(explicit_shared, "strategy.shared")
    shared = _deep_merge(shared, explicit_shared)

    model_config: dict[str, Any] = {}
    if model == "avellaneda_stoikov":
        legacy = strategy.get("as_parameters", {})
        if not isinstance(legacy, Mapping):
            raise TypeError("strategy.as_parameters must be an object")
        _reject_trust_overrides(legacy, "strategy.as_parameters")
        model_config = _deep_merge(model_config, legacy)
    for key, value in strategy.items():
        if _known_model(key) != model:
            continue
        if not isinstance(value, Mapping):
            raise TypeError(f"strategy.{key} must be an object")
        _reject_trust_overrides(value, f"strategy.{key}")
        model_config = _deep_merge(model_config, value)
    models = strategy.get("models", {})
    if not isinstance(models, Mapping):
        raise TypeError("strategy.models must be an object")
    for key, value in models.items():
        if _known_model(key) != model:
            continue
        if not isinstance(value, Mapping):
            raise TypeError(f"strategy.models.{key} must be an object")
        _reject_trust_overrides(value, f"strategy.models.{key}")
        model_config = _deep_merge(model_config, value)

    effective = _deep_merge(shared, model_config)
    for key in _TRUST_KEYS:
        if key in strategy:
            effective[key] = deepcopy(strategy[key])
    if model == "glft":
        effective["glft"] = deepcopy(model_config)
    else:
        effective["as_parameters"] = deepcopy(model_config)
    return effective


def effective_strategy_config(
    config: Mapping[str, Any],
    model: Any,
) -> dict[str, Any]:
    """Resolve shared and model-specific policy without constructing a strategy."""
    return _effective_strategy_config(config, canonical_model_key(model))


def _normalized_symbols(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    symbols = tuple(str(item or "").strip().upper() for item in value)
    return symbols if all(symbols) and len(set(symbols)) == len(symbols) else ()


def strategy_policy_sha256(config: Mapping[str, Any], model: Any) -> str:
    model_key = canonical_model_key(model)
    effective = effective_strategy_config(config, model_key)
    if model_key == "glft":
        model_config = effective.get("glft", {})
        if not isinstance(model_config, Mapping):
            model_config = {}
        root_use_rpi = effective.get("use_rpi", False)
        model_use_rpi = model_config.get(
            "use_rpi",
            effective.get("use_rpi_for_glft", True),
        )
        root_fallback = effective.get("rpi_fallback_to_gtx", True)
        model_fallback = model_config.get("rpi_fallback_to_gtx", root_fallback)
        policy = {
            "model": model_key,
            "symbols": list(_normalized_symbols(config.get("symbols"))),
            "units_version": UNITS_VERSION,
            "formula_version": formula_version_for_model(model_key),
            "use_rpi": root_use_rpi,
            "use_rpi_for_glft": effective.get("use_rpi_for_glft", True),
            "glft_use_rpi": model_config.get("use_rpi"),
            "effective_use_rpi": bool(root_use_rpi) and bool(model_use_rpi),
            "rpi_fallback_to_gtx": root_fallback,
            "glft_rpi_fallback_to_gtx": model_config.get("rpi_fallback_to_gtx"),
            "effective_rpi_fallback_to_gtx": bool(model_fallback),
            "target_order_notional": effective.get("target_order_notional"),
            "max_pos_usdt": effective.get("max_pos_usdt"),
            "gamma": effective.get("gamma", model_config.get("gamma")),
            "cycle_interval": effective.get(
                "cycle_interval", model_config.get("cycle_interval")
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
                "calibrator", model_config.get("calibrator")
            ),
            "rpi_intensity": effective.get(
                "rpi_intensity", model_config.get("rpi_intensity")
            ),
            "execution": effective.get(
                "execution", model_config.get("execution")
            ),
        }
    else:
        model_config = effective.get("as_parameters", {})
        policy = {
            "model": model_key,
            "symbols": list(_normalized_symbols(config.get("symbols"))),
            "units_version": UNITS_VERSION,
            "formula_version": formula_version_for_model(model_key),
            "use_rpi": effective.get("use_rpi"),
            "rpi_fallback_to_gtx": effective.get("rpi_fallback_to_gtx"),
            "target_order_notional": effective.get("target_order_notional"),
            "max_pos_usdt": effective.get("max_pos_usdt"),
            "as_parameters": dict(model_config)
            if isinstance(model_config, Mapping)
            else {},
        }
    return hashlib.sha256(
        canonical_json_bytes(policy, label="effective strategy policy")
    ).hexdigest()


def implementation_source_paths_for_model(model: Any) -> tuple[Path, ...]:
    canonical_model_key(model)
    root = Path(__file__).resolve().parents[1]
    return tuple(root / path for path, _kind in release_files(root))


def implementation_sha256_for_model(model: Any) -> str:
    canonical_model_key(model)
    root = Path(__file__).resolve().parents[1]
    return str(build_release_manifest(root)["release_digest"])


__all__ = [
    "canonical_model_key",
    "effective_strategy_config",
    "formula_version_for_model",
    "implementation_sha256_for_model",
    "implementation_source_paths_for_model",
    "strategy_policy_sha256",
]
