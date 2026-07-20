import json
import os
from copy import deepcopy


QUOTE_ASSET_SUFFIXES = ("USDT", "USDC", "BUSD", "FDUSD")
MARKET_DATA_FRESHNESS_DEFAULTS = {
    "enabled": True,
    "require_mark_price": True,
    "require_book": True,
    "max_mark_age_ms": 3000.0,
    "max_book_age_ms": 1500.0,
    "poll_interval_sec": 0.25,
    "breach_checks": 2,
    "recovery_checks": 5,
}
CASH_FLOW_TRUTH_DEFAULTS = {
    "enabled": True,
    "require_snapshot": True,
    "max_snapshot_age_sec": 45.0,
    "poll_interval_sec": 30.0,
    "recovery_checks": 2,
    "external_income_types": ["TRANSFER"],
}
RISK_CONTROL_HEARTBEAT_DEFAULTS = {
    "enabled": True,
    "max_age_sec": 2.0,
    "required_source": "independent_supervisor",
}
INDEPENDENT_RISK_SUPERVISOR_DEFAULTS = {
    "enabled": True,
    "api_key_env": "BINANCE_RISK_API_KEY",
    "api_secret_env": "BINANCE_RISK_API_SECRET",
    "heartbeat_interval_sec": 0.25,
    "parent_heartbeat_timeout_sec": 1.5,
    "status_max_age_sec": 2.0,
    "exchange_poll_interval_sec": 5.0,
    "exchange_max_age_sec": 10.0,
    "cancel_retry_sec": 2.0,
    "orphan_exit_sec": 30.0,
    "stop_timeout_sec": 10.0,
    "emergency_countdown_time_ms": 1000,
    "recovery_checks": 2,
    "gross_kill_multiplier": 1.25,
    "flatten_enabled": True,
    "parent_loss_flatten_delay_sec": 3.0,
    "flatten_retry_sec": 2.0,
    "flat_verification_checks": 2,
    "state_path": "storage/risk/independent_supervisor_state.json",
    "state_required": True,
    "state_fsync": True,
    "rearm_prepare_ttl_sec": 10.0,
    "rearm_command_timeout_sec": 5.0,
    "daily_loss_enabled": True,
    "daily_loss_reduce_only_fraction": 0.80,
    "cash_flow_income_types": ["TRANSFER"],
    "cash_flow_assets": ["USDT", "USDC", "BUSD", "FDUSD"],
    "cash_flow_max_pages": 5,
    "clock_sync_enabled": True,
    "clock_sync_interval_sec": 30.0,
    "clock_reduce_only_offset_ms": 250.0,
    "clock_kill_offset_ms": 1000.0,
    "clock_max_rtt_ms": 1500.0,
    "liquidation_proximity_enabled": True,
    "require_liquidation_price": True,
    "liquidation_reduce_only_distance_pct": 0.05,
    "liquidation_kill_distance_pct": 0.02,
}
OUTBOUND_MESSAGE_BUDGET_DEFAULTS = {
    "enabled": True,
    "window_sec": 1.0,
    "max_total_messages_per_window": 20,
    "max_new_orders_per_window": 10,
    "max_reduce_orders_per_window": 10,
    "max_cancel_messages_per_window": 20,
    "reserved_risk_messages_per_window": 5,
}
SELF_TRADE_PREVENTION_DEFAULTS = {
    "enabled": True,
    "local_cross_check": True,
    "exchange_mode": "EXPIRE_MAKER",
}
SINGLE_WRITER_FENCE_DEFAULTS = {
    "enabled": True,
}
STRATEGY_RISK_BUDGET_DEFAULTS = {
    "enabled": True,
    "require_explicit_strategy": True,
    "budgets": {},
}
VENUE_DEAD_MAN_SWITCH_DEFAULTS = {
    "enabled": True,
    "countdown_time_ms": 120_000,
    "renewal_interval_sec": 30.0,
    "max_renewal_age_sec": 45.0,
    "recovery_checks": 2,
}


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


def _extract_quote_asset(symbol: str) -> str:
    symbol = str(symbol or "").upper()
    for suffix in QUOTE_ASSET_SUFFIXES:
        if symbol.endswith(suffix):
            return suffix
    return ""


def _tracked_quote_assets(symbols) -> list[str]:
    assets = []
    for symbol in symbols or []:
        asset = _extract_quote_asset(symbol)
        if asset and asset not in assets:
            assets.append(asset)
    return assets


def _normalize_budget_weights(raw_weights, quote_assets) -> dict[str, float]:
    if not isinstance(raw_weights, dict):
        return {}

    normalized = {}
    for asset, weight in raw_weights.items():
        asset_key = str(asset or "").upper()
        if quote_assets and asset_key not in quote_assets:
            continue
        parsed = _to_float(weight, 0.0)
        if parsed > 0.0:
            normalized[asset_key] = parsed
    return normalized


