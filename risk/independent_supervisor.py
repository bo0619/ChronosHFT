import hashlib
import json
import math
import multiprocessing
import os
import queue
import secrets
import statistics
import threading
import time
from datetime import datetime, timezone

from risk.deployment_loss import (
    MAX_CANARY_DEPLOYED_EQUITY_FRACTION,
    deployed_capital_equity_ratio,
    deployed_capital_within_equity_limit,
    deployment_policy_fingerprint,
    deployment_loss_action,
    update_deployment_loss,
)
from risk.funding_guard import (
    ALLOW as FUNDING_ALLOW,
    FundingGuardPolicy,
    FundingGuardState,
    FundingObservation,
    evaluate_funding_guard,
    parse_binance_premium_index_payload,
)


SUPERVISOR_SOURCE = "independent_supervisor"
BINANCE_PREMIUM_INDEX_ENDPOINT = "/fapi/v1/premiumIndex"
_HARD_CLOCK_FAILURE_PREFIXES = (
    "clock_phase_error_kill:",
    "clock_initial_offset_exceeded:",
    "clock_anchor_non_finite",
    "clock_monotonic_regressed",
    "clock_phase_error_non_finite",
    "clock_phase_threshold_invalid",
)


def _finite_float(value, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _is_truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _clock_failure_requires_kill(reason: str) -> bool:
    normalized = str(reason or "").strip().lower()
    return normalized.startswith(_HARD_CLOCK_FAILURE_PREFIXES)


class BinanceRiskSidecarExchange:
    """Minimal authenticated exchange channel owned by the risk sidecar."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool,
        settings: dict = None,
    ):
        import requests

        from gateway.binance.rest_api import BinanceRestApi

        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.rest = BinanceRestApi(
            api_key,
            api_secret,
            self.session,
            testnet=testnet,
        )
        self.rest.clock_resync_callback = self.sync_exchange_clock
        settings = settings or {}
        self.symbols = tuple(
            sorted(
                {
                    str(symbol or "").strip().upper()
                    for symbol in settings.get("symbols", ())
                    if str(symbol or "").strip()
                }
            )
        )
        funding_guard = settings.get("funding_guard", {})
        if not isinstance(funding_guard, dict):
            raise ValueError("funding_guard settings must be an object")
        self.funding_guard_enabled = (
            funding_guard.get("enabled", False) is True
        )
        self.funding_max_source_age_ms = _finite_float(
            funding_guard.get("max_snapshot_age_ms", 3_000.0),
            "funding_guard.max_snapshot_age_ms",
        )
        self.daily_loss_enabled = bool(
            settings.get("daily_loss_enabled", False)
        )
        self.cash_flow_income_types = {
            str(value or "").upper()
            for value in settings.get(
                "cash_flow_income_types",
                ["TRANSFER"],
            )
            if str(value or "").strip()
        }
        self.cash_flow_assets = {
            str(value or "").upper()
            for value in settings.get(
                "cash_flow_assets",
                ["USDT", "USDC", "BUSD", "FDUSD"],
            )
            if str(value or "").strip()
        }
        self.cash_flow_max_pages = max(
            1,
            int(settings.get("cash_flow_max_pages", 5) or 5),
        )
        self.clock_sync_enabled = bool(
            settings.get("clock_sync_enabled", True)
        )
        self.clock_sync_interval_sec = max(
            1.0,
            _finite_float(
                settings.get("clock_sync_interval_sec", 30.0) or 30.0,
                "clock_sync_interval_sec",
            ),
        )
        self.clock_sample_count = max(
            1,
            int(settings.get("clock_sample_count", 5) or 5),
        )
        self.clock_min_successful_samples = max(
            1,
            min(
                self.clock_sample_count,
                int(settings.get("clock_min_successful_samples", 3) or 3),
            ),
        )
        self.clock_low_rtt_sample_count = max(
            1,
            min(
                self.clock_sample_count,
                int(settings.get("clock_low_rtt_sample_count", 3) or 3),
            ),
        )
        self.clock_sample_spacing_ms = max(
            0.0,
            _finite_float(
                settings.get("clock_sample_spacing_ms", 10.0) or 0.0,
                "clock_sample_spacing_ms",
            ),
        )
        self.clock_max_rtt_ms = max(
            0.0,
            _finite_float(
                settings.get("clock_max_rtt_ms", 200.0) or 0.0,
                "clock_max_rtt_ms",
            ),
        )
        self.clock_max_uncertainty_ms = max(
            0.0,
            _finite_float(
                settings.get("clock_max_uncertainty_ms", 50.0) or 0.0,
                "clock_max_uncertainty_ms",
            ),
        )
        self.clock_max_offset_dispersion_ms = max(
            0.0,
            _finite_float(
                settings.get("clock_max_offset_dispersion_ms", 10.0) or 0.0,
                "clock_max_offset_dispersion_ms",
            ),
        )
        self.clock_max_wall_step_ms = max(
            0.0,
            _finite_float(
                settings.get("clock_max_wall_step_ms", 20.0) or 0.0,
                "clock_max_wall_step_ms",
            ),
        )
        self.clock_max_initial_offset_ms = max(
            0.0,
            _finite_float(
                settings.get("clock_max_initial_offset_ms", 5000.0) or 0.0,
                "clock_max_initial_offset_ms",
            ),
        )
        reduce_only_phase_setting = settings.get(
            "clock_reduce_only_phase_error_ms"
        )
        reduce_only_phase_key = "clock_reduce_only_phase_error_ms"
        if reduce_only_phase_setting is None:
            reduce_only_phase_setting = settings.get(
                "clock_reduce_only_offset_ms",
                25.0,
            )
            reduce_only_phase_key = "clock_reduce_only_offset_ms"
        self.clock_reduce_only_phase_error_ms = max(
            0.0,
            _finite_float(
                reduce_only_phase_setting or 0.0,
                reduce_only_phase_key,
            ),
        )
        kill_phase_setting = settings.get("clock_kill_phase_error_ms")
        kill_phase_key = "clock_kill_phase_error_ms"
        if kill_phase_setting is None:
            kill_phase_setting = settings.get(
                "clock_kill_offset_ms",
                100.0,
            )
            kill_phase_key = "clock_kill_offset_ms"
        self.clock_kill_phase_error_ms = max(
            self.clock_reduce_only_phase_error_ms,
            _finite_float(
                kill_phase_setting or 0.0,
                kill_phase_key,
            ),
        )
        self.last_clock_sync_monotonic = 0.0
        self.clock_offset_ms = 0.0
        self.clock_phase_error_ms = 0.0
        self.clock_rtt_ms = 0.0
        self.clock_uncertainty_ms = 0.0
        self.clock_offset_dispersion_ms = 0.0
        self._clock_anchor_epoch_ms = 0.0
        self._clock_anchor_monotonic = 0.0
        self.clock_reason = "clock_sync_missing"

    def _collect_clock_samples(self):
        sample_count = max(1, int(getattr(self, "clock_sample_count", 1)))
        min_samples = max(
            1,
            min(
                sample_count,
                int(getattr(self, "clock_min_successful_samples", 1)),
            ),
        )
        low_rtt_count = max(
            1,
            min(
                sample_count,
                int(getattr(self, "clock_low_rtt_sample_count", 1)),
            ),
        )
        spacing_ms = max(
            0.0,
            float(getattr(self, "clock_sample_spacing_ms", 0.0) or 0.0),
        )
        max_wall_step_ms = max(
            0.0,
            float(getattr(self, "clock_max_wall_step_ms", 20.0) or 0.0),
        )
        samples = []
        errors = []
        for index in range(sample_count):
            started_monotonic = time.perf_counter()
            started_ms = time.time() * 1000.0
            ok, payload, reason = self._response_payload(
                self.rest.get_server_time(),
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
                            (finished_monotonic - started_monotonic) * 1000.0,
                        )
                        wall_step_ms = (
                            finished_ms - started_ms - rtt_ms
                        )
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
            statistics.median(sample["rtt_ms"] for sample in selected)
        )
        dispersion_ms = float(
            statistics.median(abs(offset - offset_ms) for offset in offsets)
        )
        return {
            "offset_ms": offset_ms,
            "rtt_ms": rtt_ms,
            "dispersion_ms": dispersion_ms,
            "uncertainty_ms": rtt_ms / 2.0 + dispersion_ms,
        }, ""

    def sync_exchange_clock(self):
        from infrastructure.time_service import time_service

        sample, reason = self._collect_clock_samples()
        if sample is None:
            self.clock_reason = reason
            return False, reason
        max_rtt_ms = max(
            0.0,
            float(getattr(self, "clock_max_rtt_ms", 200.0) or 0.0),
        )
        max_uncertainty_ms = max(
            0.0,
            float(getattr(self, "clock_max_uncertainty_ms", 50.0) or 0.0),
        )
        max_dispersion_ms = max(
            0.0,
            float(
                getattr(self, "clock_max_offset_dispersion_ms", 10.0)
                or 0.0
            ),
        )
        if max_rtt_ms > 0.0 and sample["rtt_ms"] >= max_rtt_ms:
            reason = f"clock_rtt_exceeded:{sample['rtt_ms']:.3f}ms"
            self.clock_reason = reason
            return False, reason
        if (
            max_uncertainty_ms > 0.0
            and sample["uncertainty_ms"] >= max_uncertainty_ms
        ):
            reason = (
                f"clock_uncertainty_exceeded:"
                f"{sample['uncertainty_ms']:.3f}ms"
            )
            self.clock_reason = reason
            return False, reason
        if (
            max_dispersion_ms > 0.0
            and sample["dispersion_ms"] >= max_dispersion_ms
        ):
            reason = (
                f"clock_dispersion_exceeded:"
                f"{sample['dispersion_ms']:.3f}ms"
            )
            self.clock_reason = reason
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
            self.clock_reason = reason
            return False, reason

        previous_anchor_epoch_ms = float(
            getattr(self, "_clock_anchor_epoch_ms", 0.0) or 0.0
        )
        previous_anchor_monotonic = float(
            getattr(self, "_clock_anchor_monotonic", 0.0) or 0.0
        )
        if previous_anchor_epoch_ms > 0.0 and previous_anchor_monotonic > 0.0:
            monotonic_elapsed_ms = (
                anchor_monotonic - previous_anchor_monotonic
            ) * 1000.0
            if not math.isfinite(monotonic_elapsed_ms) or monotonic_elapsed_ms < 0.0:
                reason = "clock_monotonic_regressed"
                self.clock_reason = reason
                return False, reason
            expected_epoch_ms = previous_anchor_epoch_ms + monotonic_elapsed_ms
            phase_error_ms = anchor_epoch_ms - expected_epoch_ms
            if not math.isfinite(phase_error_ms):
                reason = "clock_phase_error_non_finite"
                self.clock_reason = reason
                return False, reason
        else:
            max_initial_offset_ms = max(
                0.0,
                float(
                    getattr(self, "clock_max_initial_offset_ms", 5000.0)
                    or 0.0
                ),
            )
            if max_initial_offset_ms > 0.0 and abs(offset_ms) >= max_initial_offset_ms:
                reason = f"clock_initial_offset_exceeded:{offset_ms:.3f}ms"
                self.clock_reason = reason
                return False, reason
            phase_error_ms = 0.0

        try:
            reduce_only_phase_error_ms = max(
                0.0,
                _finite_float(
                    getattr(
                        self,
                        "clock_reduce_only_phase_error_ms",
                        25.0,
                    )
                    or 0.0,
                    "clock_reduce_only_phase_error_ms",
                ),
            )
            kill_phase_error_ms = max(
                reduce_only_phase_error_ms,
                _finite_float(
                    getattr(
                        self,
                        "clock_kill_phase_error_ms",
                        100.0,
                    )
                    or 0.0,
                    "clock_kill_phase_error_ms",
                ),
            )
        except ValueError:
            reason = "clock_phase_threshold_invalid"
            self.clock_reason = reason
            return False, reason

        # Preserve candidate quality/phase telemetry, but never replace the
        # last-known-good exchange anchor with a candidate that already
        # requires an independent risk action.
        self.clock_phase_error_ms = phase_error_ms
        self.clock_rtt_ms = float(sample["rtt_ms"])
        self.clock_uncertainty_ms = float(sample["uncertainty_ms"])
        self.clock_offset_dispersion_ms = float(sample["dispersion_ms"])
        if (
            kill_phase_error_ms > 0.0
            and abs(phase_error_ms) >= kill_phase_error_ms
        ):
            reason = f"clock_phase_error_kill:{phase_error_ms:.3f}ms"
            self.clock_reason = reason
            return False, reason
        if (
            reduce_only_phase_error_ms > 0.0
            and abs(phase_error_ms) >= reduce_only_phase_error_ms
        ):
            reason = f"clock_phase_error_reduce_only:{phase_error_ms:.3f}ms"
            self.clock_reason = reason
            return False, reason

        self.clock_offset_ms = offset_ms
        self._clock_anchor_epoch_ms = anchor_epoch_ms
        self._clock_anchor_monotonic = anchor_monotonic
        time_service.offset = self.clock_offset_ms
        time_service.last_sync_time = anchor_wall_ms / 1000.0
        time_service.last_rtt_ms = self.clock_rtt_ms
        time_service.last_error = ""
        self.last_clock_sync_monotonic = anchor_monotonic
        self.clock_reason = ""
        return True, ""

    def _ensure_exchange_clock(self, force: bool = False):
        if not getattr(self, "clock_sync_enabled", False):
            return True, ""
        age = max(
            0.0,
            time.perf_counter()
            - float(getattr(self, "last_clock_sync_monotonic", 0.0) or 0.0),
        )
        if (
            not force
            and self.last_clock_sync_monotonic > 0.0
            and age <= self.clock_sync_interval_sec
            and not str(getattr(self, "clock_reason", "") or "")
        ):
            return True, ""
        return self.sync_exchange_clock()

    def check_account_channel(self):
        try:
            response = self.rest.get_account()
            status_code = getattr(response, "status_code", None)
            if status_code != 200:
                return False, f"account_status={status_code or 'unavailable'}"
            payload = response.json()
            if not isinstance(payload, dict):
                return False, "account_payload_invalid"
            return True, ""
        except Exception as exc:
            return False, f"account_exception:{type(exc).__name__}:{exc}"

    @staticmethod
    def _response_payload(response, expected_type, label: str):
        status_code = getattr(response, "status_code", None)
        if status_code != 200:
            return False, None, f"{label}_status={status_code or 'unavailable'}"
        try:
            payload = response.json()
        except Exception as exc:
            return False, None, f"{label}_json:{type(exc).__name__}:{exc}"
        if not isinstance(payload, expected_type):
            return False, None, f"{label}_payload_invalid"
        return True, payload, ""

    def _corrected_epoch_at(self, observed_monotonic: float):
        try:
            observed_monotonic = float(observed_monotonic)
            anchor_monotonic = float(self._clock_anchor_monotonic)
            anchor_epoch_ms = float(self._clock_anchor_epoch_ms)
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
            + (observed_monotonic - anchor_monotonic) * 1_000.0
        ) / 1_000.0

    def _get_funding_observations(self):
        if not getattr(self, "funding_guard_enabled", False):
            return True, {}, ""
        if not self.symbols:
            return False, {}, "funding_guard_symbols_missing"

        observations = {}
        for symbol in self.symbols:
            ok, payload, reason = self._response_payload(
                self.rest.request(
                    "GET",
                    BINANCE_PREMIUM_INDEX_ENDPOINT,
                    {"symbol": symbol},
                    signed=False,
                ),
                dict,
                f"funding_rate:{symbol}",
            )
            if not ok:
                return False, {}, reason
            received_monotonic = time.perf_counter()
            corrected_received_epoch = self._corrected_epoch_at(
                received_monotonic
            )
            if corrected_received_epoch is None:
                return (
                    False,
                    {},
                    f"funding_clock_anchor_unavailable:{symbol}",
                )
            try:
                observation = parse_binance_premium_index_payload(
                    payload,
                    expected_symbol=symbol,
                    corrected_received_epoch=corrected_received_epoch,
                    received_monotonic=received_monotonic,
                    max_source_age_ms=self.funding_max_source_age_ms,
                    clock_healthy=not bool(self.clock_reason),
                )
            except ValueError as exc:
                return (
                    False,
                    {},
                    f"funding_payload_invalid:{symbol}:{exc}",
                )
            observations[symbol] = {
                "observation_id": observation.observation_id,
                "funding_rate": observation.funding_rate,
                "next_funding_epoch": observation.next_funding_epoch,
                "corrected_received_epoch": (
                    observation.corrected_received_epoch
                ),
                "received_monotonic": observation.received_monotonic,
                "clock_healthy": observation.clock_healthy,
            }
        return True, observations, ""

    def get_risk_snapshot(self):
        try:
            clock_ok, reason = self._ensure_exchange_clock()
            if not clock_ok:
                return False, {}, reason
            account_ok, account, reason = self._response_payload(
                self.rest.get_account(),
                dict,
                "account",
            )
            if not account_ok:
                return False, {}, reason
            positions_ok, positions, reason = self._response_payload(
                self.rest.get_positions(),
                list,
                "positions",
            )
            if not positions_ok:
                return False, {}, reason
            orders_ok, open_orders, reason = self._response_payload(
                self.rest.get_open_orders(),
                list,
                "open_orders",
            )
            if not orders_ok:
                return False, {}, reason
            external_cash_flow_total = 0.0
            if getattr(self, "daily_loss_enabled", False):
                cash_flow_ok, external_cash_flow_total, reason = (
                    self._get_daily_external_cash_flow()
                )
                if not cash_flow_ok:
                    return False, {}, reason
            funding_ok, funding_observations, reason = (
                self._get_funding_observations()
            )
            if not funding_ok:
                return False, {}, reason
            from infrastructure.time_service import time_service

            return (
                True,
                {
                    "account": account,
                    "positions": positions,
                    "open_orders": open_orders,
                    "funding_observations": funding_observations,
                    "external_cash_flow_total": external_cash_flow_total,
                    "clock_offset_ms": float(
                        getattr(self, "clock_offset_ms", 0.0) or 0.0
                    ),
                    "clock_phase_error_ms": float(
                        getattr(self, "clock_phase_error_ms", 0.0) or 0.0
                    ),
                    "clock_rtt_ms": float(
                        getattr(self, "clock_rtt_ms", 0.0) or 0.0
                    ),
                    "clock_uncertainty_ms": float(
                        getattr(self, "clock_uncertainty_ms", 0.0) or 0.0
                    ),
                    "clock_offset_dispersion_ms": float(
                        getattr(
                            self,
                            "clock_offset_dispersion_ms",
                            0.0,
                        )
                        or 0.0
                    ),
                    "captured_at": time_service.now() / 1000.0,
                },
                "",
            )
        except Exception as exc:
            return False, {}, f"snapshot_exception:{type(exc).__name__}:{exc}"

    @staticmethod
    def _income_identity(row: dict) -> str:
        income_type = str(
            row.get("incomeType", row.get("income_type", "")) or ""
        ).upper()
        transaction_id = row.get("tranId", row.get("trandId"))
        if transaction_id not in (None, ""):
            return f"{income_type}:{transaction_id}"
        fingerprint = "|".join(
            str(row.get(key, "") or "")
            for key in ("time", "asset", "income", "symbol", "info")
        )
        return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()

    def _get_daily_external_cash_flow(self):
        from infrastructure.time_service import time_service

        now = datetime.fromtimestamp(
            time_service.now() / 1000.0,
            tz=timezone.utc,
        )
        day_start_ms = int(
            now.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            ).timestamp()
            * 1000
        )
        end_time_ms = int(now.timestamp() * 1000)
        total = 0.0
        seen = set()
        limit = 1000
        for page in range(1, self.cash_flow_max_pages + 1):
            ok, rows, reason = self._response_payload(
                self.rest.get_income_history(
                    start_time=day_start_ms,
                    end_time=end_time_ms,
                    page=page,
                    limit=limit,
                ),
                list,
                "income_history",
            )
            if not ok:
                return False, 0.0, reason
            for row in rows:
                if not isinstance(row, dict):
                    return False, 0.0, "income_history_row_invalid"
                income_type = str(
                    row.get(
                        "incomeType",
                        row.get("income_type", ""),
                    )
                    or ""
                ).upper()
                if income_type not in self.cash_flow_income_types:
                    continue
                asset = str(row.get("asset", "") or "").upper()
                if asset not in self.cash_flow_assets:
                    return (
                        False,
                        0.0,
                        f"cash_flow_asset_unsupported:{asset or 'empty'}",
                    )
                identity = self._income_identity(row)
                if identity in seen:
                    continue
                seen.add(identity)
                try:
                    amount = float(row.get("income", 0.0) or 0.0)
                except (TypeError, ValueError):
                    return False, 0.0, "cash_flow_amount_invalid"
                if not math.isfinite(amount):
                    return False, 0.0, "cash_flow_amount_non_finite"
                total += amount
            if len(rows) < limit:
                return True, total, ""
        return False, 0.0, "income_history_page_limit_exceeded"

    def emergency_cancel(self, symbols, countdown_time_ms: int):
        failures = []
        clock_ok, clock_reason = self._ensure_exchange_clock(force=True)
        if not clock_ok:
            failures.append(f"clock:{clock_reason}")
        target_symbols = {
            str(symbol or "").upper()
            for symbol in symbols
            if str(symbol or "").strip()
        }
        try:
            response = self.rest.get_open_orders()
            status_code = getattr(response, "status_code", None)
            if status_code != 200:
                failures.append(
                    f"account_open_orders_status={status_code}"
                )
            else:
                open_orders = response.json()
                if not isinstance(open_orders, list):
                    failures.append("account_open_orders_payload_invalid")
                else:
                    for index, order in enumerate(open_orders):
                        if not isinstance(order, dict):
                            failures.append(
                                "account_open_order_row_invalid:"
                                f"{index}"
                            )
                            continue
                        symbol = str(
                            order.get("symbol", "") or ""
                        ).upper()
                        if not symbol:
                            failures.append(
                                "account_open_order_symbol_missing:"
                                f"{index}"
                            )
                            continue
                        target_symbols.add(symbol)
        except Exception as exc:
            failures.append(
                "account_open_orders_exception:"
                f"{type(exc).__name__}:{exc}"
            )

        for symbol in sorted(target_symbols):
            try:
                countdown_response = self.rest.set_countdown_cancel_all(
                    symbol,
                    countdown_time_ms,
                )
                if getattr(countdown_response, "status_code", None) != 200:
                    failures.append(
                        f"{symbol}:countdown_status="
                        f"{getattr(countdown_response, 'status_code', None)}"
                    )
            except Exception as exc:
                failures.append(
                    f"{symbol}:countdown_exception:{type(exc).__name__}:{exc}"
                )

            try:
                cancel_response = self.rest.cancel_all_orders(symbol)
                if getattr(cancel_response, "status_code", None) != 200:
                    failures.append(
                        f"{symbol}:cancel_status="
                        f"{getattr(cancel_response, 'status_code', None)}"
                    )
            except Exception as exc:
                failures.append(
                    f"{symbol}:cancel_exception:{type(exc).__name__}:{exc}"
                )
        return not failures, ";".join(failures)

    def emergency_flatten(self):
        from event.type import OrderRequest

        clock_ok, clock_reason = self._ensure_exchange_clock(force=True)
        ok, positions, reason = self._response_payload(
            self.rest.get_positions(),
            list,
            "positions",
        )
        if not ok:
            return False, 0, reason

        failures = [] if clock_ok else [f"clock:{clock_reason}"]
        submitted = 0
        timestamp_fragment = int(time.time() * 1000) % 1_000_000_000
        for index, position in enumerate(positions):
            symbol = str(position.get("symbol", "") or "").upper()
            try:
                amount = float(position.get("positionAmt", 0.0) or 0.0)
            except (TypeError, ValueError):
                failures.append(f"{symbol or 'unknown'}:invalid_position")
                continue
            if not symbol or abs(amount) <= 1e-9:
                continue
            side = "SELL" if amount > 0.0 else "BUY"
            request = OrderRequest(
                symbol=symbol,
                price=0.0,
                volume=abs(amount),
                side=side,
                order_type="MARKET",
                time_in_force="IOC",
                reduce_only=True,
            )
            client_oid = (
                f"crsk-{os.getpid()}-{timestamp_fragment}-{index}"
            )[:36]
            try:
                response = self.rest.new_order(request, client_oid)
                if getattr(response, "status_code", None) != 200:
                    failures.append(
                        f"{symbol}:flatten_status="
                        f"{getattr(response, 'status_code', None)}"
                    )
                    continue
                submitted += 1
            except Exception as exc:
                failures.append(
                    f"{symbol}:flatten_exception:{type(exc).__name__}:{exc}"
                )
        return not failures, submitted, ";".join(failures)

    def close(self):
        self.session.close()


class _RiskSnapshotWorker:
    """Runs at most one ordinary exchange snapshot outside the control loop."""

    def __init__(self, exchange):
        self.exchange = exchange
        self.request_queue = queue.Queue(maxsize=1)
        self.result_queue = queue.Queue(maxsize=1)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name="ChronosRiskSnapshot",
            daemon=True,
        )

    def start(self):
        self.thread.start()

    def submit(
        self,
        sequence: int,
        requested_monotonic: float,
    ) -> bool:
        try:
            self.request_queue.put_nowait(
                {
                    "sequence": int(sequence),
                    "requested_monotonic": float(requested_monotonic),
                }
            )
        except queue.Full:
            return False
        return True

    def take_latest(self):
        latest = None
        while True:
            try:
                latest = self.result_queue.get_nowait()
            except queue.Empty:
                return latest

    def stop(self, join_timeout_sec: float = 0.2) -> bool:
        self.stop_event.set()
        self.thread.join(max(0.0, float(join_timeout_sec)))
        return not self.thread.is_alive()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                request = self.request_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            if self.stop_event.is_set():
                break

            sequence = int(request.get("sequence", 0) or 0)
            requested_monotonic = float(
                request.get("requested_monotonic", 0.0) or 0.0
            )
            snapshot_query = getattr(
                self.exchange,
                "get_risk_snapshot",
                None,
            )
            full_snapshot = callable(snapshot_query)
            try:
                if full_snapshot:
                    healthy, snapshot, reason = snapshot_query()
                else:
                    healthy, reason = self.exchange.check_account_channel()
                    snapshot = {}
            except Exception as exc:
                healthy = False
                snapshot = {}
                reason = (
                    f"snapshot_exception:{type(exc).__name__}:{exc}"
                )
            completed_monotonic = time.perf_counter()
            completed_at = time.time()
            _put_latest(
                self.result_queue,
                {
                    "sequence": sequence,
                    "requested_monotonic": requested_monotonic,
                    "completed_monotonic": completed_monotonic,
                    "completed_at": completed_at,
                    "full_snapshot": full_snapshot,
                    "healthy": bool(healthy),
                    "snapshot": snapshot,
                    "reason": str(reason or ""),
                },
            )


class RiskSidecarCore:
    """Deterministic sidecar state machine, separated for fault-injection tests."""

    def __init__(
        self,
        exchange,
        settings: dict,
        now: float = None,
        snapshot_worker=None,
    ):
        now = time.perf_counter() if now is None else float(now)
        self.exchange = exchange
        self.snapshot_worker = snapshot_worker
        self.symbols = tuple(
            sorted(
                {
                    str(symbol or "").upper()
                    for symbol in settings.get("symbols", [])
                    if str(symbol or "").strip()
                }
            )
        )
        funding_guard = settings.get("funding_guard", {})
        if not isinstance(funding_guard, dict):
            raise ValueError("funding_guard settings must be an object")
        self.funding_guard_policy = FundingGuardPolicy(
            enabled=funding_guard.get("enabled", False),
            require_snapshot=funding_guard.get(
                "require_snapshot",
                funding_guard.get("enabled", False),
            ),
            max_snapshot_age_ms=funding_guard.get(
                "max_snapshot_age_ms",
                3_000.0,
            ),
            pre_funding_reduce_only_sec=funding_guard.get(
                "pre_funding_reduce_only_sec",
                600.0,
            ),
            post_funding_hold_sec=funding_guard.get(
                "post_funding_hold_sec",
                120.0,
            ),
            max_abs_funding_rate=funding_guard.get(
                "max_abs_funding_rate",
                0.0005,
            ),
            max_next_funding_horizon_sec=funding_guard.get(
                "max_next_funding_horizon_sec",
                32_400.0,
            ),
            recovery_updates=funding_guard.get(
                "recovery_updates",
                5,
            ),
        )
        self.parent_heartbeat_timeout_sec = max(
            0.1,
            float(settings.get("parent_heartbeat_timeout_sec", 1.5) or 1.5),
        )
        configured_exchange_poll_interval_sec = max(
            0.1,
            _finite_float(
                settings.get("exchange_poll_interval_sec", 5.0) or 5.0,
                "exchange_poll_interval_sec",
            ),
        )
        funding_poll_ceiling_sec = max(
            0.1,
            self.funding_guard_policy.max_snapshot_age_ms / 2_000.0,
        )
        self.exchange_poll_interval_sec = (
            min(
                configured_exchange_poll_interval_sec,
                funding_poll_ceiling_sec,
            )
            if self.funding_guard_policy.enabled
            else configured_exchange_poll_interval_sec
        )
        self.exchange_max_age_sec = max(
            self.exchange_poll_interval_sec,
            float(settings.get("exchange_max_age_sec", 10.0) or 10.0),
        )
        self.snapshot_worker_timeout_sec = min(
            self.exchange_max_age_sec,
            max(
                0.1,
                _finite_float(
                    settings.get(
                        "snapshot_worker_timeout_sec",
                        self.exchange_max_age_sec,
                    )
                    or self.exchange_max_age_sec,
                    "snapshot_worker_timeout_sec",
                ),
            ),
        )
        self.rearm_snapshot_max_age_sec = min(
            self.exchange_max_age_sec,
            max(
                0.1,
                _finite_float(
                    settings.get(
                        "rearm_snapshot_max_age_sec",
                        min(1.0, self.exchange_poll_interval_sec),
                    )
                    or min(1.0, self.exchange_poll_interval_sec),
                    "rearm_snapshot_max_age_sec",
                ),
            ),
        )
        self.cancel_retry_sec = max(
            0.1,
            float(settings.get("cancel_retry_sec", 2.0) or 2.0),
        )
        self.orphan_exit_sec = max(
            self.parent_heartbeat_timeout_sec,
            float(settings.get("orphan_exit_sec", 30.0) or 30.0),
        )
        self.emergency_countdown_time_ms = max(
            1,
            int(settings.get("emergency_countdown_time_ms", 1000) or 1000),
        )
        self.max_account_gross_notional = max(
            0.0,
            _finite_float(
                settings.get("max_account_gross_notional", 0.0) or 0.0,
                "max_account_gross_notional",
            ),
        )
        self.gross_kill_multiplier = max(
            1.0,
            _finite_float(
                settings.get("gross_kill_multiplier", 1.25) or 1.25,
                "gross_kill_multiplier",
            ),
        )
        self.margin_reduce_only_ratio = max(
            0.0,
            _finite_float(
                settings.get("margin_reduce_only_ratio", 0.70),
                "margin_reduce_only_ratio",
            ),
        )
        self.margin_kill_ratio = max(
            self.margin_reduce_only_ratio,
            _finite_float(
                settings.get("margin_kill_ratio", 0.90),
                "margin_kill_ratio",
            ),
        )
        self.max_open_orders = max(
            0,
            int(settings.get("max_open_orders", 0) or 0),
        )
        self.daily_loss_enabled = bool(
            settings.get("daily_loss_enabled", False)
        )
        self.max_daily_loss = max(
            0.0,
            _finite_float(
                settings.get("max_daily_loss", 0.0) or 0.0,
                "max_daily_loss",
            ),
        )
        self.max_drawdown_pct = max(
            0.0,
            _finite_float(
                settings.get("max_drawdown_pct", 0.0) or 0.0,
                "max_drawdown_pct",
            ),
        )
        self.daily_loss_reduce_only_fraction = min(
            1.0,
            max(
                0.0,
                _finite_float(
                    settings.get(
                        "daily_loss_reduce_only_fraction",
                        0.80,
                    ),
                    "daily_loss_reduce_only_fraction",
                ),
            ),
        )
        self.deployment_id = str(
            settings.get("deployment_id", "") or ""
        ).strip()
        self.declared_account_equity = max(
            0.0,
            _finite_float(
                settings.get("declared_account_equity_usdt", 0.0) or 0.0,
                "declared_account_equity_usdt",
            ),
        )
        self.max_deployed_capital = max(
            0.0,
            _finite_float(
                settings.get("max_deployed_capital_usdt", 0.0) or 0.0,
                "max_deployed_capital_usdt",
            ),
        )
        self.max_deployment_loss = max(
            0.0,
            _finite_float(
                settings.get("max_deployment_loss_usdt", 0.0) or 0.0,
                "max_deployment_loss_usdt",
            ),
        )
        self.deployment_loss_reduce_only_fraction = min(
            1.0,
            max(
                0.0,
                _finite_float(
                    settings.get(
                        "deployment_loss_reduce_only_fraction",
                        0.80,
                    )
                    or 0.0,
                    "deployment_loss_reduce_only_fraction",
                ),
            ),
        )
        self.deployment_policy_fingerprint = deployment_policy_fingerprint(
            deployment_id=self.deployment_id,
            symbols=settings.get("symbols", []),
            declared_account_equity=self.declared_account_equity,
            max_deployed_capital=self.max_deployed_capital,
            maximum_loss=self.max_deployment_loss,
            reduce_only_fraction=self.deployment_loss_reduce_only_fraction,
        )
        self.account_key_fingerprint = str(
            settings.get("account_key_fingerprint", "") or ""
        ).strip()
        self.clock_sync_enabled = bool(
            settings.get("clock_sync_enabled", False)
        )
        reduce_only_phase_setting = settings.get(
            "clock_reduce_only_phase_error_ms"
        )
        reduce_only_phase_key = "clock_reduce_only_phase_error_ms"
        if reduce_only_phase_setting is None:
            reduce_only_phase_setting = settings.get(
                "clock_reduce_only_offset_ms",
                25.0,
            )
            reduce_only_phase_key = "clock_reduce_only_offset_ms"
        self.clock_reduce_only_phase_error_ms = max(
            0.0,
            _finite_float(
                reduce_only_phase_setting or 0.0,
                reduce_only_phase_key,
            ),
        )
        kill_phase_setting = settings.get("clock_kill_phase_error_ms")
        kill_phase_key = "clock_kill_phase_error_ms"
        if kill_phase_setting is None:
            kill_phase_setting = settings.get(
                "clock_kill_offset_ms",
                100.0,
            )
            kill_phase_key = "clock_kill_offset_ms"
        self.clock_kill_phase_error_ms = max(
            self.clock_reduce_only_phase_error_ms,
            _finite_float(
                kill_phase_setting or 0.0,
                kill_phase_key,
            ),
        )
        # Compatibility attributes for code which still inspects the old
        # names.  Their values are phase-error thresholds, never raw offsets.
        self.clock_reduce_only_offset_ms = (
            self.clock_reduce_only_phase_error_ms
        )
        self.clock_kill_offset_ms = self.clock_kill_phase_error_ms
        self.clock_max_rtt_ms = max(
            0.0,
            _finite_float(
                settings.get("clock_max_rtt_ms", 200.0) or 0.0,
                "clock_max_rtt_ms",
            ),
        )
        self.clock_max_uncertainty_ms = max(
            0.0,
            _finite_float(
                settings.get("clock_max_uncertainty_ms", 50.0) or 0.0,
                "clock_max_uncertainty_ms",
            ),
        )
        self.clock_max_offset_dispersion_ms = max(
            0.0,
            _finite_float(
                settings.get("clock_max_offset_dispersion_ms", 10.0) or 0.0,
                "clock_max_offset_dispersion_ms",
            ),
        )
        self.liquidation_proximity_enabled = bool(
            settings.get("liquidation_proximity_enabled", False)
        )
        self.require_liquidation_price = bool(
            settings.get("require_liquidation_price", True)
        )
        self.liquidation_reduce_only_distance_pct = max(
            0.0,
            _finite_float(
                settings.get(
                    "liquidation_reduce_only_distance_pct",
                    0.05,
                )
                or 0.0,
                "liquidation_reduce_only_distance_pct",
            ),
        )
        self.liquidation_kill_distance_pct = min(
            self.liquidation_reduce_only_distance_pct,
            max(
                0.0,
                _finite_float(
                    settings.get(
                        "liquidation_kill_distance_pct",
                        0.02,
                    )
                    or 0.0,
                    "liquidation_kill_distance_pct",
                ),
            ),
        )
        self.flatten_enabled = bool(settings.get("flatten_enabled", True))
        self.parent_loss_flatten_delay_sec = max(
            0.0,
            float(settings.get("parent_loss_flatten_delay_sec", 3.0) or 0.0),
        )
        self.flatten_retry_sec = max(
            0.1,
            float(settings.get("flatten_retry_sec", 2.0) or 2.0),
        )
        self.flat_verification_checks = max(
            1,
            int(settings.get("flat_verification_checks", 2) or 2),
        )
        self.started_at = now
        self.last_parent_heartbeat_at = now
        self.last_parent_heartbeat_sent_monotonic = 0.0
        self.last_parent_heartbeat_received_at = 0.0
        self.parent_heartbeat_error = ""
        self.last_parent_sequence = 0
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
        self.last_cancel_attempt_at = 0.0
        self.last_cancel_ok = None
        self.last_cancel_reason = ""
        self.last_flatten_attempt_at = 0.0
        self.last_flatten_ok = None
        self.last_flatten_count = 0
        self.last_flatten_reason = ""
        self.risk_action = "NONE"
        self.risk_reason = ""
        self.funding_observations: dict[
            str, FundingObservation
        ] = {}
        self.funding_guard_states = {
            symbol: FundingGuardState(
                reason="funding_guard:startup_hold",
                post_hold_until_monotonic=(
                    now
                    + self.funding_guard_policy.post_funding_hold_sec
                ),
            )
            for symbol in self.symbols
            if self.funding_guard_policy.enabled
        }
        self.funding_guard_decisions = {}
        self.funding_action = (
            "REDUCE_ONLY"
            if self.funding_guard_policy.enabled
            else "NONE"
        )
        self.funding_reason = (
            "funding_guard:snapshot_unavailable"
            if self.funding_guard_policy.enabled
            else ""
        )
        self.kill_latched = False
        self.kill_reason = ""
        self.risk_metrics = {
            "maintenance_margin_ratio": 0.0,
            "position_gross_notional": 0.0,
            "opening_order_notional": 0.0,
            "projected_gross_notional": 0.0,
            "open_order_count": 0,
            "nonzero_position_count": 0,
            "risk_day": "",
            "equity": 0.0,
            "cash_flow_adjusted_equity": 0.0,
            "cash_flow_adjusted_daily_loss": 0.0,
            "deployment_id": self.deployment_id,
            "deployment_start_equity": 0.0,
            "deployment_adjusted_equity": 0.0,
            "deployment_loss": 0.0,
            "max_deployment_loss": self.max_deployment_loss,
            "declared_account_equity": self.declared_account_equity,
            "max_deployed_capital": self.max_deployed_capital,
            "deployment_policy_fingerprint": (
                self.deployment_policy_fingerprint
            ),
            "peak_adjusted_equity": 0.0,
            "peak_drawdown_pct": 0.0,
            "external_cash_flow_total": 0.0,
            "clock_offset_ms": 0.0,
            "clock_phase_error_ms": 0.0,
            "clock_rtt_ms": 0.0,
            "clock_uncertainty_ms": 0.0,
            "clock_offset_dispersion_ms": 0.0,
            "minimum_liquidation_distance_pct": None,
            "minimum_liquidation_distance_symbol": "",
            "funding_guard": {
                "enabled": self.funding_guard_policy.enabled,
                "healthy": not self.funding_guard_policy.enabled,
                "action": self.funding_action,
                "reason": self.funding_reason,
                "symbols": {},
            },
        }
        self.risk_snapshot_sequence = 0
        self.last_verified_snapshot_sequence = 0
        self.flat_verification_count = 0
        self.stage = "ARMED"
        self.unsafe_since = 0.0
        self.parent_stale_since = 0.0
        self.quiesced = False
        self.quiesce_reason = ""
        self.quiesced_at = 0.0
        self.stop_requested = False
        self.stop_request_id = ""
        self.cancel_on_stop = True
        self.last_quiesce_request_id = ""
        self.last_quiesce_accepted = None
        self.last_quiesce_reason = ""
        self.last_quiesce_persisted = False
        self.last_shutdown_resume_request_id = ""
        self.last_shutdown_resume_accepted = None
        self.last_shutdown_resume_reason = ""
        self.last_shutdown_resume_persisted = False
        self.last_stop_request_id = ""
        self.last_stop_accepted = None
        self.last_stop_reason = ""
        self.last_stop_quiesced = False
        self.last_stop_cancel_requested = False
        self.last_stop_cancel_attempted = False
        self.last_stop_cancel_ok = None
        self.risk_day = str(settings.get("seed_risk_day", "") or "")
        self.day_start_equity = _finite_float(
            settings.get("seed_day_start_equity", 0.0) or 0.0,
            "seed_day_start_equity",
        )
        self.day_start_external_cash_flow_total = _finite_float(
            settings.get("seed_external_cash_flow_total", 0.0) or 0.0,
            "seed_external_cash_flow_total",
        )
        self.peak_adjusted_equity = _finite_float(
            settings.get("seed_peak_adjusted_equity", 0.0) or 0.0,
            "seed_peak_adjusted_equity",
        )
        self.last_equity = _finite_float(
            settings.get("seed_last_equity", 0.0) or 0.0,
            "seed_last_equity",
        )
        self.deployment_start_equity = _finite_float(
            settings.get("seed_deployment_start_equity", 0.0) or 0.0,
            "seed_deployment_start_equity",
        )
        self.deployment_start_external_cash_flow_total = _finite_float(
            settings.get(
                "seed_deployment_external_cash_flow_total",
                0.0,
            )
            or 0.0,
            "seed_deployment_external_cash_flow_total",
        )
        self.deployment_adjusted_equity = _finite_float(
            settings.get("seed_deployment_adjusted_equity", 0.0) or 0.0,
            "seed_deployment_adjusted_equity",
        )
        self.deployment_loss = max(
            0.0,
            _finite_float(
                settings.get("seed_deployment_loss", 0.0) or 0.0,
                "seed_deployment_loss",
            ),
        )
        self.state_path = str(settings.get("state_path", "") or "").strip()
        self.state_required = bool(settings.get("state_required", False))
        self.state_fsync = bool(settings.get("state_fsync", True))
        self.state_generation = 0
        self.state_recovered = False
        self.state_load_error = ""
        self.state_persist_error = ""
        self._last_persisted_fingerprint = None
        self.prepared_rearm = None
        self.rearm_prepare_ttl_sec = max(
            1.0,
            _finite_float(
                settings.get("rearm_prepare_ttl_sec", 10.0) or 10.0,
                "rearm_prepare_ttl_sec",
            ),
        )
        self.last_rearm_request_id = ""
        self.last_rearm_phase = ""
        self.last_rearm_accepted = None
        self.last_rearm_reason = ""
        self.last_rearm_token = ""
        self._load_durable_state()

    @staticmethod
    def _state_checksum(payload: dict) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _durable_fingerprint(self):
        return (
            bool(self.kill_latched),
            str(self.kill_reason or ""),
            str(self.stage or ""),
            bool(self.quiesced),
            str(self.quiesce_reason or ""),
            float(self.quiesced_at),
            str(self.risk_day or ""),
            float(self.day_start_equity),
            float(self.day_start_external_cash_flow_total),
            float(self.peak_adjusted_equity),
            float(self.last_equity),
            str(self.deployment_id or ""),
            float(self.deployment_start_equity),
            float(self.deployment_start_external_cash_flow_total),
            float(self.deployment_adjusted_equity),
            float(self.deployment_loss),
            float(self.declared_account_equity),
            float(self.max_deployed_capital),
            str(self.deployment_policy_fingerprint or ""),
            str(self.account_key_fingerprint or ""),
        )

    def _fail_closed_on_state_error(self, reason: str):
        self.kill_latched = True
        self.kill_reason = str(reason or "sidecar_state_error")
        self.stage = "FAILED"

    def _quarantine_corrupt_state(self):
        if not self.state_path or not os.path.exists(self.state_path):
            return
        quarantine_path = (
            f"{self.state_path}.corrupt.{int(time.time() * 1000)}"
        )
        try:
            os.replace(self.state_path, quarantine_path)
        except OSError:
            pass

    def _load_durable_state(self):
        if not self.state_path:
            if self.state_required:
                self.state_load_error = "state_path_missing"
                self._fail_closed_on_state_error(self.state_load_error)
            return
        if not os.path.exists(self.state_path):
            self._persist_durable_state("state_initialized", force=True)
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                record = json.load(handle)
            if not isinstance(record, dict):
                raise ValueError("state_record_invalid")
            payload = record.get("payload")
            checksum = str(record.get("sha256", "") or "")
            if not isinstance(payload, dict):
                raise ValueError("state_payload_invalid")
            if checksum != self._state_checksum(payload):
                raise ValueError("state_checksum_mismatch")
            if int(payload.get("schema_version", 0) or 0) != 1:
                raise ValueError("state_schema_unsupported")
            generation = int(payload.get("generation", 0) or 0)
            if generation < 0:
                raise ValueError("state_generation_invalid")
            stored_account_fingerprint = str(
                payload.get("account_key_fingerprint", "") or ""
            ).strip()
            if self.account_key_fingerprint and not stored_account_fingerprint:
                raise ValueError("state_account_identity_missing")
            if (
                stored_account_fingerprint
                and self.account_key_fingerprint
                and not secrets.compare_digest(
                    stored_account_fingerprint,
                    self.account_key_fingerprint,
                )
            ):
                raise ValueError("state_account_identity_mismatch")
            if not self.account_key_fingerprint:
                self.account_key_fingerprint = stored_account_fingerprint
            stored_deployment_id = str(
                payload.get("deployment_id", "") or ""
            ).strip()
            if self.deployment_id and not stored_deployment_id:
                raise ValueError("state_deployment_identity_missing")
            if (
                stored_deployment_id
                and self.deployment_id
                and not secrets.compare_digest(
                    stored_deployment_id,
                    self.deployment_id,
                )
            ):
                raise ValueError("state_deployment_identity_mismatch")
            if not self.deployment_id:
                self.deployment_id = stored_deployment_id
            stored_policy_fingerprint = str(
                payload.get("deployment_policy_fingerprint", "") or ""
            ).strip()
            if (
                self.deployment_policy_fingerprint
                and not stored_policy_fingerprint
            ):
                raise ValueError("state_deployment_policy_missing")
            if (
                stored_policy_fingerprint
                and self.deployment_policy_fingerprint
                and not secrets.compare_digest(
                    stored_policy_fingerprint,
                    self.deployment_policy_fingerprint,
                )
            ):
                raise ValueError("state_deployment_policy_mismatch")
            kill_latched = payload.get("kill_latched", False)
            if not isinstance(kill_latched, bool):
                raise ValueError("state_kill_latch_invalid")
            quiesced = payload.get("quiesced", False)
            if not isinstance(quiesced, bool):
                raise ValueError("state_quiesced_invalid")
            quiesced_at = _finite_float(
                payload.get("quiesced_at", 0.0) or 0.0,
                "state.quiesced_at",
            )
            if quiesced_at < 0.0:
                raise ValueError("state.quiesced_at must be non-negative")
            if quiesced and quiesced_at <= 0.0:
                raise ValueError("state.quiesced_at missing for quiesced state")
            self.state_generation = generation
            self.state_recovered = True
            self.kill_latched = kill_latched
            self.kill_reason = str(payload.get("kill_reason", "") or "")
            self.quiesced = quiesced
            self.quiesce_reason = str(
                payload.get("quiesce_reason", "") or ""
            )
            self.quiesced_at = quiesced_at
            self.risk_day = str(payload.get("risk_day", "") or "")
            self.day_start_equity = _finite_float(
                payload.get("day_start_equity", 0.0) or 0.0,
                "state.day_start_equity",
            )
            self.day_start_external_cash_flow_total = _finite_float(
                payload.get(
                    "day_start_external_cash_flow_total",
                    0.0,
                )
                or 0.0,
                "state.day_start_external_cash_flow_total",
            )
            self.peak_adjusted_equity = _finite_float(
                payload.get("peak_adjusted_equity", 0.0) or 0.0,
                "state.peak_adjusted_equity",
            )
            self.last_equity = _finite_float(
                payload.get("last_equity", 0.0) or 0.0,
                "state.last_equity",
            )
            self.deployment_start_equity = _finite_float(
                payload.get("deployment_start_equity", 0.0) or 0.0,
                "state.deployment_start_equity",
            )
            self.deployment_start_external_cash_flow_total = _finite_float(
                payload.get(
                    "deployment_start_external_cash_flow_total",
                    0.0,
                )
                or 0.0,
                "state.deployment_start_external_cash_flow_total",
            )
            self.deployment_adjusted_equity = _finite_float(
                payload.get("deployment_adjusted_equity", 0.0) or 0.0,
                "state.deployment_adjusted_equity",
            )
            self.deployment_loss = max(
                0.0,
                _finite_float(
                    payload.get("deployment_loss", 0.0) or 0.0,
                    "state.deployment_loss",
                ),
            )
            if self.quiesced:
                self.stage = "QUIESCED"
            else:
                self.stage = "FLATTENING" if kill_latched else "ARMED"
            self._last_persisted_fingerprint = self._durable_fingerprint()
        except Exception as exc:
            self.state_load_error = (
                f"state_load_failed:{type(exc).__name__}:{exc}"
            )
            identity_error = str(exc).startswith(
                (
                    "state_account_identity_",
                    "state_deployment_identity_",
                    "state_deployment_policy_",
                )
            )
            if not identity_error:
                self._quarantine_corrupt_state()
            self._fail_closed_on_state_error(self.state_load_error)

    def _persist_durable_state(self, event: str, force: bool = False) -> bool:
        if not self.state_path:
            if self.state_required:
                self.state_persist_error = "state_path_missing"
                self._fail_closed_on_state_error(self.state_persist_error)
                return False
            return True
        fingerprint = self._durable_fingerprint()
        if not force and fingerprint == self._last_persisted_fingerprint:
            return True
        next_generation = self.state_generation + 1
        payload = {
            "schema_version": 1,
            "generation": next_generation,
            "kill_latched": bool(self.kill_latched),
            "kill_reason": str(self.kill_reason or ""),
            "stage": str(self.stage or ""),
            "quiesced": bool(self.quiesced),
            "quiesce_reason": str(self.quiesce_reason or ""),
            "quiesced_at": float(self.quiesced_at),
            "risk_day": str(self.risk_day or ""),
            "day_start_equity": float(self.day_start_equity),
            "day_start_external_cash_flow_total": float(
                self.day_start_external_cash_flow_total
            ),
            "peak_adjusted_equity": float(self.peak_adjusted_equity),
            "last_equity": float(self.last_equity),
            "deployment_id": str(self.deployment_id or ""),
            "deployment_start_equity": float(
                self.deployment_start_equity
            ),
            "deployment_start_external_cash_flow_total": float(
                self.deployment_start_external_cash_flow_total
            ),
            "deployment_adjusted_equity": float(
                self.deployment_adjusted_equity
            ),
            "deployment_loss": float(self.deployment_loss),
            "declared_account_equity": float(
                self.declared_account_equity
            ),
            "max_deployed_capital": float(self.max_deployed_capital),
            "deployment_policy_fingerprint": str(
                self.deployment_policy_fingerprint or ""
            ),
            "account_key_fingerprint": str(
                self.account_key_fingerprint or ""
            ),
            "event": str(event or "state_changed"),
            "updated_at": time.time(),
            "writer_pid": os.getpid(),
        }
        record = {
            "payload": payload,
            "sha256": self._state_checksum(payload),
        }
        absolute_path = os.path.abspath(self.state_path)
        state_dir = os.path.dirname(absolute_path)
        temp_path = (
            f"{absolute_path}.tmp.{os.getpid()}.{secrets.token_hex(6)}"
        )
        try:
            os.makedirs(state_dir, exist_ok=True)
            with open(temp_path, "x", encoding="utf-8") as handle:
                json.dump(
                    record,
                    handle,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                handle.flush()
                if self.state_fsync:
                    os.fsync(handle.fileno())
            os.replace(temp_path, absolute_path)
            if self.state_fsync and os.name != "nt":
                directory_fd = os.open(state_dir, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            self.state_generation = next_generation
            self.state_persist_error = ""
            self._last_persisted_fingerprint = fingerprint
            return True
        except Exception as exc:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
            self.state_persist_error = (
                f"state_persist_failed:{type(exc).__name__}:{exc}"
            )
            self._fail_closed_on_state_error(self.state_persist_error)
            return False

    def receive_parent_heartbeat(
        self,
        sequence: int,
        sent_monotonic: float = None,
        now: float = None,
    ):
        now = time.perf_counter() if now is None else float(now)
        try:
            sequence = int(sequence or 0)
        except (TypeError, ValueError):
            self.parent_heartbeat_error = (
                "parent_heartbeat_sequence_invalid"
            )
            return False
        if sequence <= self.last_parent_sequence:
            return False
        self.last_parent_sequence = sequence
        try:
            sent_monotonic = float(sent_monotonic)
        except (TypeError, ValueError):
            self.parent_heartbeat_error = (
                "parent_heartbeat_timestamp_invalid"
            )
            return False
        if not math.isfinite(sent_monotonic) or sent_monotonic <= 0.0:
            self.parent_heartbeat_error = (
                "parent_heartbeat_timestamp_invalid"
            )
            return False
        if sent_monotonic > now:
            self.parent_heartbeat_error = (
                "parent_heartbeat_timestamp_future"
            )
            return False
        heartbeat_age = now - min(now, sent_monotonic)
        if heartbeat_age > self.parent_heartbeat_timeout_sec:
            self.parent_heartbeat_error = (
                "parent_heartbeat_timestamp_stale"
            )
            return False

        self.last_parent_heartbeat_at = min(now, sent_monotonic)
        self.last_parent_heartbeat_sent_monotonic = sent_monotonic
        self.last_parent_heartbeat_received_at = now
        self.parent_heartbeat_error = ""
        return True

    def _set_quiesce_result(
        self,
        request_id: str,
        accepted: bool,
        reason: str,
        persisted: bool,
    ):
        self.last_quiesce_request_id = str(request_id or "")
        self.last_quiesce_accepted = bool(accepted)
        self.last_quiesce_reason = str(reason or "")
        self.last_quiesce_persisted = bool(persisted)

    def _enter_quiesced(self, reason: str, event: str) -> bool:
        self.quiesced = True
        self.quiesce_reason = str(reason or "operator_quiesce")
        if self.quiesced_at <= 0.0:
            self.quiesced_at = time.time()
        self.stage = "QUIESCED"
        self.prepared_rearm = None
        if not self.state_path:
            self.state_persist_error = "quiesce_state_path_missing"
            self._fail_closed_on_state_error(self.state_persist_error)
            persisted = False
        else:
            persisted = self._persist_durable_state(event, force=True)
        if persisted:
            return True

        # A quiesce is only real after it is durable. Keep the sidecar actively
        # supervising if the transition cannot be persisted.
        self.quiesced = False
        self.quiesce_reason = ""
        self.quiesced_at = 0.0
        self.kill_latched = True
        self.kill_reason = (
            self.state_persist_error or "quiesce_state_persist_failed"
        )
        self.stage = "FAILED"
        self.prepared_rearm = None
        self.flat_verification_count = 0
        self.last_verified_snapshot_sequence = 0
        self.last_cancel_attempt_at = 0.0
        self.last_flatten_attempt_at = 0.0
        return False

    def request_quiesce(
        self,
        request_id: str,
        reason: str,
    ):
        request_id = str(request_id or "")
        if not request_id:
            self._set_quiesce_result(
                request_id,
                False,
                "request_id_missing",
                False,
            )
            return False, "request_id_missing"

        persisted = self._enter_quiesced(
            str(reason or "operator_quiesce"),
            "supervisor_quiesced",
        )
        accepted = bool(persisted)
        result_reason = (
            "supervisor_quiesced"
            if accepted
            else self.state_persist_error or "quiesce_state_persist_failed"
        )
        self._set_quiesce_result(
            request_id,
            accepted,
            result_reason,
            persisted,
        )
        return accepted, result_reason

    def _set_shutdown_resume_result(
        self,
        request_id: str,
        accepted: bool,
        reason: str,
        persisted: bool,
    ):
        self.last_shutdown_resume_request_id = str(request_id or "")
        self.last_shutdown_resume_accepted = bool(accepted)
        self.last_shutdown_resume_reason = str(reason or "")
        self.last_shutdown_resume_persisted = bool(persisted)

    def request_shutdown_resume(
        self,
        request_id: str,
        reason: str,
    ):
        """Leave QUIESCED in a durable kill state after truth drift."""
        request_id = str(request_id or "")
        if not request_id:
            self._set_shutdown_resume_result(
                request_id,
                False,
                "request_id_missing",
                False,
            )
            return False, "request_id_missing"

        self.quiesced = False
        self.quiesce_reason = ""
        self.quiesced_at = 0.0
        self.kill_latched = True
        self.kill_reason = str(
            reason or "shutdown account truth drift"
        )
        self.stage = "FLATTENING"
        self.prepared_rearm = None
        self.flat_verification_count = 0
        self.last_verified_snapshot_sequence = 0
        self.last_cancel_attempt_at = 0.0
        self.last_flatten_attempt_at = 0.0
        persisted = self._persist_durable_state(
            "supervisor_shutdown_guard_resumed",
            force=True,
        )
        accepted = bool(persisted)
        result_reason = (
            "supervisor_shutdown_guard_resumed"
            if accepted
            else self.state_persist_error
            or "shutdown_resume_state_persist_failed"
        )
        self._set_shutdown_resume_result(
            request_id,
            accepted,
            result_reason,
            persisted,
        )
        return accepted, result_reason

    def request_stop(
        self,
        request_id: str,
        cancel_orders: bool = True,
    ):
        self.stop_requested = True
        self.stop_request_id = str(request_id or "")
        self.cancel_on_stop = bool(cancel_orders)

    def _set_stop_result(
        self,
        *,
        accepted: bool,
        reason: str,
        cancel_requested: bool,
        cancel_attempted: bool,
        cancel_ok,
    ):
        self.last_stop_request_id = str(self.stop_request_id or "")
        self.last_stop_accepted = bool(accepted)
        self.last_stop_reason = str(reason or "")
        self.last_stop_quiesced = bool(self.quiesced)
        self.last_stop_cancel_requested = bool(cancel_requested)
        self.last_stop_cancel_attempted = bool(cancel_attempted)
        self.last_stop_cancel_ok = (
            None if cancel_ok is None else bool(cancel_ok)
        )

    def _complete_stop_request(self, now: float) -> bool:
        cancel_requested = bool(self.cancel_on_stop)
        cancel_attempted = False
        cancel_ok = None
        accepted = False

        if cancel_requested and self.quiesced:
            reason = "stop_cancel_suppressed_supervisor_quiesced"
        elif cancel_requested:
            cancel_attempted = True
            cancel_ok = self._emergency_cancel(now)
            if not cancel_ok:
                reason = self.last_cancel_reason or "stop_cancel_failed"
            else:
                persisted = self._enter_quiesced(
                    "stop_after_cancel",
                    "supervisor_stop_quiesced",
                )
                accepted = bool(persisted)
                reason = (
                    "supervisor_stop_ack"
                    if accepted
                    else self.state_persist_error
                    or "stop_quiesce_state_persist_failed"
                )
        else:
            persisted = self._enter_quiesced(
                self.quiesce_reason or "stop_without_cancel",
                "supervisor_stop_quiesced",
            )
            accepted = bool(persisted)
            reason = (
                "supervisor_stop_ack"
                if accepted
                else self.state_persist_error
                or "stop_quiesce_state_persist_failed"
            )

        self._set_stop_result(
            accepted=accepted,
            reason=reason,
            cancel_requested=cancel_requested,
            cancel_attempted=cancel_attempted,
            cancel_ok=cancel_ok,
        )
        self.stop_requested = False
        return accepted

    def _set_rearm_result(
        self,
        request_id: str,
        phase: str,
        accepted: bool,
        reason: str,
        token: str = "",
    ):
        self.last_rearm_request_id = str(request_id or "")
        self.last_rearm_phase = str(phase or "")
        self.last_rearm_accepted = bool(accepted)
        self.last_rearm_reason = str(reason or "")
        self.last_rearm_token = str(token or "")

    def _check_rearm_safety(self, now: float):
        if self.quiesced:
            return False, "supervisor_quiesced"
        if not self.kill_latched:
            return False, "kill_latch_not_set"
        if self.parent_heartbeat_error:
            return False, self.parent_heartbeat_error
        if (
            max(0.0, now - self.last_parent_heartbeat_at)
            > self.parent_heartbeat_timeout_sec
        ):
            return False, "parent_heartbeat_stale"
        self._service_exchange_risk(now, force=True)
        if not self.exchange_healthy:
            return False, self.exchange_reason or "exchange_snapshot_failed"
        if self.snapshot_worker is None:
            if self.last_exchange_success_at != now:
                return False, "exchange_snapshot_failed"
        elif (
            self.last_exchange_success_at <= 0.0
            or now - self.last_exchange_success_at
            > self.rearm_snapshot_max_age_sec
        ):
            return False, "exchange_snapshot_refresh_pending"
        if self.risk_action != "NONE":
            return False, self.risk_reason or "risk_breach_remains"
        funding_action, funding_reason = self._evaluate_funding_guard(now)
        if funding_action != "NONE":
            return False, funding_reason or "funding_guard_not_healthy"
        if int(self.risk_metrics.get("open_order_count", 0) or 0) != 0:
            return False, "open_orders_remain"
        if int(
            self.risk_metrics.get("nonzero_position_count", 0) or 0
        ) != 0:
            return False, "positions_remain"
        if self.kill_latched and self.stage != "FLAT_VERIFIED":
            return False, f"flat_not_verified:{self.stage}"
        return True, ""

    def prepare_rearm(self, request_id: str, reason: str, now: float = None):
        now = time.perf_counter() if now is None else float(now)
        request_id = str(request_id or "")
        if not request_id:
            self._set_rearm_result(
                request_id,
                "PREPARE",
                False,
                "request_id_missing",
            )
            return False, "", "request_id_missing"
        safe, refusal_reason = self._check_rearm_safety(now)
        if not safe:
            self.prepared_rearm = None
            self._set_rearm_result(
                request_id,
                "PREPARE",
                False,
                refusal_reason,
            )
            return False, "", refusal_reason
        token = secrets.token_hex(24)
        self.prepared_rearm = {
            "token": token,
            "reason": str(reason or "operator_rearm"),
            "expires_at": now + self.rearm_prepare_ttl_sec,
        }
        self._set_rearm_result(
            request_id,
            "PREPARE",
            True,
            "rearm_prepared",
            token,
        )
        return True, token, "rearm_prepared"

    def commit_rearm(
        self,
        request_id: str,
        token: str,
        now: float = None,
    ):
        now = time.perf_counter() if now is None else float(now)
        request_id = str(request_id or "")
        token = str(token or "")
        prepared = self.prepared_rearm or {}
        if (
            not token
            or not secrets.compare_digest(
                token,
                str(prepared.get("token", "") or ""),
            )
        ):
            reason = "rearm_token_invalid"
            self._set_rearm_result(
                request_id,
                "COMMIT",
                False,
                reason,
            )
            return False, reason
        if now > float(prepared.get("expires_at", 0.0) or 0.0):
            self.prepared_rearm = None
            reason = "rearm_prepare_expired"
            self._set_rearm_result(
                request_id,
                "COMMIT",
                False,
                reason,
            )
            return False, reason
        safe, refusal_reason = self._check_rearm_safety(now)
        if not safe:
            self.prepared_rearm = None
            self._set_rearm_result(
                request_id,
                "COMMIT",
                False,
                refusal_reason,
            )
            return False, refusal_reason

        previous_risk_state = {
            "kill_latched": self.kill_latched,
            "kill_reason": self.kill_reason,
            "stage": self.stage,
            "flat_verification_count": self.flat_verification_count,
            "last_verified_snapshot_sequence": (
                self.last_verified_snapshot_sequence
            ),
            "risk_action": self.risk_action,
            "risk_reason": self.risk_reason,
        }
        self.kill_latched = False
        self.kill_reason = ""
        self.stage = "ARMED"
        self.flat_verification_count = 0
        self.last_verified_snapshot_sequence = 0
        self.risk_action = "NONE"
        self.risk_reason = ""
        self.prepared_rearm = None
        if not self._persist_durable_state("operator_rearm_committed", force=True):
            refusal_reason = self.state_persist_error or "state_persist_failed"
            self.kill_latched = previous_risk_state["kill_latched"]
            self.kill_reason = previous_risk_state["kill_reason"]
            self.stage = previous_risk_state["stage"]
            self.flat_verification_count = previous_risk_state[
                "flat_verification_count"
            ]
            self.last_verified_snapshot_sequence = previous_risk_state[
                "last_verified_snapshot_sequence"
            ]
            self.risk_action = previous_risk_state["risk_action"]
            self.risk_reason = previous_risk_state["risk_reason"]
            self._set_rearm_result(
                request_id,
                "COMMIT",
                False,
                refusal_reason,
            )
            return False, refusal_reason
        self.state_load_error = ""
        self._set_rearm_result(
            request_id,
            "COMMIT",
            True,
            "rearm_committed",
        )
        return True, "rearm_committed"

    def abort_rearm(self, token: str):
        prepared = self.prepared_rearm or {}
        if not token or not secrets.compare_digest(
            str(token),
            str(prepared.get("token", "") or ""),
        ):
            return False
        self.prepared_rearm = None
        return True

    def _evaluate_daily_equity(self, snapshot: dict, metrics: dict):
        if (
            not self.daily_loss_enabled
            and self.max_deployment_loss <= 0.0
        ):
            return "NONE", ""
        account = snapshot.get("account", {}) or {}
        try:
            equity = float(account.get("totalMarginBalance", 0.0) or 0.0)
            external_cash_flow_total = float(
                snapshot["external_cash_flow_total"]
            )
            captured_at = float(snapshot.get("captured_at", time.time()))
        except (KeyError, TypeError, ValueError):
            return "REDUCE_ONLY", "daily_equity_snapshot_invalid"
        if (
            not math.isfinite(equity)
            or not math.isfinite(external_cash_flow_total)
            or not math.isfinite(captured_at)
            or captured_at <= 0.0
        ):
            return "REDUCE_ONLY", "daily_equity_snapshot_invalid"
        if self.max_deployed_capital > 0.0:
            try:
                deployed_equity_ratio = deployed_capital_equity_ratio(
                    equity=equity,
                    max_deployed_capital=self.max_deployed_capital,
                )
                capital_envelope_safe = (
                    deployed_capital_within_equity_limit(
                        equity=equity,
                        max_deployed_capital=self.max_deployed_capital,
                        maximum_fraction=(
                            MAX_CANARY_DEPLOYED_EQUITY_FRACTION
                        ),
                    )
                )
            except ValueError:
                return "KILL", "canary_account_equity_invalid"
            metrics["deployed_capital_equity_ratio"] = (
                deployed_equity_ratio
            )
            if not capital_envelope_safe:
                return (
                    "KILL",
                    "canary_deployed_capital_exceeds_current_equity_fraction",
                )

        current_risk_day = datetime.fromtimestamp(
            captured_at,
            tz=timezone.utc,
        ).date().isoformat()
        baseline_missing = bool(
            self.day_start_equity == 0.0
            and self.peak_adjusted_equity == 0.0
            and self.last_equity == 0.0
        )
        if self.risk_day != current_risk_day or baseline_missing:
            self.risk_day = current_risk_day
            self.day_start_equity = equity
            self.day_start_external_cash_flow_total = (
                external_cash_flow_total
            )
            self.peak_adjusted_equity = equity

        cash_flow_delta = (
            external_cash_flow_total
            - self.day_start_external_cash_flow_total
        )
        adjusted_equity = equity - cash_flow_delta
        if self.max_deployment_loss > 0.0:
            (
                self.deployment_start_equity,
                self.deployment_start_external_cash_flow_total,
                self.deployment_adjusted_equity,
                self.deployment_loss,
            ) = update_deployment_loss(
                equity=equity,
                external_cash_flow_total=external_cash_flow_total,
                start_equity=self.deployment_start_equity,
                start_external_cash_flow_total=(
                    self.deployment_start_external_cash_flow_total
                ),
            )
        if self.peak_adjusted_equity <= 0.0:
            self.peak_adjusted_equity = adjusted_equity
        elif adjusted_equity > self.peak_adjusted_equity:
            self.peak_adjusted_equity = adjusted_equity
        self.last_equity = equity
        daily_loss = max(0.0, self.day_start_equity - adjusted_equity)
        peak_drawdown_pct = (
            max(
                0.0,
                (self.peak_adjusted_equity - adjusted_equity)
                / self.peak_adjusted_equity,
            )
            if self.peak_adjusted_equity > 0.0
            else 0.0
        )
        metrics.update(
            {
                "risk_day": self.risk_day,
                "equity": equity,
                "cash_flow_adjusted_equity": adjusted_equity,
                "cash_flow_adjusted_daily_loss": daily_loss,
                "deployment_id": self.deployment_id,
                "deployment_start_equity": (
                    self.deployment_start_equity
                ),
                "deployment_adjusted_equity": (
                    self.deployment_adjusted_equity
                ),
                "deployment_loss": self.deployment_loss,
                "max_deployment_loss": self.max_deployment_loss,
                "peak_adjusted_equity": self.peak_adjusted_equity,
                "peak_drawdown_pct": peak_drawdown_pct,
                "external_cash_flow_total": external_cash_flow_total,
            }
        )

        deployment_action = deployment_loss_action(
            loss=self.deployment_loss,
            maximum_loss=self.max_deployment_loss,
            reduce_only_fraction=(
                self.deployment_loss_reduce_only_fraction
            ),
        )
        if deployment_action == "KILL":
            return (
                "KILL",
                f"deployment_loss_kill:{self.deployment_loss:.6f}",
            )
        if (
            self.daily_loss_enabled
            and self.max_daily_loss > 0.0
            and daily_loss >= self.max_daily_loss
        ):
            return "KILL", f"daily_loss_kill:{daily_loss:.6f}"
        if (
            self.daily_loss_enabled
            and self.max_drawdown_pct > 0.0
            and peak_drawdown_pct >= self.max_drawdown_pct
        ):
            return "KILL", f"peak_drawdown_kill:{peak_drawdown_pct:.6f}"
        if deployment_action == "REDUCE_ONLY":
            return (
                "REDUCE_ONLY",
                "deployment_loss_reduce_only:"
                f"{self.deployment_loss:.6f}",
            )
        reduce_fraction = self.daily_loss_reduce_only_fraction
        if (
            self.daily_loss_enabled
            and reduce_fraction > 0.0
            and self.max_daily_loss > 0.0
            and daily_loss >= self.max_daily_loss * reduce_fraction
        ):
            return "REDUCE_ONLY", f"daily_loss_reduce_only:{daily_loss:.6f}"
        if (
            self.daily_loss_enabled
            and reduce_fraction > 0.0
            and self.max_drawdown_pct > 0.0
            and peak_drawdown_pct
            >= self.max_drawdown_pct * reduce_fraction
        ):
            return (
                "REDUCE_ONLY",
                f"peak_drawdown_reduce_only:{peak_drawdown_pct:.6f}",
            )
        return "NONE", ""

    @staticmethod
    def _funding_observation_from_record(record):
        if not isinstance(record, dict):
            return None
        return FundingObservation(
            observation_id=str(record.get("observation_id", "") or ""),
            funding_rate=record.get("funding_rate"),
            next_funding_epoch=record.get("next_funding_epoch"),
            corrected_received_epoch=record.get(
                "corrected_received_epoch"
            ),
            received_monotonic=record.get("received_monotonic"),
            clock_healthy=record.get("clock_healthy") is True,
        )

    def _ingest_funding_observations(self, snapshot: dict) -> None:
        if not self.funding_guard_policy.enabled:
            return
        raw_observations = snapshot.get("funding_observations")
        if not isinstance(raw_observations, dict):
            self.funding_observations = {}
            return
        self.funding_observations = {
            symbol: observation
            for symbol in self.symbols
            if (
                observation := self._funding_observation_from_record(
                    raw_observations.get(symbol)
                )
            )
            is not None
        }

    def _evaluate_funding_guard(self, now: float):
        policy = self.funding_guard_policy
        if not policy.enabled:
            self.funding_action = "NONE"
            self.funding_reason = ""
            self.risk_metrics["funding_guard"] = {
                "enabled": False,
                "healthy": True,
                "action": "NONE",
                "reason": "",
                "symbols": {},
            }
            return self.funding_action, self.funding_reason
        if not self.symbols:
            self.funding_action = "REDUCE_ONLY"
            self.funding_reason = "funding_guard:symbols_missing"
            self.risk_metrics["funding_guard"] = {
                "enabled": True,
                "healthy": False,
                "action": self.funding_action,
                "reason": self.funding_reason,
                "symbols": {},
            }
            return self.funding_action, self.funding_reason

        symbol_metrics = {}
        blocked_reason = ""
        for symbol in self.symbols:
            previous = self.funding_guard_states.get(symbol)
            if previous is None:
                previous = FundingGuardState(
                    reason="funding_guard:startup_hold",
                    post_hold_until_monotonic=(
                        now + policy.post_funding_hold_sec
                    ),
                )
            decision, next_state = evaluate_funding_guard(
                policy,
                self.funding_observations.get(symbol),
                previous,
                now_monotonic=now,
            )
            self.funding_guard_states[symbol] = next_state
            self.funding_guard_decisions[symbol] = decision
            if decision.action != FUNDING_ALLOW and not blocked_reason:
                blocked_reason = f"{decision.reason}:{symbol}"
            symbol_metrics[symbol] = {
                "healthy": decision.healthy,
                "action": decision.action,
                "reason": decision.reason,
                "observation_valid": decision.observation_valid,
                "snapshot_age_ms": decision.snapshot_age_ms,
                "seconds_to_funding": decision.seconds_to_funding,
                "funding_rate": decision.funding_rate,
                "post_hold_remaining_sec": (
                    decision.post_hold_remaining_sec
                ),
                "consecutive_healthy_updates": (
                    decision.consecutive_healthy_updates
                ),
                "required_recovery_updates": (
                    decision.required_recovery_updates
                ),
            }

        self.funding_action = (
            "REDUCE_ONLY" if blocked_reason else "NONE"
        )
        self.funding_reason = blocked_reason
        self.risk_metrics["funding_guard"] = {
            "enabled": True,
            "healthy": not blocked_reason,
            "action": self.funding_action,
            "reason": self.funding_reason,
            "poll_interval_sec": self.exchange_poll_interval_sec,
            "max_snapshot_age_ms": policy.max_snapshot_age_ms,
            "symbols": symbol_metrics,
        }
        return self.funding_action, self.funding_reason

    def _evaluate_risk_snapshot(self, snapshot: dict):
        account = snapshot.get("account", {}) or {}
        positions = snapshot.get("positions", []) or []
        open_orders = snapshot.get("open_orders", []) or []
        try:
            maintenance_margin = float(
                account.get("totalMaintMargin", 0.0) or 0.0
            )
            margin_balance = float(
                account.get("totalMarginBalance", 0.0) or 0.0
            )
        except (TypeError, ValueError):
            return "REDUCE_ONLY", "margin_snapshot_invalid", {}
        if (
            not math.isfinite(maintenance_margin)
            or not math.isfinite(margin_balance)
            or maintenance_margin < 0.0
            or margin_balance < 0.0
        ):
            return "REDUCE_ONLY", "margin_snapshot_invalid", {}
        maintenance_margin_ratio = (
            maintenance_margin / margin_balance
            if margin_balance > 0.0
            else (
                max(1.0, self.margin_kill_ratio)
                if maintenance_margin > 0.0
                else 0.0
            )
        )

        mark_prices = {}
        position_gross_notional = 0.0
        nonzero_position_count = 0
        minimum_liquidation_distance_pct = None
        minimum_liquidation_distance_symbol = ""
        for position in positions:
            symbol = str(position.get("symbol", "") or "").upper()
            try:
                amount = float(position.get("positionAmt", 0.0) or 0.0)
                mark_price = float(position.get("markPrice", 0.0) or 0.0)
                entry_price = float(position.get("entryPrice", 0.0) or 0.0)
                liquidation_price = float(
                    position.get("liquidationPrice", 0.0) or 0.0
                )
            except (TypeError, ValueError):
                return "REDUCE_ONLY", f"position_snapshot_invalid:{symbol}", {}
            if not math.isfinite(amount):
                return "REDUCE_ONLY", f"position_amount_invalid:{symbol}", {}
            price = mark_price if mark_price > 0.0 else entry_price
            if price > 0.0 and math.isfinite(price):
                mark_prices[symbol] = price
            if abs(amount) <= 1e-9:
                continue
            if price <= 0.0 or not math.isfinite(price):
                return "REDUCE_ONLY", f"position_price_unavailable:{symbol}", {}
            nonzero_position_count += 1
            position_notional = abs(amount) * price
            if not math.isfinite(position_notional):
                return "KILL", f"position_notional_overflow:{symbol}", {}
            position_gross_notional += position_notional
            if not math.isfinite(position_gross_notional):
                return "KILL", "account_position_notional_overflow", {}
            if self.liquidation_proximity_enabled:
                if mark_price <= 0.0 or not math.isfinite(mark_price):
                    return (
                        "REDUCE_ONLY",
                        f"liquidation_mark_price_unavailable:{symbol}",
                        {},
                    )
                if (
                    liquidation_price <= 0.0
                    or not math.isfinite(liquidation_price)
                ):
                    if self.require_liquidation_price:
                        return (
                            "REDUCE_ONLY",
                            f"liquidation_price_unavailable:{symbol}",
                            {},
                        )
                    continue
                liquidation_distance_pct = (
                    (mark_price - liquidation_price) / mark_price
                    if amount > 0.0
                    else (liquidation_price - mark_price) / mark_price
                )
                if (
                    minimum_liquidation_distance_pct is None
                    or liquidation_distance_pct
                    < minimum_liquidation_distance_pct
                ):
                    minimum_liquidation_distance_pct = (
                        liquidation_distance_pct
                    )
                    minimum_liquidation_distance_symbol = symbol

        opening_order_notional = 0.0
        for order in open_orders:
            if _is_truthy(order.get("reduceOnly", False)):
                continue
            symbol = str(order.get("symbol", "") or "").upper()
            try:
                original_qty = float(
                    order.get("origQty", order.get("quantity", 0.0)) or 0.0
                )
                executed_qty = float(order.get("executedQty", 0.0) or 0.0)
                price = float(order.get("price", 0.0) or 0.0)
                stop_price = float(order.get("stopPrice", 0.0) or 0.0)
            except (TypeError, ValueError):
                return "REDUCE_ONLY", f"open_order_invalid:{symbol}", {}
            if (
                not math.isfinite(original_qty)
                or not math.isfinite(executed_qty)
                or not math.isfinite(price)
                or not math.isfinite(stop_price)
                or original_qty < 0.0
                or executed_qty < 0.0
            ):
                return "REDUCE_ONLY", f"open_order_invalid:{symbol}", {}
            remaining_qty = max(0.0, original_qty - executed_qty)
            if remaining_qty <= 1e-9:
                continue
            risk_price = price or stop_price or mark_prices.get(symbol, 0.0)
            if risk_price <= 0.0 or not math.isfinite(risk_price):
                return "REDUCE_ONLY", f"open_order_price_unavailable:{symbol}", {}
            order_notional = remaining_qty * risk_price
            if not math.isfinite(order_notional):
                return "REDUCE_ONLY", f"open_order_notional_overflow:{symbol}", {}
            opening_order_notional += order_notional
            if not math.isfinite(opening_order_notional):
                return "REDUCE_ONLY", "opening_order_notional_overflow", {}

        projected_gross_notional = (
            position_gross_notional + opening_order_notional
        )
        if not math.isfinite(projected_gross_notional):
            return "KILL", "projected_gross_notional_overflow", {}
        metrics = {
            "maintenance_margin_ratio": maintenance_margin_ratio,
            "position_gross_notional": position_gross_notional,
            "opening_order_notional": opening_order_notional,
            "projected_gross_notional": projected_gross_notional,
            "open_order_count": len(open_orders),
            "nonzero_position_count": nonzero_position_count,
            "minimum_liquidation_distance_pct": (
                minimum_liquidation_distance_pct
            ),
            "minimum_liquidation_distance_symbol": (
                minimum_liquidation_distance_symbol
            ),
        }
        daily_action, daily_reason = self._evaluate_daily_equity(
            snapshot,
            metrics,
        )
        clock_action = "NONE"
        clock_reason = ""
        if self.clock_sync_enabled:
            try:
                clock_offset_ms = float(snapshot["clock_offset_ms"])
                clock_phase_error_ms = float(
                    snapshot["clock_phase_error_ms"]
                )
                clock_rtt_ms = float(snapshot["clock_rtt_ms"])
                clock_uncertainty_ms = float(
                    snapshot["clock_uncertainty_ms"]
                )
                clock_offset_dispersion_ms = float(
                    snapshot["clock_offset_dispersion_ms"]
                )
            except (KeyError, TypeError, ValueError):
                return "REDUCE_ONLY", "clock_snapshot_invalid", metrics
            if (
                not math.isfinite(clock_offset_ms)
                or not math.isfinite(clock_phase_error_ms)
                or not math.isfinite(clock_rtt_ms)
                or not math.isfinite(clock_uncertainty_ms)
                or not math.isfinite(clock_offset_dispersion_ms)
                or clock_rtt_ms < 0.0
                or clock_uncertainty_ms < 0.0
                or clock_offset_dispersion_ms < 0.0
            ):
                return "REDUCE_ONLY", "clock_snapshot_invalid", metrics
            metrics["clock_offset_ms"] = clock_offset_ms
            metrics["clock_phase_error_ms"] = clock_phase_error_ms
            metrics["clock_rtt_ms"] = clock_rtt_ms
            metrics["clock_uncertainty_ms"] = clock_uncertainty_ms
            metrics["clock_offset_dispersion_ms"] = (
                clock_offset_dispersion_ms
            )
            if (
                self.clock_kill_phase_error_ms > 0.0
                and abs(clock_phase_error_ms)
                >= self.clock_kill_phase_error_ms
            ):
                clock_action = "KILL"
                clock_reason = (
                    f"clock_phase_error_kill:{clock_phase_error_ms:.3f}ms"
                )
            elif (
                self.clock_reduce_only_phase_error_ms > 0.0
                and abs(clock_phase_error_ms)
                >= self.clock_reduce_only_phase_error_ms
            ):
                clock_action = "REDUCE_ONLY"
                clock_reason = (
                    "clock_phase_error_reduce_only:"
                    f"{clock_phase_error_ms:.3f}ms"
                )
            elif (
                self.clock_max_rtt_ms > 0.0
                and clock_rtt_ms >= self.clock_max_rtt_ms
            ):
                clock_action = "REDUCE_ONLY"
                clock_reason = f"clock_rtt_reduce_only:{clock_rtt_ms:.3f}ms"
            elif (
                self.clock_max_uncertainty_ms > 0.0
                and clock_uncertainty_ms >= self.clock_max_uncertainty_ms
            ):
                clock_action = "REDUCE_ONLY"
                clock_reason = (
                    "clock_uncertainty_reduce_only:"
                    f"{clock_uncertainty_ms:.3f}ms"
                )
            elif (
                self.clock_max_offset_dispersion_ms > 0.0
                and clock_offset_dispersion_ms
                >= self.clock_max_offset_dispersion_ms
            ):
                clock_action = "REDUCE_ONLY"
                clock_reason = (
                    "clock_dispersion_reduce_only:"
                    f"{clock_offset_dispersion_ms:.3f}ms"
                )

        if maintenance_margin_ratio >= self.margin_kill_ratio:
            return (
                "KILL",
                f"maintenance_margin_kill:{maintenance_margin_ratio:.6f}",
                metrics,
            )
        if (
            minimum_liquidation_distance_pct is not None
            and minimum_liquidation_distance_pct
            <= self.liquidation_kill_distance_pct
        ):
            return (
                "KILL",
                "liquidation_distance_kill:"
                f"{minimum_liquidation_distance_symbol}:"
                f"{minimum_liquidation_distance_pct:.6f}",
                metrics,
            )
        if clock_action == "KILL":
            return clock_action, clock_reason, metrics
        if daily_action == "KILL":
            return daily_action, daily_reason, metrics
        if (
            self.max_account_gross_notional > 0.0
            and projected_gross_notional
            >= self.max_account_gross_notional * self.gross_kill_multiplier
        ):
            return (
                "KILL",
                f"gross_notional_kill:{projected_gross_notional:.6f}",
                metrics,
            )
        if maintenance_margin_ratio >= self.margin_reduce_only_ratio:
            return (
                "REDUCE_ONLY",
                f"maintenance_margin_reduce_only:{maintenance_margin_ratio:.6f}",
                metrics,
            )
        if (
            minimum_liquidation_distance_pct is not None
            and minimum_liquidation_distance_pct
            <= self.liquidation_reduce_only_distance_pct
        ):
            return (
                "REDUCE_ONLY",
                "liquidation_distance_reduce_only:"
                f"{minimum_liquidation_distance_symbol}:"
                f"{minimum_liquidation_distance_pct:.6f}",
                metrics,
            )
        if clock_action == "REDUCE_ONLY":
            return clock_action, clock_reason, metrics
        if daily_action == "REDUCE_ONLY":
            return daily_action, daily_reason, metrics
        if (
            self.max_account_gross_notional > 0.0
            and projected_gross_notional > self.max_account_gross_notional
        ):
            return (
                "REDUCE_ONLY",
                f"gross_notional_reduce_only:{projected_gross_notional:.6f}",
                metrics,
            )
        if self.max_open_orders > 0 and len(open_orders) > self.max_open_orders:
            return (
                "REDUCE_ONLY",
                f"open_order_count_limit:{len(open_orders)}>{self.max_open_orders}",
                metrics,
            )
        return "NONE", "", metrics

    def _fallback_risk_metrics(self, snapshot: dict):
        positions = snapshot.get("positions", []) or []
        open_orders = snapshot.get("open_orders", []) or []
        nonzero_position_count = 0
        for position in positions:
            try:
                amount = float(position.get("positionAmt", 0.0) or 0.0)
                nonzero_position_count += int(
                    not math.isfinite(amount) or abs(amount) > 1e-9
                )
            except (AttributeError, TypeError, ValueError):
                nonzero_position_count += 1
        return {
            "maintenance_margin_ratio": 0.0,
            "position_gross_notional": 0.0,
            "opening_order_notional": 0.0,
            "projected_gross_notional": 0.0,
            "open_order_count": len(open_orders),
            "nonzero_position_count": nonzero_position_count,
            "risk_day": self.risk_day,
            "equity": self.last_equity,
            "cash_flow_adjusted_equity": self.last_equity,
            "cash_flow_adjusted_daily_loss": 0.0,
            "deployment_id": self.deployment_id,
            "deployment_start_equity": self.deployment_start_equity,
            "deployment_adjusted_equity": (
                self.deployment_adjusted_equity
            ),
            "deployment_loss": self.deployment_loss,
            "max_deployment_loss": self.max_deployment_loss,
            "declared_account_equity": self.declared_account_equity,
            "max_deployed_capital": self.max_deployed_capital,
            "deployment_policy_fingerprint": (
                self.deployment_policy_fingerprint
            ),
            "peak_adjusted_equity": self.peak_adjusted_equity,
            "peak_drawdown_pct": 0.0,
            "external_cash_flow_total": 0.0,
            "clock_offset_ms": 0.0,
            "clock_phase_error_ms": 0.0,
            "clock_rtt_ms": 0.0,
            "clock_uncertainty_ms": 0.0,
            "clock_offset_dispersion_ms": 0.0,
            "minimum_liquidation_distance_pct": None,
            "minimum_liquidation_distance_symbol": "",
        }

    def _mark_exchange_snapshot_unhealthy(self, reason: str):
        reason = str(reason or "exchange_snapshot_failed")
        self.exchange_healthy = False
        self.exchange_reason = reason
        if self.risk_action != "KILL":
            self.risk_action = (
                "KILL"
                if _clock_failure_requires_kill(reason)
                else "REDUCE_ONLY"
            )
            self.risk_reason = reason

    @staticmethod
    def _snapshot_wall_time(snapshot: dict, fallback: float) -> float:
        try:
            fallback = float(fallback)
        except (TypeError, ValueError):
            fallback = 0.0
        if not math.isfinite(fallback) or fallback <= 0.0:
            fallback = time.time()
        try:
            captured_at = float(snapshot.get("captured_at", fallback))
        except (AttributeError, TypeError, ValueError):
            captured_at = fallback
        if not math.isfinite(captured_at) or captured_at <= 0.0:
            captured_at = fallback
        return captured_at

    def _apply_exchange_risk_result(
        self,
        *,
        healthy: bool,
        snapshot,
        reason: str,
        completed_monotonic: float,
        completed_at: float,
        full_snapshot: bool,
    ):
        if not healthy:
            self._mark_exchange_snapshot_unhealthy(
                reason or "exchange_snapshot_failed"
            )
            return

        if not full_snapshot:
            self.exchange_healthy = True
            self.exchange_reason = ""
            self.risk_action = "NONE"
            self.risk_reason = ""
            self.last_exchange_success_at = completed_monotonic
            return
        if not isinstance(snapshot, dict):
            self._mark_exchange_snapshot_unhealthy(
                "exchange_snapshot_payload_invalid"
            )
            return

        self._ingest_funding_observations(snapshot)
        try:
            action, risk_reason, metrics = self._evaluate_risk_snapshot(
                snapshot
            )
        except Exception as exc:
            self._mark_exchange_snapshot_unhealthy(
                "snapshot_evaluation_exception:"
                f"{type(exc).__name__}:{exc}"
            )
            return

        self.exchange_healthy = True
        self.exchange_reason = ""
        self.risk_action = action
        self.risk_reason = risk_reason
        self.risk_metrics = (
            metrics if metrics else self._fallback_risk_metrics(snapshot)
        )
        self._evaluate_funding_guard(completed_monotonic)
        self.last_exchange_success_at = completed_monotonic
        self.risk_snapshot_sequence += 1
        self.risk_snapshot_captured_monotonic = completed_monotonic
        self.risk_snapshot_captured_at = self._snapshot_wall_time(
            snapshot,
            completed_at,
        )

    def _poll_exchange_risk(self, now: float):
        snapshot_query = getattr(self.exchange, "get_risk_snapshot", None)
        if callable(snapshot_query):
            try:
                healthy, snapshot, reason = snapshot_query()
            except Exception as exc:
                healthy = False
                snapshot = {}
                reason = f"snapshot_exception:{type(exc).__name__}:{exc}"
            self._apply_exchange_risk_result(
                healthy=bool(healthy),
                snapshot=snapshot,
                reason=str(reason or ""),
                completed_monotonic=now,
                completed_at=time.time(),
                full_snapshot=True,
            )
            return

        try:
            healthy, reason = self.exchange.check_account_channel()
        except Exception as exc:
            healthy = False
            reason = f"exchange_exception:{type(exc).__name__}:{exc}"
        self._apply_exchange_risk_result(
            healthy=bool(healthy),
            snapshot={},
            reason=str(reason or ""),
            completed_monotonic=now,
            completed_at=time.time(),
            full_snapshot=False,
        )

    def _service_snapshot_worker(self, now: float, force: bool = False):
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
                expected_sequence = (
                    self.snapshot_request_inflight_sequence
                )
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
                    self._mark_exchange_snapshot_unhealthy(
                        "exchange_snapshot_worker_timestamp_invalid"
                    )
                elif (
                    completed_monotonic - requested_monotonic
                    > self.snapshot_worker_timeout_sec
                ):
                    self._mark_exchange_snapshot_unhealthy(
                        "exchange_snapshot_worker_deadline_exceeded"
                    )
                elif (
                    now - min(now, completed_monotonic)
                    > self.exchange_max_age_sec
                ):
                    self._mark_exchange_snapshot_unhealthy(
                        "exchange_snapshot_worker_result_stale"
                    )
                else:
                    self._apply_exchange_risk_result(
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
            self._mark_exchange_snapshot_unhealthy(
                "exchange_snapshot_worker_timeout"
            )

        worker_thread = getattr(self.snapshot_worker, "thread", None)
        if worker_thread is not None and not worker_thread.is_alive():
            self._mark_exchange_snapshot_unhealthy(
                "exchange_snapshot_worker_down"
            )

        due = bool(
            self.last_exchange_poll_at <= 0.0
            or now - self.last_exchange_poll_at
            >= self.exchange_poll_interval_sec
        )
        if (
            self.snapshot_request_inflight_sequence <= 0
            and (force or due)
        ):
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
                self._mark_exchange_snapshot_unhealthy(
                    "exchange_snapshot_worker_queue_full"
                )

        if self.last_exchange_success_at <= 0.0:
            if self.exchange_reason in {
                "",
                "exchange_check_missing",
            }:
                self._mark_exchange_snapshot_unhealthy(
                    "exchange_snapshot_worker_pending"
                )
        elif now - self.last_exchange_success_at > self.exchange_max_age_sec:
            self._mark_exchange_snapshot_unhealthy(
                "exchange_snapshot_worker_output_stale"
            )

    def _service_exchange_risk(self, now: float, force: bool = False):
        if self.snapshot_worker is not None:
            self._service_snapshot_worker(now, force=force)
            return
        if (
            force
            or self.last_exchange_poll_at <= 0.0
            or now - self.last_exchange_poll_at
            >= self.exchange_poll_interval_sec
        ):
            self.last_exchange_poll_at = now
            self._poll_exchange_risk(now)

    def _emergency_cancel(self, now: float):
        if self.quiesced:
            self.last_cancel_reason = "supervisor_quiesced"
            return False
        self.last_cancel_attempt_at = now
        try:
            ok, reason = self.exchange.emergency_cancel(
                self.symbols,
                self.emergency_countdown_time_ms,
            )
        except Exception as exc:
            ok = False
            reason = f"cancel_exception:{type(exc).__name__}:{exc}"
        self.last_cancel_ok = bool(ok)
        self.last_cancel_reason = str(reason or "")
        return self.last_cancel_ok

    def _emergency_flatten(self, now: float):
        if self.quiesced:
            self.last_flatten_reason = "supervisor_quiesced"
            return False
        self.last_flatten_attempt_at = now
        flatten = getattr(self.exchange, "emergency_flatten", None)
        if not callable(flatten):
            self.last_flatten_ok = False
            self.last_flatten_count = 0
            self.last_flatten_reason = "flatten_method_unavailable"
            return False
        try:
            ok, submitted, reason = flatten()
        except Exception as exc:
            ok = False
            submitted = 0
            reason = f"flatten_exception:{type(exc).__name__}:{exc}"
        self.last_flatten_ok = bool(ok)
        self.last_flatten_count = int(submitted or 0)
        self.last_flatten_reason = str(reason or "")
        return self.last_flatten_ok

    def _step_quiesced(self, now: float):
        parent_age = max(0.0, now - self.last_parent_heartbeat_at)
        parent_healthy = bool(
            not self.parent_heartbeat_error
            and parent_age <= self.parent_heartbeat_timeout_sec
        )
        if parent_healthy:
            self.parent_stale_since = 0.0
        elif self.parent_stale_since <= 0.0:
            self.parent_stale_since = now

        keep_running = not (
            not parent_healthy
            and self.parent_stale_since > 0.0
            and now - self.parent_stale_since >= self.orphan_exit_sec
        )
        reason = (
            "supervisor_quiesced"
            if parent_healthy
            else "supervisor_quiesced_parent_stale"
        )
        action = "KILL" if self.kill_latched else "REDUCE_ONLY"
        return self._status(False, reason, action, now), keep_running

    def step(self, now: float = None):
        now = time.perf_counter() if now is None else float(now)
        if self.stop_requested:
            stop_accepted = self._complete_stop_request(now)
            return self._status(
                False,
                self.last_stop_reason or "supervisor_stop_failed",
                "KILL" if self.kill_latched else "REDUCE_ONLY",
                now,
            ), not stop_accepted
        if self.quiesced:
            return self._step_quiesced(now)

        self._service_exchange_risk(now)
        funding_action, funding_reason = self._evaluate_funding_guard(now)

        parent_age = max(0.0, now - self.last_parent_heartbeat_at)
        parent_healthy = bool(
            not self.parent_heartbeat_error
            and parent_age <= self.parent_heartbeat_timeout_sec
        )
        exchange_age = (
            max(0.0, now - self.last_exchange_success_at)
            if self.last_exchange_success_at > 0.0
            else None
        )
        exchange_valid = bool(
            self.exchange_healthy
            and exchange_age is not None
            and exchange_age <= self.exchange_max_age_sec
        )

        if parent_healthy:
            self.parent_stale_since = 0.0
        elif self.parent_stale_since <= 0.0:
            self.parent_stale_since = now

        action = (
            self.risk_action
            if exchange_valid or self.risk_action == "KILL"
            else "REDUCE_ONLY"
        )
        if action == "NONE" and funding_action != "NONE":
            action = funding_action
        if not parent_healthy:
            parent_stale_age = max(0.0, now - self.parent_stale_since)
            if action == "KILL":
                reason = self.risk_reason or "independent_hard_risk_breach"
            elif (
                self.flatten_enabled
                and parent_stale_age >= self.parent_loss_flatten_delay_sec
            ):
                action = "KILL"
                reason = "parent_heartbeat_stale_flatten"
            else:
                action = "REDUCE_ONLY"
                reason = (
                    self.parent_heartbeat_error
                    or "parent_heartbeat_stale"
                )
        elif not exchange_valid:
            reason = (
                self.risk_reason
                if action == "KILL"
                else self.exchange_reason
            ) or "exchange_health_stale"
        elif action != "NONE":
            reason = (
                self.risk_reason
                if self.risk_action != "NONE"
                else funding_reason
            ) or "independent_risk_breach"
        else:
            reason = ""

        if action == "KILL" and not self.kill_latched:
            self.kill_latched = True
            self.kill_reason = str(
                reason or self.risk_reason or "independent_hard_risk_breach"
            )
        if self.kill_latched:
            action = "KILL"
            reason = self.kill_reason or "independent_hard_risk_breach"

        healthy = parent_healthy and exchange_valid and action == "NONE"
        if healthy:
            self.unsafe_since = 0.0
            self.stage = "ARMED"
            self.flat_verification_count = 0
            self.last_verified_snapshot_sequence = 0
        else:
            if self.unsafe_since <= 0.0:
                self.unsafe_since = now
            if (
                self.stage == "FLAT_VERIFIED"
                and exchange_valid
                and action == "KILL"
                and (
                    int(self.risk_metrics.get("open_order_count", 0) or 0)
                    or int(
                        self.risk_metrics.get("nonzero_position_count", 0) or 0
                    )
                )
            ):
                self.stage = "FLATTENING"
                self.flat_verification_count = 0
            if self.stage != "FLAT_VERIFIED":
                if (
                    self.last_cancel_attempt_at <= 0.0
                    or now - self.last_cancel_attempt_at >= self.cancel_retry_sec
                ):
                    self.stage = "CANCEL_PENDING"
                    self._emergency_cancel(now)

                if action == "KILL" and self.flatten_enabled:
                    self.stage = "FLATTENING"
                    exposure_remains = bool(
                        int(self.risk_metrics.get("open_order_count", 0) or 0)
                        or int(
                            self.risk_metrics.get(
                                "nonzero_position_count",
                                0,
                            )
                            or 0
                        )
                    )
                    exposure_unknown = not exchange_valid
                    if (exposure_remains or exposure_unknown) and (
                        self.last_flatten_attempt_at <= 0.0
                        or now - self.last_flatten_attempt_at
                        >= self.flatten_retry_sec
                    ):
                        self._emergency_flatten(now)

            if exchange_valid:
                open_order_count = int(
                    self.risk_metrics.get("open_order_count", 0) or 0
                )
                nonzero_position_count = int(
                    self.risk_metrics.get("nonzero_position_count", 0) or 0
                )
                if action == "KILL" and self.flatten_enabled:
                    if (
                        open_order_count == 0
                        and nonzero_position_count == 0
                        and self.risk_snapshot_sequence
                        > self.last_verified_snapshot_sequence
                    ):
                        self.last_verified_snapshot_sequence = (
                            self.risk_snapshot_sequence
                        )
                        self.flat_verification_count += 1
                    elif open_order_count or nonzero_position_count:
                        self.stage = "FLATTENING"
                        self.flat_verification_count = 0
                        self.last_verified_snapshot_sequence = (
                            self.risk_snapshot_sequence
                        )
                    if (
                        self.flat_verification_count
                        >= self.flat_verification_checks
                    ):
                        self.stage = "FLAT_VERIFIED"
                elif open_order_count == 0:
                    self.stage = "CANCEL_VERIFIED"

        if not self._persist_durable_state("risk_state_transition"):
            healthy = False
            action = "KILL"
            reason = self.state_persist_error or "state_persist_failed"

        keep_running = not (
            not parent_healthy
            and self.parent_stale_since > 0.0
            and now - self.parent_stale_since >= self.orphan_exit_sec
            and self.stage == "FLAT_VERIFIED"
        )
        return self._status(healthy, reason, action, now), keep_running

    def _status(self, healthy: bool, reason: str, action: str, now: float):
        return {
            "healthy": bool(healthy),
            "reason": str(reason or ""),
            "risk_action": str(action or "NONE"),
            "risk_reason": self.risk_reason,
            "funding_action": self.funding_action,
            "funding_reason": self.funding_reason,
            "kill_latched": self.kill_latched,
            "kill_reason": self.kill_reason,
            "quiesced": self.quiesced,
            "quiesce_reason": self.quiesce_reason,
            "quiesced_at": self.quiesced_at,
            "stage": self.stage,
            "state_path": self.state_path,
            "state_generation": self.state_generation,
            "state_recovered": self.state_recovered,
            "state_load_error": self.state_load_error,
            "state_persist_error": self.state_persist_error,
            "risk_metrics": dict(self.risk_metrics),
            "parent_sequence": self.last_parent_sequence,
            "parent_age_sec": max(0.0, now - self.last_parent_heartbeat_at),
            "parent_heartbeat_error": self.parent_heartbeat_error,
            "parent_heartbeat_sent_monotonic": (
                self.last_parent_heartbeat_sent_monotonic
            ),
            "exchange_healthy": bool(self.exchange_healthy),
            "exchange_reason": self.exchange_reason,
            "exchange_age_sec": (
                max(0.0, now - self.last_exchange_success_at)
                if self.last_exchange_success_at > 0.0
                else None
            ),
            "last_cancel_ok": self.last_cancel_ok,
            "last_cancel_reason": self.last_cancel_reason,
            "last_flatten_ok": self.last_flatten_ok,
            "last_flatten_count": self.last_flatten_count,
            "last_flatten_reason": self.last_flatten_reason,
            "flat_verification_count": self.flat_verification_count,
            "flat_verification_checks": self.flat_verification_checks,
            "risk_snapshot_sequence": self.risk_snapshot_sequence,
            "risk_snapshot_captured_at": self.risk_snapshot_captured_at,
            "risk_snapshot_captured_monotonic": (
                self.risk_snapshot_captured_monotonic
            ),
            "risk_snapshot_age_sec": (
                max(
                    0.0,
                    now - self.risk_snapshot_captured_monotonic,
                )
                if self.risk_snapshot_captured_monotonic > 0.0
                else None
            ),
            "risk_snapshot_worker_inflight": bool(
                self.snapshot_request_inflight_sequence > 0
            ),
            "last_rearm_request_id": self.last_rearm_request_id,
            "last_rearm_phase": self.last_rearm_phase,
            "last_rearm_accepted": self.last_rearm_accepted,
            "last_rearm_reason": self.last_rearm_reason,
            "last_rearm_token": self.last_rearm_token,
            "last_quiesce_request_id": self.last_quiesce_request_id,
            "last_quiesce_accepted": self.last_quiesce_accepted,
            "last_quiesce_reason": self.last_quiesce_reason,
            "last_quiesce_persisted": self.last_quiesce_persisted,
            "last_shutdown_resume_request_id": (
                self.last_shutdown_resume_request_id
            ),
            "last_shutdown_resume_accepted": (
                self.last_shutdown_resume_accepted
            ),
            "last_shutdown_resume_reason": (
                self.last_shutdown_resume_reason
            ),
            "last_shutdown_resume_persisted": (
                self.last_shutdown_resume_persisted
            ),
            "last_stop_request_id": self.last_stop_request_id,
            "last_stop_accepted": self.last_stop_accepted,
            "last_stop_reason": self.last_stop_reason,
            "last_stop_quiesced": self.last_stop_quiesced,
            "last_stop_cancel_requested": (
                self.last_stop_cancel_requested
            ),
            "last_stop_cancel_attempted": (
                self.last_stop_cancel_attempted
            ),
            "last_stop_cancel_ok": self.last_stop_cancel_ok,
        }


def _put_latest(target_queue, payload):
    try:
        target_queue.put_nowait(payload)
        return True
    except queue.Full:
        pass
    try:
        target_queue.get_nowait()
    except queue.Empty:
        pass
    try:
        target_queue.put_nowait(payload)
    except queue.Full:
        return False
    return True


def _put_reliable(target_queue, payload, timeout_sec: float) -> bool:
    try:
        target_queue.put(
            payload,
            block=True,
            timeout=max(0.0, float(timeout_sec)),
        )
    except (OSError, ValueError, queue.Full):
        return False
    return True


def run_sidecar_loop(
    command_queue,
    status_queue,
    settings: dict,
    exchange,
    snapshot_exchange=None,
    heartbeat_queue=None,
):
    session_id = str(settings.get("session_id", "") or "")
    status_interval_sec = max(
        0.05,
        float(settings.get("status_interval_sec", 0.25) or 0.25),
    )
    loop_interval_sec = min(0.05, status_interval_sec)
    snapshot_exchange = (
        exchange if snapshot_exchange is None else snapshot_exchange
    )
    if (
        snapshot_exchange is exchange
        and isinstance(exchange, BinanceRiskSidecarExchange)
    ):
        raise ValueError(
            "risk sidecar requires an isolated snapshot exchange client"
        )
    snapshot_worker = _RiskSnapshotWorker(snapshot_exchange)
    snapshot_worker.start()
    status_sequence = 0
    last_status_at = 0.0
    last_status_signature = None

    try:
        core = RiskSidecarCore(
            exchange,
            settings,
            snapshot_worker=snapshot_worker,
        )
        while True:
            while True:
                try:
                    command = command_queue.get_nowait()
                except queue.Empty:
                    break
                if str(command.get("session_id", "") or "") != session_id:
                    continue
                command_type = str(command.get("type", "") or "").upper()
                if command_type == "HEARTBEAT":
                    core.receive_parent_heartbeat(
                        command.get("sequence", 0),
                        sent_monotonic=command.get("sent_monotonic"),
                    )
                elif command_type == "QUIESCE":
                    core.request_quiesce(
                        command.get("request_id", ""),
                        command.get("reason", ""),
                    )
                elif command_type == "RESUME_SHUTDOWN":
                    core.request_shutdown_resume(
                        command.get("request_id", ""),
                        command.get("reason", ""),
                    )
                elif command_type == "STOP":
                    core.request_stop(
                        command.get("request_id", ""),
                        command.get("cancel_orders", True),
                    )
                elif command_type == "PREPARE_REARM":
                    core.prepare_rearm(
                        command.get("request_id", ""),
                        command.get("reason", ""),
                    )
                elif command_type == "COMMIT_REARM":
                    core.commit_rearm(
                        command.get("request_id", ""),
                        command.get("token", ""),
                    )
                elif command_type == "ABORT_REARM":
                    core.abort_rearm(command.get("token", ""))

            if heartbeat_queue is not None:
                latest_heartbeat = None
                while True:
                    try:
                        latest_heartbeat = heartbeat_queue.get_nowait()
                    except queue.Empty:
                        break
                if (
                    latest_heartbeat is not None
                    and str(
                        latest_heartbeat.get("session_id", "") or ""
                    )
                    == session_id
                ):
                    core.receive_parent_heartbeat(
                        latest_heartbeat.get("sequence", 0),
                        sent_monotonic=latest_heartbeat.get(
                            "sent_monotonic"
                        ),
                    )

            now = time.perf_counter()
            status, keep_running = core.step(now)
            signature = (
                status["healthy"],
                status["reason"],
                status["risk_action"],
                status["funding_action"],
                status["funding_reason"],
                status["stage"],
                status["exchange_healthy"],
                status["last_cancel_ok"],
                status["last_cancel_reason"],
                status["last_flatten_ok"],
                status["last_flatten_count"],
                status["last_flatten_reason"],
                status["flat_verification_count"],
                status["risk_snapshot_sequence"],
                status["risk_snapshot_captured_monotonic"],
                status["parent_heartbeat_error"],
                status["state_generation"],
                status["state_load_error"],
                status["state_persist_error"],
                status["last_rearm_request_id"],
                status["last_rearm_phase"],
                status["last_rearm_accepted"],
                status["last_rearm_reason"],
                status["quiesced"],
                status["last_quiesce_request_id"],
                status["last_quiesce_accepted"],
                status["last_quiesce_reason"],
                status["last_shutdown_resume_request_id"],
                status["last_shutdown_resume_accepted"],
                status["last_shutdown_resume_reason"],
                status["last_stop_request_id"],
                status["last_stop_accepted"],
                status["last_stop_reason"],
                status["last_stop_cancel_attempted"],
                status["last_stop_cancel_ok"],
            )
            if (
                signature != last_status_signature
                or now - last_status_at >= status_interval_sec
                or not keep_running
            ):
                status_sequence += 1
                _put_latest(
                    status_queue,
                    {
                        **status,
                        "session_id": session_id,
                        "sequence": status_sequence,
                        "pid": os.getpid(),
                        "reported_at": time.time(),
                    },
                )
                last_status_at = now
                last_status_signature = signature
            if not keep_running:
                break
            time.sleep(loop_interval_sec)
    finally:
        snapshot_stopped = snapshot_worker.stop()
        if snapshot_exchange is not exchange:
            close = getattr(exchange, "close", None)
            if callable(close):
                close()
        if snapshot_stopped:
            close = getattr(snapshot_exchange, "close", None)
            if callable(close):
                close()


def _risk_sidecar_process(
    command_queue,
    status_queue,
    settings: dict,
    heartbeat_queue=None,
):
    exchange = None
    snapshot_exchange = None
    try:
        api_key = str(settings.get("api_key", "") or "")
        api_secret = str(settings.get("api_secret", "") or "")
        if not api_key or not api_secret:
            raise ValueError(
                "dedicated sidecar API credentials are required"
            )
        exchange = BinanceRiskSidecarExchange(
            api_key,
            api_secret,
            bool(settings.get("testnet", False)),
            settings=settings,
        )
        snapshot_exchange = BinanceRiskSidecarExchange(
            api_key,
            api_secret,
            bool(settings.get("testnet", False)),
            settings=settings,
        )
    except Exception as exc:
        close = getattr(exchange, "close", None)
        if callable(close):
            close()
        _put_latest(
            status_queue,
            {
                "session_id": settings.get("session_id", ""),
                "sequence": 1,
                "pid": os.getpid(),
                "reported_at": time.time(),
                "healthy": False,
                "reason": f"sidecar_init_failed:{type(exc).__name__}:{exc}",
            },
        )
        return
    run_sidecar_loop(
        command_queue,
        status_queue,
        settings,
        exchange,
        snapshot_exchange=snapshot_exchange,
        heartbeat_queue=heartbeat_queue,
    )


class IndependentRiskSupervisor:
    """Parent-side controller for the independent risk supervision process."""

    def __init__(self, oms, config: dict, risk_manager=None):
        self.oms = oms
        self.risk_manager = risk_manager
        risk_config = config.get("risk", {}) or {}
        self.config = dict(risk_config.get("independent_supervisor", {}) or {})
        self.enabled = bool(self.config.get("enabled", False))
        heartbeat_config = dict(risk_config.get("risk_control_heartbeat", {}) or {})
        if self.enabled and not bool(heartbeat_config.get("enabled", False)):
            raise ValueError(
                "independent_supervisor requires risk_control_heartbeat.enabled"
            )
        required_source = str(
            heartbeat_config.get("required_source", "") or ""
        ).strip()
        if self.enabled and required_source != SUPERVISOR_SOURCE:
            raise ValueError(
                "independent_supervisor requires risk_control_heartbeat."
                f"required_source={SUPERVISOR_SOURCE!r}"
            )

        self.heartbeat_interval_sec = max(
            0.05,
            float(self.config.get("heartbeat_interval_sec", 0.25) or 0.25),
        )
        self.status_max_age_sec = max(
            self.heartbeat_interval_sec,
            float(self.config.get("status_max_age_sec", 2.0) or 2.0),
        )
        self.stop_timeout_sec = max(
            0.5,
            float(self.config.get("stop_timeout_sec", 10.0) or 10.0),
        )
        self.control_enqueue_timeout_sec = max(
            0.01,
            _finite_float(
                self.config.get(
                    "control_enqueue_timeout_sec",
                    0.5,
                )
                or 0.5,
                "control_enqueue_timeout_sec",
            ),
        )
        self.recovery_checks = max(
            1,
            int(self.config.get("recovery_checks", 2) or 2),
        )
        self.recovery_snapshot_max_age_sec = max(
            self.heartbeat_interval_sec,
            _finite_float(
                self.config.get("exchange_max_age_sec", 10.0) or 10.0,
                "exchange_max_age_sec",
            ),
        )
        self.rearm_command_timeout_sec = max(
            0.5,
            _finite_float(
                self.config.get("rearm_command_timeout_sec", 5.0) or 5.0,
                "rearm_command_timeout_sec",
            ),
        )
        limits_config = dict(risk_config.get("limits", {}) or {})
        margin_config = dict(risk_config.get("margin_health", {}) or {})
        cash_flow_config = dict(risk_config.get("cash_flow_truth", {}) or {})
        funding_guard_config = dict(
            risk_config.get("funding_guard", {}) or {}
        )
        live_launch_config = dict(config.get("live_launch", {}) or {})
        oms_config = dict(config.get("oms", {}) or {})
        self.settings = {
            **self.config,
            "api_key": str(self.config.get("api_key", "") or ""),
            "api_secret": str(self.config.get("api_secret", "") or ""),
            "testnet": bool(config.get("testnet", False)),
            "symbols": list(config.get("symbols", [])),
            "funding_guard": funding_guard_config,
            "emergency_countdown_time_ms": int(
                self.config.get("emergency_countdown_time_ms", 1000) or 1000
            ),
            "max_account_gross_notional": float(
                self.config.get(
                    "max_account_gross_notional",
                    limits_config.get("max_account_gross_notional", 0.0),
                )
                or 0.0
            ),
            "margin_reduce_only_ratio": float(
                self.config.get(
                    "margin_reduce_only_ratio",
                    margin_config.get("reduce_only_ratio", 0.70),
                )
                or 0.0
            ),
            "margin_kill_ratio": float(
                self.config.get(
                    "margin_kill_ratio",
                    margin_config.get("kill_ratio", 0.90),
                )
                or 0.0
            ),
            "max_open_orders": int(
                self.config.get(
                    "max_open_orders",
                    oms_config.get("max_total_active_orders", 100),
                )
                or 0
            ),
            "max_daily_loss": float(
                self.config.get(
                    "max_daily_loss",
                    limits_config.get("max_daily_loss", 0.0),
                )
                or 0.0
            ),
            "max_drawdown_pct": float(
                self.config.get(
                    "max_drawdown_pct",
                    limits_config.get("max_drawdown_pct", 0.0),
                )
                or 0.0
            ),
            "deployment_id": str(
                live_launch_config.get("deployment_id", "") or ""
            ),
            "declared_account_equity_usdt": float(
                live_launch_config.get(
                    "declared_account_equity_usdt",
                    0.0,
                )
                or 0.0
            ),
            "max_deployed_capital_usdt": float(
                live_launch_config.get(
                    "max_deployed_capital_usdt",
                    0.0,
                )
                or 0.0
            ),
            "max_deployment_loss_usdt": float(
                live_launch_config.get(
                    "max_deployment_loss_usdt",
                    0.0,
                )
                or 0.0
            ),
            "deployment_loss_reduce_only_fraction": float(
                live_launch_config.get(
                    "deployment_loss_reduce_only_fraction",
                    0.80,
                )
                or 0.0
            ),
            "cash_flow_income_types": list(
                self.config.get(
                    "cash_flow_income_types",
                    cash_flow_config.get(
                        "external_income_types",
                        ["TRANSFER"],
                    ),
                )
                or []
            ),
            "seed_risk_day": str(
                getattr(self.risk_manager, "risk_day", "") or ""
            ),
            "seed_day_start_equity": float(
                getattr(self.risk_manager, "initial_equity", 0.0) or 0.0
            ),
            "seed_external_cash_flow_total": float(
                getattr(
                    self.risk_manager,
                    "initial_external_cash_flow_total",
                    0.0,
                )
                or 0.0
            ),
            "seed_peak_adjusted_equity": float(
                getattr(self.risk_manager, "peak_equity", 0.0) or 0.0
            ),
            "seed_last_equity": float(
                getattr(self.risk_manager, "last_equity", 0.0) or 0.0
            ),
            "seed_deployment_start_equity": float(
                getattr(
                    self.risk_manager,
                    "deployment_start_equity",
                    0.0,
                )
                or 0.0
            ),
            "seed_deployment_external_cash_flow_total": float(
                getattr(
                    self.risk_manager,
                    "deployment_start_external_cash_flow_total",
                    0.0,
                )
                or 0.0
            ),
            "seed_deployment_adjusted_equity": float(
                getattr(
                    self.risk_manager,
                    "deployment_adjusted_equity",
                    0.0,
                )
                or 0.0
            ),
            "seed_deployment_loss": float(
                getattr(
                    self.risk_manager,
                    "deployment_loss",
                    0.0,
                )
                or 0.0
            ),
        }
        if self.enabled and (
            not self.settings["api_key"] or not self.settings["api_secret"]
        ):
            raise ValueError(
                "independent_supervisor requires dedicated API credentials"
            )
        api_key = str(self.settings.get("api_key", "") or "")
        self.settings["account_key_fingerprint"] = (
            hashlib.sha256(api_key.encode("utf-8")).hexdigest()
            if api_key
            else ""
        )
        self.session_id = secrets.token_hex(16)
        self.settings["session_id"] = self.session_id
        self.context = None
        self.command_queue = None
        self.heartbeat_queue = None
        self.status_queue = None
        self.process = None
        self.heartbeat_sequence = 0
        self.last_heartbeat_sent_at = 0.0
        self.last_status = {}
        self.last_status_received_at = 0.0
        self.started_at = 0.0
        self.recovery_count = 0
        self.last_recovery_snapshot_sequence = 0

    def start(self) -> bool:
        if not self.enabled:
            return True
        if self.process is not None and self.process.is_alive():
            return True
        self.last_status = {}
        self.last_status_received_at = 0.0
        self.last_recovery_snapshot_sequence = 0
        self.recovery_count = 0
        self.heartbeat_sequence = 0
        self.last_heartbeat_sent_at = 0.0
        self.context = multiprocessing.get_context("spawn")
        self.command_queue = self.context.Queue(maxsize=32)
        self.heartbeat_queue = self.context.Queue(maxsize=1)
        self.status_queue = self.context.Queue(maxsize=8)
        self.process = self.context.Process(
            target=_risk_sidecar_process,
            args=(
                self.command_queue,
                self.status_queue,
                self.settings,
                self.heartbeat_queue,
            ),
            name="ChronosRiskSupervisor",
            daemon=False,
        )
        self.process.start()
        self.started_at = time.perf_counter()
        self._send_heartbeat(self.started_at)
        self._apply_oms_health(False, "supervisor_starting")
        return True

    def pulse_parent_heartbeat(self) -> bool:
        """Emit only the liveness pulse, without applying parent-side risk state."""
        if not self.enabled:
            return True
        process = self.process
        if process is None or not process.is_alive():
            return False
        self._send_heartbeat(time.perf_counter())
        return True

    def _send_heartbeat(self, now: float):
        if now - self.last_heartbeat_sent_at < self.heartbeat_interval_sec:
            return
        self.heartbeat_sequence += 1
        payload = {
            "type": "HEARTBEAT",
            "session_id": self.session_id,
            "sequence": self.heartbeat_sequence,
            "sent_monotonic": now,
        }
        if self.heartbeat_queue is not None:
            delivered = _put_latest(self.heartbeat_queue, payload)
        else:
            try:
                self.command_queue.put_nowait(payload)
                delivered = True
            except (OSError, ValueError, queue.Full):
                delivered = False
        if delivered:
            self.last_heartbeat_sent_at = now

    def _drain_status(self, now: float):
        while True:
            try:
                status = self.status_queue.get_nowait()
            except queue.Empty:
                break
            if str(status.get("session_id", "") or "") != self.session_id:
                continue
            if int(status.get("sequence", 0) or 0) <= int(
                self.last_status.get("sequence", 0) or 0
            ):
                continue
            self.last_status = dict(status)
            self.last_status_received_at = now

    def _record_oms_heartbeat(self, healthy: bool, reason: str):
        record = getattr(self.oms, "record_risk_control_heartbeat", None)
        if callable(record):
            return bool(
                record(
                    source=SUPERVISOR_SOURCE,
                    healthy=healthy,
                    reason=str(reason or ""),
                )
            )
        return False

    def _reset_recovery_progress(self):
        self.recovery_count = 0
        try:
            snapshot_sequence = int(
                self.last_status.get("risk_snapshot_sequence", 0) or 0
            )
        except (AttributeError, TypeError, ValueError):
            snapshot_sequence = 0
        self.last_recovery_snapshot_sequence = max(
            0,
            snapshot_sequence,
        )

    def _apply_oms_health(self, healthy: bool, reason: str) -> bool:
        heartbeat_recorded = self._record_oms_heartbeat(healthy, reason)
        constraint_prefix = ("independent_supervisor:",)
        if not healthy:
            self._reset_recovery_progress()
            risk_action = str(
                self.last_status.get("risk_action", "") or ""
            ).upper()
            if risk_action == "KILL":
                trigger_kill = getattr(
                    self.risk_manager,
                    "trigger_kill_switch",
                    None,
                )
                if callable(trigger_kill):
                    trigger_kill(
                        f"IndependentSupervisor: {reason or 'hard risk breach'}"
                    )
                else:
                    halt = getattr(self.oms, "halt_system", None)
                    if callable(halt):
                        halt(
                            f"IndependentSupervisor: {reason or 'hard risk breach'}"
                        )
            set_mode = getattr(self.oms, "set_trading_mode", None)
            if callable(set_mode):
                from event.type import OMSCapabilityMode

                set_mode(
                    OMSCapabilityMode.REDUCE_ONLY,
                    f"independent_supervisor:{reason or 'unhealthy'}",
                )
            return False

        has_constraint = getattr(self.oms, "has_trading_mode_constraint", None)
        constrained = bool(
            callable(has_constraint) and has_constraint(constraint_prefix)
        )
        if not constrained:
            self._reset_recovery_progress()
            return heartbeat_recorded

        try:
            snapshot_sequence = int(
                self.last_status.get("risk_snapshot_sequence", 0) or 0
            )
            snapshot_captured_monotonic = float(
                self.last_status.get(
                    "risk_snapshot_captured_monotonic",
                    0.0,
                )
                or 0.0
            )
        except (AttributeError, TypeError, ValueError):
            self._reset_recovery_progress()
            return False
        now = time.perf_counter()
        snapshot_age = now - min(now, snapshot_captured_monotonic)
        if (
            snapshot_sequence <= 0
            or not math.isfinite(snapshot_captured_monotonic)
            or snapshot_captured_monotonic <= 0.0
            or snapshot_captured_monotonic > now
            or snapshot_age > self.recovery_snapshot_max_age_sec
        ):
            self._reset_recovery_progress()
            return False
        if snapshot_sequence < self.last_recovery_snapshot_sequence:
            self._reset_recovery_progress()
            return False
        if snapshot_sequence > self.last_recovery_snapshot_sequence:
            self.last_recovery_snapshot_sequence = snapshot_sequence
            self.recovery_count += 1
        if self.recovery_count < self.recovery_checks:
            return False

        clear_mode = getattr(self.oms, "clear_trading_mode", None)
        if callable(clear_mode):
            clear_mode(
                reason="independent risk supervisor recovered",
                prefixes=constraint_prefix,
            )
        self._reset_recovery_progress()
        return heartbeat_recorded

    def tick(self) -> bool:
        if not self.enabled:
            return True
        now = time.perf_counter()
        if self.process is None or not self.process.is_alive():
            self._apply_oms_health(False, "supervisor_process_down")
            return False
        self._send_heartbeat(now)
        self._drain_status(now)
        if not self.last_status:
            self._apply_oms_health(False, "supervisor_status_missing")
            return False
        status_age = max(0.0, now - self.last_status_received_at)
        if status_age > self.status_max_age_sec:
            self._apply_oms_health(False, "supervisor_status_stale")
            return False
        healthy = bool(self.last_status.get("healthy", False))
        reason = str(self.last_status.get("reason", "") or "")
        return self._apply_oms_health(healthy, reason)

    def wait_until_healthy(self, timeout_sec: float = 10.0) -> bool:
        if not self.enabled:
            return True
        deadline = time.perf_counter() + max(0.0, float(timeout_sec or 0.0))
        while time.perf_counter() <= deadline:
            if self.tick():
                return True
            time.sleep(0.05)
        return False

    def _control_failure_result(
        self,
        command_type: str,
        reason: str,
        request_id: str = "",
        **payload,
    ) -> dict:
        command_type = str(command_type or "").upper()
        result = {
            "accepted": False,
            "reason": str(reason or "supervisor_control_failed"),
            "request_id": str(request_id or ""),
        }
        if command_type == "QUIESCE":
            result.update(
                {
                    "quiesced": bool(
                        self.last_status.get("quiesced", False)
                    ),
                    "persisted": False,
                }
            )
        elif command_type == "RESUME_SHUTDOWN":
            result.update(
                {
                    "quiesced": bool(
                        self.last_status.get("quiesced", False)
                    ),
                    "kill_latched": bool(
                        self.last_status.get("kill_latched", False)
                    ),
                    "persisted": False,
                }
            )
        elif command_type == "STOP":
            result.update(
                {
                    "quiesced": bool(
                        self.last_status.get("quiesced", False)
                    ),
                    "cancel_requested": bool(
                        payload.get("cancel_orders", True)
                    ),
                    "cancel_attempted": False,
                    "cancel_ok": None,
                }
            )
        else:
            result["token"] = ""
        return result

    def _read_control_ack(
        self,
        command_type: str,
        request_id: str,
    ):
        command_type = str(command_type or "").upper()
        if command_type == "QUIESCE":
            if str(
                self.last_status.get(
                    "last_quiesce_request_id",
                    "",
                )
                or ""
            ) != request_id:
                return None
            return {
                "accepted": bool(
                    self.last_status.get(
                        "last_quiesce_accepted",
                        False,
                    )
                ),
                "reason": str(
                    self.last_status.get(
                        "last_quiesce_reason",
                        "",
                    )
                    or ""
                ),
                "request_id": request_id,
                "quiesced": bool(
                    self.last_status.get("quiesced", False)
                ),
                "persisted": bool(
                    self.last_status.get(
                        "last_quiesce_persisted",
                        False,
                    )
                ),
            }
        if command_type == "RESUME_SHUTDOWN":
            if str(
                self.last_status.get(
                    "last_shutdown_resume_request_id",
                    "",
                )
                or ""
            ) != request_id:
                return None
            return {
                "accepted": bool(
                    self.last_status.get(
                        "last_shutdown_resume_accepted",
                        False,
                    )
                ),
                "reason": str(
                    self.last_status.get(
                        "last_shutdown_resume_reason",
                        "",
                    )
                    or ""
                ),
                "request_id": request_id,
                "quiesced": bool(
                    self.last_status.get("quiesced", False)
                ),
                "kill_latched": bool(
                    self.last_status.get("kill_latched", False)
                ),
                "persisted": bool(
                    self.last_status.get(
                        "last_shutdown_resume_persisted",
                        False,
                    )
                ),
            }
        if command_type == "STOP":
            if str(
                self.last_status.get("last_stop_request_id", "") or ""
            ) != request_id:
                return None
            return {
                "accepted": bool(
                    self.last_status.get("last_stop_accepted", False)
                ),
                "reason": str(
                    self.last_status.get("last_stop_reason", "") or ""
                ),
                "request_id": request_id,
                "quiesced": bool(
                    self.last_status.get("last_stop_quiesced", False)
                ),
                "cancel_requested": bool(
                    self.last_status.get(
                        "last_stop_cancel_requested",
                        False,
                    )
                ),
                "cancel_attempted": bool(
                    self.last_status.get(
                        "last_stop_cancel_attempted",
                        False,
                    )
                ),
                "cancel_ok": self.last_status.get(
                    "last_stop_cancel_ok"
                ),
            }

        if str(
            self.last_status.get("last_rearm_request_id", "") or ""
        ) != request_id:
            return None
        return {
            "accepted": bool(
                self.last_status.get("last_rearm_accepted", False)
            ),
            "reason": str(
                self.last_status.get("last_rearm_reason", "") or ""
            ),
            "request_id": request_id,
            "token": str(
                self.last_status.get("last_rearm_token", "") or ""
            ),
        }

    def _request_sidecar_control(
        self,
        command_type: str,
        timeout_sec: float = None,
        **payload,
    ) -> dict:
        command_type = str(command_type or "").upper()
        if not self.enabled:
            result = self._control_failure_result(
                command_type,
                "supervisor_disabled",
                **payload,
            )
            if command_type == "QUIESCE":
                result.update(
                    {
                        "accepted": True,
                        "quiesced": True,
                        "persisted": True,
                    }
                )
            elif command_type == "RESUME_SHUTDOWN":
                result.update(
                    {
                        "accepted": True,
                        "quiesced": False,
                        "kill_latched": True,
                        "persisted": True,
                    }
                )
            elif command_type != "STOP":
                result["accepted"] = True
            return result
        if self.process is None or not self.process.is_alive():
            return self._control_failure_result(
                command_type,
                "supervisor_process_down",
                **payload,
            )
        request_id = secrets.token_hex(16)
        enqueued = _put_reliable(
            self.command_queue,
            {
                "type": command_type,
                "session_id": self.session_id,
                "request_id": request_id,
                **payload,
            },
            self.control_enqueue_timeout_sec,
        )
        if not enqueued:
            return self._control_failure_result(
                command_type,
                "supervisor_control_queue_full",
                request_id,
                **payload,
            )
        timeout = (
            self.rearm_command_timeout_sec
            if timeout_sec is None
            else max(0.0, float(timeout_sec or 0.0))
        )
        deadline = time.perf_counter() + timeout
        process_down_at = 0.0
        while time.perf_counter() <= deadline:
            now = time.perf_counter()
            process_alive = self.process.is_alive()
            if process_alive:
                self._send_heartbeat(now)
            self._drain_status(now)
            ack = self._read_control_ack(command_type, request_id)
            if ack is not None:
                return ack
            if not process_alive:
                if process_down_at <= 0.0:
                    process_down_at = now
                if (
                    command_type != "STOP"
                    or now - process_down_at >= 0.5
                ):
                    return self._control_failure_result(
                        command_type,
                        "supervisor_process_down_before_ack",
                        request_id,
                        **payload,
                    )
            time.sleep(0.02)
        return self._control_failure_result(
            command_type,
            f"supervisor_{command_type.lower()}_timeout",
            request_id,
            **payload,
        )

    def prepare_rearm(self, reason: str, timeout_sec: float = None) -> dict:
        return self._request_sidecar_control(
            "PREPARE_REARM",
            timeout_sec=timeout_sec,
            reason=str(reason or "operator_rearm"),
        )

    def commit_rearm(self, token: str, timeout_sec: float = None) -> dict:
        return self._request_sidecar_control(
            "COMMIT_REARM",
            timeout_sec=timeout_sec,
            token=str(token or ""),
        )

    def quiesce(
        self,
        reason: str = "parent_quiesce",
        timeout_sec: float = None,
    ) -> dict:
        result = self._request_sidecar_control(
            "QUIESCE",
            timeout_sec=timeout_sec,
            reason=str(reason or "parent_quiesce"),
        )
        if self.enabled and bool(result.get("quiesced", False)):
            self._apply_oms_health(False, "supervisor_quiesced")
        return result

    def resume_shutdown_guard(
        self,
        reason: str = "shutdown account truth drift",
        timeout_sec: float = None,
    ) -> dict:
        result = self._request_sidecar_control(
            "RESUME_SHUTDOWN",
            timeout_sec=timeout_sec,
            reason=str(reason or "shutdown account truth drift"),
        )
        if self.enabled and bool(result.get("accepted", False)):
            self._apply_oms_health(
                False,
                "supervisor_shutdown_guard_active",
            )
        return result

    def abort_rearm(self, token: str) -> bool:
        if not self.enabled:
            return True
        if self.process is None or not self.process.is_alive():
            return False
        return _put_reliable(
            self.command_queue,
            {
                "type": "ABORT_REARM",
                "session_id": self.session_id,
                "token": str(token or ""),
            },
            self.control_enqueue_timeout_sec,
        )

    def get_status_snapshot(self) -> dict:
        now = time.perf_counter()
        process_alive = bool(self.process is not None and self.process.is_alive())
        status_age = (
            max(0.0, now - self.last_status_received_at)
            if self.last_status_received_at > 0.0
            else None
        )
        return {
            "enabled": self.enabled,
            "process_alive": process_alive,
            "pid": getattr(self.process, "pid", None),
            "healthy": bool(
                not self.enabled
                or (
                    process_alive
                    and self.last_status.get("healthy", False)
                    and status_age is not None
                    and status_age <= self.status_max_age_sec
                )
            ),
            "reason": str(self.last_status.get("reason", "") or ""),
            "status_age_sec": status_age,
            "parent_sequence": int(
                self.last_status.get("parent_sequence", 0) or 0
            ),
            "parent_heartbeat_error": str(
                self.last_status.get("parent_heartbeat_error", "") or ""
            ),
            "exchange_healthy": bool(
                self.last_status.get("exchange_healthy", False)
            ),
            "last_cancel_ok": self.last_status.get("last_cancel_ok"),
            "last_cancel_reason": str(
                self.last_status.get("last_cancel_reason", "") or ""
            ),
            "risk_action": str(
                self.last_status.get("risk_action", "NONE") or "NONE"
            ),
            "risk_reason": str(
                self.last_status.get("risk_reason", "") or ""
            ),
            "funding_action": str(
                self.last_status.get("funding_action", "NONE") or "NONE"
            ),
            "funding_reason": str(
                self.last_status.get("funding_reason", "") or ""
            ),
            "stage": str(self.last_status.get("stage", "") or ""),
            "kill_latched": bool(
                self.last_status.get("kill_latched", False)
            ),
            "kill_reason": str(
                self.last_status.get("kill_reason", "") or ""
            ),
            "quiesced": bool(
                self.last_status.get("quiesced", False)
            ),
            "quiesce_reason": str(
                self.last_status.get("quiesce_reason", "") or ""
            ),
            "quiesced_at": float(
                self.last_status.get("quiesced_at", 0.0) or 0.0
            ),
            "state_path": str(
                self.last_status.get(
                    "state_path",
                    self.settings.get("state_path", ""),
                )
                or ""
            ),
            "state_generation": int(
                self.last_status.get("state_generation", 0) or 0
            ),
            "state_recovered": bool(
                self.last_status.get("state_recovered", False)
            ),
            "state_load_error": str(
                self.last_status.get("state_load_error", "") or ""
            ),
            "state_persist_error": str(
                self.last_status.get("state_persist_error", "") or ""
            ),
            "risk_metrics": dict(
                self.last_status.get("risk_metrics", {}) or {}
            ),
            "risk_snapshot_sequence": int(
                self.last_status.get("risk_snapshot_sequence", 0) or 0
            ),
            "risk_snapshot_captured_at": float(
                self.last_status.get("risk_snapshot_captured_at", 0.0)
                or 0.0
            ),
            "risk_snapshot_captured_monotonic": float(
                self.last_status.get(
                    "risk_snapshot_captured_monotonic",
                    0.0,
                )
                or 0.0
            ),
            "risk_snapshot_age_sec": self.last_status.get(
                "risk_snapshot_age_sec"
            ),
            "risk_snapshot_worker_inflight": bool(
                self.last_status.get(
                    "risk_snapshot_worker_inflight",
                    False,
                )
            ),
            "last_flatten_ok": self.last_status.get("last_flatten_ok"),
            "last_flatten_count": int(
                self.last_status.get("last_flatten_count", 0) or 0
            ),
            "last_flatten_reason": str(
                self.last_status.get("last_flatten_reason", "") or ""
            ),
            "last_quiesce_request_id": str(
                self.last_status.get(
                    "last_quiesce_request_id",
                    "",
                )
                or ""
            ),
            "last_quiesce_accepted": self.last_status.get(
                "last_quiesce_accepted"
            ),
            "last_quiesce_reason": str(
                self.last_status.get("last_quiesce_reason", "") or ""
            ),
            "last_quiesce_persisted": bool(
                self.last_status.get(
                    "last_quiesce_persisted",
                    False,
                )
            ),
            "last_shutdown_resume_request_id": str(
                self.last_status.get(
                    "last_shutdown_resume_request_id",
                    "",
                )
                or ""
            ),
            "last_shutdown_resume_accepted": self.last_status.get(
                "last_shutdown_resume_accepted"
            ),
            "last_shutdown_resume_reason": str(
                self.last_status.get(
                    "last_shutdown_resume_reason",
                    "",
                )
                or ""
            ),
            "last_shutdown_resume_persisted": bool(
                self.last_status.get(
                    "last_shutdown_resume_persisted",
                    False,
                )
            ),
            "last_stop_request_id": str(
                self.last_status.get("last_stop_request_id", "") or ""
            ),
            "last_stop_accepted": self.last_status.get(
                "last_stop_accepted"
            ),
            "last_stop_reason": str(
                self.last_status.get("last_stop_reason", "") or ""
            ),
            "last_stop_quiesced": bool(
                self.last_status.get("last_stop_quiesced", False)
            ),
            "last_stop_cancel_requested": bool(
                self.last_status.get(
                    "last_stop_cancel_requested",
                    False,
                )
            ),
            "last_stop_cancel_attempted": bool(
                self.last_status.get(
                    "last_stop_cancel_attempted",
                    False,
                )
            ),
            "last_stop_cancel_ok": self.last_status.get(
                "last_stop_cancel_ok"
            ),
        }

    def stop(self, cancel_orders: bool = True) -> dict:
        if not self.enabled:
            return {
                "accepted": not bool(cancel_orders),
                "reason": (
                    "supervisor_disabled"
                    if not cancel_orders
                    else "supervisor_disabled_cancel_not_attempted"
                ),
                "request_id": "",
                "quiesced": True,
                "cancel_requested": bool(cancel_orders),
                "cancel_attempted": False,
                "cancel_ok": None,
                "process_exited": True,
                "forced_terminated": False,
            }
        process = self.process
        if process is None:
            return {
                "accepted": False,
                "reason": "supervisor_process_missing",
                "request_id": "",
                "quiesced": bool(
                    self.last_status.get("quiesced", False)
                ),
                "cancel_requested": bool(cancel_orders),
                "cancel_attempted": False,
                "cancel_ok": None,
                "process_exited": True,
                "forced_terminated": False,
            }

        if process.is_alive():
            result = self._request_sidecar_control(
                "STOP",
                timeout_sec=self.stop_timeout_sec,
                cancel_orders=bool(cancel_orders),
            )
        else:
            result = self._control_failure_result(
                "STOP",
                "supervisor_process_down_before_ack",
                cancel_orders=bool(cancel_orders),
            )

        forced_terminated = False
        if bool(result.get("accepted", False)) and bool(
            result.get("quiesced", False)
        ):
            process.join(min(1.0, self.stop_timeout_sec))
            if process.is_alive():
                process.terminate()
                process.join(2.0)
                forced_terminated = True

        process_exited = not process.is_alive()
        result = {
            **result,
            "process_exited": process_exited,
            "forced_terminated": forced_terminated,
        }
        if process_exited:
            self._apply_oms_health(False, "supervisor_stopped")
            for channel in (
                self.command_queue,
                self.heartbeat_queue,
                self.status_queue,
            ):
                close = getattr(channel, "close", None)
                if callable(close):
                    close()
        else:
            self._apply_oms_health(
                False,
                "supervisor_stop_not_acknowledged:"
                f"{result.get('reason', 'unknown')}",
            )
        return result
