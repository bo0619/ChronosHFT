import json
import hashlib
import math
import os
import shutil
import threading
import time
from collections import deque
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
        raw_min_free_bytes = oms_conf.get("journal_min_free_bytes", 0)
        if (
            isinstance(raw_min_free_bytes, bool)
            or not isinstance(raw_min_free_bytes, int)
            or raw_min_free_bytes < 0
        ):
            raise ValueError(
                "oms.journal_min_free_bytes must be a non-negative integer"
            )
        self.min_free_bytes = raw_min_free_bytes
        raw_space_check_interval = oms_conf.get(
            "journal_space_check_interval_sec",
            1.0,
        )
        if isinstance(raw_space_check_interval, bool):
            raise ValueError(
                "oms.journal_space_check_interval_sec must be positive"
            )
        try:
            self.space_check_interval_sec = float(raw_space_check_interval)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "oms.journal_space_check_interval_sec must be positive"
            ) from exc
        if (
            not math.isfinite(self.space_check_interval_sec)
            or self.space_check_interval_sec <= 0.0
        ):
            raise ValueError(
                "oms.journal_space_check_interval_sec must be positive"
            )
        self.lock = threading.RLock()
        self._next_seq = 1
        self._last_hash = ""
        self._recent_commits = {}
        self._recent_commit_order = deque()
        self._disk_free_bytes = None
        self._last_space_check_monotonic = 0.0
        self._last_space_check_at = 0.0
        self._space_check_failure_count = 0
        self._space_rejection_count = 0
        self._write_failure_count = 0
        self._last_space_error = ""

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
        physical_count = 0
        last_durable_record = None
        for record in self._iter_records_unlocked():
            physical_count += 1
            if record.get("version") == self.RECORD_VERSION:
                last_durable_record = record
        if last_durable_record is not None:
            self._next_seq = int(last_durable_record["seq"]) + 1
            self._last_hash = str(last_durable_record["hash"])
            return
        # Legacy records did not have sequence numbers. Starting after their
        # physical count keeps the first durable sequence monotonic while still
        # allowing an in-place upgrade.
        self._next_seq = physical_count + 1

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
            committed_metadata = []
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
                    committed_metadata.append(
                        {
                            "seq": next_seq,
                            "ts": unsigned_record["ts"],
                            "hash": record_hash,
                        }
                    )
                    next_seq += 1
                    last_hash = record_hash
            except (TypeError, ValueError) as exc:
                raise JournalWriteError(
                    "OMS journal batch payload is not serializable: "
                    f"{exc}"
                ) from exc

            batch_text = "\n".join(lines) + "\n"
            encoded_batch = batch_text.encode("utf-8")
            self._ensure_disk_space(len(encoded_batch))
            try:
                with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                    f.write(batch_text)
                    f.flush()
                    if self.fsync_enabled:
                        os.fsync(f.fileno())
            except OSError as exc:
                self._write_failure_count += 1
                self._last_space_error = (
                    f"write_failed:{type(exc).__name__}:{exc}"
                )
                raise JournalWriteError(
                    "Failed to persist OMS journal batch "
                    f"seq={self._next_seq}..{next_seq - 1}: {exc}"
                ) from exc

            if self._disk_free_bytes is not None:
                self._disk_free_bytes = max(
                    0,
                    self._disk_free_bytes - len(encoded_batch),
                )

            self._next_seq = next_seq
            self._last_hash = last_hash
            for metadata in committed_metadata:
                sequence = int(metadata["seq"])
                self._recent_commits[sequence] = metadata
                self._recent_commit_order.append(sequence)
                if len(self._recent_commit_order) > 4096:
                    expired = self._recent_commit_order.popleft()
                    self._recent_commits.pop(expired, None)
            return committed_sequences

    def _ensure_disk_space(self, required_bytes: int) -> None:
        if self.min_free_bytes <= 0:
            return

        required_bytes = max(0, int(required_bytes))
        now_monotonic = time.monotonic()
        refresh_required = (
            self._disk_free_bytes is None
            or now_monotonic - self._last_space_check_monotonic
            >= self.space_check_interval_sec
            or self._disk_free_bytes < self.min_free_bytes + required_bytes
        )
        if refresh_required:
            parent = os.path.dirname(os.path.abspath(self.path))
            try:
                free_bytes = int(shutil.disk_usage(parent).free)
            except Exception as exc:
                self._space_check_failure_count += 1
                self._last_space_error = (
                    f"space_check_failed:{type(exc).__name__}:{exc}"
                )
                raise JournalWriteError(
                    "Failed to verify free space for the OMS journal: "
                    f"{type(exc).__name__}:{exc}"
                ) from exc
            self._disk_free_bytes = free_bytes
            self._last_space_check_monotonic = now_monotonic
            self._last_space_check_at = time.time()
            self._last_space_error = ""

        free_after_write = self._disk_free_bytes - required_bytes
        if free_after_write < self.min_free_bytes:
            self._space_rejection_count += 1
            self._last_space_error = (
                "insufficient_space:"
                f"free={self._disk_free_bytes}:"
                f"required={required_bytes}:"
                f"reserve={self.min_free_bytes}"
            )
            raise JournalWriteError(
                "OMS journal write rejected because disk free space would fall "
                f"below the reserve: free={self._disk_free_bytes} "
                f"batch={required_bytes} reserve={self.min_free_bytes}"
            )

    def commit_metadata(self, sequence: int):
        """Return immutable metadata for one recent in-process commit."""
        with self.lock:
            metadata = self._recent_commits.get(int(sequence))
            return dict(metadata) if metadata is not None else None

    def load(self):
        return list(self.iter_records(respect_replay_policy=True))

    def read_all(self):
        """Read and verify every record regardless of OMS replay policy."""
        return list(self.iter_records())

    def iter_records(self, *, respect_replay_policy: bool = False):
        """Yield verified records without retaining the complete journal."""
        if (
            not self.enabled
            or not os.path.exists(self.path)
            or (respect_replay_policy and not self.replay_on_startup)
        ):
            return
        with self.lock:
            yield from self._iter_records_unlocked()

    def _read_records(self):
        return list(self._iter_records_unlocked())

    def _iter_records_unlocked(self):
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
                        yield record
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

                    yield record
                    expected_seq = seq + 1
                    previous_hash = stored_hash
        except OSError as exc:
            raise JournalError(f"Failed to read OMS journal: {exc}") from exc

    def health_snapshot(self):
        with self.lock:
            return {
                "enabled": self.enabled,
                "path": os.path.abspath(self.path),
                "fsync_enabled": self.fsync_enabled,
                "integrity_check_enabled": self.integrity_check_enabled,
                "next_seq": self._next_seq,
                "last_hash": self._last_hash,
                "min_free_bytes": self.min_free_bytes,
                "disk_free_bytes": self._disk_free_bytes,
                "last_space_check_at": self._last_space_check_at,
                "space_check_failure_count": (
                    self._space_check_failure_count
                ),
                "space_rejection_count": self._space_rejection_count,
                "write_failure_count": self._write_failure_count,
                "last_space_error": self._last_space_error,
            }
