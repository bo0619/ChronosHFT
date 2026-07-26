import math
import statistics
import threading
import time

import requests

from .logger import logger


class TimeService:
    """Exchange-corrected epoch clock backed by a monotonic time source."""

    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_state_lock"):
            return
        self._state_lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._http_session = None
        self._generation = 0
        self._offset_ms = 0.0
        self.active = False
        self.url = "https://fapi.binance.com/fapi/v1/time"
        self.listeners = []

        self.max_offset_ms = 25.0
        self.halt_offset_ms = 100.0
        self.max_initial_offset_ms = 5000.0
        self.max_phase_error_ms = self.max_offset_ms
        self.halt_phase_error_ms = self.halt_offset_ms
        self.max_rtt_ms = 250.0
        self.max_uncertainty_ms = 50.0
        self.max_consecutive_failures = 3
        self.freeze_breach_threshold = 1
        self.halt_breach_threshold = 2
        self.recovery_success_threshold = 1
        self.sync_interval_sec = 10.0
        self.unhealthy_retry_sec = 1.0
        self.sample_count = 5
        self.min_successful_samples = 3
        self.low_rtt_sample_count = 3
        self.sample_spacing_ms = 10.0
        self.request_timeout_sec = 1.0
        self.connection_warmup_timeout_sec = 3.0
        self.max_offset_dispersion_ms = 10.0
        self.max_sync_age_sec = 30.0
        self.max_wall_clock_step_ms = 25.0
        self.health_poll_interval_sec = 0.1

        self.last_sync_time = 0.0
        self.last_rtt_ms = 0.0
        self.last_error = ""
        self.consecutive_failures = 0
        self.freeze_breach_count = 0
        self.halt_breach_count = 0
        self.recovery_success_count = 0
        self._health_state = "unsynchronized"
        self._synchronized = False
        self._ready = False
        self._anchor_epoch_ns = 0
        self._anchor_mono_ns = 0
        self._anchor_wall_ns = 0
        self._anchor_offset_ms = 0.0
        self._last_sync_mono_ns = 0
        self._last_now_ns = 0
        self._last_wall_step_ms = 0.0
        self._last_sample_count = 0
        self._last_selected_sample_count = 0
        self._last_offset_dispersion_ms = 0.0
        self._last_phase_error_ms = 0.0
        self._runtime_fault_reason = "clock has not completed startup calibration"
        self._last_notified_fault = ""

    @property
    def offset(self):
        return self._offset_ms

    @offset.setter
    def offset(self, value):
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("clock offset must be finite")
        # The independent risk sidecar historically writes offset directly.
        # Re-anchor immediately so that operation remains backward compatible.
        mono_ns = self.monotonic_ns()
        wall_ns = self._wall_time_ns()
        with self._state_lock:
            self._offset_ms = value
            self._anchor_mono_ns = mono_ns
            self._anchor_wall_ns = wall_ns
            self._anchor_epoch_ns = wall_ns + int(value * 1_000_000.0)
            self._anchor_offset_ms = value
            self._last_phase_error_ms = 0.0

    def configure(self, config=None):
        config = config or {}
        self.max_offset_ms = self._float_config(
            config,
            "max_offset_ms",
            self.max_offset_ms,
            minimum=0.0,
        )
        self.halt_offset_ms = self._float_config(
            config,
            "halt_offset_ms",
            self.halt_offset_ms,
            minimum=0.0,
        )
        phase_freeze_default = (
            self.max_offset_ms
            if "max_offset_ms" in config
            else self.max_phase_error_ms
        )
        phase_halt_default = (
            self.halt_offset_ms
            if "halt_offset_ms" in config
            else self.halt_phase_error_ms
        )
        self.max_initial_offset_ms = max(
            0.0,
            self._float_config(
                config,
                "max_initial_offset_ms",
                self.max_initial_offset_ms,
                minimum=0.0,
            ),
        )
        self.max_phase_error_ms = max(
            0.0,
            self._float_config(
                config,
                "max_phase_error_ms",
                phase_freeze_default,
                minimum=0.0,
            ),
        )
        self.halt_phase_error_ms = max(
            self.max_phase_error_ms,
            self._float_config(
                config,
                "halt_phase_error_ms",
                phase_halt_default,
                minimum=0.0,
            ),
        )
        # Keep the legacy public attributes as aliases of the effective phase
        # thresholds, not as a second source of truth.
        self.max_offset_ms = self.max_phase_error_ms
        self.halt_offset_ms = self.halt_phase_error_ms
        self.max_rtt_ms = self._float_config(
            config,
            "max_rtt_ms",
            self.max_rtt_ms,
            minimum=0.0,
        )
        self.max_uncertainty_ms = max(
            0.0,
            self._float_config(
                config,
                "max_uncertainty_ms",
                self.max_uncertainty_ms,
                minimum=0.0,
            ),
        )
        self.max_consecutive_failures = self._int_config(
            config, "max_consecutive_failures", self.max_consecutive_failures
        )
        self.freeze_breach_threshold = self._int_config(
            config, "freeze_breach_threshold", self.freeze_breach_threshold
        )
        self.halt_breach_threshold = self._int_config(
            config, "halt_breach_threshold", self.halt_breach_threshold
        )
        self.recovery_success_threshold = self._int_config(
            config, "recovery_success_threshold", self.recovery_success_threshold
        )
        self.sync_interval_sec = max(
            0.05,
            self._float_config(
                config,
                "sync_interval_sec",
                self.sync_interval_sec,
                minimum=0.0,
            ),
        )
        self.unhealthy_retry_sec = max(
            0.05,
            self._float_config(
                config,
                "unhealthy_retry_sec",
                self.unhealthy_retry_sec,
                minimum=0.0,
            ),
        )
        self.sample_count = self._int_config(config, "sample_count", self.sample_count)
        self.min_successful_samples = self._int_config(
            config, "min_successful_samples", self.min_successful_samples
        )
        self.low_rtt_sample_count = self._int_config(
            config, "low_rtt_sample_count", self.low_rtt_sample_count
        )
        self.sample_spacing_ms = max(
            0.0,
            self._float_config(
                config,
                "sample_spacing_ms",
                self.sample_spacing_ms,
                minimum=0.0,
            ),
        )
        self.request_timeout_sec = max(
            0.05,
            self._float_config(
                config,
                "request_timeout_sec",
                self.request_timeout_sec,
                minimum=0.0,
            ),
        )
        self.connection_warmup_timeout_sec = max(
            self.request_timeout_sec,
            self._float_config(
                config,
                "connection_warmup_timeout_sec",
                self.connection_warmup_timeout_sec,
                minimum=0.0,
            ),
        )
        self.max_offset_dispersion_ms = max(
            0.0,
            self._float_config(
                config,
                "max_offset_dispersion_ms",
                self.max_offset_dispersion_ms,
                minimum=0.0,
            ),
        )
        self.max_sync_age_sec = max(
            0.05,
            self._float_config(
                config,
                "max_sync_age_sec",
                self.max_sync_age_sec,
                minimum=0.0,
            ),
        )
        self.max_wall_clock_step_ms = max(
            0.0,
            self._float_config(
                config,
                "max_wall_clock_step_ms",
                self.max_wall_clock_step_ms,
                minimum=0.0,
            ),
        )
        self.health_poll_interval_sec = max(
            0.01,
            self._float_config(
                config,
                "health_poll_interval_sec",
                self.health_poll_interval_sec,
                minimum=0.0,
            ),
        )

    @staticmethod
    def _float_config(config, key, default, *, minimum=None):
        value = config.get(key, default)
        try:
            result = float(default if value is None else value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be a finite number") from exc
        if not math.isfinite(result):
            raise ValueError(f"{key} must be a finite number")
        if minimum is not None and result < minimum:
            raise ValueError(f"{key} must be >= {minimum}")
        return result

    @staticmethod
    def _int_config(config, key, default):
        value = config.get(key, default)
        try:
            numeric = float(default if value is None else value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be a finite non-negative integer") from exc
        if not math.isfinite(numeric) or numeric < 0.0 or not numeric.is_integer():
            raise ValueError(f"{key} must be a finite non-negative integer")
        return max(1, int(numeric))

    def register_listener(self, listener):
        with self._state_lock:
            if listener not in self.listeners:
                self.listeners.append(listener)

    def clear_listeners(self):
        with self._state_lock:
            self.listeners.clear()

    def start(self, testnet=False):
        self.stop()
        self.url = (
            "https://testnet.binancefuture.com/fapi/v1/time"
            if testnet
            else "https://fapi.binance.com/fapi/v1/time"
        )
        http_session = requests.Session()
        with self._state_lock:
            self._generation += 1
            generation = self._generation
            stop_event = threading.Event()
            sync_lock = threading.Lock()
            self._stop_event = stop_event
            self._sync_lock = sync_lock
            self._http_session = http_session
            self.active = True
            self._health_state = "starting"
            self._synchronized = False
            self._ready = False
            self._anchor_epoch_ns = 0
            self._anchor_mono_ns = 0
            self._anchor_wall_ns = 0
            self._last_now_ns = 0
            self._last_sync_mono_ns = 0
            self._last_phase_error_ms = 0.0
            self._runtime_fault_reason = "startup exchange clock calibration pending"
            self._last_notified_fault = ""
            self.consecutive_failures = 0
            self.freeze_breach_count = 0
            self.halt_breach_count = 0
            self.recovery_success_count = 0
        logger.info(f"TimeService connecting to: {self.url}")
        self._warm_up_connection(
            generation=generation,
            stop_event=stop_event,
        )
        initial_sync_ok = self._sync(
            generation=generation,
            stop_event=stop_event,
            sync_lock=sync_lock,
        )
        with self._state_lock:
            current_generation = self._generation == generation
            still_active = self.active and not stop_event.is_set()
            if current_generation and still_active:
                self._thread = threading.Thread(
                    target=self._auto_sync_loop,
                    args=(generation, stop_event, sync_lock),
                    name="exchange-clock",
                    daemon=True,
                )
                thread = self._thread
            else:
                thread = None
        if thread is not None:
            thread.start()
        return initial_sync_ok

    def stop(self):
        with self._state_lock:
            self._generation += 1
            self.active = False
            stop_event = self._stop_event
            stop_event.set()
            self._ready = False
            if self._health_state != "unsynchronized":
                self._health_state = "stopped"
                self._runtime_fault_reason = "time service stopped"
            thread = self._thread
            http_session = self._http_session
            self._http_session = None
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.5, self.request_timeout_sec + 0.25))
        stopped = not thread or not thread.is_alive()
        if http_session is not None:
            try:
                http_session.close()
            except Exception as exc:
                logger.warning(
                    f"TimeService HTTP session close failed: {type(exc).__name__}"
                )
        if stopped:
            with self._state_lock:
                if self._thread is thread:
                    self._thread = None
        return stopped

    def synchronize_now(self):
        """Run an immediate synchronization within the active generation."""

        with self._state_lock:
            if not self.active:
                return False
            generation = self._generation
            stop_event = self._stop_event
            sync_lock = self._sync_lock
        return self._sync(
            generation=generation,
            stop_event=stop_event,
            sync_lock=sync_lock,
        )

    @staticmethod
    def monotonic_ns():
        return time.perf_counter_ns()

    @staticmethod
    def _wall_time_ns():
        return time.time_ns()

    def now_ns(self):
        mono_ns = self.monotonic_ns()
        with self._state_lock:
            return self._corrected_epoch_ns_locked(mono_ns)

    def capture_timestamp(self):
        """Capture wall, monotonic, and corrected exchange time at one ingress point.

        The corrected value is extrapolated from the last exchange calibration
        anchor and the same monotonic sample used for the returned monotonic
        timestamp.  This keeps venue-latency measurements immune to local wall
        clock steps while retaining a wall timestamp for observability.
        """

        mono_ns = self.monotonic_ns()
        wall_ns = self._wall_time_ns()
        with self._state_lock:
            corrected_ns = self._corrected_epoch_ns_locked(mono_ns, wall_ns)
            offset_ms = float(self._offset_ms)
        return (
            wall_ns / 1_000_000_000.0,
            mono_ns / 1_000_000_000.0,
            corrected_ns / 1_000_000_000.0,
            offset_ms,
        )

    def _corrected_epoch_ns_locked(self, mono_ns, wall_ns=None):
        if self._anchor_mono_ns:
            candidate = self._anchor_epoch_ns + (mono_ns - self._anchor_mono_ns)
        else:
            # Diagnostics still receive a timestamp before calibration, but
            # ready remains false so execution can fail closed.
            if wall_ns is None:
                wall_ns = self._wall_time_ns()
            candidate = wall_ns + int(self._offset_ms * 1_000_000.0)
        candidate = max(candidate, self._last_now_ns)
        self._last_now_ns = candidate
        return candidate

    def now(self):
        """Backward-compatible exchange epoch timestamp in milliseconds."""

        return self.now_ns() // 1_000_000

    def now_seconds(self):
        return self.now_ns() / 1_000_000_000.0

    @property
    def ready(self):
        self._check_runtime_health()
        with self._state_lock:
            return bool(self._ready)

    def is_ready(self):
        return self.ready

    def health_snapshot(self, *, notify_listeners=True):
        self._check_runtime_health(notify_listeners=notify_listeners)
        mono_ns = self.monotonic_ns()
        with self._state_lock:
            sync_age_sec = None
            if self._last_sync_mono_ns:
                sync_age_sec = max(
                    0.0,
                    (mono_ns - self._last_sync_mono_ns) / 1_000_000_000.0,
                )
            return {
                "clock_source": "binance_server_time+perf_counter_ns",
                "ready": bool(self._ready),
                "state": self._health_state,
                "synchronized": bool(self._synchronized),
                "offset_ms": float(self.offset),
                "phase_error_ms": float(self._last_phase_error_ms),
                "max_initial_offset_ms": float(self.max_initial_offset_ms),
                "max_phase_error_ms": float(self.max_phase_error_ms),
                "halt_phase_error_ms": float(self.halt_phase_error_ms),
                "rtt_ms": float(self.last_rtt_ms),
                "offset_dispersion_ms": float(self._last_offset_dispersion_ms),
                "estimated_uncertainty_ms": (
                    float(self.last_rtt_ms) / 2.0
                    + float(self._last_offset_dispersion_ms)
                ),
                "max_uncertainty_ms": float(self.max_uncertainty_ms),
                "monotonic_resolution_ns": (
                    time.get_clock_info("perf_counter").resolution
                    * 1_000_000_000.0
                ),
                "sync_age_sec": sync_age_sec,
                "max_sync_age_sec": float(self.max_sync_age_sec),
                "wall_clock_step_ms": float(self._last_wall_step_ms),
                "last_sync_time": float(self.last_sync_time),
                "last_error": self.last_error,
                "consecutive_failures": int(self.consecutive_failures),
                "samples": int(self._last_sample_count),
                "selected_samples": int(self._last_selected_sample_count),
                "reason": self._runtime_fault_reason,
            }

    def _notify(
        self,
        severity,
        reason,
        *,
        expected_generation=None,
        expected_stop_event=None,
        **details,
    ):
        with self._state_lock:
            if expected_generation is not None:
                if (
                    not self.active
                    or expected_generation != self._generation
                    or (
                        expected_stop_event is not None
                        and (
                            expected_stop_event is not self._stop_event
                            or expected_stop_event.is_set()
                        )
                    )
                ):
                    return False
            listeners = list(self.listeners)
        for listener in listeners:
            try:
                listener(severity, reason, details)
            except Exception as exc:
                logger.error(f"TimeService listener failed: {exc}")
        return True

    def _request_server_time_ms(self, *, timeout_sec=None):
        with self._state_lock:
            http_session = self._http_session
        if http_session is None:
            raise RuntimeError("time sync HTTP session is not available")
        timeout = self.request_timeout_sec if timeout_sec is None else float(timeout_sec)
        response = http_session.get(self.url, timeout=timeout)
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        server_time_ms = float(response.json()["serverTime"])
        if not math.isfinite(server_time_ms) or server_time_ms <= 0:
            raise ValueError("invalid serverTime in exchange clock response")
        return server_time_ms

    def _warm_up_connection(self, *, generation, stop_event):
        """Establish the reusable TLS connection outside measured samples."""

        if not self._sync_context_active(generation, stop_event):
            return False
        started_ns = self.monotonic_ns()
        try:
            self._request_server_time_ms(
                timeout_sec=self.connection_warmup_timeout_sec,
            )
        except Exception as exc:
            logger.debug(
                "Time Sync HTTP connection warm-up failed; "
                f"formal sampling will retry: {type(exc).__name__}"
            )
            return False
        if not self._sync_context_active(generation, stop_event):
            return False
        elapsed_ms = max(
            0.0,
            (self.monotonic_ns() - started_ns) / 1_000_000.0,
        )
        logger.info(f"Time Sync HTTP connection warmed in {elapsed_ms:.1f}ms")
        return True

    def _request_sample(self):
        mono_start_ns = self.monotonic_ns()
        wall_start_ns = self._wall_time_ns()
        server_time_ms = self._request_server_time_ms()
        wall_end_ns = self._wall_time_ns()
        mono_end_ns = self.monotonic_ns()
        rtt_ms = max(0.0, (mono_end_ns - mono_start_ns) / 1_000_000.0)
        wall_elapsed_ms = (wall_end_ns - wall_start_ns) / 1_000_000.0
        wall_step_ms = wall_elapsed_ms - rtt_ms
        if (
            self.max_wall_clock_step_ms > 0
            and abs(wall_step_ms) >= self.max_wall_clock_step_ms
        ):
            raise ValueError(
                f"local wall clock stepped during time sample "
                f"({wall_step_ms:.3f}ms)"
            )
        local_midpoint_ms = wall_start_ns / 1_000_000.0 + rtt_ms / 2.0
        return {
            "offset_ms": server_time_ms - local_midpoint_ms,
            "rtt_ms": rtt_ms,
        }

    def _sync_context_active_locked(self, generation, stop_event):
        if generation is None:
            return True
        return bool(
            self.active
            and generation == self._generation
            and stop_event is self._stop_event
            and stop_event is not None
            and not stop_event.is_set()
        )

    def _sync_context_active(self, generation, stop_event):
        with self._state_lock:
            return self._sync_context_active_locked(generation, stop_event)

    def _collect_samples(self, generation=None, stop_event=None):
        samples = []
        errors = []
        for index in range(self.sample_count):
            if not self._sync_context_active(generation, stop_event):
                break
            try:
                sample = self._request_sample()
            except Exception as exc:
                errors.append(str(exc))
            else:
                if not self._sync_context_active(generation, stop_event):
                    break
                samples.append(sample)
            if index + 1 < self.sample_count and self.sample_spacing_ms > 0:
                spacing_sec = self.sample_spacing_ms / 1000.0
                if generation is None:
                    time.sleep(spacing_sec)
                elif stop_event.wait(spacing_sec):
                    break
        return samples, errors

    def _sync(self, generation=None, stop_event=None, sync_lock=None):
        sync_lock = sync_lock or self._sync_lock
        if not sync_lock.acquire(blocking=False):
            return False
        try:
            if not self._sync_context_active(generation, stop_event):
                return False
            samples, errors = self._collect_samples(generation, stop_event)
            if not self._sync_context_active(generation, stop_event):
                return False
            required = min(self.sample_count, self.min_successful_samples)
            if len(samples) < required:
                error = errors[-1] if errors else "insufficient exchange clock samples"
                return self._record_sync_failure(
                    f"exchange clock quorum failed ({len(samples)}/{required}): {error}",
                    generation=generation,
                    stop_event=stop_event,
                )
            selected_count = min(len(samples), self.low_rtt_sample_count)
            selected = sorted(samples, key=lambda item: item["rtt_ms"])[
                :selected_count
            ]
            offsets = [item["offset_ms"] for item in selected]
            offset_ms = float(statistics.median(offsets))
            rtt_ms = float(statistics.median(item["rtt_ms"] for item in selected))
            dispersion_ms = float(
                statistics.median(abs(value - offset_ms) for value in offsets)
            )
            anchor_mono_ns = self.monotonic_ns()
            anchor_wall_ns = self._wall_time_ns()
            return self._record_sync_success(
                offset_ms=offset_ms,
                rtt_ms=rtt_ms,
                dispersion_ms=dispersion_ms,
                anchor_epoch_ns=anchor_wall_ns + int(offset_ms * 1_000_000.0),
                anchor_mono_ns=anchor_mono_ns,
                anchor_wall_ns=anchor_wall_ns,
                sample_count=len(samples),
                selected_sample_count=selected_count,
                generation=generation,
                stop_event=stop_event,
            )
        except Exception as exc:
            if not self._sync_context_active(generation, stop_event):
                return False
            return self._record_sync_failure(
                str(exc),
                generation=generation,
                stop_event=stop_event,
            )
        finally:
            sync_lock.release()

    def _record_sync_success(
        self,
        *,
        offset_ms,
        rtt_ms,
        dispersion_ms,
        anchor_epoch_ns,
        anchor_mono_ns,
        anchor_wall_ns,
        sample_count,
        selected_sample_count,
        generation=None,
        stop_event=None,
    ):
        notification_severity = ""
        notify_recovered = False
        with self._state_lock:
            if not self._sync_context_active_locked(generation, stop_event):
                return False
            previous_state = self._health_state
            previous_ready = self._ready
            had_valid_calibration = self._synchronized
            phase_error_ms = 0.0
            if had_valid_calibration and self._anchor_mono_ns:
                projected_epoch_ns = self._anchor_epoch_ns + (
                    anchor_mono_ns - self._anchor_mono_ns
                )
                phase_error_ms = (
                    anchor_epoch_ns - projected_epoch_ns
                ) / 1_000_000.0
            self.last_rtt_ms = rtt_ms
            self.consecutive_failures = 0
            self._last_sample_count = sample_count
            self._last_selected_sample_count = selected_sample_count
            self._last_offset_dispersion_ms = dispersion_ms
            self._last_phase_error_ms = phase_error_ms
            breach_class = ""
            if not all(
                math.isfinite(value)
                for value in (offset_ms, phase_error_ms, rtt_ms, dispersion_ms)
            ):
                breach_class = "halt"
                reason = "exchange clock candidate contains non-finite values"
            elif (
                not had_valid_calibration
                and self.max_initial_offset_ms > 0
                and abs(offset_ms) >= self.max_initial_offset_ms
            ):
                breach_class = "halt"
                reason = (
                    f"initial clock offset {offset_ms:.1f}ms exceeds hard limit "
                    f"{self.max_initial_offset_ms:.1f}ms"
                )
            elif (
                had_valid_calibration
                and self.halt_phase_error_ms > 0
                and abs(phase_error_ms) >= self.halt_phase_error_ms
            ):
                breach_class = "halt"
                reason = (
                    f"clock phase error {phase_error_ms:.1f}ms exceeds halt threshold "
                    f"{self.halt_phase_error_ms:.1f}ms"
                )
            elif (
                had_valid_calibration
                and self.max_phase_error_ms > 0
                and abs(phase_error_ms) >= self.max_phase_error_ms
            ):
                breach_class = "freeze"
                reason = (
                    f"clock phase error {phase_error_ms:.1f}ms exceeds freeze threshold "
                    f"{self.max_phase_error_ms:.1f}ms"
                )
            elif self.max_rtt_ms > 0 and rtt_ms >= self.max_rtt_ms:
                breach_class = "freeze"
                reason = (
                    f"time sync RTT {rtt_ms:.1f}ms exceeds {self.max_rtt_ms:.1f}ms"
                )
            elif (
                self.max_uncertainty_ms > 0
                and (rtt_ms / 2.0 + dispersion_ms)
                >= self.max_uncertainty_ms
            ):
                breach_class = "freeze"
                uncertainty_ms = rtt_ms / 2.0 + dispersion_ms
                reason = (
                    f"time sync uncertainty {uncertainty_ms:.1f}ms exceeds "
                    f"{self.max_uncertainty_ms:.1f}ms"
                )
            elif (
                self.max_offset_dispersion_ms > 0
                and dispersion_ms >= self.max_offset_dispersion_ms
            ):
                breach_class = "freeze"
                reason = (
                    f"time sync offset dispersion {dispersion_ms:.1f}ms exceeds "
                    f"{self.max_offset_dispersion_ms:.1f}ms"
                )
            else:
                reason = "time sync healthy"

            if breach_class:
                # Reject first, debounce only the operator action.  A suspect
                # candidate must never replace the last-known-good anchor or
                # leave risk-increasing order flow enabled.
                self._ready = False
                self.recovery_success_count = 0
                self.last_error = reason
                self._runtime_fault_reason = reason
                if breach_class == "halt":
                    self.halt_breach_count += 1
                    self.freeze_breach_count = max(
                        self.freeze_breach_count,
                        self.halt_breach_count,
                    )
                    if self.halt_breach_count >= self.halt_breach_threshold:
                        notification_severity = "halt"
                    elif self.freeze_breach_count >= self.freeze_breach_threshold:
                        # A hard breach must never receive a weaker immediate
                        # response than a soft breach.  Freeze/cancel on the
                        # first confirmed breach, then escalate to HALT after
                        # the configured hard-breach quorum.
                        notification_severity = "freeze"
                else:
                    self.freeze_breach_count += 1
                    self.halt_breach_count = 0
                    if self.freeze_breach_count >= self.freeze_breach_threshold:
                        notification_severity = "freeze"
                self._health_state = notification_severity or "degraded"
            else:
                # Promote only a validated candidate.  The wall/monotonic pair
                # was captured together after sampling, so this is an atomic
                # replacement of the exchange epoch anchor.
                self._offset_ms = offset_ms
                self._anchor_offset_ms = offset_ms
                self._anchor_epoch_ns = anchor_epoch_ns
                self._anchor_mono_ns = anchor_mono_ns
                self._anchor_wall_ns = anchor_wall_ns
                self._last_sync_mono_ns = anchor_mono_ns
                self.last_sync_time = anchor_wall_ns / 1_000_000_000.0
                self.last_error = ""
                self._last_wall_step_ms = 0.0
                self._synchronized = True
                self.freeze_breach_count = 0
                self.halt_breach_count = 0

                fresh_start = (
                    not had_valid_calibration
                    and previous_state in {"starting", "unsynchronized"}
                )
                if previous_ready and previous_state == "healthy":
                    self.recovery_success_count = self.recovery_success_threshold
                    self._ready = True
                elif fresh_start:
                    self.recovery_success_count = self.recovery_success_threshold
                    self._ready = True
                else:
                    self.recovery_success_count = min(
                        self.recovery_success_threshold,
                        self.recovery_success_count + 1,
                    )
                    self._ready = (
                        self.recovery_success_count
                        >= self.recovery_success_threshold
                    )

                if self._ready:
                    self._health_state = "healthy"
                    self._runtime_fault_reason = ""
                    self._last_notified_fault = ""
                    notify_recovered = (
                        not previous_ready
                        and previous_state
                        not in {"starting", "unsynchronized"}
                    )
                else:
                    self._health_state = "recovering"
                    self._runtime_fault_reason = (
                        "exchange clock recovery quorum pending "
                        f"({self.recovery_success_count}/"
                        f"{self.recovery_success_threshold})"
                    )

        if breach_class:
            logger.warning(
                f"Time Sync candidate rejected: {reason} "
                f"(freeze={self.freeze_breach_count}/"
                f"{self.freeze_breach_threshold} halt={self.halt_breach_count}/"
                f"{self.halt_breach_threshold})"
            )
        else:
            logger.info(
                f"Time Synced. Offset: {offset_ms:.3f}ms "
                f"Phase error: {phase_error_ms:.3f}ms RTT: {rtt_ms:.3f}ms "
                f"samples: {selected_sample_count}/{sample_count}"
            )
        if notification_severity:
            self._notify(
                notification_severity,
                reason,
                expected_generation=generation,
                expected_stop_event=stop_event,
                offset_ms=offset_ms,
                phase_error_ms=phase_error_ms,
                rtt_ms=rtt_ms,
                offset_dispersion_ms=dispersion_ms,
                consecutive_failures=0,
            )
        elif notify_recovered:
            self._notify(
                "recovered",
                reason,
                expected_generation=generation,
                expected_stop_event=stop_event,
                offset_ms=offset_ms,
                phase_error_ms=phase_error_ms,
                rtt_ms=rtt_ms,
                offset_dispersion_ms=dispersion_ms,
                consecutive_failures=0,
            )
        return not breach_class

    def _record_sync_failure(self, error, *, generation=None, stop_event=None):
        with self._state_lock:
            if not self._sync_context_active_locked(generation, stop_event):
                return False
            self.consecutive_failures += 1
            self.last_error = error
            self.recovery_success_count = 0
            startup_failure = not self._synchronized
            severity = (
                "halt"
                if startup_failure
                or self.consecutive_failures >= self.max_consecutive_failures
                else "freeze"
            )
            reason = (
                f"startup time sync failed: {error}"
                if startup_failure
                else f"time sync failed {self.consecutive_failures} times: {error}"
            )
            self._health_state = severity
            self._ready = False
            self._runtime_fault_reason = reason
        logger.error(f"Time Sync Failed: {error}")
        self._notify(
            severity,
            reason,
            expected_generation=generation,
            expected_stop_event=stop_event,
            consecutive_failures=self.consecutive_failures,
            last_error=self.last_error,
        )
        return False

    def _set_runtime_fault(
        self,
        reason,
        *,
        fault_key=None,
        expected_generation=None,
        expected_sync_mono_ns=None,
        notify_listeners=True,
        **details,
    ):
        notification_key = fault_key or reason
        with self._state_lock:
            if not self.active:
                return False
            if (
                expected_generation is not None
                and expected_generation != self._generation
            ):
                return False
            if (
                expected_sync_mono_ns is not None
                and expected_sync_mono_ns != self._last_sync_mono_ns
            ):
                return False
            self._health_state = "halt"
            self._ready = False
            self.recovery_success_count = 0
            self._runtime_fault_reason = reason
            if notify_listeners:
                if notification_key == self._last_notified_fault:
                    return True
                self._last_notified_fault = notification_key
        if notify_listeners:
            self._notify(
                "halt",
                reason,
                expected_generation=expected_generation,
                **details,
            )
        return True

    def _check_runtime_health(self, *, notify_listeners=True):
        mono_ns = self.monotonic_ns()
        wall_ns = self._wall_time_ns()
        with self._state_lock:
            if not self.active:
                return
            generation = self._generation
            synchronized = self._synchronized
            last_sync_mono_ns = self._last_sync_mono_ns
            anchor_mono_ns = self._anchor_mono_ns
            anchor_wall_ns = self._anchor_wall_ns
            already_failed_unsynchronized = (
                not self._ready and self._health_state in {"freeze", "halt"}
                and self._last_notified_fault == "unsynchronized"
            )
        if not synchronized or not last_sync_mono_ns:
            if not already_failed_unsynchronized:
                self._set_runtime_fault(
                    "exchange clock is not synchronized",
                    fault_key="unsynchronized",
                    expected_generation=generation,
                    expected_sync_mono_ns=last_sync_mono_ns,
                    notify_listeners=notify_listeners,
                )
            return
        age_sec = max(
            0.0, (mono_ns - last_sync_mono_ns) / 1_000_000_000.0
        )
        if age_sec > self.max_sync_age_sec:
            self._set_runtime_fault(
                f"exchange clock sync is stale "
                f"({age_sec:.3f}s > {self.max_sync_age_sec:.3f}s)",
                fault_key="stale",
                expected_generation=generation,
                expected_sync_mono_ns=last_sync_mono_ns,
                notify_listeners=notify_listeners,
                sync_age_sec=age_sec,
                max_sync_age_sec=self.max_sync_age_sec,
            )
            return
        if anchor_mono_ns and anchor_wall_ns and self.max_wall_clock_step_ms > 0:
            mono_elapsed_ns = mono_ns - anchor_mono_ns
            wall_elapsed_ns = wall_ns - anchor_wall_ns
            wall_step_ms = (wall_elapsed_ns - mono_elapsed_ns) / 1_000_000.0
            with self._state_lock:
                if (
                    not self.active
                    or generation != self._generation
                    or last_sync_mono_ns != self._last_sync_mono_ns
                ):
                    return
                self._last_wall_step_ms = wall_step_ms
            if abs(wall_step_ms) > self.max_wall_clock_step_ms:
                self._set_runtime_fault(
                    f"local wall clock step detected ({wall_step_ms:.3f}ms)",
                    fault_key="wall_clock_step",
                    expected_generation=generation,
                    expected_sync_mono_ns=last_sync_mono_ns,
                    notify_listeners=notify_listeners,
                    wall_clock_step_ms=wall_step_ms,
                    max_wall_clock_step_ms=self.max_wall_clock_step_ms,
                )

    def _auto_sync_loop(self, generation=None, stop_event=None, sync_lock=None):
        if generation is None or stop_event is None:
            with self._state_lock:
                generation = self._generation
                stop_event = self._stop_event
                sync_lock = self._sync_lock
        elif sync_lock is None:
            with self._state_lock:
                sync_lock = self._sync_lock
        with self._state_lock:
            healthy = self._health_state == "healthy"
        initial_interval = (
            self.sync_interval_sec if healthy else self.unhealthy_retry_sec
        )
        next_sync_ns = self.monotonic_ns() + int(
            initial_interval * 1_000_000_000.0
        )
        while True:
            if not self._sync_context_active(generation, stop_event):
                return
            with self._state_lock:
                healthy = self._health_state == "healthy"
            now_ns = self.monotonic_ns()
            desired_interval = (
                self.sync_interval_sec if healthy else self.unhealthy_retry_sec
            )
            desired_deadline_ns = now_ns + int(
                desired_interval * 1_000_000_000.0
            )
            next_sync_ns = min(next_sync_ns, desired_deadline_ns)
            if now_ns >= next_sync_ns:
                self._sync(
                    generation=generation,
                    stop_event=stop_event,
                    sync_lock=sync_lock,
                )
                if not self._sync_context_active(generation, stop_event):
                    return
                with self._state_lock:
                    healthy = self._health_state == "healthy"
                interval = (
                    self.sync_interval_sec if healthy else self.unhealthy_retry_sec
                )
                next_sync_ns = self.monotonic_ns() + int(
                    interval * 1_000_000_000.0
                )
                continue
            self._check_runtime_health()
            remaining_sec = max(
                0.0, (next_sync_ns - now_ns) / 1_000_000_000.0
            )
            if stop_event.wait(
                min(self.health_poll_interval_sec, remaining_sec)
            ):
                return


time_service = TimeService()
