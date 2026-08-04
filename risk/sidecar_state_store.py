"""Account-scoped durable state store for the independent risk sidecar."""

from __future__ import annotations

from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import time
from typing import Mapping

from risk.exchange_port import StateVersion


SCHEMA_VERSION = 2
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
        self.database_path = self.root / "state.sqlite3"
        self.anchor_path = self.root / "rollback-anchor.json"
        self.account_scope_id = str(account_scope_id or "")
        self.deployment_id = str(deployment_id or "")
        self.genesis_id = str(genesis_id or "")
        self.writer_id = str(writer_id or "")
        self.connection: sqlite3.Connection | None = None
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
        root_path.mkdir(parents=True, exist_ok=True)
        paths = (
            root_path / "account.manifest.json",
            root_path / "risk-sidecar.writer.lock",
            root_path / "state.sqlite3",
            root_path / "rollback-anchor.json",
        )
        if any(path.exists() for path in paths):
            raise SidecarStateStoreError("state_lineage_already_exists")
        account_scope_id = str(account_scope_id or "").strip()
        deployment_id = str(deployment_id or "").strip()
        if not account_scope_id or not deployment_id:
            raise SidecarStateStoreError("state_identity_missing")
        genesis_id = str(genesis_id or secrets.token_hex(16))
        manifest_payload = {
            "schema_version": SCHEMA_VERSION,
            "account_scope_id": account_scope_id,
            "genesis_id": genesis_id,
            "state_store_filename": "state.sqlite3",
            "fence_filename": "risk-sidecar.writer.lock",
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
        connection = sqlite3.connect(paths[2])
        try:
            cls._initialize_schema(connection)
            payload = dict(initial_payload)
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
            connection.commit()
        finally:
            connection.close()
        target = {
            **version_data,
            "head_revision": 0,
            "state_sha256": state_hash,
        }
        _write_json_atomic(
            paths[3],
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
        return {
            "genesis_id": genesis_id,
            "manifest_sha256": manifest["manifest_sha256"],
            "state_sha256": state_hash,
        }

    @staticmethod
    def _initialize_schema(connection: sqlite3.Connection) -> None:
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
            """
        )

    @staticmethod
    def _state_hash(version_data: Mapping, payload: Mapping) -> str:
        return _sha256({"version": dict(version_data), "payload": dict(payload)})

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
            (self.database_path, "state_database"),
            (self.anchor_path, "rollback_anchor"),
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
            connection = sqlite3.connect(uri, uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise SidecarStateStoreError("state_database_integrity_failed")
            self.connection = connection
            row = self._read_head()
            if row["account_scope_id"] != self.account_scope_id:
                raise SidecarStateStoreError("state_account_scope_mismatch")
            if row["genesis_id"] != self.genesis_id:
                raise SidecarStateStoreError("state_genesis_mismatch")
            if row["deployment_id"] != self.deployment_id:
                raise SidecarStateStoreError("state_deployment_mismatch")
            payload = json.loads(row["payload_json"])
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
            self._validate_anchor(row, payload)
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
        payload = dict(payload)
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

    def close(self) -> None:
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