def _derive_budget_by_asset(total_budget: float, quote_assets, raw_weights=None) -> dict[str, float]:
    assets = list(quote_assets or [])
    weights = _normalize_budget_weights(raw_weights, assets)
    if not assets:
        assets = list(weights.keys()) or ["USDT"]
        if weights:
            weights = _normalize_budget_weights(weights, assets)

    if not weights:
        equal_weight = 1.0 / max(1, len(assets))
        weights = {asset: equal_weight for asset in assets}
    else:
        weight_sum = sum(weights.values())
        if weight_sum <= 0.0:
            equal_weight = 1.0 / max(1, len(assets))
            weights = {asset: equal_weight for asset in assets}
        else:
            weights = {asset: value / weight_sum for asset, value in weights.items()}

    allocation = {}
    residual = max(0.0, float(total_budget))
    for index, asset in enumerate(assets):
        if index == len(assets) - 1:
            allocation[asset] = round(residual, 8)
            break
        asset_budget = round(total_budget * weights.get(asset, 0.0), 8)
        residual = round(max(0.0, residual - asset_budget), 8)
        allocation[asset] = asset_budget
    return allocation


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
    order_notional_limit_factor = max(
        1.0,
        _to_float(
            scaling.get("order_notional_limit_factor", 1.0),
            1.0,
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
    quote_assets = _tracked_quote_assets(symbols)
    budget_weights = scaling.get("budget_asset_weights")
    if not isinstance(budget_weights, dict):
        budget_weights = account.get("trading_budget_by_asset", {})

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
    derived_budget_by_asset = _derive_budget_by_asset(
        derived_capital,
        quote_assets,
        raw_weights=budget_weights,
    )

    strategy["capital_multiplier"] = round(capital_multiplier, 8)
    account["initial_balance_usdt"] = round(derived_capital, 8)
    account["trading_budget_total"] = round(derived_capital, 8)
    account["trading_budget_by_asset"] = {
        asset: round(value, 8)
        for asset, value in derived_budget_by_asset.items()
        if value > 0.0
    }
    backtest["initial_capital"] = round(derived_capital, 8)
    limits["max_order_notional"] = round(
        derived_order_notional * order_notional_limit_factor,
        8,
    )
    limits["max_pos_notional"] = round(derived_symbol_cap, 8)
    limits["max_account_gross_notional"] = round(derived_total_risk_notional, 8)
    limits["max_daily_loss"] = round(derived_daily_loss, 8)
    limits["max_order_qty"] = round(derived_max_order_qty, 8)
    strategy["lot_multiplier"] = round(max(0.01, derived_lot_multiplier), 8)
    strategy["max_pos_usdt"] = round(derived_symbol_cap, 8)

    return scaled


def apply_production_safety_defaults(config: dict) -> dict:
    if not isinstance(config, dict):
        return {}

    configured = deepcopy(config)
    oms = configured.get("oms")
    if not isinstance(oms, dict):
        oms = {}
        configured["oms"] = oms

    outbound_budget = oms.get("outbound_message_budget")
    if not isinstance(outbound_budget, dict):
        outbound_budget = {}
        oms["outbound_message_budget"] = outbound_budget

    for key, value in OUTBOUND_MESSAGE_BUDGET_DEFAULTS.items():
        outbound_budget.setdefault(key, value)

    self_trade_prevention = oms.get("self_trade_prevention")
    if not isinstance(self_trade_prevention, dict):
        self_trade_prevention = {}
        oms["self_trade_prevention"] = self_trade_prevention

    for key, value in SELF_TRADE_PREVENTION_DEFAULTS.items():
        self_trade_prevention.setdefault(key, value)

    single_writer_fence = oms.get("single_writer_fence")
    if not isinstance(single_writer_fence, dict):
        single_writer_fence = {}
        oms["single_writer_fence"] = single_writer_fence

    for key, value in SINGLE_WRITER_FENCE_DEFAULTS.items():
        single_writer_fence.setdefault(key, value)

    venue_dead_man_switch = oms.get("venue_dead_man_switch")
    if not isinstance(venue_dead_man_switch, dict):
        venue_dead_man_switch = {}
        oms["venue_dead_man_switch"] = venue_dead_man_switch

    for key, value in VENUE_DEAD_MAN_SWITCH_DEFAULTS.items():
        venue_dead_man_switch.setdefault(key, value)

    risk = configured.get("risk")
    if not isinstance(risk, dict):
        risk = {}
        configured["risk"] = risk

    freshness = risk.get("market_data_freshness")
    if not isinstance(freshness, dict):
        freshness = {}
        risk["market_data_freshness"] = freshness

    for key, value in MARKET_DATA_FRESHNESS_DEFAULTS.items():
        freshness.setdefault(key, value)

    cash_flow_truth = risk.get("cash_flow_truth")
    if not isinstance(cash_flow_truth, dict):
        cash_flow_truth = {}
        risk["cash_flow_truth"] = cash_flow_truth

    for key, value in CASH_FLOW_TRUTH_DEFAULTS.items():
        cash_flow_truth.setdefault(key, deepcopy(value))

    risk_control_heartbeat = risk.get("risk_control_heartbeat")
    if not isinstance(risk_control_heartbeat, dict):
        risk_control_heartbeat = {}
        risk["risk_control_heartbeat"] = risk_control_heartbeat

    heartbeat_source_explicit = "required_source" in risk_control_heartbeat
    for key, value in RISK_CONTROL_HEARTBEAT_DEFAULTS.items():
        risk_control_heartbeat.setdefault(key, value)

    strategy_risk_budgets = risk.get("strategy_risk_budgets")
    if not isinstance(strategy_risk_budgets, dict):
        strategy_risk_budgets = {}
        risk["strategy_risk_budgets"] = strategy_risk_budgets

    for key, value in STRATEGY_RISK_BUDGET_DEFAULTS.items():
        strategy_risk_budgets.setdefault(key, deepcopy(value))

    independent_supervisor = risk.get("independent_supervisor")
    if not isinstance(independent_supervisor, dict):
        independent_supervisor = {}
        risk["independent_supervisor"] = independent_supervisor

    for key, value in INDEPENDENT_RISK_SUPERVISOR_DEFAULTS.items():
        independent_supervisor.setdefault(key, value)

    if not heartbeat_source_explicit:
        risk_control_heartbeat["required_source"] = (
            "independent_supervisor"
            if bool(independent_supervisor.get("enabled", True))
            else "risk_manager"
        )
    return configured


def resolve_runtime_secrets(config: dict) -> dict:
    if not isinstance(config, dict):
        return {}

    resolved = deepcopy(config)
    for field, default_env_name in (
        ("api_key", "BINANCE_API_KEY"),
        ("api_secret", "BINANCE_API_SECRET"),
    ):
        env_field = f"{field}_env"
        env_name = str(resolved.get(env_field, default_env_name) or "").strip()
        resolved[env_field] = env_name
        env_value = os.environ.get(env_name, "") if env_name else ""
        if env_value:
            resolved[field] = env_value
        else:
            resolved[field] = str(resolved.get(field, "") or "")

    risk = resolved.get("risk", {})
    if isinstance(risk, dict):
        supervisor = risk.get("independent_supervisor", {})
        if isinstance(supervisor, dict):
            for field, default_env_name in (
                ("api_key", "BINANCE_RISK_API_KEY"),
                ("api_secret", "BINANCE_RISK_API_SECRET"),
            ):
                env_field = f"{field}_env"
                env_name = str(
                    supervisor.get(env_field, default_env_name) or ""
                ).strip()
                supervisor[env_field] = env_name
                env_value = os.environ.get(env_name, "") if env_name else ""
                if env_value:
                    supervisor[field] = env_value
    return resolved


def finalize_strategy_risk_budgets(config: dict) -> dict:
    if not isinstance(config, dict):
        return {}
    configured = deepcopy(config)
    risk = configured.get("risk", {})
    if not isinstance(risk, dict):
        return configured
    budget_config = risk.get("strategy_risk_budgets", {})
    if not isinstance(budget_config, dict) or not bool(
        budget_config.get("enabled", False)
    ):
        return configured

    budgets = budget_config.get("budgets")
    if not isinstance(budgets, dict):
        budgets = {}
        budget_config["budgets"] = budgets
    strategy_id = str(
        (configured.get("strategy", {}) or {}).get("name", "") or ""
    ).strip()
    if not strategy_id:
        return configured

    limits = risk.get("limits", {}) or {}
    max_symbol_notional = max(
        0.0,
        _to_float(limits.get("max_pos_notional", 0.0), 0.0),
    )
    max_gross_notional = max(
        max_symbol_notional,
        _to_float(
            limits.get("max_account_gross_notional", max_symbol_notional),
            max_symbol_notional,
        ),
    )
    strategy_budget = budgets.get(strategy_id)
    if not isinstance(strategy_budget, dict):
        strategy_budget = {"auto_scale": True}
        budgets[strategy_id] = strategy_budget
    if bool(strategy_budget.get("auto_scale", False)):
        strategy_budget["max_gross_notional"] = max_gross_notional
        strategy_budget["max_symbol_notional"] = max_symbol_notional
    else:
        strategy_budget.setdefault("max_gross_notional", max_gross_notional)
        strategy_budget.setdefault("max_symbol_notional", max_symbol_notional)
    return configured


def load_root_config(path: str = "config.json") -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:
        return {}
    configured = apply_capital_scaling(apply_production_safety_defaults(raw))
    configured = finalize_strategy_risk_budgets(configured)
    return resolve_runtime_secrets(configured)
