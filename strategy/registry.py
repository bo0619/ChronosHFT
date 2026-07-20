"""Strategy model registration and single-primary construction.

Registration is deliberately separate from construction: a deployment can
declare every model it is allowed to run while instantiating exactly one
primary execution strategy.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class StrategyRegistration:
    model_key: str
    strategy_id: str
    aliases: tuple[str, ...]


_REGISTRATIONS = (
    StrategyRegistration(
        model_key="ml_sniper",
        strategy_id="ML_Sniper",
        aliases=("ML_Sniper",),
    ),
    StrategyRegistration(
        model_key="glft",
        strategy_id="GLFT_MultiScale",
        aliases=("GLFT", "GLFT_MultiScale"),
    ),
    StrategyRegistration(
        model_key="avellaneda_stoikov",
        strategy_id="AvellanedaStoikov",
        aliases=("as", "AvellanedaStoikov"),
    ),
)

STRATEGY_REGISTRY = {
    registration.model_key: registration for registration in _REGISTRATIONS
}


def _normalize_model_name(value: Any) -> str:
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


_ALIASES = {
    _normalize_model_name(alias): registration.model_key
    for registration in _REGISTRATIONS
    for alias in (registration.model_key, *registration.aliases)
}

_REGISTRY_CONTROL_KEYS = {
    "name",
    "primary_model",
    "registered_models",
    "shared",
    "models",
}


def canonical_model_key(model_key: Any) -> str:
    """Return a canonical model key or fail before any model is constructed."""
    normalized = _normalize_model_name(model_key)
    canonical = _ALIASES.get(normalized)
    if canonical is None:
        supported = ", ".join(STRATEGY_REGISTRY)
        raise ValueError(
            f"Unknown strategy model {model_key!r}; supported models: {supported}"
        )
    return canonical


def strategy_id_for_model(model_key: Any) -> str:
    """Map a canonical key or alias to the stable OMS/risk strategy ID."""
    return STRATEGY_REGISTRY[canonical_model_key(model_key)].strategy_id


def registered_model_keys(config: Mapping[str, Any]) -> tuple[str, ...]:
    """Validate and canonicalize ``strategy.registered_models``."""
    strategy_config = _strategy_config(config)
    configured = strategy_config.get("registered_models")
    if not isinstance(configured, (list, tuple)) or not configured:
        raise ValueError("strategy.registered_models must be a non-empty list")

    canonical_models: list[str] = []
    for model in configured:
        canonical = canonical_model_key(model)
        if canonical not in canonical_models:
            canonical_models.append(canonical)
    return tuple(canonical_models)


def _strategy_config(config: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        raise TypeError("strategy configuration must be a mapping")
    nested = config.get("strategy")
    if isinstance(nested, Mapping):
        return nested
    return config


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _model_key_if_known(value: Any) -> str | None:
    return _ALIASES.get(_normalize_model_name(value))


def _merged_model_config(
    strategy_config: Mapping[str, Any],
    model_key: str,
) -> dict[str, Any]:
    # Preserve the legacy flat strategy fields as shared defaults, while
    # excluding registration controls and every model-specific block.
    shared = {
        key: deepcopy(value)
        for key, value in strategy_config.items()
        if key not in _REGISTRY_CONTROL_KEYS
        and key != "as_parameters"
        and _model_key_if_known(key) is None
    }
    explicit_shared = strategy_config.get("shared", {})
    if explicit_shared is not None and not isinstance(explicit_shared, Mapping):
        raise TypeError("strategy.shared must be a mapping")
    shared = _deep_merge(shared, explicit_shared or {})

    model_config: dict[str, Any] = {}
    # ``as_parameters`` is the existing A-S configuration spelling. Canonical
    # blocks take precedence when both legacy and canonical forms are present.
    if model_key == "avellaneda_stoikov":
        legacy_as_config = strategy_config.get("as_parameters", {})
        if legacy_as_config is not None and not isinstance(legacy_as_config, Mapping):
            raise TypeError("strategy.as_parameters must be a mapping")
        model_config = _deep_merge(model_config, legacy_as_config or {})

    for key, value in strategy_config.items():
        if _model_key_if_known(key) != model_key:
            continue
        if not isinstance(value, Mapping):
            raise TypeError(f"strategy.{key} must be a mapping")
        model_config = _deep_merge(model_config, value)

    models = strategy_config.get("models", {})
    if models is not None and not isinstance(models, Mapping):
        raise TypeError("strategy.models must be a mapping")
    for key, value in (models or {}).items():
        if _model_key_if_known(key) != model_key:
            continue
        if not isinstance(value, Mapping):
            raise TypeError(f"strategy.models.{key} must be a mapping")
        model_config = _deep_merge(model_config, value)

    merged = _deep_merge(shared, model_config)
    if model_key == "glft":
        merged["glft"] = deepcopy(model_config)
    elif model_key == "avellaneda_stoikov":
        merged["as_parameters"] = deepcopy(model_config)
    elif model_key == "ml_sniper":
        merged["ml_sniper"] = deepcopy(model_config)
    return merged


def _primary_model_key(
    strategy_config: Mapping[str, Any],
    registered_models: tuple[str, ...],
) -> str:
    configured_primary = strategy_config.get("primary_model")
    if configured_primary is None:
        configured_primary = strategy_config.get("name")
    if configured_primary is None and len(registered_models) == 1:
        configured_primary = registered_models[0]
    if configured_primary is None:
        raise ValueError("strategy.primary_model is required when multiple models are registered")

    primary_model = canonical_model_key(configured_primary)
    if primary_model not in registered_models:
        raise ValueError(
            f"Primary strategy model {primary_model!r} is not present in "
            "strategy.registered_models"
        )
    return primary_model


def _runtime_alpha_process_config(config: Mapping[str, Any]) -> dict[str, Any]:
    system_config = config.get("system", {}) if isinstance(config, Mapping) else {}
    if not isinstance(system_config, Mapping):
        return {}
    runtime_config = system_config.get("strategy_runtime", {})
    if not isinstance(runtime_config, Mapping):
        return {}
    alpha_config = runtime_config.get("alpha_process", {})
    return deepcopy(dict(alpha_config)) if isinstance(alpha_config, Mapping) else {}


def create_primary_strategy(
    engine: Any,
    oms: Any,
    config: Mapping[str, Any],
    *,
    alpha_process_config: Mapping[str, Any] | None = None,
) -> Any:
    """Validate registrations and construct the sole primary strategy."""
    strategy_config = _strategy_config(config)
    execution_policy = str(
        strategy_config.get("execution_policy", "single_primary")
        or "single_primary"
    ).strip().casefold()
    if execution_policy != "single_primary":
        raise ValueError(
            "Only strategy.execution_policy=single_primary is supported; "
            "multiple execution models would duplicate orders and inventory risk"
        )
    registered_models = registered_model_keys(config)
    primary_model = _primary_model_key(strategy_config, registered_models)
    merged_config = _merged_model_config(strategy_config, primary_model)

    if primary_model == "ml_sniper":
        from strategy.ml_sniper.ml_sniper import MLSniperStrategy

        if alpha_process_config is None:
            configured_alpha = merged_config.get("alpha_process_config")
            if isinstance(configured_alpha, Mapping):
                alpha_process_config = configured_alpha
            else:
                alpha_process_config = _runtime_alpha_process_config(config)
        strategy = MLSniperStrategy(
            engine,
            oms,
            alpha_process_config=dict(alpha_process_config or {}),
            strategy_config=merged_config,
        )
    elif primary_model == "glft":
        from strategy.glft import GLFTStrategy

        strategy = GLFTStrategy(engine, oms, strategy_config=merged_config)
    elif primary_model == "avellaneda_stoikov":
        from strategy.avellaneda_stoikov import AvellanedaStoikovStrategy

        strategy = AvellanedaStoikovStrategy(
            engine,
            oms,
            strategy_config=merged_config,
        )
    else:  # pragma: no cover - guarded by registry validation above.
        raise AssertionError(f"No strategy builder for {primary_model!r}")

    registration = STRATEGY_REGISTRY[primary_model]
    strategy.name = registration.strategy_id
    strategy.strategy_id = registration.strategy_id
    strategy.model_key = primary_model
    strategy.registered_models = registered_models
    strategy.execution_role = "primary"
    return strategy


__all__ = [
    "STRATEGY_REGISTRY",
    "StrategyRegistration",
    "canonical_model_key",
    "create_primary_strategy",
    "registered_model_keys",
    "strategy_id_for_model",
]
