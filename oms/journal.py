import json
import hashlib
import os
import threading
from datetime import datetime
from enum import Enum


class JournalError(RuntimeError):
    """Base class for durable OMS journal failures."""


class JournalCorruptionError(JournalError):
    """Raised when the persisted journal is not a valid hash chain."""


class JournalWriteError(JournalError):
    """Raised when a journal record cannot be made durable."""


def _reject_nonstandard_json_constant(value: str):
    raise ValueError(f"non-standard numeric constant {value}")


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
    """Append-only, crash-detectable journal for OMS commands and state."""

    RECORD_VERSION = 2

    def __init__(self, config: dict):
        oms_conf = config.get("oms", {})
        self.enabled = oms_conf.get("journal_enabled", True)
        self.replay_on_startup = oms_conf.get("replay_journal_on_startup", True)
        self.fsync_enabled = bool(oms_conf.get("journal_fsync", True))
        self.integrity_check_enabled = bool(
            oms_conf.get("journal_integrity_check", True)
        )
        self.path = oms_conf.get(
            "journal_path",
            os.path.join("storage", "oms", "oms_journal.jsonl"),
        )
        self.lock = threading.RLock()
        self._next_seq = 1
        self._last_hash = ""

        if self.enabled:
            parent = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(parent, exist_ok=True)
            self._initialize_tail()

    @staticmethod
    def _canonical_json(value: dict) -> str:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def _calculate_hash(cls, record_without_hash: dict) -> str:
        encoded = cls._canonical_json(record_without_hash).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _initialize_tail(self):
        if not os.path.exists(self.path):
            return
        records = self._read_records()
        for record in reversed(records):
            if record.get("version") == self.RECORD_VERSION:
                self._next_seq = int(record["seq"]) + 1
                self._last_hash = str(record["hash"])
                return
        # Legacy records did not have sequence numbers. Starting after their
        # physical count keeps the first durable sequence monotonic while still
        # allowing an in-place upgrade.
        self._next_seq = len(records) + 1

    def append(self, kind: str, payload: dict):
        committed = self.append_batch(((kind, payload),))
        return committed[0] if committed else 0

    def append_batch(self, records) -> list[int]:
        """Append related records under one hash-chain lock and one fsync."""
        if not self.enabled:
            return [0 for _item in records]

        records = list(records)
        if not records:
            return []

        with self.lock:
            next_seq = self._next_seq
            last_hash = self._last_hash
            lines = []
            committed_sequences = []
            try:
                for kind, payload in records:
                    unsigned_record = {
                        "version": self.RECORD_VERSION,
                        "seq": next_seq,
                        "ts": (
                            datetime.utcnow().isoformat(
                                timespec="milliseconds"
                            )
                            + "Z"
                        ),
                        "kind": str(kind),
                        "payload": _normalize(payload),
                        "prev_hash": last_hash,
                    }
                    record_hash = self._calculate_hash(unsigned_record)
                    record = dict(unsigned_record)
                    record["hash"] = record_hash
                    lines.append(self._canonical_json(record))
                    committed_sequences.append(next_seq)
                    next_seq += 1
                    last_hash = record_hash
            except (TypeError, ValueError) as exc:
                raise JournalWriteError(
                    "OMS journal batch payload is not serializable: "
                    f"{exc}"
                ) from exc

            try:
                with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                    f.write("\n".join(lines) + "\n")
                    f.flush()
                    if self.fsync_enabled:
                        os.fsync(f.fileno())
            except OSError as exc:
                raise JournalWriteError(
                    "Failed to persist OMS journal batch "
                    f"seq={self._next_seq}..{next_seq - 1}: {exc}"
                ) from exc

            self._next_seq = next_seq
            self._last_hash = last_hash
            return committed_sequences

    def load(self):
        if not self.enabled or not self.replay_on_startup or not os.path.exists(self.path):
            return []

        with self.lock:
            return self._read_records()

    def _read_records(self):
        records = []
        expected_seq = None
        previous_hash = ""
        durable_chain_started = False

        try:
            with open(self.path, "r", encoding="utf-8-sig") as f:
                for line_number, raw in enumerate(f, start=1):
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    try:
                        record = json.loads(
                            stripped,
                            parse_constant=_reject_nonstandard_json_constant,
                        )
                    except (json.JSONDecodeError, ValueError) as exc:
                        raise JournalCorruptionError(
                            f"Invalid JSON in OMS journal at line {line_number}: {exc}"
                        ) from exc

                    is_durable_record = record.get("version") == self.RECORD_VERSION
                    if not is_durable_record:
                        if durable_chain_started and self.integrity_check_enabled:
                            raise JournalCorruptionError(
                                f"Legacy record after durable chain at line {line_number}"
                            )
                        records.append(record)
                        continue

                    durable_chain_started = True
                    try:
                        seq = int(record["seq"])
                        stored_hash = str(record["hash"])
                        stored_prev_hash = str(record.get("prev_hash", ""))
                    except (KeyError, TypeError, ValueError) as exc:
                        raise JournalCorruptionError(
                            f"Malformed durable OMS journal record at line {line_number}"
                        ) from exc

                    if expected_seq is None:
                        expected_seq = seq
                    if self.integrity_check_enabled and seq != expected_seq:
                        raise JournalCorruptionError(
                            f"OMS journal sequence gap at line {line_number}: "
                            f"expected {expected_seq}, got {seq}"
                        )
                    if self.integrity_check_enabled and stored_prev_hash != previous_hash:
                        raise JournalCorruptionError(
                            f"OMS journal hash-chain mismatch at line {line_number}"
                        )

                    unsigned_record = dict(record)
                    unsigned_record.pop("hash", None)
                    calculated_hash = self._calculate_hash(unsigned_record)
                    if self.integrity_check_enabled and calculated_hash != stored_hash:
                        raise JournalCorruptionError(
                            f"OMS journal record hash mismatch at line {line_number}"
                        )

                    records.append(record)
                    expected_seq = seq + 1
                    previous_hash = stored_hash
        except OSError as exc:
            raise JournalError(f"Failed to read OMS journal: {exc}") from exc

        return records

    def health_snapshot(self):
        return {
            "enabled": self.enabled,
            "path": os.path.abspath(self.path),
            "fsync_enabled": self.fsync_enabled,
            "integrity_check_enabled": self.integrity_check_enabled,
            "next_seq": self._next_seq,
            "last_hash": self._last_hash,
        }
