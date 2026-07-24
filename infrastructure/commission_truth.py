from collections.abc import Mapping
from decimal import Decimal, InvalidOperation


COMMISSION_RATE_FIELDS = (
    "makerCommissionRate",
    "takerCommissionRate",
    "rpiCommissionRate",
)
MAX_ABSOLUTE_COMMISSION_RATE = Decimal("0.01")


def parse_commission_rate_payload(
    payload: object,
    *,
    symbol: str,
) -> dict[str, Decimal]:
    """Validate one account-specific Binance commission-rate response."""
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        raise ValueError("Commission-rate symbol must not be empty")
    if not isinstance(payload, Mapping):
        raise ValueError(
            f"Commission-rate truth unavailable for {normalized_symbol}"
        )

    payload_symbol = str(payload.get("symbol", "") or "").strip().upper()
    if payload_symbol and payload_symbol != normalized_symbol:
        raise ValueError(
            "Commission-rate truth symbol mismatch: "
            f"expected {normalized_symbol}, got {payload_symbol}"
        )

    parsed = {}
    for field in COMMISSION_RATE_FIELDS:
        if field not in payload:
            raise ValueError(f"Missing {field} for {normalized_symbol}")
        try:
            rate = Decimal(str(payload[field]))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(
                f"Invalid {field} for {normalized_symbol}"
            ) from exc
        if (
            not rate.is_finite()
            or abs(rate) > MAX_ABSOLUTE_COMMISSION_RATE
        ):
            raise ValueError(
                f"Out-of-range {field} for {normalized_symbol}: {rate}"
            )
        parsed[field] = rate
    return parsed


def resolve_passive_fee_rate(
    *,
    maker_rate: object,
    symbol: str,
    is_rpi: bool,
    rpi_commission_rates: Mapping[str, object] | None = None,
    default_rpi_commission_rate: object = 0.0,
) -> float:
    """Return the final route rate; an RPI rate replaces the maker rate."""
    if not is_rpi:
        return float(maker_rate)

    normalized_symbol = str(symbol or "").upper()
    symbol_rate = None
    if isinstance(rpi_commission_rates, Mapping):
        symbol_rate = rpi_commission_rates.get(normalized_symbol)
        if symbol_rate is None:
            symbol_rate = rpi_commission_rates.get(str(symbol or ""))
    return float(
        symbol_rate
        if symbol_rate is not None
        else default_rpi_commission_rate
    )
