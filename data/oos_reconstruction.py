from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from data.live_evidence import (
    LIVE_EVIDENCE_SCHEMA,
    LiveEvidenceCorruptionError,
    iter_validated_live_evidence_records,
    validate_live_evidence_journal,
)


OOS_RECONSTRUCTION_SCHEMA = "chronoshft.glft_rpi_oos_reconstruction.v1"
RAW_OOS_EVIDENCE_SCHEMA = "chronoshft.glft_rpi_raw_oos_evidence.v1"
OMS_JOURNAL_RECORD_VERSION = 2
_SHA256_HEX_LENGTH = 64
_TWO_SIDED_95_T_CRITICAL = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
    11: 2.201,
    12: 2.179,
    13: 2.160,
    14: 2.145,
    15: 2.131,
    16: 2.120,
    17: 2.110,
    18: 2.101,
    19: 2.093,
    20: 2.086,
    21: 2.080,
    22: 2.074,
    23: 2.069,
    24: 2.064,
    25: 2.060,
    26: 2.056,
    27: 2.052,
    28: 2.048,
    29: 2.045,
    30: 2.042,
}


class OOSReconstructionError(RuntimeError):
    """Raised when raw evidence cannot support a deterministic OOS result."""


@dataclass(frozen=True, slots=True)
class OMSJournalIdentity:
    path: str
    sha256: str
    record_count: int
    first_seq: int
    last_seq: int
    final_hash: str
    last_kind: str


@dataclass(frozen=True, slots=True)
class Execution:
    execution_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    exchange_time: float
    commission: Decimal
    booked_fee: Decimal
    commission_asset: str
    realized_pnl: Decimal
    is_maker: bool
    order_type: str
    time_in_force: str
    is_rpi: bool
    reduce_only: bool

    @property
    def signed_quantity(self) -> Decimal:
        return self.quantity if self.side == "BUY" else -self.quantity

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.price


@dataclass(frozen=True, slots=True)
class MarkObservation:
    symbol: str
    exchange_time: float
    mark_price: Decimal
    funding_rate: Decimal
    next_funding_time: float


@dataclass(frozen=True, slots=True)
class FundingCashFlow:
    asset: str
    event_time: float
    amount: Decimal


@dataclass(frozen=True, slots=True)
class OOSReconstructionRequirements:
    markout_horizons_ms: tuple[int, ...] = (1_000, 5_000)
    max_markout_lag_ms: int = 2_000
    min_utc_day_clusters: int = 5
    flat_tolerance: Decimal = Decimal("0.000000001")
    pnl_crosscheck_tolerance_usdt: Decimal = Decimal("0.000001")

    def __post_init__(self):
        if (
            not self.markout_horizons_ms
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
                for value in self.markout_horizons_ms
            )
            or len(set(self.markout_horizons_ms))
            != len(self.markout_horizons_ms)
        ):
            raise ValueError("markout horizons must be unique positive integers")
        if (
            isinstance(self.max_markout_lag_ms, bool)
            or not isinstance(self.max_markout_lag_ms, int)
            or self.max_markout_lag_ms <= 0
        ):
            raise ValueError("max_markout_lag_ms must be a positive integer")
        if (
            isinstance(self.min_utc_day_clusters, bool)
            or not isinstance(self.min_utc_day_clusters, int)
            or self.min_utc_day_clusters < 2
        ):
            raise ValueError("min_utc_day_clusters must be an integer >= 2")
        if self.flat_tolerance <= 0 or not self.flat_tolerance.is_finite():
            raise ValueError("flat_tolerance must be positive and finite")
        if (
            self.pnl_crosscheck_tolerance_usdt <= 0
            or not self.pnl_crosscheck_tolerance_usdt.is_finite()
        ):
            raise ValueError(
                "pnl_crosscheck_tolerance_usdt must be positive and finite"
            )


@dataclass(frozen=True, slots=True)
class RawEvidence:
    oms_identity: OMSJournalIdentity
    executions: tuple[Execution, ...]
    external_cash_flow_times: tuple[float, ...]
    market_journal_sha256: str
    market_journal_record_count: int
    market_journal_final_hash: str
    market_journal_mark_count: int
    market_journal_account_count: int
    marks: tuple[MarkObservation, ...]
    funding_cash_flows: tuple[FundingCashFlow, ...]


def _reject_json_constant(value: str):
    raise OOSReconstructionError(
        f"non-finite JSON constant is not allowed: {value}"
    )


def _object_without_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise OOSReconstructionError(
                f"duplicate JSON key is not allowed: {key!r}"
            )
        result[key] = value
    return result


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise OOSReconstructionError(
            "value is not strict canonical JSON"
        ) from exc


