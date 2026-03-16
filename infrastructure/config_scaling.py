import json
from copy import deepcopy


def _to_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def apply_capital_scaling(config: dict) -> dict:
    if not isinstance(config, dict):
        return {}

    scaled = deepcopy(config)
    strategy = scaled.setdefault("strategy", {})
    scaling = strategy.get("capital_scaling", {})
    if not isinstance(scaling, dict):
        scaling = {}

    enabled = bool(scaling.get("enabled", False) or "capital_multiplier" in strategy)
    if not enabled:
        return scaled

    capital_multiplier = _to_float(strategy.get("capital_multiplier", 1.0), 1.0)
    if capital_multiplier <= 0:
        capital_multiplier = 1.0

    account = scaled.setdefault("account", {})
    backtest = scaled.setdefault("backtest", {})
    risk = scaled.setdefault("risk", {})
    limits = risk.setdefault("limits", {})

    symbols = scaled.get("symbols", [])
    symbol_count = len(symbols) if isinstance(symbols, list) and symbols else 1

    reference_capital = max(
        1.0,
        _to_float(
            scaling.get(
                "reference_capital_usdt",
                account.get("initial_balance_usdt", backtest.get("initial_capital", 100.0)),
            ),
            100.0,
        ),
    )
    target_order_notional = max(
        1.0,
        _to_float(
            scaling.get("target_order_notional", limits.get("max_order_notional", 8.0)),
            8.0,
        ),
    )
    target_total_risk_notional = max(
        target_order_notional,
        _to_float(
            scaling.get("target_total_risk_notional", limits.get("max_pos_notional", 45.0)),
            45.0,
        ),
    )
    target_daily_loss = max(
        0.0,
        _to_float(scaling.get("target_daily_loss", limits.get("max_daily_loss", 5.0)), 5.0),
    )
    max_order_qty = max(
        1.0,
        _to_float(scaling.get("max_order_qty", limits.get("max_order_qty", 10000.0)), 10000.0),
    )
    target_concurrent_symbols = max(
        1,
        _to_int(
            scaling.get("target_concurrent_symbols", min(3, symbol_count)),
            min(3, symbol_count),
        ),
    )
    active_symbol_slots = min(symbol_count, target_concurrent_symbols)
    position_buffer_orders = max(
        1.0,
        _to_float(scaling.get("position_buffer_orders", 2.0), 2.0),
    )
    reference_min_notional = max(
        1.0,
        _to_float(scaling.get("reference_min_notional", 5.0), 5.0),
    )
    notional_buffer = max(
        1.0,
        _to_float(scaling.get("notional_buffer", 1.1), 1.1),
    )
    leverage = max(1.0, _to_float(account.get("leverage", 1.0), 1.0))

    derived_capital = reference_capital * capital_multiplier
    derived_order_notional = target_order_notional * capital_multiplier
    derived_total_risk_notional = target_total_risk_notional * capital_multiplier
    derived_symbol_cap = max(
        derived_order_notional * position_buffer_orders,
        derived_total_risk_notional / max(1, active_symbol_slots),
    )
    derived_daily_loss = target_daily_loss * capital_multiplier
    derived_max_order_qty = max_order_qty * max(1.0, capital_multiplier)
    derived_lot_multiplier = derived_order_notional / (
        reference_min_notional * notional_buffer * leverage
    )

    strategy["capital_multiplier"] = round(capital_multiplier, 8)
    account["initial_balance_usdt"] = round(derived_capital, 8)
    backtest["initial_capital"] = round(derived_capital, 8)
    limits["max_order_notional"] = round(derived_order_notional, 8)
    limits["max_pos_notional"] = round(derived_symbol_cap, 8)
    limits["max_account_gross_notional"] = round(derived_total_risk_notional, 8)
    limits["max_daily_loss"] = round(derived_daily_loss, 8)
    limits["max_order_qty"] = round(derived_max_order_qty, 8)
    strategy["lot_multiplier"] = round(max(0.01, derived_lot_multiplier), 8)
    strategy["max_pos_usdt"] = round(derived_symbol_cap, 8)

    return scaled


def load_root_config(path: str = "config.json") -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:
        return {}
    return apply_capital_scaling(raw)
