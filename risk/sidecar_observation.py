"""Exchange observation lifecycle for the independent risk sidecar."""

from __future__ import annotations

import math
import time
from collections.abc import Callable


class SidecarObservationController:
    """Own exchange snapshot freshness and evaluated account risk state."""

    def __init__(
        self,
        *,
        exchange,
        snapshot_worker,
        account_risk,
        funding_risk,
        exchange_poll_interval_sec: float,
        exchange_max_age_sec: float,
        snapshot_worker_timeout_sec: float,
        clock_failure_requires_kill: Callable[[str], bool],
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        self.exchange = exchange
        self.snapshot_worker = snapshot_worker
        self.account_risk = account_risk
        self.funding_risk = funding_risk
        self.exchange_poll_interval_sec = float(exchange_poll_interval_sec)
        self.exchange_max_age_sec = float(exchange_max_age_sec)
        self.snapshot_worker_timeout_sec = float(
            snapshot_worker_timeout_sec
        )
        self._clock_failure_requires_kill = clock_failure_requires_kill
        self._wall_time = wall_time

        self.last_exchange_poll_at = 0.0
        self.last_exchange_success_at = 0.0
        self.last_snapshot_result_sequence = 0
        self.snapshot_request_sequence = 0
        self.snapshot_request_inflight_sequence = 0
        self.snapshot_request_inflight_since = 0.0
        self.risk_snapshot_captured_at = 0.0
        self.risk_snapshot_captured_monotonic = 0.0
        self.exchange_healthy = False
        self.exchange_reason = "exchange_check_missing"
        self.risk_action = "NONE"
        self.risk_reason = ""
        self.risk_metrics = self.account_risk.initial_metrics(
            self.funding_risk.initial_metrics()
        )
        self.risk_snapshot_sequence = 0

    def evaluate_funding_guard(self, now: float):
        action, reason, metrics = self.funding_risk.evaluate(now)
        self.risk_metrics["funding_guard"] = metrics
        return action, reason

    def mark_unhealthy(self, reason: str) -> None:
        reason = str(reason or "exchange_snapshot_failed")
        self.exchange_healthy = False
        self.exchange_reason = reason
        if self.risk_action != "KILL":
            self.risk_action = (
                "KILL"
                if self._clock_failure_requires_kill(reason)
                else "REDUCE_ONLY"
            )
            self.risk_reason = reason

    def apply_result(
        self,
        *,
        healthy: bool,
        snapshot,
        reason: str,
        completed_monotonic: float,
        completed_at: float,
        full_snapshot: bool,
    ) -> None:
        if not healthy:
            self.mark_unhealthy(reason or "exchange_snapshot_failed")
            return

        if not full_snapshot:
            self.exchange_healthy = True
            self.exchange_reason = ""
            self.risk_action = "NONE"
            self.risk_reason = ""
            self.last_exchange_success_at = completed_monotonic
            return
        if not isinstance(snapshot, dict):
            self.mark_unhealthy("exchange_snapshot_payload_invalid")
            return

        self.funding_risk.ingest(snapshot)
        try:
            action, risk_reason, metrics = self.account_risk.evaluate(snapshot)
        except Exception as exc:
            self.mark_unhealthy(
                "snapshot_evaluation_exception:"
                f"{type(exc).__name__}:{exc}"
            )
            return

        self.exchange_healthy = True
        self.exchange_reason = ""
        self.risk_action = action
        self.risk_reason = risk_reason
        self.risk_metrics = (
            metrics
            if metrics
            else self.account_risk.fallback_metrics(snapshot)
        )
        self.evaluate_funding_guard(completed_monotonic)
        self.last_exchange_success_at = completed_monotonic
        self.risk_snapshot_sequence += 1
        self.risk_snapshot_captured_monotonic = completed_monotonic
        self.risk_snapshot_captured_at = self.snapshot_wall_time(
            snapshot,
            completed_at,
        )

    def poll(self, now: float) -> None:
        snapshot_query = getattr(self.exchange, "get_risk_snapshot", None)
        if callable(snapshot_query):
            try:
                healthy, snapshot, reason = snapshot_query()
            except Exception as exc:
                healthy = False
                snapshot = {}
                reason = f"snapshot_exception:{type(exc).__name__}:{exc}"
            self.apply_result(
                healthy=bool(healthy),
                snapshot=snapshot,
                reason=str(reason or ""),
                completed_monotonic=now,
                completed_at=self._wall_time(),
                full_snapshot=True,
            )
            return

        try:
            healthy, reason = self.exchange.check_account_channel()
        except Exception as exc:
            healthy = False
            reason = f"exchange_exception:{type(exc).__name__}:{exc}"
        self.apply_result(
            healthy=bool(healthy),
            snapshot={},
            reason=str(reason or ""),
            completed_monotonic=now,
            completed_at=self._wall_time(),
            full_snapshot=False,
        )

    def service_worker(self, now: float, force: bool = False) -> None:
        result = self.snapshot_worker.take_latest()
        if result is not None:
            try:
                result_sequence = int(result.get("sequence", 0) or 0)
                requested_monotonic = float(
                    result.get("requested_monotonic", 0.0) or 0.0
                )
                completed_monotonic = float(
                    result.get("completed_monotonic", 0.0) or 0.0
                )
                completed_at = float(
                    result.get("completed_at", 0.0) or 0.0
                )
            except (AttributeError, TypeError, ValueError):
                result_sequence = 0
                requested_monotonic = 0.0
                completed_monotonic = 0.0
                completed_at = 0.0

            if result_sequence > self.last_snapshot_result_sequence:
                expected_sequence = self.snapshot_request_inflight_sequence
                self.last_snapshot_result_sequence = result_sequence
                if result_sequence == expected_sequence:
                    self.snapshot_request_inflight_sequence = 0
                    self.snapshot_request_inflight_since = 0.0

                timing_valid = bool(
                    result_sequence > 0
                    and result_sequence == expected_sequence
                    and math.isfinite(requested_monotonic)
                    and requested_monotonic > 0.0
                    and math.isfinite(completed_monotonic)
                    and completed_monotonic >= requested_monotonic
                    and completed_monotonic <= now
                )
                if not timing_valid:
                    self.mark_unhealthy(
                        "exchange_snapshot_worker_timestamp_invalid"
                    )
                elif (
                    completed_monotonic - requested_monotonic
                    > self.snapshot_worker_timeout_sec
                ):
                    self.mark_unhealthy(
                        "exchange_snapshot_worker_deadline_exceeded"
                    )
                elif (
                    now - min(now, completed_monotonic)
                    > self.exchange_max_age_sec
                ):
                    self.mark_unhealthy(
                        "exchange_snapshot_worker_result_stale"
                    )
                else:
                    self.apply_result(
                        healthy=bool(result.get("healthy", False)),
                        snapshot=result.get("snapshot", {}),
                        reason=str(result.get("reason", "") or ""),
                        completed_monotonic=min(now, completed_monotonic),
                        completed_at=completed_at,
                        full_snapshot=bool(
                            result.get("full_snapshot", False)
                        ),
                    )

        inflight_age = (
            max(0.0, now - self.snapshot_request_inflight_since)
            if self.snapshot_request_inflight_since > 0.0
            else None
        )
        if (
            inflight_age is not None
            and inflight_age > self.snapshot_worker_timeout_sec
        ):
            self.mark_unhealthy("exchange_snapshot_worker_timeout")

        worker_thread = getattr(self.snapshot_worker, "thread", None)
        if worker_thread is not None and not worker_thread.is_alive():
            self.mark_unhealthy("exchange_snapshot_worker_down")

        due = bool(
            self.last_exchange_poll_at <= 0.0
            or now - self.last_exchange_poll_at
            >= self.exchange_poll_interval_sec
        )
        if self.snapshot_request_inflight_sequence <= 0 and (force or due):
            self.snapshot_request_sequence += 1
            submitted = self.snapshot_worker.submit(
                self.snapshot_request_sequence,
                now,
            )
            if submitted:
                self.last_exchange_poll_at = now
                self.snapshot_request_inflight_sequence = (
                    self.snapshot_request_sequence
                )
                self.snapshot_request_inflight_since = now
            else:
                self.mark_unhealthy(
                    "exchange_snapshot_worker_queue_full"
                )

        if self.last_exchange_success_at <= 0.0:
            if self.exchange_reason in {"", "exchange_check_missing"}:
                self.mark_unhealthy("exchange_snapshot_worker_pending")
        elif now - self.last_exchange_success_at > self.exchange_max_age_sec:
            self.mark_unhealthy("exchange_snapshot_worker_output_stale")

    def service(self, now: float, force: bool = False) -> None:
        if self.snapshot_worker is not None:
            self.service_worker(now, force=force)
            return
        if (
            force
            or self.last_exchange_poll_at <= 0.0
            or now - self.last_exchange_poll_at
            >= self.exchange_poll_interval_sec
        ):
            self.last_exchange_poll_at = now
            self.poll(now)

    def snapshot_valid(self, now: float) -> bool:
        success_at = self.last_exchange_success_at
        captured_at = self.risk_snapshot_captured_monotonic
        return bool(
            self.exchange_healthy
            and self.risk_snapshot_sequence > 0
            and math.isfinite(success_at)
            and math.isfinite(captured_at)
            and 0.0 < success_at <= now
            and 0.0 < captured_at <= now
            and success_at == captured_at
            and now - success_at <= self.exchange_max_age_sec
            and now - captured_at <= self.exchange_max_age_sec
        )

    def account_truth_counts(self) -> tuple[int, int]:
        return (
            max(
                0,
                int(self.risk_metrics.get("open_order_count", 0) or 0),
            ),
            max(
                0,
                int(
                    self.risk_metrics.get(
                        "nonzero_position_count",
                        0,
                    )
                    or 0
                ),
            ),
        )

    def snapshot_wall_time(self, snapshot: dict, fallback: float) -> float:
        try:
            fallback = float(fallback)
        except (TypeError, ValueError):
            fallback = 0.0
        if not math.isfinite(fallback) or fallback <= 0.0:
            fallback = self._wall_time()
        try:
            captured_at = float(snapshot.get("captured_at", fallback))
        except (AttributeError, TypeError, ValueError):
            captured_at = fallback
        if not math.isfinite(captured_at) or captured_at <= 0.0:
            captured_at = fallback
        return captured_at
