import ast
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from alpha.rpi_intensity import RPIExposureBin, estimate_rpi_intensity
from scripts.build_rpi_calibration_artifact import (
    ARTIFACT_SCHEMA,
    EXPECTED_DATA_SOURCE,
    EXPECTED_FORMULA_VERSION,
    EXPECTED_MODEL,
    EXPECTED_STRATEGY,
    EXPECTED_UNITS_VERSION,
    EXPECTED_VENUE,
    SAMPLE_KIND,
    SAMPLE_SCHEMA,
    CalibrationArtifactError,
    build_rpi_calibration_artifact as _build_rpi_calibration_artifact,
    validate_rpi_calibration_journal,
)


ROOT = Path(__file__).resolve().parents[1]
SYMBOL = "XAUUSDT"
DEPLOYMENT_ID = "rpi-calibration-test"
POLICY_SHA256 = "1" * 64
IMPLEMENTATION_SHA256 = "2" * 64
DEPLOYMENT_CONFIG_SHA256 = "3" * 64


def build_rpi_calibration_artifact(*args, **kwargs):
    kwargs.setdefault(
        "deployment_config_sha256",
        DEPLOYMENT_CONFIG_SHA256,
    )
    return _build_rpi_calibration_artifact(*args, **kwargs)


class _SamplePayload(dict):
    """Keep old test mutations concise while emitting only the v2 schema."""

    _BIN_FIELDS = frozenset({"depth_bps", "exposure_seconds"})

    def __getitem__(self, key):
        if key in self._BIN_FIELDS:
            return dict.__getitem__(self, "exposure_bins")[0][key]
        return dict.__getitem__(self, key)

    def __setitem__(self, key, value):
        if key in self._BIN_FIELDS:
            dict.__getitem__(self, "exposure_bins")[0][key] = value
            return
        dict.__setitem__(self, key, value)


def _canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sample(index, depth_bps, fill_count, **overrides):
    terminal_status = "FILLED" if fill_count else "CANCELLED"
    quantity = float(fill_count if terminal_status == "FILLED" else 1)
    ack_time = (
        datetime(2026, 7, 16, tzinfo=timezone.utc).timestamp()
        + index * 1_000.0
    )
    terminal_time = (
        ack_time + fill_count
        if terminal_status == "FILLED"
        else ack_time + 10.0
    )
    payload = _SamplePayload({
        "schema": SAMPLE_SCHEMA,
        "strategy": EXPECTED_STRATEGY,
        "symbol": SYMBOL,
        "client_oid": f"rpi-cal-{index:04d}",
        "exchange_oid": f"ex-rpi-cal-{index:04d}",
        "terminal_status": terminal_status,
        "side": "BUY",
        "price": 2000.0,
        "quantity": quantity,
        "ack_time": ack_time,
        "ack_monotonic": 10.0,
        "terminal_time": terminal_time,
        "terminal_monotonic": 20.0,
        "deployment_id": DEPLOYMENT_ID,
        "strategy_policy_sha256": POLICY_SHA256,
        "implementation_sha256": IMPLEMENTATION_SHA256,
        "exposure_bins": [
            {
                "depth_bps": depth_bps,
                "exposure_seconds": 10.0,
                "fill_count": fill_count,
                "sample_count": 1,
            }
        ],
        "fill_count": fill_count,
        "censored": False,
        "censor_reason": "",
        "units_version": EXPECTED_UNITS_VERSION,
        "formula_version": EXPECTED_FORMULA_VERSION,
        "data_source": EXPECTED_DATA_SOURCE,
    })
    for field, value in overrides.items():
        payload[field] = value
    return payload


def _ready_samples():
    fills_by_depth = (
        (0.0, [20] * 10),
        (1.0, [12] * 9 + [13]),
        (2.0, [7] * 6 + [8] * 4),
    )
    samples = []
    index = 0
    for depth_bps, fill_counts in fills_by_depth:
        for fill_count in fill_counts:
            samples.append(_sample(index, depth_bps, fill_count))
            index += 1
    return samples