def _parse_json_line(raw: str, source: str, line_number: int) -> dict[str, Any]:
    try:
        value = json.loads(
            raw,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (json.JSONDecodeError, OOSReconstructionError) as exc:
        raise OOSReconstructionError(
            f"invalid {source} JSON at line {line_number}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise OOSReconstructionError(
            f"{source} line {line_number} must be a JSON object"
        )
    return value


def _sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise OOSReconstructionError(
            f"cannot hash evidence file {path}: {exc}"
        ) from exc
    return digest.hexdigest()


def _decimal(
    value: Any,
    field: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if isinstance(value, bool):
        raise OOSReconstructionError(f"{field} must be a finite decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise OOSReconstructionError(
            f"{field} must be a finite decimal"
        ) from exc
    if not parsed.is_finite():
        raise OOSReconstructionError(f"{field} must be finite")
    if positive and parsed <= 0:
        raise OOSReconstructionError(f"{field} must be positive")
    if nonnegative and parsed < 0:
        raise OOSReconstructionError(f"{field} must be nonnegative")
    return parsed


def _finite_float(
    value: Any,
    field: str,
    *,
    positive: bool = False,
) -> float:
    if isinstance(value, bool):
        raise OOSReconstructionError(f"{field} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise OOSReconstructionError(f"{field} must be numeric") from exc
    if not math.isfinite(parsed):
        raise OOSReconstructionError(f"{field} must be finite")
    if positive and parsed <= 0:
        raise OOSReconstructionError(f"{field} must be positive")
    return parsed


def _parse_utc(value: Any, field: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise OOSReconstructionError(f"{field} is required")
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise OOSReconstructionError(
            f"{field} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OOSReconstructionError(f"{field} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _quote_asset(symbol: str) -> str:
    for suffix in ("USDT", "USDC", "BUSD", "FDUSD"):
        if symbol.endswith(suffix):
            return suffix
    raise OOSReconstructionError(
        f"unsupported linear-contract quote asset for {symbol}"
    )


def _read_oms_journal(
    path: str | os.PathLike[str],
) -> tuple[OMSJournalIdentity, tuple[dict[str, Any], ...]]:
    source = Path(path)
    digest = hashlib.sha256()
    records = []
    previous_hash = ""
    expected_seq = 1
    last_kind = ""
    final_hash = ""

    try:
        with source.open("rb") as handle:
            for line_number, raw_bytes in enumerate(handle, start=1):
                digest.update(raw_bytes)
                try:
                    raw = raw_bytes.decode(
                        "utf-8-sig" if line_number == 1 else "utf-8"
                    )
                except UnicodeDecodeError as exc:
                    raise OOSReconstructionError(
                        f"OMS journal is not UTF-8 at line {line_number}"
                    ) from exc
                if not raw.strip():
                    raise OOSReconstructionError(
                        f"blank OMS journal record at line {line_number}"
                    )
                record = _parse_json_line(raw, "OMS journal", line_number)
                expected_keys = {
                    "version",
                    "seq",
                    "ts",
                    "kind",
                    "payload",
                    "prev_hash",
                    "hash",
                }
                if set(record) != expected_keys:
                    raise OOSReconstructionError(
                        f"OMS journal record keys are invalid at line {line_number}"
                    )
                if record.get("version") != OMS_JOURNAL_RECORD_VERSION:
                    raise OOSReconstructionError(
                        "OOS reconstruction accepts only a fresh v2 OMS journal"
                    )
                seq = record.get("seq")
                if (
                    isinstance(seq, bool)
                    or not isinstance(seq, int)
                    or seq != expected_seq
                ):
                    raise OOSReconstructionError(
                        "OMS journal sequence gap at line "
                        f"{line_number}: expected {expected_seq}, got {seq!r}"
                    )
                _parse_utc(record.get("ts"), f"OMS line {line_number} ts")
                kind = str(record.get("kind", "") or "")
                if not kind or not isinstance(record.get("payload"), dict):
                    raise OOSReconstructionError(
                        f"OMS journal kind/payload is invalid at line {line_number}"
                    )
                if str(record.get("prev_hash", "") or "") != previous_hash:
                    raise OOSReconstructionError(
                        f"OMS journal previous-hash mismatch at line {line_number}"
                    )
                stored_hash = str(record.get("hash", "") or "").lower()
                if (
                    len(stored_hash) != _SHA256_HEX_LENGTH
                    or any(ch not in "0123456789abcdef" for ch in stored_hash)
                ):
                    raise OOSReconstructionError(
                        f"OMS journal hash is invalid at line {line_number}"
                    )
                unsigned = dict(record)
                unsigned.pop("hash")
                calculated = hashlib.sha256(
                    _canonical_json(unsigned).encode("utf-8")
                ).hexdigest()
                if calculated != stored_hash:
                    raise OOSReconstructionError(
                        f"OMS journal hash mismatch at line {line_number}"
                    )
                records.append(record)
                previous_hash = stored_hash
                final_hash = stored_hash
                last_kind = kind
                expected_seq += 1
    except OOSReconstructionError:
        raise
    except OSError as exc:
        raise OOSReconstructionError(
            f"cannot read OMS journal {source}: {exc}"
        ) from exc

    if not records:
        raise OOSReconstructionError("OMS journal is empty")
    if last_kind != "oms_stopped":
        raise OOSReconstructionError(
            "OMS journal is not cleanly stopped; raw OOS evidence is censored"
        )
    identity = OMSJournalIdentity(
        path=str(source.resolve()),
        sha256=digest.hexdigest(),
        record_count=len(records),
        first_seq=1,
        last_seq=len(records),
        final_hash=final_hash,
        last_kind=last_kind,
    )
    return identity, tuple(records)


def _execution_from_payload(payload: Mapping[str, Any]) -> Execution:
    required = {
        "execution_id",
        "symbol",
        "side",
        "fill_qty",
        "fill_price",
        "exchange_time",
        "commission",
        "commission_asset",
        "booked_fee",
        "realized_pnl",
        "is_maker",
        "order_type",
        "time_in_force",
        "is_rpi",
        "reduce_only",
    }
    missing = sorted(required.difference(payload))
    if missing:
        raise OOSReconstructionError(
            "execution_record lacks raw OOS fields: " + ",".join(missing)
        )
    execution_id = str(payload.get("execution_id", "") or "").strip()
    symbol = str(payload.get("symbol", "") or "").strip().upper()
    side = str(payload.get("side", "") or "").strip().upper()
    if not execution_id or not symbol or side not in {"BUY", "SELL"}:
        raise OOSReconstructionError("execution identity/side is invalid")
    is_maker = payload.get("is_maker")
    is_rpi = payload.get("is_rpi")
    reduce_only = payload.get("reduce_only")
    if not isinstance(is_maker, bool):
        raise OOSReconstructionError(
            f"execution {execution_id} lacks maker truth"
        )
    if not isinstance(is_rpi, bool) or not isinstance(reduce_only, bool):
        raise OOSReconstructionError(
            f"execution {execution_id} lacks RPI/reduce-only truth"
        )
    raw_commission = payload.get("commission")
    raw_realized_pnl = payload.get("realized_pnl")
    if raw_commission is None or raw_realized_pnl is None:
        raise OOSReconstructionError(
            f"execution {execution_id} lacks exchange commission/PnL truth"
        )
    return Execution(
        execution_id=execution_id,
        symbol=symbol,
        side=side,
        quantity=_decimal(
            payload.get("fill_qty"),
            f"{execution_id}.fill_qty",
            positive=True,
        ),
        price=_decimal(
            payload.get("fill_price"),
            f"{execution_id}.fill_price",
            positive=True,
        ),
        exchange_time=_finite_float(
            payload.get("exchange_time"),
            f"{execution_id}.exchange_time",
            positive=True,
        ),
        commission=_decimal(
            raw_commission,
            f"{execution_id}.commission",
            nonnegative=True,
        ),
        booked_fee=_decimal(
            payload.get("booked_fee"),
            f"{execution_id}.booked_fee",
            nonnegative=True,
        ),
        commission_asset=str(
            payload.get("commission_asset", "") or ""
        ).strip().upper(),
        realized_pnl=_decimal(
            raw_realized_pnl,
            f"{execution_id}.realized_pnl",
        ),
        is_maker=is_maker,
        order_type=str(
            payload.get("order_type", "") or ""
        ).strip().upper(),
        time_in_force=str(
            payload.get("time_in_force", "") or ""
        ).strip().upper(),
        is_rpi=is_rpi,
        reduce_only=reduce_only,
    )


def _extract_oms_evidence(
    records: Iterable[Mapping[str, Any]],
) -> tuple[tuple[Execution, ...], tuple[float, ...]]:
    executions = []
    execution_ids = set()
    external_cash_flow_times = []
    for record in records:
        kind = record.get("kind")
        payload = record.get("payload", {})
        if kind == "execution_record":
            execution = _execution_from_payload(payload)
            if execution.execution_id in execution_ids:
                raise OOSReconstructionError(
                    f"duplicate execution_id: {execution.execution_id}"
                )
            execution_ids.add(execution.execution_id)
            executions.append(execution)
        elif kind == "external_cash_flow_record":
            event_ms = _finite_float(
                payload.get("income_time_ms"),
                "external cash-flow income_time_ms",
                positive=True,
            )
            external_cash_flow_times.append(event_ms / 1000.0)
    executions.sort(key=lambda item: (item.exchange_time, item.execution_id))
    return tuple(executions), tuple(sorted(external_cash_flow_times))


def _mark_from_payload(payload: Mapping[str, Any]) -> MarkObservation:
    symbol = str(payload.get("symbol", "") or "").strip().upper()
    if not symbol:
        raise OOSReconstructionError("mark_price symbol is required")
    return MarkObservation(
        symbol=symbol,
        exchange_time=_finite_float(
            payload.get("exchange_timestamp"),
            "mark_price.exchange_timestamp",
            positive=True,
        ),
        mark_price=_decimal(
            payload.get("mark_price"),
            "mark_price.mark_price",
            positive=True,
        ),
        funding_rate=_decimal(
            payload.get("funding_rate"),
            "mark_price.funding_rate",
        ),
        next_funding_time=_finite_float(
            payload.get("next_funding_timestamp"),
            "mark_price.next_funding_timestamp",
            positive=True,
        ),
    )


def _funding_from_account_payload(
    payload: Mapping[str, Any],
    quote_assets: frozenset[str],
) -> tuple[FundingCashFlow, ...]:
    if str(payload.get("reason", "") or "").strip().upper() != "FUNDING_FEE":
        return ()
    event_time = _finite_float(
        payload.get("event_time"),
        "funding account_update.event_time",
        positive=True,
    )
    balances = payload.get("balances")
    if not isinstance(balances, Mapping):
        raise OOSReconstructionError(
            "funding account_update.balances must be an object"
        )
    flows = []
    for asset in sorted(quote_assets):
        balance = balances.get(asset)
        if not isinstance(balance, Mapping):
            continue
        amount_value = balance.get("balance_change")
        if amount_value is None:
            raise OOSReconstructionError(
                f"funding account update lacks {asset} balance_change"
            )
        flows.append(
            FundingCashFlow(
                asset=asset,
                event_time=event_time,
                amount=_decimal(
                    amount_value,
                    f"funding {asset} balance_change",
                ),
            )
        )
    if not flows:
        raise OOSReconstructionError(
            "funding account update has no configured quote-asset balance change"
        )
    return tuple(flows)


def _read_market_evidence(
    path: str | os.PathLike[str],
    *,
    deployment_id: str,
    deployment_config_sha256: str,
    symbols: tuple[str, ...],
) -> tuple[
    str,
    int,
    str,
    int,
    int,
    tuple[MarkObservation, ...],
    tuple[FundingCashFlow, ...],
    tuple[tuple[int, str], ...],
]:
    summary = validate_live_evidence_journal(path)
    if summary.last_kind != "clean_stop":
        raise OOSReconstructionError(
            "market evidence is not cleanly stopped"
        )
    if summary.deployment_ids != (deployment_id,):
        raise OOSReconstructionError(
            "market evidence deployment_id does not match"
        )
    if summary.deployment_config_sha256s != (
        deployment_config_sha256.lower(),
    ):
        raise OOSReconstructionError(
            "market evidence deployment config digest does not match"
        )

    configured_symbols = frozenset(symbols)
    quote_assets = frozenset(_quote_asset(symbol) for symbol in symbols)
    marks = []
    funding = []
    oms_anchors = []
    last_mark_by_symbol: dict[str, MarkObservation] = {}
    funding_keys = set()
    try:
        for record in iter_validated_live_evidence_records(path):
            kind = record["kind"]
            payload = record["payload"]
            if kind == "session_start":
                if payload.get("schema") != LIVE_EVIDENCE_SCHEMA:
                    raise OOSReconstructionError(
                        "market evidence session schema is invalid"
                    )
                if str(payload.get("deployment_id", "") or "") != deployment_id:
                    raise OOSReconstructionError(
                        "market evidence session deployment_id changed"
                    )
                if (
                    str(
                        payload.get("deployment_config_sha256", "") or ""
                    ).lower()
                    != deployment_config_sha256.lower()
                ):
                    raise OOSReconstructionError(
                        "market evidence session config digest changed"
                    )
            elif kind == "mark_price":
                mark = _mark_from_payload(payload)
                if mark.symbol not in configured_symbols:
                    raise OOSReconstructionError(
                        f"off-config mark symbol in evidence: {mark.symbol}"
                    )
                previous = last_mark_by_symbol.get(mark.symbol)
                if previous is not None:
                    if mark.exchange_time < previous.exchange_time:
                        raise OOSReconstructionError(
                            f"mark exchange time regressed for {mark.symbol}"
                        )
                    if (
                        mark.exchange_time == previous.exchange_time
                        and mark != previous
                    ):
                        raise OOSReconstructionError(
                            f"conflicting duplicate mark for {mark.symbol}"
                        )
                    if mark == previous:
                        continue
                last_mark_by_symbol[mark.symbol] = mark
                marks.append(mark)
            elif kind == "account_update":
                for flow in _funding_from_account_payload(
                    payload,
                    quote_assets,
                ):
                    key = (flow.asset, flow.event_time, flow.amount)
                    if key in funding_keys:
                        raise OOSReconstructionError(
                            "duplicate funding account cash flow"
                        )
                    funding_keys.add(key)
                    funding.append(flow)
            elif kind == "clean_stop":
                anchor = payload.get("oms_journal_anchor")
                if not isinstance(anchor, Mapping):
                    raise OOSReconstructionError(
                        "market evidence clean_stop lacks OMS anchor"
                    )
                next_seq = anchor.get("next_seq")
                last_hash = str(anchor.get("last_hash", "") or "").lower()
                if (
                    isinstance(next_seq, bool)
                    or not isinstance(next_seq, int)
                    or next_seq < 1
                    or (
                        last_hash
                        and (
                            len(last_hash) != _SHA256_HEX_LENGTH
                            or any(
                                ch not in "0123456789abcdef"
                                for ch in last_hash
                            )
                        )
                    )
                    or (next_seq == 1) != (last_hash == "")
                ):
                    raise OOSReconstructionError(
                        "market evidence clean_stop OMS anchor is invalid"
                    )
                oms_anchors.append((next_seq, last_hash))
    except LiveEvidenceCorruptionError as exc:
        raise OOSReconstructionError(
            f"market evidence validation failed: {exc}"
        ) from exc

    marks.sort(key=lambda item: (item.exchange_time, item.symbol))
    funding.sort(key=lambda item: (item.event_time, item.asset))
    return (
        _sha256_file(path),
        summary.record_count,
        summary.final_hash,
        summary.mark_price_count,
        summary.account_update_count,
        tuple(marks),
        tuple(funding),
        tuple(oms_anchors),
    )


def _validate_oms_anchors(
    records: tuple[Mapping[str, Any], ...],
    anchors: tuple[tuple[int, str], ...],
) -> None:
    if not anchors:
        raise OOSReconstructionError(
            "market evidence has no clean-stop OMS anchors"
        )
    hashes_by_seq = {
        int(record["seq"]): str(record["hash"])
        for record in records
    }
    for next_seq, last_hash in anchors:
        if next_seq == 1:
            continue
        anchored_seq = next_seq - 1
        if hashes_by_seq.get(anchored_seq) != last_hash:
            raise OOSReconstructionError(
                "market evidence OMS anchor does not match the OMS journal "
                f"at sequence {anchored_seq}"
            )


def load_raw_oos_evidence(
    *,
    oms_journal_path: str | os.PathLike[str],
    market_evidence_path: str | os.PathLike[str],
    deployment_id: str,
    deployment_config_sha256: str,
    symbols: Iterable[str],
) -> RawEvidence:
    deployment_id = str(deployment_id or "").strip()
    config_digest = str(deployment_config_sha256 or "").strip().lower()
    normalized_symbols = tuple(
        str(symbol or "").strip().upper() for symbol in symbols
    )
    if not deployment_id:
        raise OOSReconstructionError("deployment_id is required")
    if (
        len(config_digest) != _SHA256_HEX_LENGTH
        or any(ch not in "0123456789abcdef" for ch in config_digest)
    ):
        raise OOSReconstructionError(
            "deployment_config_sha256 is invalid"
        )
    if (
        not normalized_symbols
        or any(not symbol for symbol in normalized_symbols)
        or len(set(normalized_symbols)) != len(normalized_symbols)
    ):
        raise OOSReconstructionError(
            "symbols must be unique and non-empty"
        )

    oms_identity, oms_records = _read_oms_journal(oms_journal_path)
    executions, external_cash_flow_times = _extract_oms_evidence(oms_records)
    (
        market_sha256,
        market_records,
        market_final_hash,
        market_mark_count,
        market_account_count,
        marks,
        funding,
        oms_anchors,
    ) = _read_market_evidence(
        market_evidence_path,
        deployment_id=deployment_id,
        deployment_config_sha256=config_digest,
        symbols=normalized_symbols,
    )
    _validate_oms_anchors(oms_records, oms_anchors)
    return RawEvidence(
        oms_identity=oms_identity,
        executions=executions,
        external_cash_flow_times=external_cash_flow_times,
        market_journal_sha256=market_sha256,
        market_journal_record_count=market_records,
        market_journal_final_hash=market_final_hash,
        market_journal_mark_count=market_mark_count,
        market_journal_account_count=market_account_count,
        marks=marks,
        funding_cash_flows=funding,
    )


def _position_at(
    executions: Iterable[Execution],
    cutoff: float,
) -> dict[str, Decimal]:
    positions: dict[str, Decimal] = defaultdict(Decimal)
    for execution in executions:
        if execution.exchange_time >= cutoff:
            break
        positions[execution.symbol] += execution.signed_quantity
    return dict(positions)


def _require_flat(
    positions: Mapping[str, Decimal],
    *,
    symbols: Iterable[str],
    tolerance: Decimal,
    boundary: str,
) -> None:
    breaches = {
        symbol: positions.get(symbol, Decimal(0))
        for symbol in symbols
        if abs(positions.get(symbol, Decimal(0))) > tolerance
    }
    if breaches:
        rendered = ",".join(
            f"{symbol}={quantity}" for symbol, quantity in sorted(breaches.items())
        )
        raise OOSReconstructionError(
            f"OOS {boundary} boundary is not flat: {rendered}"
        )


def _daily_cluster_lcb95(
    observations: Iterable[tuple[float, float]],
    *,
    minimum_clusters: int,
) -> tuple[float, float, int]:
    clusters: dict[str, list[float]] = defaultdict(list)
    for timestamp, value in observations:
        day = datetime.fromtimestamp(
            timestamp,
            tz=timezone.utc,
        ).date().isoformat()
        clusters[day].append(value)
    daily_means = [
        statistics.fmean(values)
        for _, values in sorted(clusters.items())
    ]
    cluster_count = len(daily_means)
    if cluster_count < minimum_clusters:
        raise OOSReconstructionError(
            "markout evidence has too few independent UTC-day clusters: "
            f"{cluster_count}<{minimum_clusters}"
        )
    mean = statistics.fmean(daily_means)
    standard_deviation = statistics.stdev(daily_means)
    degrees_of_freedom = cluster_count - 1
    critical = _TWO_SIDED_95_T_CRITICAL.get(
        degrees_of_freedom,
        1.96,
    )
    lcb = mean - critical * standard_deviation / math.sqrt(cluster_count)
    if not math.isfinite(mean) or not math.isfinite(lcb):
        raise OOSReconstructionError("markout confidence bound is non-finite")
    return mean, lcb, cluster_count


def _markout_metrics(
    executions: tuple[Execution, ...],
    marks: tuple[MarkObservation, ...],
    *,
    horizons_ms: tuple[int, ...],
    max_lag_ms: int,
    min_clusters: int,
) -> dict[str, dict[str, Any]]:
    marks_by_symbol: dict[str, list[MarkObservation]] = defaultdict(list)
    mark_times: dict[str, list[float]] = defaultdict(list)
    for mark in marks:
        marks_by_symbol[mark.symbol].append(mark)
        mark_times[mark.symbol].append(mark.exchange_time)

    metrics = {}
    for horizon_ms in horizons_ms:
        horizon_sec = horizon_ms / 1000.0
        max_lag_sec = max_lag_ms / 1000.0
        observations = []
        for execution in executions:
            target = execution.exchange_time + horizon_sec
            times = mark_times.get(execution.symbol, ())
            index = bisect.bisect_left(times, target)
            if index >= len(times):
                raise OOSReconstructionError(
                    f"missing {horizon_ms}ms markout for "
                    f"{execution.execution_id}"
                )
            mark = marks_by_symbol[execution.symbol][index]
            if mark.exchange_time - target > max_lag_sec:
                raise OOSReconstructionError(
                    f"stale {horizon_ms}ms markout for "
                    f"{execution.execution_id}"
                )
            direction = Decimal(1) if execution.side == "BUY" else Decimal(-1)
            gross_bps = (
                direction
                * (mark.mark_price - execution.price)
                / execution.price
                * Decimal(10_000)
            )
            fee_bps = (
                execution.commission
                / execution.notional
                * Decimal(10_000)
            )
            net_bps = gross_bps - fee_bps
            observations.append(
                (execution.exchange_time, float(net_bps))
            )
        mean, lcb, cluster_count = _daily_cluster_lcb95(
            observations,
            minimum_clusters=min_clusters,
        )
        metrics[str(horizon_ms)] = {
            "sample_count": len(observations),
            "mean_net_edge_bps": mean,
            "net_edge_bps_lcb95": lcb,
            "cluster_count": cluster_count,
            "cluster_unit": "UTC_DAY",
            "estimator": "T_DISTRIBUTION_CLUSTER_MEAN",
            "max_mark_lag_ms": max_lag_ms,
        }
    return metrics


def _equity_curve(
    executions: tuple[Execution, ...],
    marks: tuple[MarkObservation, ...],
    funding: tuple[FundingCashFlow, ...],
    *,
    symbols: tuple[str, ...],
    started_at: float,
    ended_at: float,
) -> tuple[Decimal, Decimal]:
    latest_marks: dict[str, Decimal] = {}
    for mark in marks:
        if mark.exchange_time > started_at:
            break
        latest_marks[mark.symbol] = mark.mark_price
    missing = sorted(set(symbols).difference(latest_marks))
    if missing:
        raise OOSReconstructionError(
            "no mark at or before OOS start for: " + ",".join(missing)
        )

    events = []
    for execution in executions:
        events.append(
            (
                execution.exchange_time,
                1,
                "execution",
                execution,
            )
        )
    for cash_flow in funding:
        events.append(
            (
                cash_flow.event_time,
                0,
                "funding",
                cash_flow,
            )
        )
    for mark in marks:
        if started_at < mark.exchange_time <= ended_at:
            events.append(
                (
                    mark.exchange_time,
                    2,
                    "mark",
                    mark,
                )
            )
    events.sort(key=lambda item: (item[0], item[1]))

    positions: dict[str, Decimal] = defaultdict(Decimal)
    cash = Decimal(0)
    peak = Decimal(0)
    max_drawdown = Decimal(0)
    for _, _, kind, value in events:
        if kind == "execution":
            signed_quantity = value.signed_quantity
            positions[value.symbol] += signed_quantity
            cash -= signed_quantity * value.price
            cash -= value.commission
        elif kind == "funding":
            cash += value.amount
        else:
            latest_marks[value.symbol] = value.mark_price

        equity = cash
        for symbol, quantity in positions.items():
            mark_price = latest_marks.get(symbol)
            if quantity and mark_price is None:
                raise OOSReconstructionError(
                    f"missing mark while position is open: {symbol}"
                )
            if mark_price is not None:
                equity += quantity * mark_price
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    return cash, max_drawdown


def _decimal_json(value: Decimal) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise OOSReconstructionError("decimal result cannot be encoded")
    return result


def reconstruct_oos_evidence(
    raw: RawEvidence,
    *,
    deployment_id: str,
    deployment_config_sha256: str,
    symbols: Iterable[str],
    training_ended_at: datetime,
    started_at: datetime,
    ended_at: datetime,
    requirements: OOSReconstructionRequirements | None = None,
) -> dict[str, Any]:
    """Rebuild OOS PnL, drawdown, maker truth, fees, funding, and markout."""

    active_requirements = requirements or OOSReconstructionRequirements()
    normalized_symbols = tuple(
        str(symbol or "").strip().upper() for symbol in symbols
    )
    configured_symbols = frozenset(normalized_symbols)
    if not normalized_symbols or len(configured_symbols) != len(
        normalized_symbols
    ):
        raise OOSReconstructionError("configured symbols are invalid")
    training_ended = training_ended_at.astimezone(timezone.utc)
    started = started_at.astimezone(timezone.utc)
    ended = ended_at.astimezone(timezone.utc)
    if not training_ended < started < ended:
        raise OOSReconstructionError(
            "require training_ended_at < started_at < ended_at"
        )
    started_ts = started.timestamp()
    ended_ts = ended.timestamp()

    off_config_executions = sorted(
        {
            execution.symbol
            for execution in raw.executions
            if execution.symbol not in configured_symbols
        }
    )
    if off_config_executions:
        raise OOSReconstructionError(
            "OMS evidence contains off-config executions: "
            + ",".join(off_config_executions)
        )

    start_positions = _position_at(raw.executions, started_ts)
    _require_flat(
        start_positions,
        symbols=normalized_symbols,
        tolerance=active_requirements.flat_tolerance,
        boundary="start",
    )
    oos_executions = tuple(
        execution
        for execution in raw.executions
        if started_ts <= execution.exchange_time <= ended_ts
    )
    if not oos_executions:
        raise OOSReconstructionError("OOS window has no executions")
    final_positions = dict(start_positions)
    for execution in oos_executions:
        final_positions[execution.symbol] = (
            final_positions.get(execution.symbol, Decimal(0))
            + execution.signed_quantity
        )
    _require_flat(
        final_positions,
        symbols=normalized_symbols,
        tolerance=active_requirements.flat_tolerance,
        boundary="end",
    )

    if any(
        started_ts <= event_time <= ended_ts
        for event_time in raw.external_cash_flow_times
    ):
        raise OOSReconstructionError(
            "OOS window contains an external account cash flow"
        )

    markout_marks = tuple(
        mark
        for mark in raw.marks
        if mark.symbol in configured_symbols
        and started_ts <= mark.exchange_time <= (
            ended_ts
            + max(active_requirements.markout_horizons_ms) / 1000.0
            + active_requirements.max_markout_lag_ms / 1000.0
        )
    )
    oos_sample_count = sum(
        1
        for mark in markout_marks
        if started_ts <= mark.exchange_time <= ended_ts
    )
    if not oos_sample_count:
        raise OOSReconstructionError("OOS window has no mark observations")
    oos_funding = tuple(
        cash_flow
        for cash_flow in raw.funding_cash_flows
        if started_ts <= cash_flow.event_time <= ended_ts
    )

    maker_count = sum(
        1 for execution in oos_executions if execution.is_maker
    )
    rpi_count = sum(
        1
        for execution in oos_executions
        if execution.is_rpi
        and execution.time_in_force == "RPI"
        and execution.order_type == "LIMIT"
    )
    if maker_count != len(oos_executions):
        raise OOSReconstructionError(
            "OOS evidence contains a non-maker execution"
        )
    if rpi_count != len(oos_executions):
        raise OOSReconstructionError(
            "OOS evidence contains a non-RPI execution"
        )
    total_notional = sum(
        (execution.notional for execution in oos_executions),
        Decimal(0),
    )
    total_commission = sum(
        (execution.commission for execution in oos_executions),
        Decimal(0),
    )
    total_booked_fee = sum(
        (execution.booked_fee for execution in oos_executions),
        Decimal(0),
    )
    if total_notional <= 0:
        raise OOSReconstructionError("OOS execution notional is not positive")
    for execution in oos_executions:
        expected_asset = _quote_asset(execution.symbol)
        if execution.commission_asset != expected_asset:
            raise OOSReconstructionError(
                f"unsupported commission asset for {execution.execution_id}: "
                f"{execution.commission_asset!r}"
            )
        if execution.booked_fee != execution.commission:
            raise OOSReconstructionError(
                f"booked fee differs from exchange fee for "
                f"{execution.execution_id}"
            )
    if total_commission != 0 or total_booked_fee != 0:
        raise OOSReconstructionError(
            "OOS RPI executions did not have exactly zero booked commission"
        )

    funding_total = sum(
        (cash_flow.amount for cash_flow in oos_funding),
        Decimal(0),
    )
    exchange_net_pnl = (
        sum(
            (execution.realized_pnl for execution in oos_executions),
            Decimal(0),
        )
        - total_commission
        + funding_total
    )
    curve_marks = []
    for symbol in normalized_symbols:
        previous = [
            mark
            for mark in raw.marks
            if mark.symbol == symbol and mark.exchange_time <= started_ts
        ]
        if not previous:
            raise OOSReconstructionError(
                f"no mark at or before OOS start for: {symbol}"
            )
        curve_marks.append(previous[-1])
    curve_marks.extend(
        mark
        for mark in raw.marks
        if mark.symbol in configured_symbols
        and started_ts < mark.exchange_time <= ended_ts
    )
    curve_marks.sort(key=lambda item: (item.exchange_time, item.symbol))
    reconstructed_net_pnl, max_drawdown = _equity_curve(
        oos_executions,
        tuple(curve_marks),
        oos_funding,
        symbols=normalized_symbols,
        started_at=started_ts,
        ended_at=ended_ts,
    )
    if (
        abs(reconstructed_net_pnl - exchange_net_pnl)
        > active_requirements.pnl_crosscheck_tolerance_usdt
    ):
        raise OOSReconstructionError(
            "exchange realized PnL does not match fill-ledger reconstruction: "
            f"exchange={exchange_net_pnl} "
            f"reconstructed={reconstructed_net_pnl}"
        )

    markout = _markout_metrics(
        oos_executions,
        markout_marks,
        horizons_ms=active_requirements.markout_horizons_ms,
        max_lag_ms=active_requirements.max_markout_lag_ms,
        min_clusters=active_requirements.min_utc_day_clusters,
    )
    raw_evidence = {
        "schema": RAW_OOS_EVIDENCE_SCHEMA,
        "deployment_id": str(deployment_id),
        "deployment_config_sha256": str(
            deployment_config_sha256
        ).lower(),
        "oms_journal": {
            "sha256": raw.oms_identity.sha256,
            "record_count": raw.oms_identity.record_count,
            "first_seq": raw.oms_identity.first_seq,
            "last_seq": raw.oms_identity.last_seq,
            "final_hash": raw.oms_identity.final_hash,
            "last_kind": raw.oms_identity.last_kind,
        },
        "market_evidence_journal": {
            "sha256": raw.market_journal_sha256,
            "record_count": raw.market_journal_record_count,
            "final_hash": raw.market_journal_final_hash,
            "mark_price_count": raw.market_journal_mark_count,
            "account_update_count": raw.market_journal_account_count,
            "last_kind": "clean_stop",
        },
        "reconstruction": {
            "schema": OOS_RECONSTRUCTION_SCHEMA,
            "flat_tolerance": str(active_requirements.flat_tolerance),
            "pnl_crosscheck_tolerance_usdt": str(
                active_requirements.pnl_crosscheck_tolerance_usdt
            ),
            "max_markout_lag_ms": (
                active_requirements.max_markout_lag_ms
            ),
            "min_utc_day_clusters": (
                active_requirements.min_utc_day_clusters
            ),
        },
    }
    commission_rate = total_commission / total_notional
    oos = {
        "method": "WALK_FORWARD",
        "training_ended_at_utc": _utc_text(training_ended),
        "started_at_utc": _utc_text(started),
        "ended_at_utc": _utc_text(ended),
        "sample_count": oos_sample_count,
        "fill_count": len(oos_executions),
        "maker_fill_fraction": maker_count / len(oos_executions),
        "rpi_fill_fraction": rpi_count / len(oos_executions),
        "rpi_commission_rate": (
            "0" if commission_rate == 0 else format(commission_rate, "f")
        ),
        "total_commission_usdt": _decimal_json(total_commission),
        "total_booked_fee_usdt": _decimal_json(total_booked_fee),
        "funding_pnl_usdt": _decimal_json(funding_total),
        "net_pnl_usdt": _decimal_json(reconstructed_net_pnl),
        "exchange_net_pnl_usdt": _decimal_json(exchange_net_pnl),
        "max_drawdown_usdt": _decimal_json(max_drawdown),
        "markout": markout,
        "raw_evidence": raw_evidence,
    }
    return oos


__all__ = [
    "OOS_RECONSTRUCTION_SCHEMA",
    "RAW_OOS_EVIDENCE_SCHEMA",
    "Execution",
    "FundingCashFlow",
    "MarkObservation",
    "OMSJournalIdentity",
    "OOSReconstructionError",
    "OOSReconstructionRequirements",
    "RawEvidence",
    "load_raw_oos_evidence",
    "reconstruct_oos_evidence",
]
