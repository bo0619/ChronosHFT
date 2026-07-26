class AccountConfigurationVerificationError(ValueError):
    """Raised when Binance account configuration cannot be proven safe."""


def _parse_positive_integer(value, *, field: str) -> int:
    if isinstance(value, bool):
        raise AccountConfigurationVerificationError(f"{field} must be a positive integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        parsed = int(value)
    else:
        raise AccountConfigurationVerificationError(f"{field} must be a positive integer")
    if parsed <= 0:
        raise AccountConfigurationVerificationError(f"{field} must be a positive integer")
    return parsed


def verify_account_configuration(
    *,
    position_mode_payload,
    position_risk_payload,
    symbols,
    target_position_mode,
    target_margin_type,
    target_leverage,
) -> None:
    """Fail closed unless Binance truth exactly matches the requested safe state."""
    normalized_position_mode = str(target_position_mode or "").upper()
    if normalized_position_mode != "ONE_WAY":
        raise AccountConfigurationVerificationError("target position mode must be ONE_WAY")

    normalized_margin_type = str(target_margin_type or "").upper()
    if normalized_margin_type != "ISOLATED":
        raise AccountConfigurationVerificationError("target margin type must be ISOLATED")

    expected_leverage = _parse_positive_integer(
        target_leverage,
        field="target leverage",
    )
    if not isinstance(position_mode_payload, dict):
        raise AccountConfigurationVerificationError("position mode response must be an object")
    if position_mode_payload.get("dualSidePosition") is not False:
        raise AccountConfigurationVerificationError("account is not in ONE_WAY position mode")

    if isinstance(symbols, (str, bytes)) or not isinstance(symbols, (list, tuple, set)):
        raise AccountConfigurationVerificationError("symbols must be a collection")
    expected_symbols = set()
    for raw_symbol in symbols:
        symbol = str(raw_symbol or "").strip().upper()
        if not symbol:
            raise AccountConfigurationVerificationError("symbols contain an empty value")
        expected_symbols.add(symbol)
    if not expected_symbols:
        raise AccountConfigurationVerificationError("at least one symbol must be verified")

    if not isinstance(position_risk_payload, list):
        raise AccountConfigurationVerificationError("position risk response must be a list")

    verified_symbols = set()
    for row in position_risk_payload:
        if not isinstance(row, dict):
            raise AccountConfigurationVerificationError("position risk row must be an object")
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise AccountConfigurationVerificationError("position risk row is missing symbol")
        symbol = symbol.strip().upper()
        if symbol not in expected_symbols:
            continue
        if symbol in verified_symbols:
            raise AccountConfigurationVerificationError(
                f"duplicate position risk row for {symbol}"
            )
        if row.get("positionSide") != "BOTH":
            raise AccountConfigurationVerificationError(
                f"{symbol} positionSide is not BOTH"
            )
        margin_type = row.get("marginType")
        if (
            not isinstance(margin_type, str)
            or margin_type.strip().upper() != "ISOLATED"
        ):
            raise AccountConfigurationVerificationError(f"{symbol} margin is not ISOLATED")
        leverage = _parse_positive_integer(
            row.get("leverage"),
            field=f"{symbol} leverage",
        )
        if leverage != expected_leverage:
            raise AccountConfigurationVerificationError(
                f"{symbol} leverage {leverage} does not match target {expected_leverage}"
            )
        verified_symbols.add(symbol)

    missing = sorted(expected_symbols - verified_symbols)
    if missing:
        raise AccountConfigurationVerificationError(
            f"position risk response is missing symbols: {', '.join(missing)}"
        )