def _intent(sample, *, volume):
    return {
        "strategy_id": EXPECTED_STRATEGY,
        "symbol": sample["symbol"],
        "side": "BUY",
        "price": 2000.0,
        "volume": volume,
        "order_type": "LIMIT",
        "time_in_force": "RPI",
        "is_post_only": True,
        "reduce_only": False,
        "policy": "PASSIVE",
        "tag": "glft_quote",
    }


def _order_snapshot(
    sample,
    *,
    status,
    source,
    exchange_oid,
    volume,
    filled_volume=0.0,
    fill_price=0.0,
    exchange_status="",
    exchange_time=0.0,
    updated_monotonic=1.0,
    recovered_from_journal=False,
):
    cumulative_cost = filled_volume * fill_price
    payload = {
        "client_oid": sample["client_oid"],
        "exchange_oid": exchange_oid,
        "status": status,
        "filled_volume": filled_volume,
        "avg_price": fill_price if filled_volume else 0.0,
        "cumulative_cost": cumulative_cost,
        "created_at": sample["ack_time"] - 1.0,
        "updated_at": exchange_time or sample["ack_time"] - 1.0,
        "created_monotonic": 1.0,
        "updated_monotonic": updated_monotonic,
        "recovered_from_journal": recovered_from_journal,
        "error_msg": "",
        "last_update_seq": 0,
        "last_exchange_status": exchange_status,
        "last_exchange_update_time": exchange_time,
        "intent": _intent(sample, volume=volume),
        "source": source,
    }
    if source == "exchange_update":
        payload["extra"] = {
            "exchange_status": exchange_status,
            "seq": 0,
            "cum_filled_qty": filled_volume,
        }
    return payload


def _execution(
    sample,
    *,
    sample_index,
    ordinal,
    exchange_oid,
    cumulative_qty,
    fill_price,
):
    trade_id = sample_index * 1000 + ordinal
    return {
        "execution_id": f"BINANCE:{sample['symbol']}:{trade_id}",
        "venue": "BINANCE",
        "client_oid": sample["client_oid"],
        "exchange_oid": exchange_oid,
        "strategy_id": EXPECTED_STRATEGY,
        "symbol": sample["symbol"],
        "side": "BUY",
        "fill_qty": 1.0,
        "fill_price": fill_price,
        "cum_filled_qty": cumulative_qty,
        "exchange_status": (
            "FILLED"
            if sample["terminal_status"] == "FILLED"
            and ordinal == sample["fill_count"]
            else "PARTIALLY_FILLED"
        ),
        "exchange_time": sample["ack_time"] + ordinal,
        "trade_id": trade_id,
        "commission": 0.0,
        "commission_asset": "USDT",
        "booked_fee": 0.0,
        "realized_pnl": 0.0,
        "is_maker": True,
        "pre_status": "NEW" if ordinal == 1 else "PARTIALLY_FILLED",
    }


