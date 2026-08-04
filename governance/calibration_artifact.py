"""Build a GLFT RPI calibration artifact from a local OMS journal.

This module is deliberately offline-only. It validates journal bytes and
fits pure RPI exposure math without importing an OMS, gateway, or transport.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from alpha.rpi_intensity import (  # noqa: E402
    RPIExposureBin,
    RPIIntensityRequirements,
    estimate_rpi_intensity,
)
from governance.deployment_identity import (  # noqa: E402
    deployment_config_sha256,
)
from governance.contracts import (  # noqa: E402
    RPI_CALIBRATION_ARTIFACT_SCHEMA,
    RPI_EXPOSURE_SAMPLE_SCHEMA,
)
from governance.strategy_identity import (  # noqa: E402
    canonical_model_key,
    implementation_sha256_for_model,
    strategy_policy_sha256,
)
from strategy.quote_math import (  # noqa: E402
    GLFT_FORMULA_VERSION,
    UNITS_VERSION,
)


JOURNAL_RECORD_VERSION = 2
SAMPLE_KIND = "rpi_exposure_sample"
SAMPLE_SCHEMA = RPI_EXPOSURE_SAMPLE_SCHEMA
ARTIFACT_SCHEMA = RPI_CALIBRATION_ARTIFACT_SCHEMA
EXPECTED_STRATEGY = "GLFT_MultiScale"
EXPECTED_MODEL = "glft"
EXPECTED_VENUE = "BINANCE_USDM"
EXPECTED_DATA_SOURCE = "LIVE_BINANCE_RPI_ACK"
EXPECTED_UNITS_VERSION = UNITS_VERSION
EXPECTED_FORMULA_VERSION = GLFT_FORMULA_VERSION
MAX_SAMPLE_APPEND_LAG_SECONDS = 5.0
USDT_MICRO_SCALE = Decimal("1000000")
NANOSECONDS_PER_SECOND = Decimal("1000000000")
CALIBRATION_JOURNAL_SCHEMA = "chronoshft.oms_rpi_calibration_quota.v1"
CALIBRATION_ACTIVATION_KIND = "rpi_calibration_permit_activated"
CALIBRATION_RESERVATION_KIND = "rpi_calibration_send_reserved"
CALIBRATION_EXPIRY_KIND = "rpi_calibration_permit_expired"
CALIBRATION_BYPASS_KIND = "rpi_calibration_emergency_reduce_bypass"
CALIBRATION_KINDS = frozenset(
    {
        CALIBRATION_ACTIVATION_KIND,
        CALIBRATION_RESERVATION_KIND,
        CALIBRATION_EXPIRY_KIND,
        CALIBRATION_BYPASS_KIND,
    }
)

_JOURNAL_KEYS = frozenset(
    {"version", "seq", "ts", "kind", "payload", "prev_hash", "hash"}
)
_SAMPLE_KEYS = frozenset(
    {
        "schema",
        "strategy",
        "symbol",
        "client_oid",
        "exchange_oid",
        "terminal_status",
        "side",
        "price",
        "quantity",
        "ack_time",
        "ack_monotonic",
        "terminal_time",
        "terminal_monotonic",
        "deployment_id",
        "strategy_policy_sha256",
        "implementation_sha256",
        "exposure_bins",
        "fill_count",
        "censored",
        "censor_reason",
        "units_version",
        "formula_version",
        "data_source",
    }
)
_TERMINAL_STATUSES = frozenset(
    {
        "FILLED",
        "CANCELLED",
        "REJECTED",
        "REJECTED_LOCALLY",
        "EXPIRED",
    }
)
_EXCHANGE_ACK_STATUSES = frozenset(
    {
        "NEW",
        "PARTIALLY_FILLED",
        "FILLED",
    }
)
_EXECUTION_STATUSES = frozenset({"PARTIALLY_FILLED", "FILLED"})
_ORDER_SNAPSHOT_REQUIRED_KEYS = frozenset(
    {
        "client_oid",
        "exchange_oid",
        "status",
        "filled_volume",
        "avg_price",
        "cumulative_cost",
        "created_at",
        "updated_at",
        "created_monotonic",
        "updated_monotonic",
        "recovered_from_journal",
        "error_msg",
        "last_update_seq",
        "last_exchange_status",
        "last_exchange_update_time",
        "intent",
        "source",
    }
)
_LEGACY_INTENT_KEYS = frozenset(
    {
        "strategy_id",
        "symbol",
        "side",
        "price",
        "volume",
        "order_type",
        "time_in_force",
        "is_post_only",
        "reduce_only",
        "policy",
        "tag",
    }
)
_CALIBRATION_INTENT_KEYS = frozenset(
    {
        "calibration_permit_id",
        "calibration_depth_bps",
        "calibration_reference_mid",
    }
)
_EXECUTION_KEYS = frozenset(
    {
        "execution_id",
        "venue",
        "client_oid",
        "exchange_oid",
        "strategy_id",
        "symbol",
        "side",
        "fill_qty",
        "fill_price",
        "cum_filled_qty",
        "exchange_status",
        "exchange_time",
        "trade_id",
        "commission",
        "commission_asset",
        "booked_fee",
        "realized_pnl",
        "is_maker",
        "pre_status",
    }
)
_CALIBRATION_ACTIVATION_KEYS = frozenset(
    {
        "schema",
        "signed_permit",
        "permit_id",
        "permit_sha256",
        "deployment_id",
        "stage",
        "venue",
        "symbol",
        "model",
        "calibration_config_sha256",
        "target_deployment_config_sha256",
        "strategy_policy_sha256",
        "implementation_sha256",
        "activated_at_exchange_ns",
        "not_before_exchange_ns",
        "expires_at_exchange_ns",
        "fixed_depths_bps",
        "order_ttl_ns",
        "min_order_interval_ns",
        "max_active_orders",
        "max_order_count",
        "min_order_notional_microu",
        "max_order_notional_microu",
        "max_cumulative_submitted_notional_microu",
        "max_calibration_loss_microu",
        "effective_deployment_loss_cap_microu",
        "deployment_start_equity_microu",
        "deployment_start_external_cash_flow_microu",
        "peak_observed_loss_microu",
        "starting_reserved_order_count",
        "starting_cumulative_submitted_notional_microu",
    }
)
_CALIBRATION_RESERVATION_KEYS = frozenset(
    {
        "schema",
        "reservation_seq",
        "permit_reservation_seq",
        "reservation_id",
        "client_oid",
        "permit_id",
        "permit_sha256",
        "deployment_id",
        "calibration_config_sha256",
        "target_deployment_config_sha256",
        "strategy_policy_sha256",
        "implementation_sha256",
        "reserved_at_exchange_ns",
        "symbol",
        "strategy_id",
        "side",
        "price",
        "quantity",
        "declared_depth_bps",
        "calibration_reference_mid",
        "order_type",
        "time_in_force",
        "post_only",
        "reduce_only",
        "submitted_notional_microu",
        "cumulative_submitted_notional_microu",
        "permit_cumulative_submitted_notional_microu",
        "loss_before_send_microu",
        "effective_deployment_loss_cap_microu",
    }
)
_CALIBRATION_EXPIRY_KEYS = frozenset(
    {
        "schema",
        "signed_permit",
        "permit_id",
        "permit_sha256",
        "deployment_id",
        "symbol",
        "calibration_config_sha256",
        "target_deployment_config_sha256",
        "strategy_policy_sha256",
        "implementation_sha256",
        "reason",
        "budget_exhausted",
        "expired_at_exchange_ns",
        "reserved_order_count",
        "cumulative_submitted_notional_microu",
        "deployment_start_equity_microu",
        "deployment_start_external_cash_flow_microu",
        "peak_observed_loss_microu",
        "effective_deployment_loss_cap_microu",
    }
)
_CALIBRATION_BYPASS_KEYS = frozenset(
    {
        "schema",
        "bypass_id",
        "client_oid",
        "permit_id",
        "permit_sha256",
        "deployment_id",
        "recorded_at_exchange_ns",
        "symbol",
        "side",
        "price",
        "quantity",
        "reduce_only",
        "estimated_notional_microu",
        "reason",
    }
)
_EXCHANGE_STATUS_BY_TERMINAL_STATUS = {
    "FILLED": "FILLED",
    "CANCELLED": "CANCELED",
    "REJECTED": "REJECTED",
    "EXPIRED": "EXPIRED",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,32}$")
_CLIENT_OID_RE = re.compile(r"^[.A-Z:/a-z0-9_-]{1,36}$")


class CalibrationArtifactError(ValueError):
    """Raised when trusted calibration output cannot be produced."""


@dataclass(frozen=True)
class _EvidenceRecord:
    seq: int
    line_number: int
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class _ValidatedSample:
    seq: int
    line_number: int
    record_at_utc: str
    symbol: str
    client_oid: str
    exchange_oid: str
    terminal_status: str
    side: str
    price: float
    quantity: float
    ack_time: float
    ack_monotonic: float
    terminal_time: float
    terminal_monotonic: float
    deployment_id: str
    strategy_policy_sha256: str
    implementation_sha256: str
    reported_fill_count: int
    censored: bool
    censor_reason: str
    exposure_bins: tuple[RPIExposureBin, ...]


@dataclass(frozen=True)
class _OrderSnapshotEvidence:
    seq: int
    line_number: int
    status: str
    source: str
    exchange_oid: str
    price: float
    filled_volume: float
    avg_price: float
    cumulative_cost: float
    intent_volume: float
    side: str
    reduce_only: bool
    tag: str
    created_at: float
    updated_at: float
    created_monotonic: float
    updated_monotonic: float
    recovered_from_journal: bool
    last_exchange_status: str
    last_exchange_update_time: float
    calibration_permit_id: str
    calibration_depth_bps: float | None
    calibration_reference_mid: float | None


@dataclass(frozen=True)
class _ExecutionEvidence:
    seq: int
    line_number: int
    execution_id: str
    exchange_oid: str
    side: str
    fill_qty: float
    fill_price: float
    cumulative_qty: float
    exchange_status: str
    exchange_time: float
    trade_id: int


@dataclass(frozen=True)
class _CalibrationActivation:
    seq: int
    line_number: int
    permit_id: str
    permit_sha256: str
    deployment_id: str
    symbol: str
    calibration_config_sha256: str
    target_deployment_config_sha256: str
    strategy_policy_sha256: str
    implementation_sha256: str
    activated_at_exchange_ns: int
    not_before_exchange_ns: int
    expires_at_exchange_ns: int
    fixed_depths_bps: tuple[str, ...]
    order_ttl_ns: int
    min_order_interval_ns: int
    max_active_orders: int
    max_order_count: int
    min_order_notional_microu: int
    max_order_notional_microu: int
    max_cumulative_submitted_notional_microu: int
    max_calibration_loss_microu: int
    effective_deployment_loss_cap_microu: int
    deployment_start_equity_microu: int
    deployment_start_external_cash_flow_microu: int
    peak_observed_loss_microu: int
    starting_reserved_order_count: int
    starting_cumulative_submitted_notional_microu: int


@dataclass(frozen=True)
class _CalibrationReservation:
    seq: int
    line_number: int
    reservation_seq: int
    permit_reservation_seq: int
    client_oid: str
    permit_id: str
    permit_sha256: str
    deployment_id: str
    calibration_config_sha256: str
    target_deployment_config_sha256: str
    strategy_policy_sha256: str
    implementation_sha256: str
    reserved_at_exchange_ns: int
    symbol: str
    side: str
    price: Decimal
    quantity: Decimal
    declared_depth_bps: Decimal
    calibration_reference_mid: Decimal
    reduce_only: bool
    submitted_notional_microu: int
    cumulative_submitted_notional_microu: int
    permit_cumulative_submitted_notional_microu: int


@dataclass(frozen=True)
class RPIJournalValidationSummary:
    journal_path: str
    journal_sha256: str
    first_seq: int
    last_seq: int
    final_hash: str
    record_count: int
    first_record_at_utc: str
    last_record_at_utc: str
    first_sample_at_utc: str
    last_sample_at_utc: str
    first_ack_at_utc: str
    last_terminal_at_utc: str
    sample_count: int
    unique_order_count: int
    censored_sample_count: int
    deployment_id: str
    strategy_policy_sha256: str
    implementation_sha256: str
    calibration_config_sha256: str
    target_deployment_config_sha256: str
    permit_activation_count: int
    permit_sha256s: tuple[str, ...]
    reservation_count: int
    cumulative_submitted_notional_microu: int
    exposure_bins: tuple[RPIExposureBin, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "journal_path": self.journal_path,
            "journal_sha256": self.journal_sha256,
            "first_seq": self.first_seq,
            "last_seq": self.last_seq,
            "final_hash": self.final_hash,
            "record_count": self.record_count,
            "first_record_at_utc": self.first_record_at_utc,
            "last_record_at_utc": self.last_record_at_utc,
            "first_sample_at_utc": self.first_sample_at_utc,
            "last_sample_at_utc": self.last_sample_at_utc,
            "first_ack_at_utc": self.first_ack_at_utc,
            "last_terminal_at_utc": self.last_terminal_at_utc,
            "sample_count": self.sample_count,
            "unique_order_count": self.unique_order_count,
            "censored_sample_count": self.censored_sample_count,
            "deployment_id": self.deployment_id,
            "strategy_policy_sha256": self.strategy_policy_sha256,
            "implementation_sha256": self.implementation_sha256,
            "calibration_config_sha256": (
                self.calibration_config_sha256
            ),
            "target_deployment_config_sha256": (
                self.target_deployment_config_sha256
            ),
            "permit_activation_count": self.permit_activation_count,
            "permit_sha256s": list(self.permit_sha256s),
            "reservation_count": self.reservation_count,
            "cumulative_submitted_notional_microu": (
                self.cumulative_submitted_notional_microu
            ),
        }


def _canonical_json(value: Mapping[str, Any]) -> str:
    """Match the canonical encoding used by ``oms/journal.py``."""
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _reject_json_constant(value: str) -> None:
    raise CalibrationArtifactError(
        f"non-standard JSON number {value!r} is not allowed"
    )


def _object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CalibrationArtifactError(
                f"duplicate JSON object key {key!r} is not allowed"
            )
        result[key] = value
    return result


def _parse_record(raw: str, line_number: int) -> dict[str, Any]:
    try:
        record = json.loads(
            raw,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (json.JSONDecodeError, CalibrationArtifactError) as exc:
        raise CalibrationArtifactError(
            f"invalid OMS journal JSON at line {line_number}: {exc}"
        ) from exc
    if not isinstance(record, dict):
        raise CalibrationArtifactError(
            f"OMS journal record at line {line_number} must be an object"
        )
    if set(record) != _JOURNAL_KEYS:
        raise CalibrationArtifactError(
            f"OMS journal v2 record keys are invalid at line {line_number}"
        )
    return record


def _positive_int(value: Any, field: str, line_number: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CalibrationArtifactError(
            f"{field} must be a positive integer at line {line_number}"
        )
    return value


def _finite_number(
    value: Any,
    field: str,
    line_number: int,
    *,
    positive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CalibrationArtifactError(
            f"{field} must be a JSON number at line {line_number}"
        )
    try:
        parsed = float(value)
    except (OverflowError, ValueError) as exc:
        raise CalibrationArtifactError(
            f"{field} must be finite at line {line_number}"
        ) from exc
    invalid = not math.isfinite(parsed) or parsed < 0.0
    if positive:
        invalid = invalid or parsed == 0.0
    if invalid:
        qualifier = "positive and finite" if positive else "non-negative and finite"
        raise CalibrationArtifactError(
            f"{field} must be {qualifier} at line {line_number}"
        )
    return parsed


def _fill_count(value: Any, line_number: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CalibrationArtifactError(
            f"fill_count must be a non-negative integer at line {line_number}"
        )
    return value


def _validated_symbol(value: Any) -> str:
    if not isinstance(value, str) or not _SYMBOL_RE.fullmatch(value):
        raise CalibrationArtifactError(
            "symbol must be an exact uppercase venue symbol"
        )
    return value


def _validated_sha256(
    value: Any,
    field: str,
    line_number: int,
) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise CalibrationArtifactError(
            f"{field} must be a lowercase SHA-256 at line {line_number}"
        )
    return value


def _exact_payload(
    payload: Any,
    expected_keys: frozenset[str],
    kind: str,
    line_number: int,
) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != expected_keys:
        raise CalibrationArtifactError(
            f"{kind} payload keys are invalid at line {line_number}"
        )
    return payload


def _journal_int(
    payload: Mapping[str, Any],
    field: str,
    line_number: int,
    *,
    minimum: int | None = 0,
) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CalibrationArtifactError(
            f"{field} must be an integer at line {line_number}"
        )
    if minimum is not None and value < minimum:
        raise CalibrationArtifactError(
            f"{field} must be at least {minimum} at line {line_number}"
        )
    return value


def _journal_decimal(
    value: Any,
    field: str,
    line_number: int,
) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise CalibrationArtifactError(
            f"{field} must be a finite positive decimal at line {line_number}"
        )
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CalibrationArtifactError(
            f"{field} must be a finite positive decimal at line {line_number}"
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise CalibrationArtifactError(
            f"{field} must be a finite positive decimal at line {line_number}"
        )
    return parsed


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _usdt_to_microu(value: Decimal, *, upper_bound: bool) -> int:
    rounding = ROUND_FLOOR if upper_bound else ROUND_CEILING
    return int(
        (value * USDT_MICRO_SCALE).to_integral_value(rounding=rounding)
    )


def _utc_timestamp_to_ns(value: Any, field: str, line_number: int) -> int:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise CalibrationArtifactError(
            f"{field} must be an ISO-8601 UTC timestamp at line {line_number}"
        )
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise CalibrationArtifactError(
            f"{field} must be an ISO-8601 UTC timestamp at line {line_number}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CalibrationArtifactError(
            f"{field} must include a UTC offset at line {line_number}"
        )
    parsed = parsed.astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = parsed - epoch
    return (
        (delta.days * 86_400 + delta.seconds) * 1_000_000_000
        + delta.microseconds * 1_000
    )


def _normalized_single_symbol(
    config: Mapping[str, Any],
    field: str,
) -> str:
    symbols = config.get("symbols")
    if not isinstance(symbols, (list, tuple)) or len(symbols) != 1:
        raise CalibrationArtifactError(
            f"{field} must contain exactly one symbol"
        )
    return _validated_symbol(symbols[0])


class _CalibrationJournalReplay:
    def __init__(
        self,
        *,
        symbol: str,
        calibration_config: Mapping[str, Any],
        target_deployment_config: Mapping[str, Any],
        calibration_config_path: str | Path | None,
        target_deployment_config_path: str | Path | None,
    ) -> None:
        if not isinstance(calibration_config, Mapping):
            raise CalibrationArtifactError(
                "calibration_config must be an effective configuration object"
            )
        if not isinstance(target_deployment_config, Mapping):
            raise CalibrationArtifactError(
                "target_deployment_config must be an effective configuration "
                "object"
            )

        calibration_launch = calibration_config.get("live_launch")
        target_launch = target_deployment_config.get("live_launch")
        if not isinstance(calibration_launch, Mapping) or not isinstance(
            target_launch,
            Mapping,
        ):
            raise CalibrationArtifactError(
                "calibration and target live_launch sections are required"
            )
        if (
            calibration_launch.get("stage") != "rpi_calibration_canary"
            or target_launch.get("stage") != "canary"
        ):
            raise CalibrationArtifactError(
                "journal authorization requires calibration stage "
                "'rpi_calibration_canary' and target stage 'canary'"
            )
        deployment_id = str(
            calibration_launch.get("deployment_id", "") or ""
        ).strip()
        if (
            not deployment_id
            or deployment_id
            != str(target_launch.get("deployment_id", "") or "").strip()
        ):
            raise CalibrationArtifactError(
                "calibration and target deployment_id values must match"
            )
        if (
            _normalized_single_symbol(
                calibration_config,
                "calibration config symbols",
            )
            != symbol
            or _normalized_single_symbol(
                target_deployment_config,
                "target config symbols",
            )
            != symbol
        ):
            raise CalibrationArtifactError(
                "calibration and target configs must contain the requested "
                "symbol"
            )

        calibration_strategy = calibration_config.get("strategy")
        target_strategy = target_deployment_config.get("strategy")
        if not isinstance(calibration_strategy, Mapping) or not isinstance(
            target_strategy,
            Mapping,
        ):
            raise CalibrationArtifactError(
                "calibration and target strategy sections are required"
            )
        calibration_model = canonical_model_key(
            calibration_strategy.get(
                "primary_model",
                calibration_strategy.get("name"),
            )
        )
        target_model = canonical_model_key(
            target_strategy.get(
                "primary_model",
                target_strategy.get("name"),
            )
        )
        if calibration_model != EXPECTED_MODEL or target_model != EXPECTED_MODEL:
            raise CalibrationArtifactError(
                "RPI calibration journal authorization requires GLFT"
            )

        calibration_digest = deployment_config_sha256(calibration_config)
        target_digest = deployment_config_sha256(target_deployment_config)
        if hmac.compare_digest(calibration_digest, target_digest):
            raise CalibrationArtifactError(
                "calibration and target deployment config digests must differ"
            )
        policy_digest = strategy_policy_sha256(
            calibration_config,
            calibration_model,
        )
        if not hmac.compare_digest(
            policy_digest,
            strategy_policy_sha256(
                target_deployment_config,
                target_model,
            ),
        ):
            raise CalibrationArtifactError(
                "calibration and target strategy policy digests must match"
            )
        implementation_digest = implementation_sha256_for_model(
            calibration_model
        )
        trusted_signers = calibration_launch.get(
            "calibration_permit_trusted_signers"
        )
        if not isinstance(trusted_signers, Mapping) or not trusted_signers:
            raise CalibrationArtifactError(
                "calibration permit trusted signer keyring is required"
            )

        self.symbol = symbol
        self.deployment_id = deployment_id
        self.calibration_config = calibration_config
        self.target_deployment_config = target_deployment_config
        self.calibration_config_sha256 = calibration_digest
        self.target_deployment_config_sha256 = target_digest
        self.strategy_policy_sha256 = policy_digest
        self.implementation_sha256 = implementation_digest
        self.trusted_signers = trusted_signers
        self.calibration_base_dir = (
            Path(calibration_config_path).resolve().parent
            if calibration_config_path is not None
            else None
        )
        self.target_base_dir = (
            Path(target_deployment_config_path).resolve().parent
            if target_deployment_config_path is not None
            else None
        )

        self.activations: dict[str, _CalibrationActivation] = {}
        self.reservations: dict[str, _CalibrationReservation] = {}
        self.permit_identities: dict[str, tuple[str, str]] = {}
        self.signed_windows: dict[str, tuple[str, int, int]] = {}
        self.expired_permits: set[str] = set()
        self.send_ids: set[str] = set()
        self.open_permit_id = ""
        self.reserved_order_count = 0
        self.cumulative_notional_microu = 0
        self.last_reserved_exchange_ns = 0
        self.last_expired_exchange_ns = 0
        self.latest_signed_window_end_ns = 0
        self.deployment_start_equity_microu = 0
        self.deployment_start_external_cash_flow_microu = 0
        self.effective_loss_cap_microu = 0
        self.peak_observed_loss_microu = 0
        self.last_bypass_exchange_ns = 0

    def consume(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        seq: int,
        line_number: int,
    ) -> None:
        if kind == CALIBRATION_ACTIVATION_KIND:
            self._activate(payload, seq=seq, line_number=line_number)
        elif kind == CALIBRATION_RESERVATION_KIND:
            self._reserve(payload, seq=seq, line_number=line_number)
        elif kind == CALIBRATION_EXPIRY_KIND:
            self._expire(payload, line_number=line_number)
        elif kind == CALIBRATION_BYPASS_KIND:
            self._bypass(payload, line_number=line_number)

    def _validate_signed_permit(
        self,
        signed_permit: Any,
        permit_sha256: str,
        *,
        validation_ns: int,
        line_number: int,
    ) -> Mapping[str, Any]:
        from infrastructure.rpi_calibration_permit import (
            RpiCalibrationPermitError,
            validate_rpi_calibration_permit,
        )

        try:
            validated = validate_rpi_calibration_permit(
                signed_permit,
                calibration_config=self.calibration_config,
                target_deployment_config=self.target_deployment_config,
                trusted_signers=self.trusted_signers,
                now_utc=datetime.fromtimestamp(
                    validation_ns / 1_000_000_000,
                    tz=timezone.utc,
                ),
                calibration_config_base_dir=self.calibration_base_dir,
                target_deployment_config_base_dir=self.target_base_dir,
            )
        except (RpiCalibrationPermitError, ValueError) as exc:
            raise CalibrationArtifactError(
                "cannot independently validate signed calibration permit at "
                f"line {line_number}: {exc}"
            ) from exc
        if not hmac.compare_digest(
            validated["permit_sha256"],
            permit_sha256,
        ):
            raise CalibrationArtifactError(
                f"signed calibration permit hash mismatch at line {line_number}"
            )
        return validated["permit"]

    def _register_signed_window(
        self,
        *,
        permit_id: str,
        permit_sha256: str,
        not_before_ns: int,
        expires_at_ns: int,
        line_number: int,
    ) -> None:
        signed_identity = (permit_sha256, not_before_ns, expires_at_ns)
        existing = self.signed_windows.get(permit_id)
        if existing is not None:
            if existing != signed_identity:
                raise CalibrationArtifactError(
                    f"signed permit identity changed at line {line_number}"
                )
            return
        if not_before_ns < self.latest_signed_window_end_ns:
            raise CalibrationArtifactError(
                "calibration signed permit validity windows overlap at line "
                f"{line_number}"
            )
        if permit_sha256 in {
            identity[0] for identity in self.signed_windows.values()
        }:
            raise CalibrationArtifactError(
                f"duplicate signed permit hash at line {line_number}"
            )
        self.signed_windows[permit_id] = signed_identity
        self.latest_signed_window_end_ns = expires_at_ns

    def _activate(
        self,
        raw_payload: Mapping[str, Any],
        *,
        seq: int,
        line_number: int,
    ) -> None:
        payload = _exact_payload(
            raw_payload,
            _CALIBRATION_ACTIVATION_KEYS,
            CALIBRATION_ACTIVATION_KIND,
            line_number,
        )
        if payload.get("schema") != CALIBRATION_JOURNAL_SCHEMA:
            raise CalibrationArtifactError(
                f"invalid calibration activation schema at line {line_number}"
            )
        permit_id = str(payload.get("permit_id", "") or "").strip()
        if not permit_id or len(permit_id) > 128:
            raise CalibrationArtifactError(
                f"invalid calibration permit_id at line {line_number}"
            )
        permit_sha256 = _validated_sha256(
            payload.get("permit_sha256"),
            "calibration permit_sha256",
            line_number,
        )
        if (
            permit_id in self.activations
            or permit_id in self.expired_permits
            or permit_sha256
            in {item.permit_sha256 for item in self.activations.values()}
        ):
            raise CalibrationArtifactError(
                f"duplicate calibration permit activation at line {line_number}"
            )
        if self.open_permit_id:
            raise CalibrationArtifactError(
                "calibration permit activation overlaps an unexpired permit "
                f"at line {line_number}"
            )

        activated_at_ns = _journal_int(
            payload,
            "activated_at_exchange_ns",
            line_number,
            minimum=1,
        )
        not_before_ns = _journal_int(
            payload,
            "not_before_exchange_ns",
            line_number,
            minimum=1,
        )
        expires_at_ns = _journal_int(
            payload,
            "expires_at_exchange_ns",
            line_number,
            minimum=1,
        )
        if not not_before_ns <= activated_at_ns < expires_at_ns:
            raise CalibrationArtifactError(
                f"permit activation is outside its window at line {line_number}"
            )
        if activated_at_ns < self.last_expired_exchange_ns:
            raise CalibrationArtifactError(
                f"permit activation time moved backwards at line {line_number}"
            )

        signed_permit = payload.get("signed_permit")
        permit = self._validate_signed_permit(
            signed_permit,
            permit_sha256,
            validation_ns=activated_at_ns,
            line_number=line_number,
        )
        identity = {
            "permit_id": permit_id,
            "deployment_id": self.deployment_id,
            "stage": "rpi_calibration_canary",
            "venue": EXPECTED_VENUE,
            "symbol": self.symbol,
            "model": EXPECTED_MODEL,
            "calibration_config_sha256": (
                self.calibration_config_sha256
            ),
            "target_deployment_config_sha256": (
                self.target_deployment_config_sha256
            ),
            "strategy_policy_sha256": self.strategy_policy_sha256,
            "implementation_sha256": self.implementation_sha256,
        }
        if any(
            payload.get(field) != expected
            or permit.get(field) != expected
            for field, expected in identity.items()
        ):
            raise CalibrationArtifactError(
                f"calibration activation identity mismatch at line {line_number}"
            )
        if (
            _utc_timestamp_to_ns(
                permit.get("not_before_utc"),
                "permit.not_before_utc",
                line_number,
            )
            != not_before_ns
            or _utc_timestamp_to_ns(
                permit.get("expires_at_utc"),
                "permit.expires_at_utc",
                line_number,
            )
            != expires_at_ns
        ):
            raise CalibrationArtifactError(
                f"calibration permit timestamps mismatch at line {line_number}"
            )
        self._register_signed_window(
            permit_id=permit_id,
            permit_sha256=permit_sha256,
            not_before_ns=not_before_ns,
            expires_at_ns=expires_at_ns,
            line_number=line_number,
        )

        policy = permit["policy"]
        signed_depths = tuple(
            _decimal_text(
                _journal_decimal(
                    value,
                    "permit.policy.fixed_depths_bps",
                    line_number,
                )
            )
            for value in policy["fixed_depths_bps"]
        )
        raw_depths = payload.get("fixed_depths_bps")
        if not isinstance(raw_depths, list):
            raise CalibrationArtifactError(
                f"fixed_depths_bps must be a list at line {line_number}"
            )
        journal_depths = tuple(
            _decimal_text(
                _journal_decimal(
                    value,
                    "activation.fixed_depths_bps",
                    line_number,
                )
            )
            for value in raw_depths
        )
        if journal_depths != signed_depths:
            raise CalibrationArtifactError(
                f"activation depth schedule mismatch at line {line_number}"
            )

        expected_limits = {
            "order_ttl_ns": int(
                (
                    _journal_decimal(
                        policy["order_ttl_sec"],
                        "permit.policy.order_ttl_sec",
                        line_number,
                    )
                    * NANOSECONDS_PER_SECOND
                ).to_integral_value(rounding=ROUND_CEILING)
            ),
            "min_order_interval_ns": int(
                (
                    _journal_decimal(
                        policy["min_order_interval_sec"],
                        "permit.policy.min_order_interval_sec",
                        line_number,
                    )
                    * NANOSECONDS_PER_SECOND
                ).to_integral_value(rounding=ROUND_CEILING)
            ),
            "max_active_orders": policy["max_active_orders"],
            "max_order_count": policy["max_order_count"],
            "min_order_notional_microu": _usdt_to_microu(
                _journal_decimal(
                    policy["min_order_notional_usdt"],
                    "permit.policy.min_order_notional_usdt",
                    line_number,
                ),
                upper_bound=False,
            ),
            "max_order_notional_microu": _usdt_to_microu(
                _journal_decimal(
                    policy["max_order_notional_usdt"],
                    "permit.policy.max_order_notional_usdt",
                    line_number,
                ),
                upper_bound=True,
            ),
            "max_cumulative_submitted_notional_microu": _usdt_to_microu(
                _journal_decimal(
                    policy["max_cumulative_submitted_notional_usdt"],
                    "permit.policy.max_cumulative_submitted_notional_usdt",
                    line_number,
                ),
                upper_bound=True,
            ),
            "max_calibration_loss_microu": _usdt_to_microu(
                _journal_decimal(
                    policy["max_calibration_loss_usdt"],
                    "permit.policy.max_calibration_loss_usdt",
                    line_number,
                ),
                upper_bound=True,
            ),
        }
        limits = {
            field: _journal_int(
                payload,
                field,
                line_number,
                minimum=1,
            )
            for field in expected_limits
        }
        if limits != expected_limits:
            raise CalibrationArtifactError(
                f"activation limits differ from signed policy at line {line_number}"
            )

        starting_count = _journal_int(
            payload,
            "starting_reserved_order_count",
            line_number,
        )
        starting_cumulative = _journal_int(
            payload,
            "starting_cumulative_submitted_notional_microu",
            line_number,
        )
        if (
            starting_count != self.reserved_order_count
            or starting_cumulative != self.cumulative_notional_microu
        ):
            raise CalibrationArtifactError(
                f"calibration deployment quota reset at line {line_number}"
            )
        start_equity = _journal_int(
            payload,
            "deployment_start_equity_microu",
            line_number,
            minimum=1,
        )
        start_cash_flow = _journal_int(
            payload,
            "deployment_start_external_cash_flow_microu",
            line_number,
            minimum=None,
        )
        peak_observed_loss = _journal_int(
            payload,
            "peak_observed_loss_microu",
            line_number,
        )
        effective_loss_cap = _journal_int(
            payload,
            "effective_deployment_loss_cap_microu",
            line_number,
            minimum=1,
        )
        expected_loss_cap = min(
            (
                self.effective_loss_cap_microu
                or limits["max_calibration_loss_microu"]
            ),
            limits["max_calibration_loss_microu"],
        )
        if effective_loss_cap != expected_loss_cap:
            raise CalibrationArtifactError(
                f"calibration loss cap widened at line {line_number}"
            )
        if self.deployment_start_equity_microu and (
            start_equity != self.deployment_start_equity_microu
            or start_cash_flow
            != self.deployment_start_external_cash_flow_microu
        ):
            raise CalibrationArtifactError(
                f"calibration loss baseline reset at line {line_number}"
            )
        if (
            peak_observed_loss < self.peak_observed_loss_microu
            or peak_observed_loss >= effective_loss_cap
        ):
            raise CalibrationArtifactError(
                "calibration peak loss regressed or a breached permit was "
                f"activated at line {line_number}"
            )

        activation = _CalibrationActivation(
            seq=seq,
            line_number=line_number,
            permit_id=permit_id,
            permit_sha256=permit_sha256,
            deployment_id=self.deployment_id,
            symbol=self.symbol,
            calibration_config_sha256=self.calibration_config_sha256,
            target_deployment_config_sha256=(
                self.target_deployment_config_sha256
            ),
            strategy_policy_sha256=self.strategy_policy_sha256,
            implementation_sha256=self.implementation_sha256,
            activated_at_exchange_ns=activated_at_ns,
            not_before_exchange_ns=not_before_ns,
            expires_at_exchange_ns=expires_at_ns,
            fixed_depths_bps=journal_depths,
            order_ttl_ns=limits["order_ttl_ns"],
            min_order_interval_ns=limits["min_order_interval_ns"],
            max_active_orders=limits["max_active_orders"],
            max_order_count=limits["max_order_count"],
            min_order_notional_microu=limits[
                "min_order_notional_microu"
            ],
            max_order_notional_microu=limits[
                "max_order_notional_microu"
            ],
            max_cumulative_submitted_notional_microu=limits[
                "max_cumulative_submitted_notional_microu"
            ],
            max_calibration_loss_microu=limits[
                "max_calibration_loss_microu"
            ],
            effective_deployment_loss_cap_microu=effective_loss_cap,
            deployment_start_equity_microu=start_equity,
            deployment_start_external_cash_flow_microu=start_cash_flow,
            peak_observed_loss_microu=peak_observed_loss,
            starting_reserved_order_count=starting_count,
            starting_cumulative_submitted_notional_microu=(
                starting_cumulative
            ),
        )
        self.activations[permit_id] = activation
        self.permit_identities[permit_id] = (
            permit_sha256,
            self.deployment_id,
        )
        self.open_permit_id = permit_id
        self.latest_signed_window_end_ns = expires_at_ns
        self.deployment_start_equity_microu = start_equity
        self.deployment_start_external_cash_flow_microu = start_cash_flow
        self.effective_loss_cap_microu = effective_loss_cap
        self.peak_observed_loss_microu = peak_observed_loss

    def _reserve(
        self,
        raw_payload: Mapping[str, Any],
        *,
        seq: int,
        line_number: int,
    ) -> None:
        payload = _exact_payload(
            raw_payload,
            _CALIBRATION_RESERVATION_KEYS,
            CALIBRATION_RESERVATION_KIND,
            line_number,
        )
        if payload.get("schema") != CALIBRATION_JOURNAL_SCHEMA:
            raise CalibrationArtifactError(
                f"invalid calibration reservation schema at line {line_number}"
            )
        permit_id = str(payload.get("permit_id", "") or "").strip()
        activation = self.activations.get(permit_id)
        if (
            activation is None
            or permit_id != self.open_permit_id
            or permit_id in self.expired_permits
        ):
            raise CalibrationArtifactError(
                "calibration reservation lacks a unique active permit at "
                f"line {line_number}"
            )
        identity = {
            "permit_sha256": activation.permit_sha256,
            "deployment_id": self.deployment_id,
            "calibration_config_sha256": self.calibration_config_sha256,
            "target_deployment_config_sha256": (
                self.target_deployment_config_sha256
            ),
            "strategy_policy_sha256": self.strategy_policy_sha256,
            "implementation_sha256": self.implementation_sha256,
        }
        if any(payload.get(field) != value for field, value in identity.items()):
            raise CalibrationArtifactError(
                f"calibration reservation identity mismatch at line {line_number}"
            )

        reservation_id = str(payload.get("reservation_id", "") or "")
        client_oid = str(payload.get("client_oid", "") or "")
        if (
            not _CLIENT_OID_RE.fullmatch(client_oid)
            or reservation_id != client_oid
            or client_oid in self.send_ids
        ):
            raise CalibrationArtifactError(
                f"invalid or duplicate reservation identity at line {line_number}"
            )
        expected_contract = {
            "symbol": self.symbol,
            "strategy_id": EXPECTED_STRATEGY,
            "order_type": "LIMIT",
            "time_in_force": "RPI",
            "post_only": True,
        }
        if any(
            payload.get(field) != value
            for field, value in expected_contract.items()
        ) or not isinstance(payload.get("reduce_only"), bool):
            raise CalibrationArtifactError(
                f"calibration reservation order contract mismatch at line "
                f"{line_number}"
            )

        reservation_seq = _journal_int(
            payload,
            "reservation_seq",
            line_number,
            minimum=1,
        )
        if reservation_seq != self.reserved_order_count + 1:
            raise CalibrationArtifactError(
                f"non-contiguous reservation sequence at line {line_number}"
            )
        reserved_at_ns = _journal_int(
            payload,
            "reserved_at_exchange_ns",
            line_number,
            minimum=1,
        )
        if not (
            activation.activated_at_exchange_ns
            <= reserved_at_ns
            < activation.expires_at_exchange_ns
        ):
            raise CalibrationArtifactError(
                f"reservation is outside its active permit at line {line_number}"
            )
        if (
            self.last_reserved_exchange_ns
            and reserved_at_ns - self.last_reserved_exchange_ns
            < activation.min_order_interval_ns
        ):
            raise CalibrationArtifactError(
                f"reservation violates minimum interval at line {line_number}"
            )

        depth = _journal_decimal(
            payload.get("declared_depth_bps"),
            "declared_depth_bps",
            line_number,
        )
        if _decimal_text(depth) not in activation.fixed_depths_bps:
            raise CalibrationArtifactError(
                f"reservation depth is not permitted at line {line_number}"
            )
        price = _journal_decimal(payload.get("price"), "price", line_number)
        quantity = _journal_decimal(
            payload.get("quantity"),
            "quantity",
            line_number,
        )
        reference_mid = _journal_decimal(
            payload.get("calibration_reference_mid"),
            "calibration_reference_mid",
            line_number,
        )
        side = payload.get("side")
        if (
            side not in {"BUY", "SELL"}
            or (side == "BUY" and price >= reference_mid)
            or (side == "SELL" and price <= reference_mid)
        ):
            raise CalibrationArtifactError(
                f"reservation side/reference is invalid at line {line_number}"
            )
        effective_depth = abs(
            math.log(float(price / reference_mid)) * 10_000.0
        )
        if not math.isfinite(effective_depth) or effective_depth + 1e-9 < float(
            depth
        ):
            raise CalibrationArtifactError(
                f"reservation price is inside its declared depth at line "
                f"{line_number}"
            )

        submitted_notional = _journal_int(
            payload,
            "submitted_notional_microu",
            line_number,
            minimum=1,
        )
        calculated_notional = int(
            (price * quantity * USDT_MICRO_SCALE).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        if submitted_notional != calculated_notional or not (
            activation.min_order_notional_microu
            <= submitted_notional
            <= activation.max_order_notional_microu
        ):
            raise CalibrationArtifactError(
                f"reservation notional is invalid at line {line_number}"
            )
        cumulative = _journal_int(
            payload,
            "cumulative_submitted_notional_microu",
            line_number,
            minimum=1,
        )
        if cumulative != self.cumulative_notional_microu + submitted_notional:
            raise CalibrationArtifactError(
                f"reservation cumulative notional mismatch at line {line_number}"
            )
        permit_order_count = (
            reservation_seq - activation.starting_reserved_order_count
        )
        permit_cumulative = (
            cumulative
            - activation.starting_cumulative_submitted_notional_microu
        )
        if (
            _journal_int(
                payload,
                "permit_reservation_seq",
                line_number,
                minimum=1,
            )
            != permit_order_count
            or _journal_int(
                payload,
                "permit_cumulative_submitted_notional_microu",
                line_number,
                minimum=1,
            )
            != permit_cumulative
            or permit_order_count > activation.max_order_count
            or permit_cumulative
            > activation.max_cumulative_submitted_notional_microu
        ):
            raise CalibrationArtifactError(
                f"reservation exceeds signed permit quota at line {line_number}"
            )
        effective_loss_cap = _journal_int(
            payload,
            "effective_deployment_loss_cap_microu",
            line_number,
            minimum=1,
        )
        loss_before_send = _journal_int(
            payload,
            "loss_before_send_microu",
            line_number,
        )
        if (
            effective_loss_cap != self.effective_loss_cap_microu
            or loss_before_send >= effective_loss_cap
        ):
            raise CalibrationArtifactError(
                f"reservation loss evidence is invalid at line {line_number}"
            )

        reservation = _CalibrationReservation(
            seq=seq,
            line_number=line_number,
            reservation_seq=reservation_seq,
            permit_reservation_seq=permit_order_count,
            client_oid=client_oid,
            permit_id=permit_id,
            permit_sha256=activation.permit_sha256,
            deployment_id=self.deployment_id,
            calibration_config_sha256=self.calibration_config_sha256,
            target_deployment_config_sha256=(
                self.target_deployment_config_sha256
            ),
            strategy_policy_sha256=self.strategy_policy_sha256,
            implementation_sha256=self.implementation_sha256,
            reserved_at_exchange_ns=reserved_at_ns,
            symbol=self.symbol,
            side=side,
            price=price,
            quantity=quantity,
            declared_depth_bps=depth,
            calibration_reference_mid=reference_mid,
            reduce_only=payload["reduce_only"],
            submitted_notional_microu=submitted_notional,
            cumulative_submitted_notional_microu=cumulative,
            permit_cumulative_submitted_notional_microu=(
                permit_cumulative
            ),
        )
        self.reservations[client_oid] = reservation
        self.send_ids.add(client_oid)
        self.reserved_order_count = reservation_seq
        self.cumulative_notional_microu = cumulative
        self.last_reserved_exchange_ns = reserved_at_ns

    def _expire(
        self,
        raw_payload: Mapping[str, Any],
        *,
        line_number: int,
    ) -> None:
        payload = _exact_payload(
            raw_payload,
            _CALIBRATION_EXPIRY_KEYS,
            CALIBRATION_EXPIRY_KIND,
            line_number,
        )
        if payload.get("schema") != CALIBRATION_JOURNAL_SCHEMA:
            raise CalibrationArtifactError(
                f"invalid calibration expiry schema at line {line_number}"
            )
        permit_id = str(payload.get("permit_id", "") or "").strip()
        permit_sha256 = _validated_sha256(
            payload.get("permit_sha256"),
            "expiry permit_sha256",
            line_number,
        )
        deployment_id = str(payload.get("deployment_id", "") or "").strip()
        if (
            not permit_id
            or permit_id in self.expired_permits
            or deployment_id != self.deployment_id
        ):
            raise CalibrationArtifactError(
                f"invalid calibration expiry identity at line {line_number}"
            )
        signed_permit = payload.get("signed_permit")
        if not isinstance(signed_permit, Mapping):
            raise CalibrationArtifactError(
                f"expiry signed_permit must be an object at line {line_number}"
            )
        not_before_ns = _utc_timestamp_to_ns(
            signed_permit.get("not_before_utc"),
            "expiry permit.not_before_utc",
            line_number,
        )
        expires_at_ns = _utc_timestamp_to_ns(
            signed_permit.get("expires_at_utc"),
            "expiry permit.expires_at_utc",
            line_number,
        )
        permit = self._validate_signed_permit(
            signed_permit,
            permit_sha256,
            validation_ns=not_before_ns,
            line_number=line_number,
        )
        permit_identity = {
            "permit_id": permit_id,
            "deployment_id": self.deployment_id,
            "symbol": self.symbol,
            "calibration_config_sha256": (
                self.calibration_config_sha256
            ),
            "target_deployment_config_sha256": (
                self.target_deployment_config_sha256
            ),
            "strategy_policy_sha256": self.strategy_policy_sha256,
            "implementation_sha256": self.implementation_sha256,
        }
        if any(
            payload.get(field) != expected
            or permit.get(field) != expected
            for field, expected in permit_identity.items()
        ):
            raise CalibrationArtifactError(
                f"calibration expiry identity mismatch at line {line_number}"
            )
        self._register_signed_window(
            permit_id=permit_id,
            permit_sha256=permit_sha256,
            not_before_ns=not_before_ns,
            expires_at_ns=expires_at_ns,
            line_number=line_number,
        )
        identity = (permit_sha256, deployment_id)
        if (
            permit_id in self.permit_identities
            and self.permit_identities[permit_id] != identity
        ):
            raise CalibrationArtifactError(
                f"expiry permit identity mismatch at line {line_number}"
            )
        self.permit_identities[permit_id] = identity
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise CalibrationArtifactError(
                f"expiry reason is required at line {line_number}"
            )
        if not isinstance(payload.get("budget_exhausted"), bool):
            raise CalibrationArtifactError(
                f"expiry budget flag is invalid at line {line_number}"
            )
        if (
            _journal_int(
                payload,
                "reserved_order_count",
                line_number,
            )
            != self.reserved_order_count
            or _journal_int(
                payload,
                "cumulative_submitted_notional_microu",
                line_number,
            )
            != self.cumulative_notional_microu
        ):
            raise CalibrationArtifactError(
                f"expiry quota counters mismatch at line {line_number}"
            )
        expired_at_ns = _journal_int(
            payload,
            "expired_at_exchange_ns",
            line_number,
            minimum=1,
        )
        if expired_at_ns < self.last_reserved_exchange_ns:
            raise CalibrationArtifactError(
                f"expiry time moved backwards at line {line_number}"
            )
        activation = self.activations.get(permit_id)
        if self.open_permit_id and self.open_permit_id != permit_id:
            raise CalibrationArtifactError(
                f"expiry does not match the open permit at line {line_number}"
            )
        start_equity = _journal_int(
            payload,
            "deployment_start_equity_microu",
            line_number,
        )
        start_cash_flow = _journal_int(
            payload,
            "deployment_start_external_cash_flow_microu",
            line_number,
            minimum=None,
        )
        peak_observed_loss = _journal_int(
            payload,
            "peak_observed_loss_microu",
            line_number,
        )
        effective_loss_cap = _journal_int(
            payload,
            "effective_deployment_loss_cap_microu",
            line_number,
            minimum=1,
        )
        signed_loss_cap = _usdt_to_microu(
            _journal_decimal(
                permit["policy"]["max_calibration_loss_usdt"],
                "expiry permit.policy.max_calibration_loss_usdt",
                line_number,
            ),
            upper_bound=True,
        )
        expected_loss_cap = min(
            self.effective_loss_cap_microu or signed_loss_cap,
            signed_loss_cap,
        )
        if effective_loss_cap != expected_loss_cap:
            raise CalibrationArtifactError(
                f"expiry widened the deployment loss cap at line {line_number}"
            )
        if self.deployment_start_equity_microu:
            if (
                start_equity != self.deployment_start_equity_microu
                or start_cash_flow
                != self.deployment_start_external_cash_flow_microu
            ):
                raise CalibrationArtifactError(
                    f"expiry loss baseline mismatch at line {line_number}"
                )
        elif start_equity != 0 or start_cash_flow != 0:
            raise CalibrationArtifactError(
                f"unused permit expiry invented a loss baseline at line "
                f"{line_number}"
            )
        if peak_observed_loss < self.peak_observed_loss_microu:
            raise CalibrationArtifactError(
                f"expiry peak loss regressed at line {line_number}"
            )
        if activation is not None:
            if self.open_permit_id != permit_id:
                raise CalibrationArtifactError(
                    f"expiry does not close the active permit at line "
                    f"{line_number}"
                )
            self.open_permit_id = ""
        self.effective_loss_cap_microu = effective_loss_cap
        self.peak_observed_loss_microu = peak_observed_loss
        self.expired_permits.add(permit_id)
        self.last_expired_exchange_ns = expired_at_ns

    def _bypass(
        self,
        raw_payload: Mapping[str, Any],
        *,
        line_number: int,
    ) -> None:
        payload = _exact_payload(
            raw_payload,
            _CALIBRATION_BYPASS_KEYS,
            CALIBRATION_BYPASS_KIND,
            line_number,
        )
        if payload.get("schema") != CALIBRATION_JOURNAL_SCHEMA:
            raise CalibrationArtifactError(
                f"invalid calibration bypass schema at line {line_number}"
            )
        bypass_id = str(payload.get("bypass_id", "") or "")
        client_oid = str(payload.get("client_oid", "") or "")
        if (
            not _CLIENT_OID_RE.fullmatch(client_oid)
            or bypass_id != client_oid
            or client_oid in self.send_ids
            or payload.get("reduce_only") is not True
            or payload.get("deployment_id") != self.deployment_id
            or payload.get("symbol") != self.symbol
            or payload.get("side") not in {"BUY", "SELL"}
        ):
            raise CalibrationArtifactError(
                f"invalid calibration emergency bypass at line {line_number}"
            )
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise CalibrationArtifactError(
                f"calibration bypass reason is required at line {line_number}"
            )
        permit_id = str(payload.get("permit_id", "") or "").strip()
        permit_sha256 = _validated_sha256(
            payload.get("permit_sha256"),
            "bypass permit_sha256",
            line_number,
        )
        identity = (permit_sha256, self.deployment_id)
        if (
            not permit_id
            or (
                permit_id in self.permit_identities
                and self.permit_identities[permit_id] != identity
            )
        ):
            raise CalibrationArtifactError(
                f"bypass permit identity mismatch at line {line_number}"
            )
        self.permit_identities[permit_id] = identity
        recorded_at_ns = _journal_int(
            payload,
            "recorded_at_exchange_ns",
            line_number,
            minimum=1,
        )
        if recorded_at_ns <= self.last_bypass_exchange_ns:
            raise CalibrationArtifactError(
                f"bypass time is not monotonic at line {line_number}"
            )
        price = _journal_decimal(payload.get("price"), "price", line_number)
        quantity = _journal_decimal(
            payload.get("quantity"),
            "quantity",
            line_number,
        )
        calculated_notional = int(
            (price * quantity * USDT_MICRO_SCALE).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        if (
            _journal_int(
                payload,
                "estimated_notional_microu",
                line_number,
                minimum=1,
            )
            != calculated_notional
        ):
            raise CalibrationArtifactError(
                f"bypass notional mismatch at line {line_number}"
            )
        self.send_ids.add(client_oid)
        self.last_bypass_exchange_ns = recorded_at_ns

    def finish(
        self,
        *,
        last_record_kind: str,
        last_record_payload: Mapping[str, Any],
    ) -> None:
        if not self.activations:
            raise CalibrationArtifactError(
                "OMS journal contains no independently verified calibration "
                "permit activation"
            )
        if not self.reservations:
            raise CalibrationArtifactError(
                "OMS journal contains no authorized calibration send "
                "reservation"
            )
        if self.open_permit_id:
            raise CalibrationArtifactError(
                "authorized calibration journal is not sealed: permit "
                f"{self.open_permit_id!r} has no durable expiry"
            )
        unexpired_permits = sorted(
            set(self.activations).difference(self.expired_permits)
        )
        if unexpired_permits:
            raise CalibrationArtifactError(
                "authorized calibration journal is not sealed: activated "
                "permits without durable expiry: "
                + ",".join(unexpired_permits)
            )
        if last_record_kind != "oms_stopped":
            raise CalibrationArtifactError(
                "authorized calibration journal is not sealed: the final "
                "durable record must be oms_stopped"
            )
        if last_record_payload.get("cancel_verified") is not True:
            raise CalibrationArtifactError(
                "authorized calibration journal is not sealed: final "
                "oms_stopped must prove cancel_verified=true"
            )


def _validated_sample(
    record: Mapping[str, Any],
    *,
    seq: int,
    line_number: int,
    record_at_utc: str,
    record_at_epoch: float,
    seen_client_oids: set[str],
) -> _ValidatedSample:
    payload = record["payload"]
    if not isinstance(payload, dict) or set(payload) != _SAMPLE_KEYS:
        raise CalibrationArtifactError(
            f"RPI exposure payload keys are invalid at line {line_number}"
        )

    expected_fields = {
        "schema": SAMPLE_SCHEMA,
        "strategy": EXPECTED_STRATEGY,
        "data_source": EXPECTED_DATA_SOURCE,
        "units_version": EXPECTED_UNITS_VERSION,
        "formula_version": EXPECTED_FORMULA_VERSION,
    }
    for field, expected in expected_fields.items():
        if payload.get(field) != expected:
            raise CalibrationArtifactError(
                f"RPI exposure {field} must exactly equal "
                f"{expected!r} at line {line_number}"
            )
    sample_symbol = _validated_symbol(payload.get("symbol"))

    client_oid = payload.get("client_oid")
    if not isinstance(client_oid, str) or not _CLIENT_OID_RE.fullmatch(
        client_oid
    ):
        raise CalibrationArtifactError(
            f"invalid client_oid at line {line_number}"
        )
    if client_oid in seen_client_oids:
        raise CalibrationArtifactError(
            f"duplicate client_oid {client_oid!r} at line {line_number}"
        )
    seen_client_oids.add(client_oid)
    exchange_oid = payload.get("exchange_oid")
    if (
        not isinstance(exchange_oid, str)
        or not exchange_oid
        or len(exchange_oid) > 128
    ):
        raise CalibrationArtifactError(
            f"invalid exchange_oid at line {line_number}"
        )

    terminal_status = payload.get("terminal_status")
    if terminal_status not in _TERMINAL_STATUSES:
        raise CalibrationArtifactError(
            f"invalid terminal_status at line {line_number}"
        )

    side = payload.get("side")
    if side not in {"BUY", "SELL"}:
        raise CalibrationArtifactError(
            f"invalid RPI exposure side at line {line_number}"
        )
    price = _finite_number(
        payload.get("price"),
        "price",
        line_number,
        positive=True,
    )
    quantity = _finite_number(
        payload.get("quantity"),
        "quantity",
        line_number,
        positive=True,
    )
    ack_time = _finite_number(
        payload.get("ack_time"),
        "ack_time",
        line_number,
    )
    ack_monotonic = _finite_number(
        payload.get("ack_monotonic"),
        "ack_monotonic",
        line_number,
    )
    terminal_time = _finite_number(
        payload.get("terminal_time"),
        "terminal_time",
        line_number,
    )
    terminal_monotonic = _finite_number(
        payload.get("terminal_monotonic"),
        "terminal_monotonic",
        line_number,
    )
    deployment_id = payload.get("deployment_id")
    if (
        not isinstance(deployment_id, str)
        or not deployment_id.strip()
        or len(deployment_id) > 128
    ):
        raise CalibrationArtifactError(
            f"deployment_id is invalid at line {line_number}"
        )
    strategy_policy_sha256 = _validated_sha256(
        payload.get("strategy_policy_sha256"),
        "strategy_policy_sha256",
        line_number,
    )
    implementation_sha256 = _validated_sha256(
        payload.get("implementation_sha256"),
        "implementation_sha256",
        line_number,
    )
    fill_count = _fill_count(payload.get("fill_count"), line_number)
    censored = payload.get("censored")
    if not isinstance(censored, bool):
        raise CalibrationArtifactError(
            f"censored must be a boolean at line {line_number}"
        )
    censor_reason = payload.get("censor_reason")
    if not isinstance(censor_reason, str):
        raise CalibrationArtifactError(
            f"censor_reason must be a string at line {line_number}"
        )
    if censored != bool(censor_reason.strip()):
        raise CalibrationArtifactError(
            f"censored and censor_reason disagree at line {line_number}"
        )

    raw_bins = payload.get("exposure_bins")
    if not isinstance(raw_bins, list):
        raise CalibrationArtifactError(
            f"exposure_bins must be a list at line {line_number}"
        )
    exposure_bins = []
    for raw_bin in raw_bins:
        if not isinstance(raw_bin, dict) or set(raw_bin) != {
            "depth_bps",
            "exposure_seconds",
            "fill_count",
            "sample_count",
        }:
            raise CalibrationArtifactError(
                f"exposure_bins entry is invalid at line {line_number}"
            )
        exposure_bins.append(
            RPIExposureBin(
                depth_bps=_finite_number(
                    raw_bin.get("depth_bps"),
                    "exposure_bins.depth_bps",
                    line_number,
                ),
                exposure_seconds=_finite_number(
                    raw_bin.get("exposure_seconds"),
                    "exposure_bins.exposure_seconds",
                    line_number,
                    positive=True,
                ),
                fill_count=_fill_count(
                    raw_bin.get("fill_count"),
                    line_number,
                ),
                sample_count=_positive_int(
                    raw_bin.get("sample_count"),
                    "exposure_bins.sample_count",
                    line_number,
                ),
            )
        )

    if censored:
        if exposure_bins:
            raise CalibrationArtifactError(
                f"censored sample must not publish exposure_bins at line "
                f"{line_number}"
            )
    else:
        if not exposure_bins:
            raise CalibrationArtifactError(
                f"uncensored sample requires exposure_bins at line "
                f"{line_number}"
            )
        if min(
            ack_time,
            ack_monotonic,
            terminal_time,
            terminal_monotonic,
        ) <= 0.0:
            raise CalibrationArtifactError(
                f"uncensored sample times must be positive at line "
                f"{line_number}"
            )
        if terminal_monotonic <= ack_monotonic:
            raise CalibrationArtifactError(
                f"terminal_monotonic must follow ack_monotonic at line "
                f"{line_number}"
            )
        if terminal_time < ack_time:
            raise CalibrationArtifactError(
                f"terminal_time must not precede ack_time at line "
                f"{line_number}"
            )
        append_lag = record_at_epoch - terminal_time
        if (
            append_lag < -0.01
            or append_lag > MAX_SAMPLE_APPEND_LAG_SECONDS
        ):
            raise CalibrationArtifactError(
                "RPI sample journal timestamp is not contemporaneous with "
                f"terminal_time at line {line_number}"
            )
        if sum(item.fill_count for item in exposure_bins) != fill_count:
            raise CalibrationArtifactError(
                f"exposure_bins fill_count mismatch at line {line_number}"
            )
        try:
            exposure_duration = math.fsum(
                item.exposure_seconds for item in exposure_bins
            )
        except (OverflowError, ValueError) as exc:
            raise CalibrationArtifactError(
                f"exposure duration is invalid at line {line_number}"
            ) from exc
        if not _numbers_match(
            exposure_duration,
            terminal_monotonic - ack_monotonic,
        ):
            raise CalibrationArtifactError(
                "exposure_bins duration does not match the acknowledged "
                f"lifetime at line {line_number}"
            )

    return _ValidatedSample(
        seq=seq,
        line_number=line_number,
        record_at_utc=record_at_utc,
        symbol=sample_symbol,
        client_oid=client_oid,
        exchange_oid=exchange_oid,
        terminal_status=terminal_status,
        side=side,
        price=price,
        quantity=quantity,
        ack_time=ack_time,
        ack_monotonic=ack_monotonic,
        terminal_time=terminal_time,
        terminal_monotonic=terminal_monotonic,
        deployment_id=deployment_id.strip(),
        strategy_policy_sha256=strategy_policy_sha256,
        implementation_sha256=implementation_sha256,
        reported_fill_count=fill_count,
        censored=censored,
        censor_reason=censor_reason.strip(),
        exposure_bins=tuple(exposure_bins),
    )


def _numbers_match(left: float, right: float) -> bool:
    tolerance = max(1e-9, abs(left) * 1e-9, abs(right) * 1e-9)
    return abs(left - right) <= tolerance


def _nonnegative_int(value: Any, field: str, line_number: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CalibrationArtifactError(
            f"{field} must be a non-negative integer at line {line_number}"
        )
    return value


def _optional_finite_number(
    value: Any,
    field: str,
    line_number: int,
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CalibrationArtifactError(
            f"{field} must be a finite JSON number or null at line "
            f"{line_number}"
        )
    parsed = float(value)
    if not math.isfinite(parsed):
        raise CalibrationArtifactError(
            f"{field} must be a finite JSON number or null at line "
            f"{line_number}"
        )
    return parsed


def _validated_order_snapshot(
    record: _EvidenceRecord,
    *,
    client_oid: str,
    symbol: str,
) -> _OrderSnapshotEvidence:
    payload = record.payload
    payload_keys = set(payload)
    if (
        not _ORDER_SNAPSHOT_REQUIRED_KEYS.issubset(payload_keys)
        or payload_keys - _ORDER_SNAPSHOT_REQUIRED_KEYS - {"extra"}
    ):
        raise CalibrationArtifactError(
            "order_snapshot payload keys are invalid for "
            f"{client_oid!r} at line {record.line_number}"
        )
    if payload.get("client_oid") != client_oid:
        raise CalibrationArtifactError(
            f"order_snapshot client_oid mismatch at line {record.line_number}"
        )

    intent = payload.get("intent")
    intent_keys = (
        frozenset(intent) if isinstance(intent, dict) else frozenset()
    )
    if not isinstance(intent, dict) or intent_keys not in {
        _LEGACY_INTENT_KEYS,
        _LEGACY_INTENT_KEYS | _CALIBRATION_INTENT_KEYS,
    }:
        raise CalibrationArtifactError(
            f"order_snapshot intent is invalid for {client_oid!r} at line "
            f"{record.line_number}"
        )
    expected_identity = {
        "strategy_id": EXPECTED_STRATEGY,
        "symbol": symbol,
        "order_type": "LIMIT",
        "time_in_force": "RPI",
        "is_post_only": True,
        "policy": "PASSIVE",
    }
    for field, expected in expected_identity.items():
        if intent.get(field) != expected:
            raise CalibrationArtifactError(
                f"order_snapshot {field} must exactly equal {expected!r} "
                f"for {client_oid!r} at line {record.line_number}"
            )

    side = intent.get("side")
    if side not in {"BUY", "SELL"}:
        raise CalibrationArtifactError(
            f"order_snapshot side is invalid for {client_oid!r} at line "
            f"{record.line_number}"
        )
    intent_price = _finite_number(
        intent.get("price"),
        "order_snapshot intent.price",
        record.line_number,
        positive=True,
    )
    intent_volume = _finite_number(
        intent.get("volume"),
        "order_snapshot intent.volume",
        record.line_number,
        positive=True,
    )
    reduce_only = intent.get("reduce_only")
    if not isinstance(reduce_only, bool):
        raise CalibrationArtifactError(
            f"order_snapshot reduce_only must be boolean at line "
            f"{record.line_number}"
        )
    tag = intent.get("tag")
    if not isinstance(tag, str):
        raise CalibrationArtifactError(
            f"order_snapshot tag must be a string at line "
            f"{record.line_number}"
        )
    calibration_permit_id = intent.get("calibration_permit_id", "")
    if (
        not isinstance(calibration_permit_id, str)
        or len(calibration_permit_id) > 128
    ):
        raise CalibrationArtifactError(
            "order_snapshot calibration_permit_id must be a string no longer "
            f"than 128 characters at line {record.line_number}"
        )
    calibration_depth_bps = _optional_finite_number(
        intent.get("calibration_depth_bps"),
        "order_snapshot intent.calibration_depth_bps",
        record.line_number,
    )
    calibration_reference_mid = _optional_finite_number(
        intent.get("calibration_reference_mid"),
        "order_snapshot intent.calibration_reference_mid",
        record.line_number,
    )
    if (
        calibration_depth_bps is not None
        and calibration_depth_bps <= 0.0
    ):
        raise CalibrationArtifactError(
            "order_snapshot intent.calibration_depth_bps must be positive at "
            f"line {record.line_number}"
        )
    if (
        calibration_reference_mid is not None
        and calibration_reference_mid <= 0.0
    ):
        raise CalibrationArtifactError(
            "order_snapshot intent.calibration_reference_mid must be positive "
            f"at line {record.line_number}"
        )

    status = payload.get("status")
    if not isinstance(status, str) or not status:
        raise CalibrationArtifactError(
            f"order_snapshot status is invalid for {client_oid!r} at line "
            f"{record.line_number}"
        )
    source = payload.get("source")
    if not isinstance(source, str) or not source:
        raise CalibrationArtifactError(
            f"order_snapshot source is invalid for {client_oid!r} at line "
            f"{record.line_number}"
        )
    exchange_oid = payload.get("exchange_oid")
    if not isinstance(exchange_oid, str):
        raise CalibrationArtifactError(
            f"order_snapshot exchange_oid must be a string at line "
            f"{record.line_number}"
        )
    filled_volume = _finite_number(
        payload.get("filled_volume"),
        "order_snapshot filled_volume",
        record.line_number,
    )
    avg_price = _finite_number(
        payload.get("avg_price"),
        "order_snapshot avg_price",
        record.line_number,
    )
    cumulative_cost = _finite_number(
        payload.get("cumulative_cost"),
        "order_snapshot cumulative_cost",
        record.line_number,
    )
    created_at = _finite_number(
        payload.get("created_at"),
        "order_snapshot created_at",
        record.line_number,
    )
    updated_at = _finite_number(
        payload.get("updated_at"),
        "order_snapshot updated_at",
        record.line_number,
    )
    created_monotonic = _finite_number(
        payload.get("created_monotonic"),
        "order_snapshot created_monotonic",
        record.line_number,
    )
    updated_monotonic = _finite_number(
        payload.get("updated_monotonic"),
        "order_snapshot updated_monotonic",
        record.line_number,
    )
    recovered_from_journal = payload.get("recovered_from_journal")
    if not isinstance(recovered_from_journal, bool):
        raise CalibrationArtifactError(
            "order_snapshot recovered_from_journal must be boolean at line "
            f"{record.line_number}"
        )
    if created_monotonic <= 0.0 or updated_monotonic < created_monotonic:
        raise CalibrationArtifactError(
            f"order_snapshot monotonic times are invalid at line "
            f"{record.line_number}"
        )
    _nonnegative_int(
        payload.get("last_update_seq"),
        "order_snapshot last_update_seq",
        record.line_number,
    )
    if not isinstance(payload.get("error_msg"), str):
        raise CalibrationArtifactError(
            f"order_snapshot error_msg must be a string at line "
            f"{record.line_number}"
        )
    last_exchange_status = payload.get("last_exchange_status")
    if not isinstance(last_exchange_status, str):
        raise CalibrationArtifactError(
            f"order_snapshot last_exchange_status must be a string at line "
            f"{record.line_number}"
        )
    last_exchange_update_time = _finite_number(
        payload.get("last_exchange_update_time"),
        "order_snapshot last_exchange_update_time",
        record.line_number,
    )
    if filled_volume > intent_volume and not _numbers_match(
        filled_volume,
        intent_volume,
    ):
        raise CalibrationArtifactError(
            f"order_snapshot filled_volume exceeds intent volume for "
            f"{client_oid!r} at line {record.line_number}"
        )

    if source == "exchange_update":
        extra = payload.get("extra")
        if not isinstance(extra, dict):
            raise CalibrationArtifactError(
                f"exchange order_snapshot lacks extra exchange evidence for "
                f"{client_oid!r} at line {record.line_number}"
            )
        exchange_status = extra.get("exchange_status")
        if not isinstance(exchange_status, str) or not exchange_status:
            raise CalibrationArtifactError(
                f"exchange order_snapshot status evidence is invalid for "
                f"{client_oid!r} at line {record.line_number}"
            )
        reported_cumulative = _finite_number(
            extra.get("cum_filled_qty"),
            "order_snapshot extra.cum_filled_qty",
            record.line_number,
        )
        _nonnegative_int(
            extra.get("seq"),
            "order_snapshot extra.seq",
            record.line_number,
        )
        if last_exchange_status != exchange_status:
            raise CalibrationArtifactError(
                f"order_snapshot exchange status mismatch for "
                f"{client_oid!r} at line {record.line_number}"
            )
        if not _numbers_match(reported_cumulative, filled_volume):
            raise CalibrationArtifactError(
                f"order_snapshot cumulative fill mismatch for "
                f"{client_oid!r} at line {record.line_number}"
            )
        if not exchange_oid or last_exchange_update_time <= 0.0:
            raise CalibrationArtifactError(
                f"exchange order_snapshot lacks true exchange identity/time "
                f"for {client_oid!r} at line {record.line_number}"
            )

    return _OrderSnapshotEvidence(
        seq=record.seq,
        line_number=record.line_number,
        status=status,
        source=source,
        exchange_oid=exchange_oid,
        price=intent_price,
        filled_volume=filled_volume,
        avg_price=avg_price,
        cumulative_cost=cumulative_cost,
        intent_volume=intent_volume,
        side=side,
        reduce_only=reduce_only,
        tag=tag,
        created_at=created_at,
        updated_at=updated_at,
        created_monotonic=created_monotonic,
        updated_monotonic=updated_monotonic,
        recovered_from_journal=recovered_from_journal,
        last_exchange_status=last_exchange_status,
        last_exchange_update_time=last_exchange_update_time,
        calibration_permit_id=calibration_permit_id,
        calibration_depth_bps=calibration_depth_bps,
        calibration_reference_mid=calibration_reference_mid,
    )


def _validated_execution(
    record: _EvidenceRecord,
    *,
    client_oid: str,
    symbol: str,
) -> _ExecutionEvidence:
    payload = record.payload
    if set(payload) != _EXECUTION_KEYS:
        raise CalibrationArtifactError(
            f"execution_record payload keys are invalid for {client_oid!r} "
            f"at line {record.line_number}"
        )
    expected_identity = {
        "client_oid": client_oid,
        "strategy_id": EXPECTED_STRATEGY,
        "symbol": symbol,
        "venue": "BINANCE",
    }
    for field, expected in expected_identity.items():
        if payload.get(field) != expected:
            raise CalibrationArtifactError(
                f"execution_record {field} must exactly equal {expected!r} "
                f"for {client_oid!r} at line {record.line_number}"
            )

    exchange_oid = payload.get("exchange_oid")
    if not isinstance(exchange_oid, str) or not exchange_oid:
        raise CalibrationArtifactError(
            f"execution_record exchange_oid is invalid for {client_oid!r} "
            f"at line {record.line_number}"
        )
    side = payload.get("side")
    if side not in {"BUY", "SELL"}:
        raise CalibrationArtifactError(
            f"execution_record side is invalid for {client_oid!r} at line "
            f"{record.line_number}"
        )
    fill_qty = _finite_number(
        payload.get("fill_qty"),
        "execution_record fill_qty",
        record.line_number,
        positive=True,
    )
    fill_price = _finite_number(
        payload.get("fill_price"),
        "execution_record fill_price",
        record.line_number,
        positive=True,
    )
    cumulative_qty = _finite_number(
        payload.get("cum_filled_qty"),
        "execution_record cum_filled_qty",
        record.line_number,
        positive=True,
    )
    exchange_time = _finite_number(
        payload.get("exchange_time"),
        "execution_record exchange_time",
        record.line_number,
        positive=True,
    )
    exchange_status = payload.get("exchange_status")
    if exchange_status not in _EXECUTION_STATUSES:
        raise CalibrationArtifactError(
            f"execution_record exchange_status is invalid for "
            f"{client_oid!r} at line {record.line_number}"
        )
    trade_id = _nonnegative_int(
        payload.get("trade_id"),
        "execution_record trade_id",
        record.line_number,
    )
    execution_id = payload.get("execution_id")
    expected_execution_id = f"BINANCE:{symbol}:{trade_id}"
    if execution_id != expected_execution_id:
        raise CalibrationArtifactError(
            f"execution_record execution_id must exactly equal "
            f"{expected_execution_id!r} at line {record.line_number}"
        )
    if payload.get("is_maker") is not True:
        raise CalibrationArtifactError(
            f"RPI execution_record must be maker=true for {client_oid!r} "
            f"at line {record.line_number}"
        )
    for field in ("commission", "booked_fee", "realized_pnl"):
        _optional_finite_number(
            payload.get(field),
            f"execution_record {field}",
            record.line_number,
        )
    if not isinstance(payload.get("commission_asset"), str):
        raise CalibrationArtifactError(
            f"execution_record commission_asset must be a string at line "
            f"{record.line_number}"
        )
    if not isinstance(payload.get("pre_status"), str) or not payload.get(
        "pre_status"
    ):
        raise CalibrationArtifactError(
            f"execution_record pre_status is invalid at line "
            f"{record.line_number}"
        )

    return _ExecutionEvidence(
        seq=record.seq,
        line_number=record.line_number,
        execution_id=execution_id,
        exchange_oid=exchange_oid,
        side=side,
        fill_qty=fill_qty,
        fill_price=fill_price,
        cumulative_qty=cumulative_qty,
        exchange_status=exchange_status,
        exchange_time=exchange_time,
        trade_id=trade_id,
    )


def _is_true_exchange_snapshot(
    snapshot: _OrderSnapshotEvidence,
    exchange_status: str,
) -> bool:
    return (
        snapshot.source == "exchange_update"
        and snapshot.last_exchange_status == exchange_status
        and bool(snapshot.exchange_oid)
        and snapshot.last_exchange_update_time > 0.0
    )


def _is_rest_ack_snapshot(snapshot: _OrderSnapshotEvidence) -> bool:
    return (
        snapshot.status == "PENDING_ACK"
        and snapshot.source == "rest_ack"
        and bool(snapshot.exchange_oid)
        and snapshot.updated_monotonic > 0.0
        and not snapshot.recovered_from_journal
    )


def _correlate_sample_evidence(
    sample: _ValidatedSample,
    *,
    symbol: str,
    order_records: Sequence[_EvidenceRecord],
    execution_records: Sequence[_EvidenceRecord],
    reservation: _CalibrationReservation | None = None,
) -> tuple[RPIExposureBin, ...]:
    client_oid = sample.client_oid
    if reservation is not None:
        if (
            reservation.client_oid != client_oid
            or reservation.symbol != symbol
            or reservation.seq >= sample.seq
        ):
            raise CalibrationArtifactError(
                f"RPI sample lacks a unique prior send reservation for "
                f"{client_oid!r} at line {sample.line_number}"
            )
        if (
            sample.deployment_id != reservation.deployment_id
            or sample.strategy_policy_sha256
            != reservation.strategy_policy_sha256
            or sample.implementation_sha256
            != reservation.implementation_sha256
        ):
            raise CalibrationArtifactError(
                f"RPI sample reservation identity mismatch for "
                f"{client_oid!r} at line {sample.line_number}"
            )
    if not order_records:
        raise CalibrationArtifactError(
            f"no order lifecycle evidence for {client_oid!r} before RPI "
            f"sample at line {sample.line_number}"
        )
    if any(record.seq > sample.seq for record in order_records):
        raise CalibrationArtifactError(
            f"order lifecycle evidence must precede RPI sample for "
            f"{client_oid!r} at line {sample.line_number}"
        )
    if any(record.seq > sample.seq for record in execution_records):
        raise CalibrationArtifactError(
            f"execution evidence must precede RPI sample for {client_oid!r} "
            f"at line {sample.line_number}"
        )

    snapshots = tuple(
        _validated_order_snapshot(
            record,
            client_oid=client_oid,
            symbol=symbol,
        )
        for record in sorted(order_records, key=lambda item: item.seq)
    )
    if reservation is not None:
        if reservation.seq >= snapshots[0].seq:
            raise CalibrationArtifactError(
                f"send reservation must precede the order lifecycle for "
                f"{client_oid!r}"
            )
        for snapshot in snapshots:
            if (
                snapshot.calibration_permit_id != reservation.permit_id
                or snapshot.calibration_depth_bps is None
                or snapshot.calibration_reference_mid is None
                or snapshot.side != reservation.side
                or snapshot.reduce_only != reservation.reduce_only
                or snapshot.tag != "rpi_calibration_canary"
                or not _numbers_match(
                    snapshot.price,
                    float(reservation.price),
                )
                or not _numbers_match(
                    snapshot.intent_volume,
                    float(reservation.quantity),
                )
                or not _numbers_match(
                    snapshot.calibration_depth_bps,
                    float(reservation.declared_depth_bps),
                )
                or not _numbers_match(
                    snapshot.calibration_reference_mid,
                    float(reservation.calibration_reference_mid),
                )
            ):
                raise CalibrationArtifactError(
                    f"order lifecycle differs from its send reservation for "
                    f"{client_oid!r} at line {snapshot.line_number}"
                )
    rest_acknowledgements = tuple(
        snapshot for snapshot in snapshots if _is_rest_ack_snapshot(snapshot)
    )
    if len(rest_acknowledgements) != 1:
        raise CalibrationArtifactError(
            f"expected exactly one true REST PENDING_ACK snapshot for "
            f"{client_oid!r} before RPI sample at line {sample.line_number}"
        )
    rest_ack = rest_acknowledgements[0]

    expected_terminal_exchange_status = (
        _EXCHANGE_STATUS_BY_TERMINAL_STATUS.get(sample.terminal_status)
    )
    if expected_terminal_exchange_status is None:
        raise CalibrationArtifactError(
            f"terminal status {sample.terminal_status!r} has no exchange "
            f"terminal evidence for {client_oid!r}"
        )
    terminal_candidates = tuple(
        snapshot
        for snapshot in snapshots
        if snapshot.status == sample.terminal_status
        and _is_true_exchange_snapshot(
            snapshot,
            expected_terminal_exchange_status,
        )
    )
    if not terminal_candidates:
        raise CalibrationArtifactError(
            f"missing matching terminal order_snapshot for {client_oid!r}: "
            f"expected {sample.terminal_status}"
        )
    terminal = terminal_candidates[-1]
    if snapshots[-1].seq != terminal.seq:
        raise CalibrationArtifactError(
            f"terminal order_snapshot is not the last lifecycle state before "
            f"the RPI sample for {client_oid!r}"
        )
    if rest_ack.seq > terminal.seq:
        raise CalibrationArtifactError(
            f"exchange acknowledgement occurs after terminal snapshot for "
            f"{client_oid!r}"
        )

    exchange_oids = {
        snapshot.exchange_oid
        for snapshot in snapshots
        if snapshot.exchange_oid
    }
    if len(exchange_oids) != 1:
        raise CalibrationArtifactError(
            f"order lifecycle exchange_oid is inconsistent for {client_oid!r}"
        )
    sides = {snapshot.side for snapshot in snapshots}
    prices = {snapshot.price for snapshot in snapshots}
    intent_volumes = {snapshot.intent_volume for snapshot in snapshots}
    calibration_permit_ids = {
        snapshot.calibration_permit_id for snapshot in snapshots
    }
    calibration_depths = {
        snapshot.calibration_depth_bps for snapshot in snapshots
    }
    calibration_reference_mids = {
        snapshot.calibration_reference_mid for snapshot in snapshots
    }
    if (
        len(sides) != 1
        or len(prices) != 1
        or len(intent_volumes) != 1
        or len(calibration_permit_ids) != 1
        or len(calibration_depths) != 1
        or len(calibration_reference_mids) != 1
    ):
        raise CalibrationArtifactError(
            f"order lifecycle intent changed for {client_oid!r}"
        )
    previous_filled_volume = 0.0
    for snapshot in snapshots:
        if snapshot.filled_volume + 1e-9 < previous_filled_volume:
            raise CalibrationArtifactError(
                f"order_snapshot filled volume moved backwards for "
                f"{client_oid!r} at line {snapshot.line_number}"
            )
        previous_filled_volume = max(
            previous_filled_volume,
            snapshot.filled_volume,
        )

    executions = tuple(
        _validated_execution(
            record,
            client_oid=client_oid,
            symbol=symbol,
        )
        for record in sorted(execution_records, key=lambda item: item.seq)
    )
    if any(execution.seq <= snapshots[0].seq for execution in executions):
        raise CalibrationArtifactError(
            f"execution evidence precedes order intent lifecycle for "
            f"{client_oid!r}"
        )
    if any(execution.seq > terminal.seq for execution in executions):
        raise CalibrationArtifactError(
            f"execution evidence occurs after terminal snapshot for "
            f"{client_oid!r}"
        )
    if len({execution.execution_id for execution in executions}) != len(
        executions
    ):
        raise CalibrationArtifactError(
            f"duplicate execution_id evidence for {client_oid!r}"
        )

    expected_exchange_oid = next(iter(exchange_oids))
    expected_side = next(iter(sides))
    if sample.exchange_oid != expected_exchange_oid:
        raise CalibrationArtifactError(
            f"RPI sample exchange_oid mismatch for {client_oid!r}"
        )
    if sample.side != expected_side:
        raise CalibrationArtifactError(
            f"RPI sample side mismatch for {client_oid!r}"
        )
    if not _numbers_match(sample.price, rest_ack.price):
        raise CalibrationArtifactError(
            f"RPI sample price mismatch for {client_oid!r}"
        )
    if not _numbers_match(sample.quantity, rest_ack.intent_volume):
        raise CalibrationArtifactError(
            f"RPI sample quantity mismatch for {client_oid!r}"
        )
    if not sample.censored:
        if any(snapshot.recovered_from_journal for snapshot in snapshots):
            raise CalibrationArtifactError(
                f"uncensored RPI sample crosses a process recovery for "
                f"{client_oid!r}"
            )
        if not _numbers_match(
            sample.ack_monotonic,
            rest_ack.updated_monotonic,
        ):
            raise CalibrationArtifactError(
                f"RPI sample ACK monotonic mismatch for {client_oid!r}"
            )
        if not _numbers_match(sample.ack_time, rest_ack.updated_at):
            raise CalibrationArtifactError(
                f"RPI sample ACK time mismatch for {client_oid!r}"
            )
        if not _numbers_match(
            sample.terminal_monotonic,
            terminal.updated_monotonic,
        ):
            raise CalibrationArtifactError(
                f"RPI sample terminal monotonic mismatch for {client_oid!r}"
            )
        if not _numbers_match(sample.terminal_time, terminal.updated_at):
            raise CalibrationArtifactError(
                f"RPI sample terminal time mismatch for {client_oid!r}"
            )
    running_qty = 0.0
    running_cost = 0.0
    previous_exchange_time = 0.0
    for execution in executions:
        if execution.exchange_oid != expected_exchange_oid:
            raise CalibrationArtifactError(
                f"execution_record exchange_oid mismatch for {client_oid!r} "
                f"at line {execution.line_number}"
            )
        if execution.side != expected_side:
            raise CalibrationArtifactError(
                f"execution_record side mismatch for {client_oid!r} at line "
                f"{execution.line_number}"
            )
        if execution.exchange_time + 1e-9 < previous_exchange_time:
            raise CalibrationArtifactError(
                f"execution exchange time moved backwards for {client_oid!r} "
                f"at line {execution.line_number}"
            )
        running_qty += execution.fill_qty
        running_cost += execution.fill_qty * execution.fill_price
        if not _numbers_match(running_qty, execution.cumulative_qty):
            raise CalibrationArtifactError(
                f"execution cumulative quantity mismatch for {client_oid!r} "
                f"at line {execution.line_number}"
            )
        previous_exchange_time = execution.exchange_time

    if (
        not sample.censored
        and sample.reported_fill_count != len(executions)
    ):
        raise CalibrationArtifactError(
            f"RPI sample fill_count mismatch for {client_oid!r}: "
            f"sample={sample.reported_fill_count}, "
            f"execution_records={len(executions)}"
        )
    if not _numbers_match(running_qty, terminal.filled_volume):
        raise CalibrationArtifactError(
            f"terminal filled_volume does not match execution records for "
            f"{client_oid!r}"
        )
    if not _numbers_match(running_cost, terminal.cumulative_cost):
        raise CalibrationArtifactError(
            f"terminal cumulative_cost does not match execution records for "
            f"{client_oid!r}"
        )
    expected_avg_price = running_cost / running_qty if running_qty else 0.0
    if not _numbers_match(expected_avg_price, terminal.avg_price):
        raise CalibrationArtifactError(
            f"terminal avg_price does not match execution records for "
            f"{client_oid!r}"
        )
    if sample.terminal_status == "FILLED":
        intent_volume = next(iter(intent_volumes))
        if not executions or not _numbers_match(running_qty, intent_volume):
            raise CalibrationArtifactError(
                f"FILLED terminal evidence is incomplete for {client_oid!r}"
            )
        if executions[-1].exchange_status != "FILLED":
            raise CalibrationArtifactError(
                f"FILLED terminal lacks a final FILLED execution for "
                f"{client_oid!r}"
            )

    for snapshot in snapshots:
        executed_before_snapshot = sum(
            execution.fill_qty
            for execution in executions
            if execution.seq < snapshot.seq
        )
        if not _numbers_match(
            executed_before_snapshot,
            snapshot.filled_volume,
        ):
            raise CalibrationArtifactError(
                f"order_snapshot fill state does not match prior executions "
                f"for {client_oid!r} at line {snapshot.line_number}"
            )

    return () if sample.censored else sample.exposure_bins


def _validated_journal_timestamp(
    value: Any,
    line_number: int,
) -> tuple[datetime, str]:
    if not isinstance(value, str) or not value.strip():
        raise CalibrationArtifactError(
            f"ts must be a non-empty UTC timestamp at line {line_number}"
        )
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise CalibrationArtifactError(
            f"ts must be an ISO-8601 UTC timestamp at line {line_number}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CalibrationArtifactError(
            f"ts must include a UTC offset at line {line_number}"
        )
    parsed_utc = parsed.astimezone(timezone.utc)
    canonical = (
        parsed_utc.isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    return parsed_utc, canonical


def _resolved_config_path(base_dir: Path, value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise CalibrationArtifactError(f"{field} must be a non-empty path")
    path = Path(value.strip())
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


@contextmanager
def _authorized_journal_fence(
    journal_path: Path,
    *,
    calibration_config: Mapping[str, Any] | None,
    calibration_config_path: str | Path | None,
) -> Iterator[None]:
    """Prove the live OMS writer is absent while authorized evidence is read."""
    if calibration_config is None:
        yield
        return
    if calibration_config_path is None:
        raise CalibrationArtifactError(
            "authorized journal validation requires the exact calibration "
            "config path before acquiring its writer fence"
        )
    oms_config = calibration_config.get("oms")
    if not isinstance(oms_config, Mapping):
        raise CalibrationArtifactError(
            "authorized calibration config oms section is required"
        )
    base_dir = Path(calibration_config_path).resolve().parent
    configured_journal = _resolved_config_path(
        base_dir,
        oms_config.get("journal_path"),
        "calibration config oms.journal_path",
    )
    if configured_journal != journal_path:
        raise CalibrationArtifactError(
            "authorized journal path does not match calibration config "
            "oms.journal_path"
        )
    fence_config = oms_config.get("single_writer_fence")
    if (
        not isinstance(fence_config, Mapping)
        or fence_config.get("enabled") is not True
    ):
        raise CalibrationArtifactError(
            "authorized calibration journal requires an enabled OMS "
            "single-writer fence"
        )
    fence_path = _resolved_config_path(
        base_dir,
        fence_config.get("path"),
        "calibration config oms.single_writer_fence.path",
    )
    expected_fence_path = Path(f"{journal_path}.lock").resolve()
    if fence_path != expected_fence_path:
        raise CalibrationArtifactError(
            "authorized calibration journal fence must resolve to "
            "journal_path + '.lock'"
        )
    if not fence_path.is_file():
        raise CalibrationArtifactError(
            "authorized calibration journal writer fence file is missing; "
            "the journal is not eligible for artifact construction"
        )

    from infrastructure.single_writer_fence import (
        SingleWriterFence,
        SingleWriterFenceError,
    )

    fence = SingleWriterFence(
        str(fence_path),
        owner_metadata={
            "component": "rpi_calibration_artifact_validator",
            "journal_path": str(journal_path),
        },
    )
    try:
        fence.acquire()
    except SingleWriterFenceError as exc:
        raise CalibrationArtifactError(
            "authorized calibration journal is still owned by an OMS "
            "writer; stop the calibration runtime cleanly before building "
            "or validating its artifact"
        ) from exc
    try:
        yield
    finally:
        try:
            fence.release()
        except OSError as exc:
            raise CalibrationArtifactError(
                "could not release calibration journal validation fence"
            ) from exc


def _validate_rpi_calibration_journal_unlocked(
    journal_path: str | Path,
    *,
    symbol: str,
    calibration_config: Mapping[str, Any] | None = None,
    target_deployment_config: Mapping[str, Any] | None = None,
    calibration_config_path: str | Path | None = None,
    target_deployment_config_path: str | Path | None = None,
) -> RPIJournalValidationSummary:
    """Validate the full journal file and return source-evidence facts.

    Passing both deployment configs enables the authorization replay required
    by the live artifact and source-evidence paths. Calls without configs are
    retained only for parsing older, non-promotable research journals.
    """
    source = Path(journal_path).resolve()
    target_symbol = _validated_symbol(symbol)
    has_calibration_config = calibration_config is not None
    has_target_config = target_deployment_config is not None
    if has_calibration_config != has_target_config:
        raise CalibrationArtifactError(
            "calibration and target deployment configs must be supplied "
            "together"
        )
    if has_calibration_config and (
        calibration_config_path is None
        or target_deployment_config_path is None
    ):
        raise CalibrationArtifactError(
            "authorized journal validation requires both exact config paths"
        )
    calibration_replay = (
        _CalibrationJournalReplay(
            symbol=target_symbol,
            calibration_config=calibration_config,
            target_deployment_config=target_deployment_config,
            calibration_config_path=calibration_config_path,
            target_deployment_config_path=target_deployment_config_path,
        )
        if calibration_config is not None
        and target_deployment_config is not None
        else None
    )
    samples: list[_ValidatedSample] = []
    order_records_by_client_oid: dict[str, list[_EvidenceRecord]] = {}
    execution_records_by_client_oid: dict[str, list[_EvidenceRecord]] = {}
    seen_client_oids: set[str] = set()
    expected_seq = 1
    first_seq = 0
    last_seq = 0
    previous_hash = ""
    record_count = 0
    first_record_at_utc = ""
    last_record_at_utc = ""
    last_record_kind = ""
    last_record_payload: Mapping[str, Any] = {}
    previous_record_at: datetime | None = None
    journal_digest = hashlib.sha256()

    try:
        with source.open("rb") as handle:
            for line_number, raw_bytes in enumerate(handle, start=1):
                journal_digest.update(raw_bytes)
                try:
                    raw = raw_bytes.decode("utf-8-sig")
                except UnicodeDecodeError as exc:
                    raise CalibrationArtifactError(
                        "OMS journal is not valid UTF-8 at line "
                        f"{line_number}"
                    ) from exc
                stripped = raw.strip()
                if not stripped:
                    continue
                record_count += 1
                record = _parse_record(stripped, line_number)

                if (
                    isinstance(record["version"], bool)
                    or record["version"] != JOURNAL_RECORD_VERSION
                ):
                    raise CalibrationArtifactError(
                        "calibration input requires an all-v2 OMS journal; "
                        f"invalid version at line {line_number}"
                    )
                seq = _positive_int(record["seq"], "seq", line_number)
                if seq != expected_seq:
                    raise CalibrationArtifactError(
                        "OMS journal sequence gap at line "
                        f"{line_number}: expected {expected_seq}, got {seq}"
                    )
                if not first_seq:
                    first_seq = seq
                last_seq = seq

                stored_prev_hash = record["prev_hash"]
                if not isinstance(stored_prev_hash, str):
                    raise CalibrationArtifactError(
                        f"prev_hash must be a string at line {line_number}"
                    )
                if stored_prev_hash != previous_hash:
                    raise CalibrationArtifactError(
                        "OMS journal hash-chain mismatch at line "
                        f"{line_number}"
                    )

                stored_hash = record["hash"]
                if not isinstance(stored_hash, str) or not _SHA256_RE.fullmatch(
                    stored_hash
                ):
                    raise CalibrationArtifactError(
                        f"hash must be a lowercase SHA-256 at line {line_number}"
                    )
                unsigned_record = dict(record)
                unsigned_record.pop("hash")
                try:
                    calculated_hash = hashlib.sha256(
                        _canonical_json(unsigned_record).encode("utf-8")
                    ).hexdigest()
                except (TypeError, ValueError) as exc:
                    raise CalibrationArtifactError(
                        "OMS journal record cannot be canonically encoded "
                        f"at line {line_number}"
                    ) from exc
                if not hmac.compare_digest(calculated_hash, stored_hash):
                    raise CalibrationArtifactError(
                        "OMS journal record hash mismatch at line "
                        f"{line_number}"
                    )

                record_at, record_at_utc = _validated_journal_timestamp(
                    record["ts"],
                    line_number,
                )
                if (
                    previous_record_at is not None
                    and record_at < previous_record_at
                ):
                    raise CalibrationArtifactError(
                        "OMS journal timestamps must be non-decreasing at "
                        f"line {line_number}"
                    )
                if not first_record_at_utc:
                    first_record_at_utc = record_at_utc
                last_record_at_utc = record_at_utc
                previous_record_at = record_at
                if not isinstance(record["kind"], str):
                    raise CalibrationArtifactError(
                        f"kind must be a string at line {line_number}"
                    )
                if not isinstance(record["payload"], dict):
                    raise CalibrationArtifactError(
                        f"payload must be an object at line {line_number}"
                    )
                last_record_kind = record["kind"]
                last_record_payload = record["payload"]
                evidence_record = _EvidenceRecord(
                    seq=seq,
                    line_number=line_number,
                    payload=record["payload"],
                )
                if record["kind"] == SAMPLE_KIND:
                    samples.append(
                        _validated_sample(
                            record,
                            seq=seq,
                            line_number=line_number,
                            record_at_utc=record_at_utc,
                            record_at_epoch=record_at.timestamp(),
                            seen_client_oids=seen_client_oids,
                        )
                    )
                elif record["kind"] == "order_snapshot":
                    client_oid = record["payload"].get("client_oid")
                    if isinstance(client_oid, str):
                        order_records_by_client_oid.setdefault(
                            client_oid,
                            [],
                        ).append(evidence_record)
                elif record["kind"] == "execution_record":
                    client_oid = record["payload"].get("client_oid")
                    if isinstance(client_oid, str):
                        execution_records_by_client_oid.setdefault(
                            client_oid,
                            [],
                        ).append(evidence_record)
                if (
                    calibration_replay is not None
                    and record["kind"] in CALIBRATION_KINDS
                ):
                    calibration_replay.consume(
                        record["kind"],
                        record["payload"],
                        seq=seq,
                        line_number=line_number,
                    )

                expected_seq = seq + 1
                previous_hash = stored_hash
    except OSError as exc:
        raise CalibrationArtifactError(
            f"cannot read OMS journal at {source}: {exc}"
        ) from exc

    if record_count == 0:
        raise CalibrationArtifactError("OMS journal contains no v2 records")
    if calibration_replay is not None:
        calibration_replay.finish(
            last_record_kind=last_record_kind,
            last_record_payload=last_record_payload,
        )
        foreign_samples = tuple(
            sample for sample in samples if sample.symbol != target_symbol
        )
        if foreign_samples:
            raise CalibrationArtifactError(
                "authorized calibration journal contains exposure samples "
                "outside its single permitted symbol"
            )
    target_samples = tuple(
        sample for sample in samples if sample.symbol == target_symbol
    )
    if not target_samples:
        raise CalibrationArtifactError(
            f"OMS journal contains no {SAMPLE_KIND} samples for "
            f"{target_symbol}"
        )

    deployment_ids = {sample.deployment_id for sample in target_samples}
    policy_hashes = {
        sample.strategy_policy_sha256 for sample in target_samples
    }
    implementation_hashes = {
        sample.implementation_sha256 for sample in target_samples
    }
    if len(deployment_ids) != 1:
        raise CalibrationArtifactError(
            f"RPI samples mix deployment_id values for {target_symbol}"
        )
    if len(policy_hashes) != 1:
        raise CalibrationArtifactError(
            f"RPI samples mix strategy policy hashes for {target_symbol}"
        )
    if len(implementation_hashes) != 1:
        raise CalibrationArtifactError(
            f"RPI samples mix implementation hashes for {target_symbol}"
        )

    correlated_bins: list[RPIExposureBin] = []
    for sample in target_samples:
        reservation = (
            calibration_replay.reservations.get(sample.client_oid)
            if calibration_replay is not None
            else None
        )
        if calibration_replay is not None and reservation is None:
            raise CalibrationArtifactError(
                f"RPI exposure sample {sample.client_oid!r} has no unique "
                "prior calibration send reservation"
            )
        correlated_bins.extend(
            _correlate_sample_evidence(
                sample,
                symbol=target_symbol,
                order_records=order_records_by_client_oid.get(
                    sample.client_oid,
                    (),
                ),
                execution_records=execution_records_by_client_oid.get(
                    sample.client_oid,
                    (),
                ),
                reservation=reservation,
            )
        )
    valid_samples = tuple(
        sample for sample in target_samples if not sample.censored
    )
    if not valid_samples:
        raise CalibrationArtifactError(
            f"OMS journal contains no valid uncensored RPI samples for "
            f"{target_symbol}"
        )
    first_ack_at = min(sample.ack_time for sample in valid_samples)
    last_terminal_at = max(
        sample.terminal_time for sample in valid_samples
    )
    first_ack_at_utc = (
        datetime.fromtimestamp(first_ack_at, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    last_terminal_at_utc = (
        datetime.fromtimestamp(last_terminal_at, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    return RPIJournalValidationSummary(
        journal_path=str(source),
        journal_sha256=journal_digest.hexdigest(),
        first_seq=first_seq,
        last_seq=last_seq,
        final_hash=previous_hash,
        record_count=record_count,
        first_record_at_utc=first_record_at_utc,
        last_record_at_utc=last_record_at_utc,
        first_sample_at_utc=valid_samples[0].record_at_utc,
        last_sample_at_utc=valid_samples[-1].record_at_utc,
        first_ack_at_utc=first_ack_at_utc,
        last_terminal_at_utc=last_terminal_at_utc,
        sample_count=len(valid_samples),
        unique_order_count=len(
            {sample.client_oid for sample in valid_samples}
        ),
        censored_sample_count=sum(
            1 for sample in target_samples if sample.censored
        ),
        deployment_id=next(iter(deployment_ids)),
        strategy_policy_sha256=next(iter(policy_hashes)),
        implementation_sha256=next(iter(implementation_hashes)),
        calibration_config_sha256=(
            calibration_replay.calibration_config_sha256
            if calibration_replay is not None
            else ""
        ),
        target_deployment_config_sha256=(
            calibration_replay.target_deployment_config_sha256
            if calibration_replay is not None
            else ""
        ),
        permit_activation_count=(
            len(calibration_replay.activations)
            if calibration_replay is not None
            else 0
        ),
        permit_sha256s=(
            tuple(
                activation.permit_sha256
                for activation in sorted(
                    calibration_replay.activations.values(),
                    key=lambda item: item.seq,
                )
            )
            if calibration_replay is not None
            else ()
        ),
        reservation_count=(
            len(calibration_replay.reservations)
            if calibration_replay is not None
            else 0
        ),
        cumulative_submitted_notional_microu=(
            calibration_replay.cumulative_notional_microu
            if calibration_replay is not None
            else 0
        ),
        exposure_bins=tuple(correlated_bins),
    )


def validate_rpi_calibration_journal(
    journal_path: str | Path,
    *,
    symbol: str,
    calibration_config: Mapping[str, Any] | None = None,
    target_deployment_config: Mapping[str, Any] | None = None,
    calibration_config_path: str | Path | None = None,
    target_deployment_config_path: str | Path | None = None,
) -> RPIJournalValidationSummary:
    """Validate one stable journal while excluding its configured OMS writer."""
    source = Path(journal_path).resolve()
    with _authorized_journal_fence(
        source,
        calibration_config=calibration_config,
        calibration_config_path=calibration_config_path,
    ):
        return _validate_rpi_calibration_journal_unlocked(
            source,
            symbol=symbol,
            calibration_config=calibration_config,
            target_deployment_config=target_deployment_config,
            calibration_config_path=calibration_config_path,
            target_deployment_config_path=target_deployment_config_path,
        )


def load_rpi_exposure_bins(
    journal_path: str | Path,
    *,
    symbol: str,
) -> tuple[RPIExposureBin, ...]:
    """Compatibility wrapper returning bins from fully validated evidence."""
    return validate_rpi_calibration_journal(
        journal_path,
        symbol=symbol,
    ).exposure_bins


def _artifact_from_estimate(
    symbol: str,
    bins: tuple[RPIExposureBin, ...],
    *,
    journal_summary: RPIJournalValidationSummary,
    deployment_config_sha256: str,
) -> dict[str, Any]:
    return {
        "schema": ARTIFACT_SCHEMA,
        "model": EXPECTED_MODEL,
        "venue": EXPECTED_VENUE,
        "data_source": EXPECTED_DATA_SOURCE,
        "units_version": EXPECTED_UNITS_VERSION,
        "validated_formula_version": EXPECTED_FORMULA_VERSION,
        "deployment_id": journal_summary.deployment_id,
        "strategy_policy_sha256": (
            journal_summary.strategy_policy_sha256
        ),
        "deployment_config_sha256": deployment_config_sha256,
        "implementation_sha256": journal_summary.implementation_sha256,
        "order_sample_count": journal_summary.sample_count,
        "unique_order_count": journal_summary.unique_order_count,
        "exposure_sample_count": sum(
            item.sample_count for item in bins
        ),
        "source_journal": {
            "sha256": journal_summary.journal_sha256,
            "deployment_id": journal_summary.deployment_id,
            "first_seq": journal_summary.first_seq,
            "last_seq": journal_summary.last_seq,
            "final_hash": journal_summary.final_hash,
            "record_count": journal_summary.record_count,
            "first_record_at_utc": (
                journal_summary.first_record_at_utc
            ),
            "last_record_at_utc": journal_summary.last_record_at_utc,
            "first_sample_at_utc": (
                journal_summary.first_sample_at_utc
            ),
            "last_sample_at_utc": journal_summary.last_sample_at_utc,
            "first_ack_at_utc": journal_summary.first_ack_at_utc,
            "last_terminal_at_utc": (
                journal_summary.last_terminal_at_utc
            ),
            "sample_count": journal_summary.sample_count,
            "unique_order_count": journal_summary.unique_order_count,
            "censored_sample_count": (
                journal_summary.censored_sample_count
            ),
            "strategy_policy_sha256": (
                journal_summary.strategy_policy_sha256
            ),
            "implementation_sha256": (
                journal_summary.implementation_sha256
            ),
            "calibration_config_sha256": (
                journal_summary.calibration_config_sha256
            ),
            "target_deployment_config_sha256": (
                journal_summary.target_deployment_config_sha256
            ),
            "permit_activation_count": (
                journal_summary.permit_activation_count
            ),
            "permit_sha256s": list(journal_summary.permit_sha256s),
            "reservation_count": journal_summary.reservation_count,
            "cumulative_submitted_notional_microu": (
                journal_summary.cumulative_submitted_notional_microu
            ),
        },
        "symbols": {
            symbol: {
                "order_sample_count": journal_summary.sample_count,
                "unique_order_count": journal_summary.unique_order_count,
                "rpi_exposure_bins": [
                    {
                        "depth_bps": item.depth_bps,
                        "exposure_seconds": item.exposure_seconds,
                        "fill_count": item.fill_count,
                        "sample_count": item.sample_count,
                    }
                    for item in bins
                ]
            }
        },
    }


def _aggregate_samples(
    samples: tuple[RPIExposureBin, ...],
) -> tuple[RPIExposureBin, ...]:
    grouped: dict[float, list[RPIExposureBin]] = {}
    for sample in samples:
        grouped.setdefault(sample.depth_bps, []).append(sample)

    aggregated = []
    for depth_bps in sorted(grouped):
        depth_samples = grouped[depth_bps]
        try:
            exposure_seconds = math.fsum(
                sample.exposure_seconds for sample in depth_samples
            )
        except (OverflowError, ValueError) as exc:
            raise CalibrationArtifactError(
                f"aggregate exposure is not finite at depth {depth_bps:g}"
            ) from exc
        if not math.isfinite(exposure_seconds) or exposure_seconds <= 0.0:
            raise CalibrationArtifactError(
                f"aggregate exposure is not finite at depth {depth_bps:g}"
            )
        aggregated.append(
            RPIExposureBin(
                depth_bps=depth_bps,
                exposure_seconds=exposure_seconds,
                fill_count=sum(
                    sample.fill_count for sample in depth_samples
                ),
                sample_count=sum(
                    sample.sample_count for sample in depth_samples
                ),
            )
        )
    return tuple(aggregated)


def _sync_directory(path: Path) -> None:
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


def _atomic_write_json(
    output_path: Path,
    value: Mapping[str, Any],
    *,
    overwrite: bool,
) -> None:
    if not overwrite and os.path.lexists(output_path):
        raise CalibrationArtifactError(
            f"output already exists: {output_path}; use --overwrite explicitly"
        )

    try:
        encoded = (
            json.dumps(
                value,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        )
    except (TypeError, ValueError) as exc:
        raise CalibrationArtifactError(
            "calibration artifact is not strict JSON"
        ) from exc

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CalibrationArtifactError(
            f"cannot create output directory {output_path.parent}: {exc}"
        ) from exc

    descriptor = -1
    temporary_path: Path | None = None
    try:
        descriptor, raw_temporary_path = tempfile.mkstemp(
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
        )
        temporary_path = Path(raw_temporary_path)
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

        if overwrite:
            os.replace(temporary_path, output_path)
        else:
            try:
                os.link(temporary_path, output_path)
            except FileExistsError as exc:
                raise CalibrationArtifactError(
                    f"output already exists: {output_path}; "
                    "use --overwrite explicitly"
                ) from exc
            temporary_path.unlink()
        temporary_path = None
        _sync_directory(output_path.parent)
    except CalibrationArtifactError:
        raise
    except OSError as exc:
        raise CalibrationArtifactError(
            f"cannot atomically write calibration artifact {output_path}: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _build_rpi_calibration_artifact_with_fence_held(
    journal_path: str | Path,
    output_path: str | Path,
    *,
    symbol: str,
    deployment_config_sha256: str,
    calibration_config: Mapping[str, Any] | None = None,
    target_deployment_config: Mapping[str, Any] | None = None,
    calibration_config_path: str | Path | None = None,
    target_deployment_config_path: str | Path | None = None,
    requirements: RPIIntensityRequirements | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Build and atomically publish an artifact only when the fit is READY."""
    source = Path(journal_path).resolve()
    destination = Path(output_path).resolve()
    if source == destination:
        raise CalibrationArtifactError(
            "journal_path and output_path must be different files"
        )
    if not overwrite and os.path.lexists(destination):
        raise CalibrationArtifactError(
            f"output already exists: {destination}; use --overwrite explicitly"
        )

    target_symbol = _validated_symbol(symbol)
    config_digest = _validated_sha256(
        deployment_config_sha256,
        "deployment_config_sha256",
        0,
    )
    journal_summary = _validate_rpi_calibration_journal_unlocked(
        source,
        symbol=target_symbol,
        calibration_config=calibration_config,
        target_deployment_config=target_deployment_config,
        calibration_config_path=calibration_config_path,
        target_deployment_config_path=target_deployment_config_path,
    )
    authorization_replayed = (
        calibration_config is not None
        and target_deployment_config is not None
    )
    if authorization_replayed and not hmac.compare_digest(
        journal_summary.target_deployment_config_sha256, config_digest
    ):
        raise CalibrationArtifactError(
            "target deployment configuration hash does not match journal "
            "authorization"
        )
    active_requirements = requirements or RPIIntensityRequirements()
    if journal_summary.sample_count != journal_summary.unique_order_count:
        raise CalibrationArtifactError(
            "RPI journal sample_count does not equal unique_order_count"
        )
    if (
        journal_summary.unique_order_count
        < active_requirements.min_sample_count
    ):
        raise CalibrationArtifactError(
            "RPI journal unique_order_count is below min_sample_count: "
            f"{journal_summary.unique_order_count}"
            f"<{active_requirements.min_sample_count}"
        )
    aggregated_bins = _aggregate_samples(journal_summary.exposure_bins)
    estimate = estimate_rpi_intensity(
        aggregated_bins,
        requirements=active_requirements,
    )
    if not estimate.ready:
        reasons = ",".join(estimate.reasons) or estimate.state
        raise CalibrationArtifactError(
            "RPI intensity estimate is not READY: "
            f"{estimate.state}[{reasons}]"
        )

    artifact = _artifact_from_estimate(
        target_symbol,
        estimate.bins,
        journal_summary=journal_summary,
        deployment_config_sha256=config_digest,
    )
    _atomic_write_json(destination, artifact, overwrite=overwrite)
    return artifact


