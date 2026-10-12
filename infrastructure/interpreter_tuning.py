"""CPython settings for the latency-sensitive runtime process."""

from __future__ import annotations

import gc
import sys

# CPython hands the GIL to a waiting thread only every switch interval
# (5 ms by default). While any thread computes, an event queued for another
# lane waits up to that long; 0.5 ms bounds the wait at a small throughput
# cost.
GIL_SWITCH_INTERVAL_SEC = 0.0005


def tune_interpreter_for_runtime() -> None:
    """Apply once startup is complete and before the control loop runs.

    ``gc.freeze`` moves every object that survived startup (modules, config,
    contracts, caches) into the permanent generation, so later full
    collections no longer stop all threads to rescan them.
    """
    sys.setswitchinterval(GIL_SWITCH_INTERVAL_SEC)
    gc.collect()
    gc.freeze()
