"""Exchange-clock policy used by the independent Binance risk sidecar."""

from __future__ import annotations

import math
import statistics
import time
from typing import Callable, Protocol


class BinanceSidecarClockOwner(Protocol):
    """Exchange adapter state and compatibility callbacks for clock policy."""

    rest: object
    clock_sync_enabled: bool
    clock_sync_interval_sec: float
    clock_sample_count: int
    clock_min_successful_samples: int
    clock_low_rtt_sample_count: int
    clock_sample_spacing_ms: float
    clock_max_rtt_ms: float
    clock_max_uncertainty_ms: float
    clock_max_offset_dispersion_ms: float
    clock_max_wall_step_ms: float
    clock_max_initial_offset_ms: float
    clock_reduce_only_phase_error_ms: float
    clock_kill_phase_error_ms: float
    last_clock_sync_monotonic: float
    clock_offset_ms: float
    clock_phase_error_ms: float
    clock_rtt_ms: float
    clock_uncertainty_ms: float
    clock_offset_dispersion_ms: float
    _clock_anchor_epoch_ms: float
    _clock_anchor_monotonic: float
    clock_reason: str

    def _response_payload(self, response, expected_type, label: str): ...

    def _collect_clock_samples(self, *, emergency: bool = False): ...

    def sync_exchange_clock(self, *, emergency: bool = False): ...


