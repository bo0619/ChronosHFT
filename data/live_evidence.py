from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from event.type import (
    EVENT_EXCHANGE_ACCOUNT_UPDATE,
    EVENT_MARK_PRICE,
    Event,
    ExchangeAccountUpdate,
    MarkPriceData,
)
from infrastructure.logger import logger
from infrastructure.single_writer_fence import SingleWriterFence


LIVE_EVIDENCE_SCHEMA = "chronoshft.live_market_evidence.v1"
LIVE_EVIDENCE_RECORD_VERSION = 1
LIVE_EVIDENCE_KINDS = frozenset(
    {
        "session_start",
        "mark_price",
        "account_update",
        "clean_stop",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,32}$")
_ASSET_RE = re.compile(r"^[A-Z0-9]{2,16}$")
_STOP = object()
_MIN_QUEUE_CAPACITY = 1024
_MAX_QUEUE_CAPACITY = 65_536
_MAX_BATCH_RECORDS = 1024
_MAX_FSYNC_INTERVAL_SEC = 5.0
_MAX_CLOSE_TIMEOUT_SEC = 30.0


class LiveEvidenceError(RuntimeError):
    """Base class for durable Live evidence failures."""


class LiveEvidenceCorruptionError(LiveEvidenceError):
    """Raised when a persisted evidence journal is not a valid hash chain."""


class LiveEvidenceWriteError(LiveEvidenceError):
    """Raised when a Live evidence record cannot be made durable."""


@dataclass(frozen=True, slots=True)
class LiveEvidenceJournalSummary:
    path: str
    record_count: int
    first_seq: int
    last_seq: int
    final_hash: str
    first_recorded_at_utc: str
    last_recorded_at_utc: str
    last_kind: str
    session_count: int
    mark_price_count: int
    account_update_count: int
    clean_stop_count: int
    deployment_ids: tuple[str, ...]
    deployment_config_sha256s: tuple[str, ...]


def _reject_json_constant(value: str):
    raise LiveEvidenceCorruptionError(
        f"non-finite JSON constant is not allowed: {value}"
    )


def _object_without_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise LiveEvidenceCorruptionError(
                f"duplicate JSON object key is not allowed: {key!r}"
            )
        value[key] = item
    return value


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise LiveEvidenceWriteError(
            "Live evidence payload is not strict canonical JSON"
        ) from exc


def _record_hash(record_without_hash: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        _canonical_json(record_without_hash).encode("ascii")
    ).hexdigest()


def _parse_record(raw: str, line_number: int) -> dict[str, Any]:
    try:
        value = json.loads(
            raw,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (
        json.JSONDecodeError,
        LiveEvidenceCorruptionError,
    ) as exc:
        raise LiveEvidenceCorruptionError(
            f"invalid Live evidence JSON at line {line_number}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise LiveEvidenceCorruptionError(
            f"Live evidence line {line_number} must be a JSON object"
        )
    return value


def _validated_utc(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text or not text.endswith("Z"):
        raise LiveEvidenceCorruptionError(f"{field} must be UTC with a Z suffix")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise LiveEvidenceCorruptionError(
            f"{field} is not a valid UTC timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LiveEvidenceCorruptionError(f"{field} must be timezone-aware")
    return text


def _validated_sha256(value: Any, field: str) -> str:
    text = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(text):
        raise LiveEvidenceCorruptionError(f"{field} must be a SHA-256 digest")
    return text


def iter_validated_live_evidence_records(
    path: str | os.PathLike[str],
) -> Iterator[dict[str, Any]]:
    """Yield a strict, contiguous, hash-validated evidence journal."""

    source = Path(path)
    expected_seq = 1
    previous_hash = ""
    try:
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if not raw.strip():
                    raise LiveEvidenceCorruptionError(
                        f"blank Live evidence record at line {line_number}"
                    )
                record = _parse_record(raw, line_number)
                expected_keys = {
                    "version",
                    "seq",
                    "recorded_at_utc",
                    "kind",
                    "payload",
                    "prev_hash",
                    "hash",
                }
                if set(record) != expected_keys:
                    raise LiveEvidenceCorruptionError(
                        "Live evidence record keys are invalid at line "
                        f"{line_number}"
                    )
                if record.get("version") != LIVE_EVIDENCE_RECORD_VERSION:
                    raise LiveEvidenceCorruptionError(
                        "unsupported Live evidence record version at line "
                        f"{line_number}"
                    )
                seq = record.get("seq")
                if isinstance(seq, bool) or not isinstance(seq, int):
                    raise LiveEvidenceCorruptionError(
                        f"invalid Live evidence sequence at line {line_number}"
                    )
                if seq != expected_seq:
                    raise LiveEvidenceCorruptionError(
                        "Live evidence sequence gap at line "
                        f"{line_number}: expected {expected_seq}, got {seq}"
                    )
                _validated_utc(
                    record.get("recorded_at_utc"),
                    f"line {line_number} recorded_at_utc",
                )
                kind = str(record.get("kind", "") or "")
                if kind not in LIVE_EVIDENCE_KINDS:
                    raise LiveEvidenceCorruptionError(
                        f"unsupported Live evidence kind at line {line_number}"
                    )
                if not isinstance(record.get("payload"), dict):
                    raise LiveEvidenceCorruptionError(
                        f"Live evidence payload must be an object at line {line_number}"
                    )
                stored_previous = str(record.get("prev_hash", "") or "")
                if stored_previous != previous_hash:
                    raise LiveEvidenceCorruptionError(
                        "Live evidence previous-hash mismatch at line "
                        f"{line_number}"
                    )
                stored_hash = _validated_sha256(
                    record.get("hash"),
                    f"line {line_number} hash",
                )
                unsigned = dict(record)
                unsigned.pop("hash")
                if _record_hash(unsigned) != stored_hash:
                    raise LiveEvidenceCorruptionError(
                        f"Live evidence record hash mismatch at line {line_number}"
                    )
                yield record
                expected_seq += 1
                previous_hash = stored_hash
    except OSError as exc:
        raise LiveEvidenceError(
            f"cannot read Live evidence journal {source}: {exc}"
        ) from exc


def validate_live_evidence_journal(
    path: str | os.PathLike[str],
) -> LiveEvidenceJournalSummary:
    """Validate the complete journal and return its immutable tail metadata."""

    first_seq = 0
    last_seq = 0
    final_hash = ""
    first_recorded_at = ""
    last_recorded_at = ""
    last_kind = ""
    counts = {
        "session_start": 0,
        "mark_price": 0,
        "account_update": 0,
        "clean_stop": 0,
    }
    deployment_ids = set()
    config_digests = set()
    active_session_id = ""

    for record in iter_validated_live_evidence_records(path):
        if not first_seq:
            first_seq = int(record["seq"])
            first_recorded_at = str(record["recorded_at_utc"])
        last_seq = int(record["seq"])
        final_hash = str(record["hash"])
        last_recorded_at = str(record["recorded_at_utc"])
        last_kind = str(record["kind"])
        counts[last_kind] += 1
        if last_kind == "session_start":
            payload = record["payload"]
            if active_session_id:
                raise LiveEvidenceCorruptionError(
                    "Live evidence session_start appeared before clean_stop"
                )
            if payload.get("schema") != LIVE_EVIDENCE_SCHEMA:
                raise LiveEvidenceCorruptionError(
                    "Live evidence session_start schema is invalid"
                )
            session_id = str(payload.get("session_id", "") or "").strip()
            if not session_id:
                raise LiveEvidenceCorruptionError(
                    "Live evidence session_start session_id is required"
                )
            deployment_id = str(payload.get("deployment_id", "") or "").strip()
            if not deployment_id:
                raise LiveEvidenceCorruptionError(
                    "Live evidence session_start deployment_id is required"
                )
            deployment_ids.add(deployment_id)
            config_digests.add(
                _validated_sha256(
                    payload.get("deployment_config_sha256"),
                    "session_start deployment_config_sha256",
                )
            )
            symbols = payload.get("symbols")
            if (
                not isinstance(symbols, list)
                or not symbols
                or any(
                    not isinstance(symbol, str)
                    or not _SYMBOL_RE.fullmatch(symbol)
                    for symbol in symbols
                )
                or len(set(symbols)) != len(symbols)
            ):
                raise LiveEvidenceCorruptionError(
                    "Live evidence session_start symbols are invalid"
                )
            active_session_id = session_id
        elif last_kind in {"mark_price", "account_update"}:
            if not active_session_id:
                raise LiveEvidenceCorruptionError(
                    f"Live evidence {last_kind} is outside an active session"
                )
        elif last_kind == "clean_stop":
            payload = record["payload"]
            if not active_session_id:
                raise LiveEvidenceCorruptionError(
                    "Live evidence clean_stop has no active session"
                )
            if payload.get("schema") != LIVE_EVIDENCE_SCHEMA:
                raise LiveEvidenceCorruptionError(
                    "Live evidence clean_stop schema is invalid"
                )
            if (
                str(payload.get("session_id", "") or "").strip()
                != active_session_id
            ):
                raise LiveEvidenceCorruptionError(
                    "Live evidence clean_stop session_id mismatch"
                )
            deployment_id = str(
                payload.get("deployment_id", "") or ""
            ).strip()
            if not deployment_id or deployment_id not in deployment_ids:
                raise LiveEvidenceCorruptionError(
                    "Live evidence clean_stop deployment_id mismatch"
                )
            dropped_records = payload.get("dropped_records")
            if (
                isinstance(dropped_records, bool)
                or not isinstance(dropped_records, int)
                or dropped_records != 0
            ):
                raise LiveEvidenceCorruptionError(
                    "Live evidence clean_stop requires dropped_records=0"
                )
            active_session_id = ""

    if not last_seq:
        raise LiveEvidenceCorruptionError("Live evidence journal is empty")
    if first_seq != 1:
        raise LiveEvidenceCorruptionError(
            "Live evidence journal must begin at sequence 1"
        )
    if counts["session_start"] < 1:
        raise LiveEvidenceCorruptionError(
            "Live evidence journal has no session_start record"
        )
    if active_session_id or counts["session_start"] != counts["clean_stop"]:
        raise LiveEvidenceCorruptionError(
            "Live evidence journal has an unsealed session"
        )

    return LiveEvidenceJournalSummary(
        path=str(Path(path).resolve()),
        record_count=last_seq,
        first_seq=first_seq,
        last_seq=last_seq,
        final_hash=final_hash,
        first_recorded_at_utc=first_recorded_at,
        last_recorded_at_utc=last_recorded_at,
        last_kind=last_kind,
        session_count=counts["session_start"],
        mark_price_count=counts["mark_price"],
        account_update_count=counts["account_update"],
        clean_stop_count=counts["clean_stop"],
        deployment_ids=tuple(sorted(deployment_ids)),
        deployment_config_sha256s=tuple(sorted(config_digests)),
    )


def _finite_float(
    value: Any,
    field: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> float:
    if isinstance(value, bool):
        raise LiveEvidenceWriteError(f"{field} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise LiveEvidenceWriteError(f"{field} must be numeric") from exc
    if not math.isfinite(parsed):
        raise LiveEvidenceWriteError(f"{field} must be finite")
    if positive and parsed <= 0.0:
        raise LiveEvidenceWriteError(f"{field} must be positive")
    if nonnegative and parsed < 0.0:
        raise LiveEvidenceWriteError(f"{field} must be nonnegative")
    return parsed


def _optional_finite_float(value: Any, field: str) -> float | None:
    if value is None:
        return None
    return _finite_float(value, field)


def _validated_symbol(value: Any, field: str = "symbol") -> str:
    symbol = str(value or "").strip().upper()
    if not _SYMBOL_RE.fullmatch(symbol):
        raise LiveEvidenceWriteError(f"{field} is invalid")
    return symbol


def mark_price_evidence_payload(mark: MarkPriceData) -> dict[str, Any]:
    """Build a strict exchange-time mark/funding record."""

    next_funding_timestamp = _finite_float(
        getattr(mark, "next_funding_timestamp", 0.0),
        "next_funding_timestamp",
        positive=True,
    )
    return {
        "symbol": _validated_symbol(mark.symbol),
        "mark_price": _finite_float(
            mark.mark_price,
            "mark_price",
            positive=True,
        ),
        "index_price": _finite_float(
            mark.index_price,
            "index_price",
            positive=True,
        ),
        "funding_rate": _finite_float(
            mark.funding_rate,
            "funding_rate",
        ),
        "next_funding_timestamp": next_funding_timestamp,
        "exchange_timestamp": _finite_float(
            mark.exchange_timestamp,
            "exchange_timestamp",
            positive=True,
        ),
        "received_timestamp": _finite_float(
            mark.received_timestamp,
            "received_timestamp",
            positive=True,
        ),
        "corrected_received_timestamp": _finite_float(
            mark.corrected_received_timestamp,
            "corrected_received_timestamp",
            positive=True,
        ),
        "clock_offset_ms": _optional_finite_float(
            mark.clock_offset_ms,
            "clock_offset_ms",
        ),
    }


def account_update_evidence_payload(
    update: ExchangeAccountUpdate,
) -> dict[str, Any]:
    """Build a strict account/funding event without credentials or raw payloads."""

    balances = {}
    for raw_asset, raw_values in sorted((update.balances or {}).items()):
        asset = str(raw_asset or "").strip().upper()
        if not _ASSET_RE.fullmatch(asset):
            raise LiveEvidenceWriteError("account balance asset is invalid")
        if not isinstance(raw_values, Mapping):
            raise LiveEvidenceWriteError(
                f"account balance {asset} must be an object"
            )
        balances[asset] = {
            "wallet_balance": _finite_float(
                raw_values.get("wallet_balance"),
                f"balances.{asset}.wallet_balance",
            ),
            "available_balance": _optional_finite_float(
                raw_values.get("available_balance"),
                f"balances.{asset}.available_balance",
            ),
            "balance_change": _optional_finite_float(
                raw_values.get("balance_change"),
                f"balances.{asset}.balance_change",
            ),
        }

    positions = {}
    for raw_symbol, raw_values in sorted((update.positions or {}).items()):
        symbol = _validated_symbol(
            raw_symbol,
            "account position symbol",
        )
        if not isinstance(raw_values, Mapping):
            raise LiveEvidenceWriteError(
                f"account position {symbol} must be an object"
            )
        positions[symbol] = {
            "volume": _finite_float(
                raw_values.get("volume"),
                f"positions.{symbol}.volume",
            ),
            "entry_price": _finite_float(
                raw_values.get("entry_price"),
                f"positions.{symbol}.entry_price",
                nonnegative=True,
            ),
            "unrealized_pnl": _finite_float(
                raw_values.get("unrealized_pnl"),
                f"positions.{symbol}.unrealized_pnl",
            ),
        }

    update_asset = str(update.asset or "").strip().upper()
    if update_asset and not _ASSET_RE.fullmatch(update_asset):
        raise LiveEvidenceWriteError("account update asset is invalid")

    return {
        "asset": update_asset,
        "wallet_balance": _finite_float(
            update.wallet_balance,
            "wallet_balance",
        ),
        "available_balance": _optional_finite_float(
            update.available_balance,
            "available_balance",
        ),
        "balances": balances,
        "positions": positions,
        "reason": str(update.reason or "").strip().upper(),
        "event_time": _finite_float(
            update.event_time,
            "event_time",
            positive=True,
        ),
        "received_timestamp": _finite_float(
            update.received_timestamp,
            "received_timestamp",
            positive=True,
        ),
        "corrected_received_timestamp": _finite_float(
            update.corrected_received_timestamp,
            "corrected_received_timestamp",
            positive=True,
        ),
        "clock_offset_ms": _optional_finite_float(
            update.clock_offset_ms,
            "clock_offset_ms",
        ),
    }


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _sync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


class LiveEvidenceRecorder:
    """Bounded, batched writer for reconstructable Live market evidence."""

    def __init__(
        self,
        event_engine,
        config: Mapping[str, Any],
        *,
        failure_callback: Callable[[str], Any] | None = None,
        oms_journal_snapshot: Callable[[], Mapping[str, Any]] | None = None,
    ):
        if not isinstance(config, Mapping):
            raise TypeError("Live evidence recorder requires a root config")
        system = config.get("system", {})
        system = system if isinstance(system, Mapping) else {}
        settings = system.get("evidence_recorder", {})
        settings = settings if isinstance(settings, Mapping) else {}
        if not bool(settings.get("enabled", False)):
            raise LiveEvidenceError(
                "system.evidence_recorder.enabled must be true"
            )

        live_launch = config.get("live_launch", {})
        live_launch = (
            live_launch if isinstance(live_launch, Mapping) else {}
        )
        deployment_id = str(
            live_launch.get("deployment_id", "") or ""
        ).strip()
        if not deployment_id:
            raise LiveEvidenceError(
                "Live evidence recorder requires live_launch.deployment_id"
            )
        symbols = tuple(
            _validated_symbol(value)
            for value in config.get("symbols", ())
        )
        if not symbols or len(set(symbols)) != len(symbols):
            raise LiveEvidenceError(
                "Live evidence recorder requires unique configured symbols"
            )

        path_text = str(settings.get("path", "") or "").strip()
        if not path_text:
            raise LiveEvidenceError(
                "system.evidence_recorder.path must be configured"
            )
        self.path = Path(path_text).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._created_new_file = not self.path.exists()

        writer_fence = settings.get("single_writer_fence", {})
        writer_fence = (
            writer_fence if isinstance(writer_fence, Mapping) else {}
        )
        if not bool(writer_fence.get("enabled", False)):
            raise LiveEvidenceError(
                "Live evidence recorder single-writer fence is required"
            )
        fence_path = str(
            writer_fence.get("path", "") or f"{self.path}.lock"
        ).strip()
        self.fence = SingleWriterFence(
            fence_path,
            owner_metadata={
                "component": "ChronosHFT.LiveEvidenceRecorder",
                "deployment_id": deployment_id,
                "symbols": list(symbols),
                "evidence_path": str(self.path),
            },
        )

        queue_capacity = settings.get("queue_capacity", 8192)
        max_batch_records = settings.get("max_batch_records", 256)
        if (
            isinstance(queue_capacity, bool)
            or not isinstance(queue_capacity, int)
            or not _MIN_QUEUE_CAPACITY
            <= queue_capacity
            <= _MAX_QUEUE_CAPACITY
        ):
            raise LiveEvidenceError(
                "evidence recorder queue_capacity must be an integer between "
                f"{_MIN_QUEUE_CAPACITY} and {_MAX_QUEUE_CAPACITY}"
            )
        if (
            isinstance(max_batch_records, bool)
            or not isinstance(max_batch_records, int)
            or not 1 <= max_batch_records <= queue_capacity
            or max_batch_records > _MAX_BATCH_RECORDS
        ):
            raise LiveEvidenceError(
                "evidence recorder max_batch_records must be within its queue "
                f"and no more than {_MAX_BATCH_RECORDS}"
            )
        self.fsync_interval_sec = _finite_float(
            settings.get("fsync_interval_sec", 5.0),
            "evidence recorder fsync_interval_sec",
            positive=True,
        )
        if self.fsync_interval_sec > _MAX_FSYNC_INTERVAL_SEC:
            raise LiveEvidenceError(
                "evidence recorder fsync_interval_sec must be <= "
                f"{_MAX_FSYNC_INTERVAL_SEC:g}"
            )
        self.close_timeout_sec = _finite_float(
            settings.get("close_timeout_sec", 15.0),
            "evidence recorder close_timeout_sec",
            positive=True,
        )
        if self.close_timeout_sec > _MAX_CLOSE_TIMEOUT_SEC:
            raise LiveEvidenceError(
                "evidence recorder close_timeout_sec must be <= "
                f"{_MAX_CLOSE_TIMEOUT_SEC:g}"
            )
        self.max_batch_records = max_batch_records
        self._queue: queue.Queue = queue.Queue(maxsize=queue_capacity)
        self._failure_callback = failure_callback
        self._oms_journal_snapshot = oms_journal_snapshot
        self._lock = threading.RLock()
        self._handle = None
        self._thread = None
        self._closing = False
        self._closed = False
        self._healthy = True
        self._failure_reason = ""
        self._failure_notified = False
        self._next_seq = 1
        self._last_hash = ""
        self._committed_seq = 0
        self._last_fsync_monotonic = 0.0
        self._last_record_monotonic = 0.0
        self._dropped_records = 0
        self._session_id = uuid.uuid4().hex
        self._deployment_id = deployment_id
        self._symbols = symbols

        from strategy.model_readiness import deployment_config_sha256

        self._deployment_config_sha256 = deployment_config_sha256(config)
        oms = config.get("oms", {})
        oms = oms if isinstance(oms, Mapping) else {}
        self._oms_journal_path = str(
            oms.get("journal_path", "") or ""
        ).strip()

        try:
            self.fence.acquire()
            if self.path.exists() and self.path.stat().st_size > 0:
                summary = validate_live_evidence_journal(self.path)
                if summary.last_kind != "clean_stop":
                    raise LiveEvidenceCorruptionError(
                        "existing Live evidence journal is not cleanly sealed; "
                        "use a new deployment_id/path after incident review"
                    )
                if summary.deployment_ids != (deployment_id,):
                    raise LiveEvidenceCorruptionError(
                        "existing Live evidence deployment_id does not match"
                    )
                if summary.deployment_config_sha256s != (
                    self._deployment_config_sha256,
                ):
                    raise LiveEvidenceCorruptionError(
                        "existing Live evidence configuration digest does not match"
                    )
                self._next_seq = summary.last_seq + 1
                self._last_hash = summary.final_hash
                self._committed_seq = summary.last_seq
            self._handle = self.path.open(
                "a",
                encoding="utf-8",
                newline="\n",
                buffering=1,
            )
            self._write_records(
                [
                    (
                        "session_start",
                        {
                            "schema": LIVE_EVIDENCE_SCHEMA,
                            "session_id": self._session_id,
                            "deployment_id": deployment_id,
                            "deployment_config_sha256": (
                                self._deployment_config_sha256
                            ),
                            "symbols": list(symbols),
                            "oms_journal_path": self._oms_journal_path,
                            "oms_journal_anchor": self._journal_anchor(),
                            "process_id": os.getpid(),
                        },
                    )
                ],
                force_fsync=True,
            )
            self._thread = threading.Thread(
                target=self._run_writer,
                daemon=True,
                name="LiveEvidenceWriter",
            )
            self._thread.start()
            event_engine.register_cold(
                EVENT_MARK_PRICE,
                self.on_mark_price,
            )
            event_engine.register_cold(
                EVENT_EXCHANGE_ACCOUNT_UPDATE,
                self.on_account_update,
            )
            logger.info(
                "[LiveEvidence] Recorder started "
                f"path={self.path} deployment_id={deployment_id}"
            )
        except BaseException:
            self._close_handle()
            self.fence.release()
            raise

    def _journal_anchor(self) -> dict[str, Any]:
        if not callable(self._oms_journal_snapshot):
            return {}
        try:
            snapshot = self._oms_journal_snapshot()
        except Exception:
            return {}
        if not isinstance(snapshot, Mapping):
            return {}
        next_seq = snapshot.get("next_seq")
        last_hash = str(snapshot.get("last_hash", "") or "").lower()
        if (
            isinstance(next_seq, bool)
            or not isinstance(next_seq, int)
            or next_seq < 1
            or (last_hash and not _SHA256_RE.fullmatch(last_hash))
        ):
            return {}
        return {
            "next_seq": next_seq,
            "last_hash": last_hash,
        }

    def _make_record(
        self,
        kind: str,
        payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str]:
        if kind not in LIVE_EVIDENCE_KINDS:
            raise LiveEvidenceWriteError(
                f"unsupported Live evidence kind: {kind!r}"
            )
        unsigned = {
            "version": LIVE_EVIDENCE_RECORD_VERSION,
            "seq": self._next_seq,
            "recorded_at_utc": _utc_now(),
            "kind": kind,
            "payload": dict(payload),
            "prev_hash": self._last_hash,
        }
        record_hash = _record_hash(unsigned)
        record = dict(unsigned)
        record["hash"] = record_hash
        return record, record_hash

    def _write_records(
        self,
        items: list[tuple[str, Mapping[str, Any]]],
        *,
        force_fsync: bool,
    ) -> None:
        handle = self._handle
        if handle is None:
            raise LiveEvidenceWriteError(
                "Live evidence file handle is unavailable"
            )
        for kind, payload in items:
            record, record_hash = self._make_record(kind, payload)
            handle.write(_canonical_json(record) + "\n")
            self._last_hash = record_hash
            self._committed_seq = self._next_seq
            self._next_seq += 1
            self._last_record_monotonic = time.perf_counter()
        handle.flush()
        if force_fsync:
            os.fsync(handle.fileno())
            self._last_fsync_monotonic = time.perf_counter()
            if self._created_new_file:
                _sync_directory(self.path.parent)
                self._created_new_file = False

    def _fail(self, reason: str) -> None:
        callback = None
        with self._lock:
            self._healthy = False
            if not self._failure_reason:
                self._failure_reason = str(reason or "evidence_writer_failed")
            if not self._failure_notified:
                self._failure_notified = True
                callback = self._failure_callback
        logger.critical(
            "[LiveEvidence] Recorder unavailable: "
            f"{self._failure_reason}"
        )
        if callable(callback):
            try:
                callback(self._failure_reason)
            except Exception as exc:
                logger.error(
                    "[LiveEvidence] Failure callback failed: "
                    f"{type(exc).__name__}"
                )

    def _run_writer(self) -> None:
        pending: list[tuple[str, Mapping[str, Any]]] = []
        stop_requested = False
        try:
            while not stop_requested:
                timeout = max(
                    0.05,
                    self.fsync_interval_sec
                    - (
                        time.perf_counter()
                        - self._last_fsync_monotonic
                    ),
                )
                try:
                    item = self._queue.get(timeout=timeout)
                except queue.Empty:
                    item = None

                if item is _STOP:
                    stop_requested = True
                    self._queue.task_done()
                elif item is not None:
                    pending.append(item)
                    self._queue.task_done()

                while (
                    not stop_requested
                    and len(pending) < self.max_batch_records
                ):
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is _STOP:
                        stop_requested = True
                        self._queue.task_done()
                        break
                    pending.append(item)
                    self._queue.task_done()

                now = time.perf_counter()
                fsync_due = (
                    now - self._last_fsync_monotonic
                    >= self.fsync_interval_sec
                )
                if pending and (
                    len(pending) >= self.max_batch_records
                    or fsync_due
                    or stop_requested
                ):
                    self._write_records(
                        pending,
                        force_fsync=fsync_due or stop_requested,
                    )
                    pending = []
                elif fsync_due and self._handle is not None:
                    self._handle.flush()
                    os.fsync(self._handle.fileno())
                    self._last_fsync_monotonic = now

            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    if item is not _STOP:
                        pending.append(item)
                finally:
                    self._queue.task_done()
            if pending:
                self._write_records(pending, force_fsync=False)
            self._write_records(
                [
                    (
                        "clean_stop",
                        {
                            "schema": LIVE_EVIDENCE_SCHEMA,
                            "session_id": self._session_id,
                            "deployment_id": self._deployment_id,
                            "oms_journal_anchor": self._journal_anchor(),
                            "dropped_records": self._dropped_records,
                        },
                    )
                ],
                force_fsync=True,
            )
        except BaseException as exc:
            self._fail(f"writer_{type(exc).__name__}")

    def _enqueue(self, kind: str, payload: Mapping[str, Any]) -> bool:
        with self._lock:
            if self._closing or self._closed or not self._healthy:
                return False
        try:
            self._queue.put_nowait((kind, dict(payload)))
            return True
        except queue.Full:
            with self._lock:
                self._dropped_records += 1
            self._fail("queue_overflow")
            return False

    def on_mark_price(self, event: Event) -> None:
        try:
            payload = mark_price_evidence_payload(event.data)
            if payload["symbol"] not in self._symbols:
                return
            if not self._enqueue("mark_price", payload):
                raise LiveEvidenceWriteError(
                    "mark-price evidence was not accepted"
                )
        except Exception as exc:
            self._fail(f"invalid_mark_price:{type(exc).__name__}")

    def on_account_update(self, event: Event) -> None:
        try:
            payload = account_update_evidence_payload(event.data)
            if not self._enqueue("account_update", payload):
                raise LiveEvidenceWriteError(
                    "account evidence was not accepted"
                )
        except Exception as exc:
            self._fail(f"invalid_account_update:{type(exc).__name__}")

    def health_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": True,
                "healthy": self._healthy,
                "failure_reason": self._failure_reason,
                "path": str(self.path),
                "fence_held": self.fence.handle is not None,
                "queue_depth": self._queue.qsize(),
                "queue_capacity": self._queue.maxsize,
                "committed_seq": self._committed_seq,
                "last_hash": self._last_hash,
                "last_fsync_age_sec": (
                    max(
                        0.0,
                        time.perf_counter()
                        - self._last_fsync_monotonic,
                    )
                    if self._last_fsync_monotonic > 0.0
                    else None
                ),
                "dropped_records": self._dropped_records,
                "closing": self._closing,
                "closed": self._closed,
            }

    def close(self) -> bool:
        with self._lock:
            if self._closed:
                return bool(self._healthy and self._dropped_records == 0)
            if not self._closing:
                self._closing = True
                try:
                    self._queue.put(_STOP, timeout=self.close_timeout_sec)
                except queue.Full:
                    self._dropped_records += 1
                    self._fail("close_queue_timeout")

        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=self.close_timeout_sec)
        thread_stopped = thread is None or not thread.is_alive()
        if not thread_stopped:
            self._fail("close_writer_timeout")

        self._close_handle()
        fence_released = self.fence.release()
        with self._lock:
            self._closed = thread_stopped
            clean = bool(
                self._closed
                and self._healthy
                and self._dropped_records == 0
                and fence_released
            )
        if clean:
            logger.info("[LiveEvidence] Recorder closed and sealed")
        return clean

    def _close_handle(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass


class RecorderGroup:
    """Close several passive recorders through the existing shutdown barrier."""

    def __init__(self, *recorders):
        self.recorders = tuple(
            recorder for recorder in recorders if recorder is not None
        )
        self._closed = False

    def close(self) -> bool:
        if self._closed:
            return True
        clean = True
        for recorder in self.recorders:
            try:
                clean = bool(recorder.close()) and clean
            except Exception as exc:
                clean = False
                logger.error(
                    "[RecorderGroup] close failed: "
                    f"{type(exc).__name__}:{exc}"
                )
        self._closed = clean
        return clean


__all__ = [
    "LIVE_EVIDENCE_KINDS",
    "LIVE_EVIDENCE_RECORD_VERSION",
    "LIVE_EVIDENCE_SCHEMA",
    "LiveEvidenceCorruptionError",
    "LiveEvidenceError",
    "LiveEvidenceJournalSummary",
    "LiveEvidenceRecorder",
    "LiveEvidenceWriteError",
    "RecorderGroup",
    "account_update_evidence_payload",
    "iter_validated_live_evidence_records",
    "mark_price_evidence_payload",
    "validate_live_evidence_journal",
]