def _sample_lifecycle_entries(sample, *, sample_index):
    raw_fill_count = sample["fill_count"]
    fill_count = (
        raw_fill_count
        if isinstance(raw_fill_count, int)
        and not isinstance(raw_fill_count, bool)
        and raw_fill_count >= 0
        else 0
    )
    terminal_status = sample["terminal_status"]
    volume = (
        max(1, fill_count)
        if terminal_status == "FILLED"
        else max(1, fill_count + 1)
    )
    exchange_oid = sample["exchange_oid"]
    raw_depth = sample["depth_bps"]
    fixture_depth = (
        float(raw_depth)
        if isinstance(raw_depth, (int, float))
        and not isinstance(raw_depth, bool)
        else 0.0
    )
    fill_price = 2000.0 + fixture_depth
    entries = [
        (
            "order_snapshot",
            _order_snapshot(
                sample,
                status="SUBMITTING",
                source="accepted",
                exchange_oid="",
                volume=volume,
            ),
        ),
        (
            "order_snapshot",
            _order_snapshot(
                sample,
                status="PENDING_ACK",
                source="rest_ack",
                exchange_oid=exchange_oid,
                volume=volume,
                exchange_status="NEW",
                exchange_time=sample["ack_time"],
                updated_monotonic=sample["ack_monotonic"],
            ),
        ),
        (
            "order_snapshot",
            _order_snapshot(
                sample,
                status="NEW",
                source="exchange_update",
                exchange_oid=exchange_oid,
                volume=volume,
                exchange_status="NEW",
                exchange_time=sample["ack_time"],
                updated_monotonic=sample["ack_monotonic"],
            ),
        ),
    ]
    for ordinal in range(1, fill_count + 1):
        exchange_status = (
            "FILLED"
            if terminal_status == "FILLED" and ordinal == fill_count
            else "PARTIALLY_FILLED"
        )
        entries.extend(
            [
                (
                    "execution_record",
                    _execution(
                        sample,
                        sample_index=sample_index,
                        ordinal=ordinal,
                        exchange_oid=exchange_oid,
                        cumulative_qty=float(ordinal),
                        fill_price=fill_price,
                    ),
                ),
                (
                    "order_snapshot",
                    _order_snapshot(
                        sample,
                        status=exchange_status,
                        source="exchange_update",
                        exchange_oid=exchange_oid,
                        volume=volume,
                        filled_volume=float(ordinal),
                        fill_price=fill_price,
                        exchange_status=exchange_status,
                        exchange_time=sample["ack_time"] + ordinal,
                        updated_monotonic=(
                            sample["terminal_monotonic"]
                            if exchange_status == "FILLED"
                            else sample["ack_monotonic"] + ordinal * 0.1
                        ),
                    ),
                ),
            ]
        )
    if terminal_status != "FILLED":
        exchange_status = {
            "CANCELLED": "CANCELED",
            "REJECTED": "REJECTED",
            "EXPIRED": "EXPIRED",
        }.get(terminal_status, terminal_status)
        entries.append(
            (
                "order_snapshot",
                _order_snapshot(
                    sample,
                    status=terminal_status,
                    source="exchange_update",
                    exchange_oid=exchange_oid,
                    volume=volume,
                    filled_volume=float(fill_count),
                    fill_price=fill_price,
                    exchange_status=exchange_status,
                    exchange_time=sample["terminal_time"],
                    updated_monotonic=sample["terminal_monotonic"],
                ),
            )
        )
    entries.append((SAMPLE_KIND, sample))
    return entries


def _chain(
    entries,
    *,
    start_seq=1,
    started_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
    duration_seconds=None,
):
    records = []
    previous_hash = ""
    denominator = max(1, len(entries) - 1)
    for offset, (kind, payload) in enumerate(entries):
        elapsed = (
            float(offset)
            if duration_seconds is None
            else float(duration_seconds) * offset / denominator
        )
        if duration_seconds is None:
            if kind == SAMPLE_KIND:
                record_epoch = float(payload["terminal_time"]) + 0.001
            elif kind == "order_snapshot":
                record_epoch = float(
                    payload.get(
                        "updated_at",
                        started_at.timestamp() + offset,
                    )
                )
            elif kind == "execution_record":
                record_epoch = float(payload["exchange_time"])
            else:
                record_epoch = started_at.timestamp()
            record_at = datetime.fromtimestamp(
                record_epoch,
                tz=timezone.utc,
            )
        else:
            record_at = started_at + timedelta(seconds=elapsed)
        unsigned = {
            "version": 2,
            "seq": start_seq + offset,
            "ts": (
                record_at.isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            ),
            "kind": kind,
            "payload": payload,
            "prev_hash": previous_hash,
        }
        record_hash = hashlib.sha256(
            _canonical_json(unsigned).encode("utf-8")
        ).hexdigest()
        record = dict(unsigned)
        record["hash"] = record_hash
        records.append(record)
        previous_hash = record_hash
    return records


def _rehash(records):
    previous_hash = ""
    for record in records:
        record["prev_hash"] = previous_hash
        unsigned = dict(record)
        unsigned.pop("hash", None)
        record["hash"] = hashlib.sha256(
            _canonical_json(unsigned).encode("utf-8")
        ).hexdigest()
        previous_hash = record["hash"]
    return records


def _sample_entries(samples):
    entries = [("runtime_state", {"state": "READY"})]
    for sample_index, sample in enumerate(samples):
        entries.extend(
            _sample_lifecycle_entries(
                sample,
                sample_index=sample_index,
            )
        )
    return entries


