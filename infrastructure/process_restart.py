"""Operator-requested process restart.

A restart runs the normal verified shutdown, then replaces the process image
with a fresh interpreter on the same command line. The new process replays
the journal and reconciles against the venue like any cold start, so a
restart is the operator's tool for faults where the running process state
itself is no longer trusted.
"""

from __future__ import annotations

import os
import sys
import threading

# Distinct non-zero status for "restart me": launcher.py and the systemd unit
# both restart on it if the in-place re-exec is unavailable.
RESTART_EXIT_CODE = 75


class RestartRequested(Exception):
    """Raised by the control loop to unwind into main's verified shutdown."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ProcessRestartRequest:
    """Thread-safe one-shot restart request shared by dashboard and loop."""

    def __init__(self):
        self._lock = threading.Lock()
        self._reason = ""

    def request(self, reason: str) -> bool:
        """Record a restart request; return False if one is already pending."""
        reason = str(reason or "").strip() or "operator_restart"
        with self._lock:
            if self._reason:
                return False
            self._reason = reason
            return True

    def pending_reason(self) -> str:
        with self._lock:
            return self._reason

    def clear(self) -> None:
        with self._lock:
            self._reason = ""

    def raise_if_requested(self) -> None:
        reason = self.pending_reason()
        if reason:
            raise RestartRequested(reason)


PROCESS_RESTART = ProcessRestartRequest()


def reexec_current_process(execv=os.execv, *, reason: str = "") -> int:
    """Replace this process with a fresh interpreter on the same argv.

    Call only after a verified shutdown: an unverified teardown keeps the
    operator in the loop like any other failed shutdown. Returns
    RESTART_EXIT_CODE only if the exec itself failed, so an outer
    supervisor can restart the process instead.
    """
    print(f"ChronosHFT restarting: {reason or 'operator_restart'}", flush=True)
    argv = [sys.executable, *sys.argv]
    try:
        execv(sys.executable, argv)
    except OSError:
        return RESTART_EXIT_CODE
    return RESTART_EXIT_CODE
