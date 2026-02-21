# file: strategy/ml_sniper/config_loader.py

import json

def load_sniper_config():
    try:
        with open("config.json", "r") as f:
            cfg = json.load(f)
            return cfg.get("strategy", {}).get("ml_sniper", {})
    except:
        return {}