def _write_records(path, records):
    path.write_text(
        "\n".join(_canonical_json(record) for record in records) + "\n",
        encoding="utf-8",
    )


def _write_ready_journal(path):
    _write_records(path, _chain(_sample_entries(_ready_samples())))


def test_builder_has_no_network_gateway_or_oms_imports():
    script_path = ROOT / "scripts" / "build_rpi_calibration_artifact.py"
    tree = ast.parse(script_path.read_text(encoding="utf-8"))
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    forbidden_prefixes = (
        "aiohttp",
        "gateway",
        "httpx",
        "oms",
        "requests",
        "socket",
        "urllib",
    )
    assert not {
        module
        for module in imported_modules
        if module.startswith(forbidden_prefixes)
    }


def test_valid_hash_chain_builds_ready_aggregated_artifact_atomically(tmp_path):
    journal = tmp_path / "oms.jsonl"
    output = tmp_path / "calibration.json"
    _write_ready_journal(journal)

    artifact = build_rpi_calibration_artifact(
        journal,
        output,
        symbol=SYMBOL,
    )

    assert output.is_file()
    assert json.loads(output.read_text(encoding="utf-8")) == artifact
    assert artifact["schema"] == ARTIFACT_SCHEMA
    assert artifact["model"] == EXPECTED_MODEL
    assert artifact["venue"] == EXPECTED_VENUE
    assert artifact["data_source"] == EXPECTED_DATA_SOURCE
    assert artifact["units_version"] == EXPECTED_UNITS_VERSION
    assert (
        artifact["validated_formula_version"] == EXPECTED_FORMULA_VERSION
    )
    assert artifact["deployment_id"] == DEPLOYMENT_ID
    assert artifact["strategy_policy_sha256"] == POLICY_SHA256
    assert (
        artifact["deployment_config_sha256"]
        == DEPLOYMENT_CONFIG_SHA256
    )
    assert artifact["implementation_sha256"] == IMPLEMENTATION_SHA256
    assert artifact["order_sample_count"] == 30
    assert artifact["unique_order_count"] == 30
    assert artifact["exposure_sample_count"] == 30
    assert artifact["source_journal"]["sample_count"] == 30
    assert artifact["source_journal"]["unique_order_count"] == 30
    assert artifact["source_journal"]["censored_sample_count"] == 0
    bins = artifact["symbols"][SYMBOL]["rpi_exposure_bins"]
    assert bins == [
        {
            "depth_bps": 0.0,
            "exposure_seconds": 100.0,
            "fill_count": 200,
            "sample_count": 10,
        },
        {
            "depth_bps": 1.0,
            "exposure_seconds": 100.0,
            "fill_count": 121,
            "sample_count": 10,
        },
        {
            "depth_bps": 2.0,
            "exposure_seconds": 100.0,
            "fill_count": 74,
            "sample_count": 10,
        },
    ]
    estimate = estimate_rpi_intensity(
        tuple(RPIExposureBin(**item) for item in bins)
    )
    assert estimate.ready
    assert estimate.A_per_s == pytest.approx(2.0, rel=0.02)
    assert estimate.k_per_bps == pytest.approx(0.5, rel=0.03)
    assert not list(tmp_path.glob(".calibration.json.*.tmp"))


def test_existing_output_is_not_overwritten_by_default(tmp_path):
    journal = tmp_path / "oms.jsonl"
    output = tmp_path / "calibration.json"
    _write_ready_journal(journal)
    output.write_text("operator-owned", encoding="utf-8")

    with pytest.raises(CalibrationArtifactError, match="already exists"):
        build_rpi_calibration_artifact(
            journal,
            output,
            symbol=SYMBOL,
        )

    assert output.read_text(encoding="utf-8") == "operator-owned"
    assert not list(tmp_path.glob(".calibration.json.*.tmp"))


def test_explicit_overwrite_replaces_existing_output(tmp_path):
    journal = tmp_path / "oms.jsonl"
    output = tmp_path / "calibration.json"
    _write_ready_journal(journal)
    output.write_text("stale", encoding="utf-8")

    artifact = build_rpi_calibration_artifact(
        journal,
        output,
        symbol=SYMBOL,
        overwrite=True,
    )

    assert json.loads(output.read_text(encoding="utf-8")) == artifact
    assert not list(tmp_path.glob(".calibration.json.*.tmp"))


