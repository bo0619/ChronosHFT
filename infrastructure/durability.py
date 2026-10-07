"""Shared failure contract and helpers for durable runtime state."""

import os


class DurabilityError(RuntimeError):
    """Base class for failures at a durable-state boundary."""


def sync_directory(path) -> None:
    """Best-effort fsync of a directory so a completed rename survives a crash."""
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
