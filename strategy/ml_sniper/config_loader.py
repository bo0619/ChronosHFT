# file: strategy/ml_sniper/config_loader.py

from infrastructure.config_scaling import load_root_config


def load_sniper_config():
    cfg = load_root_config("config.json")
    if not cfg:
        return {}

    strategy_cfg = cfg.get("strategy", {})
    sniper_cfg = dict(strategy_cfg.get("ml_sniper", {}))
    inherited = {
        key: value
        for key, value in strategy_cfg.items()
        if key != "ml_sniper"
    }
    inherited.update(sniper_cfg)
    return inherited
