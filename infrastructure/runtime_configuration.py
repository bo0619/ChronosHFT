"""Small presentation helpers for resolved runtime configuration."""


def print_config_summary(config: dict, *, paper_trade: bool) -> None:
    strategy = config.get("strategy", {}) or {}
    account = config.get("account", {}) or {}
    limits = (config.get("risk", {}) or {}).get("limits", {}) or {}
    mode = "paper" if paper_trade else "live"
    print(
        "CONFIG_OK "
        f"mode={mode} "
        f"symbols={len(config.get('symbols', []) or [])} "
        f"primary_model={strategy.get('primary_model', '')} "
        "capital_usdt="
        f"{float(account.get('trading_budget_total', 0.0)):g} "
        "order_notional_usdt="
        f"{float(strategy.get('target_order_notional', 0.0)):g} "
        "max_position_usdt="
        f"{float(limits.get('max_pos_notional', 0.0)):g} "
        "max_gross_usdt="
        f"{float(limits.get('max_account_gross_notional', 0.0)):g} "
        "max_daily_loss_usdt="
        f"{float(limits.get('max_daily_loss', 0.0)):g}",
        flush=True,
    )


__all__ = ["print_config_summary"]