class BinanceSidecarClock:
    """Validate clock samples and maintain a monotonic exchange-time anchor."""

    __slots__ = ("_finite_float", "_owner")

    def __init__(
        self,
        owner: BinanceSidecarClockOwner,
        finite_float: Callable[[object, str], float],
    ):
        self._owner = owner
        self._finite_float = finite_float

    def collect_samples(self, *, emergency: bool = False):
        owner = self._owner
        sample_count = max(
            1,
            int(getattr(owner, "clock_sample_count", 1)),
        )
        min_samples = max(
            1,
            min(
                sample_count,
                int(
                    getattr(
                        owner,
                        "clock_min_successful_samples",
                        1,
                    )
                ),
            ),
        )
        low_rtt_count = max(
            1,
            min(
                sample_count,
                int(getattr(owner, "clock_low_rtt_sample_count", 1)),
            ),
        )
        spacing_ms = max(
            0.0,
            float(
                getattr(owner, "clock_sample_spacing_ms", 0.0) or 0.0
            ),
        )
        max_wall_step_ms = max(
            0.0,
            float(
                getattr(owner, "clock_max_wall_step_ms", 20.0) or 0.0
            ),
        )
        samples = []
        errors = []
        for index in range(sample_count):
            started_monotonic = time.perf_counter()
            started_ms = time.time() * 1000.0
            if emergency:
                server_time_response = owner.rest.get_server_time(
                    emergency=True
                )
            else:
                server_time_response = owner.rest.get_server_time()
            ok, payload, reason = owner._response_payload(
                server_time_response,
                dict,
                "server_time",
            )
            finished_ms = time.time() * 1000.0
            finished_monotonic = time.perf_counter()
            if not ok:
                errors.append(reason)
            else:
                try:
                    server_time_ms = float(payload["serverTime"])
                except (KeyError, TypeError, ValueError):
                    errors.append("server_time_payload_invalid")
                else:
                    if not math.isfinite(server_time_ms):
                        errors.append("server_time_non_finite")
                    elif server_time_ms <= 0.0:
                        errors.append("server_time_non_positive")
                    else:
                        rtt_ms = max(
                            0.0,
                            (
                                finished_monotonic - started_monotonic
                            )
                            * 1000.0,
                        )
                        wall_step_ms = finished_ms - started_ms - rtt_ms
                        if (
                            max_wall_step_ms > 0.0
                            and abs(wall_step_ms) >= max_wall_step_ms
                        ):
                            errors.append(
                                f"clock_wall_step:{wall_step_ms:.3f}ms"
                            )
                        else:
                            samples.append(
                                {
                                    "offset_ms": server_time_ms
                                    - (started_ms + rtt_ms / 2.0),
                                    "rtt_ms": rtt_ms,
                                }
                            )
            if index + 1 < sample_count and spacing_ms > 0.0:
                time.sleep(spacing_ms / 1000.0)
        if len(samples) < min_samples:
            reason = errors[-1] if errors else "clock_sample_quorum_failed"
            return None, reason
        selected = sorted(samples, key=lambda item: item["rtt_ms"])[
            : min(len(samples), low_rtt_count)
        ]
        offsets = [sample["offset_ms"] for sample in selected]
        offset_ms = float(statistics.median(offsets))
        rtt_ms = float(
            statistics.median(
                sample["rtt_ms"] for sample in selected
            )
        )
        dispersion_ms = float(
            statistics.median(
                abs(offset - offset_ms) for offset in offsets
            )
        )
        return {
            "offset_ms": offset_ms,
            "rtt_ms": rtt_ms,
            "dispersion_ms": dispersion_ms,
            "uncertainty_ms": rtt_ms / 2.0 + dispersion_ms,
        }, ""

    def sync(self, *, emergency: bool = False):
        from infrastructure.time_service import time_service

        owner = self._owner
        sample, reason = owner._collect_clock_samples(
            emergency=emergency,
        )
        if sample is None:
            owner.clock_reason = reason
            return False, reason
        max_rtt_ms = max(
            0.0,
            float(getattr(owner, "clock_max_rtt_ms", 200.0) or 0.0),
        )
        max_uncertainty_ms = max(
            0.0,
            float(
                getattr(owner, "clock_max_uncertainty_ms", 50.0) or 0.0
            ),
        )
        max_dispersion_ms = max(
            0.0,
            float(
                getattr(
                    owner,
                    "clock_max_offset_dispersion_ms",
                    10.0,
                )
                or 0.0
            ),
        )
        if max_rtt_ms > 0.0 and sample["rtt_ms"] >= max_rtt_ms:
            reason = f"clock_rtt_exceeded:{sample['rtt_ms']:.3f}ms"
            owner.clock_reason = reason
            return False, reason
        if (
            max_uncertainty_ms > 0.0
            and sample["uncertainty_ms"] >= max_uncertainty_ms
        ):
            reason = (
                "clock_uncertainty_exceeded:"
                f"{sample['uncertainty_ms']:.3f}ms"
            )
            owner.clock_reason = reason
            return False, reason
        if (
            max_dispersion_ms > 0.0
            and sample["dispersion_ms"] >= max_dispersion_ms
        ):
            reason = (
                "clock_dispersion_exceeded:"
                f"{sample['dispersion_ms']:.3f}ms"
            )
            owner.clock_reason = reason
            return False, reason

        offset_ms = float(sample["offset_ms"])
        anchor_monotonic = time.perf_counter()
        anchor_wall_ms = time.time() * 1000.0
        anchor_epoch_ms = anchor_wall_ms + offset_ms
        if not all(
            math.isfinite(value)
            for value in (offset_ms, anchor_monotonic, anchor_epoch_ms)
        ):
            reason = "clock_anchor_non_finite"
            owner.clock_reason = reason
            return False, reason

        previous_anchor_epoch_ms = float(
            getattr(owner, "_clock_anchor_epoch_ms", 0.0) or 0.0
        )
        previous_anchor_monotonic = float(
            getattr(owner, "_clock_anchor_monotonic", 0.0) or 0.0
        )
        if (
            previous_anchor_epoch_ms > 0.0
            and previous_anchor_monotonic > 0.0
        ):
            monotonic_elapsed_ms = (
                anchor_monotonic - previous_anchor_monotonic
            ) * 1000.0
            if (
                not math.isfinite(monotonic_elapsed_ms)
                or monotonic_elapsed_ms < 0.0
            ):
                reason = "clock_monotonic_regressed"
                owner.clock_reason = reason
                return False, reason
            expected_epoch_ms = (
                previous_anchor_epoch_ms + monotonic_elapsed_ms
            )
            phase_error_ms = anchor_epoch_ms - expected_epoch_ms
            if not math.isfinite(phase_error_ms):
                reason = "clock_phase_error_non_finite"
                owner.clock_reason = reason
                return False, reason
        else:
            max_initial_offset_ms = max(
                0.0,
                float(
                    getattr(
                        owner,
                        "clock_max_initial_offset_ms",
                        5000.0,
                    )
                    or 0.0
                ),
            )
            if (
                max_initial_offset_ms > 0.0
                and abs(offset_ms) >= max_initial_offset_ms
            ):
                reason = (
                    f"clock_initial_offset_exceeded:{offset_ms:.3f}ms"
                )
                owner.clock_reason = reason
                return False, reason
            phase_error_ms = 0.0

        try:
            reduce_only_phase_error_ms = max(
                0.0,
                self._finite_float(
                    getattr(
                        owner,
                        "clock_reduce_only_phase_error_ms",
                        25.0,
                    )
                    or 0.0,
                    "clock_reduce_only_phase_error_ms",
                ),
            )
            kill_phase_error_ms = max(
                reduce_only_phase_error_ms,
                self._finite_float(
                    getattr(
                        owner,
                        "clock_kill_phase_error_ms",
                        100.0,
                    )
                    or 0.0,
                    "clock_kill_phase_error_ms",
                ),
            )
        except ValueError:
            reason = "clock_phase_threshold_invalid"
            owner.clock_reason = reason
            return False, reason

        # Retain quality telemetry without replacing the last good anchor
        # when the candidate already requires an independent risk action.
        owner.clock_phase_error_ms = phase_error_ms
        owner.clock_rtt_ms = float(sample["rtt_ms"])
        owner.clock_uncertainty_ms = float(sample["uncertainty_ms"])
        owner.clock_offset_dispersion_ms = float(sample["dispersion_ms"])
        if (
            kill_phase_error_ms > 0.0
            and abs(phase_error_ms) >= kill_phase_error_ms
        ):
            reason = f"clock_phase_error_kill:{phase_error_ms:.3f}ms"
            owner.clock_reason = reason
            return False, reason
        if (
            reduce_only_phase_error_ms > 0.0
            and abs(phase_error_ms) >= reduce_only_phase_error_ms
        ):
            reason = (
                f"clock_phase_error_reduce_only:{phase_error_ms:.3f}ms"
            )
            owner.clock_reason = reason
            return False, reason

        owner.clock_offset_ms = offset_ms
        owner._clock_anchor_epoch_ms = anchor_epoch_ms
        owner._clock_anchor_monotonic = anchor_monotonic
        time_service.offset = owner.clock_offset_ms
        time_service.last_sync_time = anchor_wall_ms / 1000.0
        time_service.last_rtt_ms = owner.clock_rtt_ms
        time_service.last_error = ""
        owner.last_clock_sync_monotonic = anchor_monotonic
        owner.clock_reason = ""
        return True, ""

    def ensure(self, force: bool = False):
        owner = self._owner
        if not getattr(owner, "clock_sync_enabled", False):
            return True, ""
        age = max(
            0.0,
            time.perf_counter()
            - float(
                getattr(owner, "last_clock_sync_monotonic", 0.0) or 0.0
            ),
        )
        if (
            not force
            and owner.last_clock_sync_monotonic > 0.0
            and age <= owner.clock_sync_interval_sec
            and not str(getattr(owner, "clock_reason", "") or "")
        ):
            return True, ""
        if force:
            return owner.sync_exchange_clock(emergency=True)
        return owner.sync_exchange_clock()

    def corrected_epoch_at(self, observed_monotonic: float):
        owner = self._owner
        try:
            observed_monotonic = float(observed_monotonic)
            anchor_monotonic = float(owner._clock_anchor_monotonic)
            anchor_epoch_ms = float(owner._clock_anchor_epoch_ms)
        except (AttributeError, TypeError, ValueError):
            return None
        if (
            not math.isfinite(observed_monotonic)
            or not math.isfinite(anchor_monotonic)
            or not math.isfinite(anchor_epoch_ms)
            or observed_monotonic < anchor_monotonic
            or anchor_monotonic <= 0.0
            or anchor_epoch_ms <= 0.0
        ):
            return None
        return (
            anchor_epoch_ms
            + (observed_monotonic - anchor_monotonic) * 1000.0
        ) / 1000.0