def test_public_journal_summary_binds_bytes_identity_and_timestamps(tmp_path):
    journal = tmp_path / "oms.jsonl"
    records = _chain(
        _sample_entries(_ready_samples()),
    )
    _write_records(journal, records)

    summary = validate_rpi_calibration_journal(journal, symbol=SYMBOL)

    assert summary.journal_sha256 == hashlib.sha256(
        journal.read_bytes()
    ).hexdigest()
    assert summary.first_seq == 1
    assert summary.last_seq == len(records)
    assert summary.record_count == len(records)
    assert summary.sample_count == 30
    assert summary.unique_order_count == 30
    assert summary.censored_sample_count == 0
    assert summary.first_record_at_utc == records[0]["ts"]
    assert summary.last_record_at_utc == records[-1]["ts"]
    assert summary.first_sample_at_utc > summary.first_record_at_utc
    assert summary.last_sample_at_utc == records[-1]["ts"]


def test_censored_sample_is_counted_but_excluded_from_artifact(tmp_path):
    journal = tmp_path / "oms.jsonl"
    output = tmp_path / "calibration.json"
    censored = _sample(99, 0.0, 0)
    entries = _sample_entries([*_ready_samples(), censored])
    censored["censored"] = True
    censored["censor_reason"] = "stale_pre_ack_orderbook"
    censored["exposure_bins"] = []
    _write_records(journal, _chain(entries))

    artifact = build_rpi_calibration_artifact(
        journal,
        output,
        symbol=SYMBOL,
    )

    assert artifact["order_sample_count"] == 30
    assert artifact["unique_order_count"] == 30
    assert artifact["source_journal"]["censored_sample_count"] == 1


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("deployment_id", "other-deployment", "mix deployment_id"),
        ("strategy_policy_sha256", "3" * 64, "mix strategy policy"),
        ("implementation_sha256", "4" * 64, "mix implementation"),
    ],
)
def test_mixed_sampling_identity_is_rejected(
    tmp_path,
    field,
    value,
    expected,
):
    journal = tmp_path / "oms.jsonl"
    output = tmp_path / "calibration.json"
    samples = _ready_samples()
    samples[0][field] = value
    _write_records(journal, _chain(_sample_entries(samples)))

    with pytest.raises(CalibrationArtifactError, match=expected):
        build_rpi_calibration_artifact(journal, output, symbol=SYMBOL)

    assert not output.exists()


def test_exposure_duration_must_equal_acknowledged_lifetime(tmp_path):
    journal = tmp_path / "oms.jsonl"
    output = tmp_path / "calibration.json"
    samples = _ready_samples()
    samples[0]["exposure_seconds"] = 9.0
    _write_records(journal, _chain(_sample_entries(samples)))

    with pytest.raises(
        CalibrationArtifactError,
        match="duration does not match",
    ):
        build_rpi_calibration_artifact(journal, output, symbol=SYMBOL)

    assert not output.exists()


