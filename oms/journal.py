"""Versioned, segmented durable journal for OMS state and commands.

Version 3 deliberately has no runtime compatibility reader. Legacy JSONL and
version 2 journals are accepted only by the offline migration helpers near the
bottom of this module.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import struct
import threading
import time
import uuid
import zlib
from collections import deque
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable, Iterator

from infrastructure.durability import DurabilityError


class JournalError(DurabilityError):
    """Base class for durable OMS journal failures."""


class JournalCorruptionError(JournalError):
    """Raised when persisted journal state cannot be proven consistent."""


class JournalWriteError(JournalError):
    """Raised when a journal record cannot be made durable."""


class JournalMigrationRequiredError(JournalError):
    """Raised when runtime encounters a journal that needs offline migration."""


def _reject_nonstandard_json_constant(value: str):
    raise ValueError(f"non-standard numeric constant {value}")


def _normalize(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    return value


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class OMSJournal:
    """Append-only v3 journal with framed segments and bounded tail recovery."""

    FORMAT_VERSION = 3
    RECORD_VERSION = FORMAT_VERSION
    EVENT_RECORD_VERSION = 1
    SEGMENT_HEADER_VERSION = 1
    CHECKPOINT_VERSION = 1
    CHECKPOINT_POINTER_VERSION = 1
    MANIFEST_SCHEMA = "chronoshft.oms-journal-manifest"
    RECORD_KIND = "oms_event"
    PAYLOAD_SCHEMA_PREFIX = "chronoshft.oms.event."
    SEGMENT_MAGIC = b"CHRONOSHFT-OMS-JOURNAL-V3\n"
    FRAME_LENGTH = struct.Struct(">I")
    FRAME_CRC = struct.Struct(">I")
    DEFAULT_MAX_FRAME_BYTES = 64 * 1024 * 1024

    def __init__(self, config: dict):
        oms_conf = config.get("oms", {})
        self.enabled = bool(oms_conf.get("journal_enabled", True))
        self.replay_on_startup = bool(
            oms_conf.get("replay_journal_on_startup", True)
        )
        self.fsync_enabled = bool(oms_conf.get("journal_fsync", True))
        self.integrity_check_enabled = bool(
            oms_conf.get("journal_integrity_check", True)
        )
        configured_format = oms_conf.get(
            "journal_format_version",
            self.FORMAT_VERSION,
        )
        if (
            isinstance(configured_format, bool)
            or not isinstance(configured_format, int)
            or configured_format != self.FORMAT_VERSION
        ):
            raise JournalMigrationRequiredError(
                "OMS runtime only accepts journal format v3; run the offline "
                "migration tool before startup"
            )

        self.path = str(
            oms_conf.get(
                "journal_path",
                os.path.join("storage", "oms", "oms_journal"),
            )
        )
        self.manifest_path = Path(f"{self.path}.v3-manifest.json").resolve()
        self.segment_directory = Path(f"{self.path}.v3-segments").resolve()
        self.checkpoint_directory = Path(
            f"{self.path}.v3-checkpoints"
        ).resolve()
        self.checkpoint_head_path = Path(
            f"{self.path}.v3-checkpoint-head.json"
        ).resolve()
        self.require_existing = bool(
            oms_conf.get("journal_require_existing", False)
        )
        self.segment_max_records = self._positive_integer(
            oms_conf.get("journal_segment_max_records", 100_000),
            "oms.journal_segment_max_records",
        )
        self.segment_max_bytes = self._positive_integer(
            oms_conf.get("journal_segment_max_bytes", 256 * 1024 * 1024),
            "oms.journal_segment_max_bytes",
        )
        self.max_frame_bytes = self._positive_integer(
            oms_conf.get(
                "journal_max_frame_bytes",
                self.DEFAULT_MAX_FRAME_BYTES,
            ),
            "oms.journal_max_frame_bytes",
        )
        self.min_free_bytes = self._non_negative_integer(
            oms_conf.get("journal_min_free_bytes", 0),
            "oms.journal_min_free_bytes",
        )
        self.space_check_interval_sec = self._positive_float(
            oms_conf.get("journal_space_check_interval_sec", 1.0),
            "oms.journal_space_check_interval_sec",
        )

        self.lock = threading.RLock()
        self.journal_id = ""
        self._next_seq = 1
        self._last_hash = ""
        self._active_segment_index = 0
        self._active_segment_record_count = 0
        self._active_segment_size = 0
        self._segments: list[dict] = []
        self._recent_commits: dict[int, dict] = {}
        self._recent_commit_order = deque()
        self._checkpoint: dict | None = None
        self._checkpoint_marker_seq = 0
        self._checkpoint_marker_hash = ""
        self._verified_start_seq = 1
        self._disk_free_bytes = None
        self._last_space_check_monotonic = 0.0
        self._last_space_check_at = 0.0
        self._space_check_failure_count = 0
        self._space_rejection_count = 0
        self._write_failure_count = 0
        self._last_space_error = ""

        if self.enabled:
            self._open_v3_storage()

    @staticmethod
    def _positive_integer(value, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{field} must be a positive integer")
        return int(value)

    @staticmethod
    def _non_negative_integer(value, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field} must be a non-negative integer")
        return int(value)

    @staticmethod
    def _positive_float(value, field: str) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{field} must be positive")
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be positive") from exc
        if not math.isfinite(parsed) or parsed <= 0.0:
            raise ValueError(f"{field} must be positive")
        return parsed

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
    def _canonical_bytes(cls, value: dict) -> bytes:
        return cls._canonical_json(value).encode("utf-8")

    @classmethod
    def _calculate_hash(cls, record_without_hash: dict) -> str:
        return hashlib.sha256(cls._canonical_bytes(record_without_hash)).hexdigest()

    @classmethod
    def _document_digest(cls, document: dict, digest_field: str) -> str:
        unsigned = dict(document)
        unsigned.pop(digest_field, None)
        return hashlib.sha256(cls._canonical_bytes(unsigned)).hexdigest()

    @staticmethod
    def _atomic_write(path: Path, payload: bytes, *, fsync: bool) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(temporary, "xb") as handle:
                handle.write(payload)
                handle.flush()
                if fsync:
                    os.fsync(handle.fileno())
            os.replace(temporary, path)
            if fsync and os.name != "nt":
                descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @classmethod
    def _load_json_document(cls, path: Path, label: str) -> dict:
        try:
            raw = path.read_text(encoding="utf-8-sig")
            document = json.loads(
                raw,
                parse_constant=_reject_nonstandard_json_constant,
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise JournalCorruptionError(f"Invalid {label}: {exc}") from exc
        if not isinstance(document, dict):
            raise JournalCorruptionError(f"Invalid {label}: expected object")
        return document

    def _open_v3_storage(self) -> None:
        legacy_path = Path(self.path).resolve()
        if not self.manifest_path.exists():
            residual_v3 = (
                self.segment_directory.exists()
                or self.checkpoint_directory.exists()
                or self.checkpoint_head_path.exists()
            )
            if legacy_path.exists() or residual_v3:
                raise JournalMigrationRequiredError(
                    "OMS journal has no valid v3 manifest; legacy, partial, "
                    "and mixed layouts require explicit offline migration"
                )
            if self.require_existing:
                raise JournalMigrationRequiredError(
                    "Required OMS journal v3 durable state is missing"
                )
            self._create_manifest()

        manifest = self._load_json_document(
            self.manifest_path,
            "OMS journal v3 manifest",
        )
        self._validate_manifest(manifest)
        self.journal_id = str(manifest["journal_id"])
        self.segment_directory.mkdir(parents=True, exist_ok=True)
        self.checkpoint_directory.mkdir(parents=True, exist_ok=True)
        self._initialize_tail()

    def _create_manifest(self, *, journal_id: str | None = None) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_id = str(journal_id or uuid.uuid4())
        try:
            candidate_id = str(uuid.UUID(candidate_id))
        except ValueError as exc:
            raise ValueError("journal_id must be a UUID") from exc
        document = {
            "schema": self.MANIFEST_SCHEMA,
            "format_version": self.FORMAT_VERSION,
            "journal_id": candidate_id,
            "created_at_utc": _utc_now(),
            "segment_format": "length32be-json-crc32",
            "hash_algorithm": "sha256",
        }
        document["manifest_sha256"] = self._document_digest(
            document,
            "manifest_sha256",
        )
        try:
            with open(self.manifest_path, "xb") as handle:
                handle.write(self._canonical_bytes(document) + b"\n")
                handle.flush()
                if self.fsync_enabled:
                    os.fsync(handle.fileno())
        except FileExistsError:
            return
        except OSError as exc:
            raise JournalWriteError(
                f"Failed to create OMS journal v3 manifest: {exc}"
            ) from exc

    def _validate_manifest(self, manifest: dict) -> None:
        if manifest.get("schema") != self.MANIFEST_SCHEMA:
            raise JournalCorruptionError("Unknown OMS journal manifest schema")
        if manifest.get("format_version") != self.FORMAT_VERSION:
            raise JournalMigrationRequiredError(
                "OMS runtime only accepts journal format v3"
            )
        if manifest.get("segment_format") != "length32be-json-crc32":
            raise JournalCorruptionError("Unknown OMS journal segment format")
        if manifest.get("hash_algorithm") != "sha256":
            raise JournalCorruptionError("Unknown OMS journal hash algorithm")
        try:
            canonical_id = str(uuid.UUID(str(manifest.get("journal_id", ""))))
        except ValueError as exc:
            raise JournalCorruptionError("Invalid OMS journal_id") from exc
        if canonical_id != manifest.get("journal_id"):
            raise JournalCorruptionError("Non-canonical OMS journal_id")
        expected = self._document_digest(manifest, "manifest_sha256")
        if manifest.get("manifest_sha256") != expected:
            raise JournalCorruptionError("OMS journal manifest digest mismatch")

    def _segment_path(self, index: int) -> Path:
        return self.segment_directory / f"{int(index):020d}.seg"

    def _discover_segment_paths(self) -> list[Path]:
        paths = sorted(self.segment_directory.glob("*.seg"))
        for expected_index, path in enumerate(paths, start=1):
            if path.name != f"{expected_index:020d}.seg":
                raise JournalCorruptionError(
                    "OMS journal segment sequence is not contiguous"
                )
        return paths

    @classmethod
    def _encode_frame(cls, document: dict) -> bytes:
        payload = cls._canonical_bytes(document)
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        return cls.FRAME_LENGTH.pack(len(payload)) + payload + cls.FRAME_CRC.pack(crc)

    def _iter_frames(self, path: Path) -> Iterator[tuple[int, dict]]:
        try:
            with open(path, "rb") as handle:
                magic = handle.read(len(self.SEGMENT_MAGIC))
                if magic != self.SEGMENT_MAGIC:
                    raise JournalCorruptionError(
                        f"Invalid OMS journal segment magic: {path.name}"
                    )
                frame_index = 0
                while True:
                    length_bytes = handle.read(self.FRAME_LENGTH.size)
                    if not length_bytes:
                        return
                    if len(length_bytes) != self.FRAME_LENGTH.size:
                        raise JournalCorruptionError(
                            f"Torn frame length in {path.name}"
                        )
                    frame_length = self.FRAME_LENGTH.unpack(length_bytes)[0]
                    if frame_length <= 0 or frame_length > self.max_frame_bytes:
                        raise JournalCorruptionError(
                            f"Invalid frame length in {path.name}: {frame_length}"
                        )
                    payload = handle.read(frame_length)
                    crc_bytes = handle.read(self.FRAME_CRC.size)
                    if (
                        len(payload) != frame_length
                        or len(crc_bytes) != self.FRAME_CRC.size
                    ):
                        raise JournalCorruptionError(
                            f"Torn frame payload in {path.name}"
                        )
                    expected_crc = self.FRAME_CRC.unpack(crc_bytes)[0]
                    actual_crc = zlib.crc32(payload) & 0xFFFFFFFF
                    if expected_crc != actual_crc:
                        raise JournalCorruptionError(
                            f"OMS journal frame CRC mismatch in {path.name}"
                        )
                    try:
                        document = json.loads(
                            payload.decode("utf-8"),
                            parse_constant=_reject_nonstandard_json_constant,
                        )
                    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                        raise JournalCorruptionError(
                            f"Invalid framed JSON in {path.name}: {exc}"
                        ) from exc
                    if not isinstance(document, dict):
                        raise JournalCorruptionError(
                            f"Non-object frame in {path.name}"
                        )
                    frame_index += 1
                    yield frame_index, document
        except OSError as exc:
            raise JournalError(f"Failed to read OMS journal segment: {exc}") from exc

    def _validate_segment_header(
        self,
        header: dict,
        *,
        segment_index: int,
        previous_segment_hash: str | None,
    ) -> tuple[int, str]:
        expected = {
            "frame_kind": "segment_header",
            "header_version": self.SEGMENT_HEADER_VERSION,
            "journal_format_version": self.FORMAT_VERSION,
            "journal_id": self.journal_id,
            "segment_index": segment_index,
        }
        for field, value in expected.items():
            if header.get(field) != value:
                raise JournalCorruptionError(
                    f"Invalid segment header field {field} in segment {segment_index}"
                )
        start_seq = header.get("start_seq")
        if isinstance(start_seq, bool) or not isinstance(start_seq, int) or start_seq <= 0:
            raise JournalCorruptionError(
                f"Invalid start_seq in segment {segment_index}"
            )
        previous = str(header.get("previous_segment_hash", "") or "")
        if previous_segment_hash is not None and previous != previous_segment_hash:
            raise JournalCorruptionError(
                f"Segment hash linkage mismatch at segment {segment_index}"
            )
        allowed = {
            "frame_kind",
            "header_version",
            "journal_format_version",
            "journal_id",
            "segment_index",
            "start_seq",
            "previous_segment_hash",
            "created_at_utc",
        }
        if set(header) != allowed:
            raise JournalCorruptionError(
                f"Unknown segment header fields in segment {segment_index}"
            )
        return start_seq, previous

    def _validate_record(
        self,
        record: dict,
        *,
        expected_seq: int,
        previous_hash: str,
    ) -> tuple[int, str]:
        allowed = {
            "version",
            "journal_id",
            "record_kind",
            "record_version",
            "seq",
            "ts",
            "kind",
            "payload_schema",
            "payload_version",
            "payload",
            "prev_hash",
            "hash",
        }
        unknown = set(record).difference(allowed)
        missing = allowed.difference(record)
        if unknown or missing:
            raise JournalCorruptionError(
                "Malformed OMS journal v3 record: "
                f"unknown={sorted(unknown)!r} missing={sorted(missing)!r}"
            )
        if record.get("version") != self.FORMAT_VERSION:
            raise JournalCorruptionError(
                f"Unsupported OMS journal version: {record.get('version')!r}"
            )
        if record.get("journal_id") != self.journal_id:
            raise JournalCorruptionError("OMS journal record identity mismatch")
        if record.get("record_kind") != self.RECORD_KIND:
            raise JournalCorruptionError(
                f"Unknown OMS journal record kind: {record.get('record_kind')!r}"
            )
        if record.get("record_version") != self.EVENT_RECORD_VERSION:
            raise JournalCorruptionError(
                "Unsupported OMS journal event record version: "
                f"{record.get('record_version')!r}"
            )
        seq = record.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq != expected_seq:
            raise JournalCorruptionError(
                f"OMS journal sequence gap: expected {expected_seq}, got {seq!r}"
            )
        kind = record.get("kind")
        if not isinstance(kind, str) or not kind or len(kind) > 128:
            raise JournalCorruptionError("Invalid OMS journal event kind")
        expected_schema = f"{self.PAYLOAD_SCHEMA_PREFIX}{kind}"
        if record.get("payload_schema") != expected_schema:
            raise JournalCorruptionError(
                f"Unknown payload schema for OMS journal event {kind}"
            )
        if record.get("payload_version") != 1:
            raise JournalCorruptionError(
                f"Unsupported payload version for OMS journal event {kind}"
            )
        if not isinstance(record.get("payload"), dict):
            raise JournalCorruptionError("OMS journal payload must be an object")
        stored_hash = record.get("hash")
        stored_previous = record.get("prev_hash")
        if not isinstance(stored_hash, str) or len(stored_hash) != 64:
            raise JournalCorruptionError("Invalid OMS journal record hash")
        if stored_previous != previous_hash:
            raise JournalCorruptionError("OMS journal hash-chain mismatch")
        unsigned = dict(record)
        unsigned.pop("hash")
        if self._calculate_hash(unsigned) != stored_hash:
            raise JournalCorruptionError("OMS journal record hash mismatch")
        return seq, stored_hash

    def _load_checkpoint(self) -> tuple[dict, dict] | None:
        if not self.checkpoint_head_path.exists():
            return None
        pointer = self._load_json_document(
            self.checkpoint_head_path,
            "OMS checkpoint head",
        )
        if pointer.get("pointer_version") != self.CHECKPOINT_POINTER_VERSION:
            raise JournalCorruptionError("Unknown OMS checkpoint pointer version")
        if pointer.get("journal_id") != self.journal_id:
            raise JournalCorruptionError("OMS checkpoint pointer identity mismatch")
        expected_pointer = self._document_digest(pointer, "pointer_sha256")
        if pointer.get("pointer_sha256") != expected_pointer:
            raise JournalCorruptionError("OMS checkpoint pointer digest mismatch")
        checkpoint_name = str(pointer.get("checkpoint_file", "") or "")
        if not checkpoint_name or Path(checkpoint_name).name != checkpoint_name:
            raise JournalCorruptionError("Invalid OMS checkpoint filename")
        checkpoint_path = self.checkpoint_directory / checkpoint_name
        checkpoint = self._load_json_document(
            checkpoint_path,
            "OMS recovery checkpoint",
        )
        if checkpoint.get("checkpoint_version") != self.CHECKPOINT_VERSION:
            raise JournalCorruptionError("Unknown OMS checkpoint version")
        if checkpoint.get("journal_id") != self.journal_id:
            raise JournalCorruptionError("OMS checkpoint identity mismatch")
        expected_checkpoint = self._document_digest(
            checkpoint,
            "checkpoint_sha256",
        )
        if checkpoint.get("checkpoint_sha256") != expected_checkpoint:
            raise JournalCorruptionError("OMS checkpoint digest mismatch")
        if pointer.get("checkpoint_sha256") != expected_checkpoint:
            raise JournalCorruptionError("OMS checkpoint pointer target mismatch")
        return pointer, checkpoint

    def _scan_segments(
        self,
        *,
        start_segment_index: int = 1,
        trusted_anchor_seq: int = 0,
        trusted_anchor_hash: str = "",
        collect: bool = False,
    ) -> tuple[int, str, list[dict], list[dict]]:
        paths = self._discover_segment_paths()
        if not paths:
            if trusted_anchor_seq:
                raise JournalCorruptionError(
                    "OMS checkpoint references missing journal segments"
                )
            return 0, "", [], []
        if start_segment_index < 1 or start_segment_index > len(paths):
            raise JournalCorruptionError("OMS checkpoint segment is missing")

        expected_seq = 1
        previous_hash = ""
        previous_segment_hash: str | None = ""
        anchor_seen = trusted_anchor_seq == 0
        collected: list[dict] = []
        segment_metadata: list[dict] = []
        for segment_index, path in enumerate(paths, start=1):
            frames = self._iter_frames(path)
            try:
                header_frame_index, header = next(frames)
            except StopIteration as exc:
                raise JournalCorruptionError(
                    f"Empty OMS journal segment: {path.name}"
                ) from exc
            if header_frame_index != 1:
                raise JournalCorruptionError("Missing OMS segment header")
            header_start_seq, header_previous_hash = self._validate_segment_header(
                header,
                segment_index=segment_index,
                previous_segment_hash=(
                    previous_segment_hash
                    if segment_index >= start_segment_index
                    else None
                ),
            )
            if segment_index < start_segment_index:
                previous_segment_hash = None
                continue
            if segment_index == start_segment_index and trusted_anchor_seq:
                expected_seq = header_start_seq
                previous_hash = header_previous_hash
            elif header_start_seq != expected_seq:
                raise JournalCorruptionError(
                    f"Segment start sequence mismatch at segment {segment_index}"
                )

            record_count = 0
            last_hash_in_segment = previous_hash
            for frame_index, record in frames:
                if frame_index < 2:
                    raise JournalCorruptionError("Invalid OMS segment frame order")
                seq, record_hash = self._validate_record(
                    record,
                    expected_seq=expected_seq,
                    previous_hash=previous_hash,
                )
                expected_seq = seq + 1
                previous_hash = record_hash
                last_hash_in_segment = record_hash
                record_count += 1
                if seq == trusted_anchor_seq:
                    if record_hash != trusted_anchor_hash:
                        raise JournalCorruptionError(
                            "OMS checkpoint anchor hash mismatch"
                        )
                    anchor_seen = True
                if collect and (not trusted_anchor_seq or seq > trusted_anchor_seq):
                    collected.append(record)
            previous_segment_hash = last_hash_in_segment
            segment_metadata.append(
                {
                    "index": segment_index,
                    "path": path,
                    "start_seq": header_start_seq,
                    "record_count": record_count,
                    "size": path.stat().st_size,
                    "last_hash": last_hash_in_segment,
                }
            )

        if not anchor_seen:
            raise JournalCorruptionError("OMS checkpoint anchor sequence is missing")
        return expected_seq - 1, previous_hash, segment_metadata, collected

    def _initialize_tail(self) -> None:
        loaded_checkpoint = self._load_checkpoint()
        if loaded_checkpoint is None:
            last_seq, last_hash, metadata, _records = self._scan_segments()
            self._verified_start_seq = 1
        else:
            pointer, checkpoint = loaded_checkpoint
            anchor_seq = checkpoint.get("anchor_seq")
            anchor_segment = checkpoint.get("anchor_segment_index")
            if (
                isinstance(anchor_seq, bool)
                or not isinstance(anchor_seq, int)
                or anchor_seq < 0
                or isinstance(anchor_segment, bool)
                or not isinstance(anchor_segment, int)
                or anchor_segment <= 0
            ):
                raise JournalCorruptionError("Invalid OMS checkpoint anchor")
            last_seq, last_hash, metadata, tail = self._scan_segments(
                start_segment_index=anchor_segment,
                trusted_anchor_seq=anchor_seq,
                trusted_anchor_hash=str(checkpoint.get("anchor_hash", "") or ""),
                collect=True,
            )
            if not tail:
                raise JournalCorruptionError(
                    "OMS checkpoint has no committed journal marker"
                )
            marker = tail[0]
            if (
                marker.get("seq") != pointer.get("marker_seq")
                or marker.get("hash") != pointer.get("marker_hash")
                or marker.get("kind") != "checkpoint_committed"
                or marker.get("payload", {}).get("checkpoint_sha256")
                != checkpoint.get("checkpoint_sha256")
                or marker.get("payload", {}).get("anchor_seq") != anchor_seq
                or marker.get("payload", {}).get("anchor_hash")
                != checkpoint.get("anchor_hash")
            ):
                raise JournalCorruptionError(
                    "OMS checkpoint was not committed by the journal tail"
                )
            self._checkpoint = checkpoint
            self._checkpoint_marker_seq = int(pointer["marker_seq"])
            self._checkpoint_marker_hash = str(pointer["marker_hash"])
            self._verified_start_seq = anchor_seq

        self._next_seq = last_seq + 1
        self._last_hash = last_hash
        self._segments = metadata
        if metadata:
            active = metadata[-1]
            self._active_segment_index = int(active["index"])
            self._active_segment_record_count = int(active["record_count"])
            self._active_segment_size = int(active["size"])

    def _create_segment(self, index: int, start_seq: int, previous_hash: str) -> Path:
        path = self._segment_path(index)
        header = {
            "frame_kind": "segment_header",
            "header_version": self.SEGMENT_HEADER_VERSION,
            "journal_format_version": self.FORMAT_VERSION,
            "journal_id": self.journal_id,
            "segment_index": index,
            "start_seq": start_seq,
            "previous_segment_hash": previous_hash,
            "created_at_utc": _utc_now(),
        }
        payload = self.SEGMENT_MAGIC + self._encode_frame(header)
        self._ensure_disk_space(len(payload))
        try:
            with open(path, "xb") as handle:
                handle.write(payload)
                handle.flush()
                if self.fsync_enabled:
                    os.fsync(handle.fileno())
        except OSError as exc:
            self._write_failure_count += 1
            self._last_space_error = (
                f"segment_create_failed:{type(exc).__name__}:{exc}"
            )
            raise JournalWriteError(
                f"Failed to create OMS journal segment: {exc}"
            ) from exc
        self._active_segment_index = index
        self._active_segment_record_count = 0
        self._active_segment_size = len(payload)
        self._segments.append(
            {
                "index": index,
                "path": path,
                "start_seq": start_seq,
                "record_count": 0,
                "size": len(payload),
                "last_hash": previous_hash,
            }
        )
        return path

    def _ensure_append_segment(self, estimated_bytes: int, records: int) -> Path:
        rotate = (
            self._active_segment_index == 0
            or (
                self._active_segment_record_count > 0
                and (
                    self._active_segment_record_count + records
                    > self.segment_max_records
                    or self._active_segment_size + estimated_bytes
                    > self.segment_max_bytes
                )
            )
        )
        if rotate:
            return self._create_segment(
                self._active_segment_index + 1,
                self._next_seq,
                self._last_hash,
            )
        return self._segment_path(self._active_segment_index)

    def append(self, kind: str, payload: dict):
        committed = self.append_batch(((kind, payload),))
        return committed[0] if committed else 0

    def append_batch(self, records: Iterable[tuple[str, dict]]) -> list[int]:
        if not self.enabled:
            records = list(records)
            return [0 for _item in records]
        records = list(records)
        if not records:
            return []

        with self.lock:
            next_seq = self._next_seq
            last_hash = self._last_hash
            encoded_frames = []
            committed_sequences = []
            committed_metadata = []
            try:
                for kind, payload in records:
                    kind = str(kind or "")
                    if not kind or len(kind) > 128:
                        raise ValueError("event kind must be 1..128 characters")
                    normalized_payload = _normalize(payload)
                    if not isinstance(normalized_payload, dict):
                        raise ValueError("event payload must be an object")
                    unsigned = {
                        "version": self.FORMAT_VERSION,
                        "journal_id": self.journal_id,
                        "record_kind": self.RECORD_KIND,
                        "record_version": self.EVENT_RECORD_VERSION,
                        "seq": next_seq,
                        "ts": _utc_now(),
                        "kind": kind,
                        "payload_schema": f"{self.PAYLOAD_SCHEMA_PREFIX}{kind}",
                        "payload_version": 1,
                        "payload": normalized_payload,
                        "prev_hash": last_hash,
                    }
                    record_hash = self._calculate_hash(unsigned)
                    record = dict(unsigned)
                    record["hash"] = record_hash
                    frame = self._encode_frame(record)
                    if len(frame) > self.max_frame_bytes + 8:
                        raise ValueError("event frame exceeds configured maximum")
                    encoded_frames.append(frame)
                    committed_sequences.append(next_seq)
                    committed_metadata.append(
                        {
                            "journal_id": self.journal_id,
                            "seq": next_seq,
                            "ts": unsigned["ts"],
                            "hash": record_hash,
                        }
                    )
                    next_seq += 1
                    last_hash = record_hash
            except (TypeError, ValueError) as exc:
                raise JournalWriteError(
                    f"OMS journal batch payload is invalid: {exc}"
                ) from exc

            encoded_batch = b"".join(encoded_frames)
            path = self._ensure_append_segment(len(encoded_batch), len(records))
            self._ensure_disk_space(len(encoded_batch))
            try:
                with open(path, "ab") as handle:
                    handle.write(encoded_batch)
                    handle.flush()
                    if self.fsync_enabled:
                        os.fsync(handle.fileno())
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
            self._active_segment_record_count += len(records)
            self._active_segment_size += len(encoded_batch)
            active = self._segments[-1]
            active["record_count"] = self._active_segment_record_count
            active["size"] = self._active_segment_size
            active["last_hash"] = last_hash
            for metadata in committed_metadata:
                metadata["segment_index"] = self._active_segment_index
                sequence = int(metadata["seq"])
                self._recent_commits[sequence] = metadata
                self._recent_commit_order.append(sequence)
                if len(self._recent_commit_order) > 4096:
                    expired = self._recent_commit_order.popleft()
                    self._recent_commits.pop(expired, None)
            return committed_sequences

    def commit_checkpoint(self, rebuild_summary: dict) -> dict:
        """Persist an immutable rebuild checkpoint and commit its tail marker."""
        if not self.enabled:
            raise JournalWriteError("Cannot checkpoint a disabled OMS journal")
        if not isinstance(rebuild_summary, dict):
            raise ValueError("rebuild_summary must be an object")
        with self.lock:
            if self._next_seq <= 1 or self._active_segment_index <= 0:
                raise JournalWriteError(
                    "Cannot checkpoint an OMS journal without durable records"
                )
            checkpoint = {
                "checkpoint_version": self.CHECKPOINT_VERSION,
                "journal_format_version": self.FORMAT_VERSION,
                "journal_id": self.journal_id,
                "anchor_seq": self._next_seq - 1,
                "anchor_hash": self._last_hash,
                "anchor_segment_index": self._active_segment_index,
                "summary_schema": "chronoshft.oms.rebuild-summary",
                "summary_version": 1,
                "summary": _normalize(rebuild_summary),
                "created_at_utc": _utc_now(),
            }
            checkpoint["checkpoint_sha256"] = self._document_digest(
                checkpoint,
                "checkpoint_sha256",
            )
            checkpoint_name = (
                f"checkpoint-{checkpoint['anchor_seq']:020d}-"
                f"{checkpoint['checkpoint_sha256']}.json"
            )
            checkpoint_path = self.checkpoint_directory / checkpoint_name
            if checkpoint_path.exists():
                existing = self._load_json_document(
                    checkpoint_path,
                    "OMS recovery checkpoint",
                )
                if existing != checkpoint:
                    raise JournalCorruptionError(
                        "OMS checkpoint filename collision"
                    )
            else:
                self._atomic_write(
                    checkpoint_path,
                    self._canonical_bytes(checkpoint) + b"\n",
                    fsync=self.fsync_enabled,
                )

            marker_seq = self.append(
                "checkpoint_committed",
                {
                    "checkpoint_sha256": checkpoint["checkpoint_sha256"],
                    "checkpoint_file": checkpoint_name,
                    "anchor_seq": checkpoint["anchor_seq"],
                    "anchor_hash": checkpoint["anchor_hash"],
                },
            )
            marker = self.commit_metadata(marker_seq)
            if marker is None:
                raise JournalWriteError("Checkpoint marker metadata is unavailable")
            pointer = {
                "pointer_version": self.CHECKPOINT_POINTER_VERSION,
                "journal_id": self.journal_id,
                "checkpoint_file": checkpoint_name,
                "checkpoint_sha256": checkpoint["checkpoint_sha256"],
                "marker_seq": marker_seq,
                "marker_hash": marker["hash"],
                "marker_segment_index": marker["segment_index"],
            }
            pointer["pointer_sha256"] = self._document_digest(
                pointer,
                "pointer_sha256",
            )
            self._atomic_write(
                self.checkpoint_head_path,
                self._canonical_bytes(pointer) + b"\n",
                fsync=self.fsync_enabled,
            )
            self._checkpoint = checkpoint
            self._checkpoint_marker_seq = marker_seq
            self._checkpoint_marker_hash = str(marker["hash"])
            return dict(checkpoint)

    def recovery_checkpoint(self) -> dict | None:
        with self.lock:
            return dict(self._checkpoint) if self._checkpoint is not None else None

    def recovery_plan(self) -> tuple[dict | None, Iterator[dict]]:
        """Return a trusted checkpoint summary and records committed after it."""
        checkpoint = self.recovery_checkpoint()
        start_seq = 1
        if checkpoint is not None:
            start_seq = int(checkpoint["anchor_seq"]) + 1
        return checkpoint, self.iter_records(start_seq=start_seq)

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
            try:
                free_bytes = int(shutil.disk_usage(self.manifest_path.parent).free)
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
        """Read and verify every v3 record regardless of replay policy."""
        return list(self.iter_records())

    def iter_records(
        self,
        *,
        respect_replay_policy: bool = False,
        start_seq: int = 1,
        expected_prev_hash: str | None = None,
    ):
        if (
            not self.enabled
            or (respect_replay_policy and not self.replay_on_startup)
        ):
            return
        if isinstance(start_seq, bool) or not isinstance(start_seq, int) or start_seq < 1:
            raise ValueError("start_seq must be a positive integer")
        with self.lock:
            previous_hash = ""
            expected_seq = 1
            for segment_index, path in enumerate(
                self._discover_segment_paths(),
                start=1,
            ):
                frames = self._iter_frames(path)
                try:
                    _frame_index, header = next(frames)
                except StopIteration as exc:
                    raise JournalCorruptionError(
                        f"Empty OMS journal segment: {path.name}"
                    ) from exc
                header_start_seq, _header_previous = self._validate_segment_header(
                    header,
                    segment_index=segment_index,
                    previous_segment_hash=previous_hash,
                )
                if header_start_seq != expected_seq:
                    raise JournalCorruptionError(
                        f"Segment start sequence mismatch at segment {segment_index}"
                    )
                for _frame_index, record in frames:
                    seq, record_hash = self._validate_record(
                        record,
                        expected_seq=expected_seq,
                        previous_hash=previous_hash,
                    )
                    if seq == start_seq and expected_prev_hash is not None:
                        if previous_hash != expected_prev_hash:
                            raise JournalCorruptionError(
                                "Projection high-water hash does not match journal tail"
                            )
                    expected_seq = seq + 1
                    previous_hash = record_hash
                    if seq >= start_seq:
                        yield record

    def health_snapshot(self):
        with self.lock:
            return {
                "enabled": self.enabled,
                "format_version": self.FORMAT_VERSION,
                "journal_id": self.journal_id,
                "path": str(self.manifest_path),
                "segment_directory": str(self.segment_directory),
                "fsync_enabled": self.fsync_enabled,
                "integrity_check_enabled": self.integrity_check_enabled,
                "next_seq": self._next_seq,
                "last_hash": self._last_hash,
                "segment_count": len(self._segments),
                "active_segment_index": self._active_segment_index,
                "active_segment_record_count": self._active_segment_record_count,
                "checkpoint_anchor_seq": int(
                    (self._checkpoint or {}).get("anchor_seq", 0) or 0
                ),
                "checkpoint_marker_seq": self._checkpoint_marker_seq,
                "verified_start_seq": self._verified_start_seq,
                "min_free_bytes": self.min_free_bytes,
                "disk_free_bytes": self._disk_free_bytes,
                "last_space_check_at": self._last_space_check_at,
                "space_check_failure_count": self._space_check_failure_count,
                "space_rejection_count": self._space_rejection_count,
                "write_failure_count": self._write_failure_count,
                "last_space_error": self._last_space_error,
            }


def decode_legacy_journal(path: str | os.PathLike[str]) -> list[dict]:
    """Strictly decode one complete legacy or v2 JSONL journal for migration."""
    source = Path(path)
    records: list[dict] = []
    detected_format = ""
    expected_seq: int | None = None
    previous_hash = ""
    try:
        with open(source, "r", encoding="utf-8-sig") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if not raw.strip():
                    continue
                try:
                    record = json.loads(
                        raw,
                        parse_constant=_reject_nonstandard_json_constant,
                    )
                except (json.JSONDecodeError, ValueError) as exc:
                    raise JournalCorruptionError(
                        f"Invalid legacy OMS journal JSON at line {line_number}: {exc}"
                    ) from exc
                if not isinstance(record, dict):
                    raise JournalCorruptionError(
                        f"Legacy OMS journal line {line_number} is not an object"
                    )
                version = record.get("version")
                durable_fields = {"version", "seq", "hash", "prev_hash"}.intersection(
                    record
                )
                if version is None:
                    if durable_fields:
                        raise JournalCorruptionError(
                            "Malformed legacy OMS journal record with partial envelope"
                        )
                    record_format = "legacy"
                elif version == 2 and not isinstance(version, bool):
                    record_format = "v2"
                else:
                    raise JournalCorruptionError(
                        f"Unsupported legacy migration journal version: {version!r}"
                    )
                if detected_format and detected_format != record_format:
                    raise JournalCorruptionError(
                        "Mixed legacy/v2 OMS journal cannot be migrated"
                    )
                detected_format = record_format
                if record_format == "v2":
                    try:
                        seq = int(record["seq"])
                        stored_hash = str(record["hash"])
                        stored_previous = str(record.get("prev_hash", ""))
                    except (KeyError, TypeError, ValueError) as exc:
                        raise JournalCorruptionError(
                            f"Malformed v2 OMS journal record at line {line_number}"
                        ) from exc
                    if expected_seq is None:
                        expected_seq = seq
                    if seq != expected_seq or stored_previous != previous_hash:
                        raise JournalCorruptionError(
                            f"Invalid v2 OMS journal chain at line {line_number}"
                        )
                    unsigned = dict(record)
                    unsigned.pop("hash", None)
                    calculated = hashlib.sha256(
                        OMSJournal._canonical_bytes(unsigned)
                    ).hexdigest()
                    if calculated != stored_hash:
                        raise JournalCorruptionError(
                            f"Invalid v2 OMS journal hash at line {line_number}"
                        )
                    expected_seq = seq + 1
                    previous_hash = stored_hash
                records.append(record)
    except OSError as exc:
        raise JournalError(f"Failed to read legacy OMS journal: {exc}") from exc
    return records


__all__ = [
    "JournalCorruptionError",
    "JournalError",
    "JournalMigrationRequiredError",
    "JournalWriteError",
    "OMSJournal",
    "decode_legacy_journal",
]
