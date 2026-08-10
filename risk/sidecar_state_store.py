"""Account-scoped durable state store for the independent risk sidecar."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import sqlite3
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

from risk.exchange_port import StateVersion
from risk.sidecar_state_payload import (
    SCHEMA_VERSION,
    SidecarStatePayloadError,
    parse_sidecar_state_payload,
)

_SAFETY_INCREASING = "SAFETY_INCREASING"
_NEUTRAL = "NEUTRAL"
_RISK_INCREASING = "RISK_INCREASING"
OPERATION_CLASSES = frozenset(
    {_SAFETY_INCREASING, _NEUTRAL, _RISK_INCREASING}
)


class SidecarStateStoreError(RuntimeError):
    """Raised when durable lineage cannot be recovered safely."""


class SidecarStateCasError(SidecarStateStoreError):
    """Raised when a stale writer attempts to modify durable state."""


class SidecarWriterFenceError(SidecarStateStoreError):
    """Raised when another sidecar owns the account writer fence."""


def _validated_state_payload(
    payload: Mapping,
    *,
    account_scope_id: str,
    deployment_id: str,
) -> dict:
    try:
        return parse_sidecar_state_payload(
            payload,
            account_scope_id=account_scope_id,
            deployment_id=deployment_id,
        )
    except SidecarStatePayloadError as exc:
        raise SidecarStateStoreError(str(exc)) from exc


def _canonical_bytes(value: Mapping) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: Mapping) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _write_json_atomic(path: Path, payload: Mapping) -> None:
    encoded = _canonical_bytes(payload)
    temporary = path.with_name(
        f"{path.name}.tmp.{os.getpid()}.{secrets.token_hex(6)}"
    )
    try:
        with open(temporary, "xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        with suppress(OSError):
            temporary.unlink()


def _read_json_object(path: Path, label: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError) as exc:
        raise SidecarStateStoreError(f"{label}_unreadable:{exc}") from exc
    if not isinstance(value, dict):
        raise SidecarStateStoreError(f"{label}_not_object")
    return value


class AccountWriterFence:
    """Recover-only OS lock held by the child sidecar for its lifetime."""

    def __init__(self, path: Path, owner: Mapping):
        self.path = Path(path)
        self.owner = dict(owner)
        self.handle = None
        self.file_identity: tuple[int, int] | None = None

    @staticmethod
    def provision(path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "xb") as handle:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _lock(handle) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def acquire(self) -> None:
        if self.handle is not None:
            return
        if not self.path.is_file():
            raise SidecarWriterFenceError("writer_fence_missing")
        handle = open(self.path, "r+b")
        try:
            self._lock(handle)
        except OSError as exc:
            handle.close()
            raise SidecarWriterFenceError("writer_fence_already_held") from exc
        stat = os.fstat(handle.fileno())
        self.file_identity = (int(stat.st_dev), int(stat.st_ino))
        metadata = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "acquired_at": time.time(),
            **self.owner,
        }
        handle.seek(1)
        handle.truncate(1)
        handle.write(_canonical_bytes(metadata))
        handle.flush()
        os.fsync(handle.fileno())
        self.handle = handle
        self.validate()

    def validate(self) -> None:
        if self.handle is None or self.file_identity is None:
            raise SidecarWriterFenceError("writer_fence_not_held")
        handle_stat = os.fstat(self.handle.fileno())
        try:
            path_stat = os.stat(self.path)
        except OSError as exc:
            raise SidecarWriterFenceError("writer_fence_path_missing") from exc
        handle_identity = (int(handle_stat.st_dev), int(handle_stat.st_ino))
        path_identity = (int(path_stat.st_dev), int(path_stat.st_ino))
        if handle_identity != self.file_identity or path_identity != self.file_identity:
            raise SidecarWriterFenceError("writer_fence_identity_changed")

    def release(self) -> None:
        handle = self.handle
        self.handle = None
        self.file_identity = None
        if handle is None:
            return
        try:
            self._unlock(handle)
        finally:
            handle.close()


class SidecarStateStore:
    """SQLite state store with optimistic CAS and a local rollback anchor."""

    def __init__(
        self,
        root: str | os.PathLike,
        *,
        account_scope_id: str,
        deployment_id: str,
        genesis_id: str,
        writer_id: str,
    ) -> None:
        self.root = Path(root).resolve()
        self.manifest_path = self.root / "account.manifest.json"
        self.fence_path = self.root / "risk-sidecar.writer.lock"
        self.parent_fence_path = self.root / "runtime-parent.writer.lock"
        self.database_path = self.root / "state.sqlite3"
        self.anchor_path = self.root / "rollback-anchor.json"
        self.cash_flow_anchor_path = self.root / "cash-flow-anchor.json"
        self.account_scope_id = str(account_scope_id or "")
        self.deployment_id = str(deployment_id or "")
        self.genesis_id = str(genesis_id or "")
        self.writer_id = str(writer_id or "")
        self.connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self.fence = AccountWriterFence(
            self.fence_path,
            {
                "component": "ChronosHFT.RiskSidecar",
                "account_scope_id": self.account_scope_id,
                "deployment_id": self.deployment_id,
                "writer_id": self.writer_id,
            },
        )
        self.version: StateVersion | None = None
        self.payload: dict = {}

    @classmethod
    def provision(
        cls,
        root: str | os.PathLike,
        *,
        account_scope_id: str,
        deployment_id: str,
        initial_payload: Mapping,
        genesis_id: str | None = None,
    ) -> dict:
        """Create a new lineage; this is intentionally an offline-only API."""
        root_path = Path(root).resolve()
        paths = (
            root_path / "account.manifest.json",
            root_path / "risk-sidecar.writer.lock",
            root_path / "runtime-parent.writer.lock",
            root_path / "state.sqlite3",
            root_path / "rollback-anchor.json",
            root_path / "cash-flow-anchor.json",
        )
        if any(path.exists() for path in paths):
            raise SidecarStateStoreError("state_lineage_already_exists")
        account_scope_id = str(account_scope_id or "").strip()
        deployment_id = str(deployment_id or "").strip()
        if not account_scope_id or not deployment_id:
            raise SidecarStateStoreError("state_identity_missing")
        payload = _validated_state_payload(
            initial_payload,
            account_scope_id=account_scope_id,
            deployment_id=deployment_id,
        )
        root_path.mkdir(parents=True, exist_ok=True)
        genesis_id = str(genesis_id or secrets.token_hex(16))
        manifest_payload = {
            "schema_version": SCHEMA_VERSION,
            "account_scope_id": account_scope_id,
            "genesis_id": genesis_id,
            "state_store_filename": "state.sqlite3",
            "fence_filename": "risk-sidecar.writer.lock",
            "parent_fence_filename": "runtime-parent.writer.lock",
            "cash_flow_anchor_filename": "cash-flow-anchor.json",
            "created_at": time.time(),
        }
        manifest = {
            **manifest_payload,
            "manifest_sha256": _sha256(manifest_payload),
        }
        with open(paths[0], "x", encoding="utf-8") as handle:
            json.dump(manifest, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        AccountWriterFence.provision(paths[1])
        AccountWriterFence.provision(paths[2])
        connection = sqlite3.connect(paths[3])
        try:
            cls._initialize_schema(connection)
            version_data = {
                "writer_epoch": 0,
                "owner_epoch": 0,
                "safety_epoch": 0,
                "generation": 0,
            }
            state_hash = cls._state_hash(version_data, payload)
            connection.execute(
                """
                INSERT INTO state_head (
                    singleton, head_revision, account_scope_id, genesis_id,
                    deployment_id, writer_epoch, owner_epoch, safety_epoch,
                    generation, state_sha256, prev_state_sha256, payload_json,
                    last_event, last_writer_id
                ) VALUES (1, 0, ?, ?, ?, 0, 0, 0, 0, ?, '', ?, ?, '')
                """,
                (
                    account_scope_id,
                    genesis_id,
                    deployment_id,
                    state_hash,
                    _canonical_bytes(payload).decode("ascii"),
                    "state_provisioned",
                ),
            )
            connection.execute(
                """
                INSERT INTO state_history (
                    head_revision, writer_epoch, owner_epoch, safety_epoch,
                    generation, state_sha256, prev_state_sha256, payload_json,
                    event, writer_id, committed_at
                ) VALUES (0, 0, 0, 0, 0, ?, '', ?, ?, '', ?)
                """,
                (
                    state_hash,
                    _canonical_bytes(payload).decode("ascii"),
                    "state_provisioned",
                    time.time(),
                ),
            )
            ledger_start_ms = max(
                0,
                int(payload.get("cash_flow_deployment_start_ms", 0) or 0),
            )
            ledger_hash = cls._ledger_hash(
                previous_hash="",
                generation=0,
                start_time_ms=ledger_start_ms,
                complete_through_ms=ledger_start_ms - 1,
                event_count=0,
                total_amount=0.0,
            )
            connection.execute(
                """
                INSERT INTO cash_flow_cursor (
                    scope, start_time_ms, complete_through_ms, generation,
                    ledger_sha256
                ) VALUES ('external', ?, ?, 0, ?)
                """,
                (ledger_start_ms, ledger_start_ms - 1, ledger_hash),
            )
            connection.execute(
                """
                INSERT INTO cash_flow_history (
                    scope, generation, start_time_ms, complete_through_ms,
                    event_count, total_amount, prev_ledger_sha256,
                    ledger_sha256, committed_at
                ) VALUES ('external', 0, ?, ?, 0, 0, '', ?, ?)
                """,
                (
                    ledger_start_ms,
                    ledger_start_ms - 1,
                    ledger_hash,
                    time.time(),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        target = {
            **version_data,
            "head_revision": 0,
            "state_sha256": state_hash,
        }
        _write_json_atomic(
            paths[4],
            {
                "schema_version": SCHEMA_VERSION,
                "phase": "COMMITTED",
                "operation_class": _SAFETY_INCREASING,
                "base_head": None,
                "target_head": target,
                "target_state": payload,
                "sha256": _sha256({"target_head": target, "target_state": payload}),
            },
        )
        ledger_target = {
            "scope": "external",
            "generation": 0,
            "start_time_ms": ledger_start_ms,
            "complete_through_ms": ledger_start_ms - 1,
            "event_count": 0,
            "total_amount": 0.0,
            "ledger_sha256": ledger_hash,
        }
        _write_json_atomic(
            paths[5],
            {
                "schema_version": SCHEMA_VERSION,
                "phase": "COMMITTED",
                "target": ledger_target,
                "sha256": _sha256(ledger_target),
            },
        )
        return {
            "genesis_id": genesis_id,
            "manifest_sha256": manifest["manifest_sha256"],
            "state_sha256": state_hash,
        }

    @staticmethod
    def _initialize_schema(connection: sqlite3.Connection) -> None:
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE state_head (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                head_revision INTEGER NOT NULL,
                account_scope_id TEXT NOT NULL,
                genesis_id TEXT NOT NULL,
                deployment_id TEXT NOT NULL,
                writer_epoch INTEGER NOT NULL,
                owner_epoch INTEGER NOT NULL,
                safety_epoch INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                state_sha256 TEXT NOT NULL,
                prev_state_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                last_event TEXT NOT NULL,
                last_writer_id TEXT NOT NULL
            );
            CREATE TABLE state_history (
                head_revision INTEGER PRIMARY KEY,
                writer_epoch INTEGER NOT NULL,
                owner_epoch INTEGER NOT NULL,
                safety_epoch INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                state_sha256 TEXT NOT NULL,
                prev_state_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                event TEXT NOT NULL,
                writer_id TEXT NOT NULL,
                committed_at REAL NOT NULL
            );
            CREATE TABLE command_receipt (
                request_id TEXT PRIMARY KEY,
                command_type TEXT NOT NULL,
                request_sha256 TEXT NOT NULL,
                result_json TEXT NOT NULL,
                committed_generation INTEGER NOT NULL
            );
            CREATE TABLE cash_flow_event (
                event_id TEXT PRIMARY KEY,
                event_time_ms INTEGER NOT NULL,
                asset TEXT NOT NULL,
                amount REAL NOT NULL,
                raw_sha256 TEXT NOT NULL UNIQUE
            );
            CREATE TABLE cash_flow_cursor (
                scope TEXT PRIMARY KEY,
                start_time_ms INTEGER NOT NULL,
                complete_through_ms INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                ledger_sha256 TEXT NOT NULL
            );
            CREATE TABLE cash_flow_history (
                scope TEXT NOT NULL,
                generation INTEGER NOT NULL,
                start_time_ms INTEGER NOT NULL,
                complete_through_ms INTEGER NOT NULL,
                event_count INTEGER NOT NULL,
                total_amount REAL NOT NULL,
                prev_ledger_sha256 TEXT NOT NULL,
                ledger_sha256 TEXT NOT NULL,
                committed_at REAL NOT NULL,
                PRIMARY KEY (scope, generation)
            );
            """
        )

    @staticmethod
    def _state_hash(version_data: Mapping, payload: Mapping) -> str:
        return _sha256({"version": dict(version_data), "payload": dict(payload)})

    @staticmethod
    def _ledger_hash(
        *,
        previous_hash: str,
        generation: int,
        start_time_ms: int,
        complete_through_ms: int,
        event_count: int,
        total_amount: float,
    ) -> str:
        return _sha256(
            {
                "previous_hash": str(previous_hash),
                "generation": int(generation),
                "start_time_ms": int(start_time_ms),
                "complete_through_ms": int(complete_through_ms),
                "event_count": int(event_count),
                "total_amount": float(total_amount),
            }
        )

    @staticmethod
    def _version_from_row(row: sqlite3.Row) -> StateVersion:
        return StateVersion(
            writer_epoch=int(row["writer_epoch"]),
            owner_epoch=int(row["owner_epoch"]),
            safety_epoch=int(row["safety_epoch"]),
            generation=int(row["generation"]),
            state_sha256=str(row["state_sha256"]),
        )

    def _read_head(self) -> sqlite3.Row:
        if self.connection is None:
            raise SidecarStateStoreError("state_store_not_open")
        row = self.connection.execute(
            "SELECT * FROM state_head WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise SidecarStateStoreError("state_head_missing")
        return row

    def open_recover(self) -> tuple[dict, StateVersion]:
        """Open an existing lineage and acquire a new writer epoch."""
        for path, label in (
            (self.manifest_path, "manifest"),
            (self.fence_path, "writer_fence"),
            (self.parent_fence_path, "parent_writer_fence"),
            (self.database_path, "state_database"),
            (self.anchor_path, "rollback_anchor"),
            (self.cash_flow_anchor_path, "cash_flow_anchor"),
        ):
            if not path.is_file():
                raise SidecarStateStoreError(f"{label}_missing")
        manifest = _read_json_object(self.manifest_path, "manifest")
        manifest_digest = str(manifest.pop("manifest_sha256", "") or "")
        if _sha256(manifest) != manifest_digest:
            raise SidecarStateStoreError("manifest_checksum_mismatch")
        if int(manifest.get("schema_version", 0) or 0) != SCHEMA_VERSION:
            raise SidecarStateStoreError("manifest_schema_unsupported")
        if manifest.get("account_scope_id") != self.account_scope_id:
            raise SidecarStateStoreError("manifest_account_scope_mismatch")
        if manifest.get("genesis_id") != self.genesis_id:
            raise SidecarStateStoreError("manifest_genesis_mismatch")

        self.fence.acquire()
        try:
            uri = self.database_path.as_uri() + "?mode=rw"
            connection = sqlite3.connect(
                uri,
                uri=True,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise SidecarStateStoreError("state_database_integrity_failed")
            self.connection = connection
            self._validate_schema()
            row = self._read_head()
            if row["account_scope_id"] != self.account_scope_id:
                raise SidecarStateStoreError("state_account_scope_mismatch")
            if row["genesis_id"] != self.genesis_id:
                raise SidecarStateStoreError("state_genesis_mismatch")
            if row["deployment_id"] != self.deployment_id:
                raise SidecarStateStoreError("state_deployment_mismatch")
            payload = _validated_state_payload(
                json.loads(row["payload_json"]),
                account_scope_id=self.account_scope_id,
                deployment_id=self.deployment_id,
            )
            current = self._version_from_row(row)
            expected_hash = self._state_hash(
                {
                    "writer_epoch": current.writer_epoch,
                    "owner_epoch": current.owner_epoch,
                    "safety_epoch": current.safety_epoch,
                    "generation": current.generation,
                },
                payload,
            )
            if current.state_sha256 != expected_hash:
                raise SidecarStateStoreError("state_checksum_mismatch")
            self._validate_history(row)
            self._validate_anchor(row, payload)
            self._validate_cash_flow_ledger()
            target = self._cas(
                current,
                payload,
                event="writer_epoch_acquired",
                operation_class=_NEUTRAL,
                writer_epoch=current.writer_epoch + 1,
            )
            self.payload = dict(payload)
            self.version = target
            return dict(payload), target
        except Exception:
            self.close()
            raise

    def _validate_schema(self) -> None:
        if self.connection is None:
            raise SidecarStateStoreError("state_store_not_open")
        user_version = int(
            self.connection.execute("PRAGMA user_version").fetchone()[0]
        )
        if user_version != SCHEMA_VERSION:
            raise SidecarStateStoreError("state_database_schema_unsupported")
        expected_tables = {
            "state_head",
            "state_history",
            "command_receipt",
            "cash_flow_event",
            "cash_flow_cursor",
            "cash_flow_history",
        }
        actual_tables = {
            str(row[0])
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not expected_tables <= actual_tables:
            raise SidecarStateStoreError("state_database_schema_incomplete")

    def _validate_history(self, head: sqlite3.Row) -> None:
        if self.connection is None:
            raise SidecarStateStoreError("state_store_not_open")
        rows = self.connection.execute(
            "SELECT * FROM state_history ORDER BY head_revision"
        ).fetchall()
        if not rows or int(rows[0]["head_revision"]) != 0:
            raise SidecarStateStoreError("state_history_genesis_missing")
        previous_hash = ""
        previous_revision = -1
        for row in rows:
            revision = int(row["head_revision"])
            if revision != previous_revision + 1:
                raise SidecarStateStoreError("state_history_revision_gap")
            if str(row["prev_state_sha256"]) != previous_hash:
                raise SidecarStateStoreError("state_history_chain_mismatch")
            try:
                payload = _validated_state_payload(
                    json.loads(row["payload_json"]),
                    account_scope_id=self.account_scope_id,
                    deployment_id=self.deployment_id,
                )
            except (TypeError, ValueError) as exc:
                raise SidecarStateStoreError(
                    "state_history_payload_invalid"
                ) from exc
            if not isinstance(payload, dict):
                raise SidecarStateStoreError("state_history_payload_invalid")
            expected_hash = self._state_hash(
                {
                    "writer_epoch": int(row["writer_epoch"]),
                    "owner_epoch": int(row["owner_epoch"]),
                    "safety_epoch": int(row["safety_epoch"]),
                    "generation": int(row["generation"]),
                },
                payload,
            )
            state_hash = str(row["state_sha256"])
            if state_hash != expected_hash:
                raise SidecarStateStoreError(
                    "state_history_checksum_mismatch"
                )
            previous_hash = state_hash
            previous_revision = revision
        if (
            previous_revision != int(head["head_revision"])
            or previous_hash != str(head["state_sha256"])
        ):
            raise SidecarStateStoreError("state_history_head_mismatch")

    def _validate_anchor(self, row: sqlite3.Row, payload: Mapping) -> None:
        anchor = _read_json_object(self.anchor_path, "rollback_anchor")
        if int(anchor.get("schema_version", 0) or 0) != SCHEMA_VERSION:
            raise SidecarStateStoreError("rollback_anchor_schema_unsupported")
        target = anchor.get("target_head")
        target_state = anchor.get("target_state")
        if not isinstance(target, dict) or not isinstance(target_state, dict):
            raise SidecarStateStoreError("rollback_anchor_payload_invalid")
        anchor_digest = str(anchor.get("sha256", "") or "")
        if anchor_digest != _sha256(
            {"target_head": target, "target_state": target_state}
        ):
            raise SidecarStateStoreError("rollback_anchor_checksum_mismatch")
        database_target = {
            "writer_epoch": int(row["writer_epoch"]),
            "owner_epoch": int(row["owner_epoch"]),
            "safety_epoch": int(row["safety_epoch"]),
            "generation": int(row["generation"]),
            "head_revision": int(row["head_revision"]),
            "state_sha256": str(row["state_sha256"]),
        }
        if target != database_target or target_state != dict(payload):
            raise SidecarStateStoreError("rollback_or_split_brain_suspected")
        if anchor.get("phase") != "COMMITTED":
            raise SidecarStateStoreError("rollback_anchor_not_committed")

    def _validate_cash_flow_ledger(self) -> None:
        if self.connection is None:
            raise SidecarStateStoreError("state_store_not_open")
        cursor = self.connection.execute(
            "SELECT * FROM cash_flow_cursor WHERE scope = 'external'"
        ).fetchone()
        if cursor is None:
            raise SidecarStateStoreError("cash_flow_cursor_missing")
        rows = self.connection.execute(
            "SELECT * FROM cash_flow_history WHERE scope = 'external' "
            "ORDER BY generation"
        ).fetchall()
        if not rows or int(rows[0]["generation"]) != 0:
            raise SidecarStateStoreError("cash_flow_history_genesis_missing")
        previous_hash = ""
        previous_generation = -1
        for row in rows:
            generation = int(row["generation"])
            if generation != previous_generation + 1:
                raise SidecarStateStoreError("cash_flow_history_generation_gap")
            if str(row["prev_ledger_sha256"]) != previous_hash:
                raise SidecarStateStoreError("cash_flow_history_chain_mismatch")
            expected_hash = self._ledger_hash(
                previous_hash=previous_hash,
                generation=generation,
                start_time_ms=int(row["start_time_ms"]),
                complete_through_ms=int(row["complete_through_ms"]),
                event_count=int(row["event_count"]),
                total_amount=float(row["total_amount"]),
            )
            if str(row["ledger_sha256"]) != expected_hash:
                raise SidecarStateStoreError(
                    "cash_flow_history_checksum_mismatch"
                )
            previous_hash = expected_hash
            previous_generation = generation
        event_count, total_amount = self.connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM cash_flow_event"
        ).fetchone()
        target = {
            "scope": "external",
            "generation": int(cursor["generation"]),
            "start_time_ms": int(cursor["start_time_ms"]),
            "complete_through_ms": int(cursor["complete_through_ms"]),
            "event_count": int(event_count),
            "total_amount": float(total_amount),
            "ledger_sha256": str(cursor["ledger_sha256"]),
        }
        if (
            previous_generation != target["generation"]
            or previous_hash != target["ledger_sha256"]
            or int(rows[-1]["event_count"]) != target["event_count"]
            or float(rows[-1]["total_amount"]) != target["total_amount"]
        ):
            raise SidecarStateStoreError("cash_flow_history_head_mismatch")
        anchor = _read_json_object(
            self.cash_flow_anchor_path,
            "cash_flow_anchor",
        )
        if int(anchor.get("schema_version", 0) or 0) != SCHEMA_VERSION:
            raise SidecarStateStoreError("cash_flow_anchor_schema_unsupported")
        if anchor.get("phase") != "COMMITTED":
            raise SidecarStateStoreError("cash_flow_anchor_not_committed")
        if anchor.get("target") != target:
            raise SidecarStateStoreError("cash_flow_rollback_suspected")
        if anchor.get("sha256") != _sha256(target):
            raise SidecarStateStoreError("cash_flow_anchor_checksum_mismatch")

    @staticmethod
    def _head_dict(row: sqlite3.Row) -> dict:
        return {
            "writer_epoch": int(row["writer_epoch"]),
            "owner_epoch": int(row["owner_epoch"]),
            "safety_epoch": int(row["safety_epoch"]),
            "generation": int(row["generation"]),
            "head_revision": int(row["head_revision"]),
            "state_sha256": str(row["state_sha256"]),
        }

    def compare_and_swap(
        self,
        expected: StateVersion,
        payload: Mapping,
        *,
        event: str,
        operation_class: str = _NEUTRAL,
        owner_epoch: int | None = None,
        safety_epoch: int | None = None,
    ) -> StateVersion:
        return self._cas(
            expected,
            payload,
            event=event,
            operation_class=operation_class,
            owner_epoch=owner_epoch,
            safety_epoch=safety_epoch,
        )

    def _cas(
        self,
        expected: StateVersion,
        payload: Mapping,
        *,
        event: str,
        operation_class: str,
        writer_epoch: int | None = None,
        owner_epoch: int | None = None,
        safety_epoch: int | None = None,
    ) -> StateVersion:
        with self._lock:
            return self._cas_locked(
                expected,
                payload,
                event=event,
                operation_class=operation_class,
                writer_epoch=writer_epoch,
                owner_epoch=owner_epoch,
                safety_epoch=safety_epoch,
            )

    def _cas_locked(
        self,
        expected: StateVersion,
        payload: Mapping,
        *,
        event: str,
        operation_class: str,
        writer_epoch: int | None = None,
        owner_epoch: int | None = None,
        safety_epoch: int | None = None,
    ) -> StateVersion:
        if operation_class not in OPERATION_CLASSES:
            raise ValueError("operation_class_invalid")
        self.fence.validate()
        row = self._read_head()
        current = self._version_from_row(row)
        if current != expected:
            raise SidecarStateCasError("state_cas_conflict")
        writer_epoch = (
            current.writer_epoch if writer_epoch is None else int(writer_epoch)
        )
        owner_epoch = current.owner_epoch if owner_epoch is None else int(owner_epoch)
        safety_epoch = (
            current.safety_epoch if safety_epoch is None else int(safety_epoch)
        )
        generation = current.generation + 1
        version_data = {
            "writer_epoch": writer_epoch,
            "owner_epoch": owner_epoch,
            "safety_epoch": safety_epoch,
            "generation": generation,
        }
        payload = _validated_state_payload(
            payload,
            account_scope_id=self.account_scope_id,
            deployment_id=self.deployment_id,
        )
        target_hash = self._state_hash(version_data, payload)
        target = StateVersion(state_sha256=target_hash, **version_data)
        base_head = self._head_dict(row)
        target_head = {
            **version_data,
            "head_revision": int(row["head_revision"]) + 1,
            "state_sha256": target_hash,
        }
        anchor_base = {
            "schema_version": SCHEMA_VERSION,
            "phase": "PREPARED",
            "operation_class": operation_class,
            "base_head": base_head,
            "target_head": target_head,
            "target_state": payload,
        }
        _write_json_atomic(
            self.anchor_path,
            {
                **anchor_base,
                "sha256": _sha256(
                    {"target_head": target_head, "target_state": payload}
                ),
            },
        )
        connection = self.connection
        if connection is None:
            raise SidecarStateStoreError("state_store_not_open")
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                """
                UPDATE state_head SET
                    head_revision = head_revision + 1,
                    writer_epoch = ?, owner_epoch = ?, safety_epoch = ?,
                    generation = ?, state_sha256 = ?, prev_state_sha256 = ?,
                    payload_json = ?, last_event = ?, last_writer_id = ?
                WHERE singleton = 1 AND head_revision = ?
                  AND writer_epoch = ? AND owner_epoch = ?
                  AND safety_epoch = ? AND generation = ? AND state_sha256 = ?
                """,
                (
                    writer_epoch,
                    owner_epoch,
                    safety_epoch,
                    generation,
                    target_hash,
                    current.state_sha256,
                    _canonical_bytes(payload).decode("ascii"),
                    str(event or "state_changed"),
                    self.writer_id,
                    int(row["head_revision"]),
                    current.writer_epoch,
                    current.owner_epoch,
                    current.safety_epoch,
                    current.generation,
                    current.state_sha256,
                ),
            )
            if result.rowcount != 1:
                raise SidecarStateCasError("state_cas_conflict")
            connection.execute(
                """
                INSERT INTO state_history (
                    head_revision, writer_epoch, owner_epoch, safety_epoch,
                    generation, state_sha256, prev_state_sha256, payload_json,
                    event, writer_id, committed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    target_head["head_revision"],
                    writer_epoch,
                    owner_epoch,
                    safety_epoch,
                    generation,
                    target_hash,
                    current.state_sha256,
                    _canonical_bytes(payload).decode("ascii"),
                    str(event or "state_changed"),
                    self.writer_id,
                    time.time(),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        _write_json_atomic(
            self.anchor_path,
            {
                **anchor_base,
                "phase": "COMMITTED",
                "sha256": _sha256(
                    {"target_head": target_head, "target_state": payload}
                ),
            },
        )
        self.payload = payload
        self.version = target
        return target

    def cash_flow_cursor(self) -> dict:
        with self._lock:
            if self.connection is None:
                raise SidecarStateStoreError("state_store_not_open")
            row = self.connection.execute(
                "SELECT * FROM cash_flow_cursor WHERE scope = 'external'"
            ).fetchone()
            if row is None:
                raise SidecarStateStoreError("cash_flow_cursor_missing")
            return {
                "start_time_ms": int(row["start_time_ms"]),
                "complete_through_ms": int(row["complete_through_ms"]),
                "generation": int(row["generation"]),
                "ledger_sha256": str(row["ledger_sha256"]),
            }

    def cash_flow_total(self, start_time_ms: int, end_time_ms: int) -> float:
        start_time_ms = int(start_time_ms)
        end_time_ms = int(end_time_ms)
        if end_time_ms < start_time_ms:
            return 0.0
        with self._lock:
            if self.connection is None:
                raise SidecarStateStoreError("state_store_not_open")
            value = self.connection.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM cash_flow_event "
                "WHERE event_time_ms >= ? AND event_time_ms <= ?",
                (start_time_ms, end_time_ms),
            ).fetchone()[0]
            return float(value or 0.0)

    def commit_cash_flow_refresh(
        self,
        events,
        *,
        start_time_ms: int,
        complete_through_ms: int,
    ) -> int:
        start_time_ms = int(start_time_ms)
        complete_through_ms = int(complete_through_ms)
        if start_time_ms < 0 or complete_through_ms < start_time_ms:
            raise SidecarStateStoreError("cash_flow_interval_invalid")
        normalized: dict[str, tuple[int, str, float, str]] = {}
        for event in events:
            if not isinstance(event, Mapping):
                raise SidecarStateStoreError("cash_flow_event_invalid")
            event_id = str(event.get("event_id", "") or "")
            asset = str(event.get("asset", "") or "").upper()
            raw_sha256 = str(event.get("raw_sha256", "") or "")
            try:
                event_time_ms = int(event["event_time_ms"])
                amount = float(event["amount"])
            except (KeyError, TypeError, ValueError) as exc:
                raise SidecarStateStoreError("cash_flow_event_invalid") from exc
            if (
                not event_id
                or not asset
                or len(raw_sha256) != 64
                or not event_time_ms >= 0
                or not amount == amount
                or amount in {float("inf"), float("-inf")}
            ):
                raise SidecarStateStoreError("cash_flow_event_invalid")
            value = (event_time_ms, asset, amount, raw_sha256)
            existing = normalized.get(event_id)
            if existing is not None and existing != value:
                raise SidecarStateStoreError("cash_flow_event_identity_collision")
            normalized[event_id] = value

        with self._lock:
            self.fence.validate()
            connection = self.connection
            if connection is None:
                raise SidecarStateStoreError("state_store_not_open")
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "SELECT * FROM cash_flow_cursor WHERE scope = 'external'"
                ).fetchone()
                if cursor is None:
                    raise SidecarStateStoreError("cash_flow_cursor_missing")
                if complete_through_ms < int(cursor["complete_through_ms"]):
                    raise SidecarStateStoreError("cash_flow_cursor_regression")
                for event_id, value in normalized.items():
                    event_time_ms, asset, amount, raw_sha256 = value
                    existing = connection.execute(
                        "SELECT event_time_ms, asset, amount, raw_sha256 "
                        "FROM cash_flow_event WHERE event_id = ?",
                        (event_id,),
                    ).fetchone()
                    if existing is not None:
                        if (
                            int(existing[0]),
                            str(existing[1]),
                            float(existing[2]),
                            str(existing[3]),
                        ) != value:
                            raise SidecarStateStoreError(
                                "cash_flow_event_identity_collision"
                            )
                        continue
                    raw_owner = connection.execute(
                        "SELECT event_id FROM cash_flow_event "
                        "WHERE raw_sha256 = ?",
                        (raw_sha256,),
                    ).fetchone()
                    if raw_owner is not None:
                        raise SidecarStateStoreError(
                            "cash_flow_event_digest_collision"
                        )
                    connection.execute(
                        "INSERT INTO cash_flow_event("
                        "event_id, event_time_ms, asset, amount, raw_sha256"
                        ") VALUES (?, ?, ?, ?, ?)",
                        (event_id, event_time_ms, asset, amount, raw_sha256),
                    )
                event_count, total_amount = connection.execute(
                    "SELECT COUNT(*), COALESCE(SUM(amount), 0) "
                    "FROM cash_flow_event"
                ).fetchone()
                generation = int(cursor["generation"]) + 1
                previous_hash = str(cursor["ledger_sha256"])
                ledger_start_ms = int(cursor["start_time_ms"])
                ledger_hash = self._ledger_hash(
                    previous_hash=previous_hash,
                    generation=generation,
                    start_time_ms=ledger_start_ms,
                    complete_through_ms=complete_through_ms,
                    event_count=int(event_count),
                    total_amount=float(total_amount),
                )
                target = {
                    "scope": "external",
                    "generation": generation,
                    "start_time_ms": ledger_start_ms,
                    "complete_through_ms": complete_through_ms,
                    "event_count": int(event_count),
                    "total_amount": float(total_amount),
                    "ledger_sha256": ledger_hash,
                }
                _write_json_atomic(
                    self.cash_flow_anchor_path,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "phase": "PREPARED",
                        "target": target,
                        "sha256": _sha256(target),
                    },
                )
                connection.execute(
                    "INSERT INTO cash_flow_history("
                    "scope, generation, start_time_ms, complete_through_ms, "
                    "event_count, total_amount, prev_ledger_sha256, "
                    "ledger_sha256, committed_at"
                    ") VALUES ('external', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        generation,
                        ledger_start_ms,
                        complete_through_ms,
                        int(event_count),
                        float(total_amount),
                        previous_hash,
                        ledger_hash,
                        time.time(),
                    ),
                )
                connection.execute(
                    "UPDATE cash_flow_cursor SET complete_through_ms = ?, "
                    "generation = ?, ledger_sha256 = ? "
                    "WHERE scope = 'external' AND generation = ?",
                    (
                        complete_through_ms,
                        generation,
                        ledger_hash,
                        int(cursor["generation"]),
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            _write_json_atomic(
                self.cash_flow_anchor_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "phase": "COMMITTED",
                    "target": target,
                    "sha256": _sha256(target),
                },
            )
            return generation

    def close(self) -> None:
        with self._lock:
            connection = self.connection
            self.connection = None
            if connection is not None:
                connection.close()
            self.fence.release()

    def __enter__(self) -> SidecarStateStore:
        self.open_recover()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()