def test_journal_timestamps_must_be_utc_and_non_decreasing(tmp_path):
    journal = tmp_path / "oms.jsonl"
    records = _chain(_sample_entries(_ready_samples()))
    records[2]["ts"] = records[0]["ts"]
    _write_records(journal, _rehash(records))

    with pytest.raises(
        CalibrationArtifactError,
        match="timestamps must be non-decreasing",
    ):
        validate_rpi_calibration_journal(journal, symbol=SYMBOL)


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (
            lambda records: records[2].__setitem__(
                "seq",
                records[2]["seq"] + 1,
            ),
            "sequence gap",
        ),
        (
            lambda records: records[2].__setitem__("prev_hash", "0" * 64),
            "hash-chain mismatch",
        ),
        (
            lambda records: records[2]["payload"].__setitem__(
                "exposure_seconds",
                11.0,
            ),
            "record hash mismatch",
        ),
    ],
)
def test_corrupt_sequence_or_hash_chain_never_writes_output(
    tmp_path,
    mutation,
    expected,
):
    journal = tmp_path / "oms.jsonl"
    output = tmp_path / "calibration.json"
    records = _chain(_sample_entries(_ready_samples()))
    mutation(records)
    _write_records(journal, records)

    with pytest.raises(CalibrationArtifactError, match=expected):
        build_rpi_calibration_artifact(
            journal,
            output,
            symbol=SYMBOL,
        )

    assert not output.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", "chronoshft.rpi_exposure_sample.v0"),
        ("strategy", "glft"),
        ("data_source", "PUBLIC_AGG_TRADE"),
        ("units_version", "price_ticks"),
        ("formula_version", "glft.legacy"),
    ],
)
def test_rehashed_sample_identity_mismatch_is_rejected(
    tmp_path,
    field,
    value,
):
    journal = tmp_path / "oms.jsonl"
    output = tmp_path / "calibration.json"
    samples = _ready_samples()
    samples[0][field] = value
    _write_records(journal, _chain(_sample_entries(samples)))

    with pytest.raises(
        CalibrationArtifactError,
        match=f"RPI exposure {field} must exactly equal",
    ):
        build_rpi_calibration_artifact(
            journal,
            output,
            symbol=SYMBOL,
        )

    assert not output.exists()


@pytest.mark.parametrize(
    "client_oid",
    ["", "contains space", "x" * 37, "订单-1"],
)
def test_invalid_client_oid_is_rejected(tmp_path, client_oid):
    journal = tmp_path / "oms.jsonl"
    output = tmp_path / "calibration.json"
    samples = _ready_samples()
    samples[0]["client_oid"] = client_oid
    _write_records(journal, _chain(_sample_entries(samples)))

    with pytest.raises(CalibrationArtifactError, match="invalid client_oid"):
        build_rpi_calibration_artifact(
            journal,
            output,
            symbol=SYMBOL,
        )

    assert not output.exists()


def test_duplicate_client_oid_is_rejected_even_with_valid_chain(tmp_path):
    journal = tmp_path / "oms.jsonl"
    output = tmp_path / "calibration.json"
    samples = _ready_samples()
    samples[1]["client_oid"] = samples[0]["client_oid"]
    _write_records(journal, _chain(_sample_entries(samples)))

    with pytest.raises(CalibrationArtifactError, match="duplicate client_oid"):
        build_rpi_calibration_artifact(
            journal,
            output,
            symbol=SYMBOL,
        )

    assert not output.exists()


def test_sample_without_order_lifecycle_evidence_is_rejected(tmp_path):
    journal = tmp_path / "oms.jsonl"
    output = tmp_path / "calibration.json"
    sample = _sample(0, 0.0, 0)
    _write_records(journal, _chain([(SAMPLE_KIND, sample)]))

    with pytest.raises(
        CalibrationArtifactError,
        match="no order lifecycle evidence",
    ):
        build_rpi_calibration_artifact(journal, output, symbol=SYMBOL)

    assert not output.exists()


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("strategy_id", "OtherStrategy", "strategy_id"),
        ("symbol", "BTCUSDT", "order_snapshot symbol"),
        ("time_in_force", "GTX", "time_in_force"),
        ("is_post_only", False, "is_post_only"),
        ("policy", "AGGRESSIVE", "policy"),
    ],
)
def test_order_snapshot_must_prove_rpi_glft_symbol_identity(
    tmp_path,
    field,
    value,
    expected,
):
    journal = tmp_path / "oms.jsonl"
    output = tmp_path / "calibration.json"
    entries = _sample_entries([_sample(0, 0.0, 0)])
    first_snapshot = next(
        payload
        for kind, payload in entries
        if kind == "order_snapshot"
    )
    first_snapshot["intent"][field] = value
    _write_records(journal, _chain(entries))

    with pytest.raises(CalibrationArtifactError, match=expected):
        build_rpi_calibration_artifact(journal, output, symbol=SYMBOL)

    assert not output.exists()


