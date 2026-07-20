import json
import os
import socket
import time


class SingleWriterFenceError(RuntimeError):
    """Raised when another process already owns the OMS writer lease."""


class SingleWriterFence:
    """Process-scoped exclusive lock backed by an operating-system file lock."""

    def __init__(self, path: str, owner_metadata=None):
        self.path = os.path.abspath(path)
        self.owner_metadata = dict(owner_metadata or {})
        self.handle = None
        self.acquired_at = 0.0

    def _lock(self, handle):
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self, handle):
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _read_owner(handle) -> dict:
        try:
            handle.seek(1)
            payload = handle.read().decode("utf-8", errors="replace").strip()
            return json.loads(payload) if payload else {}
        except (OSError, ValueError, TypeError):
            return {}

    def acquire(self) -> bool:
        if self.handle is not None:
            return True

        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())

            self._lock(handle)
        except OSError as exc:
            owner = self._read_owner(handle)
            handle.close()
            owner_text = json.dumps(owner, ensure_ascii=True, sort_keys=True)
            raise SingleWriterFenceError(
                f"OMS single-writer fence is already held at {self.path}; "
                f"owner={owner_text or '{}'}"
            ) from exc

        self.acquired_at = time.time()
        metadata = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "acquired_at": self.acquired_at,
            **self.owner_metadata,
        }
        encoded = json.dumps(
            metadata,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        handle.seek(1)
        handle.truncate(1)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
        self.handle = handle
        return True

    def release(self) -> bool:
        handle = self.handle
        if handle is None:
            return False
        self.handle = None
        try:
            self._unlock(handle)
        finally:
            handle.close()
        return True

    def health_snapshot(self) -> dict:
        return {
            "held": self.handle is not None,
            "path": self.path,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "acquired_at": self.acquired_at,
        }

    def __del__(self):
        try:
            self.release()
        except Exception:
            pass
