import hashlib
import json
import math


MAX_CANARY_DEPLOYED_EQUITY_FRACTION = 0.02


def deployment_policy_fingerprint(
    *,
    deployment_id: str,
    symbols,
    declared_account_equity: float,
    max_deployed_capital: float,
    maximum_loss: float,
    reduce_only_fraction: float,
    max_equity_fraction: float = MAX_CANARY_DEPLOYED_EQUITY_FRACTION,
) -> str:
    """Return a stable identity for one deployment's immutable risk envelope."""
    deployment_id = str(deployment_id or "").strip()
    if not deployment_id:
        return ""
    numeric_values = (
        declared_account_equity,
        max_deployed_capital,
        maximum_loss,
        reduce_only_fraction,
        max_equity_fraction,
    )
    if not all(math.isfinite(float(value)) for value in numeric_values):
        raise ValueError("deployment policy inputs must be finite")
    payload = {
        "declared_account_equity": float(declared_account_equity),
        "deployment_id": deployment_id,
        "max_deployed_capital": float(max_deployed_capital),
        "maximum_loss": float(maximum_loss),
        "max_equity_fraction": float(max_equity_fraction),
        "reduce_only_fraction": float(reduce_only_fraction),
        "symbols": sorted(
            str(symbol or "").strip().upper()
            for symbol in (symbols or ())
            if str(symbol or "").strip()
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def deployed_capital_equity_ratio(
    *,
    equity: float,
    max_deployed_capital: float,
) -> float:
    """Return the live capital envelope as a fraction of current equity."""
    try:
        equity = float(equity)
        max_deployed_capital = float(max_deployed_capital)
    except (TypeError, ValueError) as exc:
        raise ValueError("deployed capital inputs must be finite") from exc
    if (
        not math.isfinite(equity)
        or not math.isfinite(max_deployed_capital)
        or equity <= 0.0
        or max_deployed_capital < 0.0
    ):
        raise ValueError(
            "equity must be positive and deployed capital non-negative"
        )
    return max_deployed_capital / equity


def deployed_capital_within_equity_limit(
    *,
    equity: float,
    max_deployed_capital: float,
    maximum_fraction: float = MAX_CANARY_DEPLOYED_EQUITY_FRACTION,
) -> bool:
    """Fail closed when the configured canary envelope is too large."""
    try:
        maximum_fraction = float(maximum_fraction)
    except (TypeError, ValueError) as exc:
        raise ValueError("maximum deployed equity fraction must be finite") from exc
    if not math.isfinite(maximum_fraction) or not 0.0 < maximum_fraction <= 1.0:
        raise ValueError(
            "maximum deployed equity fraction must be in (0, 1]"
        )
    ratio = deployed_capital_equity_ratio(
        equity=equity,
        max_deployed_capital=max_deployed_capital,
    )
    return ratio <= maximum_fraction + 1e-12


def update_deployment_loss(
    *,
    equity: float,
    external_cash_flow_total: float,
    start_equity: float,
    start_external_cash_flow_total: float,
) -> tuple[float, float, float, float]:
    """Return a cash-flow-adjusted, non-resetting deployment loss state."""
    values = (
        equity,
        external_cash_flow_total,
        start_equity,
        start_external_cash_flow_total,
    )
    if not all(math.isfinite(float(value)) for value in values):
        raise ValueError("deployment loss inputs must be finite")

    equity = float(equity)
    external_cash_flow_total = float(external_cash_flow_total)
    start_equity = float(start_equity)
    start_external_cash_flow_total = float(
        start_external_cash_flow_total
    )
    if start_equity <= 0.0:
        start_equity = equity
        start_external_cash_flow_total = external_cash_flow_total

    cash_flow_delta = (
        external_cash_flow_total - start_external_cash_flow_total
    )
    adjusted_equity = equity - cash_flow_delta
    loss = max(0.0, start_equity - adjusted_equity)
    return (
        start_equity,
        start_external_cash_flow_total,
        adjusted_equity,
        loss,
    )


def deployment_loss_action(
    *,
    loss: float,
    maximum_loss: float,
    reduce_only_fraction: float,
) -> str:
    """Return NONE, REDUCE_ONLY, or KILL for a deployment loss."""
    loss = float(loss)
    maximum_loss = float(maximum_loss)
    reduce_only_fraction = float(reduce_only_fraction)
    if not all(
        math.isfinite(value)
        for value in (loss, maximum_loss, reduce_only_fraction)
    ):
        raise ValueError("deployment loss policy must be finite")
    if maximum_loss <= 0.0:
        return "NONE"
    if loss >= maximum_loss:
        return "KILL"
    threshold = maximum_loss * min(
        1.0,
        max(0.0, reduce_only_fraction),
    )
    if threshold > 0.0 and loss >= threshold:
        return "REDUCE_ONLY"
    return "NONE"