def test_local_intent_and_terminal_without_true_exchange_ack_is_rejected(
    tmp_path,
):
    journal = tmp_path / "oms.jsonl"
    output = tmp_path / "calibration.json"
    entries = [
        entry
        for entry in _sample_entries([_sample(0, 0.0, 0)])
            if not (
                entry[0] == "order_snapshot"
                and entry[1].get("status") == "PENDING_ACK"
            )
    ]
    _write_records(journal, _chain(entries))

    with pytest.raises(
        CalibrationArtifactError,
        match="exactly one true REST PENDING_ACK",
    ):
        build_rpi_calibration_artifact(journal, output, symbol=SYMBOL)

    assert not output.exists()


def test_missing_matching_terminal_snapshot_is_rejected(tmp_path):
    journal = tmp_path / "oms.jsonl"
    output = tmp_path / "calibration.json"
    entries = [
        entry
        for entry in _sample_entries([_sample(0, 0.0, 0)])
        if not (
            entry[0] == "order_snapshot"
            and entry[1].get("status") == "CANCELLED"
        )
    ]
    _write_records(journal, _chain(entries))

    with pytest.raises(
        CalibrationArtifactError,
        match="missing matching terminal",
    ):
        build_rpi_calibration_artifact(journal, output, symbol=SYMBOL)

    assert not output.exists()


def test_sample_terminal_status_must_match_terminal_snapshot(tmp_path):
    journal = tmp_path / "oms.jsonl"
    output = tmp_path / "calibration.json"
    entries = _sample_entries([_sample(0, 0.0, 0)])
    terminal = next(
        payload
        for kind, payload in entries
        if kind == "order_snapshot" and payload["status"] == "CANCELLED"
    )
    terminal["status"] = "EXPIRED"
    terminal["last_exchange_status"] = "EXPIRED"
    terminal["extra"]["exchange_status"] = "EXPIRED"
    _write_records(journal, _chain(entries))

    with pytest.raises(
        CalibrationArtifactError,
        match="missing matching terminal",
    ):
        build_rpi_calibration_artifact(journal, output, symbol=SYMBOL)

    assert not output.exists()


def test_self_reported_fill_count_must_match_execution_record_count(tmp_path):
    journal = tmp_path / "oms.jsonl"
    output = tmp_path / "calibration.json"
    entries = _sample_entries([_sample(0, 0.0, 1)])
    sample_payload = next(
        payload for kind, payload in entries if kind == SAMPLE_KIND
    )
    sample_payload["fill_count"] = 2
    _write_records(journal, _chain(entries))

    with pytest.raises(CalibrationArtifactError, match="fill_count mismatch"):
        build_rpi_calibration_artifact(journal, output, symbol=SYMBOL)

    assert not output.exists()


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("cum_filled_qty", 2.0, "cumulative quantity mismatch"),
        ("fill_price", 2100.0, "cumulative_cost"),
        ("is_maker", False, "maker=true"),
        ("strategy_id", "OtherStrategy", "strategy_id"),
        ("symbol", "BTCUSDT", "execution_record symbol"),
    ],
)
def test_execution_records_are_cross_checked_with_terminal_fill(
    tmp_path,
    field,
    value,
    expected,
):
    journal = tmp_path / "oms.jsonl"
    output = tmp_path / "calibration.json"
    entries = _sample_entries([_sample(0, 0.0, 1)])
    execution = next(
        payload for kind, payload in entries if kind == "execution_record"
    )
    execution[field] = value
    _write_records(journal, _chain(entries))

    with pytest.raises(CalibrationArtifactError, match=expected):
        build_rpi_calibration_artifact(journal, output, symbol=SYMBOL)

    assert not output.exists()


def test_lifecycle_evidence_after_sample_is_rejected_as_inverted(tmp_path):
    journal = tmp_path / "oms.jsonl"
    output = tmp_path / "calibration.json"
    entries = _sample_entries([_sample(0, 0.0, 0)])
    sample_entry = next(entry for entry in entries if entry[0] == SAMPLE_KIND)
    reordered = [
        entries[0],
        sample_entry,
        *(entry for entry in entries[1:] if entry is not sample_entry),
    ]
    _write_records(journal, _chain(reordered))

    with pytest.raises(
        CalibrationArtifactError,
        match=(
            "timestamps must be non-decreasing|"
            "lifecycle evidence must precede"
        ),
    ):
        build_rpi_calibration_artifact(journal, output, symbol=SYMBOL)

    assert not output.exists()


