"""Secret-free deployment identity shared across architectural layers."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from governance.canonical import canonical_json_bytes


DEPLOYMENT_CONFIG_PROJECTION_SCHEMA = (
    "chronoshft.live_deployment_config_projection.v2"
)

_MODEL_ALIASES = {
    "glft": "glft",
    "glftmultiscale": "glft",
    "as": "avellaneda_stoikov",
    "avellanedastoikov": "avellaneda_stoikov",
    "avellanedastoikovstrategy": "avellaneda_stoikov",
}


def _model_key(value: Any) -> str:
    normalized = "".join(
        character
        for character in str(value or "").casefold()
        if character.isalnum()
    )
    model = _MODEL_ALIASES.get(normalized)
    if model is None:
        raise ValueError(f"unknown deployment strategy model {value!r}")
    return model


def _normalized_symbols(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    symbols = tuple(str(symbol or "").strip().upper() for symbol in value)
    if any(not symbol for symbol in symbols) or len(set(symbols)) != len(symbols):
        return ()
    return symbols


def _redacted(value: Any, excluded_keys: set[str] | frozenset[str]) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _redacted(item, excluded_keys)
            for key, item in value.items()
            if isinstance(key, str)
            and key not in excluded_keys
            and not key.startswith("_validated_")
            and key != "_runtime_calibration"
        }
    if isinstance(value, list):
        return [_redacted(item, excluded_keys) for item in value]
    return deepcopy(value)


def _section(
    config: Mapping[str, Any],
    name: str,
    *,
    excluded_keys: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    value = config.get(name, {})
    if not isinstance(value, Mapping):
        return {}
    return _redacted(value, excluded_keys)


def deployment_config_projection(
    config: Mapping[str, Any],
) -> dict[str, Any]:
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
    model = _model_key(
        raw_strategy.get("primary_model", raw_strategy.get("name"))
    )
    strategy = _redacted(
        raw_strategy,
        {
            "commission_truth",
            "maker_fee",
            "rpi_commission_rate",
            "rpi_commission_rates",
            "taker_fee",
        },
    )
    readiness = strategy.get("model_readiness")
    if isinstance(readiness, dict):
        approval = readiness.get("live_approval")
        if isinstance(approval, dict):
            approval.pop("manifest_path", None)

    system = _section(
        config,
        "system",
        excluded_keys={"api_key", "api_key_env", "api_secret", "api_secret_env"},
    )
    system.pop("web_dashboard", None)
    projection = {
        "schema": DEPLOYMENT_CONFIG_PROJECTION_SCHEMA,
        "symbols": list(symbols),
        "execution": _section(config, "execution"),
        "paper_trade": _section(config, "paper_trade"),
        "testnet": config.get("testnet"),
        "record_data": config.get("record_data"),
        "live_launch": _section(config, "live_launch"),
        "system": system,
        "account": _section(config, "account"),
        "oms": _section(config, "oms"),
        "risk": _section(
            config,
            "risk",
            excluded_keys={
                "api_key",
                "api_key_env",
                "api_secret",
                "api_secret_env",
            },
        ),
        "strategy": {"primary_model": model, "config": strategy},
    }
    canonical_json_bytes(projection, label="deployment configuration projection")
    return projection


def deployment_config_sha256(config: Mapping[str, Any]) -> str:
    projection = deployment_config_projection(config)
    return hashlib.sha256(
        canonical_json_bytes(
            projection,
            label="deployment configuration projection",
        )
    ).hexdigest()


__all__ = [
    "DEPLOYMENT_CONFIG_PROJECTION_SCHEMA",
    "deployment_config_projection",
    "deployment_config_sha256",
]
