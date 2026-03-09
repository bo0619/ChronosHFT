import json
import os
import threading
from datetime import datetime
from enum import Enum


def _normalize(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    return value


class OMSJournal:
    """Append-only audit journal for OMS decisions and state transitions."""

    def __init__(self, config: dict):
        oms_conf = config.get("oms", {})
        self.enabled = oms_conf.get("journal_enabled", True)
        self.replay_on_startup = oms_conf.get("replay_journal_on_startup", True)
        self.path = oms_conf.get(
            "journal_path",
            os.path.join("storage", "oms", "oms_journal.jsonl"),
        )
        self.lock = threading.RLock()

        if self.enabled:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def append(self, kind: str, payload: dict):
        if not self.enabled:
            return

        record = {
            "ts": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
            "kind": kind,
            "payload": _normalize(payload),
        }

        line = json.dumps(record, ensure_ascii=True)
        with self.lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def load(self):
        if not self.enabled or not self.replay_on_startup or not os.path.exists(self.path):
            return []

        records = []
        with self.lock:
            with open(self.path, "r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        records.append(json.loads(raw))
                    except json.JSONDecodeError:
                        continue
        return records