def build_rpi_calibration_artifact(
    journal_path: str | Path,
    output_path: str | Path,
    *,
    symbol: str,
    deployment_config_sha256: str,
    calibration_config: Mapping[str, Any] | None = None,
    target_deployment_config: Mapping[str, Any] | None = None,
    calibration_config_path: str | Path | None = None,
    target_deployment_config_path: str | Path | None = None,
    requirements: RPIIntensityRequirements | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Build and publish while excluding the configured live journal writer."""
    source = Path(journal_path).resolve()
    with _authorized_journal_fence(
        source,
        calibration_config=calibration_config,
        calibration_config_path=calibration_config_path,
    ):
        return _build_rpi_calibration_artifact_with_fence_held(
            source,
            output_path,
            symbol=symbol,
            deployment_config_sha256=deployment_config_sha256,
            calibration_config=calibration_config,
            target_deployment_config=target_deployment_config,
            calibration_config_path=calibration_config_path,
            target_deployment_config_path=target_deployment_config_path,
            requirements=requirements,
            overwrite=overwrite,
        )


def _load_effective_deployment_config(path: str | Path) -> dict[str, Any]:
    source = Path(path).resolve()
    try:
        with source.open("r", encoding="utf-8") as handle:
            raw = json.load(
                handle,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_object_without_duplicate_keys,
            )
    except (
        OSError,
        json.JSONDecodeError,
        CalibrationArtifactError,
    ) as exc:
        raise CalibrationArtifactError(
            f"cannot read deployment config {source}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise CalibrationArtifactError(
            "deployment config must be a JSON object"
        )

    from infrastructure.config_scaling import (
        normalize_root_config_preapproval,
    )

    configured = normalize_root_config_preapproval(raw)
    execution = configured.get("execution", {})
    paper_trade = configured.get("paper_trade", {})
    if (
        not isinstance(execution, Mapping)
        or execution.get("mode") != "live"
        or not isinstance(paper_trade, Mapping)
        or paper_trade.get("enabled") is not False
        or configured.get("testnet") is not False
    ):
        raise CalibrationArtifactError(
            "calibration artifact binding requires an explicit production "
            "live config"
        )
    return configured


def build_argument_parser() -> argparse.ArgumentParser:
    defaults = RPIIntensityRequirements()
    parser = argparse.ArgumentParser(
        description=(
            "Build a GLFT RPI calibration artifact from a local, hash-chained "
            "OMS journal. No network, gateway, OMS, or order path is used."
        )
    )
    parser.add_argument("--journal", required=True, help="OMS journal JSONL path")
    parser.add_argument("--output", required=True, help="Artifact JSON path")
    parser.add_argument(
        "--config",
        required=True,
        help=(
            "Exact live deployment config whose redacted safety projection "
            "will be bound into the artifact"
        ),
    )
    parser.add_argument(
        "--calibration-config",
        required=True,
        help=(
            "Exact rpi_calibration_canary config used to validate signed "
            "permit activations and send reservations in the journal"
        ),
    )
    parser.add_argument(
        "--symbol",
        required=True,
        help="Exact uppercase symbol to accept, for example XAUUSDT",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Explicitly replace an existing output atomically",
    )
    parser.add_argument(
        "--min-sample-count",
        type=int,
        default=defaults.min_sample_count,
    )
    parser.add_argument(
        "--min-depth-level-count",
        type=int,
        default=defaults.min_depth_level_count,
    )
    parser.add_argument(
        "--min-total-exposure-seconds",
        type=float,
        default=defaults.min_total_exposure_seconds,
    )
    parser.add_argument(
        "--min-fill-count",
        type=int,
        default=defaults.min_fill_count,
    )
    parser.add_argument(
        "--min-depth-span-bps",
        type=float,
        default=defaults.min_depth_span_bps,
    )
    parser.add_argument(
        "--min-k-per-bps",
        type=float,
        default=defaults.min_k_per_bps,
    )
    parser.add_argument(
        "--max-k-per-bps",
        type=float,
        default=defaults.max_k_per_bps,
    )
    return parser


# Public governance API used by both runtime approval validation and the
# offline CLI. The underscore aliases remain available only for compatibility
# with older test/tool imports.
authorized_journal_fence = _authorized_journal_fence
load_effective_deployment_config = _load_effective_deployment_config
validate_rpi_calibration_journal_unlocked = (
    _validate_rpi_calibration_journal_unlocked
)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        deployment_config = _load_effective_deployment_config(args.config)
        calibration_config = _load_effective_deployment_config(
            args.calibration_config
        )
        strategy = deployment_config.get("strategy", {})
        if not isinstance(strategy, Mapping):
            raise CalibrationArtifactError("strategy must be an object")
        model = canonical_model_key(
            strategy.get("primary_model", strategy.get("name"))
        )
        if model != EXPECTED_MODEL:
            raise CalibrationArtifactError(
                "deployment config primary model must be 'glft'"
            )
        calibration_strategy = calibration_config.get("strategy", {})
        if not isinstance(calibration_strategy, Mapping):
            raise CalibrationArtifactError(
                "calibration config strategy must be an object"
            )
        calibration_model = canonical_model_key(
            calibration_strategy.get(
                "primary_model",
                calibration_strategy.get("name"),
            )
        )
        if calibration_model != EXPECTED_MODEL:
            raise CalibrationArtifactError(
                "calibration config primary model must be 'glft'"
            )
        configured_symbols = deployment_config.get("symbols")
        if configured_symbols != [args.symbol]:
            raise CalibrationArtifactError(
                "deployment config must contain exactly the requested symbol"
            )
        if calibration_config.get("symbols") != [args.symbol]:
            raise CalibrationArtifactError(
                "calibration config must contain exactly the requested symbol"
            )
        deployment_launch = deployment_config.get("live_launch", {})
        calibration_launch = calibration_config.get("live_launch", {})
        if (
            not isinstance(deployment_launch, Mapping)
            or deployment_launch.get("stage") != "canary"
            or not isinstance(calibration_launch, Mapping)
            or calibration_launch.get("stage")
            != "rpi_calibration_canary"
        ):
            raise CalibrationArtifactError(
                "config stages must be target 'canary' and "
                "'rpi_calibration_canary'"
            )
        deployment_id = str(
            deployment_launch.get("deployment_id", "") or ""
        ).strip()
        if (
            not deployment_id
            or deployment_id
            != str(
                calibration_launch.get("deployment_id", "") or ""
            ).strip()
        ):
            raise CalibrationArtifactError(
                "deployment and calibration config deployment_id values "
                "must match"
            )
        if strategy_policy_sha256(
            calibration_config,
            calibration_model,
        ) != strategy_policy_sha256(deployment_config, model):
            raise CalibrationArtifactError(
                "calibration and target GLFT strategy policies must match"
            )
        requirements = RPIIntensityRequirements(
            min_sample_count=args.min_sample_count,
            min_depth_level_count=args.min_depth_level_count,
            min_total_exposure_seconds=args.min_total_exposure_seconds,
            min_fill_count=args.min_fill_count,
            min_depth_span_bps=args.min_depth_span_bps,
            min_k_per_bps=args.min_k_per_bps,
            max_k_per_bps=args.max_k_per_bps,
        )
        artifact = build_rpi_calibration_artifact(
            args.journal,
            args.output,
            symbol=args.symbol,
            deployment_config_sha256=deployment_config_sha256(
                deployment_config
            ),
            calibration_config=calibration_config,
            target_deployment_config=deployment_config,
            calibration_config_path=args.calibration_config,
            target_deployment_config_path=args.config,
            requirements=requirements,
            overwrite=args.overwrite,
        )
    except (CalibrationArtifactError, ValueError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2

    symbol_payload = artifact["symbols"][args.symbol]
    bins = symbol_payload["rpi_exposure_bins"]
    sample_count = sum(item["sample_count"] for item in bins)
    print(
        f"WROTE {Path(args.output).resolve()} "
        f"({args.symbol}, {sample_count} samples, {len(bins)} depth bins)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
