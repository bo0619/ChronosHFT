# file: strategy/ml_sniper/config_loader.py

import json


def load_sniper_config():
    try:
        with open("config.json", "r", encoding="utf-8") as handle:
            cfg = json.load(handle)
    except Exception:
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