def test_execution_after_terminal_snapshot_is_rejected_as_inverted(tmp_path):
    journal = tmp_path / "oms.jsonl"
    output = tmp_path / "calibration.json"
    entries = _sample_entries([_sample(0, 0.0, 1)])
    execution_entry = next(
        entry for entry in entries if entry[0] == "execution_record"
    )
    sample_index = next(
        index
        for index, entry in enumerate(entries)
        if entry[0] == SAMPLE_KIND
    )
    entries.remove(execution_entry)
    entries.insert(sample_index - 1, execution_entry)
    _write_records(journal, _chain(entries))

    with pytest.raises(
        CalibrationArtifactError,
        match="after terminal snapshot",
    ):
        build_rpi_calibration_artifact(journal, output, symbol=SYMBOL)

    assert not output.exists()


def test_execution_after_sample_is_rejected_as_inverted(tmp_path):
    journal = tmp_path / "oms.jsonl"
    output = tmp_path / "calibration.json"
    entries = _sample_entries([_sample(0, 0.0, 1)])
    execution_entry = next(
        entry for entry in entries if entry[0] == "execution_record"
    )
    entries.remove(execution_entry)
    entries.append(execution_entry)
    _write_records(journal, _chain(entries))

    with pytest.raises(
        CalibrationArtifactError,
        match=(
            "timestamps must be non-decreasing|"
            "execution evidence must precede"
        ),
    ):
        build_rpi_calibration_artifact(journal, output, symbol=SYMBOL)

    assert not output.exists()


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("depth_bps", -1.0, "non-negative and finite"),
        ("depth_bps", "1.0", "JSON number"),
        ("exposure_seconds", 0.0, "positive and finite"),
        ("fill_count", -1, "non-negative integer"),
        ("fill_count", 1.0, "non-negative integer"),
    ],
)
def test_invalid_sample_math_is_rejected_before_estimation(
    tmp_path,
    field,
    value,
    expected,
):
    journal = tmp_path / "oms.jsonl"
    output = tmp_path / "calibration.json"
    samples = _ready_samples()
    samples[0][field] = value
    _write_records(journal, _chain(_sample_entries(samples)))

    with pytest.raises(CalibrationArtifactError, match=expected):
        build_rpi_calibration_artifact(
            journal,
            output,
            symbol=SYMBOL,
        )

    assert not output.exists()


def test_non_ready_poisson_fit_fails_closed_without_output(tmp_path):
    journal = tmp_path / "oms.jsonl"
    output = tmp_path / "calibration.json"
    samples = []
    index = 0
    for depth_bps, fill_count in ((0.0, 1), (1.0, 2), (2.0, 3)):
        for _ in range(10):
            samples.append(_sample(index, depth_bps, fill_count))
            index += 1
    _write_records(journal, _chain(_sample_entries(samples)))

    with pytest.raises(
        CalibrationArtifactError,
        match="not READY: FIT_FAILED",
    ):
        build_rpi_calibration_artifact(
            journal,
            output,
            symbol=SYMBOL,
        )

    assert not output.exists()
    assert not list(tmp_path.glob(".calibration.json.*.tmp"))


def test_only_non_sample_records_are_not_accepted_as_calibration(tmp_path):
    journal = tmp_path / "oms.jsonl"
    output = tmp_path / "calibration.json"
    records = _chain(
        [("order_snapshot", {"client_oid": f"ignored-{index}"}) for index in range(30)]
    )
    _write_records(journal, records)

    with pytest.raises(
        CalibrationArtifactError,
        match="contains no rpi_exposure_sample",
    ):
        build_rpi_calibration_artifact(
            journal,
            output,
            symbol=SYMBOL,
        )

    assert not output.exists()


def test_legacy_or_truncated_non_v2_input_is_rejected(tmp_path):
    journal = tmp_path / "oms.jsonl"
    output = tmp_path / "calibration.json"
    journal.write_text(
        json.dumps(
            {
                "kind": SAMPLE_KIND,
                "payload": _ready_samples()[0],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(CalibrationArtifactError, match="record keys"):
        build_rpi_calibration_artifact(
            journal,
            output,
            symbol=SYMBOL,
        )

    assert not output.exists()
