from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation

from strategy.registry import (
    canonical_model_key,
    effective_primary_strategy_config,
)


def _enabled(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    if isinstance(value, (int, float)):
        return value == 1
    return False


def _effective_strategy(config: Mapping) -> Mapping:
    strategy = config.get("strategy", {})
    if not isinstance(strategy, Mapping):
        return {}
    if not strategy.get("registered_models"):
        return strategy
    return effective_primary_strategy_config(config)


def requires_zero_rpi_commission(config: Mapping) -> bool:
    """Return the configured account-truth requirement without coercion loss."""
    if not isinstance(config, Mapping):
        return False
    strategy = _effective_strategy(config)
    policy = strategy.get("rpi_live_policy", {})
    if not isinstance(policy, Mapping):
        return False
    return _enabled(policy.get("require_zero_commission", False))


def effective_rpi_route_enabled(config: Mapping) -> bool:
    """Return whether the primary strategy will actually request RPI."""
    if not isinstance(config, Mapping):
        return False
    strategy = _effective_strategy(config)
    if not _enabled(strategy.get("use_rpi")):
        return False

    root_strategy = config.get("strategy", {})
    if not isinstance(root_strategy, Mapping):
        root_strategy = strategy
    primary_value = root_strategy.get(
        "primary_model",
        root_strategy.get("name"),
    )
    if primary_value is None:
        # Preserve support for legacy single-strategy configurations.
        return True
    try:
        primary_model = canonical_model_key(primary_value)
    except ValueError:
        return False

    if primary_model == "glft":
        model_config = strategy.get("glft", {})
        model_config = (
            model_config if isinstance(model_config, Mapping) else {}
        )
        model_route = model_config.get(
            "use_rpi",
            strategy.get("use_rpi_for_glft", True),
        )
    else:
        model_config = strategy.get("as_parameters", {})
        model_config = (
            model_config if isinstance(model_config, Mapping) else {}
        )
        model_route = model_config.get(
            "use_rpi",
            strategy.get("use_rpi_for_avellaneda_stoikov", True),
        )
    return _enabled(model_route)


def validate_live_rpi_policy(
    config: Mapping,
    symbols: Iterable[str],
    supports_rpi_by_symbol: Mapping[str, object],
    rpi_commission_rates: Mapping[str, object],
) -> dict:
    """Validate the fail-closed RPI-only policy from already-fetched truth."""
    if not isinstance(config, Mapping):
        raise ValueError("Live RPI policy configuration must be a mapping")

    try:
        strategy = _effective_strategy(config)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Unsafe live RPI policy: effective strategy is invalid: {exc}"
        ) from exc
    policy = strategy.get("rpi_live_policy", {})
    if not isinstance(policy, Mapping):
        policy = {}

    violations = []
    if not _enabled(strategy.get("use_rpi")):
        violations.append("strategy.use_rpi must be true")
    elif not effective_rpi_route_enabled(config):
        violations.append(
            "the primary strategy's effective RPI route must be enabled"
        )
    if _enabled(strategy.get("rpi_fallback_to_gtx", True)):
        violations.append("strategy.rpi_fallback_to_gtx must be false")

    for section_name in ("glft", "avellaneda_stoikov", "as_parameters"):
        section = strategy.get(section_name, {})
        if (
            isinstance(section, Mapping)
            and "rpi_fallback_to_gtx" in section
            and _enabled(section.get("rpi_fallback_to_gtx"))
        ):
            violations.append(
                f"strategy.{section_name}.rpi_fallback_to_gtx must be false"
            )

    normalized_symbols = []
    seen_symbols = set()
    for raw_symbol in symbols or ():
        symbol = str(raw_symbol or "").strip().upper()
        if not symbol:
            violations.append("configured live symbol must not be empty")
            continue
        if symbol in seen_symbols:
            violations.append(f"configured live symbol is duplicated: {symbol}")
            continue
        seen_symbols.add(symbol)
        normalized_symbols.append(symbol)
    if not normalized_symbols:
        violations.append("at least one live RPI symbol is required")

    normalized_support = {
        str(symbol or "").strip().upper(): _enabled(supported)
        for symbol, supported in (supports_rpi_by_symbol or {}).items()
    }
    normalized_rates = {
        str(symbol or "").strip().upper(): rate
        for symbol, rate in (rpi_commission_rates or {}).items()
    }
    require_zero_commission = requires_zero_rpi_commission(config)
    validated_rates = {}

    for symbol in normalized_symbols:
        if normalized_support.get(symbol) is not True:
            violations.append(
                f"{symbol} must be TRADING with RPI permission in exchangeInfo"
            )

        if symbol not in normalized_rates:
            violations.append(
                f"account-specific rpiCommissionRate is missing for {symbol}"
            )
            continue
        try:
            rate = Decimal(str(normalized_rates[symbol]))
        except (InvalidOperation, TypeError, ValueError):
            violations.append(
                f"account-specific rpiCommissionRate is invalid for {symbol}"
            )
            continue
        if not rate.is_finite() or abs(rate) > Decimal("0.01"):
            violations.append(
                f"account-specific rpiCommissionRate is out of range for "
                f"{symbol}: {rate}"
            )
            continue
        validated_rates[symbol] = float(rate)
        if require_zero_commission and rate != Decimal(0):
            violations.append(
                f"account-specific rpiCommissionRate must be zero for "
                f"{symbol}, got {rate}"
            )

    if violations:
        raise ValueError("Unsafe live RPI policy: " + "; ".join(violations))

    return {
        "symbols": tuple(normalized_symbols),
        "rpi_commission_rates": validated_rates,
        "require_zero_commission": require_zero_commission,
    }
